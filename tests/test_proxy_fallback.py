"""Regression tests for the global-fallback gate in _forward.

Bug: a streaming request hitting a dead model with HTTP 402 (no_credit,
e.g. "Insufficient balance." from llm7/kimi-k3) did NOT trigger the global
fallback. Two gate defects:

1. Only 5xx and 429-access errors fell back — 401/402/403/404 access errors
   (no_credit, model_not_found, auth_invalid, …) leaked straight into the
   anti-leak path → the agent got a 503 "provider temporarily unavailable"
   and opencode's compaction stalled.
2. `not body.get("stream")` blocked fallback for every streaming request
   (opencode always streams). Safe to retry: HTTPError is raised by
   opener.open() BEFORE any SSE is emitted to the client — only mid-stream
   URLError failures can't be replayed.

Regression: 402 no_credit + stream=True MUST fall back to the next healthy
model. Negative: generic 429 (rate limit) must NOT fall back.
"""

import io
import json
import urllib.error
from http.client import HTTPMessage
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

from health_registry import HealthRegistry
from proxy_handler import HealthProxyHandler
from smart_router import SmartRouter


class _FakeOpener:
    """Opener whose open() replays a scripted sequence of outcomes.

    Each outcome is either ("error", status, body_bytes) — raises
    urllib.error.HTTPError — or ("ok", resp) — returns the response object.
    """

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def open(self, req, timeout=180):
        self.calls.append(req.full_url)
        kind = self.outcomes.pop(0)
        if kind[0] == "error":
            _, status, body = kind
            raise urllib.error.HTTPError(
                req.full_url, status, "Error", HTTPMessage(), io.BytesIO(body)
            )
        return kind[1]


class _FakeSSEResponse:
    """Context-manager SSE response (text/event-stream) with data: lines."""

    status = 200

    def __init__(self, lines):
        self.lines = list(lines)
        self.headers = {"Content-Type": "text/event-stream"}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def readline(self):
        return self.lines.pop(0) if self.lines else b""

    def __iter__(self):
        while self.lines:
            yield self.lines.pop(0)


class _FakeJSONResponse:
    """Context-manager non-streaming JSON response (application/json)."""

    status = 200

    def __init__(self, body: bytes):
        self.body = body
        self.headers = {"Content-Type": "application/json"}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body


def _stub_handler(registry: HealthRegistry, opener: _FakeOpener) -> tuple[HealthProxyHandler, io.BytesIO]:
    """Return (handler, wfile) — wfile is the local BytesIO for assertions."""
    handler = cast(Any, HealthProxyHandler.__new__(HealthProxyHandler))
    handler.path = "/v1/chat/completions"
    handler.command = "POST"
    handler.headers = {"Content-Type": "application/json"}
    handler.opener = opener
    handler.meta_selector = None
    handler.registry = registry
    handler.metrics_store = None
    handler.audit = SimpleNamespace(
        requests_proxied=0, requests_blocked=0, cooldowns_applied=0
    )
    handler._fallback_attempts = 0
    handler._tried_models = set()
    handler._request_cache_key = ""
    handler._combo_cache = []
    handler._combo_cache_time = 0.0
    wfile = io.BytesIO()
    handler.wfile = wfile
    handler.send_response = lambda code: None
    handler.send_header = lambda k, v: None
    handler.end_headers = lambda: None
    return handler, wfile


def _fallback_env(smart_router: MagicMock):
    """Patch the catalog so _global_fallback_chain sees the full pool."""
    return patch.object(
        SmartRouter,
        "get_default_combos",
        return_value=["dead/model-a", "healthy/model-b"],
    )


def _stream_body(model: str = "dead/model-a") -> dict:
    return {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    }


def _sse_ok() -> _FakeSSEResponse:
    return _FakeSSEResponse([
        b'data: {"id":"c1","object":"chat.completion.chunk",'
        b'"choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}\n\n',
        b'data: {"id":"c1","object":"chat.completion.chunk",'
        b'"choices":[{"index":0,"delta":{"content":"hello from healthy"},"finish_reason":null}]}\n\n',
        b"data: [DONE]\n\n",
    ])


def _sse_empty() -> _FakeSSEResponse:
    """SSE stream with NO usable content (role-only chunk + [DONE]).

    Mirrors a dead model / exhausted quota upstream: HTTP 200, but the stream
    never carries content. First line has the "data: " prefix so the proxy's
    real-SSE detection keeps is_streaming=True.
    """
    return _FakeSSEResponse([
        b'data: {"id":"c1","object":"chat.completion.chunk",'
        b'"choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}\n\n',
        b"data: [DONE]\n\n",
    ])


def _json_empty() -> _FakeJSONResponse:
    """Non-streaming JSON 200 with zero choices (dead model)."""
    return _FakeJSONResponse(b'{"id":"c1","object":"chat.completion","choices":[]}')


def _json_ok() -> _FakeJSONResponse:
    """Non-streaming JSON 200 with real content (healthy model)."""
    return _FakeJSONResponse(
        b'{"id":"c2","object":"chat.completion","choices":['
        b'{"index":0,"message":{"role":"assistant","content":"hello from healthy"},'
        b'"finish_reason":"stop"}]}'
    )


