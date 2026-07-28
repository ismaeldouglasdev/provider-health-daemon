"""Tests for ModelMapper."""

import pytest
from model_mapper import ModelMapper


class TestModelMapper:
    def test_resolve_known_alias(self):
        mapper = ModelMapper()
        result = mapper.resolve("9router", "llama-3.3-70b")
        assert result == "@cf/meta/llama-3.3-70b-instruct-fp8-fast"

    def test_resolve_unknown_router(self):
        mapper = ModelMapper()
        assert mapper.resolve("nonexistent", "llama-3.3-70b") is None

    def test_resolve_unknown_alias(self):
        mapper = ModelMapper()
        assert mapper.resolve("9router", "model-that-does-not-exist") is None

    def test_resolve_any_finds_first(self):
        mapper = ModelMapper()
        result = mapper.resolve_any("kimi-k2.6")
        assert result is not None
        rn, mid = result
        assert rn == "9router"
        assert mid == "@cf/moonshotai/kimi-k2.6"

    def test_resolve_any_unknown(self):
        mapper = ModelMapper()
        assert mapper.resolve_any("bogus-model-v99") is None

    def test_register_new(self):
        mapper = ModelMapper()
        mapper.register("TestRouter", "my-model", "test-org/my-model-v1")
        assert mapper.resolve("TestRouter", "my-model") == "test-org/my-model-v1"

    def test_register_update(self):
        mapper = ModelMapper()
        mapper.register("9router", "llama-3.3-70b", "custom-path/llama")
        assert mapper.resolve("9router", "llama-3.3-70b") == "custom-path/llama"

    def test_aliases_for(self):
        mapper = ModelMapper()
        aliases = mapper.aliases_for("Kiro")
        assert "llama-3.3-70b" in aliases
        assert aliases["llama-3.3-70b"] == "groq/llama-3.3-70b-versatile"

    def test_aliases_for_unknown(self):
        mapper = ModelMapper()
        assert mapper.aliases_for("nope") == {}

    def test_routers_for(self):
        mapper = ModelMapper()
        routers = mapper.routers_for("llama-3.3-70b")
        assert len(routers) >= 2
        names = {r[0] for r in routers}
        assert "9router" in names
        assert "OmniRoute" in names
        assert "Kiro" in names

    def test_routers_for_unknown(self):
        mapper = ModelMapper()
        assert mapper.routers_for("ghost-model") == []

    def test_reverse_map_exact(self):
        mapper = ModelMapper()
        alias = mapper.reverse_map("groq/llama-3.3-70b-versatile")
        assert alias == "llama-3.3-70b"

    def test_reverse_map_suffix(self):
        mapper = ModelMapper()
        alias = mapper.reverse_map("some-unknown-router/llama-3.3-70b-instruct-fp8-fast")
        assert alias == "llama-3.3-70b"

    def test_reverse_map_unknown(self):
        mapper = ModelMapper()
        assert mapper.reverse_map("totally/unknown/model") is None

    def test_empty_map(self):
        mapper = ModelMapper(router_map={})
        assert mapper.resolve("x", "y") is None
        assert mapper.resolve_any("y") is None
        assert mapper.aliases_for("x") == {}
        assert mapper.routers_for("y") == []
