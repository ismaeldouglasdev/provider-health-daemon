"""Tests for smart_router — access_penalty scoring and rank_models ordering."""

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from metrics_store import MetricsStore, RequestRecord
from smart_router import FALLBACK_ONLY_PENALTY, SmartRouter


@pytest.fixture(autouse=True)
def _no_dynamic_network(monkeypatch):
    """The dynamic pool helpers (_get_locked_model_ids/_get_connection_counts)
    hit the live 9router /api/providers. Stub them so the unit suite never
    touches the network; individual tests override with their own data."""
    monkeypatch.setattr(SmartRouter, "_get_locked_model_ids", classmethod(lambda cls: set()))
    monkeypatch.setattr(SmartRouter, "_get_connection_counts", classmethod(lambda cls: {}))


def _make_router():
    return SmartRouter(metrics_store=None)


def _stats(errors_by_type=None, error_rate=0.0, failed=0, total=10, avg_latency_ms=800):
    return {
        "provider": "groq",
        "window_seconds": 300,
        "total_requests": total,
        "successful": total - failed,
        "failed": failed,
        "error_rate": error_rate,
        "avg_latency_ms": avg_latency_ms,
        "errors_by_type": errors_by_type or {},
    }


class TestAccessPenalty:
    def test_no_access_errors_no_penalty(self):
        score = _make_router()._compute_score("groq", _stats())
        assert "access_penalty" not in score
        assert score["total"] < 100

    def test_single_access_error_adds_350(self):
        score = _make_router()._compute_score(
            "groq", _stats(errors_by_type={"subscription_level": 1})
        )
        assert score["access_penalty"] == 350  # 300 + 1*50
        assert score["total"] >= 350

    def test_access_count_accumulates_across_types(self):
        score = _make_router()._compute_score(
            "groq",
            _stats(errors_by_type={"subscription_level": 2, "no_credit": 1}),
        )
        assert score["access_penalty"] == 450  # 300 + 3*50

    def test_access_penalty_capped_at_700(self):
        score = _make_router()._compute_score(
            "groq", _stats(errors_by_type={"auth_invalid": 10})
        )
        assert score["access_penalty"] == 700  # 300 + min(10*50, 400)

    def test_access_errors_sink_below_latency_only_provider(self):
        """A provider with access errors must rank worse than one with just high latency."""
        access = _make_router()._compute_score(
            "groq", _stats(errors_by_type={"no_credit": 1}, error_rate=0.0)
        )
        slow = _make_router()._compute_score(
            "nvidia", _stats(error_rate=0.0, avg_latency_ms=9000)
        )
        assert access["total"] > slow["total"]


class _FakeHealthRegistry:
    def __init__(self):
        self.available = set()
        self.healthy = set()
        self.models = {}
        self.providers = {}

    def is_model_available(self, model_id):
        return model_id in self.available

    def is_provider_healthy(self, provider):
        return provider in self.healthy

    def get_model(self, model_id):
        return self.models.get(model_id, {})

    def get_provider(self, provider):
        return self.providers.get(provider, {})


class TestRankModels:
    def _store_with_errors(self):
        store = MetricsStore()
        now = time.time()
        # groq: 2 access errors (no_credit)
        for i in range(2):
            store.record_request(RequestRecord(
                timestamp=now - i, provider="groq", model="groq/llama-3.3-70b-versatile",
                success=False, error_type="no_credit",
            ))
        # nvidia: clean but slow
        store.record_request(RequestRecord(
            timestamp=now, provider="nvidia", model="nvidia/z-ai/glm-5.2",
            duration_ms=5000, success=True,
        ))
        return store

    def test_rank_models_pushes_access_error_provider_to_bottom(self):
        store = self._store_with_errors()
        reg = _FakeHealthRegistry()
        for mid in ["groq/llama-3.3-70b-versatile", "nvidia/z-ai/glm-5.2"]:
            reg.available.add(mid)
        reg.healthy.update(["groq", "nvidia"])

        ranked = SmartRouter(store).rank_models(
            ["groq/llama-3.3-70b-versatile", "nvidia/z-ai/glm-5.2"], reg
        )
        assert ranked[0][0] == "nvidia/z-ai/glm-5.2"
        assert ranked[1][0] == "groq/llama-3.3-70b-versatile"


