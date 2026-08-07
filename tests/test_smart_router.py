"""Tests for smart_router — access_penalty scoring and rank_models ordering."""

from metrics_store import MetricsStore, RequestRecord
from smart_router import SmartRouter


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

    def is_model_available(self, model_id):
        return model_id in self.available

    def is_provider_healthy(self, provider):
        return provider in self.healthy


class TestRankModels:
    def _store_with_errors(self):
        store = MetricsStore()
        now = 1_700_000_000.0
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
