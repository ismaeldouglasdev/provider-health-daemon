"""Provider Health Daemon — main entrypoint.

Runs in parallel:
1. HTTP proxy (health-aware + smart routing + prompt limiting)
2. Access log listener (parse 9router access.log for real-time metrics)
3. Health cleanup (promote expired cooldowns to probing)
4. Dashboard server (real-time web UI)
5. Router health probes (downstream router health checks)
6. Self-audit / observability (structured logs, metrics, health summaries)
"""

import atexit
import json
import logging
import logging.handlers
import os
import signal
import subprocess
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

# Ensure local dir takes priority over prompt-limiter (which has its own smart_router.py)
_local_dir = str(Path(__file__).parent)
if _local_dir in sys.path:
    sys.path.remove(_local_dir)
sys.path.insert(0, _local_dir)

from config import (
    HEALTH_PROXY_PORT, PROBER_INTERVAL_MINUTES, ACCESS_LOG_PATH, DASHBOARD_PORT,
    DOWNSTREAM_ROUTERS, ROUTER_STATE_FILE, NINEROUTER_URL, NINEROUTER_KEY,
    PROBE_TIMEOUT,
)
from smart_router import SmartRouter
from health_registry import HealthRegistry
from router_registry import RouterRegistry
from router_probe import RouterProbe
from meta_router import MetaRouterSelector
from model_catalog import ModelCatalog
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
    root = logging.getLogger()
    root.handlers = []
    root.setLevel(level)

    # Stdout
    sh = logging.StreamHandler()
    if json_logs:
        sh.setFormatter(StructuredFormatter())
    else:
        sh.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s HEALTH %(levelname)s %(message)s",
                datefmt="%H:%M:%S",
            )
        )
    root.addHandler(sh)

    # Rotating file
    log_dir = Path.home() / ".9router"
    log_dir.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(
        log_dir / "health-daemon.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=3,
    )
    fh.setFormatter(StructuredFormatter())
    root.addHandler(fh)


log = logging.getLogger("health-daemon")

shutdown_event = threading.Event()


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
    while not shutdown_event.is_set():
        if shutdown_event.wait(interval_seconds):
            break
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

    while not shutdown_event.is_set():
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