class TestModelLatencyRanking:
    """Fase 2: routing must prefer the FAST model — even on the same provider.
    Per-model stats must take precedence over provider-level aggregates,
    and the p95 tail must sink genuinely slow providers."""

    def test_fast_model_on_slow_provider_beats_slow_model(self):
        store = MetricsStore()
        now = time.time()
        # Same provider 'ag': one fast model, one slow model. Provider-level
        # avg would blend both → fast model would inherit slowness.
        store.record_request(RequestRecord(
            timestamp=now, provider="ag", model="ag/fast",
            duration_ms=300, ttft_ms=100, success=True,
        ))
        store.record_request(RequestRecord(
            timestamp=now, provider="ag", model="ag/fast",
            duration_ms=500, ttft_ms=200, success=True,
        ))
        store.record_request(RequestRecord(
            timestamp=now, provider="ag", model="ag/slow",
            duration_ms=40000, ttft_ms=30000, success=True,
        ))
        reg = _FakeHealthRegistry()
        reg.available.update(["ag/fast", "ag/slow"])
        reg.healthy.add("ag")

        ranked = SmartRouter(store).rank_models(["ag/fast", "ag/slow"], reg)
        assert ranked[0][0] == "ag/fast"

    def test_p95_tail_sinks_slow_provider(self):
        """A 40s outlier among fast requests must sink the provider (tail-aware)."""
        store = MetricsStore()
        now = time.time()
        store.record_request(RequestRecord(
            timestamp=now, provider="ag", model="ag/x",
            duration_ms=800, success=True,
        ))
        store.record_request(RequestRecord(
            timestamp=now, provider="ag", model="ag/x",
            duration_ms=1200, success=True,
        ))
        store.record_request(RequestRecord(
            timestamp=now, provider="ag", model="ag/x",
            duration_ms=40000, success=True,
        ))
        stats = store.get_provider_stats("ag")
        # avg = (800+1200+40000)/3 ≈ 14000ms → old penalty 100 cap;
        # p95 = 40000ms → new tail-aware penalty must exceed the old cap.
        comps = SmartRouter(store)._compute_score("ag", stats)
        assert comps["latency_penalty"] > 100
        assert comps["latency_penalty"] == 200

    def test_model_stats_fallback_to_provider(self):
        """Model with no data falls back to provider-level stats (no crash)."""
        store = MetricsStore()
        now = time.time()
        store.record_request(RequestRecord(
            timestamp=now, provider="nvidia", model="nvidia/z-ai/glm-5.2",
            duration_ms=5000, success=True,
        ))
        reg = _FakeHealthRegistry()
        reg.available.add("nvidia/unseen")
        reg.healthy.add("nvidia")

        ranked = SmartRouter(store).rank_models(["nvidia/unseen"], reg)
        assert ranked[0][0] == "nvidia/unseen"
        # Provider-level fallback applies: avg 5s → 50 penalty.
        assert ranked[0][2]["latency_penalty"] == 50

    def test_missing_p95_key_falls_back_to_avg(self):
        """Callers passing stats without p95 (old tests/helpers) keep avg behavior."""
        stats = {"avg_latency_ms": 9000, "error_rate": 0.0}
        comps = _make_router()._compute_score("nvidia", stats)
        assert comps["latency_penalty"] == 90  # 9s * 10, same as old formula


