import pytest
from router_registry import RouterRegistry


def test_init_creates_states(mock_registry):
    routers = mock_registry.get_all_routers()
    assert len(routers) == 3
    names = {r.name for r in routers}
    assert names == {"router-a", "router-b", "router-c"}


def test_get_router_exists(mock_registry):
    r = mock_registry.get_router("router-a")
    assert r is not None
    assert r.name == "router-a"
    assert r.url == "http://localhost:21000"


def test_get_router_missing(mock_registry):
    assert mock_registry.get_router("nonexistent") is None


def test_mark_healthy_flapping_guard(mock_registry):
    r = mock_registry.get_router("router-a")
    assert r.health_status == "unknown"
    mock_registry.mark_healthy("router-a", ["gpt-4"])
    assert r.health_status == "probing"
    assert r.consecutive_probes_ok == 1
    assert mock_registry.get_healthy_routers() == []
    mock_registry.mark_healthy("router-a", ["gpt-4", "claude-3"])
    assert r.health_status == "healthy"
    assert len(mock_registry.get_healthy_routers()) == 1


def test_get_healthy_routers_sorted(mock_registry):
    mock_registry.mark_healthy("router-a", ["gpt-4"])
    mock_registry.mark_healthy("router-a", ["gpt-4"])
    mock_registry.mark_healthy("router-b", ["claude-3"])
    mock_registry.mark_healthy("router-b", ["claude-3"])
    mock_registry.mark_healthy("router-c", ["llama-3"])
    mock_registry.mark_healthy("router-c", ["llama-3"])
    healthy = mock_registry.get_healthy_routers()
    assert len(healthy) == 3
    assert healthy[0].name == "router-a"
    assert healthy[1].name == "router-b"
    assert healthy[2].name == "router-c"


def test_mark_unhealthy_sets_cooldown(mock_registry):
    mock_registry.mark_unhealthy("router-a", "timeout")
    r = mock_registry.get_router("router-a")
    assert r.health_status == "cooldown"
    assert r.failure_count == 1
    assert r.cooldown_until is not None
    assert r.last_failure is not None


def test_mark_unhealthy_exponential_backoff(mock_registry):
    for i in range(3):
        mock_registry.mark_unhealthy("router-a", "timeout")
    r = mock_registry.get_router("router-a")
    assert r.failure_count == 3
    assert r.cooldown_until is not None


def test_model_catalog_dedup(mock_registry):
    mock_registry.mark_healthy("router-a", ["gpt-4", "claude-3"])
    mock_registry.mark_healthy("router-a", ["gpt-4", "claude-3"])
    mock_registry.mark_healthy("router-b", ["gpt-4", "llama-3"])
    mock_registry.mark_healthy("router-b", ["gpt-4", "llama-3"])
    catalog = mock_registry.get_model_catalog()
    assert "gpt-4" in catalog
    assert len(catalog["gpt-4"]["router_origins"]) == 2
    assert "llama-3" in catalog
    assert len(catalog["llama-3"]["router_origins"]) == 1


def test_refresh_models_replaces(mock_registry):
    mock_registry.mark_healthy("router-a", ["gpt-4", "claude-3", "llama-3"])
    mock_registry.mark_healthy("router-a", ["gpt-4", "claude-3", "llama-3"])
    mock_registry.refresh_models_from_router("router-a", ["gpt-4"])
    catalog = mock_registry.get_model_catalog()
    assert "gpt-4" in catalog
    assert "claude-3" not in catalog
    assert "llama-3" not in catalog


def test_save_load_state(mock_registry, tmp_path, monkeypatch):
    monkeypatch.setattr("router_registry.ROUTER_STATE_FILE", tmp_path / "state.json")
    mock_registry.mark_healthy("router-a", ["gpt-4"])
    mock_registry.mark_healthy("router-a", ["gpt-4"])
    mock_registry.mark_unhealthy("router-b", "timeout")
    mock_registry.save_state()
    assert (tmp_path / "state.json").exists()
    new_registry = RouterRegistry.__new__(RouterRegistry)
    new_registry._routers = {}
    from router_registry import RouterState
    for cfg in [
        {"name": "router-a", "url": "http://a:1", "priority": 1, "weight": 1},
        {"name": "router-b", "url": "http://b:1", "priority": 1, "weight": 1},
    ]:
        s = RouterState(cfg["name"], cfg["url"], cfg["priority"], cfg["weight"])
        new_registry._routers[s.name] = s
    new_registry.load_state()
    ra = new_registry.get_router("router-a")
    assert ra.health_status == "healthy"
    rb = new_registry.get_router("router-b")
    assert rb.health_status == "cooldown"
    assert rb.failure_count >= 1
