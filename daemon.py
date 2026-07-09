"""Provider Health Daemon — main entrypoint.

Runs three tasks in parallel:
1. HTTP proxy (health-aware + prompt limiting)
2. Health cleanup (promote expired cooldowns → probing)
3. Error log monitor (parse 9router error.log for missed errors)
"""

import logging
import os
import sys
import threading
import time
from pathlib import Path

# Add prompt-limiter to path for reuse
PROMPT_LIMITER_DIR = Path.home() / "Desktop" / "code_study" / "MeusProjetos" / "prompt-limiter"
if str(PROMPT_LIMITER_DIR) not in sys.path:
    sys.path.insert(0, str(PROMPT_LIMITER_DIR))

from config import HEALTH_PROXY_PORT, PROBER_INTERVAL_MINUTES
from health_registry import HealthRegistry
from error_parser import parse_log_line, parse_access_log_line
from proxy_handler import HealthProxyServer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s HEALTH %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("health-daemon")


# ── Error log monitor ───────────────────────────────────────────────

LOG_PATH = Path.home() / ".9router" / "logs" / "error.log"
ACCESS_LOG_PATH = Path.home() / ".9router" / "logs" / "access.log"


def monitor_logs(registry: HealthRegistry):
    """Tail error.log and feed parsed errors to registry."""
    if not LOG_PATH.exists():
        log.warning("9router error.log not found, log monitoring disabled")
        return

    # Probing thread: periodically re-check cooled-down providers
    def prober():
        interval = PROBER_INTERVAL_MINUTES * 60
        while True:
            time.sleep(interval)
            try:
                promoted = registry.cleanup_expired()
                if promoted:
                    summary = registry.status_summary()
                    log.info(f"Probe: {promoted} expired → probing. "
                             f"Status: h={summary['by_status']['healthy']} "
                             f"c={summary['by_status']['cooldown']} "
                             f"p={summary['by_status']['probing']}")
            except Exception as e:
                log.error(f"Prober error: {e}")

    threading.Thread(target=prober, daemon=True, name="prober").start()

    # Log tailer
    last_size = LOG_PATH.stat().st_size if LOG_PATH.exists() else 0

    while True:
        try:
            current_size = LOG_PATH.stat().st_size

            if current_size > last_size:
                with open(LOG_PATH, "r") as f:
                    f.seek(last_size)
                    new_lines = f.read()

                for line in new_lines.split("\n"):
                    line = line.strip()
                    if not line:
                        continue
                    parsed = parse_log_line(line)
                    if parsed:
                        provider = parsed.get("provider_hint", "unknown")
                        model = parsed.get("model_hint")
                        registry.mark_error(
                            provider=provider,
                            error_info=parsed,
                            model=model if parsed.get("model_specific") else None,
                        )

                last_size = current_size

            time.sleep(5)
        except Exception as e:
            log.error(f"Log monitor error: {e}")
            time.sleep(10)


# ── Main ─────────────────────────────────────────────────────────────

def main():
    registry = HealthRegistry()

    # Start log monitor thread
    threading.Thread(
        target=monitor_logs,
        args=(registry,),
        daemon=True,
        name="log-monitor",
    ).start()

    # Start HTTP proxy (blocking)
    server = HealthProxyServer(port=HEALTH_PROXY_PORT)
    server.registry = registry  # share registry with monitor
    server.run()


if __name__ == "__main__":
    main()