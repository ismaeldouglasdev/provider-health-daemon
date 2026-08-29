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
    assert r.health_status == "probing", "first probe after unknown sets probing"
    assert len(mock_registry.get_healthy_routers()) == 0
    mock_registry.mark_healthy("router-a", ["gpt-4"])
    assert r.health_status == "healthy", "second probe promotes to healthy"
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


def test_mark_unhealthy_hysteresis_keeps_healthy_on_isolated_failure(mock_registry):
    """1-2 falhas isoladas NÃO tiram o router do ar (timeout de /v1/models lento)."""
    mock_registry.mark_healthy("router-a", ["gpt-4"])
    mock_registry.mark_healthy("router-a", ["gpt-4"])
    assert mock_registry.get_router("router-a").health_status == "healthy"

    mock_registry.mark_unhealthy("router-a", "TimeoutError")
    r = mock_registry.get_router("router-a")
    assert r is not None
    assert r.health_status == "healthy", "1a falha: segue saudável e roteando"
    assert r.cooldown_until is None
    assert r.failure_count == 1

    mock_registry.mark_unhealthy("router-a", "TimeoutError")
    assert mock_registry.get_router("router-a").health_status == "healthy"
    assert len(mock_registry.get_healthy_routers()) == 1


def test_mark_unhealthy_trips_after_strikes(mock_registry):
    mock_registry.mark_healthy("router-a", ["gpt-4"])
    mock_registry.mark_healthy("router-a", ["gpt-4"])
    for _ in range(3):
        mock_registry.mark_unhealthy("router-a", "TimeoutError")
    r = mock_registry.get_router("router-a")
    assert r is not None
    assert r.health_status == "cooldown"
    assert r.cooldown_until is not None


def test_mark_unhealthy_success_resets_strikes(mock_registry):
    mock_registry.mark_healthy("router-a", ["gpt-4"])
    mock_registry.mark_healthy("router-a", ["gpt-4"])
    mock_registry.mark_unhealthy("router-a", "TimeoutError")
    mock_registry.mark_unhealthy("router-a", "TimeoutError")
    mock_registry.mark_healthy("router-a", ["gpt-4"])
    r = mock_registry.get_router("router-a")
    assert r.health_status == "healthy"
    assert r.failure_count == 0
    mock_registry.mark_unhealthy("router-a", "TimeoutError")
    mock_registry.mark_unhealthy("router-a", "TimeoutError")
    assert r.health_status == "healthy", "contador zerado: precisa de strikes cheios de novo"


def test_mark_unhealthy_backoff_capped(mock_registry, monkeypatch):
    import router_registry as rr
    monkeypatch.setattr(rr, "ROUTER_UNHEALTHY_STRIKES", 1)
    mock_registry2 = RouterRegistry([
        {"name": "router-a", "url": "http://localhost:21000"},
    ])
    for _ in range(12):
        mock_registry2.mark_unhealthy("router-a", "http_503")
    r = mock_registry2.get_router("router-a")
    from config import ROUTER_BACKOFF_CAP
    delta = r.cooldown_until - r.last_failure
    assert delta <= ROUTER_BACKOFF_CAP + 1, f"backoff estourou o cap: {delta}"


def test_mark_unhealthy_already_cooled_keeps_cooldown_below_strikes(mock_registry):
    """Router já em cooldown não é reabilitado por falhas abaixo do strike."""
    mock_registry.mark_unhealthy("router-a", "URLError")
    mock_registry.mark_unhealthy("router-a", "URLError")
    mock_registry.mark_unhealthy("router-a", "URLError")
    r = mock_registry.get_router("router-a")
    assert r.health_status == "cooldown" and r.cooldown_until is not None
    until = r.cooldown_until
    mock_registry.mark_unhealthy("router-a", "URLError")
    mock_registry.mark_unhealthy("router-a", "URLError")
    assert r.cooldown_until == until or r.cooldown_until >= until


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
    for _ in range(3):
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
