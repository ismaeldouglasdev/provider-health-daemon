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
OpenCode ──→ :20131 ──→ Health Daemon ──→ :20128 ──→ 9router ──→ Providers
                           │
                           ├── prompt_limiter (import)
                           └── ~/.9router/health.json
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
| `daemon.py` | Entrypoint — proxy + log monitor + prober |
| `proxy_handler.py` | HTTP proxy with health gate |
| `health_registry.py` | Health state CRUD + persistence |
| `error_parser.py` | Parse errors → provider + cooldown |
| `cooldown.py` | Exponential backoff logic |
| `config.py` | Paths, ports, defaults |
| `run.sh` | Quick start/stop/status |

## Depends On

- [prompt-limiter](https://github.com/ismaeldouglasdev/prompt-limiter) — for `count_tokens`, `get_model_limits`, `truncate_prompt`
- [9router](https://github.com/decolua/9router) — the AI gateway being monitored

## License

MIT