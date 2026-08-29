"""Tests for error_parser — cooldown decisions for upstream error bodies."""

import pytest

from error_parser import (
    extract_provider_model,
    parse_error,
    parse_log_line,
    parse_request_detail_row,
)


def _cd(info):
    return info.get("cooldown") or info


def test_kiro_subscription_level_is_model_specific():
    body = '{"error": {"message": "Invalid model ID or insufficient subscription level to use it.", "type": "kiro_api_error", "code": 400}}'
    info = parse_error(400, body)
    cd = _cd(info)
    assert cd["type"] == "subscription_level"
    # PERMANENT (2026-08-14): subscription level is account-tier-specific; a
    # recheck never succeeds. Matches error_parser's deliberate change from 24h
    # to a permanent cooldown now that meta_router._eligible_routers no longer
    # misroutes non-Kiro models to Kiro.
    assert cd["hours"] == 0
    assert info["model_specific"] is True
    assert info["permanent"] is True
    assert info.get("recheck") is not True


def test_bazaarlink_no_credit_extracts_provider_model():
    body = ('{"error": {"message": "[bazaarlink/claude-opus-4.7] [402]: '
            '{\\"error\\":{\\"message\\":\\"Insufficient credits. Please top up to continue.\\",'
            '\\"type\\":\\"invalid_request_error\\",\\"code\\":402}}", "type": "api_error", "code": null}}')
    info = parse_error(402, body)
    cd = _cd(info)
    assert cd["type"] == "no_credit"
    assert cd["hours"] == 24
    assert info["provider_hint"] == "bazaarlink"
    assert info["model_hint"] == "claude-opus-4.7"
    assert info["recheck"] is True


def test_anthropic_no_credentials():
    body = "No active credentials for provider: anthropic"
    info = parse_error(400, body)
    cd = _cd(info)
    assert cd["type"] == "no_credentials"
    assert cd["hours"] == 24
    assert info["provider_hint"] == "anthropic"


def test_extract_provider_model_no_credentials_json_body():
    body = ('{"error": {"message": "No active credentials for provider: openai",'
            ' "type": "invalid_request_error", "code": "model_not_found"}}')
    provider, model = extract_provider_model(body)
    assert provider == "openai"
    assert model is None


def test_extract_provider_model_no_credentials_plain_body():
    provider, model = extract_provider_model("No active credentials for provider: openai")
    assert provider == "openai"
    assert model is None


def test_monthly_request_count():
    body = "You have reached the limit of requests per month for the selected model"
    info = parse_error(402, body)
    cd = _cd(info)
    assert cd["type"] == "monthly_limit"
    assert cd["hours"] == 1
    assert info["recheck"] is True


def test_model_not_found_is_model_specific():
    body = '{"error": {"message": "Model not found: 9router/claude-opus-4-7"}}'
    info = parse_error(404, body)
    cd = _cd(info)
    assert cd["type"] == "model_not_found"
    assert cd["hours"] == 24
    assert info["model_specific"] is True


def test_gemini_requested_entity_not_found_is_model_specific():
    # antigravity/gemini-3.5-flash-high [404] — Google "Requested entity was
    # not found" (dead endpoint name): classify as model_not_found so the proxy
    # shields 503 + falls back instead of leaking the raw 404 to the agent.
    body = '{"error":{"message":"Requested entity was not found.","status":"NOT_FOUND","code":404}}'
    info = parse_error(404, body)
    cd = _cd(info)
    assert cd["type"] == "model_not_found"
    assert cd["hours"] == 24
    assert info["model_specific"] is True
    assert info["recheck"] is True


def test_nemotron_bad_request_null_shape_is_model_not_found():
    # nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free [400] — Anthropic-style
    # generic rejection {"message":"Bad Request","type":null,"param":null} from
    # a dead :free endpoint. Only the anchored bare-message form matches.
    body = '{"error":{"message":"Bad Request","type":null,"param":null,"code":"400"}}'
    info = parse_error(400, body)
    cd = _cd(info)
    assert cd["type"] == "model_not_found"
    assert cd["hours"] == 24
    assert info["model_specific"] is True
    assert info["recheck"] is True


