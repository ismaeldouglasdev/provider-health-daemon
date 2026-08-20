"""Tests for the response cache + global 5xx fallback (Point B).

Response cache:
- Identical retries within TTL are served from cache (X-Health-Proxy-Cache:
  hit) instead of re-hitting upstream — a retry after an agent crash on the
  same prompt gets the exact same answer without burning a provider call.
- Streaming requests and error bodies are NEVER cached (a cached error would
  poison retries); the cache is bounded by RESPONSE_CACHE_MAX_BYTES.

Global fallback (Point B):
- A 5xx or access error (no_credit, model_not_found, subscription, …) from
  the router is retried with the next healthy model from the FULL catalog
  (not just the combo pool), bounded by MAX_FALLBACK_RETRIES to avoid a
  retry storm when the whole catalog is down. Models already tried are
  skipped. Streaming requests ARE retried on status errors — HTTPError is
  raised by opener.open() before any SSE is emitted to the client; only
  mid-stream connection failures (URLError) can't be replayed.
"""

import io
import json
import time
import urllib.error
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import proxy_handler
from proxy_handler import (
    HealthProxyHandler,
    MAX_FALLBACK_RETRIES,
    RESPONSE_CACHE_MAX_BYTES,
)
from smart_router import SmartRouter


def _stub_handler(registry=None) -> HealthProxyHandler:
    handler = HealthProxyHandler.__new__(HealthProxyHandler)
    handler.audit = SimpleNamespace(
        requests_proxied=0, requests_blocked=0, cooldowns_applied=0
    )
    handler.registry = registry
    handler.metrics_store = None
    handler.smart_router = None
    handler._combo_cache = []
    handler._combo_cache_time = time.time()
    # Instance-level cache (class attr is shared across tests)
    handler._response_cache = {}
    handler._response_cache_bytes = 0
    handler._fallback_attempts = 0
    handler._tried_models = set()
    handler._request_cache_key = ""
    return handler


def _chat_body(model="groq/llama-3.3-70b-versatile", msg="hello", stream=False):
    return {
        "model": model,
        "messages": [{"role": "user", "content": msg}],
        "stream": stream,
    }


def _ok_response(body: bytes) -> MagicMock:
    resp = MagicMock()
    resp.status = 200
    resp.headers = {"Content-Type": "application/json"}
    resp.read.return_value = body
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


def _http_error(code: int = 502, body: bytes = b'{"error": {"message": "upstream failed"}}') -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://router/v1/chat/completions",
        code,
        f"HTTP {code}",
        {},
        io.BytesIO(body),
    )


class TestCacheKey:
    def test_stable_across_calls(self):
        h = _stub_handler()
        assert h._cache_key(_chat_body()) == h._cache_key(_chat_body())

    def test_changes_with_model(self):
        h = _stub_handler()
        assert h._cache_key(_chat_body(model="a/x")) != h._cache_key(_chat_body(model="b/y"))

    def test_changes_with_messages(self):
        h = _stub_handler()
        assert h._cache_key(_chat_body(msg="hello")) != h._cache_key(_chat_body(msg="bye"))

    def test_changes_with_sampling_params(self):
        h = _stub_handler()
        a = _chat_body()
        b = _chat_body()
        b["temperature"] = 0.9
        assert h._cache_key(a) != h._cache_key(b)

    def test_stream_normalized_to_false(self):
        """stream=True and stream=False map to the same key (payload forces False)."""
        h = _stub_handler()
        assert h._cache_key(_chat_body(stream=True)) == h._cache_key(_chat_body(stream=False))


