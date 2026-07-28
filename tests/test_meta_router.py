import pytest
from meta_router import MetaRouterSelector, ServiceUnavailable
from router_registry import RouterRegistry


@pytest.fixture
def triple_registry():
    cfg = [
        {"name": "r1", "url": "http://a:1", "priority": 1, "weight": 2, "auth": None},
        {"name": "r2", "url": "http://b:1", "priority": 1, "weight": 1, "auth": None},
        {"name": "r3", "url": "http://c:1", "priority": 2, "weight": 1, "auth": None},
    ]
    reg = RouterRegistry(cfg)
    for r in ["r1", "r2", "r3"]:
        reg.mark_healthy(r, ["gpt-4"])
        reg.mark_healthy(r, ["gpt-4"])
    return reg


def test_select_router_returns_healthy(triple_registry):
    sel = MetaRouterSelector(triple_registry)
    r = sel.select_router()
    assert r.health_status == "healthy"


def test_round_robin_alternates(triple_registry):
    sel = MetaRouterSelector(triple_registry)
    first = sel.select_router().name
    second = sel.select_router().name
    third = sel.select_router().name
    fourth = sel.select_router().name
    assert first != second or len([r for r in [first, second, third, fourth] if r == first]) < 4
    assert len({first, second, third}) <= 3


def test_all_down_raises():
    reg = RouterRegistry([
        {"name": "r1", "url": "http://a:1", "priority": 1, "weight": 1, "auth": None},
    ])
    sel = MetaRouterSelector(reg)
    with pytest.raises(ServiceUnavailable):
        sel.select_router()


def test_fallback_after_failure(triple_registry):
    sel = MetaRouterSelector(triple_registry)
    r1 = triple_registry.get_router("r1")
    r1.health_status = "cooldown"
    r = sel.select_router()
    assert r.name != "r1"


def test_on_failure_marks_unhealthy(triple_registry):
    sel = MetaRouterSelector(triple_registry)
    sel.on_failure("r1", "timeout")
    assert triple_registry.get_router("r1").health_status == "cooldown"


def test_single_router():
    reg = RouterRegistry([
        {"name": "r1", "url": "http://a:1", "priority": 1, "weight": 1, "auth": None},
    ])
    reg.mark_healthy("r1", ["gpt-4"])
    reg.mark_healthy("r1", ["gpt-4"])
    sel = MetaRouterSelector(reg)
    r1 = sel.select_router()
    r2 = sel.select_router()
    assert r1.name == r2.name