def test_descriptive_bad_request_not_banned_as_model():
    # Negative: a descriptive "Bad Request: ..." is a client request issue, NOT
    # a dead model — must not receive the 24h model_not_found ban.
    body = '{"error":{"message":"Bad Request: max_tokens exceeds the limit","type":"invalid_request_error"}}'
    info = parse_error(400, body)
    cd = _cd(info)
    assert cd["type"] != "model_not_found"


def test_extract_provider_model_bracket_prefix():
    body = '{"error": {"message": "[kiro/claude-sonnet-4.5] [402]: ..."}}'
    provider, model = extract_provider_model(body)
    assert provider == "kiro"
    assert model == "claude-sonnet-4.5"


def test_extract_provider_model_plain_body_returns_none():
    provider, model = extract_provider_model("Internal Server Error")
    assert provider is None
    assert model is None


def test_parse_log_line_unknown_status():
    info = parse_log_line("❌ kiro [400]: [400]: {'error': 'something'}")
    assert info is not None
    assert info["provider_hint"] == "kiro"
    assert _cd(info)["type"].startswith("unknown_")


def test_worker_request_limit():
    body = "ResourceExhausted: request limit reached for this worker"
    info = parse_error(429, body)
    cd = _cd(info)
    assert cd["type"] == "worker_request_limit"
    assert cd["hours"] == 1
    assert info["recheck"] is True


def test_generic_429_empty_body():
    info = parse_error(429, "")
    cd = _cd(info)
    assert cd["type"] == "generic_429"
    assert cd["minutes"] == 5


def test_model_deprecated_is_model_specific():
    body = '{"error": {"message": "model has been deprecated, use a newer version"}}'
    info = parse_error(410, body)
    cd = _cd(info)
    assert cd["type"] == "model_deprecated"
    assert cd["hours"] == 24
    assert info["model_specific"] is True


def test_request_invalid_has_no_cooldown():
    body = "Improperly formed request: messages field cannot be empty"
    info = parse_error(400, body)
    cd = _cd(info)
    assert cd["type"] == "request_invalid"
    assert cd["hours"] == 0
    assert cd["minutes"] == 0


def test_paid_required_is_permanent():
    body = "This model is not supported when using Codex with a ChatGPT account"
    info = parse_error(403, body)
    cd = _cd(info)
    assert cd["type"] == "paid_required"
    assert info["permanent"] is True


def test_rate_limit_until_iso_far_future_capped_at_24h():
    body = "model is rate limited until 2099-01-01T00:00:00"
    info = parse_error(429, body)
    cd = _cd(info)
    assert cd["type"] == "rate_limit_until"
    assert cd["hours"] == 24
    assert info["recheck"] is True


def test_context_length_from_413():
    body = "exceeded this model context window limit (32768)"
    info = parse_error(413, body)
    cd = _cd(info)
    assert cd["type"] == "context_length"
    assert cd["minutes"] == 15
    assert info["model_specific"] is True


def test_zai_glm_1261_prompt_exceeds_max_length_is_model_specific():
    body = '{"error":{"code":"1261","message":"Prompt exceeds max length"}}'
    info = parse_error(400, body)
    cd = _cd(info)
    assert cd["type"] == "context_length"
    assert cd["minutes"] == 15
    assert info["model_specific"] is True


def test_zai_glm_1261_plain_message():
    body = "Prompt exceeds max length"
    info = parse_error(400, body)
    cd = _cd(info)
    assert cd["type"] == "context_length"
    assert info["model_specific"] is True


def test_sambanova_max_len_exceeded_is_model_specific():
    body = '{"error":"Max_len exceeded: Input is 66547 tokens but this model only supports 8192."}'
    info = parse_error(400, body)
    cd = _cd(info)
    assert cd["type"] == "context_length"
    assert cd["minutes"] == 15
    assert info["model_specific"] is True


