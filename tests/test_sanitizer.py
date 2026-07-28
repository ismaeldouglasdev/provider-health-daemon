"""Tests for sanitizer module."""

import pytest
from sanitizer import (
    validate_model_id,
    sanitize_model_id,
    validate_router_url,
    sanitize_url,
    validate_router_name,
    sanitize_router_name,
    sanitize_router_config,
    sanitize_routers_config,
)


class TestValidateModelId:
    def test_valid_full_path(self):
        assert validate_model_id("cf/@cf/meta/llama-3.3-70b-instruct-fp8-fast")

    def test_valid_simple(self):
        assert validate_model_id("groq/llama-3.3-70b-versatile")

    def test_valid_nvidia(self):
        assert validate_model_id("nvidia/minimaxai/minimax-m3")

    def test_invalid_empty(self):
        assert not validate_model_id("")

    def test_invalid_no_provider(self):
        assert not validate_model_id("/model-only")

    def test_invalid_special_chars(self):
        assert not validate_model_id("provider/ bad spacing")

    def test_invalid_none(self):
        assert not validate_model_id(None)

    def test_invalid_too_long(self):
        assert not validate_model_id("p/" + "x" * 500)

    def test_invalid_trailing_slash(self):
        assert not validate_model_id("provider/model/")

    def test_ollama_tag(self):
        assert validate_model_id("ollama/gpt-oss:120b")


class TestSanitizeModelId:
    def test_cleans_valid(self):
        assert sanitize_model_id("  groq/llama-3.3-70b  ") == "groq/llama-3.3-70b"

    def test_returns_none_invalid(self):
        assert sanitize_model_id("") is None

    def test_returns_none_none(self):
        assert sanitize_model_id(None) is None


class TestValidateRouterURL:
    def test_valid_http(self):
        assert validate_router_url("http://router.example.com/v1")

    def test_valid_https(self):
        assert validate_router_url("https://api.example.com/v1/models")

    def test_valid_localhost(self):
        assert validate_router_url("http://localhost:20131")

    def test_valid_private_ip(self):
        assert validate_router_url("http://192.168.1.1")
        assert validate_router_url("http://10.0.0.1")
        assert validate_router_url("http://172.16.0.1")

    def test_valid_loopback(self):
        assert validate_router_url("http://127.0.0.1:8080")

    def test_invalid_scheme(self):
        assert not validate_router_url("ftp://example.com")

    def test_invalid_empty(self):
        assert not validate_router_url("")

    def test_invalid_too_long(self):
        assert not validate_router_url("http://x.com/" + "a" * 2100)

    def test_valid_with_path_and_query(self):
        assert validate_router_url("https://api.groq.com/openai/v1/models?test=true")


class TestSanitizeURL:
    def test_strips_trailing_slash(self):
        assert sanitize_url("https://example.com/") == "https://example.com"

    def test_returns_none_invalid(self):
        assert sanitize_url("") is None
        assert sanitize_url("not-a-url") is None


class TestValidateRouterName:
    def test_valid_names(self):
        assert validate_router_name("9router")
        assert validate_router_name("OmniRoute")
        assert validate_router_name("Kiro")
        assert validate_router_name("my-router-2")

    def test_invalid_short(self):
        assert not validate_router_name("a")

    def test_invalid_long(self):
        assert not validate_router_name("a" * 100)

    def test_invalid_empty(self):
        assert not validate_router_name("")

    def test_invalid_spaces(self):
        assert not validate_router_name("my router")


class TestSanitizeRouterConfig:
    def test_valid_config_passthrough(self):
        cfg = {
            "name": "TestRouter",
            "url": "https://api.test.com/v1",
            "priority": 2,
            "weight": 3,
            "timeout": 5.0,
            "health_check_path": "/health",
        }
        result = sanitize_router_config(cfg)
        assert result["name"] == "TestRouter"
        assert result["url"] == "https://api.test.com/v1"
        assert result["priority"] == 2
        assert result["weight"] == 3
        assert result["timeout"] == 5.0
        assert result["health_check_path"] == "/health"

    def test_empty_name_returns_empty(self):
        assert sanitize_router_config({"url": "https://example.com"}) == {}

    def test_localhost_url_accepted(self):
        result = sanitize_router_config({"name": "local", "url": "http://localhost:9999"})
        assert result["name"] == "local"
        assert result["url"] == "http://localhost:9999"

    def test_ftp_url_rejected(self):
        assert sanitize_router_config({"name": "bad", "url": "ftp://invalid.scheme"}) == {}

    def test_defaults_for_missing_fields(self):
        cfg = {"name": "DefaultRouter", "url": "https://api.example.com"}
        result = sanitize_router_config(cfg)
        assert result["priority"] == 1
        assert result["weight"] == 1
        assert result["timeout"] == 2.0

    def test_clamps_extreme_values(self):
        cfg = {"name": "Extreme", "url": "https://api.example.com", "priority": 999, "timeout": 999}
        result = sanitize_router_config(cfg)
        assert result["priority"] == 100
        assert result["timeout"] == 30.0

    def test_keeps_valid_auth(self):
        cfg = {"name": "Auth", "url": "https://api.example.com", "auth": {"header": "X-Key", "value": "abc"}}
        result = sanitize_router_config(cfg)
        assert result["auth"]["header"] == "X-Key"

    def test_drops_broken_auth(self):
        cfg = {"name": "NoAuth", "url": "https://api.example.com", "auth": {"header": ""}}
        result = sanitize_router_config(cfg)
        assert "auth" not in result


class TestSanitizeRoutersConfig:
    def test_filters_invalid(self):
        configs = [
            {"name": "ValidRouter", "url": "https://api.valid.com"},
            {"name": "Bad", "url": "ftp://invalid.scheme"},
        ]
        result = sanitize_routers_config(configs)
        assert len(result) == 1
        assert result[0]["name"] == "ValidRouter"

    def test_empty_input(self):
        assert sanitize_routers_config([]) == []

    def test_all_invalid(self):
        configs = [
            {"name": "a", "url": "https://example.com"},
            {"name": "Bad", "url": "ftp://invalid.scheme"},
        ]
        assert sanitize_routers_config(configs) == []
