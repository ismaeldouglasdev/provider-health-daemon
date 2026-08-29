"""Tests for Account Pools — per-account discovery, registry sync, dashboard API.

Covers the additive "pools de contas" feature:
  1. fetch_provider_connections() enriches each prefix with per-account details.
  2. HealthRegistry persists an `accounts` section (sync/get/summary, denied purge).
  3. Dashboard GET /api/pools exposes the pools view (degrades to empty shape).

All fixtures use tmp_path — never touches the real ~/.9router/health.json.
"""

import io
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dashboard import DashboardHandler
from health_registry import HealthRegistry
from provider_discovery import fetch_provider_connections


# ── fetch_provider_connections: per-account enrichment ──────────────────


def _mock_connections_response(connections: list) -> MagicMock:
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.__enter__.return_value = mock_response
    mock_response.read.return_value = json.dumps({"connections": connections}).encode()
    return mock_response


def test_fetch_connections_two_accounts_same_provider():
    """Two accounts of the same provider → accounts list with 2 entries + connection_count=2."""
    conns = [
        {
            "id": "acc-1",
            "provider": "kiro",
            "name": "account-one@example.com",
            "testStatus": "active",
            "backoffLevel": 0,
            "errorCode": None,
            "lastUsedAt": "2026-08-29T10:00:00Z",
            "isActive": True,
        },
        {
            "id": "acc-2",
            "provider": "kiro",
            "name": "account-two@example.com",
            "testStatus": "error",
            "backoffLevel": 2,
            "errorCode": 429,
            "modelLock_gpt-4o": "2026-08-30T00:00:00Z",
            "lastUsedAt": "2026-08-29T09:00:00Z",
            "isActive": False,
        },
    ]
    with patch("provider_discovery.compute_cli_token", return_value="test-token"), \
         patch("provider_discovery.urllib.request.urlopen",
               return_value=_mock_connections_response(conns)):
        result = fetch_provider_connections()

    assert "kr" in result  # kiro → kr
    entry = result["kr"]
    assert entry["connection_count"] == 2
    assert len(entry["accounts"]) == 2
    # Retrocompat: `connections` (names) still present
    assert entry["connections"] == ["account-one@example.com", "account-two@example.com"]

    acc1 = entry["accounts"][0]
    assert acc1["id"] == "acc-1"
    assert acc1["name"] == "account-one@example.com"
    assert acc1["status"] == "active"
    assert acc1["backoffLevel"] == 0
    assert acc1["errorCode"] is None
    assert acc1["model_locks"] == {}
    assert acc1["lastUsedAt"] == "2026-08-29T10:00:00Z"
    assert acc1["isActive"] is True

    acc2 = entry["accounts"][1]
    assert acc2["id"] == "acc-2"
    assert acc2["status"] == "error"
    assert acc2["backoffLevel"] == 2
    assert acc2["errorCode"] == 429
    assert acc2["model_locks"] == {"gpt-4o": "2026-08-30T00:00:00Z"}
    assert acc2["isActive"] is False


def test_fetch_connections_account_without_name_email():
    """Account missing name/email must not crash; name falls back to the id."""
    conns = [
        {
            "id": "acc-x",
            "provider": "nvidia",
            "testStatus": "active",
            "backoffLevel": 0,
        },
    ]
    with patch("provider_discovery.compute_cli_token", return_value="test-token"), \
         patch("provider_discovery.urllib.request.urlopen",
               return_value=_mock_connections_response(conns)):
        result = fetch_provider_connections()

    entry = result["nvidia"]
    assert len(entry["accounts"]) == 1
    assert entry["accounts"][0]["name"] == "acc-x"  # id fallback, no crash
    assert entry["accounts"][0]["status"] == "active"
    assert entry["accounts"][0]["backoffLevel"] == 0
    assert entry["accounts"][0]["model_locks"] == {}
    assert entry["accounts"][0]["errorCode"] is None
    # connections list stays empty (no name/email) — no crash
    assert entry["connections"] == []