def test_sambanova_max_len_exceeded_plain_message():
    body = "Max_len exceeded: Input is 66547 tokens but this model only supports 8192."
    info = parse_error(400, body)
    cd = _cd(info)
    assert cd["type"] == "context_length"
    assert info["model_specific"] is True


# ── parse_log_line: timestamp prefix + ⚠️ [COMBO]/[CHAT] lines ──────


def test_combo_429_generic_429():
    line = '[16:18:40] ⚠️  [COMBO] Model cf/@cf/moonshotai/kimi-k2.6 failed, trying next {"status":429}'
    info = parse_log_line(line)
    assert info is not None
    cd = _cd(info)
    assert cd["type"] == "generic_429"
    assert cd["minutes"] == 5
    assert info["provider_hint"] == "cf"
    assert info["model_hint"] == "cf/@cf/moonshotai/kimi-k2.6"


def test_combo_410_model_deprecated():
    line = '[12:15:59] ⚠️  [COMBO] Model nvidia/deepseek-ai/deepseek-v4-pro failed, trying next {"status":410}'
    info = parse_log_line(line)
    assert info is not None
    cd = _cd(info)
    assert cd["type"] == "model_deprecated"
    assert cd["hours"] == 24
    assert info["provider_hint"] == "nvidia"
    assert info["model_hint"] == "nvidia/deepseek-ai/deepseek-v4-pro"
    assert info["model_specific"] is True


def test_combo_502_generic_500():
    line = '[12:16:13] ⚠️  [COMBO] Model nvidia/z-ai/glm-5.2 failed, trying next {"status":502}'
    info = parse_log_line(line)
    assert info is not None
    cd = _cd(info)
    assert cd["type"] == "generic_500"
    assert cd["minutes"] == 2


def test_combo_402_monthly_quota_temporary():
    # kiro 402 "Monthly request limit exceeded" = quota mensal (TEMPORÁRIA,
    # reseta no mês seguinte) — NUNCA payment_required permanente
    line = '[12:16:20] ⚠️  [COMBO] Model kc/anthropic/claude-opus-4-20250514 failed, trying next {"status":402}'
    info = parse_log_line(line)
    assert info is not None
    cd = _cd(info)
    assert cd["type"] == "monthly_limit"
    assert cd["hours"] == 1
    assert info["permanent"] is False
    assert info["recheck"] is True


def test_monthly_quota_kiro_real_body():
    # Body real do kiro (via chat request direto) — mesma classificação
    body = ('{"error":{"message":"Monthly request limit exceeded. '
            'Account has reached its monthly quota.","type":"kiro_api_error","code":402}}')
    info = parse_error(402, body)
    cd = _cd(info)
    assert cd["type"] == "monthly_limit"
    assert cd["hours"] == 1
    assert info["permanent"] is False
    assert info["recheck"] is True


def test_chat_line_parses_body_daily_free():
    line = ('[16:18:40] ⚠️  [CHAT] [cloudflare-ai/@cf/moonshotai/kimi-k2.6] [429]: '
            '{"errors":[{"message":"AiError: you have used up your daily free allocation of 10,000"}]}')
    info = parse_log_line(line)
    assert info is not None
    cd = _cd(info)
    assert cd["type"] == "daily_free_exhausted"
    assert cd["hours"] == 24
    assert info["provider_hint"] == "cloudflare-ai"
    assert info["model_hint"] == "@cf/moonshotai/kimi-k2.6"


def test_timestamped_error_line_parses():
    line = '[13:26:38] ❌ cloudflare-ai [429]: [429]: {"errors":[{"message":"used up your daily free allocation"}]}'
    info = parse_log_line(line)
    assert info is not None
    assert info["provider_hint"] == "cloudflare-ai"
    assert _cd(info)["type"] == "daily_free_exhausted"


