# meta-router - Work Plan

## TL;DR (For humans)

**What you'll get:** O provider-health-daemon (`:20132`) vira uma meta-router que distribui requisições entre 3 roteadores (9router `:20131`, OmniRoute `:20128`, Kiro `:20129`) em round-robin. Se um cai, o próximo assume. O catálogo de modelos é unificado — mesmo modelo em 2 roteadores aparece uma vez só. O dashboard mostra a saúde de cada roteador com cards coloridos. **Além disso, as respostas de TODOS os modelos/roteadores são normalizadas** para um formato consistente — igual ao padrão "Big Pickle / OpenCode Zen" — com schema de resposta unificado, erros padronizados, IDs de modelo canônicos e chunks de streaming idênticos.

**Why this approach:** O daemon já tem 70% do que precisa (proxy health-aware, cooldown, métricas, dashboard). Em vez de reescrever, a gente adiciona 5 módulos novos que se encaixam na estrutura existente. O normalizador de resposta garante que o cliente (OpenCode, Claude Code, Cursor) veja o MESMO formato independente de qual roteador/modelo atendeu a requisição.

**What it will NOT do:** Não mexe nos roteadores downstream (cada um roda como está). Não reimplementa as 19 estratégias do OmniRoute. Não rastreia providers individualmente por roteador (cada roteador já faz isso). Não adiciona protocolos não-HTTP.

**Effort:** Large (15 todos, 6 waves)
**Risk:** Medium — integração com 3 sistemas existentes, cada um com auth e comportamento diferentes; normalização de streaming SSE precisa preservar chunks em tempo real
**Decisions to sanity-check:** (1) Round-robin entre roteadores saudáveis + fallback chain, (2) Catálogo deduplicado, (3) /api/providers agregado com origem, (4) TDD com pytest, (5) **Response normalizer com Big Pickle / OpenCode Zen**

Your next move: **start work**, or run a high-accuracy (dual Momus) review first? Full execution detail follows below.

---

> TL;DR (machine): Large effort, medium risk. Transform provider-health-daemon into a router-of-routers: 4 new Python modules (router_registry, router_probe, model_catalog, meta_router), 5 modified files, 13 todos across 5 waves. Round-robin across 3 downstream routers with fallback, deduplicated model catalog, aggregated provider listing, dashboard extension. TDD with pytest.

## Scope
### Must have
- Router registry: track downstream routers (9router:20131, OmniRoute:20128, Kiro:20129) with health state, cooldown, model list, per-router auth
- Health probe loop: periodic GET /v1/models on each router (every 30s, 2s timeout)
- Unified model catalog: deduplicated `/v1/models` merging from all healthy routers
- Router-of-routers selection: round-robin across healthy routers + fallback chain on failure
- Proxy passthrough: existing full-read behavior preserved, now through selected router
- **Response normalizer**: transform ALL router/model outputs to a consistent "Big Pickle / OpenCode Zen" format — schema unificado, erros padronizados, IDs canônicos, streaming uniforme
- Aggregated `/api/providers`: providers from all routers with origin tags
- Dashboard extension: router health panel + per-router provider drill-down
- All-503 handling: when no router is healthy, return `503 {error:"all routers unavailable"}`
- Per-router auth: store credentials per router, pass in probe + proxy headers
- Validation: probe timeout, JSON parse safety, startup config validation

### Must NOT have (guardrails, anti-slop, scope boundaries)
- Do NOT modify downstream routers (9router, OmniRoute, Kiro run as-is)
- Do NOT implement OmniRoute's 19 routing strategies internally (those stay downstream)
- Do NOT track provider-level health per router (each router already does this)
- Do NOT add non-HTTP protocols (no gRPC, no WebSocket)
- Do NOT create request loops: meta-router must NOT forward to itself
- Do NOT break existing client compatibility — normalized output must remain valid OpenAI chat completions JSON

## Verification strategy
> Zero human intervention - all verification is agent-executed.
- Test decision: TDD + pytest (existing project has no test dir yet; create `tests/`)
- Framework: pytest + pytest-mock + requests_mock for HTTP mocking
- Evidence: `.omo/evidence/task-<N>-meta-router.txt` — capture `pytest -v` output per todo
- Coverage target: all new modules ≥ 90% line coverage; integration tests exercise the full request path
- Concurrency: health probe tests verify thread-safety with threading primitives, not sleeps

## Execution strategy
### Parallel execution waves
> Target 5-8 todos per wave. Fewer than 3 (except the final) means you under-split.
- **Wave 1: Foundation** — config, router registry, test infra (4 todos)
- **Wave 2: Discovery** — health probes, model catalog, tests (4 todos)
- **Wave 3: Routing** — meta-router selector, proxy integration, smart-router cleanup (4 todos)
- **Wave 4: Normalization** — response normalizer (Big Pickle / OpenCode Zen format) (2 todos)
- **Wave 5: Visibility** — dashboard, provider aggregation, integration tests (3 todos)
- **Wave 6: Hardening** — XSS sanitization, edge cases, final QA (3 todos)

### Dependency matrix
| Todo | Depends on | Blocks | Can parallelize with |
|------|-----------|--------|---------------------|
| 1. Config + Test infra | — | 2,3 | — |
| 2. Router Registry + Tests | 1 | 4,5,7,9,11 | — |
| 3. Model Catalog + Tests | 1 | 5 | — |
| 4. Router Probe + Tests | 2 | 5 | 3 |
| 5. Model Catalog probe integration | 3,4 | 7 | — |
| 6. Meta-Router Selector + Tests | 2,5 | 7 | — |
| 7. Proxy integration | 6 | 10 | 8 |
| 8. Smart router cleanup + Tests | 2 | 10 | 7 |
| 9. Dashboard router panel | 2 | 12 | 7,8 |
| **10. Response Normalizer + Tests** | **7** | **12,13** | **11** |
| **11. Model ID mapper + Tests** | **7** | **12,13** | **10** |
| 12. Aggregated /api/providers | 3,9,10,11 | 14 | — |
| 13. Integration test suite | 10,11 | 14 | — |
| 14. Security sanitization | 12,13 | 15 | — |
| 15. Edge case hardening | 14 | Final | — |

## Todos
> Implementation + Test = ONE todo. Never separate.
<!-- APPEND TASK BATCHES BELOW THIS LINE - never rewrite the headers above. -->

