"""Sanitization — validate and sanitize model IDs, URLs, and config fields.

Defense-in-depth: every external input that touches the routing layer
is validated before use.
"""

from __future__ import annotations

import re
import logging
from urllib.parse import urlparse
from typing import Optional

log = logging.getLogger(__name__)

# ── Model ID sanitization ───────────────────────────────────────────

# Allowed pattern: provider/model-name with optional @org/ prefix
# e.g. "cf/@cf/meta/llama-3.3-70b-instruct-fp8-fast"
#      "nvidia/minimaxai/minimax-m3"
#      "groq/llama-3.3-70b-versatile"
_MODEL_ID_RE = re.compile(
    r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}"
    r"(/@[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63})?"
    r"/[a-zA-Z0-9][a-zA-Z0-9/_.:-]{0,254}[a-zA-Z0-9]$"
)


def validate_model_id(model_id: str) -> bool:
    """Check if a model ID follows the expected format.

    Rules:
      - Starts with provider (alphanumeric + . _ -, 1-64 chars)
      - Optional @org section
      - Model path after /
      - Total max 512 chars
    """
    if not model_id or not isinstance(model_id, str):
        return False
    if len(model_id) > 512:
        return False
    return bool(_MODEL_ID_RE.match(model_id))


def sanitize_model_id(model_id: str) -> Optional[str]:
    """Return validated model ID, or None if invalid.

    Also strips whitespace and normalizes separators.
    """
    if not model_id or not isinstance(model_id, str):
        return None
    cleaned = model_id.strip()
    if not cleaned:
        return None
    if validate_model_id(cleaned):
        return cleaned
    return None


# ── URL sanitization ────────────────────────────────────────────────

_ALLOWED_SCHEMES = ("http", "https")

# Internal router URLs are admin-configured, not user-supplied.
# No SSRF protection needed — only basic format validation.


def validate_router_url(url: str) -> bool:
    """Check if a URL is valid for routing.

    - Only http/https schemes
    - Basic format, reasonable length (2048 chars)
    - No SSRF checks (routers are admin-configured, not user-supplied)
    """
    if not url or not isinstance(url, str):
        return False
    if len(url) > 2048:
        return False
    try:
        parsed = urlparse(url)
        if parsed.scheme not in _ALLOWED_SCHEMES:
            return False
        host = parsed.hostname or ""
        if not host:
            return False
        return True
    except Exception:
        return False


def sanitize_url(url: str) -> Optional[str]:
    """Return validated URL, or None if invalid."""
    cleaned = url.strip() if url else ""
    if not cleaned:
        return None
    if validate_router_url(cleaned):
        return cleaned.rstrip("/")
    return None


# ── Config field sanitization ───────────────────────────────────────

_MAX_ROUTER_NAME_LEN = 64


def validate_router_name(name: str) -> bool:
    """Router names: alphanumeric, hyphens, underscores, 2-64 chars."""
    if not name or not isinstance(name, str):
        return False
    if len(name) < 2 or len(name) > _MAX_ROUTER_NAME_LEN:
        return False
    return bool(re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{1,63}$", name))


def sanitize_router_name(name: str) -> Optional[str]:
    """Return validated router name, or None."""
    cleaned = name.strip() if name else ""
    if validate_router_name(cleaned):
        return cleaned
    return None


# ── Bulk sanitization ───────────────────────────────────────────────

def sanitize_router_config(config: dict) -> dict:
    """Validate a single router config dict, fixing or rejecting bad fields.

    Returns a sanitized copy with only valid fields.
    Empty dict = rejected.
    """
    result: dict = {}

    name = sanitize_router_name(config.get("name"))
    if not name:
        log.warning("Router config rejected: invalid name %r", config.get("name"))
        return result

    url = sanitize_url(config.get("url"))
    if not url:
        log.warning("Router %r rejected: invalid URL %r", name, config.get("url"))
        return result

    result["name"] = name
    result["url"] = url

    # Integer fields with safe defaults
    result["priority"] = max(1, min(100, int(config.get("priority", 1))))
    result["weight"] = max(1, min(100, int(config.get("weight", 1))))
    result["timeout"] = max(0.5, min(30.0, float(config.get("timeout", 2.0))))

    # Optional fields
    result["health_check_path"] = str(config.get("health_check_path", "/v1/models"))
    auth = config.get("auth")
    if auth and isinstance(auth, dict):
        header = str(auth.get("header", ""))
        value = str(auth.get("value", ""))
        if header and value:
            result["auth"] = {"header": header, "value": value}

    return result


def sanitize_routers_config(configs: list[dict]) -> list[dict]:
    """Sanitize a list of router configs, dropping invalid entries."""
    sanitized = []
    for cfg in configs:
        cleaned = sanitize_router_config(cfg)
        if cleaned:
            sanitized.append(cleaned)
        else:
            log.warning("Dropping invalid router config: %r", cfg)
    return sanitized
