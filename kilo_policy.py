"""Read-only Kilo Gateway policy catalog cache.

Kilo exposes mayTrainOnYourPrompts in its live model catalog. We cache only
model IDs and policy flags; prompts and responses are never stored or sent.
"""

import json
import logging
import threading
import time
import urllib.request

log = logging.getLogger(__name__)

CATALOG_URL = "https://api.kilo.ai/api/gateway/models"
_REFRESH_SECONDS = 600
_LOCK = threading.Lock()
_POLICIES: dict[str, bool] = {}
_LAST_REFRESH = 0.0


def refresh(force: bool = False, timeout: float = 8.0) -> int:
    global _LAST_REFRESH, _POLICIES
    now = time.monotonic()
    with _LOCK:
        if not force and now - _LAST_REFRESH < _REFRESH_SECONDS:
            return len(_POLICIES)
    try:
        req = urllib.request.Request(CATALOG_URL, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read())
        data = payload.get("data", []) if isinstance(payload, dict) else []
        policies = {
            str(item.get("id")): bool(item.get("mayTrainOnYourPrompts"))
            for item in data
            if isinstance(item, dict) and item.get("id")
            and "mayTrainOnYourPrompts" in item
        }
        with _LOCK:
            _POLICIES = policies
            _LAST_REFRESH = time.monotonic()
        log.info("KILO_POLICY_REFRESH models=%d", len(policies), extra={"event": "kilo_policy_refresh"})
        return len(policies)
    except Exception as exc:
        log.warning("KILO_POLICY_REFRESH_FAILED error=%s", exc)
        return len(_POLICIES)


def get(model: str) -> bool | None:
    with _LOCK:
        return _POLICIES.get((model or "").strip())


def snapshot() -> dict[str, bool]:
    with _LOCK:
        return dict(_POLICIES)