def test_warning_line_without_combo_or_chat_returns_none():
    line = '[16:18:40] ⚠️  [AUTH] cloudflare-ai | all 1 accounts locked'
    assert parse_log_line(line) is None


# ── parse_request_detail_row (SQLite requestDetails rows) ──────────────
import json


def _row(**overrides):
    base = {
        "provider": "nvidia",
        "model": "deepseek-ai/deepseek-v4-pro",
        "status": "error",
        "response": {
            "error": json.dumps(
                {"type": "about:blank", "title": "Gone", "status": 410,
                 "detail": "The model 'deepseek-ai/deepseek-v4-pro' has reached its end "
                           "of life on 2026-08-07T09:00:00Z and is no longer available."}
            )
        },
    }
    base.update(overrides)
    return json.dumps(base)


def test_request_detail_410_deprecated_model_specific():
    info = parse_request_detail_row(_row())
    assert info is not None
    cd = _cd(info)
    assert cd["type"] == "model_deprecated"
    assert cd["hours"] == 24
    assert info["model_specific"] is True
    assert info["provider_hint"] == "nvidia"
    assert info["model_hint"] == "deepseek-ai/deepseek-v4-pro"


def test_request_detail_429_openrouter_rate_limit():
    row = _row(
        provider="openrouter",
        model="nvidia/nemotron-3-super-120b-a12b:free",
        response={"error": json.dumps(
            {"error": {"message": "Rate limit exceeded: free-models-per-day. Add 10 "
                                  "credits to unlock 1000 free model requests per day",
                       "code": 429}}
        )},
    )
    info = parse_request_detail_row(row)
    assert info is not None
    cd = _cd(info)
    assert cd["type"] == "generic_429"
    assert cd["minutes"] == 5
    assert info["provider_hint"] == "openrouter"
    assert info["model_hint"] == "nvidia/nemotron-3-super-120b-a12b:free"
    assert info["model_specific"] is True


def test_request_detail_429_daily_limit_generic():
    row = _row(
        provider="bazaarlink",
        model="auto:free",
        response={"error": json.dumps(
            {"error": {"message": "Free model daily limit reached. Top up credits to "
                                  "continue with the paid version.",
                       "type": "rate_limit_error", "code": 429}}
        )},
    )
    info = parse_request_detail_row(row)
    assert info is not None
    cd = _cd(info)
    assert cd["type"] == "unknown_429"
    assert cd["minutes"] == 15
    assert info["provider_hint"] == "bazaarlink"
    assert info["model_hint"] == "auto:free"
    assert info["model_specific"] is True


def test_free_model_generic_429_is_model_specific():
    body = "Rate limit exceeded"
    info = parse_error(429, body)
    info["model_hint"] = "openrouter/nvidia/nemotron-3-super-120b-a12b:free"
    from error_parser import _postprocess_error_result
    info = _postprocess_error_result(info, body)
    assert _cd(info)["type"] == "generic_429"
    assert info["model_specific"] is True


def test_request_detail_metadata_raw_unwrapped():
    """SQLite row com message genérica + metadata.raw (double-encoded) deve usar
    a mensagem real do upstream — ex: duplicate tool ids → request_invalid, 0 cooldown."""
    row = _row(
        provider="openrouter",
        model="cohere/north-mini-code:free",
        response={"error": json.dumps(
            {"error": {"message": "Provider returned error", "code": 400,
                       "metadata": {"raw": json.dumps(
                           {"id": "2e8b5fef", "message": "invalid tool call provided in "
                            "messages[36].tool_calls[0]: cannot have duplicate tool ids 'bash0'"
                           }), "provider_name": "Cohere", "is_byok": False}},
             "user_id": "user_x"}
        )},
    )
    info = parse_request_detail_row(row)
    assert info is not None
    cd = _cd(info)
    assert cd["type"] == "request_invalid"
    assert cd["hours"] == 0 and cd["minutes"] == 0
    assert info["model_specific"] is False
    assert info["provider_hint"] == "openrouter"
    assert info["model_hint"] == "cohere/north-mini-code:free"