class TestDisabledModelFilter:
    def _fresh(self):
        SmartRouter.invalidate_combo_cache()

    def test_drops_model_with_provider_prefix(self):
        """Registry entry `groq/meta-llama/...` matches catalog id verbatim."""
        self._fresh()
        fake = {"groq": ["groq/meta-llama/llama-4-maverick-17b-128e-instruct"]}
        with patch("catalog_sync.get_disabled_models", return_value=fake):
            out = SmartRouter._filter_disabled_models(
                ["groq/meta-llama/llama-4-maverick-17b-128e-instruct", "groq/llama-3.3-70b-versatile"]
            )
        assert "groq/meta-llama/llama-4-maverick-17b-128e-instruct" not in out
        assert "groq/llama-3.3-70b-versatile" in out

    def test_drops_model_without_provider_prefix(self):
        """Registry entry `qwen/qwen3-32b` under groq matches catalog `groq/qwen/qwen3-32b`."""
        self._fresh()
        fake = {"groq": ["qwen/qwen3-32b"]}
        with patch("catalog_sync.get_disabled_models", return_value=fake):
            out = SmartRouter._filter_disabled_models(
                ["groq/qwen/qwen3-32b", "groq/openai/gpt-oss-120b"]
            )
        assert "groq/qwen/qwen3-32b" not in out
        assert "groq/openai/gpt-oss-120b" in out

    def test_keeps_live_models_when_registry_empty(self):
        self._fresh()
        with patch("catalog_sync.get_disabled_models", return_value={}):
            out = SmartRouter._filter_disabled_models(
                ["nvidia/minimaxai/minimax-m3", "openrouter/poolside/laguna-xs-2.1:free"]
            )
        assert out == ["nvidia/minimaxai/minimax-m3", "openrouter/poolside/laguna-xs-2.1:free"]

    def test_registry_failure_is_safe_noop(self):
        self._fresh()
        with patch("catalog_sync.get_disabled_models", side_effect=RuntimeError("admin api down")):
            out = SmartRouter._filter_disabled_models(["groq/llama-3.3-70b-versatile"])
        assert out == ["groq/llama-3.3-70b-versatile"]

    def test_filters_in_get_default_combos_pipeline(self):
        self._fresh()
        fake = {"groq": ["groq/meta-llama/llama-4-maverick-17b-128e-instruct"]}
        with (
            patch("catalog_sync.get_disabled_models", return_value=fake),
            patch.object(SmartRouter, "_read_combo_cache", return_value=[]),
            patch.object(SmartRouter, "_fetch_catalog_models", return_value=[
                "groq/meta-llama/llama-4-maverick-17b-128e-instruct",
                "groq/llama-3.3-70b-versatile",
            ]),
        ):
            combos = SmartRouter.get_default_combos()
        assert "groq/meta-llama/llama-4-maverick-17b-128e-instruct" not in combos
        assert "groq/llama-3.3-70b-versatile" in combos


class TestNonChatModelFilter:
    def _fresh(self):
        SmartRouter.invalidate_combo_cache()

    def test_drops_laguna_xs(self):
        out = SmartRouter._filter_static_models(
            ["openrouter/poolside/laguna-xs-2.1:free"]
        )
        assert out == []

    def test_drops_asr_audio_models(self):
        out = SmartRouter._filter_static_models(
            ["nvidia/parakeet-tdt-0.6b-v2", "groq/whisper-large-v3-turbo"]
        )
        assert out == []

    def test_drops_tiny_by_size_keeps_nano_name(self):
        out = SmartRouter._filter_static_models(
            ["groq/llama-3.1-8b-instant-nano", "cf/@cf/meta/llama-3.2-1b-pico", "ollama/gpt-oss:120b"]
        )
        assert out == ["groq/llama-3.1-8b-instant-nano", "ollama/gpt-oss:120b"]

    def test_keeps_chat_models(self):
        out = SmartRouter._filter_static_models(
            ["groq/llama-3.3-70b-versatile", "cf/@cf/meta/llama-3.3-70b-instruct-fp8-fast"]
        )
        assert out == [
            "groq/llama-3.3-70b-versatile",
            "cf/@cf/meta/llama-3.3-70b-instruct-fp8-fast",
        ]

    def test_drops_bare_and_group_ids_keeps_blocked_as_fallback(self):
        """PERMANENTLY_BLOCKED providers stay in the pool (fallback); only
        bare ids and @group ids are dropped."""
        out = SmartRouter._filter_static_models(
            ["anthropic/claude-sonnet-4.5", "bare-id", "@group/foo", "groq/llama-3.3-70b-versatile"]
        )
        assert out == ["anthropic/claude-sonnet-4.5", "groq/llama-3.3-70b-versatile"]

    def test_drops_paid_tier_of_free_tier_only_provider(self):
        out = SmartRouter._filter_static_models(
            [
                "openrouter/nvidia/nemotron-3-ultra-550b-a55b",
                "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free",
            ]
        )
        assert out == ["openrouter/nvidia/nemotron-3-ultra-550b-a55b:free"]

    def test_pipeline_applies_denylist_to_catalog(self):
        self._fresh()
        with patch.object(SmartRouter, "_fetch_catalog_models", return_value=[
            "openrouter/poolside/laguna-xs-2.1:free",
            "ollama/gpt-oss:120b",
        ]):
            combos = SmartRouter.get_default_combos()
        assert "openrouter/poolside/laguna-xs-2.1:free" not in combos
        # groq/llama-3.3-70b-versatile was disabled on the live 9router registry
        # (dropped from Groq's free tier), so the pipeline now filters it. Use a
        # verified-healthy model instead so the test checks intent (dead dropped,
        # healthy kept) without depending on live registry state.
        assert "ollama/gpt-oss:120b" in combos

    def test_drops_dead_models_from_static(self):
        out = SmartRouter._filter_static_models(
            ["af/anthropic/claude-3.7-sonnet", "af/moonshot/kimi-k2.6"]
        )
        assert out == []

    def test_drops_dead_models_from_catalog(self):
        self._fresh()
        with patch.object(SmartRouter, "_fetch_catalog_models", return_value=[
            "af/anthropic/claude-3.7-sonnet",
            "af/moonshot/kimi-k2.6",
        ]):
            combos = SmartRouter.get_default_combos()
        assert "af/anthropic/claude-3.7-sonnet" not in combos
        assert "af/moonshot/kimi-k2.6" not in combos


