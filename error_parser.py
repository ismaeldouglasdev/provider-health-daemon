"""Parse provider error responses → cooldown decision."""

import re
import json
import logging
from typing import Optional

from cooldown import CooldownCalculator

log = logging.getLogger(__name__)


def _iso_rate_limit(iso_str: str) -> dict:
    """Cooldown until an ISO timestamp (kimchi 'rate limited until YYYY-MM-DDTHH:MM:SS')."""
    from datetime import datetime
    try:
        target = datetime.fromisoformat(iso_str)
        delta = target - datetime.now()
        total_min = max(int(delta.total_seconds() // 60), 5)
        hours, minutes = divmod(total_min, 60)
        return {"hours": min(hours, 24), "minutes": minutes, "type": "rate_limit_until", "recheck": True}
    except (ValueError, TypeError):
        return {"hours": 1, "minutes": 0, "type": "rate_limit_until", "recheck": True}


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
        r"Rate limit reached.*try again in ([\d.]+)s",
        lambda m: {"hours": 0, "minutes": 1, "type": "rate_limit_rpm"},
    ),
    (
        r"model is rate limited until (\d{4}-\d{2}-\d{2}T[\d:]+)",
        lambda m: _iso_rate_limit(m.group(1)),
    ),
    (
        r"rate_limit_daily",
        lambda m: {"hours": 24, "minutes": 0, "type": "daily_free_exhausted"},
    ),
    (
        r"(?:daily free allocation|used up your daily)",
        lambda m: {"hours": 24, "minutes": 0, "type": "daily_free_exhausted"},
    ),
    (
        r"You have reached the limit|MONTHLY_REQUEST_COUNT",
        lambda m: {"hours": 1, "minutes": 0, "type": "monthly_limit", "recheck": True},
    ),
    (
        r"The usage limit has been reached",
        lambda m: {"hours": 1, "minutes": 0, "type": "monthly_limit", "recheck": True},
    ),
    (
        r"exceeded your current quota|quota exceeded|weekly usage limit",
        lambda m: {"hours": 1, "minutes": 0, "type": "monthly_limit", "recheck": True},
    ),
    (
        r"exceeded your rate limit|session usage limit|sending requests too quickly",
        lambda m: {"hours": 0, "minutes": 5, "type": "generic_429"},
    ),
    # groq 429: "Rate limit reached for model X in organization..." (sem try again in)
    (
        r"Rate limit reached for model",
        lambda m: {"hours": 0, "minutes": 5, "type": "rate_limit_rpm"},
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
    # Model-specific: kiro rejeita claude-opus mas serve kr/claude-sonnet-4.5
    (
        r"Invalid model ID or insufficient subscription level",
        lambda m: {"hours": 24, "minutes": 0, "type": "subscription_level", "model_specific": True, "recheck": True},
    ),
    # Account-access errors: model requires paid subscription on provider plan
    (
        r"requires a paid subscription|requires a subscription|Your plan does not include|plan does not include it|paid subscription on its provider",
        lambda m: {"hours": 24, "minutes": 0, "type": "subscription_level", "model_specific": True, "recheck": True},
    ),
    (
        r"not subscribed to required|ENTITLEMENT_ERROR|entitlement|membership benefits|not included in your plan|upgrade for access|upgrade your plan",
        lambda m: {"hours": 24, "minutes": 0, "type": "subscription_level", "recheck": True},
    ),
    (
        r"requires a paid plan|pricingUrl|not supported when using Codex with a ChatGPT account",
        lambda m: {"hours": 0, "minutes": 0, "type": "paid_required", "permanent": True},
    ),
    (
        r"credit balance is too|Insufficient credits|Insufficient balance|Please top up",
        lambda m: {"hours": 24, "minutes": 0, "type": "no_credit", "recheck": True},
    ),
    (
        r"balance is insufficient|Insufficient USD|Insufficient.*balance",
        lambda m: {"hours": 24, "minutes": 0, "type": "no_credit", "recheck": True},
    ),
    (
        r"Quota exceeded and account balance|payment method is required|Payment required to access",
        lambda m: {"hours": 24, "minutes": 0, "type": "no_credit", "recheck": True},
    ),
    (
        r"Add credits to continue|run out of credits|spending-limit|PAID_MODEL_AUTH_REQUIRED|out of credits",
        lambda m: {"hours": 24, "minutes": 0, "type": "no_credit", "recheck": True},
    ),
    (
        r"No active credentials for provider",
        lambda m: {"hours": 24, "minutes": 0, "type": "no_credentials", "recheck": True},
    ),
    (
        r"bearer token.*invalid|HTTP 403$",
        lambda m: {"hours": 0, "minutes": 0, "type": "auth_invalid", "permanent": True},
    ),
    # 401s: cline/clinepass "Unauthorized", kilocode "You need to sign in"
    (
        r"Unauthorized|Please make sure you're using the latest version.*re-auth|You need to sign in|AuthenticationError",
        lambda m: {"hours": 0, "minutes": 0, "type": "auth_invalid", "permanent": True},
    ),
    # Client-side request validation errors: not provider health issues (no cooldown)
    (
        r"the following must be satisfied|'messages' : minimum number of items|Improperly formed request|stream_options' field is only allowed|max_tokens must be (?:at least|less than or equal)|Unsupported parameter|1 validation error|Tool call id was|--enable-auto-tool-choice|'messages' field cannot be empty|This model only supports s|failed to template request|Invalid parameter: messages with role",
        lambda m: {"hours": 0, "minutes": 0, "type": "request_invalid"},
    ),
    # Model-specific
    (
        r"Function id.*not found|Function.*Not Found|Function '[0-9a-f-]{8,}'",
        lambda m: {"hours": 1, "minutes": 0, "type": "function_not_found", "model_specific": True},
    ),
    (
        r"Model not found|model_not_found|Invalid model|model '[^']*' not found|does not exist",
        lambda m: {"hours": 24, "minutes": 0, "type": "model_not_found", "model_specific": True, "recheck": True},
    ),
    (
        r"no registered providers found|please check the model you provided",
        lambda m: {"hours": 24, "minutes": 0, "type": "model_not_found", "model_specific": True, "recheck": True},
    ),
    # Deprecated / retired models (kimchi 410, cloudflare 410, nvidia 410, ollama 410)
    (
        r"no longer available|has been deprecated|was deprecated on|model has been deprecated|has been removed|was retired at|was retired on",
        lambda m: {"hours": 24, "minutes": 0, "type": "model_deprecated", "model_specific": True, "recheck": True},
    ),
    (
        r'"title"\s*:\s*"Gone"',
        lambda m: {"hours": 24, "minutes": 0, "type": "model_deprecated", "model_specific": True, "recheck": True},
    ),
    # Model not supported by provider/integrator (github 400, codex 400)
    (
        r"model is not supported|not available for integrator|model_not_supported|does not exist or you do not have access",
        lambda m: {"hours": 24, "minutes": 0, "type": "model_not_supported", "model_specific": True, "recheck": True},
    ),
    (
        r"is not available on the Worker",
        lambda m: {"hours": 24, "minutes": 0, "type": "model_not_supported", "model_specific": True, "recheck": True},
    ),
    (
        r"currently unavailable|model_config for",
        lambda m: {"hours": 24, "minutes": 0, "type": "model_not_supported", "model_specific": True, "recheck": True},
    ),
    (
        r"prompt too long.*exceeded max context",
        lambda m: {"minutes": 15, "type": "context_length", "model_specific": True},
    ),
    (
        r"context_length_exceeded|prompt token count of.*exceeds the limit",
        lambda m: {"minutes": 15, "type": "context_length", "model_specific": True},
    ),
    (
        r"maximum context length is \d+ tokens",
        lambda m: {"minutes": 15, "type": "context_length_nvidia", "model_specific": True},
    ),
    # Cloudflare 413: "exceeded this model context window limit (32768)"
    (
        r"exceeded this model context window limit|exceeded.*context window limit|context window limit \(",
        lambda m: {"minutes": 15, "type": "context_length", "model_specific": True},
    ),
    # groq 413 "Request too large for model X", cloudflare 413 "estimated number of input tokens"
    (
        r"Request too large for model|estimated number of input and maximum output tokens",
        lambda m: {"minutes": 15, "type": "context_length", "model_specific": True},
    ),
    # Mistral 429 rate_limited, nvidia 529 overload
    (
        r"Rate limit exceeded|rate_limited|Service temporarily overloaded",
        lambda m: {"hours": 0, "minutes": 5, "type": "generic_429"},
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
        r"fetch connect timeout|connect timeout",
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
    Some routers (kiro/bazaarlink) embed '[provider/model] [status]:' in the
    message — capture that too.
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
            err = data.get("error")
            if isinstance(err, dict):
                msg = err.get("message", "")
                if not model:
                    model = err.get("model")
                if not provider:
                    provider = err.get("provider")
                body = msg or body
            elif isinstance(err, str):
                body = err
    except (json.JSONDecodeError, TypeError):
        pass

    # '[provider/model] [status]: ...' — kiro/bazaarlink format
    if (not provider or not model) and isinstance(body, str):
        m = re.search(r"\[([^/\]]+)/([^/\]]+)\]\s*\[\d+\]", body)
        if m:
            if not provider:
                provider = m.group(1)
            if not model:
                model = m.group(2)

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

    # Empty/near-empty body with 429: still a rate limit (antigravity/gemini send `{` or empty)
    if status == 429 and len(body.strip()) < 10:
        return {
            "cooldown": {"hours": 0, "minutes": 5, "type": "generic_429"},
            "permanent": False,
            "model_specific": False,
            "provider_hint": provider,
            "model_hint": model,
        }

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

    # Unknown error — derive cooldown from status code
    if 400 <= status < 500:
        # 4xx client errors: short cooldown, likely transient
        cd_hours, cd_minutes = 0, 15
    elif 500 <= status < 600:
        # 5xx server errors: very short cooldown
        cd_hours, cd_minutes = 0, 5
    else:
        cd_hours, cd_minutes = 1, 0
    return {
        "cooldown": {"hours": cd_hours, "minutes": cd_minutes, "type": f"unknown_{status}"},
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