def _no_credit_402_body() -> bytes:
    return json.dumps({
        "error": {
            "message": "Insufficient balance.",
            "type": "insufficient_quota",
            "code": "insufficient_balance",
        }
    }).encode()


def test_402_no_credit_streaming_triggers_global_fallback(tmp_path):
    """Regression: 402 no_credit on a STREAMING request must fall back."""
    registry = HealthRegistry(filepath=tmp_path / "health.json")
    opener = _FakeOpener([
        ("error", 402, _no_credit_402_body()),
        ("ok", _sse_ok()),
    ])
    handler, wfile = _stub_handler(registry, opener)

    router = MagicMock()
    router.fallback_chain.return_value = ["healthy/model-b"]
    handler.smart_router = router

    body = _stream_body()
    with _fallback_env(router):
        handler._forward(body)

    # Fallback attempted exactly once, retried on the next healthy model
    assert handler._fallback_attempts == 1
    assert len(opener.calls) == 2
    assert body["model"] == "healthy/model-b"

    # Dead provider recorded in cooldown (no_credit → provider scope)
    assert registry.is_provider_healthy("dead") is False

    # Client received the fallback model's SSE, not the anti-leak 503
    out = wfile.getvalue()
    assert b"hello from healthy" in out
    assert b"provider temporarily unavailable" not in out


def test_429_generic_rate_limit_does_not_fallback(tmp_path):
    """Negative: generic 429 (rate limit) is transient — never falls back."""
    registry = HealthRegistry(filepath=tmp_path / "health.json")
    opener = _FakeOpener([
        ("error", 429, b'{"error":{"message":"Rate limit exceeded.","type":"rate_limit_exceeded"}}'),
    ])
    handler, wfile = _stub_handler(registry, opener)
    handler.smart_router = MagicMock()

    body = _stream_body()
    with _fallback_env(handler.smart_router):
        handler._forward(body)

    # No retry — generic_429 is not in _ACCESS_ERROR_TYPES
    assert handler._fallback_attempts == 0
    assert len(opener.calls) == 1
    assert wfile.getvalue() != b""


def test_503_streaming_still_falls_back(tmp_path):
    """5xx on a streaming request still falls back (gate widened for stream)."""
    registry = HealthRegistry(filepath=tmp_path / "health.json")
    opener = _FakeOpener([
        ("error", 503, b'{"error":{"message":"Bad gateway"}}'),
        ("ok", _sse_ok()),
    ])
    handler, wfile = _stub_handler(registry, opener)

    router = MagicMock()
    router.fallback_chain.return_value = ["healthy/model-b"]
    handler.smart_router = router

    body = _stream_body()
    with _fallback_env(router):
        handler._forward(body)

    assert handler._fallback_attempts == 1
    assert len(opener.calls) == 2
    assert b"hello from healthy" in wfile.getvalue()


def test_402_no_credit_marks_provider_cooldown(tmp_path):
    """no_credit is provider-scoped: whole provider leaves the pool."""
    registry = HealthRegistry(filepath=tmp_path / "health.json")
    opener = _FakeOpener([("error", 402, _no_credit_402_body())])
    handler, _ = _stub_handler(registry, opener)
    handler.smart_router = MagicMock()
    handler.smart_router.fallback_chain.return_value = []

    body = _stream_body()
    with _fallback_env(handler.smart_router):
        handler._forward(body)

    assert registry.is_provider_healthy("dead") is False
    assert registry.is_model_available("dead/model-a") is False


def test_empty_200_streaming_triggers_global_fallback(tmp_path):
    """Regression: Empty 200 on a STREAMING request must fall back.

    A dead model answers HTTP 200 with a content-less SSE stream. Previously
    this was emitted straight to the client (silent session completion — the
    "new session didn't respond to ping" bug) and only detected afterwards in
    _record_upstream_health, too late to retry. Now _emit_upstream_response
    raises EmptyUpstreamResponse BEFORE any byte reaches the client, and
    _forward replays the request on the next healthy model.
    """
    registry = HealthRegistry(filepath=tmp_path / "health.json")
    opener = _FakeOpener([
        ("ok", _sse_empty()),
        ("ok", _sse_ok()),
    ])
    handler, wfile = _stub_handler(registry, opener)

    router = MagicMock()
    router.fallback_chain.return_value = ["healthy/model-b"]
    handler.smart_router = router

    body = _stream_body()
    with _fallback_env(router):
        handler._forward(body)

    # Fallback attempted exactly once, retried on the next healthy model
    assert handler._fallback_attempts == 1
    assert len(opener.calls) == 2
    assert body["model"] == "healthy/model-b"

    # Dead model recorded in 15m empty_response cooldown (model-scoped)
    assert registry.is_model_available("dead/model-a") is False

    # Client received the fallback model's SSE, not a silent empty stream
    out = wfile.getvalue()
    assert b"hello from healthy" in out
    assert b"provider temporarily unavailable" not in out