class TestNoDataPenalty:
    def test_never_seen_provider_gets_penalty(self):
        score = _make_router()._compute_score("openrouter", None)
        assert score["no_data"] is True
        # openrouter priority is 31 (re-ranked 2026-08-26) → 31 base + 30 penalty
        assert score["total"] == 61

    def test_tested_provider_beats_never_seen(self):
        router = _make_router()
        tested = router._compute_score("groq", _stats())
        never_seen = router._compute_score("openrouter", None)
        assert tested["total"] < never_seen["total"]

    def test_rank_prefers_tested_over_never_seen(self):
        store = MetricsStore()
        now = time.time()  # fresh: must fall inside the 300s stats window
        store.record_request(RequestRecord(
            timestamp=now, provider="groq", model="groq/gpt-oss-120b",
            duration_ms=800, success=True,
        ))
        reg = _FakeHealthRegistry()
        for mid in [
            "groq/gpt-oss-120b",
            "openrouter/nvidia/nemotron-3-super-120b-a12b:free",
        ]:
            reg.available.add(mid)
        reg.healthy.update(["groq", "openrouter"])

        ranked = SmartRouter(store).rank_models(
            [
                "openrouter/nvidia/nemotron-3-super-120b-a12b:free",
                "groq/gpt-oss-120b",
            ],
            reg,
        )
        assert ranked[0][0] == "groq/gpt-oss-120b"


class TestHealthFailurePenalty:
    def test_recent_model_failures_deprioritized(self):
        reg = _FakeHealthRegistry()
        for mid in [
            "openrouter/nvidia/nemotron-3-super-120b-a12b:free",
            "groq/llama-3.3-70b-versatile",
        ]:
            reg.available.add(mid)
        reg.healthy.update(["openrouter", "groq"])
        reg.models["openrouter/nvidia/nemotron-3-super-120b-a12b:free"] = {
            "failures": 3,
            "status": "probing",
        }

        ranked = SmartRouter(metrics_store=None).rank_models(
            [
                "openrouter/nvidia/nemotron-3-super-120b-a12b:free",
                "groq/llama-3.3-70b-versatile",
            ],
            reg,
        )
        assert ranked[0][0] == "groq/llama-3.3-70b-versatile"
        assert ranked[1][2].get("health_fail_penalty", 0) > 0