def test_request_detail_metadata_raw_not_json_falls_back():
    """metadata.raw não-JSON → mantém o body original (não quebra)."""
    row = _row(
        provider="openrouter",
        model="some/model",
        response={"error": json.dumps(
            {"error": {"message": "Provider returned error", "code": 400,
                       "metadata": {"raw": "not-json-at-all"}}}
        )},
    )
    info = parse_request_detail_row(row)
    assert info is not None
    cd = _cd(info)
    assert cd["type"] == "unknown_400"  # fallback preservado
    assert info["provider_hint"] == "openrouter"


def test_llm7_retry_after_respected():
    body = "Daily token quota exceeded. Retry after 77466s"
    info = parse_error(429, body)
    cd = _cd(info)
    assert cd["type"] == "daily_quota_exceeded"
    assert cd["hours"] == 21
    assert cd["minutes"] == 31


def test_weekly_usage_limit_is_model_specific():
    body = "You have reached your weekly usage limit for this model"
    info = parse_error(429, body)
    cd = _cd(info)
    assert cd["type"] == "weekly_limit"
    assert cd["hours"] == 168
    assert info["model_specific"] is True
    assert info["recheck"] is True


def test_retry_after_in_json_body():
    body = '{"error": {"message": "quota exceeded", "retry_after": 3600}}'
    info = parse_error(429, body)
    cd = _cd(info)
    assert cd["hours"] == 1
    assert cd["minutes"] == 0


def test_reset_after_suffix_respected():
    # 9router annotates error lines with "(reset after 4m 51s)" — cooldown
    # must honor the provider's real reset time instead of the pattern default.
    body = '{"error": {"message": "You have used up your daily free allocation of 10000 neurons"}} (reset after 4m 51s)'
    info = parse_error(429, body)
    cd = _cd(info)
    assert cd["type"] == "daily_free_exhausted"
    assert cd["hours"] == 0
    assert cd["minutes"] == 4


def test_reset_after_seconds_short_cooldown():
    body = '{"error": {"message": "Named models unavailable for this provider"}} (reset after 2s)'
    info = parse_error(429, body)
    cd = _cd(info)
    assert cd["hours"] == 0
    assert cd["minutes"] == 0


def test_request_detail_success_row_returns_none():
    row = _row(status="success", response={})
    assert parse_request_detail_row(row) is None


def test_request_detail_invalid_json_returns_none():
    assert parse_request_detail_row("not json{{{") is None


def test_request_detail_missing_response_error_returns_none():
    row = _row(response={})
    assert parse_request_detail_row(row) is None


def test_request_detail_status_as_string_code():
    row = _row(response={"error": json.dumps(
        {"error": {"message": "upstream down", "code": "500"}}
    )})
    info = parse_request_detail_row(row)
    assert info is not None
    cd = _cd(info)
    assert cd["type"] == "unknown_500"
    assert cd["minutes"] == 5


# ── Coverage: one representative body per ERROR_PATTERNS entry ────────
# Each case pins (type, cooldown, scope) for a pattern without a dedicated
# test above. Bodies were chosen to match ONLY their target pattern.

