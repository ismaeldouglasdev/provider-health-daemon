"""Provider Health Daemon — main entrypoint.

Runs in parallel:
1. HTTP proxy (health-aware + smart routing + prompt limiting)
2. Access log listener (parse 9router access.log for real-time metrics)
3. Health cleanup (promote expired cooldowns to probing)
4. Dashboard server (real-time web UI)
5. Self-audit / observability (structured logs, metrics, health summaries)
"""

import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROMPT_LIMITER_DIR = Path.home() / "Desktop" / "code_study" / "MeusProjetos" / "prompt-limiter"
if str(PROMPT_LIMITER_DIR) not in sys.path:
    sys.path.insert(0, str(PROMPT_LIMITER_DIR))

from config import HEALTH_PROXY_PORT, PROBER_INTERVAL_MINUTES, ACCESS_LOG_PATH, DASHBOARD_PORT
from health_registry import HealthRegistry
from error_parser import parse_log_line
from proxy_handler import HealthProxyServer
from access_parser import parse_line as parse_access_line
from dashboard import DashboardServer
from metrics_store import MetricsStore, RequestRecord
from metrics_persistence import MetricsPersistence

try:
    from alerter import Alerter
    HAS_ALERTER = True
except ImportError:
    Alerter = None  # type: ignore
    HAS_ALERTER = False
    import sys as _sys
    _sys.stderr.write("WARNING: alerter module not available — desktop notifications disabled\n")

# ── Structured logging ────────────────────────────────────────────────

class StructuredFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in {
                "name", "msg", "args", "created", "filename", "funcName",
                "levelname", "levelno", "lineno", "module", "msecs",
                "message", "name", "pathname", "process", "processName",
                "relativeCreated", "thread", "threadName", "exc_info",
                "exc_text", "stack_info",
            }:
                base[key] = value
        return json.dumps(base, ensure_ascii=False, default=str)


def setup_logging(json_logs: bool = True, level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    if json_logs:
        handler.setFormatter(StructuredFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s HEALTH %(levelname)s %(message)s",
                datefmt="%H:%M:%S",
            )
        )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)


log = logging.getLogger("health-daemon")


# ── Audit / metrics ──────────────────────────────────────────────────

@dataclass(slots=True)
class AuditMetrics:
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    requests_proxied: int = 0
    requests_blocked: int = 0
    errors_parsed: int = 0
    cooldowns_applied: int = 0
    cooldowns_promoted: int = 0
    last_summary_log: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "uptime_seconds": (datetime.now(timezone.utc) - self.started_at).total_seconds(),
            "requests_proxied": self.requests_proxied,
            "requests_blocked": self.requests_blocked,
            "errors_parsed": self.errors_parsed,
            "cooldowns_applied": self.cooldowns_applied,
            "cooldowns_promoted": self.cooldowns_promoted,
        }


METRICS = AuditMetrics()


def log_health_summary(registry: HealthRegistry) -> None:
    summary = registry.status_summary()
    metrics = METRICS.to_dict()
    log.info(
        "Health summary",
        extra={
            "event": "health_summary",
            "providers": summary["by_status"],
            "expired_ready": summary["expired_ready"],
            "metrics": metrics,
        },
    )


def audit_loop(registry: HealthRegistry, interval_seconds: int = 60) -> None:
    while True:
        time.sleep(interval_seconds)
        try:
            log_health_summary(registry)
        except Exception as e:
            log.error(f"Audit loop error: {e}")


# ── Access Log Listener ──────────────────────────────────────────────

def monitor_access_log(metrics_store):
    """Tail 9router access.log and feed parsed events to metrics store."""
    if not ACCESS_LOG_PATH.exists():
        log.warning(f"Access log not found at {ACCESS_LOG_PATH}, monitoring disabled")
        return

    log.info(f"Monitoring access log: {ACCESS_LOG_PATH}")
    last_size = ACCESS_LOG_PATH.stat().st_size
    pending_request = {}  # model -> start_time tracking

    while True:
        try:
            current_size = ACCESS_LOG_PATH.stat().st_size
            if current_size > last_size:
                with open(ACCESS_LOG_PATH, "rb") as f:
                    f.seek(last_size)
                    new_data = f.read()
                    try:
                        text = new_data.decode("utf-8", errors="replace")
                    except Exception:
                        text = new_data.decode("latin-1", errors="replace")

                for line in text.split("\n"):
                    line = line.strip()
                    if not line:
                        continue

                    event = parse_access_line(line)
                    if event is None:
                        continue

                    etype = event.get("type")

                    if etype == "request":
                        model = event.get("model", "")
                        provider = event.get("provider", "")
                        pending_request[model] = {
                            "provider": provider,
                            "model": model,
                            "start_time": time.time(),
                        }

                    elif etype == "done":
                        dur = event.get("duration_ms", 0)
                        ttft = event.get("ttft_ms", 0)
                        tokens_in = event.get("tokens_in", 0)
                        tokens_out = event.get("tokens_out", 0)
                        tokens_cache = event.get("tokens_cache", 0)

                        for model_key, req in list(pending_request.items()):
                            provider = req["provider"]
                            record = RequestRecord(
                                timestamp=req["start_time"],
                                provider=provider,
                                model=req["model"],
                                duration_ms=dur,
                                ttft_ms=ttft,
                                tokens_in=tokens_in,
                                tokens_out=tokens_out,
                                tokens_cache=tokens_cache,
                                success=True,
                            )
                            metrics_store.record_request(record)
                            del pending_request[model_key]
                            break

                    elif etype == "combo_result":
                        if not event.get("success"):
                            record = RequestRecord(
                                timestamp=time.time(),
                                provider=event.get("provider", "unknown"),
                                model=event.get("model", ""),
                                duration_ms=0,
                                success=False,
                                error_type="combo_failed",
                            )
                            metrics_store.record_request(record)

                    elif etype == "error":
                        record = RequestRecord(
                            timestamp=time.time(),
                            provider=event.get("provider", "unknown"),
                            model="",
                            duration_ms=0,
                            success=False,
                            error_type=f"http_{event.get('status', 0)}",
                        )
                        metrics_store.record_request(record)

                last_size = current_size

            time.sleep(3)
        except Exception as e:
            log.error(f"Access log monitor error: {e}")
            time.sleep(10)


