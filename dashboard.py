"""Dashboard web server — serves real-time metrics UI + API endpoints.

Endpoints:
  GET /dashboard/          → Dashboard HTML page
  GET /api/metrics         → JSON metrics snapshot
  GET /api/metrics/sse     → Server-Sent Events stream
  GET /api/metrics/history → Time-series history for charting
  GET /api/history         → Recent request history
  GET /api/providers       → Provider list with health status
  GET /api/providers/ready → Healthy providers with reliability score
  GET /api/export/csv      → Export history as CSV
  GET /api/alert/webhook   → Get webhook config
  POST /api/alert/webhook  → Set webhook URL
"""

import csv
import io
import json
import logging
import os
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional
from socketserver import ThreadingMixIn

from metrics_store import MetricsStore
from config import DASHBOARD_PORT

log = logging.getLogger(__name__)

HERE = Path(__file__).parent
TEMPLATES = HERE / "templates"


class ThreadingDashboardHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class DashboardHandler(BaseHTTPRequestHandler):
    """HTTP handler that serves dashboard UI + JSON API."""

    metrics_store: MetricsStore = None
    health_registry = None
    router_registry = None  # for aggregated providers view
    metrics_persistence = None
    webhook_url = ""
    sse_clients: list = []
    _sse_lock = threading.Lock()

    @classmethod
    def _clean_stale_sse(cls):
        now = time.time()
        stale = [c for c in cls.sse_clients if isinstance(c, str) and c.startswith("client_")]
        cls.sse_clients.clear()
        cls.sse_clients.extend(stale[-50:])

    # Silence default logging
    def log_message(self, fmt, *args):
        pass

    def _send_json(self, data: dict, status: int = 200):
        body = json.dumps(data, indent=2, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str, status: int = 200):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, path: Path):
        if path.exists() and path.is_file():
            body = path.read_bytes()
            ext = path.suffix
            content_types = {
                ".html": "text/html; charset=utf-8",
                ".js": "application/javascript",
                ".css": "text/css",
                ".png": "image/png",
                ".svg": "image/svg+xml",
                ".ico": "image/x-icon",
            }
            ctype = content_types.get(ext, "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
        else:
            self._send_json({"error": "not found"}, 404)

    def do_GET(self):
        path = self.path.split("?")[0]

        # ── Dashboard UI ─────────────────────────────────────────────
        if path == "/" or path == "/dashboard/" or path == "/dashboard":
            self._serve_file(TEMPLATES / "dashboard.html")
            return

        if path.startswith("/dashboard/static/"):
            static_file = TEMPLATES / path[18:]  # remove /dashboard/static/
            self._serve_file(static_file)
            return

        # ── API endpoints ────────────────────────────────────────────

        # SSE stream
        if path == "/api/metrics/sse":
            self._handle_sse()
            return

        # Full metrics snapshot
        if path == "/api/metrics":
            window = int(self._get_param("window", "300"))
            data = self._build_metrics(window)
            self._send_json(data)
            return

        # Provider list with health
        if path == "/api/providers":
            data = self._build_providers()
            self._send_json(data)
            return

        # Ready providers (healthy only, with reliability score)
        if path == "/api/providers/ready":
            data = self._build_providers()
            provs = data.get("providers", {})
            ready = [
                {"name": name, **info}
                for name, info in provs.items()
                if info.get("status") == "healthy" and name != "unknown"
            ]
            total = sum(1 for n in provs if n != "unknown")
            self._send_json({
                "count": len(ready),
                "total": total,
                "providers": ready,
            })
            return

        # Aggregated providers across all routers
        if path == "/api/providers/aggregated":
            if self.router_registry:
                agg = self.router_registry.get_provider_aggregation()
                self._send_json({"providers": agg, "total": len(agg)})
            else:
                self._send_json({"providers": {}, "total": 0})
            return

        # Account-access errors (auth/credit/subscription) — separated from health
        if path == "/api/access-errors":
            window = int(self._get_param("window", "300"))
            data = self.metrics_store.get_access_errors(window) if self.metrics_store else {
                "window_seconds": window, "total": 0, "providers": 0, "by_provider": {},
            }
            self._send_json(data)
            return

        # Recent request history
        if path == "/api/history":
            limit = int(self._get_param("limit", "50"))
            records = self.metrics_store.get_recent_requests(limit) if self.metrics_store else []
            self._send_json({"records": records})
            return

        # Metrics history (time-series for charts)
        if path == "/api/metrics/history":
            minutes = int(self._get_param("minutes", "60"))
            resolution = int(self._get_param("resolution", "60"))
            if self.metrics_persistence:
                history = self.metrics_persistence.get_history(minutes=minutes, resolution=resolution)
                latest = self.metrics_persistence.latest_summary()
            else:
                history = []
                latest = {}
            self._send_json({"history": history, "latest": latest})
            return

        # CSV export
        if path == "/api/export/csv":
            minutes = int(self._get_param("minutes", "60"))
            if self.metrics_persistence:
                csv_data = self.metrics_persistence.export_csv(minutes=minutes)
            else:
                csv_data = "timestamp,datetime,healthy,probing,cooldown,disabled,total,failures\n"
            body = csv_data.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="metrics-{int(time.time())}.csv"')
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
            return

        # Webhook config (GET)
        if path == "/api/alert/webhook":
            self._send_json({
                "webhook_url": DashboardHandler.webhook_url,
                "enabled": bool(DashboardHandler.webhook_url),
            })
            return

        if path == "/api/health":
            checks = {"server": "ok"}
            if self.health_registry:
                try:
                    self.health_registry.status_summary()
                    checks["registry"] = "ok"
                except Exception:
                    checks["registry"] = "error"
            if self.metrics_store:
                checks["metrics"] = "ok"
            if self.router_registry:
                checks["routers"] = "ok"
            overall = "ok" if all(v == "ok" for v in checks.values()) else "degraded"
            self._send_json({"status": overall, "checks": checks})
            return

        if path == "/api/config":
            import config as cfg
            sanitized = {}
            for key in dir(cfg):
                if key.isupper() and not key.startswith("_"):
                    val = getattr(cfg, key)
                    if isinstance(val, (str, int, float, bool, list)):
                        sanitized[key] = val
            self._send_json({"config": sanitized})
            return

        if path == "/api/routers":
            routers_data = []
            if self.router_registry:
                for router in self.router_registry.get_all_routers():
                    routers_data.append({
                        "name": router.name,
                        "url": router.url,
                        "status": router.health_status,
                        "models_count": len(router.models),
                        "last_success": router.last_success,
                        "last_failure": router.last_failure,
                        "cooldown_until": router.cooldown_until,
                        "failure_count": router.failure_count,
                        "priority": router.priority,
                        "weight": router.weight,
                    })
            self._send_json(routers_data)
            return

        if path == "/api/admin/router":
            providers = {}
            if self.health_registry:
                reg = self.health_registry.snapshot().get("providers", {})
                for name, entry in reg.items():
                    providers[name] = {
                        "status": entry.get("status", "unknown"),
                        "failures": entry.get("failures", 0),
                        "models": entry.get("models", []),
                        "reason": entry.get("reason"),
                    }
            self._send_json({
                "healthy_count": sum(1 for p in providers.values() if p["status"] == "healthy"),
                "total": sum(1 for n in providers if n != "unknown"),
                "default_provider": "combo-round-robin",
                "providers": providers,
            })
            return

        self._send_json({"error": "not found"}, 404)

    def _get_param(self, name: str, default: str = "") -> str:
        qs = self.path.split("?", 1)
        if len(qs) < 2:
            return default
        for part in qs[1].split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                if k == name:
                    return v
        return default

    def _build_metrics(self, window_seconds: int) -> dict:
        """Build full metrics snapshot."""
        stats = {}
        if self.metrics_store:
            stats = self.metrics_store.get_all_stats(window_seconds)

        health_summary = {}
        if self.health_registry:
            health_summary = self.health_registry.status_summary()

        return {
            "timestamp": time.time(),
            "window": window_seconds,
            "metrics": stats,
            "health": health_summary,
        }

    def _build_providers(self) -> dict:
        """Build provider list with health + metrics."""
        providers = {}
        now = time.time()

        if self.health_registry:
            reg = self.health_registry.snapshot().get("providers", {})
            for name, entry in reg.items():
                until = entry.get("until")
                remaining = 0
                if until:
                    try:
                        from datetime import datetime, timezone
                        until_dt = datetime.fromisoformat(until)
                        remaining = max(0, (until_dt - datetime.now(timezone.utc)).total_seconds())
                    except (ValueError, TypeError):
                        pass

                # Get metrics
                pm = None
                if self.metrics_store:
                    pm = self.metrics_store.get_provider_stats(name, 300)

                # Calculate reliability score (0-100)
                score = 100.0
                if pm:
                    if pm.get("error_rate", 0) > 0.1:
                        score -= 20
                    if pm.get("error_rate", 0) > 0.5:
                        score -= 30
                    latency_penalty = min(pm.get("avg_latency_ms", 0) / 100, 50)
                    score -= latency_penalty
                    if pm.get("failed", 0) > 10:
                        score -= 20
                    if pm.get("total_requests", 0) > 10:
                        score += 10
                score = max(0, min(100, int(round(score))))
                risk = "low" if score >= 80 else "medium" if score >= 50 else "high"

                providers[name] = {
                    "status": entry.get("status", "unknown"),
                    "failures": entry.get("failures", 0),
                    "cooldown_remaining": int(remaining),
                    "reason": entry.get("reason"),
                    "models": entry.get("models", []),
                    "metrics": pm,
                    "reliability_score": score,
                    "risk_level": risk,
                }

        return {
            "timestamp": now,
            "providers": providers,
            "daemon_uptime": self._get_uptime(),
        }

    def _get_uptime(self) -> float:
        try:
            with open("/proc/self/stat") as f:
                parts = f.read().split()
                start_time_ticks = int(parts[21])
                clock_ticks = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
                uptime = time.time() - (start_time_ticks / clock_ticks)
                return uptime
        except Exception:
            return 0.0

    def _handle_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        client_id = f"client_{time.time()}"
        DashboardHandler.sse_clients.append(client_id)
        DashboardHandler._clean_stale_sse()
        prev_state: dict[str, str] = {}

        try:
            while True:
                data = self._build_providers()
                providers = data.get("providers", {})
                payload = json.dumps(data, default=str)

                # Detect and emit provider_change events
                curr_state = {n: p.get("status", "unknown") for n, p in providers.items() if n != "unknown"}
                for name, status in curr_state.items():
                    old = prev_state.get(name)
                    if old is not None and old != status:
                        reason = providers.get(name, {}).get("reason") or "status change"
                        change = json.dumps({
                            "provider": name, "from": old, "to": status, "reason": reason,
                        }, default=str)
                        try:
                            self.wfile.write(f"event: provider_change\ndata: {change}\n\n".encode())
                        except (BrokenPipeError, ConnectionResetError, OSError):
                            break
                prev_state = curr_state

                # Emit provider_update event + backward-compat bare data
                try:
                    self.wfile.write(f"event: provider_update\ndata: {payload}\n\n".encode())
                    self.wfile.write(f"data: {payload}\n\n".encode())
                except (BrokenPipeError, ConnectionResetError, OSError):
                    break

                time.sleep(2)
        finally:
            try:
                DashboardHandler.sse_clients.remove(client_id)
            except ValueError:
                pass

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_POST(self):
        path = self.path.split("?")[0]

        if path == "/api/alert/webhook":
            content_len = int(self.headers.get("Content-Length", 0))
            if content_len > 0:
                body = self.rfile.read(content_len)
                try:
                    data = json.loads(body)
                    url = data.get("url", "").strip()
                    if url and not url.startswith("http"):
                        self._send_json({"error": "invalid URL"}, 400)
                        return
                    DashboardHandler.webhook_url = url
                    log.info("Webhook URL updated: %s", url or "(cleared)")
                    self._send_json({"webhook_url": url, "enabled": bool(url)})
                except (json.JSONDecodeError, UnicodeDecodeError):
                    self._send_json({"error": "invalid JSON"}, 400)
            else:
                self._send_json({"error": "empty body"}, 400)
            return

        if path == "/api/alert/webhook/test":
            url = DashboardHandler.webhook_url
            if not url:
                self._send_json({"error": "no webhook configured"}, 400)
                return
            import urllib.request
            test_payload = json.dumps({
                "event": "test",
                "message": "This is a test webhook from provider-health-daemon",
                "timestamp": time.time(),
            }).encode()
            try:
                req = urllib.request.Request(url, data=test_payload, headers={"Content-Type": "application/json"})
                resp = urllib.request.urlopen(req, timeout=10)
                self._send_json({"status": "sent", "code": resp.getcode()})
                log.info("Test webhook sent to %s (status %d)", url, resp.getcode())
            except Exception as e:
                log.warning("Test webhook to %s failed: %s", url, e)
                self._send_json({"status": "failed", "error": str(e)}, 502)
            return

        # Admin: manually reactivate a provider from cooldown
        if path == "/api/admin/reactivate":
            provider_name = ""
            
            content_len = int(self.headers.get("Content-Length", 0))
            if content_len > 0:
                body = self.rfile.read(content_len)
                try:
                    data = json.loads(body)
                    provider_name = data.get("provider", "").strip()
                except (json.JSONDecodeError, UnicodeDecodeError):
                    self._send_json({"error": "invalid JSON"}, 400)
                    return
            
            if not provider_name:
                provider_name = self._get_param("provider", "").strip()
            
            if not provider_name:
                self._send_json({"error": "provider name required (POST body or ?provider=name)"}, 400)
                return
            
            if not self.health_registry:
                self._send_json({"error": "health registry not available"}, 503)
                return
            
            self.health_registry.force_healthy(provider_name)
            provider_status = self.health_registry.snapshot().get("providers", {}).get(provider_name, {})
            log.info("Admin reactivated provider: %s", provider_name)
            self._send_json({
                "status": "ok",
                "provider": provider_name,
                "new_status": provider_status.get("status", "healthy"),
            })
            return

        # Admin: get router info (current routing state)
        if path == "/api/admin/router":
            providers = {}
            if self.health_registry:
                reg = self.health_registry.snapshot().get("providers", {})
                for name, entry in reg.items():
                    providers[name] = {
                        "status": entry.get("status", "unknown"),
                        "failures": entry.get("failures", 0),
                        "models": entry.get("models", []),
                        "reason": entry.get("reason"),
                    }
            self._send_json({
                "healthy_count": sum(1 for p in providers.values() if p["status"] == "healthy"),
                "total": sum(1 for n in providers if n != "unknown"),
                "default_provider": "combo-round-robin",
                "providers": providers,
            })
            return

        self._send_json({"error": "not found"}, 404)


class DashboardServer:
    """Dashboard server that runs in a background thread."""

    def __init__(self, port: int = DASHBOARD_PORT):
        self.port = port
        self.metrics_store = MetricsStore()
        self.health_registry = None
        self.metrics_persistence = None
        self.router_registry = None  # RouterRegistry for aggregated providers
        self.server: Optional[ThreadingDashboardHTTPServer] = None
        self.thread: Optional[threading.Thread] = None

    def get_handler(self):
        ms = self.metrics_store
        hr = self.health_registry
        mp = self.metrics_persistence
        rr = self.router_registry

        class Handler(DashboardHandler):
            pass

        Handler.metrics_store = ms
        Handler.health_registry = hr
        Handler.metrics_persistence = mp
        Handler.router_registry = rr
        return Handler

    def start(self):
        handler = self.get_handler()

        class ThreadingServer(ThreadingMixIn, HTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        try:
            self.server = ThreadingServer(("0.0.0.0", self.port), handler)
        except OSError as e:
            if e.errno == 98:  # EADDRINUSE — another process holds this port
                log.error(
                    f"Dashboard port {self.port} already in use — a second daemon instance "
                    "is likely running. Check 'ss -tlnp | grep 20132' and kill the orphan.",
                    extra={"event": "dashboard_port_conflict", "port": self.port},
                )
            raise

        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
            name="dashboard",
        )
        self.thread.start()
        log.info(f"📊 Dashboard → http://localhost:{self.port}/dashboard/")
        log.info(f"   API → http://localhost:{self.port}/api/metrics")

    def stop(self):
        if self.server:
            self.server.shutdown()
            log.info("Dashboard server stopped")
