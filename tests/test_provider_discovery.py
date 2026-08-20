"""Tests for provider_discovery module."""

import json
from unittest.mock import patch, MagicMock

import pytest

from provider_discovery import (
    _PROVIDER_ALIAS_MAP,
    _probe_error_info,
    normalize_provider,
    fetch_provider_connections,
    ProviderDiscovery,
)


# ── normalize_provider tests ────────────────────────────────────────────


def test_normalize_provider_known_alias():
    """Test normalize_provider with known connection names."""
    assert normalize_provider("kiro") == "kr"
    assert normalize_provider("kr") == "kr"
    assert normalize_provider("kiro ").strip().lower() == "kr"


def test_normalize_provider_known_prefix():
    """Test normalize_provider with known catalog prefixes."""
    assert normalize_provider("kr") == "kr"
    assert normalize_provider("cf") == "cf"
    assert normalize_provider("cf-ai") == "cf"


def test_normalize_provider_trailing_hyphen():
    """Test normalize_provider handles trailing hyphen in some aliases."""
    result = normalize_provider("cursor")
    assert result == "cu"


def test_normalize_provider_unknown_passthrough():
    """Test unknown connection names are normalized (lowercased) and passed through."""
    assert normalize_provider("unknown-provider") == "unknown-provider"
    assert normalize_provider("UNKNOWN") == "unknown"
    assert normalize_provider("MyCustomProvider") == "mycustomprovider"


def test_normalize_provider_empty():
    """Test normalize_provider with empty string."""
    assert normalize_provider("") == ""


def test_normalize_provider_whitespace_only():
    """Test normalize_provider with whitespace only."""
    assert normalize_provider("   ") == ""


def test_normalize_provider_idempotent():
    """Test normalize_provider is idempotent."""
    name = "Kiro"
    normalized_1 = normalize_provider(name)
    normalized_2 = normalize_provider(normalized_1)
    assert normalized_1 == normalized_2


# ── fetch_provider_connections tests ─────────────────────────────────────


def test_fetch_provider_connections_no_secrets(caplog):
    """Test fetch returns empty dict when CLI token secrets are missing."""
    with patch("provider_discovery.compute_cli_token", return_value=""), \
         caplog.at_level("WARNING"):
        result = fetch_provider_connections()

    assert result == {}
    assert any("no CLI token" in record.message for record in caplog.records)


def test_fetch_provider_connections_json_parse_error(caplog):
    """Test fetch returns empty dict when API returns invalid JSON."""
    with patch("provider_discovery.compute_cli_token", return_value="test-token"):
        with patch("provider_discovery.urllib.request.urlopen") as mock_urlopen:
            mock_response = MagicMock()
            mock_response.status = 200
            mock_response.__enter__.return_value = mock_response
            mock_response.read.return_value = b"{ invalid json"
            mock_urlopen.return_value = mock_response

            with caplog.at_level("WARNING"):
                result = fetch_provider_connections()

    assert result == {}
    assert any("Failed to parse JSON response" in record.message for record in caplog.records)


# ── sync_connections tests ───────────────────────────────────────────────


def test_sync_connections_empty_response():
    """Test sync_connections returns empty dict when API returns no connections."""
    discovery = ProviderDiscovery(base_url="http://localhost:20128")
    
    with patch.object(discovery, "base_url", "http://localhost:20128"):
        with patch("provider_discovery.fetch_provider_connections") as mock_fetch:
            mock_fetch.return_value = {}
            result = discovery.sync_connections()
        
    assert result == {}
    # Check that new providers were not added (called twice - initial and in run_discovery_once)
    assert mock_fetch.call_count == 1