def test_fetch_connections_denied_provider_not_in_accounts():
    """Denied providers (mock/test/fixture) never appear in accounts."""
    conns = [
        {"id": "m1", "provider": "mock-provider", "name": "mock@x.com", "testStatus": "active"},
        {"id": "r1", "provider": "real-provider", "name": "real@x.com", "testStatus": "active"},
    ]
    with patch("provider_discovery.compute_cli_token", return_value="test-token"), \
         patch("provider_discovery.urllib.request.urlopen",
               return_value=_mock_connections_response(conns)):
        result = fetch_provider_connections()

    assert "mock-provider" not in result
    assert "real-provider" in result
    assert result["real-provider"]["accounts"][0]["id"] == "r1"


# ── HealthRegistry: accounts section ────────────────────────────────────


@pytest.fixture
def tmp_registry(tmp_path: Path) -> HealthRegistry:
    """Create a fresh registry on a temp file."""
    return HealthRegistry(filepath=tmp_path / "health.json")


def test_sync_accounts_and_get(tmp_registry: HealthRegistry):
    tmp_registry.sync_accounts({
        "kr": [
            {"id": "acc-1", "name": "a@x.com", "status": "active", "backoffLevel": 0,
             "errorCode": None, "model_locks": {}, "lastUsedAt": None, "isActive": True},
            {"id": "acc-2", "name": "b@x.com", "status": "error", "backoffLevel": 3,
             "errorCode": 429, "model_locks": {"gpt-4o": "2099-01-01T00:00:00Z"},
             "lastUsedAt": None, "isActive": False},
        ],
    })
    accounts = tmp_registry.get_accounts("kr")
    assert len(accounts) == 2
    by_id = {a["id"]: a for a in accounts}
    assert by_id["acc-1"]["status"] == "active"
    assert by_id["acc-2"]["model_locks"] == {"gpt-4o": "2099-01-01T00:00:00Z"}

    summary = tmp_registry.account_summary("kr")
    assert summary["count"] == 2
    assert summary["active"] == 1
    assert summary["locked"] == 1
    assert summary["backoff"] == 1


def test_sync_accounts_preserves_extra_fields(tmp_registry: HealthRegistry):
    """Fields the discovery does not send (e.g. a manual flag) survive a re-sync."""
    tmp_registry.sync_accounts({
        "kr": [{"id": "acc-1", "name": "a@x.com", "status": "active"}],
    })
    # Simulate a manual flag written by an admin tool
    with tmp_registry._lock:
        tmp_registry._data[tmp_registry.ACCOUNTS]["kr"][0]["manual_flag"] = True
    # Re-sync with same account, no manual_flag
    tmp_registry.sync_accounts({
        "kr": [{"id": "acc-1", "name": "a@x.com", "status": "active", "backoffLevel": 1}],
    })
    acc = tmp_registry.get_accounts("kr")[0]
    assert acc["manual_flag"] is True
    assert acc["backoffLevel"] == 1  # incoming wins for overlapping keys


def test_sync_accounts_idempotent(tmp_registry: HealthRegistry):
    data = {"kr": [{"id": "acc-1", "name": "a@x.com", "status": "active"}]}
    tmp_registry.sync_accounts(data)
    first = tmp_registry.snapshot()["accounts"]
    tmp_registry.sync_accounts(data)
    second = tmp_registry.snapshot()["accounts"]
    assert first == second


def test_sync_accounts_denied_provider_skipped(tmp_registry: HealthRegistry):
    tmp_registry.sync_accounts({
        "mock-provider": [{"id": "m1", "name": "mock@x.com"}],
        "kr": [{"id": "acc-1", "name": "a@x.com"}],
    })
    accounts = tmp_registry.snapshot()["accounts"]
    assert "mock-provider" not in accounts
    assert "kr" in accounts


def test_accounts_persistence(tmp_path: Path):
    """Reloading the registry from disk reads the accounts section back."""
    fp = tmp_path / "health.json"
    r1 = HealthRegistry(filepath=fp)
    r1.sync_accounts({"kr": [{"id": "acc-1", "name": "a@x.com", "status": "active"}]})

    r2 = HealthRegistry(filepath=fp)
    accounts = r2.get_accounts("kr")
    assert len(accounts) == 1
    assert accounts[0]["id"] == "acc-1"
    assert accounts[0]["status"] == "active"


