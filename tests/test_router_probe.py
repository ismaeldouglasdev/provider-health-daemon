import pytest
import json
import threading
import time
from router_probe import RouterProbe
from router_registry import RouterRegistry


@pytest.fixture
def registry():
    cfg = [
        {"name": "r1", "url": "http://localhost:21999", "priority": 1, "weight": 1, "timeout": 0.5, "auth": None},
        {"name": "r2", "url": "http://localhost:21998", "priority": 1, "weight": 1, "timeout": 0.5, "auth": None},
    ]
    return RouterRegistry(cfg)


def test_probe_router_timeout(registry):
    """Router que não responde entra em cooldown após as strikes.

    Histerese (2026-08-24): falhas isoladas mantêm o router roteando;
    o cooldown só vem na ROUTER_UNHEALTHY_STRIKES-ésima falha consecutiva.
    """
    probe = RouterProbe(registry)
    r = registry.get_router("r1")
    assert r is not None
    for _ in range(3):
        assert probe.probe_router(r) is False
    assert registry.get_router("r1").health_status == "cooldown"


def test_probe_all_timeout(registry):
    """Both routers timeout → ambos em cooldown após as strikes."""
    probe = RouterProbe(registry)
    from config import ROUTER_UNHEALTHY_STRIKES
    for _ in range(ROUTER_UNHEALTHY_STRIKES):
        probe.probe_all()
    probe = RouterProbe(registry)
    results = probe.probe_all()
    assert results["healthy"] == 0
    assert results["unhealthy"] == 2


def test_probe_loop_stop():
    """Probe loop should stop gracefully."""
    reg = RouterRegistry([])
    probe = RouterProbe(reg)
    assert probe._running is False
    t = threading.Thread(target=probe.probe_loop, daemon=True)
    t.start()
    time.sleep(0.1)
    assert probe._running is True
    probe.stop()
    assert probe._running is False
