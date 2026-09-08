"""Regression tests for proxy_handler fast-path features.

- _strip_anthropic_caching: opencode emits Anthropic-style cache_control
  blocks which strict OpenAI-compatible schemas (mistral 422) reject.
- _trivial_reply: "ping"-style single-word messages get a canned reply
  from the proxy itself instead of burning seconds of LLM thinking time.
- _record_usage TTFT: the real time-to-first-byte measured at the first
  SSE line must be persisted, not a copy of the total duration.
"""

import time
import urllib.error

import pytest

from proxy_handler import _is_empty_chat_response, _strip_anthropic_caching, _trivial_reply


class TestStripAnthropicCaching:
    def test_removes_top_level_cache_control(self):
        obj = {"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}
        _strip_anthropic_caching(obj)
        assert obj == {"type": "text", "text": "hi"}

    def test_removes_nested_cache_control_in_messages(self):
        obj = {
            "messages": [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "hi", "cache_control": {"type": "ephemeral"}},
            ]
        }
        _strip_anthropic_caching(obj)
        assert "cache_control" not in obj["messages"][1]
        assert obj["messages"][0] == {"role": "system", "content": "sys"}

    def test_removes_cache_control_inside_content_blocks(self):
        obj = {
            "content": [
                {"type": "text", "text": "a", "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": "b"},
            ]
        }
        _strip_anthropic_caching(obj)
        assert all("cache_control" not in c for c in obj["content"])
        assert obj["content"][1] == {"type": "text", "text": "b"}

    def test_preserves_other_fields_and_nested_lists(self):
        obj = {
            "stream_options": {"include_usage": True},
            "tools": [
                {"function": {"name": "f", "parameters": {"x": 1}}, "cache_control": {"type": "ephemeral"}},
            ],
        }
        _strip_anthropic_caching(obj)
        assert obj["stream_options"] == {"include_usage": True}
        assert obj["tools"][0] == {"function": {"name": "f", "parameters": {"x": 1}}}

    def test_handles_primitives_without_error(self):
        for obj in ["plain string", 42, None, ["a", 1]]:
            _strip_anthropic_caching(obj)  # must not raise

    def test_mutates_in_place_and_returns_none(self):
        obj = {"cache_control": {"type": "ephemeral"}}
        assert _strip_anthropic_caching(obj) is None
        assert obj == {}


class TestTrivialReply:
    def test_ping_returns_pong(self):
        body = {"model": "main-rr", "messages": [{"role": "user", "content": "ping"}]}
        assert _trivial_reply(body) == "pong"

    def test_pong_returns_ping(self):
        body = {"model": "main-rr", "messages": [{"role": "user", "content": "pong"}]}
        assert _trivial_reply(body) == "ping"

    def test_ping_case_insensitive_and_punctuation(self):
        body = {"model": "main-rr", "messages": [{"role": "user", "content": "Ping!"}]}
        assert _trivial_reply(body) == "pong"

    def test_known_word_after_system_message(self):
        body = {"model": "main-rr", "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "ping"},
        ]}
        assert _trivial_reply(body) == "pong"

    def test_multiple_user_messages_uses_last(self):
        body = {"model": "main-rr", "messages": [
            {"role": "user", "content": "oi"},
            {"role": "assistant", "content": "oi!"},
            {"role": "user", "content": "ping"},
        ]}
        assert _trivial_reply(body) == "pong"

    def test_unknown_word_returns_none(self):
        body = {"model": "main-rr", "messages": [{"role": "user", "content": "banana"}]}
        assert _trivial_reply(body) is None

    def test_greeting_not_trivial(self):
        body = {"model": "main-rr", "messages": [{"role": "user", "content": "oi"}]}
        assert _trivial_reply(body) is None

    def test_generic_ack_not_trivial(self):
        body = {"model": "main-rr", "messages": [{"role": "user", "content": "ok"}]}
        assert _trivial_reply(body) is None

    def test_multiword_sentence_returns_none(self):
        body = {"model": "main-rr", "messages": [{"role": "user", "content": "ping the server now"}]}
        assert _trivial_reply(body) is None

    def test_tools_present_returns_none(self):
        body = {
            "model": "main-rr",
            "tools": [{"type": "function", "function": {"name": "f"}}],
            "messages": [{"role": "user", "content": "ping"}],
        }
        assert _trivial_reply(body) is None

    def test_last_message_not_user_returns_none(self):
        body = {"model": "main-rr", "messages": [{"role": "assistant", "content": "ping"}]}
        assert _trivial_reply(body) is None

    def test_multimodal_content_returns_none(self):
        body = {"model": "main-rr", "messages": [{"role": "user", "content": [{"type": "image_url"}]}]}
        assert _trivial_reply(body) is None

    def test_empty_messages_returns_none(self):
        assert _trivial_reply({"model": "main-rr", "messages": []}) is None

    def test_whitespace_only_returns_none(self):
        body = {"model": "main-rr", "messages": [{"role": "user", "content": "   "}]}
        assert _trivial_reply(body) is None


