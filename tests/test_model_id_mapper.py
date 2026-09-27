"""Tests for canonical model ID mapping."""

from model_id_mapper import ModelIdMapper


def test_to_canonical_known_prefixes():
    mapper = ModelIdMapper({})
    assert mapper.to_canonical("groq/llama-3.3-70b-versatile") == "llama-3.3-70b-versatile"
    assert mapper.to_canonical("nvidia/deepseek-ai/deepseek-v4-pro") == "deepseek-ai/deepseek-v4-pro"
    assert mapper.to_canonical("anthropic/claude-sonnet-4.5") == "claude-sonnet-4.5"
    assert mapper.to_canonical("ollama/kimi-k2.5") == "kimi-k2.5"


def test_router_level_ids_pass_through():
    mapper = ModelIdMapper({})
    for model in ("main-rr", "combo-round-robin", "combo-fast", "combo-thinking", "kr/auto"):
        assert mapper.to_canonical(model) == model


def test_unknown_and_bare_ids_pass_through():
    mapper = ModelIdMapper({})
    assert mapper.to_canonical("claude-sonnet-4.5") == "claude-sonnet-4.5"
    assert mapper.to_canonical("custom/providerless-model") == "custom/providerless-model"
def test_reverse_uses_catalog_before_prefix_fallback():
    mapper = ModelIdMapper({
        "9router": {
            "prefixes": ["groq/", "nvidia/"],
            "catalog": {
                "groq/llama-3.3-70b-versatile": {},
                "nvidia/deepseek-ai/deepseek-v4-pro": {},
            },
        }
    })
    assert mapper.to_router_specific("llama-3.3-70b-versatile", "9router") == "groq/llama-3.3-70b-versatile"
    assert mapper.to_router_specific("deepseek-ai/deepseek-v4-pro", "9router") == "nvidia/deepseek-ai/deepseek-v4-pro"


def test_reverse_falls_back_without_catalog():
    mapper = ModelIdMapper({"9router": {"prefixes": ["groq/"]}})
    assert mapper.to_router_specific("llama-3.3-70b-versatile", "9router") == "groq/llama-3.3-70b-versatile"


def test_reverse_preserves_kiro_bare_ids():
    mapper = ModelIdMapper({})
    assert mapper.to_router_specific("claude-sonnet-4.5", "kiro") == "claude-sonnet-4.5"
def test_collision_is_reported():
    mapper = ModelIdMapper({
        "r1": {"prefixes": ["groq/"], "catalog": {"groq/foo": {}}},
        "r2": {"prefixes": ["nvidia/"], "catalog": {"nvidia/foo": {}}},
    })
    stats = mapper.get_mapping_stats()
    assert stats["collisions"] == 1