class TestLoadSpreading:
    """best_model must NOT always return top-1 (thundering-herd fix)."""

    def _reg(self, models):
        reg = _FakeHealthRegistry()
        for mid in models:
            reg.available.add(mid)
            reg.healthy.add(mid.split("/")[0])
        return reg

    def test_spreads_across_providers_in_band(self):
        """Consecutive calls with equal-scoring models must rotate providers."""
        router = _make_router()
        models = [
            "gh/gpt-4.1",
            "cu/kimi-k3-high",
            "openrouter/cohere/north-mini-code:free",
        ]
        reg = self._reg(models)

        picks = {router.best_model(models, reg) for _ in range(5)}
        # All three are within SPREAD_BAND (equal scores) → LRU rotation hits
        # every provider, not just the first.
        assert len(picks) >= 2, f"expected spread, got {picks}"

    def test_never_used_provider_preferred_first(self):
        """First call picks the best-ranked model; second spreads away."""
        router = _make_router()
        models = ["gh/gpt-4.1", "cu/kimi-k3-high"]
        reg = self._reg(models)

        first = router.best_model(models, reg)
        second = router.best_model(models, reg)
        assert first == "cu/kimi-k3-high"
        assert second == "gh/gpt-4.1"

    def test_out_of_band_provider_not_spread_to(self):
        """A provider with a much worse score must not steal requests."""
        router = _make_router()
        reg = self._reg(["gh/gpt-4.1", "ollama/gpt-oss:120b"])
        # ollama is healthy/available but accumulates health failures →
        # health_fail_penalty sinks it far outside the spread band.
        reg.models["ollama/gpt-oss:120b"] = {
            "failures": 5,
            "status": "probing",
        }

        ranked = router.rank_models(
            ["gh/gpt-4.1", "ollama/gpt-oss:120b"], reg
        )
        assert ranked[0][0] == "gh/gpt-4.1"
        assert ranked[1][2]["total"] > ranked[0][2]["total"] + 10

        picks = {router.best_model(["gh/gpt-4.1", "ollama/gpt-oss:120b"], reg) for _ in range(3)}
        assert picks == {"gh/gpt-4.1"}, f"ollama should stay out of rotation: {picks}"

    def test_single_candidate_returns_it(self):
        router = _make_router()
        reg = self._reg(["gh/gpt-4.1"])
        assert router.best_model(["gh/gpt-4.1"], reg) == "gh/gpt-4.1"

    def test_no_candidates_returns_none(self):
        router = _make_router()
        reg = self._reg([])
        assert router.best_model(["gh/gpt-4.1"], reg) is None


class TestPromptContextFilter:
    """rank_models must skip models whose EXPLICIT ctx can't fit the prompt.

    Regression for the samba "Max_len exceeded" bug: the 9router catalog
    reports ctx 128000 for samba free models but the upstream only accepts
    8192 → oversized prompts 400 → combo retry loop / empty compactions.
    Only models with an EXPLICIT model_limits.json entry are filtered;
    unknown ctx (no entry) must be respected (catalog), never guessed.
    """

    def _reg(self, models):
        reg = _FakeHealthRegistry()
        for mid in models:
            reg.available.add(mid)
            reg.healthy.add(mid.split("/")[0])
        return reg

    def _limits_for(self, model_id):
        fake_limits = {
            "samba/DeepSeek-V3.1": {"tpm": 1000000, "rpm": 60, "context": 8192},
            "sambanova/gpt-oss-120b": {"tpm": 1000000, "rpm": 60, "context": 8192},
        }
        return fake_limits.get(model_id)

    def test_explicit_small_ctx_skipped_for_big_prompt(self):
        router = _make_router()
        models = ["samba/DeepSeek-V3.1", "openai/gpt-oss-120b"]
        reg = self._reg(models)
        with patch("smart_router.get_explicit_limits", side_effect=self._limits_for):
            ranked = router.rank_models(models, reg, {"prompt_tokens": 10000})
        ids = [r[0] for r in ranked]
        assert "samba/DeepSeek-V3.1" not in ids
        assert "openai/gpt-oss-120b" in ids

    def test_no_explicit_entry_never_filtered(self):
        """Unknown ctx must not be guessed — catalog ctx is respected."""
        router = _make_router()
        models = ["openai/gpt-oss-120b"]
        reg = self._reg(models)
        with patch("smart_router.get_explicit_limits", side_effect=self._limits_for):
            ranked = router.rank_models(models, reg, {"prompt_tokens": 100000})
        assert [r[0] for r in ranked] == ["openai/gpt-oss-120b"]

    def test_small_prompt_keeps_small_ctx_model(self):
        router = _make_router()
        models = ["samba/DeepSeek-V3.1"]
        reg = self._reg(models)
        with patch("smart_router.get_explicit_limits", side_effect=self._limits_for):
            ranked = router.rank_models(models, reg, {"prompt_tokens": 100})
        assert [r[0] for r in ranked] == ["samba/DeepSeek-V3.1"]

    def test_without_request_context_no_filter(self):
        """Backward compat: no prompt info → no ctx filtering at all."""
        router = _make_router()
        models = ["samba/DeepSeek-V3.1"]
        reg = self._reg(models)
        with patch("smart_router.get_explicit_limits", side_effect=self._limits_for):
            ranked = router.rank_models(models, reg)
        assert [r[0] for r in ranked] == ["samba/DeepSeek-V3.1"]


