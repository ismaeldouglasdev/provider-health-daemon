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
import re
import threading
import time
import urllib.request
from datetime import datetime, timezone
from typing import Optional

from config import (
    CATALOG_TIMEOUT,
    COMBO_CACHE_FILE,
    COMBO_REFRESH_INTERVAL,
    NINEROUTER_KEY,
    NINEROUTER_URL,
    QUOTA_AWARE_ROTATION,
)
from metrics_store import MetricsStore

try:
    from prompt_limiter import get_explicit_limits
except ImportError:
    def get_explicit_limits(model_id: str) -> dict | None:
        return None

log = logging.getLogger(__name__)

# Providers with permanent failures (no auth, no credits) — skip entirely
PERMANENTLY_BLOCKED = {
    "anthropic",     # no credits
    "kc",            # kilocode - no credits
    "cl",            # cline - no auth
    "bpm",           # byteplus - subscription expired (402 verified 2026-08-10)
    "ps",            # poolside - 404 laguna-s-2.1 (model unknown to API)
    "cbai",          # cursor - credits exhausted (429 verified 2026-08-10)
    "gcli",          # grok cli - requires paid subscription
    "qoder",         # qoder - requires paid subscription (403)
    "kimi",          # kimi - no active credentials (404 on all models)
    "xai",           # xai - no active credentials (404 on all models)
    "mistral",       # mistral api - paid, no free tier (mistral-large/codestral/medium)
    "tokenrouter",   # tokenrouter - paid (kimi-k3-free reports subscription_level)
    "together",      # together ai - credit limit exceeded even on free-tier models (verified 2026-08-11)
    "di",            # deepinfra - no balance (402 "You need positive balance") (verified 2026-08-11)
    "replicate",     # replicate - no credit (402 "Insufficient credit", 429 until payment method) (verified 2026-08-11)
    "kimchi",        # cast ai serverless - ALL models 402 credits exhausted / 410 model_not_found (verified 2026-08-13)
    "modelscope",    # modelscope - 401 "Please bind your Alibaba Cloud account before use" on all models (verified 2026-08-13)
    "cerebras",      # 402 payment required on all models via 9router (verified 2026-08-26)
    "blockrun",      # 402 payment required on all models via 9router (verified 2026-08-26)
    "bzl",           # 402 payment required on all models via 9router (verified 2026-08-26)
    "hf",            # 404 on all models via 9router, no valid API key (verified 2026-08-26)
    "pollinations",  # payment_required on all models (verified 2026-08-26)
}

# Providers whose FREE tier is only reachable via `:free`-suffixed ids
FREE_TIER_SUFFIX_ONLY = {"openrouter"}

# Providers whose FREE tier is only reachable via `-free-auto`-suffixed ids
FREE_TIER_AUTO_SUFFIX_ONLY: set[str] = set()  # gh removed 2026-08-10: gpt-4o-mini-2024-07-18 works without suffix

# Model-name tokens for NON-chat / agent-unusable models (asr/audio/tts/
# embedding) — they advertise big context windows but produce garbage for
# agentic chat. Checked on the last path segment, split on [-_:.]
# Size tokens (xs/nano/tiny/pico/micro) were REMOVED 2026-08-11: they killed
# legit mid-size models like nemotron-nano 9B-30B (AnyAPI). Tiny models are
# now handled by _is_tiny_model() instead — a numeric-size filter, not a name
# token — so a "nano" 30B model stays in the pool while a real 2B runt is
# blocked. "laguna": poolside laguna-xs/s 2.1B/8B, known-404 via ps upstream.
# "thinking"/"agentic": kiro variants require a non-standard request body the
# proxy never sends — all 8 tested returned 400 REQUEST_BODY_INVALID
# (verified 2026-08-10). Harmless to keep in the pool otherwise.
NON_CHAT_MODEL_TOKENS = {
    "asr", "parakeet", "whisper", "speech",
    "audio", "tts", "stt",
    "embedding", "embed", "rerank",
    "laguna",  # poolside laguna-xs/s: 2.1B/8B, known-404 via ps upstream
    "thinking", "agentic",  # kiro: 400 REQUEST_BODY_INVALID via proxy (verified)
}

# Numeric model-size tiers (billions of params), parsed from the model id
# (e.g. "nemotron-3-nano-30b-a3b" -> 30, "llama-3.3-70b" -> 70). Models whose
# size we can't parse get NO penalty (assume big enough / unknown). Smaller
# models are demoted in ranking so the combo prefers capable ones while still
# keeping the small ones as last-resort fallbacks.
SIZE_PENALTY_TIERS = [
    # (min_billions, penalty); first match wins, biggest size checked last
    (80.0, 0),
    (40.0, 15),
    (20.0, 50),
    (8.0, 100),
]
TINY_MODEL_MAX_B = 8.0  # below this (parsed size) the model is blocked outright
_SIZE_B_RE = re.compile(r"(\d+(?:\.\d+)?)b(?![\w])", re.IGNORECASE)

