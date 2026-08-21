"""Provider Health Daemon — configuration."""

import json
import os
from pathlib import Path

# ── Paths (needed by key fallback below) ─────────────────────────────
OPCODE_CONFIG = Path.home() / ".config" / "opencode" / "opencode.json"


def _load_opencode_api_key(provider: str) -> str:
    """Fallback: read a provider apiKey from opencode.json when env is unset.

    The daemon is often started via run.sh from a shell without the provider
    keys exported (NINEROUTER_KEY / KRI_KEY). opencode.json always has them
    under provider.<name>.options.apiKey — reuse that so the meta-router
    never forwards an empty `Authorization: Bearer ` to upstream routers.
    """
    try:
        cfg = json.loads(OPCODE_CONFIG.read_text())
        return (cfg.get("provider", {}).get(provider, {}).get("options", {}).get("apiKey") or "").strip()
    except (OSError, json.JSONDecodeError):
        return ""


# ── Network ──────────────────────────────────────────────────────────
HEALTH_PROXY_PORT = int(os.environ.get("HEALTH_PROXY_PORT", "20131"))
NINEROUTER_URL = os.environ.get("NINEROUTER_URL", "http://localhost:20128")
NINEROUTER_KEY = os.environ.get("NINEROUTER_KEY", "").strip() or _load_opencode_api_key("9router")
KRI_KEY = os.environ.get("KRI_KEY", "").strip() or _load_opencode_api_key("kiro")

# Must stay BELOW typical client timeouts so the proxy can time out a slow
# upstream, fall back, and still answer (regression guard: bugs-erros-opencode.md 2026-08-14).
UPSTREAM_TIMEOUT = float(os.environ.get("UPSTREAM_TIMEOUT", "60"))

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

# ── Provider Discovery (auto-detect new providers from the 9router hub) ──
DISCOVERY_INTERVAL_SECONDS = 120  # seconds between new-provider discovery passes

# ── Proxy auto-management (renew pool + apply to new providers/accounts) ──
PROXY_CHECK_INTERVAL_SECONDS = 300      # tick: detect connections missing a proxy
PROXY_REFRESH_INTERVAL_SECONDS = 6 * 3600  # full pool refetch + reassign cadence
PROXY_SOURCES = [
    "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=20000&country=all&ssl=all&anonymity=all",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
]
PROXY_FETCH_TIMEOUT = 15.0            # seconds per proxy-list source fetch
PROXY_TEST_URL = "https://api.ipify.org"  # HTTPS-CONNECT probe target
PROXY_TEST_TIMEOUT = 8.0              # seconds per proxy probe
PROXY_MIN_POOL = 10                   # minimum alive proxies before applying a pool
PROXY_PROBE_MAX_WORKERS = 50          # thread pool for parallel proxy probes

# ── Global fallback (exhaust the full catalog on 5xx) ────────────────
MAX_FALLBACK_RETRIES = 2  # catalog fallback attempts per request (bounded: avoids latency bombs)
GLOBAL_REFRESH_MIN_INTERVAL = 15.0  # seconds between forced catalog refetches (GET /v1/models is expensive)

# ── Response cache (retry dedup) ─────────────────────────────────────
RESPONSE_CACHE_TTL = 30  # seconds a cached response is served
RESPONSE_CACHE_MAX_BYTES = 8 * 1024 * 1024  # 8MB cap on total cached bytes

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
        "timeout": 30.0,  # OmniRoute /v1/models latency ~13.5s; 15s caused false cooldowns (2026-08-14)
        "auth": {"header": "Authorization", "value": f"Bearer {NINEROUTER_KEY}"},
    },
    {
        "name": "Kiro",
        "url": "http://localhost:20129",
        "priority": 2,
        "weight": 1,
        "health_check_path": "/v1/models",
        "timeout": 5.0,
        "auth": {"header": "Authorization", "value": f"Bearer {KRI_KEY}"},
    },
]

# ── Meta-Router: Probe Settings ──────────────────────────────────────
PROBER_INTERVAL_SECONDS = 30       # how often to probe routers for health
PROBE_TIMEOUT = 30.0              # seconds per health check request (must be > /v1/models latency with 1000+ models; 15s falsely failed OmniRoute at 13.5s latency - 2026-08-14)
PROBE_MAX_WORKERS = 5              # thread pool size for parallel probes
MAX_MODEL_CATALOG = 500            # cap on catalog size after dedup

# ── Meta-Router: State ───────────────────────────────────────────────
ROUTER_STATE_FILE = Path.home() / ".9router" / "router_state.json"

# ── Sanitize routers at load time ─────────────────────────────────────
from sanitizer import sanitize_routers_config
DOWNSTREAM_ROUTERS = sanitize_routers_config(_RAW_DOWNSTREAM_ROUTERS)