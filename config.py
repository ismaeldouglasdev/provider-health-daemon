"""Provider Health Daemon — configuration."""

import os
from pathlib import Path

# ── Network ──────────────────────────────────────────────────────────
HEALTH_PROXY_PORT = int(os.environ.get("HEALTH_PROXY_PORT", "20131"))
NINEROUTER_URL = os.environ.get("NINEROUTER_URL", "http://localhost:20128")
NINEROUTER_KEY = os.environ.get("NINEROUTER_KEY", "")

# ── Paths ────────────────────────────────────────────────────────────
HEALTH_FILE = Path.home() / ".9router" / "health.json"

# Node config for loading from oh-my-openagent.json
AGENT_CONFIG = Path.home() / ".config" / "opencode" / "oh-my-openagent.json"
MODEL_LIMITS_FILE = Path.home() / ".config" / "opencode" / "model_limits.json"

# ── Prompt Limiter integration ───────────────────────────────────────
PROMPT_LIMITER_DIR = Path.home() / "Desktop" / "code_study" / "MeusProjetos" / "prompt-limiter"

# ── Cooldown defaults ────────────────────────────────────────────────
PROBER_INTERVAL_MINUTES = 5  # how often to probe cooled-down providers
MAX_COOLDOWN_HOURS = 24  # cap exponential backoff