# ── Error log monitor ───────────────────────────────────────────────

LOG_PATH = Path.home() / ".9router" / "logs" / "error.log"


def monitor_logs(registry: HealthRegistry):
    """Tail error.log and feed parsed errors to registry."""
    if not LOG_PATH.exists():
        log.warning("9router error.log not found, log monitoring disabled")
        return

    def prober():
        interval = PROBER_INTERVAL_MINUTES * 60
        while True:
            time.sleep(interval)
            try:
                promoted = registry.cleanup_expired()
                if promoted:
                    METRICS.cooldowns_promoted += promoted
                    summary = registry.status_summary()
                    log.info(
                        "Probe promoted expired cooldowns",
                        extra={
                            "event": "probe_promoted",
                            "promoted": promoted,
                            "status": summary["by_status"],
                        },
                    )
            except Exception as e:
                log.error(f"Prober error: {e}")

    threading.Thread(target=prober, daemon=True, name="prober").start()

    threading.Thread(
        target=audit_loop,
        args=(registry, 60),
        daemon=True,
        name="audit",
    ).start()

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
                        METRICS.errors_parsed += 1
                        provider = parsed.get("provider_hint", "unknown")
                        model = parsed.get("model_hint")
                        registry.mark_error(
                            provider=provider,
                            error_info=parsed,
                            model=model if parsed.get("model_specific") else None,
                        )
                        log.warning(
                            "9router error parsed",
                            extra={
                                "event": "error_parsed",
                                "provider": provider,
                                "model": model,
                                "error_type": parsed.get("cooldown", {}).get("type"),
                                "permanent": parsed.get("permanent"),
                                "model_specific": parsed.get("model_specific"),
                            },
                        )

                last_size = current_size

            time.sleep(5)
        except Exception as e:
            log.error(f"Log monitor error: {e}")
            time.sleep(10)


# ── Alerter ──────────────────────────────────────────────────────────

def alerter_loop(registry):
    """Monitor provider health transitions and send desktop notifications."""
    if not HAS_ALERTER:
        return
    alerter = Alerter()
    while True:
        try:
            transitions = alerter.check_transitions(registry._data)
            for t in transitions:
                alerter.alert(t)
                log.info(
                    "Alert: %s %s -> %s",
                    t["provider"], t["from"], t["to"],
                    extra={"event": "alerter", "transition": t},
                )
            time.sleep(10)
        except Exception as e:
            log.error(f"Alerter error: {e}")
            time.sleep(30)


# ── Main ─────────────────────────────────────────────────────────────

def main():
    setup_logging(json_logs=True, level=logging.INFO)

    log.info(
        "Provider Health Daemon starting",
        extra={
            "event": "daemon_start",
            "port": HEALTH_PROXY_PORT,
            "dashboard_port": DASHBOARD_PORT,
            "ninerouter_url": os.environ.get("NINEROUTER_URL", "http://localhost:20128"),
            "pid": os.getpid(),
        },
    )

    registry = HealthRegistry()

    # ── Shared metrics store ──────────────────────────────────────────
    # Used by proxy handler, access log monitor, and dashboard
    shared_metrics = MetricsStore()

    # ── Metrics persistence (snapshot history for charts) ────────────
    metrics_persist = MetricsPersistence()

    def metrics_snapshot_loop():
        while True:
            try:
                providers = {}
                if hasattr(registry, '_data'):
                    providers = registry._data.get("providers", {})
                global_stats = shared_metrics.get_all_stats(300).get("global", {})
                metrics_persist.snapshot(providers, global_stats)
            except Exception as e:
                log.error(f"Snapshot error: {e}")
            time.sleep(metrics_persist.interval)

    threading.Thread(
        target=metrics_snapshot_loop,
        daemon=True,
        name="metrics-snapshot",
    ).start()

    threading.Thread(
        target=monitor_access_log,
        args=(shared_metrics,),
        daemon=True,
        name="access-log-monitor",
    ).start()

    # ── Start error log monitor ──────────────────────────────────────
    threading.Thread(
        target=monitor_logs,
        args=(registry,),
        daemon=True,
        name="log-monitor",
    ).start()

    # ── Start alerter (desktop notifications) ────────────────────────
    if HAS_ALERTER:
        threading.Thread(
            target=alerter_loop,
            args=(registry,),
            daemon=True,
            name="alerter",
        ).start()

    # ── Start dashboard server (web UI) ──────────────────────────────
    dashboard = DashboardServer(port=DASHBOARD_PORT)
    dashboard.metrics_store = shared_metrics
    dashboard.health_registry = registry
    dashboard.metrics_persistence = metrics_persist
    dashboard.start()

    # ── Start HTTP proxy (blocking) ──────────────────────────────────
    server = HealthProxyServer(port=HEALTH_PROXY_PORT, metrics_store=shared_metrics)
    server.registry = registry
    server.run()


if __name__ == "__main__":
    main()
