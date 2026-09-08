"""Tests for SmartRouter combo disk cache fallback."""

import json

import pytest

import smart_router
from smart_router import SmartRouter


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(SmartRouter, "_combo_cache", [])
    monkeypatch.setattr(SmartRouter, "_combo_cache_time", 0.0)
    monkeypatch.setattr(SmartRouter, "_disabled_cache", set())
    monkeypatch.setattr(SmartRouter, "_disabled_cache_time", 0.0)
    monkeypatch.setattr(SmartRouter, "_locked_models_cache", set())
    monkeypatch.setattr(SmartRouter, "_locked_models_cache_time", 0.0)
    monkeypatch.setattr(SmartRouter, "_conn_counts_cache", {})
    monkeypatch.setattr(SmartRouter, "_conn_counts_cache_time", 0.0)
    monkeypatch.setattr(SmartRouter, "_get_locked_model_ids", classmethod(lambda cls: set()))
    monkeypatch.setattr(SmartRouter, "_get_connection_counts", classmethod(lambda cls: {}))
    monkeypatch.setattr(smart_router, "COMBO_CACHE_FILE", tmp_path / "combo_cache.json")
    monkeypatch.setattr("catalog_sync.get_disabled_models", lambda: {})


class TestComboCache:
    def test_catalog_success_writes_disk_cache(self, _reset_cache, tmp_path, monkeypatch):
        catalog = ["groq/llama-3.3-70b-versatile", "gh/gpt-4.1"]
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

    def test_filter_keeps_blocked_as_fallback_drops_bare(self):
        """PERMANENTLY_BLOCKED providers stay in the pool as last-resort
        fallbacks (rank_models applies the penalty); only @-prefixed and bare
        ids are dropped."""
        ids = [
            "groq/llama-3.3-70b-versatile",
            "mistral/codestral-latest",    # PERMANENTLY_BLOCKED → fallback, kept
            "anthropic/claude-sonnet-4",   # PERMANENTLY_BLOCKED → fallback, kept
            "@cf/meta/llama-3.1-8b",       # starts with @ → dropped
            "ollama",                      # bare id, no provider → dropped
        ]
        assert SmartRouter._filter_static_models(ids) == [
            "groq/llama-3.3-70b-versatile",
            "mistral/codestral-latest",
            "anthropic/claude-sonnet-4",
        ]

    def test_filter_dedupes_preserving_order(self):
        ids = ["groq/llama-3.3-70b-versatile", "groq/llama-3.3-70b-versatile"]
        assert SmartRouter._filter_static_models(ids) == ["groq/llama-3.3-70b-versatile"]


class TestComboBackgroundRefresh:
    """Background combo-refresh keeps the disk cache fresh so on-demand
    requests (short CATALOG_TIMEOUT) never fall back to a stale 1-model cache."""

    def test_background_refresh_writes_full_catalog_to_disk(
        self, _reset_cache, tmp_path, monkeypatch
    ):
        full_catalog = [
            "groq/llama-3.3-70b-versatile",
            "any/qwen3.8-max",
            "cu/kimi-k3-max",
            "ollama/gpt-oss:120b",
        ]
        # Background thread (long timeout) fetches the full catalog, then
        # explicitly writes it to disk (mirrors daemon combo_refresh_loop)
        monkeypatch.setattr(SmartRouter, "_fetch_catalog_models", lambda: full_catalog)
        models = SmartRouter.get_default_combos(skip_disabled=True)
        assert models == full_catalog
        SmartRouter._write_combo_cache(models)
        cache_file = tmp_path / "combo_cache.json"
        assert json.loads(cache_file.read_text()) == full_catalog

    def test_on_demand_uses_fresh_disk_cache_not_stale(
        self, _reset_cache, tmp_path, monkeypatch
    ):
        # Background refresh already wrote a rich cache
        fresh = ["groq/llama-3.3-70b-versatile", "any/qwen3.8-max"]
        (tmp_path / "combo_cache.json").write_text(json.dumps(fresh))
        # On-demand fetch (short timeout) fails -> must use the FRESH disk cache
        monkeypatch.setattr(SmartRouter, "_fetch_catalog_models", lambda: [])
        combos = SmartRouter.get_default_combos()
        assert combos == fresh

    def test_degraded_fallback_not_persisted_over_good_cache(
        self, _reset_cache, tmp_path, monkeypatch
    ):
        # A good 300+ model cache is on disk. A timed-out/truncated fetch
        # returns a tiny fallback (1 model). The daemon guard must NOT
        # overwrite the good cache with the tiny fallback.
        good = [f"rw/model-{i}" for i in range(300)]
        (tmp_path / "combo_cache.json").write_text(json.dumps(good))
        # Simulate the daemon combo_refresh_loop: with skip_disabled=True the
        # fast-path disk cache is bypassed, so a truncated fetch yields a tiny
        # pool (the actual self-destructive trigger).
        monkeypatch.setattr(SmartRouter, "_fetch_catalog_models", lambda: ["rw/model-0"])
        fallback = SmartRouter.get_default_combos(skip_disabled=True)
        assert len(fallback) < smart_router.MIN_COMBO_WRITE
        # daemon guard: only write when pool >= MIN_COMBO_WRITE
        if len(fallback) >= smart_router.MIN_COMBO_WRITE:
            SmartRouter._write_combo_cache(fallback)
        assert json.loads((tmp_path / "combo_cache.json").read_text()) == good