# Model-ID substrings that mark whole families as dead (any variant). Unlike
# DEAD_MODELS (exact match), these block every -picker/-secondary/-tertiary
# variant of a broken family without listing each one.
BLOCKED_MODEL_SUBSTRINGS = {
    "mai-code",  # github: all variants 400 "requested model not supported" (verified)
    "glm-4.6v",  # Z.AI GLM vision (small ctx): overflows on normal agentic prompts (400 code 1261, verified 2026-08-11)
    "copilot-search",  # github search models: NOT chat-agentic — refuse agentic prompts with
    # "I'm sorry, I don't have the necessary tools" and fire tool calls during summary
    # (breaks opencode compaction: "Tool call not allowed while generating summary").
    # Selected by combo 3x in a row on 2026-08-13 (gh/copilot-search-b) — root cause of
    # the "combo quebrado" report. Blocked here at the source (verified 2026-08-13).
    "exec-agent",  # github execution agents: same problem — not general chat models
    # (exec-agent-a/b/c; exec-agent-c already DEAD_MODELS: empty body). Verified 2026-08-13.
    "claude-opus-5",  # kiro 400 "Invalid model ID or insufficient subscription" (genuine Kiro rejection,
    # não é transient) — contamina o provider kr e adia a seleção dos modelos rápidos
    # (claude-haiku-4.5 1.8s, minimax-m2.1 1.9s). Verificado 2026-09-03.
    "claude-sonnet-5",  # kiro 400 "Invalid model ID..." (mesma causa). Verificado 2026-09-03.
    "claude-opus-4.8",  # kiro 400 Invalid model ID. Verificado 2026-09-03.
    "gemini-3.5-flash",  # Google descontinuou o Gemini 3.5 Flash (retired). Variantes
    # (ag/cu/rw/gemini/blockrun/bai) retornam "Gemini 3.5 Flash is no longer available",
    # 404 NOT_FOUND ou timeout de 60s+ — não entram cooldown (sem status model-level no
    # health), então o combo os re-seleciona indefinidamente, causando stall de ~2min
    # ("só carrega e não responde"). Bloqueado aqui na fonte. Verificado 2026-09-03:
    # gemini/gemini-3.5-flash-lite timeout 60s, ag/gemini-3.5-flash-high 404, rw 429.
    # Alternativa viva: gemini-3.6-flash / 3.7-flash (200 OK).
    "gemini-3.7-flash-high",  # ag: lento (11-17s real, avg ~16s verified 2026-09-03) — gera picos
    # de latência no combo. Bloqueado pra manter consistência rápida (kr/haiku 1.8s).
    "gemini-3.7-flash-medium",  # ag: lento (~12.6s). Mesma causa. Verificado 2026-09-03.
    "gemini-3.1-pro",  # ag: lento (13.6s). Verificado 2026-09-03.
    "gemini-pro-agent",  # ag: resposta vazia em stress test. Verificado 2026-09-03.
}

# Per-provider allowlists: when a provider is listed, ONLY these model ids are
# eligible for the pool. Everything else is rejected at the source — no more
# whack-a-mole when stale variants resurface. Verified by live batch test
# against 9router:20128 on 2026-08-10.
PROVIDER_ALLOWLIST = {
    "gh": {
        # 8 verified OK + 2 transient 429 (self-heal) + gpt-4o-mini variants.
        # EXCLUDED 2026-08-13: copilot-search-* and exec-agent-* are search/
        # execution models, NOT chat-agentic — they refuse agentic prompts and
        # fire tool calls during summary ("Tool call not allowed while
        # generating summary"), breaking the combo. gpt-3.5-turbo too weak.
        "gh/gpt-4-o-preview",
        "gh/gpt-4-0125-preview",  # 429 transient only
        "gh/gpt-4.1", "gh/gpt-4.1-2025-04-14",
        "gh/gpt-4o", "gh/gpt-4o-2024-05-13", "gh/gpt-4o-2024-08-06",
        "gh/gpt-4o-2024-11-20",  # 429 transient only (gpt-4o base)
        "gh/gpt-4o-mini", "gh/gpt-4o-mini-2024-07-18",
        # EXCLUDED (verified failures): gpt-4, gpt-4-0613, gpt-5-mini,
        # gpt-5.4-mini-free-auto, gpt-5.6-luna-free-auto (400 not supported),
        # trajectory-compaction (503), exec-agent-c (empty body), mai-code-* (400),
        # copilot-search-*/exec-agent-*/gpt-3.5-turbo* (not chat-agentic / too weak)
    },
    "samba": {
        # 4 models in the sambanova.js registry (131K ctx each), verified live.
        # samba/MiniMax-M2.7 is EXCLUDED — it 404s / has no valid key on the
        # provider (connection status=error, no_credit) and is not in the registry.
        "samba/Meta-Llama-3.3-70B-Instruct",
        "samba/gpt-oss-120b",
        "samba/gemma-4-31B-it",
        "samba/DeepSeek-V3.1",
    },
    "kr": {
        # Kiro — RÁPIDO e confiável, melhor latência do pool. Verified 2026-09-03
        # via live tests 20128: minimax-m2.1 (1.25s), claude-haiku-4.5 (2.5s),
        # auto (1.6s). NOT include mortos/lentos: opus-5/sonnet-5/opus-4.8 (400),
        # qwen3-coder-next*/minimax-m2.5/deepseek-3.2/glm-5 (erros ou falhas).
        "kr/claude-haiku-4.5",
        "kr/minimax-m2.1",
        "kr/auto",
    },
    "ali": {
        # Alibaba Cloud MaaS (ali) — MUITO rápido e confiável, key sk-ws-* adicionada
        # 2026-09-03 (endpoint compatible-mode/v1). Verified 20128: qwen3.8-flash
        # (1.15s), qwen-plus (1.7s), qwen3.8-max (2.5s). Agora o provider mais rápido
        # do pool — ótimo p/ priorizar no combo.
        "ali/qwen-plus",
        "ali/qwen3.8-flash",
        "ali/qwen3.8-max",
    },
}