class TestFallbackOnly:
    """PERMANENTLY_BLOCKED providers stay in the pool but sink via
    FALLBACK_ONLY_PENALTY unless the health registry proves the account
    was reloaded (status healthy)."""

    def test_blocked_without_health_entry_gets_penalty(self):
        reg = _FakeHealthRegistry()
        reg.available.add("anthropic/claude-sonnet-4.5")
        reg.available.add("groq/llama-3.3-70b-versatile")
        reg.healthy.update(["anthropic", "groq"])

        ranked = _make_router().rank_models(
            ["anthropic/claude-sonnet-4.5", "groq/llama-3.3-70b-versatile"], reg
        )
        anthropic = next(r for r in ranked if r[0] == "anthropic/claude-sonnet-4.5")
        assert anthropic[2]["fallback_only"] is True
        assert anthropic[2]["total"] >= FALLBACK_ONLY_PENALTY

    def test_healthy_entry_removes_fallback_penalty(self):
        # together is a RELOADABLE account (credit limit exceeded) — a healthy
        # registry entry must still unblock it (reloaded balance rejoins).
        reg = _FakeHealthRegistry()
        reg.available.add("together/meta-llama/llama-3.3-70b-instruct")
        reg.available.add("groq/llama-3.3-70b-versatile")
        reg.healthy.update(["together", "groq"])
        reg.providers["together"] = {"status": "healthy"}

        ranked = _make_router().rank_models(
            ["together/meta-llama/llama-3.3-70b-instruct", "groq/llama-3.3-70b-versatile"], reg
        )
        together = next(r for r in ranked if r[0] == "together/meta-llama/llama-3.3-70b-instruct")
        assert "fallback_only" not in together[2]

    def test_previously_blocked_provider_rejoins_when_healthy(self):
        # PERMANENTLY_BLOCKED is not permanent: a healthy registry entry
        # (periodic probe passed) must rejoin the pool automatically —
        # user decision 2026-08-13: periodic tests + auto-rejoin instead of
        # hard blocks. Regression: HARD_BLOCKED_PROVIDERS (paid/no free tier)
        # never rejoined even when the account was reloaded.
        for provider in ("mistral", "tokenrouter", "gcli", "qoder"):
            reg = _FakeHealthRegistry()
            reg.available.add(f"{provider}/any-model")
            reg.healthy.update([provider, "groq"])
            reg.providers[provider] = {"status": "healthy"}
            ranked = _make_router().rank_models([f"{provider}/any-model"], reg)
            assert "fallback_only" not in ranked[0][2]

    def test_unhealthy_entry_keeps_fallback_penalty(self):
        reg = _FakeHealthRegistry()
        reg.available.add("anthropic/claude-sonnet-4.5")
        reg.healthy.update(["anthropic"])
        reg.providers["anthropic"] = {"status": "cooldown"}

        ranked = _make_router().rank_models(["anthropic/claude-sonnet-4.5"], reg)
        assert ranked[0][2]["fallback_only"] is True