def test_sync_connections_new_provider_expansion(caplog):
    """Test sync_connections adds new provider prefixes to alias map."""
    discovery = ProviderDiscovery(base_url="http://localhost:20128")
    
    with patch("provider_discovery.fetch_provider_connections") as mock_fetch:
        mock_fetch.return_value = {
            "new-provider": {"status": "online", "backoffLevel": 0},
        }
        
        with caplog.at_level("INFO"):
            result = discovery.sync_connections()
        
    assert result["new-provider"]["status"] == "online"
    # New provider should be added to alias map
    assert "new-provider" in _PROVIDER_ALIAS_MAP
    assert normalize_provider("new-provider") in _PROVIDER_ALIAS_MAP


# ── probe_provider tests ────────────────────────────────────────────────


def test_probe_provider_success(caplog):
    """Test probe_provider succeeds on valid HTTP 200 response."""
    discovery = ProviderDiscovery(base_url="http://localhost:20128")
    
    with patch("provider_discovery.NINEROUTER_KEY", "test-key-123"):
        with patch("provider_discovery.urllib.request.urlopen") as mock_urlopen:
            mock_response = MagicMock()
            mock_response.__enter__.return_value = mock_response
            mock_response.read.return_value = b'{"id": "test", "choices": []}'
            mock_response.getcode.return_value = 200
            mock_urlopen.return_value = mock_response
            
            with caplog.at_level("DEBUG"):
                result = discovery.probe_provider("kr", "kr/test")
    
    assert result["ok"] is True
    assert result["status"] == 200
    assert result["error"] is None
    assert result["latency_ms"] >= 0


def test_probe_provider_http_error(caplog):
    """Test probe_provider handles HTTP errors gracefully."""
    discovery = ProviderDiscovery(base_url="http://localhost:20128")
    
    with patch("provider_discovery.NINEROUTER_KEY", "test-key-123"):
        with patch("provider_discovery.urllib.request.urlopen") as mock_urlopen:
            import urllib.error
            from email.message import Message
            mock_urlopen.side_effect = urllib.error.HTTPError(
                "http://example.com", 404, "Not Found", Message(), None
            )
            
            with caplog.at_level("DEBUG"):
                result = discovery.probe_provider("", "test")
    
    assert result["ok"] is False
    assert result["status"] == 404
    assert result["error"] == "Not Found"


def test_probe_provider_timeout_error(caplog):
    """Test probe_provider handles timeout gracefully."""
    discovery = ProviderDiscovery(base_url="http://localhost:20128")
    
    with patch("provider_discovery.NINEROUTER_KEY", "test-key-123"):
        with patch("provider_discovery.urllib.request.urlopen") as mock_urlopen:
            import urllib.error
            import socket
            mock_urlopen.side_effect = urllib.error.URLError("timed out")
            
            with caplog.at_level("DEBUG"):
                result = discovery.probe_provider("", "test")
    
    assert result["ok"] is False
    assert result["status"] == 0
    assert "timed out" in result["error"]


def test_probe_provider_no_auth_key(caplog):
    """Test probe_provider fails gracefully when NINEROUTER_KEY is not set."""
    discovery = ProviderDiscovery(base_url="http://localhost:20128")
    
    with patch("provider_discovery.NINEROUTER_KEY", ""):
        result = discovery.probe_provider("", "test")
    
    assert result["ok"] is False
    assert result["status"] == 500
    assert "NINEROUTER_KEY not set" in result["error"]


# ── run_discovery_once tests ────────────────────────────────────────────


def test_run_discovery_once_basic(caplog):
    """Test run_discovery_once calls sync_connections and probes."""
    discovery = ProviderDiscovery(base_url="http://localhost:20128")
    
    with patch("provider_discovery.fetch_provider_connections") as mock_fetch, \
         patch("provider_discovery.NINEROUTER_KEY", "test-key-123"), \
         patch("provider_discovery.urllib.request.urlopen") as mock_urlopen:
        
        # Mock sync_connections response
        mock_fetch.return_value = {
            "test-conn": {"status": "online", "backoffLevel": 0},
        }
        
        # Mock catalog and probe response
        mock_response = MagicMock()
        mock_response.__enter__.return_value = mock_response
        mock_response.read.return_value = b'{"data": [{"id": "kr/test1"}, {"id": "kr/test2"}]}'
        mock_response.getcode.return_value = 200
        mock_urlopen.return_value = mock_response
        
        with caplog.at_level("INFO"):
            result = discovery.run_discovery_once()
        
    assert result["connections"] == 1
    assert "connections" in result
    assert "new_providers" in result
    assert "status_by_provider" in result