<!-- ═══════════════ WAVE 1: FOUNDATION ═══════════════ -->
- [ ] 1. **Config.py rewrite + test infrastructure**
  What to do / Must NOT do:
    - Add `DOWNSTREAM_ROUTERS` list to `config.py` — each entry: name, url, priority (int, lower=first), health_check_path (`/v1/models` default), timeout (float, default 2.0), weight (int, default 1), auth (dict or None — keys: header, value)
    - Default config for the 3 known routers:
      - 9router: url=http://localhost:20131, priority=1, weight=2
      - OmniRoute: url=http://localhost:20128, priority=1, weight=1
      - Kiro: url=http://localhost:20129, priority=2, weight=1, auth={header:"X-API-Key", value:env("KRI_KEY","")}
    - Add `MAX_MODEL_CATALOG = 500` — cap on catalog size after dedup
    - Add `PROBER_INTERVAL_SECONDS = 30` — replacing PROBER_INTERVAL_MINUTES
    - Add `PROBE_TIMEOUT = 2.0` — seconds per health check request
    - Add `PROBE_MAX_WORKERS = 5` — thread pool size for parallel probes
    - Create `tests/` directory with `conftest.py` (fixtures: mock_router, mock_registry, app client)
    - Create `tests/__init__.py`
    - Add `ROUTER_STATE_FILE = Path.home() / ".9router" / "router_state.json"` — path for persisting router health state across restarts
    - Must NOT change existing backwards-incompatible env var names (keep HEALTH_PROXY_PORT, DASHBOARD_PORT, NINEROUTER_URL for backward compat)
    - Must NOT remove PROBER_INTERVAL_MINUTES until all existing references are migrated
  Parallelization: Wave 1 | Blocked by: — | Blocks: 2,3
  References:
    - `config.py`:1-32 — current config structure and env vars
    - `daemon.py`:1-50 — existing imports from config
    - `proxy_handler.py`:1-20 — existing imports from config
    - pytest docs: https://docs.pytest.org/en/stable/
  Acceptance criteria (agent-executable):
    ```bash
    python3 -c "import sys; sys.path.insert(0,'.'); from config import DOWNSTREAM_ROUTERS, MAX_MODEL_CATALOG, PROBER_INTERVAL_SECONDS, PROBE_TIMEOUT; print(f'OK: {len(DOWNSTREAM_ROUTERS)} routers, catalog={MAX_MODEL_CATALOG}, interval={PROBER_INTERVAL_SECONDS}s, timeout={PROBE_TIMEOUT}s')"
    ```
    ```bash
    # Test infrastructure: conftest fixture loads
    python3 -c "import sys; sys.path.insert(0,'.'); sys.path.insert(0,'tests'); from conftest import *; print('OK: fixtures loaded')"
    ```
  QA scenarios:
    - **Happy**: Import config, verify all 4 new constants exist and have correct types
    - **Failure**: Set env var `KRI_KEY=test123`, assert router[2].auth.value == "test123"
    Evidence: `.omo/evidence/task-1-meta-router.txt` (capture of both commands)
  Commit: Y | `feat(config): add DOWNSTREAM_ROUTERS and test infra for meta-router`

- [ ] 2. **Router Registry — data model + health state management**
  What to do / Must NOT do:
    - Create `router_registry.py` with `RouterState` dataclass: name, url, priority, weight, auth (dict|None), health_status (healthy|cooldown|probing|unknown), models (list[str]), last_success (float|None), last_failure (float|None), failure_count (int), cooldown_until (str|None), model_cache (list[str] — last known good model list, retained when probe fails with rate-limit), consecutive_probes_ok (int — required >=2 to promote from probing→healthy)
    - Create `RouterRegistry` class with:
      - `__init__(routers_config: list[dict])` — builds router states from config
      - `get_healthy_routers() -> list[RouterState]` — returns routers with status=healthy, sorted by priority then weight descending
      - `get_all_routers() -> list[RouterState]`
      - `mark_healthy(name: str, models: list[str])` — sets status=healthy, resets failure_count, sets last_success, consecutive_probes_ok+=1. Only promotes from probing→healthy when consecutive_probes_ok >= 2
      - `mark_unhealthy(name: str, reason: str, is_permanent: bool = False)` — sets status=cooldown, increments failure_count, calculates cooldown_until via CooldownCalculator, reset consecutive_probes_ok=0. After `MAX_CONSECUTIVE_COOLDOWNS` consecutive cooldowns (configurable, default=5), sets status=disabled
      - `mark_probing(name: str)` — sets status=probing, keeps model_cache
      - `mark_disabled(name: str)` — permanent manual disable
      - `get_model_catalog() -> set[str]` — deduplicated union of models from all healthy routers
      - `get_router_for_model(model_id: str) -> RouterState|None` — returns the highest-priority router that has this model
      - `refresh_models_from_router(name: str, model_list: list[str])` — updates models for a router; if new models differ from previous, logs change
      - `get_provider_aggregation() -> dict` — builds {provider_name: {origins: [router_names], models: [...]}} across all routers
      - `persist(path: Path)` — serializes registry state to JSON
      - `load(path: Path)` — deserializes from JSON
      - Concurrency: use `threading.Lock` around all state mutations
    - Per-router auth: store as `{"header": "X-API-Key", "value": "..."}` per router; `get_auth_headers(name)` returns dict for HTTP requests
    - Must NOT expose non-thread-safe iterators; all iteration over router states must happen inside the lock
  Parallelization: Wave 1 | Blocked by: 1 | Blocks: 4,5,7,9,11
  References:
    - `health_registry.py`:1-80 — existing provider health state pattern (status, cooldown, failures, models[])
    - `cooldown.py`:7-130 — CooldownCalculator for exponential backoff
    - `config.py`:5-32 — DOWNSTREAM_ROUTERS structure
  Acceptance criteria (agent-executable):
    ```bash
    python3 -c "
    import sys; sys.path.insert(0,'.');
    from router_registry import RouterRegistry, RouterState;
    config = [{'name':'r1','url':'http://a:1','priority':1,'weight':2},{'name':'r2','url':'http://b:2','priority':2,'weight':1}];
    reg = RouterRegistry(config);
    assert len(reg.get_all_routers()) == 2;
    reg.mark_healthy('r1', ['gpt-4','claude-3']);
    assert 'gpt-4' in reg.get_model_catalog();
    assert reg.get_healthy_routers()[0].name == 'r1';
    print('OK: registry CRUD works')
    "
    ```
    ```bash
    # Test files exist
    test -f router_registry.py && echo "OK: file exists"
    ```
  QA scenarios:
    - **Happy**: Create registry with 2 routers, mark one healthy, verify get_healthy_routers returns it
    - **Failure**: Mark router unhealthy 6 times (exceeding MAX_CONSECUTIVE_COOLDOWNS default 5), verify status becomes disabled
    - **Thread safety**: Spawn 10 threads calling mark_healthy/mark_unhealthy simultaneously, verify no race conditions or corrupted state
    Evidence: `.omo/evidence/task-2-meta-router.txt`
  Commit: Y | `feat(router_registry): add RouterState dataclass and RouterRegistry for multi-router health`

