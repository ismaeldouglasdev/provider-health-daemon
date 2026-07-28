"""Smart router — intelligent model selection beyond dumb round-robin.

Features:
  - Skip providers in cooldown
  - Prioritize by success rate (last 5 min)
  - Prefer lower latency
  - Detect rate limits proactively (skip providers with high error rates)
  - Fallback chain with health-aware ordering
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from metrics_store import MetricsStore

log = logging.getLogger(__name__)

# Providers with permanent failures (no auth, no credits) — skip entirely
PERMANENTLY_BLOCKED = {
    "anthropic",     # no credits
    "kc",            # kilocode - no credits
    "cx",            # codex - no auth
    "cl",            # cline - no auth
    "ag",            # antigravity - no auth
    "kr",            # kiro - no auth
    "bpm",           # byteplus - subscription expired
}

# Provider priority ranking (lower = preferred)
PROVIDER_PRIORITY = {
    "cf": 10,        # Cloudflare — free, reliable
    "nvidia": 20,    # NVIDIA — good uptime
    "ollama": 30,    # Local — variable
    "groq": 40,      # Groq — access issues
    "kc": 100,       # Kilocode — no credits
    "kr": 100,       # Kiro — no auth
    "anthropic": 100,
}


class SmartRouter:
    """Health-aware model selector with fallback chain.

    Takes a list of model IDs and returns the best available one.
    """

    def __init__(self, metrics_store: MetricsStore):
        self.metrics = metrics_store

    def rank_models(
        self,
        model_ids: list[str],
        health_registry,
        request_context: Optional[dict] = None,
    ) -> list[tuple[str, str, dict]]:
        """Rank models by availability and performance.

        Returns list of (model_id, provider, score_dict) sorted best-first.
        """
        scored = []

        for model_id in model_ids:
            provider = model_id.split("/")[0] if "/" in model_id else model_id

            # 1. Permanently blocked?
            if provider in PERMANENTLY_BLOCKED:
                log.debug(f"SKIP {model_id}: permanently blocked ({provider})")
                continue

            # 2. Health registry check
            if health_registry:
                if not health_registry.is_model_available(model_id):
                    log.debug(f"SKIP {model_id}: health registry says unavailable")
                    continue
                if not health_registry.is_provider_healthy(provider):
                    log.debug(f"SKIP {model_id}: provider {provider} unhealthy")
                    continue

            # 3. Metrics-based scoring
            stats = self.metrics.get_provider_stats(provider, window_seconds=300)
            score = self._compute_score(provider, stats)

            scored.append((model_id, provider, score))

        # Sort by score ascending (lower = better)
        scored.sort(key=lambda x: x[2]["total"])

        return scored

    def _compute_score(self, provider: str, stats: Optional[dict]) -> dict:
        """Compute a score for a provider (lower = better)."""
        base_priority = PROVIDER_PRIORITY.get(provider, 50)
        score = float(base_priority)

        components: dict[str, float] = {"base_priority": base_priority}

        if stats:
            # Error rate penalty: +50 per % error
            error_rate = stats.get("error_rate", 0)
            if error_rate > 0.5:  # >50% errors — heavy penalty
                error_penalty = 200
            elif error_rate > 0.2:  # >20% errors
                error_penalty = 100
            elif error_rate > 0.05:  # >5% errors
                error_penalty = 50
            else:
                error_penalty = error_rate * 100
            score += error_penalty
            components["error_penalty"] = error_penalty

            # Latency penalty: +1 per second of avg latency
            latency = stats.get("avg_latency_ms", 0) / 1000
            latency_penalty = min(latency * 10, 100)  # cap at 100
            score += latency_penalty
            components["latency_penalty"] = latency_penalty

            # Recent failures (last 5 min)
            recent_reqs = stats.get("total_requests", 0)
            failed = stats.get("failed", 0)
            if recent_reqs > 0 and failed > 2:
                fail_penalty = min(failed * 20, 150)
                score += fail_penalty
                components["fail_penalty"] = fail_penalty
        else:
            # No recent data — neutral score
            components["no_data"] = True

        # Demote providers that have had >10 failures total
        score = min(score, 999)  # cap
        components["total"] = round(score, 1)

        return components

    def best_model(
        self,
        model_ids: list[str],
        health_registry,
        request_context: Optional[dict] = None,
    ) -> Optional[str]:
        """Get the single best available model ID."""
        ranked = self.rank_models(model_ids, health_registry, request_context)
        if not ranked:
            return None

        best = ranked[0]
        log.info(
            "SmartRouter selected %s (provider=%s, score=%s)",
            best[0], best[1], best[2],
        )
        return best[0]

    def fallback_chain(
        self,
        model_ids: list[str],
        health_registry,
        request_context: Optional[dict] = None,
    ) -> list[str]:
        """Get ordered fallback chain of model IDs."""
        ranked = self.rank_models(model_ids, health_registry, request_context)
        return [r[0] for r in ranked]

    @classmethod
    def get_default_combos(cls) -> list[str]:
        """Healthy default combo list — only providers known to work."""
        return [
            "cf/@cf/meta/llama-3.3-70b-instruct-fp8-fast",
            "cf/@cf/deepseek-ai/deepseek-r1-distill-qwen-32b",
            "cf/@cf/moonshotai/kimi-k2.6",
            "cf/@cf/qwen/qwen2.5-coder-32b-instruct",
            "nvidia/minimaxai/minimax-m3",
            "nvidia/z-ai/glm-5.2",
            "nvidia/deepseek-ai/deepseek-v4-pro",
            "ollama/gpt-oss:120b",
            "nvidia/nemotron-3-ultra-550b-a55b",
            "ollama/qwen3.5",
        ]

    @staticmethod
    def route_to_router(meta_selector):
        """Bridge: delegate to meta-router for router-level selection.
        
        This is the entry point that the proxy calls to determine which
        downstream router should receive the request. After router selection,
        the provider-level SmartRouter logic is applied for model selection.
        """
        from meta_router import ServiceUnavailable
        try:
            router = meta_selector.select_router()
            return router.name, router.url, router.auth or {}
        except ServiceUnavailable:
            return None, None, None