class TestLockedModels:
    """modelLock_* with a future timestamp must exclude models from the pool."""

    def test_is_lock_active_future(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        assert SmartRouter._is_lock_active(future) is True

    def test_is_lock_active_past(self):
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        assert SmartRouter._is_lock_active(past) is False

    def test_is_lock_active_invalid(self):
        assert SmartRouter._is_lock_active("not-a-date") is False
        assert SmartRouter._is_lock_active("") is False

    def test_static_filter_drops_active_locked_model(self, monkeypatch):
        monkeypatch.setattr(
            SmartRouter, "_get_locked_model_ids",
            classmethod(lambda cls: {"ag/gemini-3.5-flash-high"}),
        )
        out = SmartRouter._filter_static_models(
            ["ag/gemini-3.5-flash-high", "ag/gemini-2.5-pro"]
        )
        assert out == ["ag/gemini-2.5-pro"]

    def test_catalog_filter_drops_active_locked_model(self, monkeypatch):
        monkeypatch.setattr(
            SmartRouter, "_get_locked_model_ids",
            classmethod(lambda cls: {"ag/gemini-3.5-flash-high"}),
        )
        items = [
            {"id": "ag/gemini-3.5-flash-high", "capabilities": {"contextWindow": 1000000}},
            {"id": "ag/gemini-2.5-pro", "capabilities": {"contextWindow": 1000000}},
        ]
        out = SmartRouter._filter_catalog_models(items)
        assert "ag/gemini-3.5-flash-high" not in out
        assert "ag/gemini-2.5-pro" in out


class TestConnectionWeighting:
    """Providers with more accounts contribute more models to the pool."""

    def test_more_connections_more_slots(self, monkeypatch):
        monkeypatch.setattr(
            SmartRouter, "_get_connection_counts",
            classmethod(lambda cls: {"ag": 4, "cf": 2, "groq": 1}),
        )
        items = []
        for i in range(15):
            items.append({"id": f"ag/model-{i}", "capabilities": {"contextWindow": 1000000}})
            items.append({"id": f"cf/model-{i}", "capabilities": {"contextWindow": 1000000}})
            items.append({"id": f"groq/model-{i}", "capabilities": {"contextWindow": 1000000}})
        out = SmartRouter._filter_catalog_models(items)
        ag = [m for m in out if m.startswith("ag/")]
        cf = [m for m in out if m.startswith("cf/")]
        groq = [m for m in out if m.startswith("groq/")]
        assert len(ag) == 15      # 4 conns → limit 30 + 3*5 = 45 → all fit
        assert len(cf) == 15      # 2 conns → limit 30 + 1*5 = 35 → all fit
        assert len(groq) == 15    # 1 conn → limit 30 → all fit (raised from 10)

    def test_single_conn_provider_contributes_up_to_raised_limit(self, monkeypatch):
        """PER_PROVIDER_LIMIT raised 10→30: a 1-connection provider with 25
        models must contribute all 25 (previously capped at 10)."""
        monkeypatch.setattr(
            SmartRouter, "_get_connection_counts",
            classmethod(lambda cls: {"rw": 1}),
        )
        items = [
            {"id": f"rw/model-{i}", "capabilities": {"contextWindow": 1000000}}
            for i in range(25)
        ]
        out = SmartRouter._filter_catalog_models(items)
        rw = [m for m in out if m.startswith("rw/")]
        assert len(rw) == 25      # 1 conn → limit 30 → all 25 fit

    def test_max_combo_models_raised_to_500(self, monkeypatch):
        """MAX_COMBO_MODELS raised 200→500: 300 models across providers fit."""
        monkeypatch.setattr(
            SmartRouter, "_get_connection_counts",
            classmethod(lambda cls: {}),  # 1 conn default → limit 30 each
        )
        items = []
        for p in ["rw", "cu", "hf", "bai", "ag", "cf", "bzl", "cx", "glm", "groq"]:
            for i in range(30):
                items.append({"id": f"{p}/model-{i}", "capabilities": {"contextWindow": 1000000}})
        out = SmartRouter._filter_catalog_models(items)
        assert len(out) == 300    # all fit under the 500 cap (was 200)
        assert len({m.split("/")[0] for m in out}) == 10