# ── Integration tests ───────────────────────────────────────────────────


def test_full_discovery_cycle(caplog):
    """Test a complete discovery cycle from scratch."""
    discovery = ProviderDiscovery(base_url="http://localhost:20128")
    
    with patch("provider_discovery.compute_cli_token", return_value="test-token"), \
         patch("provider_discovery.fetch_provider_connections") as mock_fetch, \
         patch("provider_discovery.NINEROUTER_KEY", "test-key-123"), \
         patch("provider_discovery.urllib.request.urlopen") as mock_urlopen:
        
        # Mock catalog response (GET /v1/models)
        mock_cat_response = MagicMock()
        mock_cat_response.__enter__.return_value = mock_cat_response
        mock_cat_response.read.return_value = b'{"data": [{"id": "kr/test"}]}'
        mock_cat_response.getcode.return_value = 200

        # Mock probe response (POST /v1/chat/completions)
        mock_probe_response = MagicMock()
        mock_probe_response.__enter__.return_value = mock_probe_response
        mock_probe_response.read.return_value = b'{"id": "test", "choices": []}'
        mock_probe_response.getcode.return_value = 200

        # Chain responses: catalog first, then probe
        mock_urlopen.side_effect = [mock_cat_response, mock_probe_response]

        mock_fetch.return_value = {
            "kr": {"status": "online"},
        }
        
        with caplog.at_level("INFO"):
            result = discovery.run_discovery_once()
        
    assert result["connections"] > 0
    assert len(result["new_providers"]) >= 0


# ── discover_new_providers tests ────────────────────────────────────────


class _FakeRegistry:
    """Minimal registry stub recording health/error marks."""

    def __init__(self):
        self.marked_healthy = []
        self.marked_error = []

    def mark_healthy(self, provider, model=None):
        self.marked_healthy.append(provider)

    def mark_error(self, provider, error_info, model=None):
        self.marked_error.append((provider, error_info))


def test_discover_new_providers_no_new():
    """Test discover_new_providers returns empty when no unknown prefixes."""
    discovery = ProviderDiscovery(base_url="http://localhost:20128", registry=_FakeRegistry())

    with patch("provider_discovery.fetch_provider_connections") as mock_fetch:
        mock_fetch.return_value = {"kr": {"status": "online"}}
        result = discovery.discover_new_providers(known_prefixes={"kr"})

    assert result == {"new_providers": [], "status_by_provider": {}}
    mock_fetch.assert_called_once()


def test_discover_new_providers_probe_success():
    """Test new provider is probed and marked healthy on success."""
    registry = _FakeRegistry()
    discovery = ProviderDiscovery(base_url="http://localhost:20128", registry=registry)

    with patch("provider_discovery.fetch_provider_connections") as mock_fetch, \
         patch("provider_discovery.NINEROUTER_KEY", "test-key-123"), \
         patch("provider_discovery.urllib.request.urlopen") as mock_urlopen:

        mock_fetch.return_value = {"brand-new-prefix": {"status": "online"}}

        mock_cat_response = MagicMock()
        mock_cat_response.__enter__.return_value = mock_cat_response
        mock_cat_response.read.return_value = b'{"data": [{"id": "brand-new-prefix/model-x"}]}'
        mock_cat_response.getcode.return_value = 200

        mock_probe_response = MagicMock()
        mock_probe_response.__enter__.return_value = mock_probe_response
        mock_probe_response.read.return_value = b'{"id": "test", "choices": []}'
        mock_probe_response.getcode.return_value = 200

        mock_urlopen.side_effect = [mock_cat_response, mock_probe_response]

        result = discovery.discover_new_providers(known_prefixes={"kr"})

    assert result["new_providers"] == ["brand-new-prefix"]
    assert result["status_by_provider"]["brand-new-prefix"]["ok"] is True
    assert registry.marked_healthy == ["brand-new-prefix"]
    assert registry.marked_error == []


