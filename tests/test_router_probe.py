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
    """Router that doesn't respond should be marked unhealthy."""
    probe = RouterProbe(registry)
    r = registry.get_router("r1")
    result = probe.probe_router(r)
    assert result is False
    assert registry.get_router("r1").health_status == "cooldown"


def test_probe_all_timeout(registry):
    """Both routers timeout, both marked unhealthy."""
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