# Models that NO LONGER EXIST on their provider's API (verified against the
# live catalog) but keep resurfacing via stale 9router modelLocks / caches.
# api-airforce responds HTTP 200 with a fake "model does not exist" body for
# these — never a 404 — so health checks record them as success and they
# never enter cooldown on their own. Blocked here at the source instead.
DEAD_MODELS = {
    "af/anthropic/claude-3.7-sonnet",
    "af/moonshot/kimi-k2.6",  # api.airforce: 200 "model does not exist" (verified 2026-08-10)
    "nvidia/deepseek-ai/deepseek-v4-pro",  # subscription_level (paid) - verified
    "nvidia/moonshotai/kimi-k2.6",  # model_not_found (dead) - verified
    "nvidia/z-ai/glm-5.2",  # nvidia disabled, no free marker - verified
    "tokenrouter/moonshotai/kimi-k3-free",  # subscription_level (paid despite name) - verified
    "ag/gemini-pro-default",  # 404 NOT_FOUND (doesn't exist on antigravity) - verified 2026-08-10
    "kr/qwen3-coder-next-thinking-agentic",  # 402 credits via standard chat format - verified
    "kr/qwen3-coder-next-thinking",  # 400 REQUEST_BODY_INVALID via standard chat - verified
    "kr/qwen3-coder-next-agentic",  # 400 REQUEST_BODY_INVALID via standard chat - verified
    "gh/gpt-5-mini",  # 400 requested model not supported - verified
    "gh/trajectory-compaction",  # 503 upstream provider error (compaction endpoint, not chat) - verified
    "gh/mai-code-1-flash-tertiary",  # 400 requested model not supported - verified
    "gh/exec-agent-c",  # empty body (3 attempts: 2 JSON parse errors, 1 empty) - verified
    "nvidia/minimaxai/minimax-m3",  # 410 Gone (retired) - verified
    "nvidia/nemotron-3-ultra-550b-a55b",  # 404 page not found - verified
    "groq/meta-llama/llama-4-maverick-17b-128e-instruct",  # 404 model_not_found on groq API (listed in 9router catalog, doesn't exist) - verified 2026-08-10
    "groq/gpt-oss-120b",  # 404 model_not_found via 20128 direct test - verified 2026-08-14
    "groq/openai/gpt-oss-120b",  # rate_limit_rpm: 30 failures, permanently blocked by health daemon - verified 2026-08-26
    # NOTA 2026-09-03: kr/minimax-m2.1 removido daqui — re-verificado ao vivo hoje
    # (1.25s avg, resp 'ok' 2/2 na porta 20128). A entrada antiga (2026-08-14) ficou
    # obsoleta; o modelo é RÁPIDO e confiável e está no PROVIDER_ALLOWLIST do kr.
    "deepseek-v4-flash-free",  # 429 FreeUsageLimitError (rate limit) on EVERY request -> opencode retry loop, corruption. Verified 2026-08-27
    "opencode/deepseek-v4-flash-free",  # does not match substring below; same 429 loop ("AMD Radeon DeepSeek-V4-Flash"). Verified 2026-08-27
    "llm7/deepseek-v4-flash",  # model_not_supported -> 24h cooldown -> combo re-picks when expired -> loop. Verified 2026-08-27
    "cx/gpt-5",  # 400 "The 'gpt-5' model does not exist" (codex) — não existe no provider. Verified 2026-09-03
    "blockrun/moonshot/kimi-k3",  # 402 no_credit (sem crédito no openai-compatible-chat) — não se auto-cura. Verified 2026-09-03
    "groq/openai/gpt-oss-120b",  # resposta vazia (0.36s, content '') via 20128 — model morto no groq. Verified 2026-09-03
}