_COVERAGE_CASES = [
    # (label, status, body, expected_type, hours, minutes, model_specific, permanent, recheck)
    ("rate_limit_tpd_hm", 429, "Rate limit reached. Please try again in 2h30m",
     "rate_limit_tpd", 2, 30, True, False, False),
    ("rate_limit_rpm_m", 429, "Rate limit reached. Please try again in 15m",
     "rate_limit_rpm", 0, 15, True, False, False),
    ("rate_limit_rpm_s", 429, "Rate limit reached. Please try again in 30s",
     "rate_limit_rpm", 0, 1, True, False, False),
    ("rate_limit_daily", 429, "rate_limit_daily exceeded",
     "daily_free_exhausted", 24, 0, False, False, False),
    ("usage_limit_reached", 429, "The usage limit has been reached",
     "monthly_limit", 1, 0, False, False, True),
    ("exceeded_rate_limit", 429, "You have exceeded your rate limit. Slow down.",
     "generic_429", 0, 5, False, False, False),
    ("rate_limit_for_model", 429, "Rate limit reached for model gpt-4o in organization",
     "rate_limit_rpm", 0, 5, True, False, False),
    ("invalid_subscription", 400, '{"error":{"message":"InvalidSubscription: account does not have a valid subscription"}}',
     "invalid_subscription", 1, 0, False, False, True),
    ("requires_paid_subscription", 403, "This model requires a paid subscription on its provider",
     "subscription_level", 0, 0, True, True, False),
    ("entitlement_error", 403, "ENTITLEMENT_ERROR: entitlement not found",
     "subscription_level", 24, 0, False, False, True),
    ("balance_insufficient", 402, "Your balance is insufficient. Please add funds.",
     "no_credit", 24, 0, False, False, True),
    ("payment_method_required", 402, "Payment method is required to continue using this model",
     "no_credit", 24, 0, False, False, True),
    ("add_credits", 402, "Please add credits to continue",
     "no_credit", 24, 0, False, False, True),
    ("bearer_token_invalid", 401, "Your bearer token is invalid or expired",
     "auth_invalid", 0, 0, False, True, False),
    ("unauthorized", 401, "Unauthorized: invalid API key",
     "auth_invalid", 0, 0, False, True, False),
    ("function_not_found", 400, "Function 'abc12345' not found",
     "function_not_found", 1, 0, True, False, False),
    ("no_registered_providers", 404, "No registered providers found for this model. Please check the model you provided.",
     "model_not_found", 24, 0, True, False, True),
    ("model_not_supported", 400, "This model is not supported by the integrator",
     "model_not_supported", 24, 0, True, False, True),
    ("not_on_worker", 400, "This model is not available on the Worker",
     "model_not_supported", 24, 0, True, False, True),
    ("model_config_for", 400, "model_config for gpt-4o not found",
     "model_not_supported", 24, 0, True, False, True),
    ("prompt_too_long", 400, "Prompt too long: exceeded max context length",
     "context_length", None, 15, True, False, False),
    ("context_length_exceeded", 400, "context_length_exceeded",
     "context_length", None, 15, True, False, False),
    ("context_window_exceeded", 400, "{\"error\":{\"message\":\"Context window exceeded for this model.\",\"type\":\"invalid_request_error\",\"param\":null,\"code\":\"context_window_exceeded\"}}",
     "context_length", None, 15, True, False, False),
    ("duplicate_tool_id", 400, "invalid tool call provided in messages[51].tool_calls[0]: cannot have duplicate tool ids bash0",
     "request_invalid", 0, 0, False, False, False),
    ("max_context_nvidia", 400, "The maximum context length is 128000 tokens",
     "context_length_nvidia", None, 15, True, False, False),
    ("request_too_large", 413, "Request too large for model gpt-4o",
     "context_length", None, 15, True, False, False),
    ("too_many_requests", 429, "Too Many Requests",
     "generic_429", 0, 5, False, False, False),
    ("high_demand_429", 429, "gpt-oss-120b-128k is currently experiencing high demand. Please try again later!",
     "generic_429", 0, 5, False, False, False),
    ("model_unavailable_transient", 400, "Model 'deepseek-v4-flash' is currently unavailable.",
     "model_unavailable", 0, 15, True, False, True),
    ("model_unavailable_json_code", 400, '{"error":{"message":"Model \'deepseek-v4-flash\' is currently unavailable.","type":"invalid_request_error","param":null,"code":"model_unavailable"}}',
     "model_unavailable", 0, 15, True, False, True),
    ("groq_tpm_413", 413, 'Request too large for model `openai/gpt-oss-120b` in organization `org_01kjfnd1zaev798a19ptgmjty3` service tier `on_demand` on tokens per minute (TPM): Limit 8000, Requested 78478, please reduce your message size and try again.',
     "rate_limit_rpm", 0, 5, True, False, False),
    ("groq_tpd_413", 413, 'Request too large for model `groq/llama-3.3-70b-versatile` in organization `org_01kjfnd1zaev798a19ptgmjty3` service tier `on_demand` on tokens per day (TPD): Limit 100000, Requested 120900, please reduce your message size and try again.',
     "daily_free_exhausted", 24, 0, True, False, False),
    ("google_insufficient_tokens", 429, '{"error":{"code":"INSUFFICIENT_TOKENS","message":"Insufficient tokens: required 14395, available 5106"}}',
     "no_credit", 24, 0, False, False, True),
    ("fetch_failed", 500, "fetch failed: connect ECONNREFUSED",
     "fetch_failed", 0, 5, False, False, False),
    ("connect_timeout", 500, "fetch connect timeout after 30000ms",
     "fetch_failed", 0, 5, False, False, False),
    ("worker_total_limit", 429, "Worker local total request limit reached",
     "worker_request_limit", 1, 0, False, False, True),
]


