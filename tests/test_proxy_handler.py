"""Regression tests for proxy_handler fast-path features.

- _strip_anthropic_caching: opencode emits Anthropic-style cache_control
  blocks which strict OpenAI-compatible schemas (mistral 422) reject.
- _trivial_reply: "ping"-style single-word messages get a canned reply
  from the proxy itself instead of burning seconds of LLM thinking time.
"""

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
