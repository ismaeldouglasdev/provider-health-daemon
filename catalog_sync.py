"""9router catalog sync — keep downstream catalogs consistent with real availability.

Bridges the daemon and the 9router's disabled-model registry:

  - When the daemon observes a model the upstream provider no longer serves
    (model_not_found / model_not_supported / model_deprecated), it pushes a
    disable to the 9router via the CLI admin API, so the 9router catalog stops
    advertising the dead model to every client (not just this daemon).
  - A GET helper exists for diagnostics (dashboard, debugging).

Auth: the 9router CLI admin API is protected by a derived token
      sha256(machine_id + "9r-cli-auth" + cli_secret)[:16] sent as
      `x-9r-cli-token`. Both secrets live under ~/.9router.
"""

import hashlib
import json
import logging
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from config import NINEROUTER_URL
from provider_aliases import normalize_provider

log = logging.getLogger(__name__)

_MACHINE_ID_FILE = Path.home() / ".9router" / "machine-id"
_CLI_SECRET_FILE = Path.home() / ".9router" / "auth" / "cli-secret"

# Error types that mean "this model id no longer exists / is not servable"
# upstream. These are model_specific with long cooldowns (see error_parser).
DEAD_MODEL_ERRORS = {"model_not_found", "model_not_supported", "model_deprecated"}

# Errors that are model-specific but NOT dead (e.g. context too long, rate
# limited model, function not found) — these must NOT trigger a catalog disable.
_NON_DEAD_SPECIFIC = {
    "subscription_level",
    "context_length",
    "context_length_nvidia",
    "function_not_found",
    "rate_limit_tpd",
    "rate_limit_rpm",
}


def _load_secret(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except (OSError, ValueError):
        return None


def compute_cli_token() -> str | None:
    """Derive the 9router CLI admin token from local secrets."""
    machine_id = _load_secret(_MACHINE_ID_FILE)
    cli_secret = _load_secret(_CLI_SECRET_FILE)
    if not machine_id or not cli_secret:
        log.warning("9router machine-id/cli-secret not found; catalog sync disabled")
        return None
    raw = hashlib.sha256(
        f"{machine_id}9r-cli-auth{cli_secret}".encode()
    ).hexdigest()
    return raw[:16]


def _admin_request(method: str, path: str, payload: dict | None = None) -> dict:
    """Raw call to the 9router CLI admin API. Returns parsed JSON or {}."""
    token = compute_cli_token()
    if not token:
        return {}
    url = f"{NINEROUTER_URL}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {
        "Content-Type": "application/json",
        "x-9r-cli-token": token,
    }
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read()
            return json.loads(body) if body else {}
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as e:
        log.warning(f"9router admin {method} {path} failed: {e}")
        return {}


def get_disabled_models(provider: str | None = None) -> list[str] | dict:
    """Read the 9router disabled-model registry.

    provider: if given, returns the list of disabled model ids for that
              provider; otherwise returns {provider: [model ids...]}.
    """
    path = "/api/models/disabled"
    if provider:
        path += f"?providerAlias={urllib.parse.quote(normalize_provider(provider))}"
    result = _admin_request("GET", path)
    if provider:
        return result.get("ids", []) if isinstance(result, dict) else []
    return result.get("disabled", {}) if isinstance(result, dict) else {}


def disable_models(provider: str, ids: list[str]) -> bool:
    """Ask the 9router to stop advertising these models.

    Returns True on success (HTTP 200 + success:true).
    """
    if not ids:
        return False
    provider = normalize_provider(provider)
    result = _admin_request(
        "POST", "/api/models/disabled", {"providerAlias": provider, "ids": ids}
    )
    ok = bool(result.get("success"))
    if ok:
        log.info(
            "9router catalog sync: disabled models",
            extra={"event": "catalog_disable", "provider": provider, "models": ids},
        )
    else:
        log.warning(
            "9router catalog sync: disable rejected",
            extra={"event": "catalog_disable_failed", "provider": provider, "models": ids, "response": result},
        )
    return ok


def enable_models(provider: str, ids: list[str]) -> bool:
    """Re-enable models on the 9router (undo a disable)."""
    provider = normalize_provider(provider)
    ok = True
    for model_id in ids:
        path = f"/api/models/disabled?providerAlias={urllib.parse.quote(provider)}&id={urllib.parse.quote(model_id)}"
        result = _admin_request("DELETE", path)
        if not result.get("success"):
            ok = False
    if ok and ids:
        log.info(
            "9router catalog sync: enabled models",
            extra={"event": "catalog_enable", "provider": provider, "models": ids},
        )
    return ok


def is_dead_model_error(error_info: dict) -> bool:
    """True if a parsed error means the model id itself is dead upstream."""
    cd = error_info.get("cooldown") or error_info
    etype = cd.get("type", "")
    return etype in DEAD_MODEL_ERRORS


def sync_disable_dead_model(provider: str, model: str, error_info: dict) -> bool:
    """Disable a dead model on the 9router (non-blocking).

    Only acts on dead-model errors (model_not_found / model_not_supported /
    model_deprecated) with a real provider+model pair. Runs in a daemon thread
    so request handling is never delayed by the sync call.
    """
    if not provider or not model or "/" not in model:
        return False
    if not is_dead_model_error(error_info):
        return False

    def _do():
        try:
            disable_models(provider, [model])
            _invalidate_combo_cache()
        except Exception as e:  # never let sync break request handling
            log.error(f"Catalog sync error: {e}")

    threading.Thread(target=_do, daemon=True, name="catalog-sync").start()
    return True


def _invalidate_combo_cache() -> None:
    """Force SmartRouter to refetch the catalog after a disable."""
    try:
        from smart_router import SmartRouter
        SmartRouter.invalidate_combo_cache()
    except ImportError:
        pass
