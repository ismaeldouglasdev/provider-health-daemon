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


def _seconds_to_cooldown(seconds: int) -> dict:
    """Convert provider retry_after seconds to hours/minutes (truncate, keep exact value)."""
    seconds = max(int(seconds), 30)
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    return {"hours": hours, "minutes": minutes}


def _extract_retry_after_seconds(body: str) -> Optional[int]:
    """Extract retry_after from error body (plain text or JSON).

    Text forms (case-insensitive):
      - "retry after 77466s" (llm7 seconds)
      - "(reset after 30s)", "(reset after 5m)", "(reset after 2h)"
      - "(reset after 4m 51s)" — compound, as 9router annotates error lines
      - "Resets in 125h58m35s" (antigravity/google quota)
    JSON forms: retry_after / retryAfter / retry_after_seconds fields
    (top-level or inside error).
    """
    if not body:
        return None
    # "retry after X" / "reset after X" / "Resets in X" — X = compound h/m/s
    m = re.search(r"(?:resets?\s+in|retry\s+after|reset\s+after)\s+((?:\d+\s*[hms]\s*)+)", body, re.IGNORECASE)
    if m:
        total = 0
        for n_s, unit in re.findall(r"(\d+)\s*([hms])", m.group(1), re.IGNORECASE):
            n = int(n_s)
            if unit.lower() == "h":
                total += n * 3600
            elif unit.lower() == "m":
                total += n * 60
            else:
                total += n
        if total > 0:
            return total
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    for key in ("retry_after", "retryAfter", "retry_after_seconds"):
        val = data.get(key)
        if isinstance(val, (int, float)) and val > 0:
            return int(val)
    err = data.get("error")
    if isinstance(err, dict):
        for key in ("retry_after", "retryAfter", "retry_after_seconds"):
            val = err.get(key)
            if isinstance(val, (int, float)) and val > 0:
                return int(val)
    # antigravity/google quota: quotaResetDelay (Go duration) + quotaResetTimeStamp (ISO)
    reset = _extract_quota_reset_seconds(body)
    if reset is not None:
        return reset
    return None


def _extract_quota_reset_seconds(body: str) -> Optional[int]:
    """Extract the provider's real quota reset time in seconds.

    Handles antigravity/google RESOURCE_EXHAUSTED 429 bodies:
      - "quotaResetDelay": "125h58m35.937114014s"  (Go duration, fractional sec)
      - "quotaResetTimeStamp": "2026-09-02T06:00:00Z"  (ISO timestamp)
    Falls back to None if neither is present/parseable.
    """
    if not body:
        return None

    # quotaResetDelay — Go duration like "125h58m35.937114014s" or "30s"
    m = re.search(r'"quotaResetDelay"\s*:\s*"([^"]+)"', body)
    if m:
        dur = m.group(1)
        total = 0.0
        for n_s, unit in re.findall(r"(\d+(?:\.\d+)?)\s*([hms])", dur, re.IGNORECASE):
            n = float(n_s)
            if unit.lower() == "h":
                total += n * 3600
            elif unit.lower() == "m":
                total += n * 60
            else:
                total += n
        if total > 0:
            return int(total)

    # quotaResetTimeStamp — ISO timestamp; cooldown = remaining seconds
    m = re.search(r'"quotaResetTimeStamp"\s*:\s*"([^"]+)"', body)
    if m:
        from datetime import datetime, timezone
        try:
            ts = m.group(1)
            if ts.endswith("Z"):
                ts = ts[:-1] + "+00:00"
            target = datetime.fromisoformat(ts)
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            remaining = (target - datetime.now(timezone.utc)).total_seconds()
            if remaining > 0:
                return int(remaining)
        except (ValueError, TypeError):
            pass

    return None


def _is_free_tier_model(model: Optional[str]) -> bool:
    """True for :free-tier model ids (openrouter, bazaarlink auto:free, etc.)."""
    if not model:
        return False
    return ":free" in model


