"""Provider health registry — persistent state for cooldown management."""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from cooldown import CooldownCalculator
from config import HEALTH_FILE

log = logging.getLogger(__name__)


class HealthRegistry:
    """Manages provider/model health state, persisted to JSON."""

    PROVIDERS = "providers"
    MODELS = "models"

    MAX_FAILURES = 30  # after this many consecutive failures, permanently disable

    def __init__(self, filepath=None):
        self.filepath = Path(filepath) if filepath else HEALTH_FILE
        self.cooldown = CooldownCalculator(max_failures=self.MAX_FAILURES)
        self._data = self._load()

    def _empty_state(self) -> dict:
        return {self.PROVIDERS: {}, self.MODELS: {}}

    def _load(self) -> dict:
        if not self.filepath.exists():
            return self._empty_state()
        try:
            data = json.loads(self.filepath.read_text())
            # Validate structure
            data.setdefault(self.PROVIDERS, {})
            data.setdefault(self.MODELS, {})
            return data
        except (json.JSONDecodeError, IOError) as e:
            log.warning(f"Failed to load health file: {e}")
            return self._empty_state()

    def _save(self) -> None:
        self.filepath.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(self._data, indent=2, default=str)
        self.filepath.write_text(text)

    # ── Read API ─────────────────────────────────────────────────────

    def get_provider(self, name: str) -> dict:
        return self._data[self.PROVIDERS].get(name, {})

    def get_model(self, model_id: str) -> dict:
        return self._data[self.MODELS].get(model_id, {})

    def is_provider_healthy(self, name: str) -> bool:
        """Provider is usable right now."""
        entry = self.get_provider(name)
        if not entry:
            return True  # unknown = assume healthy
        return entry.get("status") == "healthy"

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
        # Model-specific vs provider-wide
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
        return [
            model_id
            for model_id, entry in self._data[self.MODELS].items()
            if entry.get("provider") == provider
        ]

    # ── Admin ─────────────────────────────────────────────────────────

    def force_healthy(self, provider: str, model: Optional[str] = None) -> None:
        """Admin override: reset to healthy."""
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
        count = 0
        now = datetime.now(timezone.utc)

        for provider, entry in list(self._data[self.PROVIDERS].items()):
            if self._garbage_entry(entry):
                del self._data[self.PROVIDERS][provider]
                log.info(f"🗑️ Removed garbage entry for {provider}")
                continue
            if entry.get("status") == "cooldown" and self.cooldown.is_expired(
                entry.get("until")
            ):
                failures = entry.get("failures", 0)
                if failures >= self.MAX_FAILURES:
                    entry["status"] = "disabled"
                    entry["reason"] = f"{entry.get('reason')} (max_failures)"
                    entry["until"] = None
                    log.info(f"🔒 {provider} cooldown expired → disabled ({failures} failures)")
                else:
                    entry["status"] = "probing"
                    entry["reason"] = f"{entry.get('reason')} (probing)"
                    log.info(f"🔄 {provider} cooldown expired → probing")
                count += 1

        for model_id, entry in list(self._data[self.MODELS].items()):
            if self._garbage_entry(entry):
                del self._data[self.MODELS][model_id]
                log.info(f"🗑️ Removed garbage entry for {model_id}")
                continue
            if entry.get("status") == "cooldown" and self.cooldown.is_expired(
                entry.get("until")
            ):
                failures = entry.get("failures", 0)
                if failures >= self.MAX_FAILURES:
                    entry["status"] = "disabled"
                    entry["reason"] = f"{entry.get('reason')} (max_failures)"
                    entry["until"] = None
                    log.info(f"🔒 {model_id} cooldown expired → disabled ({failures} failures)")
                else:
                    entry["status"] = "probing"
                    entry["reason"] = f"{entry.get('reason')} (probing)"
                    log.info(f"🔄 {model_id} cooldown expired → probing")
                count += 1

        if count > 0:
            self._save()
        return count

    def status_summary(self) -> dict:
        """Return counts per status."""
        statuses = {"healthy": 0, "cooldown": 0, "probing": 0, "disabled": 0}
        for name, entry in self._data[self.PROVIDERS].items():
            st = entry.get("status", "healthy")
            statuses[st] = statuses.get(st, 0) + 1

        expired = sum(
            1
            for e in self._data[self.PROVIDERS].values()
            if e.get("status") == "cooldown" and self.cooldown.is_expired(e.get("until"))
        )
        return {"by_status": statuses, "expired_ready": expired}