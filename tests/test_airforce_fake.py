"""Tests for api.airforce fake-200 detection in the proxy handler.

api.airforce returns HTTP 200 + plain text "The model does not exist in
https://api.airforce" for model ids it doesn't serve. The health-daemon
must treat that as model_not_found (cooldown), not as success — otherwise
dead models stay in rotation forever (the original bug).
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from health_registry import HealthRegistry
from proxy_handler import HealthProxyHandler, _is_airforce_fake_response

FAKE_BODY = b"The model does not exist in https://api.airforce\ndiscord.gg/airforce"

FAKE_SSE_BODY = (
    b'data: {"choices": [{"delta": {"content": "The model does not exist in '
    b'https://api.airforce\\ndiscord.gg/airforce"}}]}\n\ndata: [DONE]\n'
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


def test_is_airforce_fake_response_detects_marker():
    assert _is_airforce_fake_response(FAKE_BODY) is True


def test_is_airforce_fake_response_detects_marker_in_sse():
    assert _is_airforce_fake_response(FAKE_SSE_BODY) is True


def test_is_airforce_fake_response_false_for_normal_body():
    body = b'{"choices": [{"message": {"content": "hi"}}]}'
    assert _is_airforce_fake_response(body) is False


def test_is_airforce_fake_response_false_for_empty():
    assert _is_airforce_fake_response(b"") is False


def test_record_upstream_health_fake_marks_model_in_cooldown(tmp_path):
    registry = HealthRegistry(filepath=tmp_path / "health.json")
    handler = _stub_handler(registry)
    body = {"model": "af/anthropic/claude-3.7-sonnet"}

    with patch("proxy_handler.sync_disable_dead_model") as mock_sync:
        handler._record_upstream_health(body, FAKE_BODY, 0.0)

    assert registry.is_model_available("af/anthropic/claude-3.7-sonnet") is False
    assert handler.audit.cooldowns_applied == 1
    mock_sync.assert_called_once()


def test_record_upstream_health_fake_does_not_poison_provider(tmp_path):
    registry = HealthRegistry(filepath=tmp_path / "health.json")
    handler = _stub_handler(registry)
    body = {"model": "af/anthropic/claude-3.7-sonnet"}

    with patch("proxy_handler.sync_disable_dead_model"):
        handler._record_upstream_health(body, FAKE_BODY, 0.0)

    # model-specific error must NOT disable the whole provider
    assert registry.is_provider_healthy("af") is True


def test_record_upstream_health_normal_marks_provider_healthy(tmp_path):
    registry = HealthRegistry(filepath=tmp_path / "health.json")
    handler = _stub_handler(registry)
    body = {"model": "af/moonshot/kimi-k2.6"}

    handler._record_upstream_health(
        body, b'{"choices": [{"message": {"content": "ok"}}]}', 0.0
    )

    assert registry.is_provider_healthy("af") is True
    assert registry.is_model_available("af/moonshot/kimi-k2.6") is True
    assert handler.audit.cooldowns_applied == 0


def test_record_upstream_health_no_model_is_noop(tmp_path):
    registry = HealthRegistry(filepath=tmp_path / "health.json")
    handler = _stub_handler(registry)

    handler._record_upstream_health({"model": ""}, FAKE_BODY, 0.0)
    handler._record_upstream_health(None, FAKE_BODY, 0.0)

    assert handler.audit.cooldowns_applied == 0
