import pytest
from router_registry import RouterRegistry

SAMPLE_ROUTERS = [
    {"name": "router-a", "url": "http://localhost:21000", "priority": 1, "weight": 2, "health_check_path": "/v1/models", "timeout": 1.0, "auth": None},
    {"name": "router-b", "url": "http://localhost:21001", "priority": 1, "weight": 1, "health_check_path": "/v1/models", "timeout": 1.0, "auth": None},
    {"name": "router-c", "url": "http://localhost:21002", "priority": 2, "weight": 1, "health_check_path": "/v1/models", "timeout": 1.0, "auth": None},
]


@pytest.fixture
def routers_config():
    return SAMPLE_ROUTERS


@pytest.fixture
def mock_registry(routers_config):
    return RouterRegistry(routers_config)
