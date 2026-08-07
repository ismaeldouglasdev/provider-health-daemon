"""Tests for AuditMetrics counter wiring in the proxy handler (fix #7).

Verifies the three previously-dead counters increment at the right points:
  - requests_proxied   -> successful upstream emission (_emit_upstream_response)
  - requests_blocked   -> every 503 (_respond_unavailable)
  - cooldowns_applied  -> every mark_error applied (_handle_error)
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from health_registry import HealthRegistry
from proxy_handler import HealthProxyHandler


def _stub_handler(audit: SimpleNamespace, registry: HealthRegistry) -> HealthProxyHandler:
    """Build a handler without a live socket (bypasses BaseHTTPRequestHandler.__init__)."""
    handler = HealthProxyHandler.__new__(HealthProxyHandler)
    handler.audit = audit
    handler.registry = registry
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler.wfile = MagicMock()
    return handler


def _fresh_audit() -> SimpleNamespace:
    return SimpleNamespace(
        requests_proxied=0,
        requests_blocked=0,
        cooldowns_applied=0,
    )


def test_respond_unavailable_increments_requests_blocked(tmp_path):
    audit = _fresh_audit()
    registry = HealthRegistry(filepath=tmp_path / "health.json")
    handler = _stub_handler(audit, registry)

    handler._respond_unavailable("all routers unavailable")

    assert audit.requests_blocked == 1
    assert audit.requests_proxied == 0
    handler.send_response.assert_called_once_with(503)


def test_handle_error_increments_cooldowns_applied(tmp_path):
    audit = _fresh_audit()
    registry = HealthRegistry(filepath=tmp_path / "health.json")
    handler = _stub_handler(audit, registry)

    handler._handle_error(
        503,
        '{"error": {"message": "boom", "type": "server_error"}}',
        {"model": "test-provider/test-model"},
    )

    assert audit.cooldowns_applied == 1
    assert registry.is_provider_healthy("test-provider") is False


def test_emit_upstream_response_increments_requests_proxied(tmp_path):
    audit = _fresh_audit()
    registry = HealthRegistry(filepath=tmp_path / "health.json")
    handler = _stub_handler(audit, registry)

    resp = MagicMock()
    resp.status = 200
    resp.headers = {"Content-Type": "text/plain"}
    resp.read.return_value = b"ok"

    handler._emit_upstream_response(resp, is_chat=False)

    assert audit.requests_proxied == 1
    assert audit.requests_blocked == 0


def test_counters_independent(tmp_path):
    """Blocked requests must not touch proxied and vice-versa."""
    audit = _fresh_audit()
    registry = HealthRegistry(filepath=tmp_path / "health.json")
    handler = _stub_handler(audit, registry)

    handler._respond_unavailable("nope")
    assert (audit.requests_proxied, audit.requests_blocked, audit.cooldowns_applied) == (0, 1, 0)

    resp = MagicMock()
    resp.status = 200
    resp.headers = {"Content-Type": "text/plain"}
    resp.read.return_value = b"ok"
    handler._emit_upstream_response(resp, is_chat=False)
    assert (audit.requests_proxied, audit.requests_blocked, audit.cooldowns_applied) == (1, 1, 0)