class TestIsEmptyChatResponse:
    """Regression: _is_empty_chat_response must NOT flag live reasoning models.

    Root cause (2026-08-11): the 9router → glm upstream glued a trailing
    "data: [DONE]" onto a NON-streaming JSON body on the same line. The
    guard saw "data:" in the text, entered the SSE branch, but the JSON had
    no "data: " prefix so every line was skipped → returned True (empty) →
    live glm-5.1 got a 24h model_not_found cooldown.
    """

    def _reasoning_json(self) -> bytes:
        """Non-streaming completion with empty content + reasoning_content."""
        import json

        data = {
            "choices": [
                {
                    "finish_reason": "length",
                    "index": 0,
                    "message": {
                        "content": "",
                        "reasoning_content": "1.  **Analyze the Request:**\n",
                        "role": "assistant",
                    },
                }
            ],
            "created": 1,
            "id": "x",
            "model": "glm-5.1",
            "object": "chat.completion",
            "request_id": "x",
            "usage": {},
        }
        return json.dumps(data).encode()

    def test_json_with_glued_done_is_not_empty(self):
        body = self._reasoning_json() + b"data: [DONE]"
        assert _is_empty_chat_response(body) is False

    def test_json_with_separated_done_is_not_empty(self):
        body = self._reasoning_json() + b"\ndata: [DONE]\n"
        assert _is_empty_chat_response(body) is False

    def test_sse_reasoning_is_not_empty(self):
        body = (
            b'data: {"choices":[{"delta":{"reasoning_content":"x","role":"assistant"}}]}\n'
            b"\n"
            b"data: [DONE]\n"
        )
        assert _is_empty_chat_response(body) is False

    def test_sse_with_content_is_not_empty(self):
        body = (
            b'data: {"choices":[{"delta":{"content":"hi","role":"assistant"}}]}\n'
            b"\n"
            b"data: [DONE]\n"
        )
        assert _is_empty_chat_response(body) is False

    def test_sse_truly_empty_is_empty(self):
        body = (
            b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n'
            b"\n"
            b'data: {"choices":[{"delta":{},"finish_reason":null}]}\n'
            b"\n"
            b"data: [DONE]\n"
        )
        assert _is_empty_chat_response(body) is True

    def test_blank_body_is_empty(self):
        assert _is_empty_chat_response(b"") is True

    def test_non_json_error_text_is_not_empty(self):
        assert _is_empty_chat_response(b"upstream exploded") is False