class TestCacheGetPut:
    def test_roundtrip(self):
        h = _stub_handler()
        body = _chat_body()
        h._cache_put(body, b'{"id":"1","choices":[]}')
        assert h._cache_get(body) == b'{"id":"1","choices":[]}'

    def test_miss_returns_none(self):
        h = _stub_handler()
        assert h._cache_get(_chat_body()) is None

    def test_streaming_never_cached(self):
        h = _stub_handler()
        h._cache_put(_chat_body(stream=True), b'{"id":"1"}')
        assert h._response_cache == {}
        assert h._cache_get(_chat_body(stream=True)) is None

    def test_error_body_never_cached(self):
        h = _stub_handler()
        h._cache_put(_chat_body(), b'{"error": {"message": "boom"}}')
        assert h._response_cache == {}
        assert h._cache_get(_chat_body()) is None

    def test_non_json_body_never_cached(self):
        h = _stub_handler()
        h._cache_put(_chat_body(), b"<html>502 Bad Gateway</html>")
        assert h._response_cache == {}

    def test_expired_entry_evicted_on_get(self):
        h = _stub_handler()
        key = h._cache_key(_chat_body())
        h._response_cache[key] = (time.time() - 1, b'{"id":"1"}')
        h._response_cache_bytes = 12
        assert h._cache_get(_chat_body()) is None
        assert key not in h._response_cache

    def test_byte_cap_evicts_oldest_first(self, monkeypatch):
        monkeypatch.setattr(proxy_handler, "RESPONSE_CACHE_MAX_BYTES", 50)
        h = _stub_handler()
        h._cache_put(_chat_body(msg="a"), b'{"id":"1","content":"aaaaaaaaaa"}')
        h._cache_put(_chat_body(msg="b"), b'{"id":"2","content":"bbbbbbbbbb"}')
        # First entry evicted (oldest expiry), second still served
        assert h._cache_get(_chat_body(msg="a")) is None
        assert h._cache_get(_chat_body(msg="b")) is not None
        assert h._response_cache_bytes <= 50


class TestDoPostCacheHit:
    def _post(self, h, body_dict):
        raw = json.dumps(body_dict).encode()
        h.path = "/v1/chat/completions"
        h.command = "POST"
        h.headers = SimpleNamespace(get=lambda k, *a: str(len(raw)) if k == "Content-Length" else None)
        h.rfile = io.BytesIO(raw)
        h.wfile = io.BytesIO()
        h.send_response = MagicMock()
        h.send_header = MagicMock()
        h.end_headers = MagicMock()
        h.meta_selector = None
        h.registry = None
        h.do_POST()

    def test_retry_within_ttl_served_from_cache(self):
        h = _stub_handler()
        cached = b'{"id":"1","choices":[{"message":{"content":"cached answer"}}]}'
        body = _chat_body()
        h._cache_put(body, cached)

        self._post(h, body)

        h.send_response.assert_called_once_with(200)
        h.send_header.assert_any_call("X-Health-Proxy-Cache", "hit")
        assert h.wfile.getvalue() == cached

    def test_no_cache_hit_forwards_upstream(self):
        h = _stub_handler()
        body = _chat_body()
        h.opener = MagicMock()
        h.opener.open.return_value = _ok_response(b'{"id":"1","choices":[]}')
        h.audit = SimpleNamespace(requests_proxied=0, requests_blocked=0, cooldowns_applied=0)
        h.metrics_store = None

        self._post(h, body)

        h.opener.open.assert_called_once()


class TestGlobalFallbackChain:
    def _setup_chain(self, registry, catalog, chain=None):
        handler = _stub_handler(registry)
        router = MagicMock()
        # Identity: return the catalog AS FILTERED by the registry (a real
        # fallback_chain re-ranks, never re-adds cooldown models).
        router.fallback_chain.side_effect = lambda cat, _reg: cat
        handler.smart_router = router
        patcher = patch.object(SmartRouter, "get_default_combos", return_value=catalog)
        return handler, patcher

    def test_next_fallback_skips_tried_models(self, tmp_path):
        from health_registry import HealthRegistry
        registry = HealthRegistry(filepath=tmp_path / "health.json")
        catalog = ["nvidia/minimaxai/minimax-m3", "groq/llama-3.3-70b-versatile"]
        handler, patcher = self._setup_chain(registry, catalog, catalog)
        with patcher:
            assert handler._next_fallback_model({"groq/llama-3.3-70b-versatile"}) == "nvidia/minimaxai/minimax-m3"

    def test_next_fallback_returns_none_when_all_tried(self, tmp_path):
        from health_registry import HealthRegistry
        registry = HealthRegistry(filepath=tmp_path / "health.json")
        catalog = ["nvidia/minimaxai/minimax-m3"]
        handler, patcher = self._setup_chain(registry, catalog, catalog)
        with patcher:
            assert handler._next_fallback_model({"nvidia/minimaxai/minimax-m3"}) is None

    def test_chain_excludes_cooldown_models(self, tmp_path):
        from health_registry import HealthRegistry
        registry = HealthRegistry(filepath=tmp_path / "health.json")
        registry.mark_error(
            "groq",
            {"model_specific": True, "cooldown": {"hours": 24, "type": "context_length"}},
            model="groq/llama-3.3-70b-versatile",
        )
        catalog = ["nvidia/minimaxai/minimax-m3", "groq/llama-3.3-70b-versatile"]
        handler, patcher = self._setup_chain(registry, catalog, catalog)
        with patcher:
            chain = handler._global_fallback_chain()
        assert "groq/llama-3.3-70b-versatile" not in chain
        assert "nvidia/minimaxai/minimax-m3" in chain


