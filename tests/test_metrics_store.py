"""Tests for MetricsStore aggregation semantics."""

import time

import pytest

from metrics_store import MetricsStore, RequestRecord


def _record(
    provider: str,
    *,
    success: bool = True,
    error_type=None,
    ts: float | None = None,
) -> RequestRecord:
    return RequestRecord(
        timestamp=ts if ts is not None else time.time(),
        provider=provider,
        model=f"{provider}/model",
        success=success,
        error_type=error_type,
    )


class TestRequestInvalidNotAFailure:
    def test_request_invalid_not_counted_as_failed(self):
        ms = MetricsStore()
        ms.record_request(_record("groq"))
        ms.record_request(_record("groq", success=False, error_type="request_invalid"))
        ms.record_request(_record("groq", success=False, error_type="generic_429"))

        stats = ms.get_provider_stats("groq")
        assert stats["total_requests"] == 3
        assert stats["successful"] == 1
        assert stats["failed"] == 1  # only generic_429 counts
        assert stats["error_rate"] == round(1 / 3, 4)
        assert stats["errors_by_type"] == {
            "request_invalid": 1,
            "generic_429": 1,
        }

    def test_request_invalid_alone_yields_zero_error_rate(self):
        ms = MetricsStore()
        ms.record_request(_record("nvidia", success=False, error_type="request_invalid"))

        stats = ms.get_provider_stats("nvidia")
        assert stats["failed"] == 0
        assert stats["error_rate"] == 0.0
        assert stats["errors_by_type"]["request_invalid"] == 1

    def test_global_stats_use_real_failures(self):
        ms = MetricsStore()
        ms.record_request(_record("groq"))
        ms.record_request(_record("groq", success=False, error_type="request_invalid"))
        ms.record_request(_record("nvidia", success=False, error_type="no_credit"))

        agg = ms.get_all_stats()
        g = agg["global"]
        assert g["total_requests"] == 3
        assert g["successful"] == 1
        assert g["failed"] == 1  # request_invalid excluded

    def test_provider_metrics_error_rate_property(self):
        from metrics_store import ProviderMetrics

        pm = ProviderMetrics()
        pm.total_requests = 2
        pm.failed_requests = 1
        assert pm.error_rate == 0.5


class TestGetAccessErrors:
    def test_aggregates_access_errors_by_provider_and_type(self):
        ms = MetricsStore()
        ms.record_request(_record("groq", success=False, error_type="no_credit"))
        ms.record_request(_record("groq", success=False, error_type="no_credit"))
        ms.record_request(_record("groq", success=False, error_type="monthly_limit"))
        ms.record_request(_record("nvidia", success=False, error_type="auth_invalid"))
        # Non-access errors must NOT appear
        ms.record_request(_record("groq", success=False, error_type="generic_429"))

        data = ms.get_access_errors()
        assert data["total"] == 4
        assert data["providers"] == 2
        groq = data["by_provider"]["groq"]
        assert groq["total"] == 3
        assert groq["types"] == {"no_credit": 2, "monthly_limit": 1}
        nvidia = data["by_provider"]["nvidia"]
        assert nvidia["types"] == {"auth_invalid": 1}

    def test_empty_when_no_access_errors(self):
        ms = MetricsStore()
        ms.record_request(_record("groq"))
        ms.record_request(_record("groq", success=False, error_type="generic_429"))
        data = ms.get_access_errors()
        assert data["total"] == 0
        assert data["providers"] == 0
        assert data["by_provider"] == {}

    def test_last_seen_and_models(self):
        ms = MetricsStore()
        ts = time.time()
        ms.record_request(_record("groq", success=False, error_type="no_credit", ts=ts - 100))
        ms.record_request(_record("groq", success=False, error_type="no_credit", ts=ts))
        data = ms.get_access_errors()
        groq = data["by_provider"]["groq"]
        assert groq["last_seen"] == pytest.approx(ts, abs=1)
        assert groq["models"] == ["groq/model"]

    def test_respects_window(self):
        ms = MetricsStore()
        old = time.time() - 6000  # outside 1h retention, but let's use a smaller window
        ms.record_request(_record("groq", success=False, error_type="no_credit", ts=old))
        data = ms.get_access_errors(window_seconds=60)
        assert data["total"] == 0

    def test_access_errors_still_count_as_failures_for_health(self):
        """Access errors are visible in errors_by_type and count as failed for
        health scoring — only request_invalid is non-penalizing."""
        ms = MetricsStore()
        ms.record_request(_record("groq", success=False, error_type="no_credit"))
        stats = ms.get_provider_stats("groq")
        assert stats["failed"] == 1
        assert stats["errors_by_type"]["no_credit"] == 1


class TestGetModelStats:
    """Per-model stats must isolate latency by MODEL, not just provider —
    a fast model on a slow provider must not inherit the provider's latency."""

    def _seed(self) -> MetricsStore:
        ms = MetricsStore()
        now = time.time()
        ms.record_request(RequestRecord(
            timestamp=now, provider="ag", model="ag/gemini-3.5-flash",
            duration_ms=500, ttft_ms=120, success=True,
        ))
        ms.record_request(RequestRecord(
            timestamp=now, provider="ag", model="ag/gemini-3.5-flash",
            duration_ms=800, ttft_ms=200, success=True,
        ))
        ms.record_request(RequestRecord(
            timestamp=now, provider="ag", model="ag/gemini-2.5-pro",
            duration_ms=40000, ttft_ms=30000, success=True,
        ))
        return ms

    def test_model_stats_isolated_from_provider_aggregate(self):
        ms = self._seed()
        fast = ms.get_model_stats("ag/gemini-3.5-flash")
        assert fast["total_requests"] == 2
        assert fast["avg_latency_ms"] == 650.0
        assert fast["p95_latency_ms"] == 800
        assert fast["avg_ttft_ms"] == 160.0
        assert fast["failed"] == 0
        assert fast["provider"] == "ag"

        slow = ms.get_model_stats("ag/gemini-2.5-pro")
        assert slow["total_requests"] == 1
        assert slow["avg_latency_ms"] == 40000.0

        # Provider-level aggregate blends BOTH models — the router must
        # prefer the per-model view to avoid sinking the fast model.
        prov = ms.get_provider_stats("ag")
        assert prov["total_requests"] == 3
        assert prov["avg_latency_ms"] == round((500 + 800 + 40000) / 3, 1)

    def test_unknown_model_returns_none(self):
        ms = self._seed()
        assert ms.get_model_stats("ag/nope") is None

    def test_model_stats_respect_window(self):
        ms = MetricsStore()
        old = time.time() - 6000
        ms.record_request(RequestRecord(
            timestamp=old, provider="ag", model="ag/gemini-3.5-flash",
            duration_ms=100, success=True,
        ))
        assert ms.get_model_stats("ag/gemini-3.5-flash", window_seconds=60) is None
        assert ms.get_model_stats("ag/gemini-3.5-flash", window_seconds=7200) is not None

    def test_get_all_model_stats_groups_by_model(self):
        ms = self._seed()
        out = ms.get_all_model_stats()
        assert out["total_records"] == 3
        assert set(out["models"]) == {"ag/gemini-3.5-flash", "ag/gemini-2.5-pro"}
        assert out["models"]["ag/gemini-3.5-flash"]["avg_latency_ms"] == 650.0