class TestRecordUsageTTFT:
    """Fase 1: the ttft_ms measured at the first SSE line must reach the
    persisted RequestRecord — not be overwritten by the total duration."""

    @staticmethod
    def _handler():
        from proxy_handler import HealthProxyHandler
        from metrics_store import MetricsStore

        handler = HealthProxyHandler.__new__(HealthProxyHandler)
        handler.metrics_store = MetricsStore()
        return handler

    def test_uses_measured_ttft_when_provided(self):
        h = self._handler()
        body = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n'
        h._record_usage(
            {}, body, time.time() - 10, "ag", "ag/gemini-3.5-flash", True, ttft_ms=1500,
        )
        rec = h.metrics_store.records[-1]
        assert rec.ttft_ms == 1500
        assert rec.duration_ms >= 9000
        assert rec.ttft_ms != rec.duration_ms

    def test_falls_back_to_duration_when_ttft_zero(self):
        h = self._handler()
        h._record_usage(
            {}, b"", time.time() - 5, "ag", "ag/gemini-3.5-flash",
            False, "connection_error",
        )
        rec = h.metrics_store.records[-1]
        assert rec.ttft_ms == rec.duration_ms


class TestFallbackRouterPenalty:
    """Regression: a dead fallback router must be penalized with on_failure()
    when its connection fails — otherwise the meta-selector keeps it
    "healthy" and re-selects it on every request needing fallback, burning a
    failed round-trip each time (previously swallowed with a bare `pass`).
    Mirrors the primary-router rule: connection_error (URLError) → penalize;
    HTTPError (router responded) → upstream issue, not a router issue."""

    class _FakeRouter:
        def __init__(self, name, url):
            self.name = name
            self.url = url
            self.auth = None

    class _FakeSelector:
        def __init__(self, routers):
            self._routers = list(routers)
            self.failures = []

        def select_router(self, model=""):
            return self._routers.pop(0) if self._routers else None

        def on_failure(self, router_name, error_type=None):
            self.failures.append((router_name, error_type))

        def on_success(self, router_name):
            pass

    class _DeadOpener:
        """opener whose open() always raises — simulates unreachable routers."""

        def open(self, req, timeout=None):
            raise urllib.error.URLError("Connection refused")

    @staticmethod
    def _handler(selector, opener):
        from metrics_store import MetricsStore
        from proxy_handler import HealthProxyHandler

        handler = HealthProxyHandler.__new__(HealthProxyHandler)
        handler.metrics_store = MetricsStore()
        handler.meta_selector = selector
        handler.opener = opener
        handler.path = "/v1/chat/completions"
        handler.command = "POST"
        handler.headers = {}
        handler.audit = None
        handler._respond_unavailable = lambda msg: None  # avoid socket I/O
        return handler

    def test_fallback_router_is_penalized_on_connection_error(self):
        selector = self._FakeSelector([
            self._FakeRouter("primary", "http://router-a:8000"),
            self._FakeRouter("fallback", "http://router-b:8000"),
        ])
        handler = self._handler(selector, self._DeadOpener())

        handler._forward({"model": "groq/llama-3.3-70b-versatile", "stream": False})

        assert ("primary", "connection_error") in selector.failures
        assert ("fallback", "connection_error") in selector.failures


