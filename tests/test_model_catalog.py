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
