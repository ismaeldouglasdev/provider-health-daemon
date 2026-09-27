import pytest

from data_policy import DataSensitivity
from meta_router import MetaRouterSelector, PolicyUnavailable
from router_registry import RouterRegistry


def _registry():
    reg = RouterRegistry([
        {"name": "r1", "url": "http://a:1", "priority": 1, "weight": 1, "auth": None},
        {"name": "r2", "url": "http://b:1", "priority": 2, "weight": 1, "auth": None},
    ])
    for name in ("r1", "r2"):
        reg.mark_healthy(name, ["nvidia/glm-5.2", "openai/gpt-5.4"])
        reg.mark_healthy(name, ["nvidia/glm-5.2", "openai/gpt-5.4"])
    return reg


def test_sensitive_known_provider_is_allowed():
    selector = MetaRouterSelector(_registry())
    router = selector.select_router("openai/gpt-5.4", DataSensitivity.SENSITIVE)
    assert router.name in {"r1", "r2"}


def test_sensitive_training_possible_is_blocked():
    selector = MetaRouterSelector(_registry())
    with pytest.raises(PolicyUnavailable):
        selector.select_router("nvidia/glm-5.2", DataSensitivity.SENSITIVE)


def test_internal_training_possible_is_blocked():
    selector = MetaRouterSelector(_registry())
    with pytest.raises(PolicyUnavailable):
        selector.select_router("nvidia/glm-5.2", DataSensitivity.INTERNAL)


def test_public_training_possible_remains_available():
    selector = MetaRouterSelector(_registry())
    router = selector.select_router("nvidia/glm-5.2", DataSensitivity.PUBLIC)
    assert router.name in {"r1", "r2"}