class TestDetectDegeneration:
    """Guard against models that repeat the same sentence endlessly."""

    def test_detects_repeated_sentence(self):
        from proxy_handler import _detect_degeneration
        text = "Deixa eu verificar o que o plugin fornece. " * 25
        hit = _detect_degeneration(text)
        assert hit is not None
        assert hit[1] >= 12

    def test_normal_text_no_false_positive(self):
        from proxy_handler import _detect_degeneration
        text = " ".join(f"Parágrafo {i} com conteúdo distinto e variado." for i in range(500))
        assert _detect_degeneration(text) is None

    def test_below_threshold_not_detected(self):
        from proxy_handler import _detect_degeneration
        text = "Mesma frase repetida. " * 5  # 5 < 12
        assert _detect_degeneration(text) is None

    def test_short_sentences_ignored(self):
        from proxy_handler import _detect_degeneration
        text = "ok. " * 100  # "ok" (2 chars) < MIN_SENTENCE_LEN (5)
        assert _detect_degeneration(text) is None

    def test_empty_or_short_text(self):
        from proxy_handler import _detect_degeneration
        assert _detect_degeneration("") is None
        assert _detect_degeneration("abc") is None

    def test_short_phrase_loop_detected(self):
        # The observed 2026-08-31 loop: short phrases ("Vou rodar", "Executando",
        # "Rodando") repeated dozens of times — each below the old 15-char MIN.
        # With MIN_SENTENCE_LEN=5 and REPEAT_THRESHOLD=12 these are caught.
        from proxy_handler import _detect_degeneration
        text = ("Vou rodar. " * 30) + "Conteúdo final. "
        hit = _detect_degeneration(text)
        assert hit is not None
        assert "Vou rodar" in hit[0]

    def test_stuck_loop_ratio_detected(self):
        # Near-identical short phrases dominating the window below the absolute
        # threshold still trigger via the ratio detector (>= 30% + >= 8).
        from proxy_handler import _detect_degeneration
        # 10 "Executando." + 20 distinct sentences → "Executando" is 10/30=33%.
        text = ("Executando. " * 10) + " ".join(f"Passo {i} único." for i in range(20))
        hit = _detect_degeneration(text)
        assert hit is not None

    def test_ratio_no_false_positive_on_balanced_text(self):
        from proxy_handler import _detect_degeneration
        # Every sentence unique → no single phrase dominates → no false positive.
        text = " ".join(f"Frase distinta {i} de exemplo." for i in range(200))
        assert _detect_degeneration(text) is None


class TestFindHealthyAlternativeC2:
    """Cascade-fix 2026-08-31: SmartRouter must ONLY smart-route combo-family
    virtual models. Direct agent models (amd/DeepSeek-V4-Flash, kr/claude-*)
    signal an explicit choice — if unavailable they must return None (→ 503)
    so the opencode fallback_models chain (big-pickle → deepseek → combo) runs,
    instead of being silently swapped to a combo model by the proxy."""

    @staticmethod
    def _handler(smart_router=None, registry=None):
        from proxy_handler import HealthProxyHandler
        handler = HealthProxyHandler.__new__(HealthProxyHandler)
        handler.smart_router = smart_router
        handler.registry = registry
        return handler

    def test_direct_model_is_never_smart_routed(self):
        # smart_router must NOT be consulted for a direct model
        class NoCallRouter:
            def best_model(self, *a, **k):
                raise AssertionError("smart_router must not be called for direct models")
        h = self._handler(smart_router=NoCallRouter(), registry=object())
        assert h._find_healthy_alternative({"model": "amd/DeepSeek-V4-Flash"}) is None

    def test_direct_provider_slash_model_is_never_smart_routed(self):
        class NoCallRouter:
            def best_model(self, *a, **k):
                raise AssertionError("must not smart-route a direct model")
        h = self._handler(smart_router=NoCallRouter(), registry=object())
        assert h._find_healthy_alternative({"model": "kr/claude-sonnet-4"}) is None

    def test_combo_model_is_smart_routed(self):
        from smart_router import SmartRouter

        class FakeCounter:
            @staticmethod
            def count_tokens(*a, **k):
                return 10

        h = self._handler(registry=object())
        h._prompt_tokens = lambda body: 10
        # _get_combo_models is stubbed to return a fixed pool
        h._get_combo_models = lambda: ["ollama/gpt-oss:120b", "ag/gemini-3.7-flash-low"]
        h.smart_router = object.__new__(SmartRouter)
        h.smart_router.best_model = lambda models, registry, meta: "ag/gemini-3.7-flash-low"
        out = h._find_healthy_alternative({"model": "combo-round-robin"})
        assert out == "ag/gemini-3.7-flash-low"

    def test_combo_model_with_no_healthy_alternative_returns_none(self):
        from smart_router import SmartRouter
        h = self._handler(registry=object())
        h._prompt_tokens = lambda body: 10
        h._get_combo_models = lambda: ["ollama/gpt-oss:120b"]
        h.smart_router = object.__new__(SmartRouter)
        h.smart_router.best_model = lambda models, registry, meta: None
        assert h._find_healthy_alternative({"model": "combo-round-robin"}) is None
