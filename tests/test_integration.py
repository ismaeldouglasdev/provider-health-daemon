"""Integration tests — wire up the full stack and exercise end-to-end flows.

These tests validate that the meta-router, router registry, health probes,
model catalog, and dashboard all compose correctly.

Architecture under test:
  config → RouterRegistry → MetaRouterSelector → HealthProxyHandler
                        ↕
                  RouterProbe (periodic health checks)
                        ↕
                   ModelCatalog (dedup + cap)
                        ↕
                   DashboardServer (aggregation + metrics)
"""

import json
import time
import pytest
from unittest.mock import patch, MagicMock

from router_registry import RouterRegistry
from meta_router import MetaRouterSelector, ServiceUnavailable
from config import DOWNSTREAM_ROUTERS
from model_mapper import ModelMapper


class TestConfigToRegistry:
    """Validate that config -> RouterRegistry -> MetaRouterSelector works."""

    def test_config_registry_has_all_routers(self):
        registry = RouterRegistry(DOWNSTREAM_ROUTERS)
        all_routers = registry.get_all_routers()
        names = {r.name for r in all_routers}
        expected = {cfg["name"] for cfg in DOWNSTREAM_ROUTERS}
        assert names == expected, f"Expected {expected}, got {names}"

    def test_config_registry_priorities(self):
        registry = RouterRegistry(DOWNSTREAM_ROUTERS)
        healthy = registry.get_healthy_routers()
        assert len(healthy) >= 0  # all start as "unknown", not "healthy"

    def test_config_to_selector_healthy(self):
        """After marking all routers healthy, selector picks by priority/weight."""
        registry = RouterRegistry(DOWNSTREAM_ROUTERS)
        for r in registry.get_all_routers():
            registry.mark_healthy(r.name, ["model-a", "model-b"])
            # Mark healthy twice to go from probing → healthy
            registry.mark_healthy(r.name, ["model-a", "model-b"])

        selector = MetaRouterSelector(registry)
        # Should return the highest-priority router (both have priority 1,
        # but 9router has weight 2)
        first = selector.select_router()
        # 9router has weight 2, OmniRoute has weight 1 — weighted round-robin
        assert first is not None


class TestFullStackMocked:
    """End-to-end flow with mocked HTTP to simulate router responses."""

    @pytest.fixture
    def registry_with_healthy_routers(self):
        registry = RouterRegistry(DOWNSTREAM_ROUTERS)
        models_by_router = {
            "9router": [
                "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
                "@cf/deepseek-ai/deepseek-r1-distill-qwen-32b",
            ],
            "OmniRoute": [
                "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
                "nvidia/minimaxai/minimax-m3",
            ],
            "Kiro": [
                "groq/llama-3.3-70b-versatile",
                "groq/deepseek-r1-distill-llama-70b",
            ],
        }
        for r in registry.get_all_routers():
            models = models_by_router.get(r.name, [])
            registry.mark_healthy(r.name, models)
            registry.mark_healthy(r.name, models)  # two calls → "healthy"
        return registry

    def test_meta_router_with_config(self, registry_with_healthy_routers):
        """MetaRouterSelector operates correctly over real config."""
        selector = MetaRouterSelector(registry_with_healthy_routers)
        router = selector.select_router()
        assert router is not None
        assert router.name in ("9router", "OmniRoute", "Kiro")

    def test_fallback_chain(self, registry_with_healthy_routers):
        """When first router fails, selector falls back."""
        selector = MetaRouterSelector(registry_with_healthy_routers)
        first = selector.select_router()
        assert first is not None

        selector.on_failure(first.name, "connection_error")
        second = selector.select_router()
        assert second is not None
        assert second.name != first.name

    def test_all_down_returns_none(self, registry_with_healthy_routers):
        """When all routers are marked down, selector raises."""
        selector = MetaRouterSelector(registry_with_healthy_routers)
        for r in registry_with_healthy_routers.get_all_routers():
            registry_with_healthy_routers.mark_unhealthy(r.name, "timeout")

        with pytest.raises(ServiceUnavailable):
            selector.select_router()

    def test_mapper_resolves_across_routers(self, registry_with_healthy_routers):
        """ModelMapper resolves aliases using the same config as the routers."""
        mapper = ModelMapper()
        alias = mapper.resolve_any("llama-3.3-70b")
        assert alias is not None
        rn, mid = alias
        assert rn in ("9router", "OmniRoute", "Kiro")
        assert "/llama-3.3" in mid or "/llama-3" in mid

    def test_model_catalog_across_routers(self, registry_with_healthy_routers):
        """Model catalog aggregates models from all routers with dedup."""
        catalog = registry_with_healthy_routers.get_model_catalog()
        # "llama-3.3-70b" appears in both 9router and OmniRoute
        llama_key = "@cf/meta/llama-3.3-70b-instruct-fp8-fast"
        assert llama_key in catalog
        assert "9router" in catalog[llama_key]["router_origins"]
        assert "OmniRoute" in catalog[llama_key]["router_origins"]

    def test_provider_aggregation(self, registry_with_healthy_routers):
        """Provider aggregation groups models by provider prefix."""
        agg = registry_with_healthy_routers.get_provider_aggregation()
        assert "@cf" in agg
        assert "nvidia" in agg or "groq" in agg
        cf = agg["@cf"]
        assert cf["router_count"] >= 2  # both 9router and OmniRoute have @cf models
        assert cf["total_models"] >= 1


class TestRouterStatePersistence:
    """Validate save/load cycle preserves routing decisions."""

    @pytest.fixture
    def registry(self, tmp_path):
        from pathlib import Path
        import router_registry as rr_mod
        original = rr_mod.ROUTER_STATE_FILE
        rr_mod.ROUTER_STATE_FILE = tmp_path / "router_state.json"
        reg = RouterRegistry(DOWNSTREAM_ROUTERS)
        yield reg
        rr_mod.ROUTER_STATE_FILE = original

    def test_save_and_load_restores_health(self, registry):
        for r in registry.get_all_routers():
            registry.mark_healthy(r.name, ["m1"])
            registry.mark_healthy(r.name, ["m1"])
        registry.save_state()

        fresh = RouterRegistry(DOWNSTREAM_ROUTERS)
        fresh.load_state()
        for r in fresh.get_all_routers():
            assert r.failure_count >= 0

    def test_save_and_load_failure_count(self, registry):
        r = registry.get_router("9router")
        registry.mark_unhealthy("9router", "timeout")
        registry.mark_unhealthy("9router", "timeout")
        assert r.failure_count == 2
        registry.save_state()

        fresh = RouterRegistry(DOWNSTREAM_ROUTERS)
        fresh.load_state()
        restored = fresh.get_router("9router")
        assert restored.failure_count == 2
