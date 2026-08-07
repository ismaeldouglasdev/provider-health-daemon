"""Regression tests for proxy_handler._strip_anthropic_caching.

The opencode AI SDK emits Anthropic-style cache_control blocks which strict
OpenAI-compatible schemas (e.g. mistral 422 extra_forbidden) reject. This
test locks the stripping behaviour so a refactor cannot silently drop it.
"""

import pytest

from proxy_handler import _strip_anthropic_caching


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
