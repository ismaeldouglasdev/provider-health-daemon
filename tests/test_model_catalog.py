import pytest
from model_catalog import ModelCatalog
from router_registry import RouterRegistry


@pytest.fixture
def populated_registry():
    cfg = [
        {"name": "r1", "url": "http://a:1", "priority": 1, "weight": 1, "auth": None},
        {"name": "r2", "url": "http://b:1", "priority": 1, "weight": 1, "auth": None},
    ]
    reg = RouterRegistry(cfg)
    reg.mark_healthy("r1", ["gpt-4", "claude-3"])
    reg.mark_healthy("r1", ["gpt-4", "claude-3"])
    reg.mark_healthy("r2", ["gpt-4", "llama-3"])
    reg.mark_healthy("r2", ["gpt-4", "llama-3"])
    return reg


def test_get_models_list(populated_registry):
    mc = ModelCatalog(populated_registry)
    models = mc.get_models_list()
    assert len(models) == 3


def test_get_model_ids(populated_registry):
    mc = ModelCatalog(populated_registry)
    ids = mc.get_model_ids()
    assert "gpt-4" in ids
    assert "claude-3" in ids
    assert "llama-3" in ids


def test_count_models(populated_registry):
    mc = ModelCatalog(populated_registry)
    assert mc.count_models() == 3


def test_empty_registry():
    reg = RouterRegistry([])
    mc = ModelCatalog(reg)
    assert mc.count_models() == 0
    assert mc.get_models_list() == []


def test_catalog_caps_at_max(monkeypatch):
    monkeypatch.setattr("model_catalog.MAX_MODEL_CATALOG", 2)
    cfg = [{"name": "r1", "url": "http://a:1", "priority": 1, "weight": 1, "auth": None}]
    reg = RouterRegistry(cfg)
    reg.mark_healthy("r1", ["a", "b", "c", "d"])
    reg.mark_healthy("r1", ["a", "b", "c", "d"])
    mc = ModelCatalog(reg)
    assert mc.count_models() == 2


def test_catalog_canonicalizes_prefixed_models_and_merges_origins():
    cfg = [
        {"name": "OmniRoute", "url": "http://a:1", "priority": 1, "weight": 1, "auth": None},
        {"name": "Kiro", "url": "http://b:1", "priority": 1, "weight": 1, "auth": None},
    ]
    reg = RouterRegistry(cfg)
    reg.mark_healthy("OmniRoute", ["groq/llama-3.3-70b-versatile", "nvidia/deepseek-ai/deepseek-v4-pro"])
    reg.mark_healthy("OmniRoute", ["groq/llama-3.3-70b-versatile", "nvidia/deepseek-ai/deepseek-v4-pro"])
    reg.mark_healthy("Kiro", ["llama-3.3-70b-versatile"])
    reg.mark_healthy("Kiro", ["llama-3.3-70b-versatile"])
    mc = ModelCatalog(reg)

    catalog = mc.get_catalog()
    assert "llama-3.3-70b-versatile" in catalog
    assert "groq/llama-3.3-70b-versatile" not in catalog
    assert set(catalog["llama-3.3-70b-versatile"]["router_origins"]) == {"OmniRoute", "Kiro"}
    assert "deepseek-ai/deepseek-v4-pro" in catalog


def test_catalog_preserves_router_level_ids():
    cfg = [{"name": "r1", "url": "http://a:1", "priority": 1, "weight": 1, "auth": None}]
    reg = RouterRegistry(cfg)
    reg.mark_healthy("r1", ["kr/auto", "main-rr", "combo-round-robin"])
    reg.mark_healthy("r1", ["kr/auto", "main-rr", "combo-round-robin"])
    mc = ModelCatalog(reg)
    ids = mc.get_model_ids()
    assert "kr/auto" in ids
    assert "main-rr" in ids
    assert "combo-round-robin" in ids


def test_catalog_cap_prefers_higher_priority_router():
    cfg = [
        {"name": "low", "url": "http://low:1", "priority": 5, "weight": 1, "auth": None},
        {"name": "high", "url": "http://high:1", "priority": 1, "weight": 1, "auth": None},
    ]
    reg = RouterRegistry(cfg)
    reg.mark_healthy("low", ["aaa", "zzz"])
    reg.mark_healthy("low", ["aaa", "zzz"])
    reg.mark_healthy("high", ["high-model"])
    reg.mark_healthy("high", ["high-model"])
    mc = ModelCatalog(reg)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("model_catalog.MAX_MODEL_CATALOG", 1)
    try:
        assert mc.get_model_ids() == ["high-model"]
    finally:
        monkeypatch.undo()


def test_get_models_returns_openai_list_schema(populated_registry):
    models = ModelCatalog(populated_registry).get_models()
    assert models["object"] == "list"
    assert len(models["data"]) == 3
    assert {entry["id"] for entry in models["data"]} == {"gpt-4", "claude-3", "llama-3"}
    assert all(entry["object"] == "model" for entry in models["data"])
    assert all(isinstance(entry["created"], int) for entry in models["data"])
    assert all("router_origins" in entry for entry in models["data"])


def test_refresh_from_registry_rebuilds_snapshot(populated_registry):
    catalog = ModelCatalog(populated_registry)
    snapshot = catalog.refresh_from_registry()
    assert snapshot == catalog.get_catalog()
    assert snapshot["gpt-4"]["router_origins"] == ["r1", "r2"]


def test_get_models_sanitizes_router_origins():
    cfg = [{"name": "<script>", "url": "http://a:1", "priority": 1, "weight": 1, "auth": None}]
    reg = RouterRegistry(cfg)
    reg.mark_healthy("<script>", ["safe-model"])
    reg.mark_healthy("<script>", ["safe-model"])
    models = ModelCatalog(reg).get_models()
    assert models["data"][0]["owned_by"] == "&lt;script&gt;"
    assert "<" not in models["data"][0]["owned_by"]


def test_models_by_router_is_canonical():
    cfg = [{"name": "r1", "url": "http://a:1", "priority": 1, "weight": 1, "auth": None}]
    reg = RouterRegistry(cfg)
    reg.mark_healthy("r1", ["groq/llama-3.3-70b-versatile", "nvidia/deepseek-ai/deepseek-v4-pro"])
    reg.mark_healthy("r1", ["groq/llama-3.3-70b-versatile", "nvidia/deepseek-ai/deepseek-v4-pro"])
    assert ModelCatalog(reg).get_models_by_router("r1") == [
        "deepseek-ai/deepseek-v4-pro",
        "llama-3.3-70b-versatile",
    ]