class TestForwardGlobalFallback:
    def _forward_stub(self, side_effect):
        h = _stub_handler()
        h.path = "/v1/chat/completions"
        h.command = "POST"
        h.headers = SimpleNamespace(get=lambda k, *a: None)
        h.meta_selector = None  # bypass router-of-routers
        h.opener = MagicMock()
        h.opener.open.side_effect = side_effect
        h.send_response = MagicMock()
        h.send_header = MagicMock()
        h.end_headers = MagicMock()
        h.wfile = io.BytesIO()

        registry = MagicMock()
        registry.get_available_models.side_effect = lambda models: models
        h.registry = registry
        router = MagicMock()
        router.fallback_chain.side_effect = lambda catalog, _reg: catalog
        h.smart_router = router
        return h

    @pytest.fixture(autouse=True)
    def _patch_catalog(self, monkeypatch):
        catalog = [
            "nvidia/minimaxai/minimax-m3",
            "groq/llama-3.3-70b-versatile",
            "cx/koo-tuned",
        ]
        monkeypatch.setattr(
            SmartRouter, "get_default_combos", lambda skip_disabled=False: catalog
        )

    def test_5xx_retries_with_next_model(self):
        ok = b'{"id":"1","choices":[{"message":{"role":"assistant","content":"retried ok"},"finish_reason":"stop"}]}'
        h = self._forward_stub([_http_error(502), _ok_response(ok)])

        h._forward(_chat_body())

        assert h.opener.open.call_count == 2
        # The second (retry) request must carry the fallback model
        second_req = h.opener.open.call_args_list[1][0][0]
        sent = json.loads(second_req.data)
        assert sent["model"] == "nvidia/minimaxai/minimax-m3"
        # First request kept the original model
        first_req = h.opener.open.call_args_list[0][0][0]
        assert json.loads(first_req.data)["model"] == "groq/llama-3.3-70b-versatile"

    def test_retries_bounded_by_max_fallback_retries(self):
        """All models failing → exactly 1 + MAX_FALLBACK_RETRIES opens, no storm."""
        h = self._forward_stub([_http_error(502), _http_error(502), _http_error(502)])

        h._forward(_chat_body())

        # 1 initial + MAX_FALLBACK_RETRIES retries, then stop
        assert h.opener.open.call_count == 1 + MAX_FALLBACK_RETRIES
        assert h._fallback_attempts == MAX_FALLBACK_RETRIES
        # Final error response sent (502), not a third retry
        assert h.send_response.call_args[0][0] == 502

    def test_streaming_5xx_retried(self):
        """HTTPError is raised before any SSE is sent, so streaming retries."""
        ok = b'{"id":"1","choices":[{"message":{"role":"assistant","content":"retried ok"},"finish_reason":"stop"}]}'
        h = self._forward_stub([_http_error(502), _ok_response(ok)])

        h._forward(_chat_body(stream=True))

        assert h.opener.open.call_count == 2
        # The second (retry) request must carry the fallback model
        second_req = h.opener.open.call_args_list[1][0][0]
        sent = json.loads(second_req.data)
        assert sent["model"] == "nvidia/minimaxai/minimax-m3"

    def test_non_5xx_never_retried(self):
        h = self._forward_stub([_http_error(429)])

        h._forward(_chat_body())

        assert h.opener.open.call_count == 1
        assert h.send_response.call_args[0][0] == 429

    def test_429_access_error_retries_with_next_model(self):
        ok = b'{"id":"1","choices":[{"message":{"role":"assistant","content":"retried ok"},"finish_reason":"stop"}]}'
        weekly = b'{"error": {"message": "you have reached your weekly usage limit, upgrade for higher limits"}}'
        h = self._forward_stub([_http_error(429, weekly), _ok_response(ok)])

        h._forward(_chat_body())

        assert h.opener.open.call_count == 2
        # The second (retry) request must carry the fallback model
        second_req = h.opener.open.call_args_list[1][0][0]
        sent = json.loads(second_req.data)
        assert sent["model"] == "nvidia/minimaxai/minimax-m3"

    def test_success_after_fallback_records_health_for_new_model(self):
        ok = b'{"id":"1","choices":[{"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}]}'
        h = self._forward_stub([_http_error(502), _ok_response(ok)])

        h._forward(_chat_body())

        # mark_healthy called for the FALLBACK model's provider (nvidia)
        h.registry.mark_healthy.assert_called_once_with("nvidia")
