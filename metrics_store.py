"""In-memory rolling window metrics store for provider performance.

Tracks per-provider:
  - Request count (success/fail)
  - Token consumption (in/out/cache)
  - Latency stats (avg, p50, p95)
  - TTFT stats
  - Error count by type

Windows: last 5 min, 15 min, 60 min.
"""

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class RequestRecord:
    """A single request's metrics."""
    timestamp: float  # unix epoch
    provider: str
    model: str
    duration_ms: int = 0
    ttft_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cache: int = 0
    success: bool = True
    error_type: Optional[str] = None


@dataclass
class ProviderMetrics:
    """Aggregated metrics for a single provider."""
    # Counts
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0

    # Tokens
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cache: int = 0

    # Timing
    total_duration_ms: int = 0
    total_ttft_ms: int = 0
    durations: list[int] = field(default_factory=list)
    ttfts: list[int] = field(default_factory=list)

    # Errors
    errors_by_type: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    @property
    def avg_latency_ms(self) -> float:
        if not self.total_requests:
            return 0.0
        return self.total_duration_ms / self.total_requests

    @property
    def avg_ttft_ms(self) -> float:
        if not self.total_requests:
            return 0.0
        return self.total_ttft_ms / self.total_requests

    @property
    def p95_latency_ms(self) -> int:
        if not self.durations:
            return 0
        sorted_d = sorted(self.durations)
        idx = int(len(sorted_d) * 0.95)
        return sorted_d[min(idx, len(sorted_d) - 1)]

    @property
    def error_rate(self) -> float:
        if not self.total_requests:
            return 0.0
        return self.failed_requests / self.total_requests

    @property
    def tokens_per_second(self) -> float:
        """Output tokens per second (generation throughput)."""
        total_seconds = self.total_duration_ms / 1000
        if total_seconds <= 0:
            return 0.0
        return self.tokens_out / total_seconds


