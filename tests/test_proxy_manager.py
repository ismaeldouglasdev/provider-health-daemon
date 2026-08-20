"""Tests for proxy_manager module."""

import json
import sqlite3
import time
import urllib.error
from unittest.mock import patch, MagicMock

import pytest

import proxy_manager as pm


# ── fetch_raw_proxies tests ──────────────────────────────────────────────


def _mock_response(body: bytes, status: int = 200):
    resp = MagicMock()
    resp.status = status
    resp.__enter__.return_value = resp
    resp.read.return_value = body
    return resp


def test_fetch_raw_proxies_normalizes_bare_hostport():
    """Bare ip:port lines are normalized to http://ip:port."""
    with patch("proxy_manager.PROXY_SOURCES", ["https://src"]), \
         patch("proxy_manager.urllib.request.urlopen", return_value=_mock_response(b"1.2.3.4:8080\n5.6.7.8:3128\n")):
        proxies = pm.fetch_raw_proxies()

    assert proxies == ["http://1.2.3.4:8080", "http://5.6.7.8:3128"]


def test_fetch_raw_proxies_keeps_scheme_and_dedups():
    """Lines already carrying a scheme are kept; duplicates collapse."""
    body = b"http://1.2.3.4:8080\nhttp://1.2.3.4:8080\nsocks5://9.9.9.9:1080\n"
    with patch("proxy_manager.PROXY_SOURCES", ["https://src"]), \
         patch("proxy_manager.urllib.request.urlopen", return_value=_mock_response(body)):
        proxies = pm.fetch_raw_proxies()

    assert proxies == ["http://1.2.3.4:8080", "socks5://9.9.9.9:1080"]