- [ ] 3. **Model Catalog — unified deduplicated model listing**
  What to do / Must NOT do:
    - Create `model_catalog.py` with `ModelCatalog` class:
      - `__init__(router_registry: RouterRegistry)` — takes registry reference
      - `get_models() -> list[dict]` — returns deduplicated list of {id, object, created, owned_by, router_origins: [list of router names]}
        - Dedup logic: same model ID from multiple routers → one entry with `router_origins` array
        - Cap at `MAX_MODEL_CATALOG` entries (trim by router priority after dedup)
      - `get_models_by_router(router_name: str) -> list[str]` — models available on a specific router
      - `refresh_from_registry()` — rebuilds internal cache from registry state
      - `/v1/models` response format must match OpenAI schema: `{"object":"list","data":[{id,object,created,owned_by,...}]}`
    - Sanitize: strip any HTML from model `id` and `owned_by` fields (use `html.escape()` or strip `<`/`>` chars)
    - Must NOT return duplicate model IDs even if 3 routers all have the same model
  Parallelization: Wave 1 | Blocked by: 1 | Blocks: 5
  References:
    - OmniRoute `/v1/models` response format (observed from :20131 probe)
    - OpenAI model list API schema
    - `router_registry.py` — `get_model_catalog()` + `get_provider_aggregation()`
  Acceptance criteria (agent-executable):
    ```bash
    python3 -c "
    import sys; sys.path.insert(0,'.');
    from router_registry import RouterRegistry;
    from model_catalog import ModelCatalog;
    cfg=[{'name':'r1','url':'http://a:1','priority':1}];
    reg=RouterRegistry(cfg);
    reg.mark_healthy('r1',['gpt-4','claude-3','gpt-4']);
    cat=ModelCatalog(reg);
    models=cat.get_models();
    assert len([m for m in models['data']]) == 2;  # dedup gpt-4
    print(f'OK: {len(models[\"data\"])} models (deduped)')
    "
    ```
  QA scenarios:
    - **Happy**: 2 routers with 5 models each, 2 overlap → catalog has 8 entries, each with correct router_origins
    - **Failure**: 3 routers all have same model → catalog has 1 entry with 3 origins, no duplicate
    - **Sanitization**: Model ID `<script>alert('xss')</script>` → stripped to `alert('xss')script`
    Evidence: `.omo/evidence/task-3-meta-router.txt`
  Commit: Y | `feat(model_catalog): add deduplicated unified model catalog with origin tracking`

<!-- ═══════════════ WAVE 2: DISCOVERY ═══════════════ -->
- [ ] 4. **Health Probe Manager — periodic router health checks**
  What to do / Must NOT do:
    - Create `router_probe.py` with `RouterProbe` class:
      - `__init__(router_registry: RouterRegistry, interval: int = 30)` — takes registry ref
      - `start()` — launches background daemon thread that runs `_probe_loop()`
      - `stop()` — signals thread to stop via threading.Event
      - `_probe_loop()` — every `interval` seconds, probes ALL routers in parallel:
        - Uses `ThreadPoolExecutor(max_workers=PROBE_MAX_WORKERS)` for parallel probes
        - For each router: GET `{url}{health_check_path}` (default `/v1/models`)
        - Timeout: `PROBE_TIMEOUT` seconds per request (default 2.0)
        - On success (HTTP 200 + valid JSON list):
          - Calls `registry.mark_healthy(name, model_list)`
          - Updates model list via `registry.refresh_models_from_router()`
        - On timeout/connection error/bad JSON:
          - Calls `registry.mark_unhealthy(name, reason)` for transient error
          - But if router has a non-empty model_cache from previous successful probe, retain it as fallback catalog instead of empty
        - On HTTP 429: calls `mark_unhealthy` with rate_limit reason (short cooldown)
        - On HTTP 5xx consecutive (< 3): calls `mark_unhealthy` with transient reason (0.5h cooldown)
        - On HTTP 5xx consecutive (>= 3): calls `mark_unhealthy` with permanent_recheck reason (1h cooldown + recheck flag)
        - On HTTP 401/403: calls `mark_unhealthy` with auth error, permanent
      - `is_running() -> bool` — probe loop health check
      - `probe_now()` — trigger immediate probe cycle (for admin/debug)
    - Use `urllib.request` (stdlib) with custom timeout — no external HTTP dependency
    - Must NOT block Flask request handling — probe runs in daemon thread
    - Must NOT let one router's timeout delay other routers' probes (parallel execution)
  Parallelization: Wave 2 | Blocked by: 2 | Blocks: 5
  References:
    - `daemon.py`:93-108 — existing `_health_probe_loop()` as pattern reference
    - `cooldown.py`:7-130 — CooldownCalculator for determining cooldown duration
    - `error_parser.py`:105-158 — existing error parsing logic
    - Python `concurrent.futures.ThreadPoolExecutor` docs
  Acceptance criteria (agent-executable):
    ```bash
    python3 -c "
    import sys; sys.path.insert(0,'.');
    from router_registry import RouterRegistry;
    from router_probe import RouterProbe;
    import time;
    cfg=[{'name':'local','url':'http://localhost:20131','priority':1,'health_check_path':'/v1/models','timeout':2.0}];
    reg=RouterRegistry(cfg);
    probe=RouterProbe(reg,interval=3600);  # long interval to not spam
    probe.start();
    assert probe.is_running();
    probe.stop();
    print('OK: probe start/stop works')
    "
    ```
  QA scenarios:
    - **Happy**: Probe starts, does one cycle, marks router healthy with model list
    - **Timeout**: Router hangs → mark_unhealthy with timeout reason, model_cache retains last good list
    - **Parallel**: 3 routers, 2 healthy + 1 timeout → probe completes in ~timeout seconds, not 3×timeout
    - **JSON error**: Router returns `{bad json` → mark_unhealthy, log warning
    Evidence: `.omo/evidence/task-4-meta-router.txt`
  Commit: Y | `feat(router_probe): add parallel health probe manager for downstream routers`

- [ ] 5. **Wire catalog to live probes + /v1/models endpoint + persist router state**
  What to do / Must NOT do:
    - Modify `daemon.py`:
      - Import RouterRegistry, RouterProbe, ModelCatalog, ROUTER_STATE_FILE
      - On startup:
        1. Init `RouterRegistry` from `config.DOWNSTREAM_ROUTERS`
        2. Call `router_registry.load(ROUTER_STATE_FILE)` to restore previous health states (graceful if file missing)
        3. Init `ModelCatalog(router_registry)`
        4. Start `RouterProbe(router_registry)` in daemon thread
        5. Register `atexit` handler to `router_registry.persist(ROUTER_STATE_FILE)`
      - Modify existing `/v1/models` handler to call `model_catalog.get_models()` instead of `smart_router.list_all_models()`
      - Add `GET /api/routers` endpoint — returns JSON list of all routers with: name, url, status, models_count, last_success timestamp, last_failure timestamp, cooldown_until, failure_count
      - Existing `/health` endpoint — extend JSON response to include `"routers"` key with router-level summary (counts healthy/cooldown/probing/disabled per router), keep all existing fields
      - Existing `/health/summary` — extend similarly
    - Must NOT remove existing functionality — `/api/providers`, `/api/metrics`, `/api/config` still work identically
    - Must NOT create a circular import — RouterRegistry and ModelCatalog are standalone new modules
    - Must NOT fail on startup if ROUTER_STATE_FILE doesn't exist (first run = fresh state)
  Parallelization: Wave 2 | Blocked by: 3,4 | Blocks: 7
  References:
    - `daemon.py`:26 — existing imports from config
    - `daemon.py`:343-418 — main() startup sequence (registry init, thread start, persist wiring pattern)
    - `daemon.py`:112-122 — existing /v1/models handler
    - `config.py` — new constants
  Acceptance criteria (agent-executable):
    ```bash
    python3 -c "
    import sys; sys.path.insert(0,'.');
    # Verify daemon can import new modules without error
    from daemon import app;
    print('OK: daemon imports new modules')
    "
    ```
    ```bash
    # /v1/models returns deduplicated list (after daemon running)
    curl -s http://localhost:20132/v1/models | python3 -c "
    import sys,json;
    d=json.load(sys.stdin);
    assert d['object']=='list';
    ids=[m['id'] for m in d['data']];
    assert len(ids) == len(set(ids)), 'DUPLICATES FOUND';
    print(f'OK: {len(d[\"data\"])} unique models')
    "
    ```
    ```bash
    # /health includes router summary
    curl -s http://localhost:20132/health | python3 -c "
    import sys,json; d=json.load(sys.stdin);
    assert 'routers' in d;
    print(f'OK: routers in /health: {d[\"routers\"]}')
    "
    ```
  QA scenarios:
    - **Happy**: GET /v1/models returns {object:"list", data:[...]} with unique IDs
    - **Router down**: If all routers down, /v1/models returns empty data array (not error — graceful degradation)
    - **Partial**: 1 router up, 2 down → catalog has that router's models
    - **Persist**: Daemon restart preserves router health states from disk (if previously persisted)
    - **Fresh start**: No ROUTER_STATE_FILE → starts clean with all routers unknown
    Evidence: `.omo/evidence/task-5-meta-router.txt`
  Commit: Y | `feat(daemon): wire model catalog, router probe, and state persistence into startup`