def test_empty_200_nonstreaming_triggers_global_fallback(tmp_path):
    """Regression: Empty 200 on a NON-streaming chat request must fall back."""
    registry = HealthRegistry(filepath=tmp_path / "health.json")
    opener = _FakeOpener([
        ("ok", _json_empty()),
        ("ok", _json_ok()),
    ])
    handler, wfile = _stub_handler(registry, opener)

    router = MagicMock()
    router.fallback_chain.return_value = ["healthy/model-b"]
    handler.smart_router = router

    body = _stream_body()
    body["stream"] = False
    with _fallback_env(router):
        handler._forward(body)

    assert handler._fallback_attempts == 1
    assert len(opener.calls) == 2
    assert body["model"] == "healthy/model-b"
    assert registry.is_model_available("dead/model-a") is False

    out = wfile.getvalue()
    assert b"hello from healthy" in out
    assert b"provider temporarily unavailable" not in out


def test_empty_200_no_fallback_responds_503(tmp_path):
    """Empty 200 with no healthy fallback → explicit 503, never a silent empty 200."""
    registry = HealthRegistry(filepath=tmp_path / "health.json")
    opener = _FakeOpener([("ok", _sse_empty())])
    handler, wfile = _stub_handler(registry, opener)
    handler.smart_router = MagicMock()
    handler.smart_router.fallback_chain.return_value = []

    body = _stream_body()
    with _fallback_env(handler.smart_router):
        handler._forward(body)

    # No retry available — client gets a loud 503 instead of an empty 200
    assert handler._fallback_attempts == 0
    assert len(opener.calls) == 1
    out = wfile.getvalue()
    assert b"provider_unavailable" in out
    assert b"hello from healthy" not in out


def test_empty_200_healthy_stream_unaffected(tmp_path):
    """Negative: healthy streams never trigger the empty-200 guard."""
    registry = HealthRegistry(filepath=tmp_path / "health.json")
    opener = _FakeOpener([("ok", _sse_ok())])
    handler, wfile = _stub_handler(registry, opener)
    handler.smart_router = MagicMock()

    with _fallback_env(handler.smart_router):
        handler._forward(_stream_body())

    assert handler._fallback_attempts == 0
    assert len(opener.calls) == 1
    assert b"hello from healthy" in wfile.getvalue()


def test_router_selected_unclassified_4xx_triggers_global_fallback(tmp_path):
    """Router-selected model returns an UNCLASSIFIED 404 → global fallback to
    the next healthy model (previously only 5xx / access-error types fell back).
    """
    registry = HealthRegistry(filepath=tmp_path / "health.json")
    opener = _FakeOpener([
        ("error", 404, b'{"error":{"message":"Some mysterious upstream failure","code":404}}'),
        ("ok", _sse_ok()),
    ])
    handler, wfile = _stub_handler(registry, opener)

    router = MagicMock()
    router.fallback_chain.return_value = ["healthy/model-b"]
    handler.smart_router = router
    handler._router_selected = True

    body = _stream_body()
    with _fallback_env(router):
        handler._forward(body)

    assert handler._fallback_attempts == 1
    assert len(opener.calls) == 2
    assert body["model"] == "healthy/model-b"
    assert b"hello from healthy" in wfile.getvalue()


def test_router_selected_unclassified_4xx_shielded_as_503(tmp_path):
    """Anti-leak: router-selected model returns an UNCLASSIFIED 404 and no
    healthy fallback remains → client gets a generic 503, never the raw 404
    (which previously stalled opencode)."""
    registry = HealthRegistry(filepath=tmp_path / "health.json")
    opener = _FakeOpener([
        ("error", 404, b'{"error":{"message":"Some mysterious upstream failure","code":404}}'),
    ])
    handler, wfile = _stub_handler(registry, opener)
    handler.smart_router = MagicMock()
    handler.smart_router.fallback_chain.return_value = []
    handler._router_selected = True

    statuses = []
    cast(Any, handler).send_response = statuses.append

    body = _stream_body()
    with _fallback_env(handler.smart_router):
        handler._forward(body)

    assert handler._fallback_attempts == 0
    assert len(opener.calls) == 1
    assert statuses == [503]
    out = wfile.getvalue()
    assert b"provider_unavailable" in out
    assert b"Some mysterious upstream failure" not in out


def test_direct_request_unclassified_4xx_returns_raw(tmp_path):
    """Negative: a DIRECT (non-router-selected) request keeps today's behavior —
    an unclassified 4xx is normalized and returned with its original status."""
    registry = HealthRegistry(filepath=tmp_path / "health.json")
    opener = _FakeOpener([
        ("error", 404, b'{"error":{"message":"Some mysterious upstream failure","code":404}}'),
    ])
    handler, wfile = _stub_handler(registry, opener)
    handler.smart_router = MagicMock()
    handler.smart_router.fallback_chain.return_value = []
    handler._router_selected = False

    statuses = []
    cast(Any, handler).send_response = statuses.append

    body = _stream_body()
    with _fallback_env(handler.smart_router):
        handler._forward(body)

    assert handler._fallback_attempts == 0
    assert len(opener.calls) == 1
    assert statuses == [404]
    assert b"provider_unavailable" not in wfile.getvalue()
