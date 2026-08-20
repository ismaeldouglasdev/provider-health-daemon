"""Regression tests for combo model filtering.

A combo must never pick a model that is in cooldown even when its provider
is healthy. Doing so made the health gate 503 every retry of the same
combo (groq healthy but groq/openai/gpt-oss-120b in context_length
cooldown), producing an infinite retry loop:
`Model 'groq/openai/gpt-oss-120b' is in cooldown ... [retrying in 1m attempt #6]`
"""

import time
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

from health_registry import HealthRegistry
from proxy_handler import HealthProxyHandler
from smart_router import SmartRouter


def _stub_handler(registry: HealthRegistry) -> HealthProxyHandler:
    handler = cast(Any, HealthProxyHandler.__new__(HealthProxyHandler))
    handler.audit = SimpleNamespace(
        requests_proxied=0, requests_blocked=0, cooldowns_applied=0
    )
    handler.registry = registry
    handler.metrics_store = None
    handler.smart_router = None
    handler._combo_cache = []
    handler._combo_cache_time = time.time()
    return handler


def _cooldown_model(registry, model_id, provider, reason="context_length"):
    registry.mark_error(
        provider,
        {"model_specific": True, "cooldown": {"hours": 24, "type": reason}},
        model=model_id,
    )


def test_combo_skips_cooldown_model_when_provider_healthy(tmp_path):
    """Regression: provider healthy + model in cooldown must NOT be picked."""
    registry = HealthRegistry(filepath=tmp_path / "health.json")
    _cooldown_model(registry, "groq/openai/gpt-oss-120b", "groq")
    _cooldown_model(registry, "groq/llama-3.3-70b-versatile", "groq")

    assert registry.is_provider_healthy("groq") is True
    assert registry.is_model_available("groq/openai/gpt-oss-120b") is False

    handler = _stub_handler(registry)
    handler._combo_cache = ["groq/openai/gpt-oss-120b", "groq/llama-3.3-70b-versatile"]

    router = MagicMock()
    router.best_model.return_value = None  # smart router finds nothing usable
    router.fallback_chain.return_value = []  # global catalog also exhausted
    handler.smart_router = router

    with patch.dict("os.environ", {"OPENCODE_FALLBACK_MODEL": ""}, clear=False), \
         patch.object(
             SmartRouter,
             "get_default_combos",
             return_value=["groq/openai/gpt-oss-120b", "groq/llama-3.3-70b-versatile"],
         ):
        result = handler._filter_combo_providers({"model": "main-rr"})

    # No healthy candidate → pass through (None), NEVER available[0] (cooldown model)
    assert result is None
    router.best_model.assert_not_called()


def test_combo_picks_healthy_model_over_cooldown_one(tmp_path):
    """A healthy model wins over a cooldown model from the same pool."""
    registry = HealthRegistry(filepath=tmp_path / "health.json")
    _cooldown_model(registry, "groq/openai/gpt-oss-120b", "groq")

    handler = _stub_handler(registry)
    handler._combo_cache = ["groq/openai/gpt-oss-120b", "nvidia/minimaxai/minimax-m3"]

    router = MagicMock()
    router.best_model.return_value = "nvidia/minimaxai/minimax-m3"
    handler.smart_router = router

    with patch.dict("os.environ", {"OPENCODE_FALLBACK_MODEL": ""}, clear=False):
        result = handler._filter_combo_providers({"model": "main-rr"})

    assert result == "nvidia/minimaxai/minimax-m3"
    router.best_model.assert_called_once()


def test_combo_passthrough_when_smart_router_finds_nothing(tmp_path):
    """best_model None must pass through, not blindly return available[0]."""
    registry = HealthRegistry(filepath=tmp_path / "health.json")

    handler = _stub_handler(registry)
    handler._combo_cache = ["groq/openai/gpt-oss-120b", "nvidia/minimaxai/minimax-m3"]

    router = MagicMock()
    router.best_model.return_value = None
    handler.smart_router = router

    with patch.dict("os.environ", {"OPENCODE_FALLBACK_MODEL": ""}, clear=False):
        result = handler._filter_combo_providers({"model": "main-rr"})

    assert result is None


def test_combo_pool_broad_fallback_when_disabled_filter_exhausted(tmp_path):
    """When disabled-registry filter leaves only cooldown models, broaden pool."""
    registry = HealthRegistry(filepath=tmp_path / "health.json")
    _cooldown_model(registry, "groq/llama-3.3-70b-versatile", "groq")

    handler = _stub_handler(registry)
    narrow = ["groq/llama-3.3-70b-versatile"]
    broad = ["openrouter/cohere/north-mini-code:free", "llm7/codestral-latest"]

    with patch.object(
        SmartRouter,
        "get_default_combos",
        side_effect=lambda skip_disabled=False: narrow if not skip_disabled else broad,
    ):
        models = handler._get_combo_models()

    assert "groq/llama-3.3-70b-versatile" not in models
    assert len(models) >= 1


def test_combo_pool_excludes_cooldown_models(tmp_path):
    """Combo cache refresh must drop models in health-registry cooldown."""
    registry = HealthRegistry(filepath=tmp_path / "health.json")
    _cooldown_model(registry, "ollama/gpt-oss:120b", "ollama", reason="weekly_limit")

    handler = _stub_handler(registry)
    with patch.object(SmartRouter, "get_default_combos", return_value=[
        "ollama/gpt-oss:120b",
        "nvidia/minimaxai/minimax-m3",
    ]):
        models = handler._get_combo_models()

    assert "ollama/gpt-oss:120b" not in models
    assert "nvidia/minimaxai/minimax-m3" in models


def test_combo_all_providers_unavailable_returns_none(tmp_path):
    """Provider-wide cooldown still excludes candidates."""
    registry = HealthRegistry(filepath=tmp_path / "health.json")
    registry.mark_error(
        "nvidia",
        {"cooldown": {"hours": 24, "type": "generic_429"}},
    )

    handler = _stub_handler(registry)
    handler._combo_cache = ["nvidia/minimaxai/minimax-m3"]

    with patch.dict("os.environ", {"OPENCODE_FALLBACK_MODEL": ""}, clear=False), \
         patch.object(
             SmartRouter,
             "get_default_combos",
             return_value=["nvidia/minimaxai/minimax-m3"],
         ):
        result = handler._filter_combo_providers({"model": "main-rr"})

    assert result is None