def monitor_logs(registry: HealthRegistry, router_names: set[str] | None = None):
    """Tail error.log and feed parsed errors to registry.

    router_names: set of lowercased router names to exclude from provider tracking.
                  Routers are tracked by RouterRegistry, not HealthRegistry.
    """
    if not LOG_PATH.exists():
        log.warning("9router error.log not found, log monitoring disabled")
        return
    router_names = router_names or set()

    def prober():
        interval = PROBER_INTERVAL_MINUTES * 60
        while not shutdown_event.is_set():
            if shutdown_event.wait(interval):
                break
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

    def recovery_prober():
        """Periodically test cooldown/probing providers to auto-recover them."""
        combo_models = SmartRouter.get_default_combos()
        provider_models: dict[str, list[str]] = {}
        for cm in combo_models:
            p = cm.split("/")[0]
            provider_models.setdefault(p, []).append(cm)

        interval = max(PROBER_INTERVAL_MINUTES * 60, 60)
        while not shutdown_event.is_set():
            if shutdown_event.wait(interval):
                break
            try:
                summary = registry.status_summary()
                cooldown_count = summary["by_status"].get("cooldown", 0)
                probing_count = summary["by_status"].get("probing", 0)
                if cooldown_count + probing_count == 0:
                    continue

                import urllib.request, json as _json

                for provider, entry in list(registry.snapshot().get("providers", {}).items()):
                    status = entry.get("status", "")
                    if status not in ("cooldown", "probing"):
                        continue
                    if entry.get("failures", 0) >= HealthRegistry.MAX_FAILURES:
                        continue  # don't probe permanently disabled
                    if provider not in provider_models:
                        continue  # don't know what model to test

                    test_models = provider_models[provider]
                    for test_model in test_models:
                        probe_body = _json.dumps({
                            "model": test_model,
                            "messages": [{"role": "user", "content": "ping"}],
                            "max_tokens": 1,
                        }).encode()
                        try:
                            probe_req = urllib.request.Request(
                                f"{NINEROUTER_URL}/v1/chat/completions",
                                data=probe_body,
                                headers={
                                    "Content-Type": "application/json",
                                    "Authorization": f"Bearer {NINEROUTER_KEY}",
                                },
                                method="POST",
                            )
                            with urllib.request.urlopen(probe_req, timeout=10) as probe_resp:
                                if probe_resp.status == 200:
                                    log.info(
                                        "Auto-recovery: provider responded healthy",
                                        extra={
                                            "event": "provider_recovered",
                                            "provider": provider,
                                            "model": test_model,
                                        },
                                    )
                                    registry.mark_healthy(provider)
                                    METRICS.cooldowns_promoted += 1
                                    break
                        except (urllib.error.URLError, urllib.error.HTTPError):
                            pass
                        time.sleep(2)  # rate limit between probes

            except Exception as e:
                log.error(f"Recovery prober error: {e}")

    threading.Thread(target=recovery_prober, daemon=True, name="recovery-prober").start()

    threading.Thread(
        target=audit_loop,
        args=(registry, 60),
        daemon=True,
        name="audit",
    ).start()

    last_size = LOG_PATH.stat().st_size if LOG_PATH.exists() else 0

    while not shutdown_event.is_set():
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
                        if provider.lower() in router_names:
                            continue
                        model = parsed.get("model_hint")
                        registry.mark_error(
                            provider=provider,
                            error_info=parsed,
                            model=model if parsed.get("model_specific") else None,
                        )
                        METRICS.cooldowns_applied += 1
                        parsed_info = parsed.get("cooldown") or parsed
                        log.warning(
                            "9router error parsed",
                            extra={
                                "event": "error_parsed",
                                "provider": provider,
                                "model": model,
                                "error_type": parsed_info.get("type"),
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
    while not shutdown_event.is_set():
        try:
            transitions = alerter.check_transitions(registry.snapshot())
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

    # ── Single-instance lock (prevents EADDRINUSE crash-loop class) ───
    # A second daemon (e.g. spawned by systemd while an orphan holds the
    # ports) exits cleanly with code 0, so systemd Restart=always does NOT
    # enter a restart loop. The lock is released automatically on exit.
    import fcntl
    _lock_path = Path.home() / ".9router" / "daemon.lock"
    try:
        _lock_path.parent.mkdir(parents=True, exist_ok=True)
        _lock_fd = open(_lock_path, "w")
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fd.write(f"{os.getpid()}\n")
        _lock_fd.flush()
    except OSError:
        log.warning(
            "Another provider-health-daemon instance is already running "
            f"(lock {_lock_path} held). Exiting cleanly (code 0).",
            extra={"event": "single_instance_skip", "pid": os.getpid()},
        )
        sys.exit(0)

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

    # ── Startup validation ────────────────────────────────────────────
    if not DOWNSTREAM_ROUTERS:
        log.warning("DOWNSTREAM_ROUTERS is empty — no downstream routers configured")
    else:
        meta_self = f"http://localhost:{HEALTH_PROXY_PORT}"
        for r in DOWNSTREAM_ROUTERS:
            if r["url"].rstrip("/") == meta_self:
                log.warning(f"Router '{r['name']}' URL points to self ({meta_self}) — misconfiguration")

    # ── Probe timeout sanity check (single-shot, boot only) ──────────
    # Measures real latency of each router health endpoint; warns when the
    # configured timeout leaves no headroom — the exact failure mode that
    # produced 914 false failures on OmniRoute (2.0s timeout vs 2.7s latency).
    import urllib.request as _urlreq
    for r in DOWNSTREAM_ROUTERS:
        timeout = r.get("timeout", PROBE_TIMEOUT)
        try:
            url = r["url"].rstrip("/") + r.get("health_check_path", "/v1/models")
            req = _urlreq.Request(url, method="GET")
            auth = r.get("auth")
            if auth:
                req.add_header(auth["header"], auth["value"])
            req.add_header("Accept", "application/json")
            t0 = time.monotonic()
            with _urlreq.urlopen(req, timeout=timeout) as resp:
                resp.read()
            latency = time.monotonic() - t0
            if timeout < latency * 1.5:
                log.warning(
                    f"Probe timeout for router '{r['name']}' is {timeout}s but actual latency is "
                    f"{latency:.2f}s — health checks will falsely fail. Increase 'timeout' in config.py.",
                    extra={
                        "event": "probe_timeout_underestimate",
                        "router": r["name"],
                        "timeout": timeout,
                        "latency": round(latency, 3),
                    },
                )
            else:
                log.info(
                    f"Startup probe OK for router '{r['name']}' ({latency:.2f}s, timeout {timeout}s)",
                    extra={"event": "startup_probe_ok", "router": r["name"], "latency": round(latency, 3)},
                )
        except Exception as e:
            log.warning(
                f"Startup probe failed for router '{r['name']}': {type(e).__name__} {e}",
                extra={"event": "startup_probe_failed", "router": r["name"], "error": type(e).__name__},
            )

    # ── Router-of-routers infrastructure (Wave 2) ─────────────────────
    meta_registry = RouterRegistry(DOWNSTREAM_ROUTERS)
    meta_registry.load_state()  # Restore previous router health states
    meta_selector = MetaRouterSelector(meta_registry)
    model_catalog = ModelCatalog(meta_registry)
    
    # Start router health probe loop in background
    router_probe = RouterProbe(meta_registry)

    _router_history_dir = Path.home() / ".9router" / "metrics_history"
    _routers_ever_healthy = False
    _all_down_alerted = False
    _down_cycles = 0

    def on_router_probe(results: dict) -> None:
        """Log, persist history, and alert when every router goes down."""
        nonlocal _routers_ever_healthy, _all_down_alerted, _down_cycles
        routers = meta_registry.get_all_routers()
        healthy = [r for r in routers if r.health_status == "healthy"]

        try:
            _router_history_dir.mkdir(parents=True, exist_ok=True)
            hist_path = _router_history_dir / f"routers-{datetime.now().strftime('%Y-%m-%d')}.jsonl"
            with open(hist_path, "a") as f:
                for r in routers:
                    f.write(json.dumps({
                        "ts": time.time(),
                        "router": r.name,
                        "status": r.health_status,
                        "models_count": len(r.models or []),
                        "failure_count": r.failure_count,
                        "cooldown_until": r.cooldown_until,
                    }) + "\n")
        except Exception as e:
            log.debug(f"Router history write failed: {e}")

        log.debug(f"Router probe: {results}", extra={"event": "router_probe", "results": results})

        if healthy:
            if _all_down_alerted:
                log.info("All routers recovered", extra={"event": "routers_recovered"})
            _all_down_alerted = False
            _down_cycles = 0
            _routers_ever_healthy = True
            return

        if not routers:
            return
        _down_cycles += 1
        should_alert = (_routers_ever_healthy and not _all_down_alerted) or _down_cycles == 3
        if should_alert:
            _all_down_alerted = True
            detail = ", ".join(f"{r.name}={r.health_status}" for r in routers)
            log.error(
                f"ALL routers down ({_down_cycles} cycles): {detail}",
                extra={"event": "all_routers_down", "routers": [r.name for r in routers]},
            )
            try:
                subprocess.run(
                    ["notify-send", "-a", "caelestia", "-u", "critical",
                     "9Router: ALL routers down", detail],
                    timeout=3,
                )
            except Exception:
                pass
            try:
                inc_path = Path.home() / ".9router" / "logs" / "incidents.jsonl"
                inc_path.parent.mkdir(parents=True, exist_ok=True)
                with open(inc_path, "a") as f:
                    f.write(json.dumps({
                        "ts": time.time(),
                        "event": "all_routers_down",
                        "routers": [{"name": r.name, "status": r.health_status} for r in routers],
                    }) + "\n")
            except Exception as e:
                log.debug(f"Incident log write failed: {e}")

    probe_thread = threading.Thread(
        target=router_probe.probe_loop,
        kwargs={"callback": on_router_probe},
        daemon=True,
        name="router-probe",
    )
    probe_thread.start()
    log.info("Router probe started", extra={"event": "probe_start"})
    
    # Register atexit handler to persist router state on shutdown
    def persist_router_state():
        try:
            meta_registry.save_state()
            log.info("Router state persisted", extra={"event": "persist_router_state"})
        except Exception as e:
            log.error(f"Failed to persist router state: {e}")
    atexit.register(persist_router_state)

    # ── Shared metrics store ──────────────────────────────────────────
    # Used by proxy handler, access log monitor, and dashboard
    shared_metrics = MetricsStore()

    # ── Metrics persistence (snapshot history for charts) ────────────
    metrics_persist = MetricsPersistence()

    def metrics_snapshot_loop():
        while not shutdown_event.is_set():
            try:
                providers = {}
                if hasattr(registry, 'snapshot'):
                    providers = registry.snapshot().get("providers", {})
                global_stats = shared_metrics.get_all_stats(300).get("global", {})
                metrics_persist.snapshot(providers, global_stats)
            except Exception as e:
                log.error(f"Snapshot error: {e}")
            if shutdown_event.wait(metrics_persist.interval):
                break

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
    router_names = {r["name"].lower() for r in DOWNSTREAM_ROUTERS}
    threading.Thread(
        target=monitor_logs,
        args=(registry, router_names),
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

    # ── Signal handlers for graceful shutdown ─────────────────────────
    def _signal_handler(signum, frame):
        if shutdown_event.is_set():
            return  # already shutting down
        log.info(f"Signal {signum} received, shutting down gracefully...")
        shutdown_event.set()
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    # Start dashboard server (web UI)
    dashboard = DashboardServer(port=DASHBOARD_PORT)
    dashboard.metrics_store = shared_metrics
    dashboard.health_registry = registry
    dashboard.metrics_persistence = metrics_persist
    dashboard.router_registry = meta_registry  # RouterRegistry for /api/routers + /api/providers/aggregated
    dashboard.model_catalog = model_catalog
    dashboard.start()

    # Start HTTP proxy in background thread so main thread can wait on shutdown
    server = HealthProxyServer(port=HEALTH_PROXY_PORT, metrics_store=shared_metrics)
    server.registry = registry
    server.meta_registry = meta_registry
    server.meta_selector = meta_selector
    server.audit = METRICS
    proxy_thread = threading.Thread(target=server.run, daemon=False, name="proxy")
    proxy_thread.start()

    log.info("Daemon ready — waiting for signals", extra={"event": "daemon_ready"})
    shutdown_event.wait()  # block until SIGTERM/SIGINT

    # ── Graceful shutdown sequence ────────────────────────────────────
    log.info("Shutting down...", extra={"event": "shutdown_start"})

    # 1. Stop probe loop
    router_probe.stop()
    probe_thread.join(timeout=5)

    # 2. Persist router state
    try:
        meta_registry.save_state()
        log.info("Router state persisted", extra={"event": "persist_router_state"})
    except Exception as e:
        log.error(f"Failed to persist router state: {e}")

    # 3. Stop proxy server
    server.shutdown()
    proxy_thread.join(timeout=5)

    # 4. Stop dashboard server
    if dashboard.server:
        dashboard.server.shutdown()
    if dashboard.thread:
        dashboard.thread.join(timeout=5)

    log.info("Shutdown complete", extra={"event": "shutdown_done"})


if __name__ == "__main__":
    main()