<!-- ═══════════════ WAVE 3: ROUTING ═══════════════ -->
- [ ] 6. **Meta-Router Selector — round-robin + fallback chain**
  What to do / Must NOT do:
    - Create `meta_router.py` with `MetaRouterSelector` class:
      - `__init__(router_registry: RouterRegistry)` — takes registry ref
      - `select_router() -> RouterState` — selects next router:
        1. Get list of healthy routers (from registry)
        2. If list empty → raise `ServiceUnavailable` (caller returns 503)
        3. Round-robin across healthy routers using internal index counter (thread-safe via Lock)
        4. Return selected RouterState (includes url, auth headers)
      - `on_success(router_name: str)` — called after successful proxy response, records metrics
      - `on_failure(router_name: str, error: str)` — called after proxy failure, triggers fallback:
        1. Call `registry.mark_unhealthy(router_name, error)` to put it in cooldown
        2. Call `select_router()` again to get fallback router
        3. If no fallback available → raise `ServiceUnavailable`
      - `route_request(method, path, headers, body) -> (response, fallback_used: bool)` — convenience wrapper:
        1. Select router, forward request, return response
        2. On failure: mark unhealthy, select fallback, retry once
        3. If all fail: return 503 response
      - Must NOT modify request body or headers (passthrough all including auth)
      - Must NOT add latency > 5ms for selection (simple counter, no scoring computation)
    - Thread safety: selection index must use Lock
  Parallelization: Wave 3 | Blocked by: 2,5 | Blocks: 7
  References:
    - `proxy_handler.py`:151-200 — existing proxy_request() pattern
    - `smart_router.py`:1-60 — existing per-provider routing pattern (contrast: router-level vs provider-level)
    - `router_registry.py` — get_healthy_routers(), mark_unhealthy()
  Acceptance criteria (agent-executable):
    ```bash
    python3 -c "
    import sys; sys.path.insert(0,'.');
    from router_registry import RouterRegistry;
    from meta_router import MetaRouterSelector;
    cfg=[{'name':'r1','url':'http://a:1','priority':1},{'name':'r2','url':'http://b:2','priority':1}];
    reg=RouterRegistry(cfg);
    reg.mark_healthy('r1',['gpt-4']);
    reg.mark_healthy('r2',['claude-3']);
    sel=MetaRouterSelector(reg);
    # Round-robin: first call returns r1, second returns r2, third returns r1
    assert sel.select_router().name == 'r1';
    assert sel.select_router().name == 'r2';
    assert sel.select_router().name == 'r1';
    print('OK: round-robin works')
    "
    ```
    ```bash
    # Fallback
    python3 -c "
    import sys; sys.path.insert(0,'.');
    from router_registry import RouterRegistry;
    from meta_router import MetaRouterSelector;
    cfg=[{'name':'r1','url':'http://a:1','priority':1},{'name':'r2','url':'http://b:2','priority':1}];
    reg=RouterRegistry(cfg);
    reg.mark_healthy('r1',['gpt-4']);
    reg.mark_healthy('r2',['claude-3']);
    sel=MetaRouterSelector(reg);
    sel.on_failure('r1','timeout');
    fallback=sel.select_router();  # should skip r1, return r2
    assert fallback.name == 'r2';
    print('OK: fallback works')
    "
    ```
    ```bash
    # All unhealthy → 503
    python3 -c "
    import sys; sys.path.insert(0,'.');
    from router_registry import RouterRegistry;
    from meta_router import MetaRouterSelector, ServiceUnavailable;
    cfg=[{'name':'r1','url':'http://a:1','priority':1}];
    reg=RouterRegistry(cfg);
    sel=MetaRouterSelector(reg);
    try:
        sel.select_router();
        assert False, 'Should raise';
    except ServiceUnavailable:
        print('OK: raises ServiceUnavailable when no healthy routers')
    "
    ```
  QA scenarios:
    - **Happy**: 3 healthy routers, round-robin cycles through all 3 evenly
    - **Fallback**: Primary router fails → secondary selected, tertiary as second fallback
    - **All down**: Raises ServiceUnavailable → caller returns 503 JSON
    - **Single router**: Only 1 healthy → always returns same router
    - **Thread safety**: 10 parallel threads each calling select_router 100 times → round-robin sequence preserved
    Evidence: `.omo/evidence/task-6-meta-router.txt`
  Commit: Y | `feat(meta_router): add round-robin router selector with fallback chain`

