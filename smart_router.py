"""Smart router — intelligent model selection beyond dumb round-robin.

Features:
  - Skip providers in cooldown
  - Prioritize by success rate (last 5 min)
  - Prefer lower latency
  - Detect rate limits proactively (skip providers with high error rates)
  - Fallback chain with health-aware ordering
"""

import json
import logging
import os
import time
import urllib.request
from datetime import datetime, timezone
from typing import Optional

from config import COMBO_CACHE_FILE, COMBO_REFRESH_INTERVAL, NINEROUTER_KEY, NINEROUTER_URL
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
    "ps",            # poolside - 404 laguna-s-2.1 (model unknown to API)
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

# Account-access error types: won't self-heal within the 5-min scoring window.
_ACCESS_ERROR_TYPES = {
    "subscription_level",
    "no_credit",
    "no_credentials",
    "payment_required",
    "paid_required",
    "auth_invalid",
    "invalid_subscription",
    "monthly_limit",
    "daily_free_exhausted",
}

_STATIC_COMBOS = [
    "cf/@cf/meta/llama-3.1-70b-instruct-fp8-fast",
    "cf/@cf/meta/llama-3.1-8b-instruct-fp8-fast",
    "cf/@cf/meta/llama-3.3-70b-instruct-fp8-fast",
    "cf/@cf/mistralai/mistral-small-3.1-24b-instruct",
    "cf/@cf/qwen/qwen2.5-coder-32b-instruct",
    "groq/llama-3.3-70b-versatile",
    "nvidia/z-ai/glm-5.2",
    "ollama/gpt-oss:120b",
]

PER_PROVIDER_LIMIT = 5
MAX_COMBO_MODELS = 60
_COMBO_CACHE_TTL = 24 * 3600  # disk cache validity (seconds)


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

            # Account-access penalty: subscription_level / no_credit / auth errors
            # won't self-heal in the window — sink provider to the bottom.
            errors_by_type = stats.get("errors_by_type") or {}
            access_count = sum(
                errors_by_type.get(t, 0) for t in _ACCESS_ERROR_TYPES
            )
            if access_count > 0:
                access_penalty = 300 + min(access_count * 50, 400)
                score += access_penalty
                components["access_penalty"] = access_penalty

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

    _combo_cache: list[str] = []
    _combo_cache_time: float = 0.0

    @classmethod
    def get_default_combos(cls) -> list[str]:
        """Real combo candidates: in-memory → catalog → disk cache → static."""
        now = time.time()
        if cls._combo_cache and now - cls._combo_cache_time < COMBO_REFRESH_INTERVAL:
            return cls._combo_cache

        models = cls._fetch_catalog_models()
        if models:
            cls._write_combo_cache(models)
        else:
            models = cls._read_combo_cache()
            if not models:
                log.warning("9router catalog unavailable; using static combo list")
                models = list(_STATIC_COMBOS)
        models = cls._filter_static_models(models)
        cls._combo_cache = models
        cls._combo_cache_time = now
        return models

    @classmethod
    def _read_combo_cache(cls) -> list[str]:
        """Load last-good catalog from disk (fresh only)."""
        try:
            if not COMBO_CACHE_FILE.exists():
                return []
            if time.time() - COMBO_CACHE_FILE.stat().st_mtime > _COMBO_CACHE_TTL:
                log.warning("Combo disk cache stale; ignoring")
                return []
            data = json.loads(COMBO_CACHE_FILE.read_text())
            if not isinstance(data, list):
                return []
            return [m for m in data if isinstance(m, str)]
        except (OSError, json.JSONDecodeError) as e:
            log.warning(f"Combo disk cache unreadable: {e}")
            return []

    @classmethod
    def _write_combo_cache(cls, models: list[str]) -> None:
        """Persist catalog atomically so a dead gateway still has a fallback."""
        try:
            COMBO_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = COMBO_CACHE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(models))
            os.replace(tmp, COMBO_CACHE_FILE)
        except OSError as e:
            log.warning(f"Combo disk cache write failed: {e}")

    @staticmethod
    def _filter_static_models(model_ids: list[str]) -> list[str]:
        """Drop blocked/@ providers and bare ids from cached/static lists."""
        out: list[str] = []
        for mid in model_ids:
            if not isinstance(mid, str) or "/" not in mid:
                continue
            provider = mid.split("/")[0]
            if provider.startswith("@") or provider in PERMANENTLY_BLOCKED:
                continue
            if mid not in out:
                out.append(mid)
        return out

    @classmethod
    def _fetch_catalog_models(cls) -> list[str]:
        """Fetch real model IDs from the 9router catalog, best-first."""
        try:
            req = urllib.request.Request(
                f"{NINEROUTER_URL}/v1/models",
                headers={"Authorization": f"Bearer {NINEROUTER_KEY}"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                payload = json.loads(resp.read())
        except Exception as e:
            log.warning(f"Combo catalog fetch failed: {e}")
            return []

        items = payload.get("data", []) if isinstance(payload, dict) else []
        if not isinstance(items, list):
            return []
        return cls._filter_catalog_models(items)

    @staticmethod
    def _filter_catalog_models(items: list) -> list[str]:
        """Keep real, non-blocked models — bounded, priority-ordered."""
        per_provider: dict[str, list[tuple[int, str]]] = {}
        for it in items:
            if not isinstance(it, dict):
                continue
            model_id = it.get("id")
            if not isinstance(model_id, str) or "/" not in model_id:
                continue  # bare IDs (combos) carry no provider
            provider = model_id.split("/")[0]
            if provider.startswith("@") or provider in PERMANENTLY_BLOCKED:
                continue
            caps = it.get("capabilities") or {}
            context = caps.get("contextWindow") or 0
            per_provider.setdefault(provider, []).append((context, model_id))

        ranked: list[tuple[int, str]] = []
        for provider, models in per_provider.items():
            models.sort(reverse=True)  # larger context first
            priority = PROVIDER_PRIORITY.get(provider, 50)
            ranked.extend((priority, m) for _, m in models[:PER_PROVIDER_LIMIT])
        ranked.sort(key=lambda x: x[0])
        return [m for _, m in ranked[:MAX_COMBO_MODELS]]

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