@pytest.mark.parametrize(
    "label,status,body,exp_type,exp_hours,exp_minutes,exp_ms,exp_perm,exp_recheck",
    _COVERAGE_CASES,
    ids=[c[0] for c in _COVERAGE_CASES],
)
def test_error_pattern_coverage(label, status, body, exp_type, exp_hours, exp_minutes, exp_ms, exp_perm, exp_recheck):
    info = parse_error(status, body)
    assert info is not None
    cd = _cd(info)
    assert cd["type"] == exp_type
    assert cd.get("hours") == exp_hours
    assert cd.get("minutes") == exp_minutes
    assert info.get("model_specific") is exp_ms
    assert info.get("permanent") is exp_perm
    assert info.get("recheck") is exp_recheck


def test_antigravity_quota_reset_delay_go_duration():
    """antigravity/google RESOURCE_EXHAUSTED 429: quotaResetDelay Go duration
    (125h58m35.937114014s) → cooldown must honor the real ~5-day reset, not
    the 15min unknown_429 fallback."""
    body = ('{"error": {"code": 429, "message": "Individual quota reached. '
            'Please upgrade your subscription to increase your limits. Resets in 125h58m35s.", '
            '"status": "RESOURCE_EXHAUSTED", "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", '
            '"reason": "QUOTA_EXHAUSTED", "domain": "cloudcode-pa.googleapis.com", '
            '"metadata": {"quotaResetDelay": "125h58m35.937114014s", '
            '"quotaResetTimeStamp": "2026-09-02T06:00:00Z"}}]}}')
    info = parse_error(429, body)
    cd = _cd(info)
    assert cd["hours"] == 125
    assert cd["minutes"] == 58


def test_antigravity_resets_in_text_form():
    """'Resets in 5m 30s' text form → cooldown honors the real reset."""
    body = "Individual quota reached. Please upgrade your subscription. Resets in 5m 30s."
    info = parse_error(429, body)
    cd = _cd(info)
    assert cd["hours"] == 0
    assert cd["minutes"] == 5


def test_antigravity_resets_in_hours_text_form():
    """'Resets in 2h' → cooldown honors the real reset (overrides monthly_limit 1h)."""
    body = "Quota exceeded. Resets in 2h."
    info = parse_error(429, body)
    cd = _cd(info)
    assert cd["hours"] == 2
    assert cd["minutes"] == 0


def test_antigravity_quota_reset_timestamp_iso():
    """quotaResetTimeStamp ISO (future) → cooldown = remaining seconds."""
    body = ('{"error": {"message": "quota", "details": [{"metadata": '
            '{"quotaResetTimeStamp": "2099-01-01T00:00:00Z"}}]}}')
    info = parse_error(429, body)
    cd = _cd(info)
    assert cd["hours"] >= 24  # far-future reset → long cooldown
