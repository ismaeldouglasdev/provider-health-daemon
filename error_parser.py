"""Parse provider error responses → cooldown decision."""

import re
import json
import logging
from typing import Optional

from cooldown import CooldownCalculator

log = logging.getLogger(__name__)

# ── Error → cooldown duration ────────────────────────────────────────
# Format: (status, regex_pattern) → (provider_scope, model_specific, cooldown_type, hours_if_parseable)
ERROR_PATTERNS = [
    # Rate limits
    (
        r"Rate limit reached.*try again in (\d+)h(\d+)m",
        lambda m: {"hours": int(m.group(1)), "minutes": int(m.group(2)), "type": "rate_limit_tpd"},
    ),
    (
        r"Rate limit reached.*try again in (\d+)m",
        lambda m: {"hours": 0, "minutes": int(m.group(1)), "type": "rate_limit_rpm"},
    ),
    (
        r"(?:daily free allocation|used up your daily)",
        lambda m: {"hours": 24, "minutes": 0, "type": "daily_free_exhausted"},
    ),
    # Auth / subscription
    (
        r"HTTP 402|402 Payment Required",
        lambda m: {"hours": 0, "minutes": 0, "type": "payment_required", "permanent": True},
    ),
    (
        r'InvalidSubscription.*does not have a v',
        lambda m: {"hours": 1, "minutes": 0, "type": "invalid_subscription", "recheck": True},
    ),
    (
        r"credit balance is too",
        lambda m: {"hours": 24, "minutes": 0, "type": "no_credit", "recheck": True},
    ),
    (
        r"bearer token.*invalid|HTTP 403$",
        lambda m: {"hours": 0, "minutes": 0, "type": "auth_invalid", "permanent": True},
    ),
    (
        r"requires paid plan|pricingUrl",
        lambda m: {"hours": 0, "minutes": 0, "type": "paid_required", "permanent": True},
    ),
    # Model-specific
    (
        r"Function id.*not found|Function.*Not Found",
        lambda m: {"hours": 1, "minutes": 0, "type": "function_not_found", "model_specific": True},
    ),
    (
        r"prompt too long.*exceeded max context",
        lambda m: {"minutes": 15, "type": "context_length", "model_specific": True},
    ),
    (
        r"maximum context length is \d+ tokens",
        lambda m: {"minutes": 15, "type": "context_length_nvidia", "model_specific": True},
    ),
    # Generic
    (
        r"Too Many Requests",
        lambda m: {"hours": 0, "minutes": 5, "type": "generic_429"},
    ),
    (
        r"fetch failed",
        lambda m: {"hours": 0, "minutes": 5, "type": "fetch_failed"},
    ),
    (
        r"Internal Server Error",
        lambda m: {"hours": 0, "minutes": 2, "type": "generic_500"},
    ),
    # Worker local request limit
    (
        r"ResourceExhausted.*request limit reached",
        lambda m: {"hours": 1, "minutes": 0, "type": "worker_request_limit", "recheck": True},
    ),
    (
        r"Worker local total request limit",
        lambda m: {"hours": 1, "minutes": 0, "type": "worker_request_limit", "recheck": True},
    ),
]


def extract_provider_model(body: str) -> tuple[Optional[str], Optional[str]]:
    """Extract provider and model from error body if present.

    9router error format: '❌ provider [status]: [status]: {body}'
    Provider name comes from log prefix; model from JSON if present.
    """
    provider = None
    model = None

    # Try parsing JSON body for model info
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            model = data.get("model")
            # provider sometimes in error metadata
            provider = data.get("provider")  # can be None
    except (json.JSONDecodeError, TypeError):
        pass

    return provider, model


def parse_error(status: int, body: str) -> dict:
    """Parse HTTP error into cooldown decision.

    Returns:
        {
            "cooldown": {"hours": int, "minutes": int, "type": str},
            "permanent": bool,
            "provider_scope": bool,
            "model_specific": bool,
            "provider_hint": str | None,
            "model_hint": str | None,
        }
    """
    provider, model = extract_provider_model(body)

    for pattern, handler in ERROR_PATTERNS:
        m = re.search(pattern, body, re.IGNORECASE)
        if m:
            result = handler(m) if callable(handler) else handler.copy()

            result.setdefault("model_specific", False)
            result.setdefault("permanent", False)
            result.setdefault("recheck", False)

            result["provider_hint"] = provider
            result["model_hint"] = model

            # If no hours/minutes from handler, derive from status
            if "hours" not in result and "minutes" not in result:
                if status == 429:
                    result["hours"], result["minutes"] = 0, 5
                elif status == 403:
                    result["hours"], result["minutes"] = 0, 0
                    result["permanent"] = True
                elif status == 404:
                    result["hours"], result["minutes"] = 1, 0
                    result["model_specific"] = True
                elif status == 413:
                    result["hours"], result["minutes"] = 0, 1  # short, use prompt limiter
                elif status >= 500:
                    result["hours"], result["minutes"] = 0, 2
                else:
                    result["hours"], result["minutes"] = 1, 0

            return result

    # Unknown error — default 1h cooldown
    return {
        "cooldown": {"hours": 1, "minutes": 0, "type": f"unknown_{status}"},
        "permanent": False,
        "model_specific": False,
        "provider_hint": provider,
        "model_hint": model,
    }


def parse_log_line(line: str) -> Optional[dict]:
    """Parse 9router error.log line: '❌ provider [status]: [status]: body'"""
    stripped = line.strip()
    if not stripped.startswith("❌"):
        return None

    # Format: ❌ provider [status]: [status]: body...
    parts = stripped.split("[", 2)
    if len(parts) < 3:
        return None

    provider = parts[0].replace("❌", "").strip()
    rest = "[" + parts[1] + "[" + parts[2]

    # Extract status
    m = re.match(r"\[(\d+)\]:\s*\[(\d+)\]:", rest)
    if not m:
        return None
    status = int(m.group(1))
    body = rest[m.end():].strip()

    parsed = parse_error(status, body)
    parsed["provider_hint"] = parsed["provider_hint"] or provider
    return parsed


def parse_access_log_line(line: str) -> Optional[dict]:
    """Parse 9router access.log combo lines for provider/model info."""
    # [COMBO] Model X/Y succeeded / failed
    m = re.search(r"\[COMBO\].*Model\s+(\S+)\s+(succeeded|failed)", line)
    if not m:
        return None

    model_id = m.group(1)
    success = m.group(2) == "succeeded"

    # Extract provider prefix from model_id (groq/llama..., nvidia/..., etc.)
    provider = model_id.split("/")[0] if "/" in model_id else model_id

    return {"provider": provider, "model": model_id, "status": "healthy" if success else "failed"}