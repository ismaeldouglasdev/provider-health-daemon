"""Provider Health Daemon — configuration."""

import os
from pathlib import Path

# ── Network ──────────────────────────────────────────────────────────
HEALTH_PROXY_PORT = int(os.environ.get("HEALTH_PROXY_PORT", "20131"))
NINEROUTER_URL = os.environ.get("NINEROUTER_URL", "http://localhost:20128")
NINEROUTER_KEY = os.environ.get("NINEROUTER_KEY", "")

# ── Dashboard ────────────────────────────────────────────────────────
DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", "20132"))

# ── Access Log ───────────────────────────────────────────────────────
ACCESS_LOG_PATH = Path.home() / ".9router" / "logs" / "access.log"

# ── Paths ────────────────────────────────────────────────────────────
HEALTH_FILE = Path.home() / ".9router" / "health.json"

# Node config for loading from oh-my-openagent.json
AGENT_CONFIG = Path.home() / ".config" / "opencode" / "oh-my-openagent.json"
MODEL_LIMITS_FILE = Path.home() / ".config" / "opencode" / "model_limits.json"

# ── Prompt Limiter integration ───────────────────────────────────────
PROMPT_LIMITER_DIR = Path.home() / "Desktop" / "code_study" / "MeusProjetos" / "prompt-limiter"

# ── Cooldown defaults ────────────────────────────────────────────────
PROBER_INTERVAL_MINUTES = 5  # how often to probe cooled-down providers (deprecated, use PROBER_INTERVAL_SECONDS)
MAX_COOLDOWN_HOURS = 24  # cap exponential backoff

# ── Smart Router ─────────────────────────────────────────────────────
COMBO_REFRESH_INTERVAL = 60  # seconds between combo list refresh
COMBO_CACHE_FILE = Path.home() / ".9router" / "combo_cache.json"  # last-good catalog fallback

# ── Meta-Router: Downstream Routers ──────────────────────────────────
# Each entry: {name, url, priority(int, lower=first), health_check_path(str), timeout(float), weight(int), auth(dict|None)}
# auth format: {"header": "X-API-Key", "value": "..."}
_RAW_DOWNSTREAM_ROUTERS = [
    {
        "name": "OmniRoute",
        "url": "http://localhost:20128",
        "priority": 1,
        "weight": 1,
        "health_check_path": "/v1/models",
        "timeout": 15.0,
        "auth": {"header": "Authorization", "value": f"Bearer {os.environ.get('NINEROUTER_KEY', '')}"},
    },
    {
        "name": "Kiro",
        "url": "http://localhost:20129",
        "priority": 2,
        "weight": 1,
        "health_check_path": "/v1/models",
        "timeout": 5.0,
        "auth": {"header": "Authorization", "value": f"Bearer {os.environ.get('KRI_KEY', '')}"},
    },
]

# ── Meta-Router: Probe Settings ──────────────────────────────────────
PROBER_INTERVAL_SECONDS = 30       # how often to probe routers for health
PROBE_TIMEOUT = 15.0              # seconds per health check request (must be > /v1/models latency with 1000+ models)
PROBE_MAX_WORKERS = 5              # thread pool size for parallel probes
MAX_MODEL_CATALOG = 500            # cap on catalog size after dedup

# ── Meta-Router: State ───────────────────────────────────────────────
ROUTER_STATE_FILE = Path.home() / ".9router" / "router_state.json"

# ── Sanitize routers at load time ─────────────────────────────────────
from sanitizer import sanitize_routers_config
DOWNSTREAM_ROUTERS = sanitize_routers_config(_RAW_DOWNSTREAM_ROUTERS)