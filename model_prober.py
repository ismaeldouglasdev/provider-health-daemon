"""Fractionated model prober — proactive per-model recovery testing.

Problem it fixes (observed 2026-09-08): the recovery prober only tests
PROVIDERS — one working model marks the whole provider healthy while its
other broken models keep burning 10-45s per combo request. Model-level
cooldowns (from 429/403/empty-200 with "reset after Xm" parsed by
error_parser) expire silently: nothing re-tests the model, so the SmartRouter
keeps skipping it until a real user request wastes a slot discovering it
recovered (or stays broken).

Design (user request, 2026-09-08): "testagem fracionada" + "leitura do tempo
de rate limiting":
  1. Scan health-registry MODELS for cooldown/probing entries with `until`.
  2. When `until` expires (the rate-limit window the provider itself told
     us), schedule a re-test — never before, respecting the limiter.
  3. Fraction: test at most MODELS_PROBE_BATCH models per cycle, 2s apart,
     cycling fairly so every due model is eventually re-tested.
  4. Outcome: success → mark_healthy(model) — back in the pool instantly;
     failure → mark_error(model) — cooldown recomputed with exponential
     backoff + the NEW "reset after" if the upstream advertises one.
  5. Permanent death: failures >= MAX_FAILURES → sync-disable in the 9router
     registry so it leaves the catalog/pool for good.

All probes go through the 9router (single choke point, same auth as user
traffic) with tiny max_tokens to minimize quota burn.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("model_prober")

# ── Tunables (env-overridable) ──────────────────────────────────────────
import os

MODELS_PROBE_INTERVAL_SECONDS = float(os.environ.get("MODELS_PROBE_INTERVAL_SECONDS", "60"))
MODELS_PROBE_BATCH = int(os.environ.get("MODELS_PROBE_BATCH", "3"))        # per cycle
MODELS_PROBE_GAP_SECONDS = float(os.environ.get("MODELS_PROBE_GAP_SECONDS", "2"))
MODELS_PROBE_TIMEOUT = float(os.environ.get("MODELS_PROBE_TIMEOUT", "45"))
MODELS_PROBE_MAX_TOKENS = int(os.environ.get("MODELS_PROBE_MAX_TOKENS", "4"))


def _is_empty_chat_response(body: bytes) -> bool:
    """A 200 whose choices have no usable content (dead model / exhausted quota)."""
    try:
        j = json.loads(body)
    except (ValueError, TypeError):
        return False
    if not isinstance(j, dict) or "choices" not in j:
        return False
    try:
        for ch in j["choices"]:
            msg = ch.get("message") or {}
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                return False
            if msg.get("tool_calls") or msg.get("function_call"):
                return False
        return True
    except (KeyError, TypeError, AttributeError):
        return False


class ModelProber:
    """Scans model cooldowns and re-tests due models in small batches."""

    def __init__(self, registry, ninerouter_url: str, ninerouter_key: str, metrics=None):
        self.registry = registry
        self.ninerouter_url = ninerouter_url.rstrip("/")
        self.ninerouter_key = ninerouter_key
        self.metrics = metrics  # optional MetricsStore-like counter object
        # Fairness: remember where the last cycle stopped scanning the due list
        self._cursor = 0
        # Models that recently failed a probe get a shorter revisit window
        # (avoid hammering a permanently-broken model every cycle):
        # model_id -> monotonic timestamp of last probe attempt
        self._last_probe_at: dict[str, float] = {}

    # ── Scheduling ────────────────────────────────────────────────────
    def _due_models(self, now: datetime) -> list[tuple[str, dict]]:
        """Models in cooldown/probing whose `until` has passed."""
        snap = self.registry.snapshot()
        models = snap.get(self.registry.MODELS, {}) or {}
        due: list[tuple[str, dict]] = []
        for model_id, entry in models.items():
            if not isinstance(entry, dict):
                continue
            status = entry.get("status")
            if status not in ("cooldown", "probing"):
                continue
            until_raw = entry.get("until")
            if not until_raw:
                continue
            try:
                until = datetime.fromisoformat(str(until_raw))
            except ValueError:
                continue
            if until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
            if until <= now:
                # Fairness floor: skip if probed very recently (min 60s gap
                # between attempts for the same model)
                last = self._last_probe_at.get(model_id, 0)
                if time.monotonic() - last < 60:
                    continue
                due.append((model_id, entry))
        return due

    # ── Probing ───────────────────────────────────────────────────────
    def _probe_model(self, model_id: str) -> tuple[str, dict | None]:
        """Send a minimal chat request through the 9router.

        Returns ("healthy", None) | ("error", {"status": int, "body": str}).
        """
        payload = json.dumps({
            "model": model_id,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": MODELS_PROBE_MAX_TOKENS,
            "stream": False,
        }).encode()
        req = urllib.request.Request(
            f"{self.ninerouter_url}/v1/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.ninerouter_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=MODELS_PROBE_TIMEOUT) as resp:
                body = resp.read()
                if resp.status == 200:
                    if _is_empty_chat_response(body):
                        return ("error", {"status": 200, "body": "empty_response", "empty": True})
                    return ("healthy", None)
                return ("error", {"status": resp.status, "body": body.decode("utf-8", "replace")[:500]})
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8", "replace")[:500]
            except Exception:
                body = ""
            return ("error", {"status": e.code, "body": body})
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            return ("error", {"status": 0, "body": f"probe_transport: {e}"})

    def _apply_outcome(self, model_id: str, outcome: str, err: dict | None) -> None:
        provider = model_id.split("/")[0]
        if outcome == "healthy":
            self.registry.mark_healthy(provider, model=model_id)
            log.info(
                f"Model prober: {model_id} recovered → healthy",
                extra={"event": "model_recovered", "provider": provider, "model": model_id},
            )
            if self.metrics is not None:
                try:
                    self.metrics.cooldowns_promoted += 1
                except Exception:
                    pass
            return

        # Failure → recompute cooldown (with backoff + new reset-after if any)
        from error_parser import parse_error
        error_info = parse_error(err.get("status", 0), err.get("body", ""))
        self.registry.mark_error(provider, error_info=error_info, model=model_id)
        log.info(
            f"Model prober: {model_id} still failing ({err.get('status')}) — cooldown extended",
            extra={
                "event": "model_probe_failed",
                "provider": provider,
                "model": model_id,
                "http_status": err.get("status"),
            },
        )

        # Permanent death → sync-disable upstream so it leaves the catalog
        entry = self.registry.get_model(model_id) or {}
        if entry.get("failures", 0) >= self.registry.MAX_FAILURES:
            try:
                from catalog_sync import disable_models
                if disable_models(provider, [model_id]):
                    log.info(
                        f"Model prober: {model_id} disabled in 9router catalog (max failures)",
                        extra={"event": "model_disabled_upstream", "provider": provider, "model": model_id},
                    )
            except Exception as e:
                log.debug(f"catalog disable failed for {model_id}: {e}")

    # ── Main loop ─────────────────────────────────────────────────────
    def run_forever(self, stop_event: threading.Event):
        log.info(
            "Model prober started",
            extra={
                "event": "model_prober_start",
                "interval_s": MODELS_PROBE_INTERVAL_SECONDS,
                "batch": MODELS_PROBE_BATCH,
            },
        )
        while not stop_event.is_set():
            try:
                now = datetime.now(timezone.utc)
                due = self._due_models(now)
                if due:
                    # Fraction: take a rotating slice so all due models get
                    # coverage even when the due list is long.
                    batch = self._pick_batch(due)
                    log.debug(
                        f"Model prober: {len(due)} models due, probing {len(batch)}",
                        extra={"event": "model_probe_cycle", "due": len(due), "batch": len(batch)},
                    )
                    for model_id, _entry in batch:
                        if stop_event.is_set():
                            break
                        self._last_probe_at[model_id] = time.monotonic()
                        outcome, err = self._probe_model(model_id)
                        self._apply_outcome(model_id, outcome, err)
                        if stop_event.wait(MODELS_PROBE_GAP_SECONDS):
                            break
            except Exception as e:
                log.error(f"Model prober error: {e}", extra={"event": "model_prober_error"})
            if stop_event.wait(MODELS_PROBE_INTERVAL_SECONDS):
                break
        log.info("Model prober stopped", extra={"event": "model_prober_stop"})

    def _pick_batch(self, due: list[tuple[str, dict]]) -> list[tuple[str, dict]]:
        """Rotating fair slice of the due list, size <= MODELS_PROBE_BATCH."""
        if len(due) <= MODELS_PROBE_BATCH:
            return due
        if self._cursor >= len(due):
            self._cursor = 0
        batch = due[self._cursor:self._cursor + MODELS_PROBE_BATCH]
        self._cursor += MODELS_PROBE_BATCH
        return batch


def start_model_prober(registry, ninerouter_url: str, ninerouter_key: str, metrics=None, stop_event: threading.Event | None = None) -> threading.Event:
    """Spawn the model prober thread; returns its stop event.

    A ``stop_event`` may be passed in to tie the thread's lifetime to an
    external shutdown signal (e.g. the daemon's ``shutdown_event``); when
    omitted a fresh event is created.
    """
    if stop_event is None:
        stop_event = threading.Event()
    prober = ModelProber(registry, ninerouter_url, ninerouter_key, metrics)
    t = threading.Thread(target=prober.run_forever, args=(stop_event,), daemon=True, name="model-prober")
    t.start()
    return stop_event