def test_discover_new_providers_probe_failure_marks_error():
    """Test failed probe marks error with mapped error type."""
    registry = _FakeRegistry()
    discovery = ProviderDiscovery(base_url="http://localhost:20128", registry=registry)

    with patch("provider_discovery.fetch_provider_connections") as mock_fetch, \
         patch("provider_discovery.NINEROUTER_KEY", "test-key-123"), \
         patch("provider_discovery.urllib.request.urlopen") as mock_urlopen:

        mock_fetch.return_value = {"brand-new-prefix-2": {"status": "online"}}

        mock_cat_response = MagicMock()
        mock_cat_response.__enter__.return_value = mock_cat_response
        mock_cat_response.read.return_value = b'{"data": [{"id": "brand-new-prefix-2/model-x"}]}'
        mock_cat_response.getcode.return_value = 200

        import urllib.error
        from email.message import Message
        mock_urlopen.side_effect = [
            mock_cat_response,
            urllib.error.HTTPError(
                "http://example.com", 402, "Payment Required", Message(), None
            ),
        ]

        result = discovery.discover_new_providers(known_prefixes={"kr"})

    assert result["new_providers"] == ["brand-new-prefix-2"]
    assert result["status_by_provider"]["brand-new-prefix-2"]["ok"] is False
    assert registry.marked_healthy == []
    assert registry.marked_error == [
        ("brand-new-prefix-2", {"type": "no_credit", "model_specific": False})
    ]


def test_discover_new_providers_no_catalog_models():
    """Test new provider without catalog models is not registered."""
    registry = _FakeRegistry()
    discovery = ProviderDiscovery(base_url="http://localhost:20128", registry=registry)

    with patch("provider_discovery.fetch_provider_connections") as mock_fetch, \
         patch("provider_discovery.NINEROUTER_KEY", "test-key-123"), \
         patch("provider_discovery.urllib.request.urlopen") as mock_urlopen:

        mock_fetch.return_value = {"orphan-prefix": {"status": "online"}}

        mock_cat_response = MagicMock()
        mock_cat_response.__enter__.return_value = mock_cat_response
        mock_cat_response.read.return_value = b'{"data": [{"id": "kr/model-x"}]}'
        mock_cat_response.getcode.return_value = 200
        mock_urlopen.return_value = mock_cat_response

        result = discovery.discover_new_providers(known_prefixes={"kr"})

    assert result["new_providers"] == ["orphan-prefix"]
    assert result["status_by_provider"]["orphan-prefix"]["error"] == "no catalog models"
    assert registry.marked_healthy == []
    assert registry.marked_error == []


# ── _probe_error_info tests ─────────────────────────────────────────────


def test_probe_error_info_mapping():
    """Test _probe_error_info maps status codes to error types."""
    assert _probe_error_info({"status": 401}) == {
        "type": "no_credentials", "model_specific": False}
    assert _probe_error_info({"status": 403}) == {
        "type": "no_credentials", "model_specific": False}
    assert _probe_error_info({"status": 402}) == {
        "type": "no_credit", "model_specific": False}
    assert _probe_error_info({"status": 429}) == {
        "type": "rate_limit", "model_specific": False}
    assert _probe_error_info({"status": 500}) == {
        "type": "probe_failed", "model_specific": False}
    assert _probe_error_info({"status": 0}) == {
        "type": "probe_failed", "model_specific": False}