def test_fetch_raw_proxies_source_failure_skipped():
    """A failing source is logged and skipped, not fatal."""
    with patch("proxy_manager.PROXY_SOURCES", ["https://dead"]), \
         patch("proxy_manager.urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
        proxies = pm.fetch_raw_proxies()

    assert proxies == []


def test_fetch_raw_proxies_non_200_skipped():
    """Non-200 responses from a source are skipped."""
    with patch("proxy_manager.PROXY_SOURCES", ["https://src"]), \
         patch("proxy_manager.urllib.request.urlopen", return_value=_mock_response(b"", status=503)):
        proxies = pm.fetch_raw_proxies()

    assert proxies == []


# ── probe_proxies tests ──────────────────────────────────────────────────


def test_probe_proxies_filters_dead():
    """Only proxies whose probe succeeds are returned."""
    def _probe(proxy_url):
        return proxy_url != "http://b:2"

    with patch("proxy_manager._probe_one", side_effect=_probe) as mock_probe:
        alive = pm.probe_proxies(["http://a:1", "http://b:2", "http://c:3"], max_workers=3)

    assert mock_probe.call_count == 3
    assert alive == ["http://a:1", "http://c:3"]


def test_probe_proxies_empty_input():
    """Empty input returns empty list without touching the executor."""
    with patch("proxy_manager._probe_one") as mock_probe:
        assert pm.probe_proxies([]) == []
    mock_probe.assert_not_called()


def test_probe_one_success():
    """A 200 response means the proxy supports HTTPS CONNECT."""
    with patch("proxy_manager.urllib.request.build_opener") as mock_build:
        opener = MagicMock()
        resp = MagicMock()
        resp.status = 200
        resp.__enter__.return_value = resp
        opener.open.return_value = resp
        mock_build.return_value = opener

        assert pm._probe_one("http://x:1") is True


def test_probe_one_network_error():
    """A raised error means the proxy is dead."""
    with patch("proxy_manager.urllib.request.build_opener") as mock_build:
        opener = MagicMock()
        opener.open.side_effect = urllib.error.URLError("timeout")
        mock_build.return_value = opener

        assert pm._probe_one("http://x:1") is False


# ── fetch_connections tests ──────────────────────────────────────────────


def test_fetch_connections_no_token(caplog):
    """Missing CLI token skips the fetch with a warning."""
    with patch("proxy_manager.compute_cli_token", return_value=None), \
         caplog.at_level("WARNING"):
        result = pm.fetch_connections()

    assert result == []
    assert any("no CLI token" in r.message for r in caplog.records)


def test_fetch_connections_returns_connections():
    """Valid API response returns connections that have an id."""
    payload = json.dumps({"connections": [
        {"id": "c1", "provider": "nvidia", "providerSpecificData": {}},
        {"id": "c2", "provider": "groq", "providerSpecificData": {"connectionProxyUrl": "http://p:1"}},
        {"provider": "orphan", "providerSpecificData": {}},  # no id -> dropped
    ]}).encode()
    with patch("proxy_manager.compute_cli_token", return_value="tok"), \
         patch("proxy_manager.urllib.request.urlopen", return_value=_mock_response(payload)):
        result = pm.fetch_connections()

    assert [c["id"] for c in result] == ["c1", "c2"]


def test_fetch_connections_parse_error():
    """Invalid JSON returns empty list."""
    with patch("proxy_manager.compute_cli_token", return_value="tok"), \
         patch("proxy_manager.urllib.request.urlopen", return_value=_mock_response(b"{ nope")):
        assert pm.fetch_connections() == []


def test_fetch_connections_network_error():
    """URLError returns empty list, never raises."""
    with patch("proxy_manager.compute_cli_token", return_value="tok"), \
         patch("proxy_manager.urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
        assert pm.fetch_connections() == []


# ── compute_assignments tests ────────────────────────────────────────────


def _conn(cid, provider, psd=None):
    return {"id": cid, "provider": provider, "providerSpecificData": psd or {}}


def test_compute_assignments_distinct_within_provider():
    """Accounts of the same provider get distinct proxies when pool allows."""
    conns = [
        _conn("a1", "nvidia"), _conn("a2", "nvidia"),
        _conn("b1", "groq"), _conn("b2", "groq"),
    ]
    pool = ["http://p1:1", "http://p2:2", "http://p3:3", "http://p4:4"]

    assignments = pm.compute_assignments(conns, pool)

    assert assignments["a1"] != assignments["a2"]
    assert assignments["b1"] != assignments["b2"]
    assert assignments["a1"] == "http://p1:1"
    assert assignments["b1"] == "http://p3:3"


def test_compute_assignments_empty_pool():
    """Empty pool yields no assignments."""
    assert pm.compute_assignments([_conn("a1", "nvidia")], []) == {}


def test_compute_assignments_empty_connections():
    """No connections yields no assignments."""
    assert pm.compute_assignments([], ["http://p1:1"]) == {}


# ── assignments_for_unproxied tests ──────────────────────────────────────


def test_assignments_for_unproxied_only_targets_unproxied():
    """Already-proxied connections are untouched; new ones avoid sibling proxies."""
    conns = [
        _conn("a1", "nvidia", {"connectionProxyUrl": "http://p1:1"}),
        _conn("a2", "nvidia"),  # new account, same provider as a1
    ]
    pool = ["http://p1:1", "http://p2:2", "http://p3:3"]

    assignments = pm.assignments_for_unproxied(conns, pool)

    assert assignments == {"a2": "http://p2:2"}


def test_assignments_for_unproxied_falls_back_when_pool_short():
    """Pool shorter than sibling count falls back to a reused proxy."""
    conns = [
        _conn("a1", "nvidia", {"connectionProxyUrl": "http://p1:1"}),
        _conn("a2", "nvidia"),
        _conn("a3", "nvidia"),
    ]
    pool = ["http://p1:1", "http://p2:2"]

    assignments = pm.assignments_for_unproxied(conns, pool)

    assert set(assignments) == {"a2", "a3"}
    assert assignments["a2"] == "http://p2:2"


# ── apply_assignments tests ──────────────────────────────────────────────


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "data.sqlite"
    db = sqlite3.connect(str(db_path))
    db.execute("CREATE TABLE providerConnections (id TEXT, provider TEXT, name TEXT, data TEXT, updatedAt TEXT)")
    db.execute(
        "INSERT INTO providerConnections VALUES (?, ?, ?, ?, ?)",
        ("c1", "nvidia", "acc1", json.dumps({"providerSpecificData": {"connectionNoProxy": "api.groq.com"}}), "old"),
    )
    db.execute(
        "INSERT INTO providerConnections VALUES (?, ?, ?, ?, ?)",
        ("c2", "groq", "acc2", json.dumps({"providerSpecificData": {"connectionProxyUrl": "http://p1:1", "connectionProxyEnabled": True}}), "old"),
    )
    db.commit()
    db.close()

    monkeypatch.setattr(pm, "DB_PATH", db_path)
    monkeypatch.setattr(pm, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(pm, "_backup_db", lambda: None)
    return db_path


def test_apply_assignments_writes_legacy_fields(tmp_db):
    """Assignments are persisted into providerSpecificData with enabled=True."""
    changed = pm.apply_assignments({"c1": "http://p9:9"})

    assert changed == 1
    db = sqlite3.connect(str(tmp_db))
    row = db.execute("SELECT data, updatedAt FROM providerConnections WHERE id='c1'").fetchone()
    db.close()

    d = json.loads(row[0])
    psd = d["providerSpecificData"]
    assert psd["connectionProxyEnabled"] is True
    assert psd["connectionProxyUrl"] == "http://p9:9"
    assert psd["connectionNoProxy"] == "api.groq.com"  # preserved
    assert row[1] != "old"


def test_apply_assignments_skips_unchanged(tmp_db):
    """A connection already holding the same proxy is not rewritten."""
    changed = pm.apply_assignments({"c2": "http://p1:1"})

    assert changed == 0
    db = sqlite3.connect(str(tmp_db))
    row = db.execute("SELECT data FROM providerConnections WHERE id='c2'").fetchone()
    db.close()
    assert row[0] is not None


def test_apply_assignments_empty(tmp_db):
    """No assignments means no DB writes."""
    assert pm.apply_assignments({}) == 0


def test_apply_assignments_unknown_id(tmp_db):
    """Unknown ids are skipped without error."""
    assert pm.apply_assignments({"nope": "http://p1:1"}) == 0


# ── ProxyManager tests ───────────────────────────────────────────────────


def test_pool_needs_refresh_empty_pool():
    manager = pm.ProxyManager()
    assert manager.pool_needs_refresh() is True


def test_pool_needs_refresh_fresh_pool():
    manager = pm.ProxyManager()
    manager._pool = ["http://p1:1"]
    manager._pool_fetched_at = time.monotonic()
    assert manager.pool_needs_refresh() is False


def test_pool_needs_refresh_stale_pool():
    manager = pm.ProxyManager()
    manager._pool = ["http://p1:1"]
    manager._pool_fetched_at = time.monotonic() - pm.PROXY_REFRESH_INTERVAL_SECONDS - 1
    assert manager.pool_needs_refresh() is True


def test_ensure_all_proxied_no_connections():
    """No connections returned -> no-op summary."""
    manager = pm.ProxyManager()
    with patch("proxy_manager.fetch_connections", return_value=[]):
        result = manager.ensure_all_proxied()

    assert result["connections"] == 0
    assert result["applied"] == 0


def test_ensure_all_proxied_new_account_gets_proxy():
    """A new unproxied account is assigned a proxy without full reassignment."""
    manager = pm.ProxyManager()
    manager._pool = ["http://p1:1", "http://p2:2"]
    manager._pool_fetched_at = time.monotonic()
    conns = [
        _conn("a1", "nvidia", {"connectionProxyUrl": "http://p1:1"}),
        _conn("a2", "nvidia"),  # new account
    ]

    with patch("proxy_manager.fetch_connections", return_value=conns), \
         patch("proxy_manager.apply_assignments", return_value=1) as mock_apply:
        result = manager.ensure_all_proxied()

    assert result["unproxied"] == 1
    assert result["refreshed"] is False
    mock_apply.assert_called_once()
    assignments = mock_apply.call_args.args[0]
    assert assignments["a2"] == "http://p2:2"


def test_ensure_all_proxied_refresh_reassigns_all():
    """Stale pool triggers refresh and full reassignment."""
    manager = pm.ProxyManager()
    conns = [_conn("a1", "nvidia"), _conn("b1", "groq")]

    def _fake_load():
        manager._pool = ["http://p1:1", "http://p2:2"]
        manager._pool_fetched_at = time.monotonic()
        return manager._pool

    with patch("proxy_manager.fetch_connections", return_value=conns), \
         patch.object(manager, "_load_pool", side_effect=_fake_load) as mock_load, \
         patch("proxy_manager.compute_assignments", return_value={"a1": "http://p1:1", "b1": "http://p2:2"}) as mock_compute, \
         patch("proxy_manager.apply_assignments", return_value=2) as mock_apply:
        result = manager.ensure_all_proxied()

    mock_load.assert_called_once()
    mock_compute.assert_called_once()
    assert result["refreshed"] is True
    assert result["applied"] == 2


def test_ensure_all_proxied_dry_run_skips_write():
    """Dry-run computes assignments but never writes to the DB."""
    manager = pm.ProxyManager()
    manager._pool = ["http://p1:1"]
    manager._pool_fetched_at = time.monotonic()
    conns = [_conn("a1", "nvidia")]

    with patch("proxy_manager.fetch_connections", return_value=conns), \
         patch("proxy_manager.apply_assignments") as mock_apply:
        result = manager.ensure_all_proxied(dry_run=True)

    mock_apply.assert_not_called()
    assert result["dry_run"] is True
    assert result["applied"] == 1


def test_ensure_all_proxied_pool_too_small_keeps_old():
    """A pool below the minimum is refused and the old pool survives."""
    manager = pm.ProxyManager()
    manager._pool = ["http://old:1"]
    manager._pool_fetched_at = time.monotonic() - pm.PROXY_REFRESH_INTERVAL_SECONDS - 1
    conns = [_conn("a1", "nvidia")]

    with patch("proxy_manager.fetch_connections", return_value=conns), \
         patch("proxy_manager.fetch_raw_proxies", return_value=["http://only:1"]), \
         patch("proxy_manager.probe_proxies", return_value=["http://only:1"]), \
         patch("proxy_manager.PROXY_MIN_POOL", 5), \
         patch("proxy_manager.apply_assignments", return_value=0):
        result = manager.ensure_all_proxied()

    assert result["pool"] == 1  # old pool kept
    assert manager._pool == ["http://old:1"]