# Provider priority ranking (lower = preferred)
# Verified 2026-08-10 via live tests against 9router:20128
# Expanded 2026-08-26: added rw (498 free models), gemini, llm7, openrouter, any
PROVIDER_PRIORITY = {
    "ali": 5,        # Alibaba Cloud MaaS — MUITO rápido e confiável, provider mais rápido
    # do pool. Verified 2026-09-03 via 20128: qwen3.8-flash (1.15s), qwen-plus (1.7s),
    # qwen3.8-max (2.5s). Key sk-ws-* adicionada hoje. Benigno: modelos qwen em pt-br ok.
    "kr": 8,         # Kiro — modelos RÁPIDOS e confiáveis comprovados: claude-haiku-4.5 (1.8s), minimax-m2.1 (1.9s), auto (8.7s). Melhor latência do pool (verified 2026-09-03). Mortos (opus-5/sonnet-5) bloqueados.
    "ag": 18,        # Antigravity — MIX: gemini-3.7-flash-low (5.2s) bom, mas 3.1-pro-low (13.6s) e 3.7-flash-medium (12.6s) LENTOS, 3.5-flash morto. Rebaixado pra não dominar o ranking com modelos lentos (verified 2026-09-03).
    "cu": 20,        # Cursor — 3/3 models OK
    "rw": 22,        # Replicate/Runway — 498 free models, DeepSeek/Qwen/Llama/Gemma all 200 OK (verified 2026-08-26)
    "amd": 23,       # AMD Radeon — DeepSeek-V4-Flash (1M ctx), GLM-5.2, MinerU2.5-Pro free (verified 2026-08-26)
    "glm": 25,       # GLM Z.AI — glm-4.7 verified live 2026-08-11 (glm-5.2 model_not_found)
    "glm-cn": 28,    # GLM China — glm-5.1 verified live 2026-08-11 (glm-5.2 model_not_found)
    "cf": 30,        # Cloudflare — free, reliable (429 rate-limited right now)
    "openrouter": 31, # OpenRouter — 13 free models, reliable (verified 2026-08-26)
    "gemini": 32,    # Google Gemini — 6 free models, gemini-3.6-flash 200 OK (verified 2026-08-26)
    "samba": 35,     # SambaNova — 4 models in registry (allowlisted), key validada 2026-08-11 (3/4 modelos OK direto)
    "llm7": 37,      # LLM7 — 10 models, deepseek-v4-flash 200 OK (verified 2026-08-26)
    "any": 38,       # AnyAPI — 7 free nvidia models, free tier (verified 2026-08-26)
    "groq": 40,      # Groq — gpt-oss-120b OK, llama-3.3 429
    "gh": 45,        # GitHub Copilot — only gpt-4o-mini-2024-07-18 verified OK
    "cx": 55,        # Codex — gpt-5 morto (400), gpt-5.5 lento (~12s+). Rebaixado (verified 2026-09-03)
    "nvidia": 60,    # NVIDIA — 429/410 (minimax-m3 retired)
    "ollama": 75,    # Ollama Cloud — 429 weekly limit + gpt-oss:120b lento. Rebaixado (verified 2026-09-03)
    "kc": 100,       # Kilocode — no credits
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
    "weekly_limit",
    "daily_free_exhausted",
}

_STATIC_COMBOS = [
    # Cloudflare — free, reliable
    "cf/@cf/meta/llama-3.3-70b-instruct-fp8-fast",
    "cf/@cf/meta/llama-3.1-70b-instruct-fp8-fast",
    "cf/@cf/qwen/qwen2.5-coder-32b-instruct",
    # AMD Radeon — free, 1M context (DeepSeek-V4-Flash)
    "amd/DeepSeek-V4-Flash",
    "amd/GLM-5.2",
    "amd/MinerU2.5-Pro",
    # Groq — fast, free tier
    "groq/llama-3.3-70b-versatile",
    # Ollama Cloud — local fallback
    "ollama/gpt-oss:120b",
    # Replicate — 498 free models
    "rw/deepseek-ai/deepseek-v4-flash:free",
    "rw/google/gemma-4-31b-it:free",
    "rw/nvidia/nemotron-3-super-120b-a12b:free",
]

# Pool-size limits. Raised 2026-08-13: the 9router catalog holds ~1030 models
# (rw=479, cu=201, hf=132) but PER_PROVIDER_LIMIT=10 capped the biggest
# providers at 10 models each → only 122 of 1030 models ever reached the combo
# and ~50 were unusable (disabled/cooldown), leaving the pool exhausted while
# healthy providers sat under-used. 30/provider × ~25 providers fits under the
# 500 cap while keeping the ranked list bounded for per-request scoring.
PER_PROVIDER_LIMIT = 30
MAX_COMBO_MODELS = 500
NO_DATA_PENALTY = 30  # prefer tested+healthy providers over never-seen ones
# Minimum pool size worth persisting to the combo disk cache. A genuine
# /v1/models catalog fetch returns 300+ models; a timed-out fetch falls back
# to the existing (possibly tiny) disk cache. Persisting that fallback would
# overwrite a good cache with a 1-model list, so the daemon only writes when
# the pool is a real catalog (>= MIN_COMBO_WRITE).
MIN_COMBO_WRITE = 20
# Load-spread band: models within this many points of the best score are
# rotation candidates. Must cover the no-data tier (NO_DATA_PENALTY=30) so a
# top provider with base 30 (e.g. ollama=60) doesn't monopolize the combo —
# otherwise concurrent requests all pile onto it and burn its rate limit.
SPREAD_BAND = 30
_COMBO_CACHE_TTL = 24 * 3600  # disk cache validity (seconds)
_DISABLED_CACHE_TTL = 30  # seconds between 9router disabled-registry refreshes
# Locked-model / connection-count caches (from /api/providers modelLock_* and
# per-provider connection counts). Short TTL: locks expire over time and the
# pool must pick them back up quickly.
_LOCKED_MODELS_TTL = 60
_CONN_COUNTS_TTL = 60
# Score penalty for PERMANENTLY_BLOCKED providers that haven't proven
# themselves healthy since — keeps them as last-resort fallbacks instead of
# excluding them outright (a reloaded account should rejoin the pool).
FALLBACK_ONLY_PENALTY = 500


