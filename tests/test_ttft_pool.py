"""Testes para ttft_percentiles (todo 7) e transições de pool no alerter."""

import time

from metrics_store import MetricsStore, RequestRecord


def _record(ttft_ms: int, ts_offset: float = 0.0) -> RequestRecord:
    return RequestRecord(
        timestamp=time.time() - ts_offset,
        provider="prov",
        model="model",
        ttft_ms=ttft_ms,
    )


def test_ttft_percentiles_basic():
    store = MetricsStore()
    # 10 amostras: p50 = 5º valor ordenado, p95 ≈ 10º
    for i, ttft in enumerate([500, 100, 400, 200, 300, 900, 800, 700, 600, 1000]):
        store.record_request(_record(ttft))

    result = store.ttft_percentiles()
    assert result["samples"] == 10
    assert result["p50"] == 500
    assert result["p95"] == 1000


def test_ttft_zero_is_ignored():
    """ttft_ms == 0 significa 'não medido' — não entra no cálculo."""
    store = MetricsStore()
    store.record_request(_record(0))
    store.record_request(_record(300))
    result = store.ttft_percentiles()
    assert result == {"p50": 300, "p95": 300, "samples": 1}


def test_ttft_empty_store_returns_nones():
    store = MetricsStore()
    assert store.ttft_percentiles() == {"p50": None, "p95": None, "samples": 0}


def test_old_records_excluded_by_window():
    store = MetricsStore()
    store.record_request(_record(100, ts_offset=7200))   # 2h atrás
    store.record_request(_record(500))                    # agora
    result = store.ttft_percentiles(window_seconds=3600)
    assert result == {"p50": 500, "p95": 500, "samples": 1}


def test_significant_transitions_include_pool():
    from alerter import SIGNIFICANT_TRANSITIONS

    assert ("healthy", "degraded") in SIGNIFICANT_TRANSITIONS
    assert ("degraded", "healthy") in SIGNIFICANT_TRANSITIONS
