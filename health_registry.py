"""Provider health registry — persistent state for cooldown management."""

import copy
import json
import logging
import os
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from cooldown import CooldownCalculator
from config import HEALTH_FILE, PROBER_INTERVAL_MINUTES, PROVIDER_DENYLIST
from provider_aliases import normalize_provider

log = logging.getLogger(__name__)


class HealthRegistry:
    """Manages provider/model health state, persisted to JSON."""

    PROVIDERS = "providers"
    MODELS = "models"
    ACCOUNTS = "accounts"

    MAX_FAILURES = 10  # after this many consecutive failures, permanently disable (reduced from 30: gpt-oss-120b accumulated 30 rate_limit failures before being blocked, wasting user prompts)
    # Fresh window granted when cooldown → probing: the recovery prober runs
    # every PROBER_INTERVAL_MINUTES, so 2x that interval guarantees it has a
    # chance to test the provider before the entry becomes orphan-cleanup eligible.
    PROBING_WINDOW_MINUTES = max(PROBER_INTERVAL_MINUTES * 2, 10)

    def __init__(self, filepath=None):
        self.filepath = Path(filepath) if filepath else HEALTH_FILE
        self.cooldown = CooldownCalculator(max_failures=self.MAX_FAILURES)
        self._lock = threading.RLock()
        self._data = self._load()

    def _empty_state(self) -> dict:
        return {self.PROVIDERS: {}, self.MODELS: {}, self.ACCOUNTS: {}}

    def _load(self) -> dict:
        if not self.filepath.exists():
            return self._empty_state()
        try:
            data = json.loads(self.filepath.read_text())
            # Validate structure
            data.setdefault(self.PROVIDERS, {})
            data.setdefault(self.MODELS, {})
            data.setdefault(self.ACCOUNTS, {})
            self._migrate_duplicate_keys(data)
            denied = [k for k in data.get(self.PROVIDERS, {}) if PROVIDER_DENYLIST.match(k)]
            for k in denied:
                del data[self.PROVIDERS][k]
            denied_m = [k for k in data.get(self.MODELS, {}) if PROVIDER_DENYLIST.match(k.split('/')[0])]
            for k in denied_m:
                del data[self.MODELS][k]
            denied_a = [k for k in data.get(self.ACCOUNTS, {}) if PROVIDER_DENYLIST.match(k)]
            for k in denied_a:
                del data[self.ACCOUNTS][k]
            if denied or denied_m or denied_a:
                log.warning(f"purged denied providers from health file: {denied + denied_m + denied_a}")
            return data
        except (json.JSONDecodeError, IOError) as e:
            log.warning(f"Failed to load health file: {e}")
            return self._empty_state()

    def _migrate_duplicate_keys(self, data: dict) -> None:
        providers = data.get(self.PROVIDERS, {})
        merged: dict = {}
        for raw_key, entry in providers.items():
            canonical = normalize_provider(raw_key)
            if canonical != raw_key:
                log.info(
                    f"Migrating health entry '{raw_key}' -> '{canonical}'"
                )
            if canonical not in merged:
                merged[canonical] = entry
            else:
                existing = merged[canonical]
                merged[canonical] = self._merge_entries(existing, entry)
        data[self.PROVIDERS] = merged

    @staticmethod
    def _merge_entries(a: dict, b: dict) -> dict:
        rank = {"disabled": 3, "cooldown": 2, "probing": 1, "healthy": 0}
        ra = rank.get(str(a.get("status")), 0)
        rb = rank.get(str(b.get("status")), 0)
        if rb > ra:
            a, b = b, a
        merged = copy.deepcopy(a)
        merged["failures"] = max(a.get("failures", 0), b.get("failures", 0))
        models = set(a.get("models") or []) | set(b.get("models") or [])
        if models:
            merged["models"] = sorted(models)
        if b.get("updated_at") and (
            not merged.get("updated_at") or b["updated_at"] < merged["updated_at"]
        ):
            merged["updated_at"] = b["updated_at"]
        return merged

    def _save(self) -> None:
        self.filepath.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(self._data, indent=2, default=str)
        # Atomic write: tmp file in the SAME directory (same filesystem so
        # os.replace is atomic) + fsync before rename. A crash mid-write then
        # leaves the previous valid file instead of a truncated/corrupt one.
        tmp = self.filepath.with_name(self.filepath.name + ".tmp")
        try:
            with tmp.open("w") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.filepath)
            self._rotate_backup()
        except OSError as e:
            log.error(f"Failed to save health file: {e}")
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _rotate_backup(self) -> None:
        """Um snapshot por dia (health.json.bak-YYYYMMDD), 7 dias de retenção."""
        today = time.strftime("%Y%m%d")
        bak = self.filepath.with_name(f"{self.filepath.name}.bak-{today}")
        if not bak.exists():
            try:
                shutil.copy2(self.filepath, bak)
            except OSError as e:
                log.warning(f"backup rotation skipped: {e}")
        prefix = f"{self.filepath.name}.bak-"
        cutoff = time.strftime("%Y%m%d", time.localtime(time.time() - 7 * 86400))
        for old in self.filepath.parent.glob(prefix + "*"):
            day = old.name[len(prefix):]
            if len(day) == 8 and day.isdigit() and day < cutoff:
                try:
                    old.unlink()
                except OSError:
                    pass

    # ── Read API ─────────────────────────────────────────────────────

    def get_provider(self, name: str) -> dict:
        with self._lock:
            return self._data[self.PROVIDERS].get(normalize_provider(name), {})

    def get_model(self, model_id: str) -> dict:
        with self._lock:
            return self._data[self.MODELS].get(model_id, {})

    def is_provider_healthy(self, name: str) -> bool:
        """Provider is usable right now (probing counts as semi-available).

        Must stay consistent with ``is_model_available``/``_entry_is_healthy``:
        providers transition to ``probing`` right after a cooldown expires and
        are selectable again — if this method only accepted ``healthy``, the
        smart router would reject every candidate of a probing provider and
        fall through to the downstream combo router, which then fails with
        no_credentials/monthly_limit ("provider temporarily unavailable").
        """
        entry = self.get_provider(name)
        if not entry:
            return True  # unknown = assume healthy
        return self._entry_is_healthy(entry)

    def is_model_available(self, model_id: str) -> bool:
        """Model is usable right now (checks both model-specific and parent provider)."""
        # Check model-specific entry
        model_entry = self.get_model(model_id)
        if model_entry and not self._entry_is_healthy(model_entry):
            return False

        # Check parent provider
        provider = model_id.split("/")[0]
        provider_entry = self.get_provider(provider)
        if provider_entry and not self._entry_is_healthy(provider_entry):
            return False

        return True

    def get_available_models(self, model_ids: list[str]) -> list[str]:
        """Filter model_ids to only those currently available."""
        return [m for m in model_ids if self.is_model_available(m)]

    def _entry_is_healthy(self, entry: dict) -> bool:
        if entry.get("status") == "healthy":
            return True
        if entry.get("status") == "probing":
            return True  # probing is semi-available
        if entry.get("status") == "disabled":
            return False
        if entry.get("status") == "cooldown":
            return self.cooldown.is_expired(entry.get("until"))
        return False

    # ── Write API ────────────────────────────────────────────────────

    def mark_healthy(self, provider: str, model: Optional[str] = None) -> None:
        """Record successful request."""
        provider = normalize_provider(provider)
        if PROVIDER_DENYLIST.match(provider):
            return
        with self._lock:
            if model:
                self._data[self.MODELS][model] = self._healthy_entry(provider, model)

            entry = self._healthy_entry(provider)
            self._data[self.PROVIDERS][provider] = entry
            self._save()
            log.debug(f"✓ {provider}{'/' + model if model else ''} → healthy")

    def mark_error(
        self,
        provider: str,
        error_info: dict,
        model: Optional[str] = None,
    ) -> None:
        """Apply cooldown from parsed error."""
        provider = normalize_provider(provider)
        if PROVIDER_DENYLIST.match(provider):
            return
        with self._lock:
            if error_info.get("model_specific") and model:
                current = self.get_model(model)
                current_failures = current.get("failures", 0)
                current_until = None
                if current.get("until"):
                    try:
                        current_until = datetime.fromisoformat(current["until"])
                    except ValueError:
                        pass
                result = self.cooldown.calculate(
                    error_info, current_failures, current_until
                )
                entry = {
                    "status": "disabled" if result["permanent"] else "cooldown",
                    "until": result.get("until"),
                    "reason": result["type"],
                    "failures": result["failures"],
                    "backoff_applied": result["backoff_applied"],
                    "provider": provider,
                    "model": model,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
                self._data[self.MODELS][model] = entry
                log.info(f"⚠ {model} → cooldown {result['duration_hours']:.1f}h ({result['type']})")
            else:
                # Provider-wide
                current = self.get_provider(provider)
                current_failures = current.get("failures", 0)
                current_until = None
                if current.get("until"):
                    try:
                        current_until = datetime.fromisoformat(current["until"])
                    except ValueError:
                        pass
                result = self.cooldown.calculate(
                    error_info, current_failures, current_until
                )
                entry = {
                    "status": "disabled" if result["permanent"] else "cooldown",
                    "until": result.get("until"),
                    "reason": result["type"],
                    "failures": result["failures"],
                    "backoff_applied": result["backoff_applied"],
                    "models": list(
                        set(self._provider_models(provider))  # inherit existing
                    ),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
                self._data[self.PROVIDERS][provider] = entry
                log.info(f"⚠ {provider} → cooldown {result['duration_hours']:.1f}h ({result['type']})")

            self._save()

    @staticmethod
    def _healthy_entry(provider: str, model: Optional[str] = None) -> dict:
        return {
            "status": "healthy",
            "until": None,
            "reason": None,
            "failures": 0,
            "provider": provider,
            "model": model,
        }

    def _provider_models(self, provider: str) -> list[str]:
        """List model entries belonging to provider."""
        canonical = normalize_provider(provider)
        return [
            model_id
            for model_id, entry in self._data[self.MODELS].items()
            if normalize_provider(str(entry.get("provider", ""))) == canonical
        ]

    # ── Account Pools ─────────────────────────────────────────────────

    def sync_accounts(self, accounts_by_provider: dict) -> None:
        """Merge per-provider account pool state from discovery.

        Accounts are stored as a list per provider, matched by ``id``: fields the
        discovery does not send (e.g. a manual flag written by an admin tool)
        survive a re-sync, while incoming values win for overlapping keys.

        Args:
            accounts_by_provider: {normalized_prefix: [account_dicts]} as produced
                by provider_discovery.fetch_provider_connections (each account has
                id/name/status/errorCode/backoffLevel/isActive/lastUsedAt/model_locks).
        """
        with self._lock:
            for prefix, accounts in (accounts_by_provider or {}).items():
                if PROVIDER_DENYLIST.match(prefix):
                    continue
                if not isinstance(accounts, list):
                    continue
                existing = {
                    a.get("id"): a
                    for a in self._data[self.ACCOUNTS].get(prefix, [])
                    if isinstance(a, dict) and a.get("id") is not None
                }
                merged = []
                for a in accounts:
                    if not isinstance(a, dict):
                        continue
                    aid = a.get("id")
                    if aid is not None and aid in existing:
                        base = dict(existing[aid])
                        base.update(a)
                        merged.append(base)
                    else:
                        merged.append(a)
                self._data[self.ACCOUNTS][prefix] = merged
            self._save()

    def get_accounts(self, provider: str) -> list:
        """Return the account pool for a provider (empty list if none)."""
        with self._lock:
            return list(self._data[self.ACCOUNTS].get(normalize_provider(provider), []))

    def account_summary(self, provider: str) -> dict:
        """Aggregate the account pool for a provider.

        Returns:
            {"count", "active", "locked", "backoff", "status"} where:
            - active: accounts with status not in ("error", "unavailable")
            - locked: accounts with any active model_lock
            - backoff: accounts with backoffLevel > 0
            - status: worst-case status across the pool ("healthy" if none bad)
        """
        accounts = self.get_accounts(provider)
        if not accounts:
            return {"count": 0, "active": 0, "locked": 0, "backoff": 0, "status": "healthy"}
        active = 0
        locked = 0
        backoff = 0
        worst = "healthy"
        for a in accounts:
            st = a.get("status", "active")
            if st in ("error", "unavailable"):
                worst = "unavailable"
            else:
                active += 1
            if a.get("model_locks"):
                locked += 1
            if a.get("backoffLevel", 0) > 0:
                backoff += 1
        return {
            "count": len(accounts),
            "active": active,
            "locked": locked,
            "backoff": backoff,
            "status": worst,
        }

    # ── Admin ─────────────────────────────────────────────────────────

    def force_healthy(self, provider: str, model: Optional[str] = None) -> None:
        """Admin override: reset to healthy."""
        provider = normalize_provider(provider)
        with self._lock:
            if model:
                self._data[self.MODELS].pop(model, None)
            self._data[self.PROVIDERS].pop(provider, None)
            self.mark_healthy(provider, model)

    def _garbage_entry(self, entry: dict) -> bool:
        """Check if entry is garbage (missing required fields)."""
        if entry.get("until") is None and entry.get("status") not in ("healthy", "disabled"):
            return True
        return False

    def cleanup_expired(self) -> int:
        """Promote expired cooldowns to probing. Returns count of promotions."""
        with self._lock:
            count = 0
            now = datetime.now(timezone.utc)

            for provider, entry in list(self._data[self.PROVIDERS].items()):
                if self._garbage_entry(entry):
                    del self._data[self.PROVIDERS][provider]
                    log.info(f"Removed garbage entry for {provider}")
                    continue
                if entry.get("status") == "cooldown" and self.cooldown.is_expired(
                    entry.get("until")
                ):
                    failures = entry.get("failures", 0)
                    if failures >= self.MAX_FAILURES:
                        entry["status"] = "disabled"
                        entry["reason"] = f"{entry.get('reason')} (max_failures)"
                        entry["until"] = None
                        log.info(f"Lock {provider} cooldown expired -> disabled ({failures} failures)")
                    else:
                        entry["status"] = "probing"
                        entry["reason"] = f"{entry.get('reason')} (probing)"
                        entry["until"] = (
                            now + timedelta(minutes=self.PROBING_WINDOW_MINUTES)
                        ).isoformat()
                        entry["updated_at"] = now.isoformat()
                        log.info(f"Rotate {provider} cooldown expired -> probing")
                    count += 1
                elif entry.get("status") == "probing" and self.cooldown.is_expired(
                    entry.get("until")
                ):
                    # Probing window expired → revert to healthy (unknown).
                    # Without this, providers removed from the catalog but
                    # left in health.json get stuck in "probing" forever
                    # (until stale, never re-tested, never cleaned up).
                    del self._data[self.PROVIDERS][provider]
                    log.info(
                        f"Removed orphaned probing entry for {provider} "
                        f"(until expired {(now - datetime.fromisoformat(entry['until'].replace('Z','+00:00'))).days}d ago)"
                    )
                    count += 1

            for model_id, entry in list(self._data[self.MODELS].items()):
                if self._garbage_entry(entry):
                    del self._data[self.MODELS][model_id]
                    log.info(f"Removed garbage entry for {model_id}")
                    continue
                if entry.get("status") == "cooldown" and self.cooldown.is_expired(
                    entry.get("until")
                ):
                    failures = entry.get("failures", 0)
                    if failures >= self.MAX_FAILURES:
                        entry["status"] = "disabled"
                        entry["reason"] = f"{entry.get('reason')} (max_failures)"
                        entry["until"] = None
                        log.info(f"Lock {model_id} cooldown expired -> disabled ({failures} failures)")
                    else:
                        entry["status"] = "probing"
                        entry["reason"] = f"{entry.get('reason')} (probing)"
                        entry["until"] = (
                            now + timedelta(minutes=self.PROBING_WINDOW_MINUTES)
                        ).isoformat()
                        entry["updated_at"] = now.isoformat()
                        log.info(f"Rotate {model_id} cooldown expired -> probing")
                    count += 1
                elif entry.get("status") == "probing" and self.cooldown.is_expired(
                    entry.get("until")
                ):
                    del self._data[self.MODELS][model_id]
                    log.info(f"Removed orphaned probing entry for model {model_id}")
                    count += 1

            if count > 0:
                self._save()
        return count

    def status_summary(self) -> dict:
        """Return counts per status."""
        with self._lock:
            statuses = {"healthy": 0, "cooldown": 0, "probing": 0, "disabled": 0}
            for entry in self._data[self.PROVIDERS].values():
                st = entry.get("status", "healthy")
                statuses[st] = statuses.get(st, 0) + 1

            expired = sum(
                1
                for e in self._data[self.PROVIDERS].values()
                if e.get("status") == "cooldown" and self.cooldown.is_expired(e.get("until"))
            )
            return {"by_status": statuses, "expired_ready": expired}

    def snapshot(self) -> dict:
        """Deep copy of full state, safe for concurrent readers (dashboard, alerter)."""
        with self._lock:
            return copy.deepcopy(self._data)