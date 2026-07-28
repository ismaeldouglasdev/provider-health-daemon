---
slug: meta-router
status: approved
intent: clear
pending-action: write .omo/plans/meta-router.md
decisions-confirmed:
  - deduplicated_catalog: yes
  - round_robin_fallback: yes
  - aggregate_providers: yes
  - tdd: yes
approach: Transform provider-health-daemon into a router-of-routers by adding a router registry, health probe manager, and meta-router selection logic that rotates between 9router (:20131), OmniRoute (:20128), Kiro (:20129) and future downstreams.
---

# Draft: meta-router

## Components (topology ledger)
| id | outcome | status | evidence path |
|---|---|---|---|
| 1. Router Registry | Manages list of downstream routers, their health state, model catalogs, cooldown state | active | Design: `router_registry.py` (NEW) - data class per router: url, name, health_status, models[], cooldown_until, priority, weight |
| 2. Health Probe Manager | Periodically pings each router's /v1/models or /health, updates registry, triggers cooldown | active | Design: `router_probe.py` (NEW) - threading loop similar to daemon.py's `_health_probe_loop()` |
| 3. Meta-Router Selector | Chooses which downstream router to use for each request based on health, round-robin, fallback | active | Design: `meta_router.py` (NEW) - `select_router()`, `route_request()`, fallback chain |
| 4. Unified Model Catalog | Merges model lists from all healthy routers into one catalog | active | Design: `model_catalog.py` (NEW) - `/v1/models` endpoint returns unified list |
| 5. Config | Router definitions (URLs, priorities, timeouts, weights) | active | Modify `config.py` - add `DOWNSTREAM_ROUTERS` config list |
| 6. Proxy Handler | Routes incoming requests through meta-router selection | active | Modify `proxy_handler.py` - replace single NINEROUTER_URL with meta-router selection |
| 7. Dashboard | Shows router-level health + drill-down to provider health per router | active | Extend `dashboard.py` - add router health view alongside provider view |
| 8. Smart Router | Router-level strategy (which downstream, not which provider within a router) | active | Modify `smart_router.py` - at top level, select router; each router handles its own internal routing |

## Open assumptions (announced defaults)
| assumption | adopted default | rationale | reversible? |
|---|---|---|---|
| Downstream routers are stateless for routing decisions | Each router manages its own health/routing internally; meta-router only cares if it responds | OmniRoute already has 19 strategies internally; we layer above | Yes - could add router-specific routing hints later |
| Health = responds to /v1/models within timeout | Simple GET to /v1/models on each router; also try /health | All routers expose /v1/models in same schema; /health is optional | Yes - could add custom health check paths per router |
| Round-robin across healthy routers with priority tiers | Primary/secondary/tertiary tiers; round-robin within tier | Simplest approach; spread load while respecting preference | Yes - weighted, least-used, or other strategy |
| Model dedup by model ID string | Same model ID from multiple routers = one entry in catalog | Models are identified by same string across routers | Yes - could add origin tracking per model |

## Findings (cited)
- `config.py`:5-9 — NINEROUTER_URL = localhost:20128 (OmniRoute), HEALTH_PROXY_PORT=20131, DASHBOARD_PORT=20132
- `daemon.py`:93-108 — health probe loop fetches NINEROUTER_URL/api/providers every 10s
- `daemon.py`:112-122 — `/v1/models` endpoint returns models from `smart_router.list_all_models()` which NINEROUTER_URL/models
- `proxy_handler.py`:151-200 — `proxy_request()` POSTs to NINEROUTER_URL/v1/chat/completions with health-aware routing
- `proxy_handler.py`:45-70 — `handle_cooldown()` returns 503 when all providers are in cooldown
- `smart_router.py`:1-60 — 23 providers registered in health registry; combo-round-robin and main-rr are healthy; 11 in cooldown; 10 in probing; 0 models
- `smart_router.py`:70-120 — PERMANENTLY_BLOCKED set: anthropic, kc, cx, cl, ag, kr, bpm (no auth/credits for >3 days)
- `health_registry.py`:30-60 — providers dict with status, failures, cooldown_until, models[]
- Topology: :20128=OmniRoute (Next.js TypeScript 6.0), :20129=Kiro (Python), :20131=9router (OmniRoute, Next.js), :20132=provider-health-daemon (Python/Flask)
- OmniRoute GitHub: 33K stars, MIT, 19 routing strategies, 290+ providers
- All routers expose OpenAI-compatible /v1/chat/completions endpoint

## Decisions (with rationale)
1. **New files, not modifications only**: Adding 4 new Python modules (router_registry, router_probe, meta_router, model_catalog) keeps concerns separated and the existing health/provider logic intact
2. **Router-level round-robin + fallback chain**: All healthy routers get equal turns. On failure, retry next router. On all fail, return 503. Confirmed by user.
3. **Router health ≠ provider health**: A router can be healthy (responds to /v1/models) even if its providers are in cooldown. Meta-router tracks both layers independently
4. **No changes to downstream routers**: 9router/OmniRoute/Kiro run as-is. Meta-router only queries their public endpoints
5. **Config-driven discovery**: Router list in config.py; each router has name, url, priority, health_check_path, timeout, weight
6. **Deduplicated model catalog**: Same model ID from multiple routers → one entry in `/v1/models`. Confirmed by user.
7. **Aggregated /api/providers**: Shows providers from ALL routers, tagged with origin router. Confirmed by user.
8. **TDD**: Write tests first for router selection logic. Confirmed by user.

## Scope IN
- Router registry data structure and health tracking per router
- Periodic health probing of each downstream router (GET /v1/models)
- Unified model catalog endpoint (`/v1/models` returning merged list from all healthy routers)
- Meta-router selection logic (select downstream router per request)
- Request proxying through selected router with fallback chain
- Dashboard showing router-level health + per-router provider drill-down
- Config for router definitions (list of downstream routers)
- CLI command or script to add/remove routers dynamically

## Scope OUT (Must NOT have)
- Changes to downstream routers' code (they run as-is)
- Implementation of OmniRoute's 19 routing strategies internally (those stay downstream)
- Provider-level health tracking per router (each router already does this)
- Non-HTTP protocols (no gRPC, no WebSocket)
- Changes to downstream routers' code (they run as-is)
- Implementation of OmniRoute's 19 routing strategies internally (those stay downstream)
- Provider-level health tracking per router (each router already does this)
- Non-HTTP protocols (no gRPC, no WebSocket)
- Rate-limit tracking per-router (the meta-router relies on the error responses from routers to trigger cooldown)

## Approval gate
status: approved
<!-- All 4 decisions confirmed by user on 2026-07-28. -->
