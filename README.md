# Provider Health Daemon 🛡️

AI provider health daemon for [9router](https://github.com/decolua/9router) — real-time error-aware routing with cooldown management, exponential backoff, and prompt limiting integration.

## What It Does

- **Health-aware proxy** (port 20131) — intercepts requests to 9router, checks provider/model health before routing
- **Error parsing** — extracts provider name + cooldown duration from HTTP errors (429, 403, 400, 404, 500) and 9router log lines
- **Exponential backoff** — each failure doubles cooldown (max 24h)
- **Probing** — expired cooldowns auto-promote to probing; successful probe → healthy again
- **Prompt limiting** — reuses [prompt-limiter](https://github.com/ismaeldouglasdev/prompt-limiter) to truncate oversized requests before they burn a fallback
- **Log monitor** — tails `~/.9router/logs/error.log` for errors missed by proxy interception

## Architecture

```
OpenCode ──→ :20131 ──→ Health Daemon ──→ :20128 ──→ 9router ──→ Providers (free tiers)
                           │
                           ├── :20129 → Kiro (downstream router)
                           ├── :20132 → Dashboard web (/api/pool, /api/metrics, /api/providers)
                           ├── prompt_limiter (import)
                           └── ~/.9router/  → health.json, data, logs
```

## Robustness (recent additions)

- **Bind-first startup** — ports open <1s after start; boot-time network probes run in background so opencode fallback chains never hit a dead window
- **Progressive SSE streaming** — head-window drain (64 lines) keeps the empty-200 fallback while forwarding live chunks (real TTFT instead of full-body buffering)
- **Global fallback chain** — 5xx, access errors and empty-200s retry the next healthy model (`MAX_FALLBACK_RETRIES=2`)
- **Quota-aware rotation** (`QUOTA_AWARE_ROTATION=true`) — providers with zero usage today get priority inside the spread band; goal: burn every free tier daily without hammering used ones
- **Pool degraded alert** — `healthy_count < POOL_DEGRADED_THRESHOLD` fires desktop notification via alerter; live status at `GET :20132/api/pool`
- **Local-only bind** — proxy + dashboard listen on `127.0.0.1` by default (`HEALTH_PROXY_HOST`/`DASHBOARD_HOST` override)

## Operations Runbook

### Safe restart
```bash
systemctl --user restart provider-health-daemon
# Verify fast bind (<3s expected):
python3 -c "import socket,time; t0=time.time()
while True:
    try:
        socket.create_connection(('127.0.0.1',20131),0.2).close(); print(f'{time.time()-t0:.1f}s'); break
    except OSError: time.sleep(0.05)"
```
NEVER start the daemon manually outside systemd — an orphan process holds the ports and causes a crash-loop (see bugs file 2026-08-20).

### Anti-ghost sanitizer (session resume failures)
Hourly timer runs `scripts/opencode_ghost_sanitizer.py` + `scripts/omo_plugin_patcher.py` (ExecStartPre):
```bash
systemctl --user list-timers opencode-ghost-sanitizer.timer    # next run
python3 scripts/opencode_ghost_sanitizer.py --dry-run          # manual inspection
journalctl --user -u opencode-ghost-sanitizer.service -n 20    # history
```
Cleans stale `providerID:"opencode"` refs (>1h) from `~/.local/share/opencode/opencode.db`, remapping to `9router/ollama/gpt-oss:120b`. The patcher re-applies the dot-strip patch to the oh-my-openagent plugin whenever `@latest` updates wipe it.

### Provider cooldowns
```bash
python3 -c "import json; d=json.load(open('$HOME/.9router/health.json')); [print(k, v['status'], v.get('until','')) for k,v in d['providers'].items()]"
curl -X POST 'http://127.0.0.1:20132/api/admin/reactivate' -H 'Content-Type: application/json' -d '{"provider":"<name>"}'
```
Cooldowns expire on their own (recovery prober re-tests); manual reactivation is for exceptional cases.

### Configuration (env-overridable, see config.py)

| Variable | Default | Purpose |
|---|---|---|
| `UPSTREAM_TIMEOUT` | 60s | Forward timeout (kept below client timeouts) |
| `HEALTH_PROXY_HOST` / `DASHBOARD_HOST` | 127.0.0.1 | Server binds |
| `QUOTA_AWARE_ROTATION` | true | Boost unused-today providers |
| `POOL_DEGRADED_THRESHOLD` | 8 | Alert when healthy < N |
| `STREAM_HEAD_WINDOW_LINES` | 64 | SSE head window |

## Tests & Lint
```bash
python3 -m pytest tests/ -q                  # full suite
ruff check . --ignore E501,F403,F401,E402    # same lint as CI
```

## Install

```bash
git clone https://github.com/ismaeldouglasdev/provider-health-daemon.git
cd provider-health-daemon

# Requires Python 3.10+ and prompt-limiter installed
pip install -r requirements.txt  # (currently no external deps)
```

## Usage

```bash
# Start
./run.sh start

# Status
./run.sh status

# Stop
./run.sh stop
```

### Activate in OpenCode

In `~/.config/opencode/opencode.json`, change the 9router baseURL:

```json
"9router": {
  "options": {
    "baseURL": "http://127.0.0.1:20131/v1",  // was :20128
    ...
  }
}
```

OpenCode's `oh-my-openagent` fallback mechanism will now trigger on 503 responses from the health proxy, skipping providers/models in cooldown.

### Admin

```bash
# Health status
curl http://127.0.0.1:20131/health

# Force reset a provider/model
curl http://127.0.0.1:20131/health/reset/groq
curl http://127.0.0.1:20131/health/reset/nvidia/some-function-id

# Compact summary
curl http://127.0.0.1:20131/health/summary
```

## Error → Cooldown Mapping

| Error | Cooldown |
|---|---|
| `429 "try again in XhYm"` | X hours (parsed) |
| `429` daily free exhausted | 24h |
| `429` generic | 5min, ×2 backoff |
| `403` paid required / pricing | Permanent (manual reset) |
| `403` auth invalid | Permanent |
| `400` InvalidSubscription | 1h, recheck |
| `400` Function id not found | 1h (model-specific) |
| `400` Context too long | 15min (model-specific, + prompt limit) |
| `500` Internal error | 2min |
| `fetch failed` | 5min |

## Files

| File | Purpose |
|---|---|
| `daemon.py` | Entrypoint — proxy + log monitor + prober (bind-first startup) |
| `proxy_handler.py` | HTTP proxy with health gate + SSE head-window streaming |
| `health_registry.py` | Health state CRUD + persistence |
| `error_parser.py` | Parse errors → provider + cooldown |
| `cooldown.py` | Exponential backoff logic |
| `smart_router.py` | Model ranking + spread band + quota-aware rotation |
| `daily_usage.py` | Per-provider daily usage aggregation (from access.log) |
| `dashboard.py` | Web UI: `/api/pool` (TTFT p50/p95), metrics, providers |
| `alerter.py` | Desktop notifications incl. pool degradation |
| `scripts/opencode_ghost_sanitizer.py` | Hourly DB cleanup of dead model refs (+ systemd timer) |
| `scripts/omo_plugin_patcher.py` | Idempotent dot-strip patch re-applier for oh-my-openagent |
| `config.py` | Paths, ports, defaults (env-overridable) |
| `run.sh` | Quick start/stop/status |

## Depends On

- [prompt-limiter](https://github.com/ismaeldouglasdev/prompt-limiter) — for `count_tokens`, `get_model_limits`, `truncate_prompt`
- [9router](https://github.com/decolua/9router) — the AI gateway being monitored

## Troubleshooting

Read **`~/bugs-erros-opencode.md`** BEFORE diagnosing new errors — it documents known root causes with applied fixes (Kiro misroute, amplified timeouts, ghost model refs, lock crash-loops, etc.).

## License

MIT