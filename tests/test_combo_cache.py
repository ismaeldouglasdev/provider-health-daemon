"""Tests for SmartRouter combo disk cache fallback."""

import json

import pytest

import smart_router
from smart_router import SmartRouter


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(SmartRouter, "_combo_cache", [])
    monkeypatch.setattr(SmartRouter, "_combo_cache_time", 0.0)
    monkeypatch.setattr(smart_router, "COMBO_CACHE_FILE", tmp_path / "combo_cache.json")


class TestComboCache:
    def test_catalog_success_writes_disk_cache(self, _reset_cache, tmp_path, monkeypatch):
        catalog = ["groq/llama-3.3-70b-versatile", "nvidia/z-ai/glm-5.2"]
        monkeypatch.setattr(SmartRouter, "_fetch_catalog_models", lambda: catalog)

        assert SmartRouter.get_default_combos() == catalog

        cache_file = tmp_path / "combo_cache.json"
        assert cache_file.exists()
        assert json.loads(cache_file.read_text()) == catalog

    def test_catalog_failure_uses_fresh_disk_cache(
        self, _reset_cache, tmp_path, monkeypatch
    ):
        disk = ["cf/@cf/meta/llama-3.3-70b-instruct-fp8-fast"]
        (tmp_path / "combo_cache.json").write_text(json.dumps(disk))
        monkeypatch.setattr(SmartRouter, "_fetch_catalog_models", lambda: [])

        assert SmartRouter.get_default_combos() == disk

    def test_catalog_failure_missing_disk_uses_static(
        self, _reset_cache, monkeypatch
    ):
        monkeypatch.setattr(SmartRouter, "_fetch_catalog_models", lambda: [])
        combos = SmartRouter.get_default_combos()
        assert combos == SmartRouter._filter_static_models(list(smart_router._STATIC_COMBOS))

    def test_corrupt_disk_cache_falls_back_to_static(
        self, _reset_cache, tmp_path, monkeypatch
    ):
        (tmp_path / "combo_cache.json").write_text("{not valid json")
        monkeypatch.setattr(SmartRouter, "_fetch_catalog_models", lambda: [])
        combos = SmartRouter.get_default_combos()
        assert combos  # static list, no crash

    def test_filter_removes_blocked_and_bare(self):
        ids = [
            "groq/llama-3.3-70b-versatile",
            "kr/claude-sonnet-4.5",        # PERMANENTLY_BLOCKED
            "anthropic/claude-sonnet-4",   # PERMANENTLY_BLOCKED
            "@cf/meta/llama-3.1-8b",       # starts with @
            "ollama",                      # bare id, no provider
        ]
        assert SmartRouter._filter_static_models(ids) == ["groq/llama-3.3-70b-versatile"]

    def test_filter_dedupes_preserving_order(self):
        ids = ["groq/llama-3.3-70b-versatile", "groq/llama-3.3-70b-versatile"]
        assert SmartRouter._filter_static_models(ids) == ["groq/llama-3.3-70b-versatile"]
