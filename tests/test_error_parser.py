"""Tests for error_parser — cooldown decisions for upstream error bodies."""

from error_parser import extract_provider_model, parse_error, parse_log_line


def _cd(info):
    return info.get("cooldown") or info


def test_kiro_subscription_level_is_model_specific():
    body = '{"error": {"message": "Invalid model ID or insufficient subscription level to use it.", "type": "kiro_api_error", "code": 400}}'
    info = parse_error(400, body)
    cd = _cd(info)
    assert cd["type"] == "subscription_level"
    assert cd["hours"] == 24
    assert info["model_specific"] is True
    assert info["recheck"] is True


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