class SmartRouter:
    """Health-aware model selector with fallback chain.

    Takes a list of model IDs and returns the best available one.
    """

    def __init__(self, metrics_store: MetricsStore, usage_cache=None):
        self.metrics = metrics_store
        # Load-spreading state (thread-safe): tracks the last time each
        # provider was selected so concurrent requests don't all pile onto
        # the single top-1 model and burn its rate limit (thundering herd
        # → burst of 429s → cascading cooldowns → "provider temporarily
        # unavailable"). ThreadingHTTPServer serves requests in parallel.
        self._spread_lock = threading.Lock()
        self._last_selected: dict[str, float] = {}  # provider -> monotonic ts
        # Quota-aware rotation (todo 6): objeto com .get() -> dict[provider,
        # {"tokens","requests"}] do dia. None = feature desligada.
        self.usage_cache = usage_cache

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

        # If the caller tells us the prompt size, skip models whose REAL
        # context (from model_limits.json) can't fit it. The 9router catalog
        # inflates ctx (samba free: 128000 catalog vs 8192 real) → oversized
        # prompts 400 upstream → combo retry loop / empty compaction.
        prompt_tokens = 0
        if request_context:
            prompt_tokens = int(request_context.get("prompt_tokens") or 0)

        for model_id in model_ids:
            provider = model_id.split("/")[0] if "/" in model_id else model_id

            # 0. Prompt size vs model's real context window. Only filters
            # when the model has an EXPLICIT model_limits.json entry — the
            # catalog inflates ctx (samba free: 128000 catalog vs 8192 real)
            # → oversized prompts 400 upstream → combo retry loop / empty
            # compaction. Unknown ctx = no filter (respect catalog).
            if prompt_tokens > 0:
                try:
                    explicit = get_explicit_limits(model_id)
                except Exception:
                    explicit = None
                if explicit:
                    ctx = int(explicit.get("context") or 0)
                    if ctx > 0 and prompt_tokens > int(ctx * 0.75):
                        log.debug(
                            f"SKIP {model_id}: prompt {prompt_tokens} > ctx {ctx}"
                        )
                        continue

            # 1. Permanently blocked? → fallback-only unless the health
            # registry proves the account was reloaded (status healthy).
            # A reloaded account must rejoin the pool without code edits.
            fallback_only = provider in PERMANENTLY_BLOCKED
            if fallback_only and health_registry:
                get_provider = getattr(health_registry, "get_provider", None)
                if get_provider is not None:
                    entry = get_provider(provider)
                    if entry and entry.get("status") == "healthy":
                        fallback_only = False

            # 2. Health registry check
            if health_registry:
                if not health_registry.is_model_available(model_id):
                    log.debug(f"SKIP {model_id}: health registry says unavailable")
                    continue
                if not health_registry.is_provider_healthy(provider):
                    log.debug(f"SKIP {model_id}: provider {provider} unhealthy")
                    continue

            # 3. Metrics-based scoring — per-model stats first (a fast model
            # on a slow provider must not inherit the provider's latency),
            # falling back to provider-level stats when the model has no data.
            stats = None
            if self.metrics:
                stats = self.metrics.get_model_stats(
                    model_id, window_seconds=300
                )
                if stats is None:
                    stats = self.metrics.get_provider_stats(
                        provider, window_seconds=300
                    )
            score = self._compute_score(provider, stats)

            # 3b. Demote small models (parsed size) so the combo prefers
            # capable models while keeping small ones as last-resort fallbacks.
            size_penalty = SmartRouter._size_penalty(model_id)
            if size_penalty:
                score["size_penalty"] = size_penalty
                score["total"] = round(score["total"] + size_penalty, 1)

            # 4. Penalize models with recent failures in health registry
            # (probing after cooldown, or accumulating failures). Without this,
            # no_data providers outrank recently-burned models that just cleared
            # a short cooldown probe window.
            if health_registry:
                model_entry = health_registry.get_model(model_id)
                failures = (model_entry or {}).get("failures", 0)
                if failures > 0:
                    fail_penalty = min(failures * 50, 250)
                    score["health_fail_penalty"] = fail_penalty
                    score["total"] = round(score["total"] + fail_penalty, 1)

            # 4b. Fallback-only providers (PERMANENTLY_BLOCKED without a
            # healthy registry entry) sink to the bottom of the pool — they
            # stay eligible as last-resort fallbacks instead of being excluded.
            if fallback_only:
                score["fallback_only"] = True
                score["total"] = round(score["total"] + FALLBACK_ONLY_PENALTY, 1)

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

            # Latency penalty: +1 per second, tail-aware. avg hides outliers
            # (a 40s response among 3 requests → avg ~4.3s → tiny penalty);
            # p95 captures the worst requests (computed on the 5-min window).
            # We penalize the WORST of avg/p95 so one slow request sinks the
            # provider, and raise the cap 100 → 200 so genuinely slow
            # providers sort below fast ones instead of tying at the old
            # 10s-equivalent ceiling.
            avg_latency = stats.get("avg_latency_ms", 0) / 1000
            p95_latency = (stats.get("p95_latency_ms") or 0) / 1000
            latency = max(avg_latency, p95_latency) if p95_latency else avg_latency
            latency_penalty = min(latency * 10, 200)  # cap at 200
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
            components["no_data"] = True
            score += NO_DATA_PENALTY

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
        """Get a single best available model ID, with load spreading.

        Pure top-1 selection is a thundering-herd hazard: under a
        ThreadingHTTPServer, N concurrent requests all resolve to the same
        top-ranked model and simultaneously burn its rate limit (burst of
        429s, cascading cooldowns, then "provider temporarily unavailable").
        Instead, among the top candidates within a small score band we pick
        the provider least recently selected, so parallel requests spread
        across the healthy pool while still strongly preferring the best.
        """
        ranked = self.rank_models(model_ids, health_registry, request_context)
        if not ranked:
            return None

        best = ranked[0]
        if len(ranked) > 1:
            best = self._spread_select(ranked)

        log.info(
            "SmartRouter selected %s (provider=%s, score=%s)",
            best[0], best[1], best[2],
        )
        return best[0]

    def _spread_select(self, ranked: list[tuple[str, str, dict]]) -> tuple[str, str, dict]:
        """Pick among the top-ranked models, avoiding recently-used providers.

        Models within SPREAD_BAND of the best score are candidates; among
        them we prefer the provider that was selected longest ago (or never).
        Thread-safe: selection state is guarded so concurrent requests
        stagger across providers instead of collapsing onto top-1.
        """
        best_total = ranked[0][2].get("total", 0)
        band = [r for r in ranked if r[2].get("total", 0) <= best_total + SPREAD_BAND]
        if not band:
            band = [ranked[0]]

        with self._spread_lock:
            now = time.monotonic()
            # Quota-aware boost (todo 6): providers sem uso hoje ordenam antes
            # dos já usados; LRU continua decidindo dentro de cada tier. Boost
            # de ordenação apenas — nada é bloqueado ou penalizado.
            usage = {}
            if self.usage_cache is not None and QUOTA_AWARE_ROTATION:
                try:
                    usage = self.usage_cache.get() or {}
                except Exception as e:
                    log.debug("quota usage cache unavailable: %s", e)

            def _sort_key(r):
                lru = self._last_selected.get(r[1], 0.0)
                if usage:
                    used_today = 1 if usage.get(r[1], {}).get("requests", 0) > 0 else 0
                    return (used_today, lru)
                return (0, lru)

            # Least-recently-used provider first; never-used counts as oldest.
            band.sort(key=_sort_key)
            chosen = band[0]
            self._last_selected[chosen[1]] = now

        # Keep the log deterministic even though selection may spread.
        if len(band) > 1 and chosen is not band[0]:
            log.debug(
                "SmartRouter spread: top=%s, chose=%s (LRU provider)",
                band[0][0], chosen[0],
            )
        return chosen

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

    _disabled_cache: set[str] = set()
    _disabled_cache_time: float = 0.0

    _locked_models_cache: set[str] = set()
    _locked_models_cache_time: float = 0.0

    _conn_counts_cache: dict[str, int] = {}
    _conn_counts_cache_time: float = 0.0

    @staticmethod
    def _is_lock_active(until: str) -> bool:
        """True when a modelLock_* timestamp is still in the future (locked)."""
        if not until:
            return False
        try:
            locked_until = datetime.fromisoformat(until.replace("Z", "+00:00"))
            return locked_until > datetime.now(timezone.utc)
        except (ValueError, TypeError):
            return False

    @classmethod
    def _get_locked_model_ids(cls) -> set[str]:
        """Model ids with an ACTIVE modelLock_* (future timestamp) on any connection.

        The 9router /api/providers endpoint reports per-connection model locks
        as ``modelLock_<model>`` fields whose value is the release timestamp.
        A locked model is temporarily unusable (rate-limited / banned account)
        — treat it as dead for the pool until the lock expires.
        """
        now = time.time()
        if cls._locked_models_cache_time and now - cls._locked_models_cache_time < _LOCKED_MODELS_TTL:
            return cls._locked_models_cache

        locked: set[str] = set()
        try:
            from provider_discovery import fetch_provider_connections
            conns = fetch_provider_connections()
            for prefix, data in conns.items():
                for model_name, until in (data.get("model_locks") or {}).items():
                    if cls._is_lock_active(until):
                        locked.add(f"{prefix}/{model_name}")
        except Exception as e:
            log.warning(f"Locked-models fetch failed: {e}")
        cls._locked_models_cache = locked
        cls._locked_models_cache_time = now
        return locked

    @classmethod
    def _get_connection_counts(cls) -> dict[str, int]:
        """Per-provider connection counts (accounts) from /api/providers, cached.

        Used to weight pool contribution: a provider with more accounts has
        more parallel rate limits and can safely contribute more models.
        """
        now = time.time()
        if cls._conn_counts_cache_time and now - cls._conn_counts_cache_time < _CONN_COUNTS_TTL:
            return cls._conn_counts_cache

        counts: dict[str, int] = {}
        try:
            from provider_discovery import fetch_provider_connections
            conns = fetch_provider_connections()
            counts = {p: d.get("connection_count", 1) for p, d in conns.items()}
        except Exception as e:
            log.warning(f"Connection-count fetch failed: {e}")
        cls._conn_counts_cache = counts
        cls._conn_counts_cache_time = now
        return counts

    @classmethod
    def _get_disabled_model_ids(cls) -> set[str]:
        """Resolved set of model ids in the 9router disabled registry.

        The registry stores ids in two shapes: with the provider prefix
        (``groq/meta-llama/...``) or without it (``qwen/qwen3-32b`` under the
        ``groq`` key). Resolve both so catalog matches work either way.
        Cached for _DISABLED_CACHE_TTL; empty set on any failure (safe no-op).
        """
        now = time.time()
        if cls._disabled_cache_time and now - cls._disabled_cache_time < _DISABLED_CACHE_TTL:
            return cls._disabled_cache

        try:
            from catalog_sync import get_disabled_models
        except ImportError:  # pragma: no cover - defensive
            return cls._disabled_cache

        resolved: set[str] = set()
        try:
            raw = get_disabled_models()
        except Exception as e:
            log.warning(f"Disabled-registry fetch failed: {e}")
            cls._disabled_cache_time = now  # avoid hammering on failure
            return cls._disabled_cache

        if isinstance(raw, dict):
            for provider, ids in raw.items():
                if not isinstance(ids, list):
                    continue
                for mid in ids:
                    if not isinstance(mid, str) or not mid:
                        continue
                    resolved.add(mid)
                    if not mid.startswith(provider + "/"):
                        resolved.add(f"{provider}/{mid}")

        cls._disabled_cache = resolved
        cls._disabled_cache_time = now
        return resolved

    @classmethod
    def _filter_disabled_models(cls, model_ids: list[str]) -> list[str]:
        """Drop models present in the 9router disabled registry."""
        if not model_ids:
            return model_ids
        disabled = cls._get_disabled_model_ids()
        if not disabled:
            return model_ids
        filtered = [m for m in model_ids if m not in disabled]
        if len(filtered) != len(model_ids):
            dropped = [m for m in model_ids if m in disabled]
            log.info(f"SmartRouter: dropped {len(dropped)} disabled models from combo list")
        return filtered

    @classmethod
    def get_default_combos(cls, skip_disabled: bool = False) -> list[str]:
        """Real combo candidates: in-memory → catalog → disk cache → static.

        skip_disabled: when True, skip the 9router disabled registry. Use when
        the filtered pool is empty but the live catalog still has healthy models
        (the disabled registry can be over-aggressive from past false positives).
        """
        now = time.time()
        if cls._combo_cache and now - cls._combo_cache_time < COMBO_REFRESH_INTERVAL:
            if not skip_disabled:
                return cls._combo_cache

        # Fast-path (cascade-fix 2026-08-31): if a fresh disk cache exists,
        # use it immediately instead of waiting up to CATALOG_TIMEOUT on the
        # slow /v1/models fetch. Only hit the network when the cache is stale
        # or absent. This unblocks combo requests that previously hung 5-15s
        # waiting for the catalog fetch to time out.
        disk = cls._read_combo_cache()
        if disk and not skip_disabled:
            disk = cls._filter_static_models(disk)
            disk = cls._filter_disabled_models(disk)
            if disk:
                cls._combo_cache = disk
                cls._combo_cache_time = now
                return disk

        models = cls._fetch_catalog_models()
        if models:
            models = cls._filter_static_models(models)
            if not skip_disabled:
                models = cls._filter_disabled_models(models)
            if not skip_disabled:
                cls._write_combo_cache(models)
        else:
            models = cls._read_combo_cache()
            if not models:
                log.warning("9router catalog unavailable; using static combo list")
                models = list(_STATIC_COMBOS)
            models = cls._filter_static_models(models)
            if not skip_disabled:
                models = cls._filter_disabled_models(models)
        if not skip_disabled:
            cls._combo_cache = models
            cls._combo_cache_time = now
        return models

    @classmethod
    def invalidate_combo_cache(cls) -> None:
        """Drop the in-memory combo cache so the next call refetches.

        Used by catalog sync: after a model is disabled upstream, the cached
        combo list may still reference it — force a fresh fetch.
        """
        cls._combo_cache = []
        cls._combo_cache_time = 0.0
        cls._disabled_cache = set()
        cls._disabled_cache_time = 0.0
        cls._locked_models_cache = set()
        cls._locked_models_cache_time = 0.0
        cls._conn_counts_cache = {}
        cls._conn_counts_cache_time = 0.0

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
    def _is_non_chat_model(model_id: str) -> bool:
        """True if the last path segment contains a NON_CHAT_MODEL_TOKENS token."""
        if not isinstance(model_id, str):
            return True
        segment = model_id.rstrip("/").split("/")[-1]
        tokens = {t.strip() for t in re.split(r"[-_:.]", segment) if t.strip()}
        return bool(tokens & NON_CHAT_MODEL_TOKENS)

    @staticmethod
    def _estimate_size_billions(model_id: str) -> Optional[float]:
        """Largest `Nb` marker in the model id, or None if unparseable.

        Handles MoE ids where the last number is the *active* params
        (``nemotron-3-nano-30b-a3b`` -> 30 total / 3 active): taking the max
        gives the total, which is what capability ranking wants.
        """
        if not isinstance(model_id, str):
            return None
        sizes = [float(m) for m in _SIZE_B_RE.findall(model_id)]
        return max(sizes) if sizes else None

    @staticmethod
    def _is_tiny_model(model_id: str) -> bool:
        """True if the parsed model size is below TINY_MODEL_MAX_B."""
        size = SmartRouter._estimate_size_billions(model_id)
        return size is not None and size < TINY_MODEL_MAX_B

    @staticmethod
    def _size_penalty(model_id: str) -> float:
        """Ranking penalty for small models; unparseable sizes get none."""
        size = SmartRouter._estimate_size_billions(model_id)
        if size is None:
            return 0.0
        for min_b, penalty in SIZE_PENALTY_TIERS:
            if size >= min_b:
                return float(penalty)
        return float(SIZE_PENALTY_TIERS[-1][1])

    @staticmethod
    def _filter_static_models(model_ids: list[str]) -> list[str]:
        """Drop @-prefixed/bare ids and dead/locked models from cached lists.

        PERMANENTLY_BLOCKED providers are intentionally KEPT: they become
        last-resort fallbacks (rank_models applies FALLBACK_ONLY_PENALTY) and
        automatically rejoin the ranking if the health registry later proves
        the account was reloaded — no code edits needed.
        """
        out: list[str] = []
        for mid in model_ids:
            if not isinstance(mid, str) or "/" not in mid:
                continue
            if mid in DEAD_MODELS:
                continue
            if any(blk in mid for blk in BLOCKED_MODEL_SUBSTRINGS):
                continue
            provider = mid.split("/")[0]
            if provider.startswith("@"):
                continue
            if mid in SmartRouter._get_locked_model_ids():
                continue  # actively locked by modelLock_* (rate-limited account)
            if provider in FREE_TIER_SUFFIX_ONLY and ":free" not in mid:
                continue  # paid tier of a free-tier-only provider
            if provider in FREE_TIER_AUTO_SUFFIX_ONLY and "-free-auto" not in mid:
                continue  # paid tier of a -free-auto-only provider (gh)
            allow = PROVIDER_ALLOWLIST.get(provider)
            if allow is not None and mid not in allow:
                continue  # not on the verified allowlist for this provider
            if SmartRouter._is_non_chat_model(mid):
                continue
            if SmartRouter._is_tiny_model(mid):
                continue  # parsed size below TINY_MODEL_MAX_B (e.g. real 2B runts)
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
            with urllib.request.urlopen(req, timeout=CATALOG_TIMEOUT) as resp:
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
        """Keep real models — bounded, priority-ordered, connection-weighted.

        PERMANENTLY_BLOCKED providers are intentionally KEPT: they become
        last-resort fallbacks (rank_models applies FALLBACK_ONLY_PENALTY) and
        automatically rejoin the ranking if the health registry later proves
        the account was reloaded — no code edits needed.
        """
        per_provider: dict[str, list[tuple[int, str]]] = {}
        for it in items:
            if not isinstance(it, dict):
                continue
            model_id = it.get("id")
            if not isinstance(model_id, str) or "/" not in model_id:
                continue  # bare IDs (combos) carry no provider
            if model_id in DEAD_MODELS:
                continue
            if any(blk in model_id for blk in BLOCKED_MODEL_SUBSTRINGS):
                continue
            provider = model_id.split("/")[0]
            if provider.startswith("@"):
                continue
            if model_id in SmartRouter._get_locked_model_ids():
                continue  # actively locked by modelLock_* (rate-limited account)
            if provider in FREE_TIER_SUFFIX_ONLY and ":free" not in model_id:
                continue  # paid tier of a free-tier-only provider
            if provider in FREE_TIER_AUTO_SUFFIX_ONLY and "-free-auto" not in model_id:
                continue  # paid tier of a -free-auto-only provider (gh)
            allow = PROVIDER_ALLOWLIST.get(provider)
            if allow is not None and model_id not in allow:
                continue  # not on the verified allowlist for this provider
            if SmartRouter._is_non_chat_model(model_id):
                continue
            if SmartRouter._is_tiny_model(model_id):
                continue  # parsed size below TINY_MODEL_MAX_B (e.g. real 2B runts)
            caps = it.get("capabilities") or {}
            context = caps.get("contextWindow") or 0
            per_provider.setdefault(provider, []).append((context, model_id))

        conn_counts = SmartRouter._get_connection_counts()
        ranked: list[tuple[int, str]] = []
        for provider, models in per_provider.items():
            models.sort(reverse=True)  # larger context first
            priority = PROVIDER_PRIORITY.get(provider, 50)
            # Providers with more accounts have more parallel rate limits and
            # can safely contribute more models to the pool.
            extra = max(0, (conn_counts.get(provider, 1) - 1) * 5)
            limit = min(PER_PROVIDER_LIMIT + extra, MAX_COMBO_MODELS)
            ranked.extend((priority, m) for _, m in models[:limit])
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