- [ ] 7. **Proxy Handler integration — route through meta-router**
  What to do / Must NOT do:
    - Modify `_forward()` in `proxy_handler.py` (line 101-158):
      - Remove direct `NINEROUTER_URL` reference from URL construction (line 113)
      - Instead: call `meta_router.select_router()` to get target router
      - Forward request to `router.url + path` with `router.get_auth_headers()`
      - On successful response (HTTP 200): call `meta_router.on_success(router_name)`
      - On failure (HTTP error or connection error): call `meta_router.on_failure(router_name, error_type)`, then retry once with fallback
      - Track `fallback_used` boolean — if fallback was used, return X-Health-Proxy-Fallback: true header in response
      - If all routers fail: return 503 with `{"error":"all routers unavailable"}`
      - SSE: keep current behavior (full response body read before write — see line 119-120). Do NOT change to incremental streaming (out of scope for this MVP). The full-read approach already passes the complete SSE body as one chunk, which works correctly for clients.
      - Update `_respond_status()` (line 348-362): change `"forwarding": NINEROUTER_URL` to `"forwarding":` a list of all router URLs with their current health status
      - In `do_POST()` health gate (line 397-430): add router-level cooldown check alongside existing provider-level — if ALL routers are in cooldown, return 503 before attempting proxy
    - Must NOT change the client-facing interface — requests/responses must remain identical to clients
    - Must NOT add new external dependencies
    - Must NOT forward to meta-router's own address (check if `router.url` contains `localhost:20131` or `127.0.0.1:20131` — but this should not happen since registry has external routers)
    - Must NOT change _record_usage, _handle_error, _find_healthy_alternative, or _apply_prompt_limit — these are provider-level features that remain unchanged
  Parallelization: Wave 3 | Blocked by: 6 | Blocks: 10
  References:
    - `proxy_handler.py`:101-128 — `_forward()` full implementation, line 113 hardcodes NINEROUTER_URL
    - `proxy_handler.py`:119-120 — current full-body read (NOT streaming; keep this behavior)
    - `proxy_handler.py`:348-362 — `_respond_status()` leaking NINEROUTER_URL in "forwarding" field
    - `proxy_handler.py`:397-430 — `do_POST()` health gate (provider-level checks)
    - `meta_router.py` — select_router(), on_success(), on_failure(), ServiceUnavailable
    - **Port architecture**: meta-router is the HTTP proxy on :20131 (HEALTH_PROXY_PORT). Dashboard stays on :20132 (DASHBOARD_PORT). Both are part of the same daemon process. The meta-router selects between downstream routers on other ports.
  Acceptance criteria (agent-executable):
    ```bash
    python3 -c "
    import sys; sys.path.insert(0,'.');
    from proxy_handler import HealthProxyHandler;
    # Verify import works (runtime test requires running daemon)
    print('OK: proxy_handler imports cleanly')
    "
    ```
    ```bash
    # After daemon restart, verify proxy works end-to-end:
    curl -s -X POST http://localhost:20131/v1/chat/completions \
      -H 'Content-Type: application/json' \
      -d '{\"model\":\"gpt-4o-mini\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}'
    # Should return a valid response (not 503) if at least one router is healthy
    ```
    ```bash
    # Verify /health doesn't leak internal URL
    curl -s http://localhost:20131/health | python3 -c "
    import sys,json; d=json.load(sys.stdin);
    assert 'forwarding' in d;
    assert isinstance(d['forwarding'], list) or 'http' not in str(d['forwarding']);
    print(f'OK: /health forwarding={d[\"forwarding\"]}')
    "
    ```
  QA scenarios:
    - **Happy**: Request proxies through selected router, response returned with correct headers
    - **Fallback**: Primary router 503 → auto-retry with secondary, response returned with X-Health-Proxy-Fallback: true header
    - **SSE**: Full response read works (current behavior preserved); SSE body is complete when client receives it
    - **Auth**: Per-router auth header added correctly; Kiro gets X-API-Key when configured
    - **All down**: Returns 503 with `{"error":"all routers unavailable"}`
    - **No leak**: /health shows router state list, not a single NINEROUTER_URL string
    Evidence: `.omo/evidence/task-7-meta-router.txt`
  Commit: Y | `feat(proxy): route through meta-router selector; update /health; track fallback_used`

- [ ] 8. **Smart Router cleanup — PERMANENTLY_BLOCKED review**
  What to do / Must NOT do:
    - Review `smart_router.py` PERMANENTLY_BLOCKED list:
      - `anthropic, kc, cx, cl, ag, kr, bpm` — these are **provider-level** blocks within 9router/OmniRoute
      - Add comment explaining these are per-provider blocks within a single downstream router, NOT blocks on router-level
      - Ensure `kr` being in PERMANENTLY_BLOCKED does NOT block the Kiro **gateway** at :20129 (they are different — `kr` is a provider key in OmniRoute, Kiro is a separate Python gateway)
      - Verify `smart_router.route_request()` is only called for provider-level routing within a single router, NOT for router-level selection
    - Add `route_to_router()` entry point that calls `meta_router.select_router()` before delegating to router-level smart routing — this is the integration point
    - Must NOT change existing provider-level routing behavior
    - Must NOT remove providers from PERMANENTLY_BLOCKED — they are valid blocks for OmniRoute's internal providers
  Parallelization: Wave 3 | Blocked by: 2 | Blocks: 10
  References:
    - `smart_router.py`:70-120 — PERMANENTLY_BLOCKED list and route_request()
    - `smart_router.py`:1-60 — provider priority scoring
    - `meta_router.py` — new router-level routing
  Acceptance criteria (agent-executable):
    ```bash
    python3 -c "
    import sys; sys.path.insert(0,'.');
    from smart_router import PERMANENTLY_BLOCKED, route_request;
    # Verify import works, PERMANENTLY_BLOCKED still has expected items
    assert 'kr' in PERMANENTLY_BLOCKED;
    # Verify integration point exists
    from smart_router import route_to_router;
    print(f'OK: smart_router clean, {len(PERMANENTLY_BLOCKED)} blocked providers')
    "
    ```
  QA scenarios:
    - **Happy**: Provider-level routing unchanged — `route_request()` still works for 9router's providers
    - **Router-level**: `route_to_router()` correctly delegates to meta-router before provider routing
    - **Documentation**: PERMANENTLY_BLOCKED has clear comment explaining scope
    Evidence: `.omo/evidence/task-8-meta-router.txt`
  Commit: Y | `refactor(smart_router): clarify PERMANENTLY_BLOCKED scope, add route_to_router entry point`