class MetricsStore:
    """Rolling-window metrics store with per-provider aggregation."""

    WINDOWS = [300, 900, 3600]  # 5min, 15min, 60min in seconds

    def __init__(self, max_records: int = 50000):
        self.max_records = max_records
        self.records: list[RequestRecord] = []
        self._last_cleanup = time.time()

    def record_request(self, record: RequestRecord):
        """Record a completed request."""
        self.records.append(record)
        if len(self.records) > self.max_records:
            self.records = self.records[-self.max_records:]
        self._cleanup()

    def _cleanup(self):
        """Remove records older than the max window (1h)."""
        now = time.time()
        if now - self._last_cleanup < 60:
            return  # only cleanup once per minute
        self._last_cleanup = now
        cutoff = now - 3600
        before = len(self.records)
        self.records = [r for r in self.records if r.timestamp >= cutoff]
        after = len(self.records)
        if before - after > 0:
            log.debug(f"Cleaned {before - after} old records")

    def _window_records(self, window_seconds: int) -> list[RequestRecord]:
        """Get records within the given time window."""
        cutoff = time.time() - window_seconds
        return [r for r in self.records if r.timestamp >= cutoff]

    def _aggregate(self, records: list[RequestRecord]) -> dict[str, ProviderMetrics]:
        """Aggregate records by provider."""
        providers: dict[str, ProviderMetrics] = defaultdict(ProviderMetrics)

        for r in records:
            pm = providers[r.provider]
            pm.total_requests += 1
            if r.success:
                pm.successful_requests += 1
            else:
                pm.failed_requests += 1
                if r.error_type:
                    pm.errors_by_type[r.error_type] += 1

            pm.tokens_in += r.tokens_in
            pm.tokens_out += r.tokens_out
            pm.tokens_cache += r.tokens_cache

            pm.total_duration_ms += r.duration_ms
            pm.total_ttft_ms += r.ttft_ms
            pm.durations.append(r.duration_ms)
            pm.ttfts.append(r.ttft_ms)

        return providers

    def get_provider_stats(self, provider: str, window_seconds: int = 300) -> Optional[dict]:
        """Get stats for a specific provider in the given window."""
        records = self._window_records(window_seconds)
        provider_records = [r for r in records if r.provider == provider]
        if not provider_records:
            return None
        agg = self._aggregate(provider_records)
        pm = agg.get(provider)
        if not pm:
            return None
        return {
            "provider": provider,
            "window_seconds": window_seconds,
            "total_requests": pm.total_requests,
            "successful": pm.successful_requests,
            "failed": pm.failed_requests,
            "error_rate": round(pm.error_rate, 4),
            "tokens_in": pm.tokens_in,
            "tokens_out": pm.tokens_out,
            "tokens_cache": pm.tokens_cache,
            "total_tokens": pm.tokens_in + pm.tokens_out,
            "avg_latency_ms": round(pm.avg_latency_ms, 1),
            "p95_latency_ms": pm.p95_latency_ms,
            "avg_ttft_ms": round(pm.avg_ttft_ms, 1),
            "tokens_per_second": round(pm.tokens_per_second, 1),
            "errors_by_type": dict(pm.errors_by_type),
        }

    def get_all_stats(self, window_seconds: int = 300) -> dict:
        """Get stats for all providers in the given window."""
        records = self._window_records(window_seconds)
        agg = self._aggregate(records)

        return {
            "window_seconds": window_seconds,
            "total_records": len(records),
            "providers": {
                name: {
                    "total_requests": pm.total_requests,
                    "successful": pm.successful_requests,
                    "failed": pm.failed_requests,
                    "error_rate": round(pm.error_rate, 4),
                    "tokens_in": pm.tokens_in,
                    "tokens_out": pm.tokens_out,
                    "tokens_cache": pm.tokens_cache,
                    "total_tokens": pm.tokens_in + pm.tokens_out,
                    "avg_latency_ms": round(pm.avg_latency_ms, 1),
                    "p95_latency_ms": pm.p95_latency_ms,
                    "avg_ttft_ms": round(pm.avg_ttft_ms, 1),
                    "tokens_per_second": round(pm.tokens_per_second, 1),
                    "errors_by_type": dict(pm.errors_by_type),
                }
                for name, pm in agg.items()
            },
            "global": self._global_stats(agg),
        }

    def _global_stats(self, agg: dict[str, ProviderMetrics]) -> dict:
        """Aggregate across all providers."""
        total_req = sum(pm.total_requests for pm in agg.values())
        total_ok = sum(pm.successful_requests for pm in agg.values())
        total_tokens_in = sum(pm.tokens_in for pm in agg.values())
        total_tokens_out = sum(pm.tokens_out for pm in agg.values())
        total_dur = sum(pm.total_duration_ms for pm in agg.values())

        return {
            "total_requests": total_req,
            "successful": total_ok,
            "failed": total_req - total_ok,
            "error_rate": round((total_req - total_ok) / total_req, 4) if total_req else 0,
            "tokens_in": total_tokens_in,
            "tokens_out": total_tokens_out,
            "total_tokens": total_tokens_in + total_tokens_out,
            "avg_latency_ms": round(total_dur / total_req, 1) if total_req else 0,
        }

    def get_recent_requests(self, limit: int = 50) -> list[dict]:
        """Get the most recent requests."""
        recent = self.records[-limit:]
        return [
            {
                "timestamp": r.timestamp,
                "provider": r.provider,
                "model": r.model,
                "duration_ms": r.duration_ms,
                "ttft_ms": r.ttft_ms,
                "tokens_in": r.tokens_in,
                "tokens_out": r.tokens_out,
                "tokens_cache": r.tokens_cache,
                "success": r.success,
                "error_type": r.error_type,
            }
            for r in reversed(recent)
        ]

    def get_provider_list(self) -> list[str]:
        """Get list of all known providers."""
        return list(set(r.provider for r in self.records))