def _apply_cooldown_fields(result: dict, hours: int, minutes: int) -> None:
    cd = result.get("cooldown")
    if cd is not None:
        cd["hours"] = hours
        cd["minutes"] = minutes
    else:
        result["hours"] = hours
        result["minutes"] = minutes


def _postprocess_error_result(result: dict, body: str) -> dict:
    """Apply retry_after overrides and free-tier 429 model scoping."""
    retry = _extract_retry_after_seconds(body)
    if retry is not None:
        h_m = _seconds_to_cooldown(retry)
        _apply_cooldown_fields(result, h_m["hours"], h_m["minutes"])

    cd = result.get("cooldown") or result
    err_type = str(cd.get("type", ""))
    model = result.get("model_hint")
    if _is_free_tier_model(model) and (
        err_type in ("generic_429", "unknown_429")
        or err_type.startswith("unknown_4")  # unknown_429 from status fallback
    ):
        result["model_specific"] = True

    return result


# ── Error → cooldown duration ────────────────────────────────────────
# Format: (status, regex_pattern) → (provider_scope, model_specific, cooldown_type, hours_if_parseable)
ERROR_PATTERNS = [
    # Rate limits
    (
        r"Rate limit reached.*try again in (\d+)h(\d+)m",
        lambda m: {"hours": int(m.group(1)), "minutes": int(m.group(2)), "type": "rate_limit_tpd", "model_specific": True},
    ),
    (
        r"Rate limit reached.*try again in (\d+)m",
        lambda m: {"hours": 0, "minutes": int(m.group(1)), "type": "rate_limit_rpm", "model_specific": True},
    ),
    (
        r"Rate limit reached.*try again in ([\d.]+)s",
        lambda m: {"hours": 0, "minutes": 1, "type": "rate_limit_rpm", "model_specific": True},
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
        r"You have reached the limit|MONTHLY_REQUEST_COUNT|Monthly request limit exceeded|reached its monthly quota",
        lambda m: {"hours": 1, "minutes": 0, "type": "monthly_limit", "recheck": True},
    ),
    (
        r"The usage limit has been reached",
        lambda m: {"hours": 1, "minutes": 0, "type": "monthly_limit", "recheck": True},
    ),
    (
        r"weekly usage limit",
        lambda m: {"hours": 168, "minutes": 0, "type": "weekly_limit", "model_specific": True, "recheck": True},
    ),
    (
        r"(?:daily token quota exceeded|token quota exceeded)",
        lambda m: {"hours": 1, "minutes": 0, "type": "daily_quota_exceeded", "recheck": True},
    ),
    (
        r"exceeded your current quota|quota exceeded",
        lambda m: {"hours": 1, "minutes": 0, "type": "monthly_limit", "recheck": True},
    ),
    (
        r"exceeded your rate limit|session usage limit|sending requests too quickly",
        lambda m: {"hours": 0, "minutes": 5, "type": "generic_429"},
    ),
    # groq 429: "Rate limit reached for model X in organization..." (sem try again in)
    (
        r"Rate limit reached for model",
        lambda m: {"hours": 0, "minutes": 5, "type": "rate_limit_rpm", "model_specific": True},
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
    # PERMANENT (2026-08-14): subscription level is account-tier-specific; a
    # recheck will never succeed. Safe now that meta_router._eligible_routers
    # no longer misroutes non-Kiro models to Kiro (false kiro_api_error hits).
    (
        r"Invalid model ID or insufficient subscription level",
        lambda m: {"hours": 0, "minutes": 0, "type": "subscription_level", "model_specific": True, "permanent": True},
    ),
    # Account-access errors: model requires paid subscription on provider plan
    (
        r"requires a paid subscription|requires a subscription|Your plan does not include|plan does not include it|paid subscription on its provider",
        lambda m: {"hours": 0, "minutes": 0, "type": "subscription_level", "model_specific": True, "permanent": True},
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
    # google INSUFFICIENT_TOKENS 429: "Insufficient tokens: required N, available M"
    (
        r"INSUFFICIENT_TOKENS|Insufficient tokens",
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
        r"the following must be satisfied|'messages' : minimum number of items|Improperly formed request|stream_options' field is only allowed|max_tokens must be (?:at least|less than or equal)|Unsupported parameter|1 validation error|Tool call id was|--enable-auto-tool-choice|'messages' field cannot be empty|This model only supports s|failed to template request|Invalid parameter: messages with role|invalid tool call|duplicate tool id|cannot have duplicate tool",
        lambda m: {"hours": 0, "minutes": 0, "type": "request_invalid"},
    ),
    # Model-specific
    (
        r"Function id.*not found|Function.*Not Found|Function '[0-9a-f-]{8,}'",
        lambda m: {"hours": 1, "minutes": 0, "type": "function_not_found", "model_specific": True},
    ),
    (
        r"Model not found|model_not_found|Invalid model|model '[^']*' not found|does not exist|Requested entity was not found",
        lambda m: {"hours": 24, "minutes": 0, "type": "model_not_found", "model_specific": True, "recheck": True},
    ),
    (
        r"no registered providers found|please check the model you provided",
        lambda m: {"hours": 24, "minutes": 0, "type": "model_not_found", "model_specific": True, "recheck": True},
    ),
    # Anthropic-style generic rejection: bare "Bad Request" with null type/param
    # (nemotron :free 400 — dead :free endpoint answering generically). Anchored
    # to the EXACT message so descriptive "Bad Request: ..." errors (client-side
    # request issues) are NOT banned as dead models.
    (
        r'^Bad Request\s*$|"message"\s*:\s*"Bad Request"\s*,\s*"type"\s*:\s*null',
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
    # Transient model outage (llm7/kiro 400 "Model X is currently unavailable",
    # code model_unavailable, e.g. deepseek-v4-flash temporarily down): short
    # model-scoped cooldown — NOT a permanent "model not supported" ban (which
    # would also disable the model in the 9router catalog).
    (
        r"currently unavailable|model_unavailable|temporarily unavailable",
        lambda m: {"hours": 0, "minutes": 15, "type": "model_unavailable", "model_specific": True, "recheck": True},
    ),
    (
        r"model_config for",
        lambda m: {"hours": 24, "minutes": 0, "type": "model_not_supported", "model_specific": True, "recheck": True},
    ),
    (
        r"prompt too long.*exceeded max context",
        lambda m: {"minutes": 15, "type": "context_length", "model_specific": True},
    ),
    # Z.AI GLM 400: {"error":{"code":"1261","message":"Prompt exceeds max length"}}
    (
        r"code.?[:\"]?\s*1261|prompt exceeds max length",
        lambda m: {"minutes": 15, "type": "context_length", "model_specific": True},
    ),
    # SambaNova 400: {"error":"Max_len exceeded: Input is 66547 tokens but this model only supports 8192."}
    # (catálogo reporta ctx 131072 mas upstream real aceita só 8192 — ver bugs-erros-opencode.md 2026-08-11)
    (
        r"Max_len exceeded|this model only supports \d+ tokens",
        lambda m: {"minutes": 15, "type": "context_length", "model_specific": True},
    ),
    (
        r"context_length_exceeded|context_window_exceeded|prompt token count of.*exceeds the limit",
        lambda m: {"minutes": 15, "type": "context_length", "model_specific": True},
    ),
    # Mistral/Together 400: "Context window exceeded for this model." (code context_window_exceeded)
    (
        r"context window exceeded",
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
    # groq 413 TPD rate limit: "Request too large for model X ... on tokens per
    # day (TPD): Limit 100000, Requested 120900" — tokens per DAY is a daily
    # quota (24h cooldown), NOT per-minute. Must match BEFORE the TPM /
    # "Limit \d+, Requested \d+" pattern below (which would classify as 5min).
    (
        r"tokens per day \(TPD\)|on tokens per day",
        lambda m: {"hours": 24, "minutes": 0, "type": "daily_free_exhausted", "model_specific": True},
    ),
    # groq 413 TPM rate limit: "Request too large for model X ... on tokens per
    # minute (TPM): Limit 8000, Requested 78478" — must match BEFORE the
    # "Request too large for model" context_length pattern below
    (
        r"tokens per minute \(TPM\)|Limit \d+, Requested \d+",
        lambda m: {"hours": 0, "minutes": 5, "type": "rate_limit_rpm", "model_specific": True},
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
        r"experiencing high demand|high demand|Please try again later",
        lambda m: {"hours": 0, "minutes": 5, "type": "generic_429"},
    ),
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

    # 'No active credentials for provider: openai' — 9router combo routers
    # name the real upstream provider in the error message.
    if not provider and isinstance(body, str):
        m = re.search(
            r"no active credentials for provider:?\s*([A-Za-z0-9_.-]+)",
            body,
            re.IGNORECASE,
        )
        if m:
            provider = m.group(1)

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
        result = {
            "cooldown": {"hours": 0, "minutes": 5, "type": "generic_429"},
            "permanent": False,
            "model_specific": False,
            "provider_hint": provider,
            "model_hint": model,
        }
        return _postprocess_error_result(result, body)

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

            return _postprocess_error_result(result, body)

    # Unknown error — derive cooldown from status code
    if 400 <= status < 500:
        # 4xx client errors: short cooldown, likely transient
        cd_hours, cd_minutes = 0, 15
    elif 500 <= status < 600:
        # 5xx server errors: very short cooldown
        cd_hours, cd_minutes = 0, 5
    else:
        cd_hours, cd_minutes = 1, 0
    result = {
        "cooldown": {"hours": cd_hours, "minutes": cd_minutes, "type": f"unknown_{status}"},
        "permanent": False,
        "model_specific": False,
        "provider_hint": provider,
        "model_hint": model,
    }
    return _postprocess_error_result(result, body)


# Synthesized body per status for [COMBO] failure lines (no error body is
# logged, only the HTTP status). Chosen to hit the right ERROR_PATTERNS so
# combo failures produce the same cooldowns as direct chat failures.
_COMBO_STATUS_BODY = {
    402: "Monthly request limit exceeded. Account has reached its monthly quota.",
    404: "model not found",
    410: "model has been deprecated",
    429: "",  # empty body → generic_429 5min
    500: "Internal Server Error",
    502: "Internal Server Error",
    503: "Internal Server Error",
}

_TIMESTAMP_RE = re.compile(r"^\[\d{2}:\d{2}:\d{2}(?:\.\d+)?\]\s*")


def _strip_timestamp(line: str) -> str:
    """Strip leading '[HH:MM:SS]' (or '[HH:MM:SS.mmm]') prefix if present."""
    m = _TIMESTAMP_RE.match(line)
    return line[m.end():] if m else line


def _parse_warning_line(stripped: str) -> Optional[dict]:
    """Parse '⚠️ [CHAT] ...' and '⚠️ [COMBO] ...' lines from error.log."""
    # ⚠️  [CHAT] [provider/model] [status]: body
    m = re.match(
        r"⚠️\s*\[CHAT\]\s*\[([^/\]]+)/([^\]]+)\]\s*\[(\d+)\]:\s*(.*)",
        stripped,
        re.DOTALL,
    )
    if m:
        provider, model, status_s, body = m.groups()
        parsed = parse_error(int(status_s), body)
        parsed["provider_hint"] = parsed["provider_hint"] or provider
        parsed["model_hint"] = parsed["model_hint"] or model
        return _postprocess_error_result(parsed, body)

    # ⚠️  [COMBO] Model provider/model failed, trying next {"status":N}
    m = re.search(
        r"\[COMBO\]\s*Model\s+(\S+)\s+failed.*?\{\"status\":(\d+)\}",
        stripped,
    )
    if m:
        model_id, status_s = m.groups()
        provider = model_id.split("/")[0] if "/" in model_id else model_id
        status = int(status_s)
        body = _COMBO_STATUS_BODY.get(status, f'{{"status":{status}}}')
        parsed = parse_error(status, body)
        parsed["provider_hint"] = parsed["provider_hint"] or provider
        if "/" in model_id:
            parsed["model_hint"] = parsed["model_hint"] or model_id
        return _postprocess_error_result(parsed, body)

    return None


def parse_log_line(line: str) -> Optional[dict]:
    """Parse 9router error.log line into a cooldown decision.

    Handles formats (with optional leading '[HH:MM:SS]' timestamp):
      ❌ provider [status]: [status]: body
      ⚠️  [CHAT] [provider/model] [status]: body
      ⚠️  [COMBO] Model provider/model failed, trying next {"status":N}
    """
    stripped = _strip_timestamp(line.strip())
    if not stripped:
        return None

    if stripped.startswith("⚠️"):
        return _parse_warning_line(stripped)

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
    return _postprocess_error_result(parsed, body)


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


def parse_request_detail_row(data_json: str) -> Optional[dict]:
    """Parse 9router sqlite requestDetails.data JSON → cooldown decision.

    The 9router (next-server v1) stores per-request details in SQLite
    (table requestDetails, column data) instead of writing error.log /
    access.log. Error rows look like:
      {
        "provider": "nvidia",
        "model": "deepseek-ai/deepseek-v4-pro",
        "status": "error",
        "response": {"error": '{"type":"about:blank","status":410,"detail":"..."}'}
      }
    The response.error field is a JSON *string* — parse it to recover the
    real status code + body, then reuse parse_error() for cooldowns.
    Returns the same shape as parse_error() with provider_hint/model_hint
    filled from the row, or None if the row is not an error / not parseable.
    """
    try:
        data = json.loads(data_json)
    except (json.JSONDecodeError, TypeError):
        return None

    if not isinstance(data, dict) or data.get("status") != "error":
        return None

    provider = data.get("provider") or ""
    model = data.get("model") or ""

    # response.error is a JSON string (double-encoded) OR a plain dict
    err_raw = None
    resp = data.get("response")
    if isinstance(resp, dict):
        err_raw = resp.get("error")
    if err_raw is None:
        err_raw = data.get("error")
    if err_raw is None:
        return None

    if isinstance(err_raw, str):
        try:
            err = json.loads(err_raw)
        except (json.JSONDecodeError, TypeError):
            err = {"message": err_raw}  # opaque body
    else:
        err = err_raw

    # Normalize OpenAI-style {"error": {...}} wrapper
    if isinstance(err, dict) and isinstance(err.get("error"), dict):
        err = err["error"]

    if not isinstance(err, dict):
        return None

    # Extract status code: top-level "status", "code", or "error.code"
    status = err.get("status") or err.get("code")
    if isinstance(status, str) and status.isdigit():
        status = int(status)
    if not isinstance(status, int):
        return None  # no status → nothing to decide

    # Body: prefer detail/message, fall back to raw JSON
    body = err.get("detail") or err.get("message") or ""
    if not body:
        body = json.dumps(err)

    # The 9router wraps upstream errors as {"error": {"message": "Provider returned error",
    # "metadata": {"raw": "<double-encoded upstream body>"}}} — the generic message hides the
    # real upstream text (e.g. "invalid tool call ... duplicate tool ids"), which would
    # otherwise fall through to unknown_4xx. Unwrap metadata.raw and prefer its message.
    metadata = err.get("metadata")
    if isinstance(metadata, dict) and isinstance(metadata.get("raw"), str):
        raw = metadata["raw"]
        try:
            raw_obj = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            raw_obj = None
        if isinstance(raw_obj, dict):
            raw_msg = raw_obj.get("message") or raw_obj.get("detail")
            if isinstance(raw_msg, str) and raw_msg.strip():
                body = raw_msg

    parsed = parse_error(status, body)
    parsed["provider_hint"] = parsed["provider_hint"] or provider
    parsed["model_hint"] = parsed["model_hint"] or model
    return _postprocess_error_result(parsed, body)