def test_load_purges_denied_accounts(tmp_path: Path):
    """Old health files with denied providers in accounts are purged on load."""
    fp = tmp_path / "health.json"
    fp.write_text(json.dumps({
        "providers": {},
        "models": {},
        "accounts": {
            "mock-provider": [{"id": "m1", "name": "mock@x.com"}],
            "kr": [{"id": "acc-1", "name": "a@x.com"}],
        },
    }))
    r = HealthRegistry(filepath=fp)
    accounts = r.snapshot()["accounts"]
    assert "mock-provider" not in accounts
    assert "kr" in accounts


def test_empty_state_has_accounts(tmp_path: Path):
    r = HealthRegistry(filepath=tmp_path / "health.json")
    assert r.ACCOUNTS in r.snapshot()
    assert r.snapshot()["accounts"] == {}


# ── Dashboard /api/pools ────────────────────────────────────────────────


class _FakeSocket:
    """Minimal socket stand-in for BaseHTTPRequestHandler."""

    def __init__(self):
        self._out = io.BytesIO()

    def makefile(self, mode, *args, **kwargs):
        if mode == "wb":
            return self._out
        return io.BytesIO(b"")

    def sendall(self, data):
        self._out.write(data)

    def close(self):
        pass


class _FakeServer:
    pass


def _pools_response(registry) -> dict:
    class Handler(DashboardHandler):
        pass

    Handler.health_registry = registry
    Handler.metrics_store = None
    Handler.router_registry = None
    Handler.metrics_persistence = None

    sock = _FakeSocket()
    handler = Handler(sock, ("127.0.0.1", 0), _FakeServer())
    handler.path = "/api/pools"
    handler.requestline = "GET /api/pools HTTP/1.1"
    handler.request_version = "HTTP/1.1"
    handler.do_GET()
    raw = sock._out.getvalue().decode()
    body = raw.split("\r\n\r\n", 1)[1]
    return json.loads(body)


def test_api_pools_shape(tmp_path: Path):
    registry = HealthRegistry(filepath=tmp_path / "health.json")
    registry.sync_accounts({
        "kr": [
            {"id": "acc-1", "name": "a@x.com", "status": "active", "backoffLevel": 0,
             "errorCode": None, "model_locks": {}, "lastUsedAt": None, "isActive": True},
            {"id": "acc-2", "name": "b@x.com", "status": "error", "backoffLevel": 2,
             "errorCode": 429, "model_locks": {"gpt-4o": "2099-01-01T00:00:00Z"},
             "lastUsedAt": None, "isActive": False},
        ],
        "nvidia": [
            {"id": "acc-3", "name": "c@x.com", "status": "active", "backoffLevel": 0,
             "errorCode": None, "model_locks": {}, "lastUsedAt": None, "isActive": True},
        ],
    })
    data = _pools_response(registry)

    assert data["total_accounts"] == 3
    assert data["total_providers"] == 2
    assert set(data["providers"].keys()) == {"kr", "nvidia"}

    kr = data["providers"]["kr"]
    assert kr["connection_count"] == 2
    assert kr["active_count"] == 1
    assert kr["locked_count"] == 1
    assert len(kr["accounts"]) == 2
    assert kr["accounts"][0]["id"] == "acc-1"
    assert kr["status"] == "unavailable"  # worst-case pool status (acc-2 in error)

    nv = data["providers"]["nvidia"]
    assert nv["connection_count"] == 1
    assert nv["active_count"] == 1
    assert nv["locked_count"] == 0
    assert nv["status"] == "healthy"


def test_api_pools_empty_registry(tmp_path: Path):
    """Registry without accounts → empty shape, no crash."""
    registry = HealthRegistry(filepath=tmp_path / "health.json")
    data = _pools_response(registry)
    assert data == {"providers": {}, "total_accounts": 0, "total_providers": 0}


def test_api_pools_no_registry():
    """No registry attached → empty shape, no crash."""
    data = _pools_response(None)
    assert data == {"providers": {}, "total_accounts": 0, "total_providers": 0}