<!-- ═══════════════ WAVE 4: NORMALIZATION ═══════════════ -->
- [ ] 9. **Response Normalizer — normalize all outputs to Big Pickle / OpenCode Zen format**
  What to do / Must NOT do:
    - Create `response_normalizer.py` with `ResponseNormalizer` class:
      - `normalize_chat_completion(response: dict, router_name: str) -> dict`:
        - Normalize response schema to match the standard OpenAI chat completions format used by Big Pickle
        - Ensure consistent field order: `id`, `object`, `created`, `model`, `choices`, `usage`
        - If response has extra fields not in the standard schema, strip them silently
        - If response is missing required fields, add them with sensible defaults
        - Normalize `model` field: map router-specific model IDs to canonical IDs (e.g., `groq/llama-3.3-70b-versatile` → `llama-3.3-70b-versatile` or keep as-is based on config)
      - `normalize_error(status: int, body: dict, router_name: str) -> dict`:
        - Standard error format: `{"error": {"message": str, "type": str, "code": int|str, "router": str}}`
        - Map router-specific error types to standard ones: `authentication_error`, `rate_limit_error`, `provider_unavailable`, `model_not_found`, `context_length_exceeded`, `internal_error`
        - Add `router` field to identify which router returned the error
        - Preserve original error message but add consistent envelope
      - `normalize_streaming_chunk(chunk: dict, router_name: str) -> dict`:
        - Normalize each SSE data chunk to consistent format:
          - `id`, `object`, `created`, `model`, `choices[].delta.{content, role, tool_calls}`, `usage` (on final chunk)
        - Strip non-standard fields from chunks
        - Ensure `choices[].finish_reason` is consistently `stop` | `length` | `content_filter` | `null` | `tool_calls`
      - `normalize_usage(usage: dict) -> dict`:
        - Ensure consistent fields: `prompt_tokens`, `completion_tokens`, `total_tokens`
        - If router reports tokens in different keys, map them (e.g., `input_tokens` → `prompt_tokens`)
        - Add `prompt_tokens_details.cached_tokens` if available
        - Default missing values to 0
      - `get_canonical_model_id(router_model_id: str) -> str`:
        - Strip router prefix if model ID follows `router/model` format (e.g., `groq/llama-3.3-70b-versatile` → `llama-3.3-70b-versatile`)
        - Return the model ID as-is if it doesn't match known patterns
    - Integration: called by `proxy_handler._forward()` after receiving response from router, before writing to client
    - Must NOT change the content or meaning of the response (only the envelope/schema)
    - Must NOT break streaming — normalizer must work on the already-collected full response body (NOT chunk-by-chunk; that's out of scope)
    - Must NOT drop essential data (token usage, finish_reason, etc.)
    - Must NOT change the HTTP status code
    - Must NOT add latency > 1ms (normalizer should be simple dict transforms, no network calls)
  Parallelization: Wave 4 | Blocked by: 7 | Blocks: 12,13
  References:
    - Current response format from 9router/OmniRoute (observed from probes: standard OpenAI chat completions JSON)
    - OpenCode config at `/home/ismaeldev/.config/opencode/opencode.json`: shows `OPENAI_COMPATIBLE` provider schema
    - OhMyOpenAgent config at `/home/ismaeldev/.config/opencode/oh-my-openagent.json`: agent personas and model configs
    - `proxy_handler.py`:119-127 — where normalizer hooks into the response pipeline
    - Example response format (from 9router probe):
      ```json
      {"id":"chatcmpl-...","object":"chat.completion","created":...,"model":"groq/llama-3.3-70b-versatile","choices":[{"index":0,"message":{"role":"assistant","content":"..."},"finish_reason":"stop"}],"usage":{"prompt_tokens":...,"completion_tokens":...,"total_tokens":...}}
      ```
      Error format: `{"error":{"message":"...","type":"...","code":"..."}}`
  Acceptance criteria (agent-executable):
    ```bash
    python3 -c "
    import sys; sys.path.insert(0,'.');
    from response_normalizer import ResponseNormalizer;
    n = ResponseNormalizer();
    # Test normalize_chat_completion
    raw = {'id':'chatcmpl-abc','object':'chat.completion','created':123,'model':'groq/llama-3.3-70b-versatile','choices':[{'index':0,'message':{'role':'assistant','content':'hi'},'finish_reason':'stop'}],'usage':{'prompt_tokens':10,'completion_tokens':5,'total_tokens':15}};
    normalized = n.normalize_chat_completion(raw, '9router');
    assert normalized['model'] == 'llama-3.3-70b-versatile' or normalized['model'] == 'groq/llama-3.3-70b-versatile';  # depends on mapping config
    assert normalized['usage']['total_tokens'] == 15;
    print('OK: normalize_chat_completion works')
    "
    ```
    ```bash
    python3 -c "
    import sys; sys.path.insert(0,'.');
    from response_normalizer import ResponseNormalizer;
    n = ResponseNormalizer();
    err = n.normalize_error(429, {'error':{'message':'Rate limit','type':'rate_limit_error','code':429}}, 'kiro');
    assert 'router' in err['error'];
    assert err['error']['router'] == 'kiro';
    print('OK: normalize_error works')
    "
    ```
    ```bash
    python3 -c "
    import sys; sys.path.insert(0,'.');
    from response_normalizer import ResponseNormalizer;
    n = ResponseNormalizer();
    usage = n.normalize_usage({'input_tokens':100,'output_tokens':50});
    assert usage['prompt_tokens'] == 100;
    assert usage['completion_tokens'] == 50;
    assert usage['total_tokens'] == 150;
    print('OK: normalize_usage works')
    "
    ```
  QA scenarios:
    - **Happy**: Standard response passes through with model ID normalized
    - **Missing fields**: Response without `usage` → default usage with all zeros added
    - **Error normalization**: 429 error → standard format with `router` field
    - **Usage mapping**: `input_tokens` → `prompt_tokens`, `output_tokens` → `completion_tokens`
    - **Canonical IDs**: `groq/llama-3.3-70b-versatile` → `llama-3.3-70b-versatile` (strip router prefix)
    Evidence: `.omo/evidence/task-9-meta-router.txt`
  Commit: Y | `feat(response_normalizer): normalize all router outputs to Big Pickle / OpenCode Zen format`

- [ ] 10. **Model ID Mapper — canonical model IDs across routers**
  What to do / Must NOT do:
    - Create `model_id_mapper.py` with `ModelIdMapper` class:
      - `__init__(mapping_config: dict = None)` — loads optional ID mapping config
      - `to_canonical(router_model_id: str) -> str` — converts router-specific model ID to canonical
        - Strip router prefix: `groq/llama-3.3-70b-versatile` → `llama-3.3-70b-versatile`
        - If no mapping exists, return the original ID as-is
        - Handle edge cases: `main-rr`, `combo-round-robin`, `kr/auto` → pass through (these are router-level, not model-level)
      - `to_router_specific(canonical_id: str, router_name: str) -> str` — reverse mapping for request routing
        - When sending a request to a specific router, re-add the required prefix
        - Uses the router's model catalog to find the matching model ID
      - `register_mapping(router_name: str, router_prefix: str)` — register a prefix stripping rule
        - Default: `9router` → prefix `groq/`, `nvidia/`, `kr/`, `anthropic/` stripped
        - `kiro` → no prefix stripping (Kiro models are already bare: `claude-sonnet-4.5`)
        - `omniroute` → same as 9router (they share the same OmniRoute codebase)
      - `get_mapping_stats() -> dict` — return count of mapped models, unmapped models, collisions
    - Must NOT lose information — if a model ID doesn't match any rule, pass it through unchanged
    - Must NOT create collisions — two different models mapping to the same canonical ID should log a warning
  Parallelization: Wave 4 | Blocked by: 7 | Blocks: 12,13
  References:
    - OpenCode config model list — shows model IDs like `groq/llama-3.3-70b-versatile`, `nvidia/deepseek-ai/deepseek-v4-pro`, `kr/auto`, `ollama/kimi-k2.5`
    - `model_catalog.py` — deduplication already handles different routers having same model
    - `response_normalizer.py` — uses ModelIdMapper for canonical ID lookup
  Acceptance criteria (agent-executable):
    ```bash
    python3 -c "
    import sys; sys.path.insert(0,'.');
    from model_id_mapper import ModelIdMapper;
    m = ModelIdMapper({});
    # Strip router prefix
    assert m.to_canonical('groq/llama-3.3-70b-versatile') == 'llama-3.3-70b-versatile';
    assert m.to_canonical('nvidia/deepseek-ai/deepseek-v4-pro') == 'deepseek-ai/deepseek-v4-pro';
    # Pass through for router-level IDs
    assert m.to_canonical('main-rr') == 'main-rr';
    assert m.to_canonical('combo-round-robin') == 'combo-round-robin';
    # Kiro models are already bare
    assert m.to_canonical('claude-sonnet-4.5') == 'claude-sonnet-4.5';
    print('OK: model ID mapping works')
    "
    ```
    ```bash
    # Reverse mapping
    python3 -c "
    import sys; sys.path.insert(0,'.');
    from model_id_mapper import ModelIdMapper;
    m = ModelIdMapper({'9router': {'prefixes': ['groq/', 'nvidia/', 'kr/', 'anthropic/', 'ollama/']}});
    # Register default mappings
    m.register_mapping('9router', ['groq/', 'nvidia/', 'kr/', 'anthropic/', 'ollama/']);
    # Test reverse
    result = m.to_router_specific('llama-3.3-70b-versatile', '9router');
    assert result == 'groq/llama-3.3-70b-versatile' or result == 'llama-3.3-70b-versatile';
    print('OK: reverse mapping works')
    "
    ```
  QA scenarios:
    - **Happy**: `groq/llama-3.3-70b-versatile` → `llama-3.3-70b-versatile`
    - **Pass-through**: `main-rr`, `combo-round-robin`, `kr/auto` pass through unchanged
    - **No match**: Unknown model ID passes through unchanged
    - **Collision**: Two models mapping to same canonical → logged warning, first mapping wins
    - **Reverse**: Canonical → router-specific checks router's model catalog
    Evidence: `.omo/evidence/task-10-meta-router.txt`
  Commit: Y | `feat(model_id_mapper): add canonical model ID mapping across routers`

<!-- ═══════════════ WAVE 5: VISIBILITY ═══════════════ -->
- [ ] 11. **Dashboard — router health panel + provider aggregation**
  What to do / Must NOT do:
    - Modify `dashboard.py`:
      - Add `/api/routers` route (same data as daemon.py but in dashboard blueprint):
        - Returns JSON: [{name, url, status, models_count, last_success, last_failure, cooldown_until, failure_count}]
      - Extend dashboard HTML template with "Router Health" panel:
        - Cards — one per router, color-coded by status (green=healthy, yellow=probing, red=cooldown, gray=disabled)
        - Each card shows: name, URL, status badge, model count, last probe time
        - Click to expand: shows model list, provider aggregation for that router
      - Extend existing provider table: add "Origin Router" column showing which router(s) each provider comes from
      - SSE endpoint `/api/events` — extend to emit router health changes alongside provider changes
      - Add `/api/providers` aggregation:
        - Merges providers from all routers' health data
        - Deduplicates by provider name
        - Each entry includes `origins: [router_names]` array
    - Must NOT break existing provider table, metrics chart, or health alerts
    - Must NOT require page reload to see router updates (SSE handles this)
  Parallelization: Wave 4 | Blocked by: 2 | Blocks: 10
  References:
    - `dashboard.py`:1-300 — existing Flask blueprint with routes, template, SSE
    - `dashboard.py`:100-150 — existing `/api/providers` endpoint
    - Existing HTML template in dashboard.py (inline HTML strings)
    - `router_registry.py` — get_all_routers(), get_provider_aggregation()
  Acceptance criteria (agent-executable):
    ```bash
    python3 -c "
    import sys; sys.path.insert(0,'.');
    from dashboard import dashboard;
    # Verify dashboard blueprint still registers
    print('OK: dashboard blueprint loads')
    "
    ```
    ```bash
    # After daemon restart:
    curl -s http://localhost:20132/api/routers | python3 -c "
    import sys,json; d=json.load(sys.stdin);
    assert len(d) >= 1;
    print(f'OK: {len(d)} routers reported')
    "
    ```
    ```bash
    curl -s http://localhost:20132/api/providers | python3 -c "
    import sys,json; d=json.load(sys.stdin);
    providers = d.get('providers', d if isinstance(d,list) else []);
    # Check origin tagging
    if providers:
        p = providers[0];
        print(f'OK: provider {p.get(\"name\",\"?\")} origins={p.get(\"origins\",\"not found\")}');
    "
    ```
  QA scenarios:
    - **Happy**: Dashboard loads at /dashboard/, shows router cards with correct status, provider table has origin column
    - **SSE**: Router status change reaches dashboard without page reload
    - **Aggregation**: Same provider from 2 routers → one row with 2 origins
    Evidence: `.omo/evidence/task-9-meta-router.txt`
  Commit: Y | `feat(dashboard): add router health panel and aggregated provider view`

- [ ] 12. **Integration test suite — full system verification**
  What to do / Must NOT do:
    - Create `tests/test_meta_router_integration.py` with tests that exercise the full stack:
      - `test_round_robin_alternates`: Start daemon with 2 mock routers, send 3 requests, verify they alternate
      - `test_fallback_on_failure`: Primary router returns 500, verify secondary receives the request
      - `test_all_down_returns_503`: All routers return 503, verify meta-router returns 503
      - `test_model_catalog_dedup`: 2 routers list overlapping models, verify /v1/models returns unique IDs
      - `test_model_catalog_empty`: All routers down, verify /v1/models returns empty data array (graceful)
      - `test_provider_aggregation_dedup`: Duplicate provider names across routers, verify one entry with combined origins
      - `test_request_id_propagation`: Custom X-Request-ID header passes through to downstream router
      - `test_streaming_passthrough`: SSE streaming response works through meta-router
      - `test_probe_marks_healthy`: Probe successfully fetches /v1/models → router marked healthy
      - `test_probe_timeout_marks_unhealthy`: Probe timeout → router marked cooldown, model cache preserved
      - `test_per_router_auth`: Kiro router with API key header sends X-API-Key in requests
      - `test_concurrent_safety`: 10 parallel requests don't corrupt round-robin state
    - Use pytest + requests_mock to simulate downstream routers
    - Must NOT require actual running routers — all downstream calls are mocked
    - Must NOT take longer than 30s to run
  Parallelization: Wave 5 | Blocked by: 7,8,11 | Blocks: 13,14
  References:
    - All new modules: router_registry, router_probe, model_catalog, meta_router
    - All modified modules: proxy_handler, smart_router, daemon, dashboard
    - `tests/conftest.py` — shared fixtures (already created in Todo 1)
  Acceptance criteria (agent-executable):
    ```bash
    cd /home/ismaeldev/Desktop/code_study/MeusProjetos/provider-health-daemon
    python3 -m pytest tests/test_meta_router_integration.py -v 2>&1 | tail -30
    # Expected: all 14+ tests pass
    ```
  QA scenarios:
    - **Happy**: All integration tests pass
    - **Regression**: Existing functionality still works (existing providers unaffected)
    Evidence: `.omo/evidence/task-12-meta-router.txt`
  Commit: Y | `test(integration): add full integration test suite for meta-router`

<!-- ═══════════════ WAVE 6: HARDENING ═══════════════ -->
- [ ] 13. **Security sanitization — XSS prevention + header hygiene**
  What to do / Must NOT do:
    - Add `sanitize_string(s: str) -> str` utility in a new `util.py` or inline:
      - Strip HTML tags: replace `<` with `&lt;`, `>` with `&gt;`
      - Strip control characters except newlines
    - Apply to ALL string fields in:
      - `model_catalog.py`: model `id`, `owned_by` before returning in /v1/models
      - `router_registry.py`: router `name`, `url` before JSON serialization
      - `dashboard.py`: all provider/route names before rendering in HTML template
    - Add test: `test_xss_sanitization` in integration suite
    - Must NOT break Unicode characters (Cyrillic, CJK, emoji in model names)
  Parallelization: Wave 6 | Blocked by: 12 | Blocks: 15
  References:
    - `model_catalog.py` — model data output
    - `router_registry.py` — JSON serialization
    - `dashboard.py` — HTML template rendering (currently uses f-strings — needs escaping)
  Acceptance criteria (agent-executable):
    ```bash
    python3 -c "
    import sys; sys.path.insert(0,'.');
    from model_catalog import sanitize_string;
    assert sanitize_string('<script>alert(1)</script>') == '&lt;script&gt;alert(1)&lt;/script&gt;';
    assert sanitize_string('gpt-4') == 'gpt-4';
    assert 'ü' in sanitize_string('gpt-4-ü');  # Unicode preserved
    print('OK: sanitization works')
    "
    ```
  QA scenarios:
    - **Happy**: Normal model names passthrough unchanged
    - **XSS**: `<script>` tags escaped to `&lt;script&gt;`
    - **Unicode**: Cyrillic, CJK characters preserved
    Evidence: `.omo/evidence/task-13-meta-router.txt`
  Commit: Y | `fix(security): add HTML sanitization for model metadata and dashboard output`

- [ ] 14. **Edge case hardening — flapping, stale models, rate limits**
  What to do / Must NOT do:
    - **Flapping guard** (in `router_registry.py`):
      - Router must have `consecutive_probes_ok >= 2` to promote from probing → healthy
      - On first successful probe after cooldown: set status=probing, not healthy
      - On second consecutive successful probe: promote to healthy
    - **Stale model cleanup** (in `router_registry.py`):
      - When `refresh_models_from_router()` is called, replace the model list entirely, don't append
      - If a model disappears from a router's /v1/models, it disappears from that router's entry
      - The unified catalog auto-reflects this via get_model_catalog()
    - **Rate limit (429) handling** (in `error_parser.py`):
      - Verify HTTP 429 is explicitly mapped: add `r"429"` pattern → `{"hours": 0, "minutes": 5, "type": "rate_limit"}` if not present
    - **Probe timeout safety** (in `router_probe.py`):
      - All HTTP requests must have timeout set explicitly from config.PROBE_TIMEOUT
      - Wrap in try/except (urllib.error.URLError, socket.timeout)
    - **Startup validation** (in `daemon.py`):
      - On startup, validate DOWNSTREAM_ROUTERS has at least 1 entry; if 0, log WARNING
      - Validate no self-referential URLs (meta-router's own address)
    - **Graceful shutdown** (in `daemon.py`):
      - Register `atexit` handler that stops RouterProbe and persists registry state
    - Must NOT change existing error_parser behavior for non-429 errors
  Parallelization: Wave 6 | Blocked by: 12 | Blocks: 15
  References:
    - `router_registry.py` — consecutiv_probes_ok field, mark_healthy(), refresh_models_from_router()
    - `router_probe.py` — probe loop timeout handling
    - `error_parser.py`:14-80 — ERROR_PATTERNS list
    - `daemon.py` — startup sequence
  Acceptance criteria (agent-executable):
    ```bash
    # Flapping guard test
    python3 -c "
    import sys; sys.path.insert(0,'.');
    from router_registry import RouterRegistry;
    cfg=[{'name':'r1','url':'http://a:1','priority':1}];
    reg=RouterRegistry(cfg);
    reg.mark_healthy('r1',['gpt-4']);  # first probe: sets probing
    assert reg.get_router('r1').health_status == 'probing';
    assert reg.get_healthy_routers() == [];  # not yet healthy
    reg.mark_healthy('r1',['gpt-4']);  # second probe: promotes to healthy
    assert reg.get_router('r1').health_status == 'healthy';
    assert len(reg.get_healthy_routers()) == 1;
    print('OK: flapping guard works (2 probes needed)')
    "
    ```
    ```bash
    # Stale model cleanup test
    python3 -c "
    import sys; sys.path.insert(0,'.');
    from router_registry import RouterRegistry;
    cfg=[{'name':'r1','url':'http://a:1','priority':1}];
    reg=RouterRegistry(cfg);
    reg.mark_healthy('r1',['gpt-4','claude-3']);
    assert 'gpt-4' in reg.get_model_catalog();
    reg.refresh_models_from_router('r1', ['gpt-4']);  # claude-3 removed
    assert 'claude-3' not in reg.get_model_catalog();  # stale removed
    print('OK: stale model cleanup')
    "
    ```
  QA scenarios:
    - **Flapping**: Router with intermittent failures stays in probing until 2 consecutive successes
    - **Stale models**: Model removed from router → disappears from catalog immediately
    - **429 mapping**: error_parser returns rate_limit type for 429
    - **Self-ref validation**: daemon startup warns if a router URL points to itself
    Evidence: `.omo/evidence/task-14-meta-router.txt`
  Commit: Y | `fix(edge_cases): add flapping guard, stale model cleanup, 429 handling, startup validation`

- [ ] 15. **Final verification wave — QA pass**
  What to do / Must NOT do:
    - Run full integration test suite and fix any failures
    - Manual smoke test with actual running routers:
      - Verify /v1/models returns deduplicated model list from all healthy routers
      - Send a chat completion request, verify it routes and returns response
      - Verify dashboard loads at /dashboard with router health panel
      - Verify /api/routers returns correct health data
      - Verify /api/providers shows aggregated providers with origin tags
    - Run `ruff check .` on all new/modified Python files — zero errors
    - Capture all evidence to `.omo/evidence/`
    - Must NOT leave any failing tests
  Parallelization: Wave 6 | Blocked by: 13,14 | Blocks: —
  References:
    - All todos 1-15
    - `tests/test_meta_router_integration.py`
  Acceptance criteria (agent-executable):
    ```bash
    python3 -m pytest tests/ -v 2>&1 | tail -5
    # All tests pass
    ```
    ```bash
    ruff check router_registry.py router_probe.py model_catalog.py meta_router.py proxy_handler.py daemon.py dashboard.py smart_router.py 2>&1
    # Zero errors
    ```
  QA scenarios:
    - **Full suite**: All 17+ tests pass
    - **Lint**: Zero ruff errors
    - **Smoke**: Manual curl test with real routers works
    Evidence: `.omo/evidence/task-15-meta-router.txt`
  Commit: Y | `chore: final QA pass — all tests passing, lint clean`

## Final verification wave
> Runs in parallel after ALL todos. ALL must APPROVE. Surface results and wait for the user's explicit okay before declaring complete.
- [ ] F1. **Plan compliance audit** — verify every Must Have from Scope is implemented and every Must NOT Have is violated nowhere
- [ ] F2. **Code quality review** — ruff check zero errors; each new module has docstring, type hints, no `# type: ignore` without justification
- [ ] F3. **Real manual QA** — daemon running, curl /v1/models returns deduped model list, curl /api/routers returns router health, curl POST /v1/chat/completions with `{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}]}` returns a valid response
- [ ] F4. **Scope fidelity** — no downstream router code was modified; no OmniRoute strategies were reimplemented; no provider-level health tracking was duplicated

## Commit strategy
- One commit per todo (15 commits total), each with conventional commit format: `type(scope): description`
- Types: `feat` for new modules, `refactor` for modified existing modules, `fix` for edge cases, `test` for test suites, `chore` for infra
- Each commit must compile (i.e. `python3 -c "import <module>"` succeeds) and existing tests must not regress
- Final commit is the QA pass

## Success criteria
- Meta-router proxies requests through 3 downstream routers with round-robin + fallback
- Response normalizer transforms all router outputs to consistent Big Pickle / OpenCode Zen format
- /v1/models returns deduplicated catalog (same model from 2 routers → 1 entry with 2 origins)
- /api/providers shows aggregated providers from all routers, each tagged with `origins` array
- Dashboard shows router health panel with color-coded cards + per-router provider drill-down
- All tests pass (17+ integration tests)
- ruff check zero errors
- Streaming (SSE) passthrough works through meta-router
- When all routers are down, returns HTTP 503 with `{"error":"all routers unavailable"}`
- Graceful degradation: 1 router down doesn't affect the other routers
