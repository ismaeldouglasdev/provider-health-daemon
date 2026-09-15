"""Tests for Pollinations fake-200 budget detection in the proxy handler.

Pollinations answers HTTP 200 + plain assistant content "The API key ... has
reached its budget ... (https://enter.pollinations.ai/edit-key?id=...)" for
key-budget exhaustion. The health gate only inspects the status, so these 200s
were recorded as healthy and pollinations stayed in the combo rotation forever
(the original bug). The guard must raise EmptyUpstreamResponse (transparent
fallback for the current request) AND apply a provider-wide cooldown (the whole
key is over budget, not one dead model).
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from health_registry import HealthRegistry
from proxy_handler import (
    HealthProxyHandler,
    _is_empty_chat_response,
    _is_pollinations_budget_error,
)

BUDGET_BODY = (
    b"The API key used for this request has reached its budget. Please raise the "
    b"key budget (https://enter.pollinations.ai/edit-key?id=abc123&ref=agent_key_budget), "
    b"then try again."
)

BUDGET_SSE_BODY = (
    b'data: {"choices": [{"delta": {"content": "The API key used for this request '
    b'has reached its budget. Please raise the key budget '
    b'(https://enter.pollinations.ai/edit-key?id=abc123&ref=agent_key_budget), '
    b'then try again."}}]}\n\ndata: [DONE]\n'
)


def _stub_handler(registry: HealthRegistry) -> HealthProxyHandler:
    handler = HealthProxyHandler.__new__(HealthProxyHandler)
    handler.audit = SimpleNamespace(
        requests_proxied=0, requests_blocked=0, cooldowns_applied=0
    )
    handler.registry = registry
    handler.metrics_store = None
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler.wfile = MagicMock()
    return handler


def test_is_pollinations_budget_error_detects_marker():
    assert _is_pollinations_budget_error(BUDGET_BODY) is True


def test_is_pollinations_budget_error_detects_marker_in_sse():
    assert _is_pollinations_budget_error(BUDGET_SSE_BODY) is True


def test_is_pollinations_budget_error_false_for_normal_body():
    body = b'{"choices": [{"message": {"content": "hi"}}]}'
    assert _is_pollinations_budget_error(body) is False


def test_is_pollinations_budget_error_false_for_empty():
    assert _is_pollinations_budget_error(b"") is False
    # budget body is NOT an empty chat response (it has content) — the two
    # guards are complementary, not overlapping
    assert _is_empty_chat_response(BUDGET_BODY) is False


def test_record_upstream_health_budget_marks_provider_cooldown(tmp_path):
    registry = HealthRegistry(filepath=tmp_path / "health.json")
    handler = _stub_handler(registry)
    body = {"model": "pollinations/openai-fast"}

    handler._record_upstream_health(body, BUDGET_BODY, 0.0)

    # provider-wide: the whole key is over budget, so the provider AND all
    # its models must leave rotation
    assert registry.is_provider_healthy("pollinations") is False
    assert registry.is_model_available("pollinations/openai-fast") is False
    assert handler.audit.cooldowns_applied == 1


def test_record_upstream_health_budget_is_provider_wide_not_model_specific(tmp_path):
    registry = HealthRegistry(filepath=tmp_path / "health.json")
    handler = _stub_handler(registry)
    body = {"model": "pollinations/openai-fast"}

    # provider-wide cooldown → no sync_disable_dead_model (that callback is
    # for model-not-found); the provider entry itself carries the cooldown
    with patch("proxy_handler.sync_disable_dead_model") as mock_sync:
        handler._record_upstream_health(body, BUDGET_BODY, 0.0)

    mock_sync.assert_not_called()
    assert registry.is_provider_healthy("pollinations") is False