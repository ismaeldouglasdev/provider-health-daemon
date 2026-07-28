---
slug: provider-health-daemon-robustez
status: awaiting-approval
intent: unclear
pending-action: write .omo/plans/provider-health-daemon-robustez.md
approach: Parallel improvements across thread safety, testing, CI/CD, data persistence, connection handling, graceful shutdown, security, monitoring, and SSE cleanup.
---

# Draft: provider-health-daemon-robustez

## Components (topology ledger)
<!-- id | outcome (one line) | status: active|deferred | evidence path -->

1. **HealthRegistry** | Thread-safe + atomic writes | active | health_registry.py:24-45
2. **MetricsStore** | Thread-safe access | active | metrics_store.py (via proxy_handler + dashboard)
3. **Proxy handler** | Connection pooling, timeout hardening | active | proxy_handler.py:101-158
4. **Dashboard** | SSE cleanup, rate limiting | active | dashboard.py:311-357
5. **Daemon** | Graceful shutdown, signal handling | active | daemon.py:343-422
6. **Testing** | pytest suite + CI | active | no tests exist
7. **Config** | Single source of truth, validation | active | config.py + scattered hardcoded paths

## Open assumptions (announced defaults)
<!-- assumption | adopted default | rationale | reversible? -->

1. **Test framework** → pytest + pytest-asyncio (if needed) | Zero tests exist; pytest is Python standard | Yes
2. **CI platform** → GitHub Actions | Free, already on GitHub ecosystem | Yes
3. **Thread safety** → `threading.Lock` + atomic file write | Minimal dependency, stdlib only | Yes
4. **Connection pooling** → `urllib.request.build_opener` with HTTPHandler (keep-alive) | Avoids adding httpx/requests dependency | Yes
5. **Auth model** → Single API key via `X-API-Key` header for admin endpoints | Simple, stateless, no DB needed | Yes
6. **Monitoring** → Prometheus `/metrics` endpoint via stdlib or simple counter class | Standard for Go/Python services | Yes
7. **Config** → All hardcoded paths consolidated in config.py with env var override | Centralizes configuration | Yes
8. **Shutdown** → SIGTERM handler sets event flag, threads check flag, main joins | Standard daemon pattern | Yes
9. **SSE cleanup** → Periodically prune stale client IDs from sse_clients list | Simple, prevents memory leak | Yes
10. **Logging** → Add file handler (rotating) in addition to stderr | Persists logs across restarts | Yes

## Findings (cited - path:lines)

1. **Zero tests** — `grep -r "def test" health_registry.py cooldown.py proxy_handler.py dashboard.py` → no matches
2. **Thread safety gap** — `HealthRegistry._data` accessed from proxy handler (proxy_handler.py:134,236), log monitor (daemon.py:293), prober (daemon.py:250), dashboard (dashboard.py:249-251,404) — no Lock anywhere
3. **No atomic write** — `health_registry.py:44-45` writes JSON directly via `filepath.write_text(text)` — partial write on crash corrupts file
4. **Hardcoded .9router path** — `LOG_PATH = Path.home() / ".9router" / "logs" / "error.log"` (daemon.py:236), `SNAPSHOT_DIR` (metrics_persistence.py:18), `HEALTH_FILE` (config.py) — scattered
5. **SSE client list growth** — `DashboardHandler.sse_clients.append(client_id)` (dashboard.py:321) — appends per connection, removes only on clean disconnect in `finally`; no pruning of stale entries if `_handle_sse` never enters body
6. **Daemon threads all daemon=True** — all threads (daemon.py:378,384,392,401,474) are `daemon=True` — no graceful shutdown, threads killed abruptly on exit
7. **No CI** — `.github/workflows/` does not exist
8. **No auth on admin endpoints** — POST `/api/admin/reactivate` (dashboard.py:389), `/api/admin/router` have no access control
9. **No connection pooling** — `proxy_handler.py:119` uses `urllib.request.urlopen` per request — new TCP connection each time, no keep-alive
10. **Hardcoded port defaults** — `DASHBOARD_PORT=20132`, `HEALTH_PROXY_PORT=20131` in config.py — not overridable via env vars currently

## Decisions (with rationale)

1. **Use stdlib Lock + atomic write** instead of external libs: minimal perf cost, no dependencies added.
2. **pytest over unittest**: better fixture model, less boilerplate, `pytest-cov` for coverage.
3. **Single API key over JWT/OAuth**: this is a local daemon, not a multi-tenant web service. Simpler is safer.
4. **Prometheus `/metrics` over custom JSON endpoint**: standard tooling, Grafana dashboards available.
5. **Treat each improvement as a separate todo**: each is independently testable and reversible.

## Scope IN

- Thread safety: Lock in HealthRegistry + MetricsStore
- Atomic file writes (write .tmp → rename)
- Graceful shutdown (SIGTERM handler + thread join)
- pytest test suite for core logic
- GitHub Actions CI (test + lint + typecheck)
- Connection reuse for proxy (keep-alive opener)
- API key auth for admin endpoints
- SSE client list pruning (periodic cleanup)
- Config centralization (all paths in config.py, env var overrides)
- File logging with rotation
- Prometheus `/metrics` endpoint

## Scope OUT (Must NOT have)

- NO migration to async/await (stdlib threads work fine for this load)
- NO adding FastAPI/Flask (keep stdlib http.server)
- NO containerization (systemd service is sufficient)
- NO database (JSON file persistence is appropriate)
- NO rate limiting per-client (local daemon, single user)
- NO WebSocket migration (SSE is sufficient)
- NO multi-instance clustering

## Open questions

<!-- ALL resolved by defaults above due to UNCLEAR intent -->

## Approval gate
status: awaiting-approval
<!-- When exploration is exhausted and unknowns are answered, set status: awaiting-approval. -->
<!-- That durable record is the loop guard: on a later turn read it and resume at the gate instead of re-running exploration. -->
