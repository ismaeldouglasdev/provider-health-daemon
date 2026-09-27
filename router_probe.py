import json
import json
import threading
import time
import urllib.error
import urllib.request
from config import PROBER_INTERVAL_SECONDS, PROBE_TIMEOUT, PROBE_MAX_WORKERS
from concurrent.futures import ThreadPoolExecutor, as_completed


class RouterProbe:
    def __init__(self, registry):
        self._registry = registry
        self._running = False
        self._stop_event = threading.Event()

    def probe_router(self, router):
        try:
            url = router.url.rstrip("/") + router.health_check_path
            req = urllib.request.Request(url, method="GET")
            # The lightweight /health endpoints are local liveness pages and
            # may reject Authorization headers (OmniRoute returns 404 when an
            # auth header is attached). Only authenticated model probes need
            # the router credential.
            if router.auth and router.health_check_path not in ("/health", "/v1/health", "/api/health"):
                req.add_header(router.auth["header"], router.auth["value"])
            req.add_header("Accept", "application/json")
            with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as resp:
                if resp.status != 200:
                    self._registry.mark_unhealthy(router.name, f"http_{resp.status}")
                    return False
                body = resp.read().decode()
                data = json.loads(body)
            # /health is intentionally used for downstream liveness because
            # /v1/models can take tens of seconds on OmniRoute. Preserve the
            # last known model catalog when the lightweight health endpoint is
            # being probed; combo routing still gets its model pool from the
            # SmartRouter/catalog cache.
            if router.health_check_path in ("/health", "/v1/health", "/api/health"):
                models = list(router.models or [])
            elif "data" in data and isinstance(data["data"], list):
                models = [item.get("id") for item in data["data"] if isinstance(item, dict) and item.get("id")]
            elif isinstance(data, list):
                models = [item.get("id") for item in data if isinstance(item, dict) and item.get("id")]
            else:
                models = []
            self._registry.mark_healthy(router.name, models)
            return True
        except json.JSONDecodeError:
            # /health is a liveness endpoint, not a model catalog. A 200 HTML
            # page is still a successful router health signal; preserve the
            # last known model catalog rather than marking the router dead.
            if router.health_check_path in ("/health", "/v1/health", "/api/health"):
                self._registry.mark_healthy(router.name, list(router.models or []))
                return True
            self._registry.mark_unhealthy(router.name, "bad_json")
            return False
        except (urllib.error.URLError, urllib.error.HTTPError, ConnectionError, TimeoutError, OSError) as e:
            self._registry.mark_unhealthy(router.name, type(e).__name__)
            return False

    def probe_all(self):
        routers = self._registry.get_all_routers()
        results = {"healthy": 0, "unhealthy": 0}
        with ThreadPoolExecutor(max_workers=PROBE_MAX_WORKERS) as pool:
            fut_to_router = {pool.submit(self.probe_router, r): r for r in routers}
            for fut in as_completed(fut_to_router):
                if fut.result():
                    results["healthy"] += 1
                else:
                    results["unhealthy"] += 1
        return results

    def probe_loop(self, callback=None):
        self._stop_event.clear()
        self._running = True
        while self._running:
            results = self.probe_all()
            if callback:
                callback(results)
            if self._stop_event.wait(PROBER_INTERVAL_SECONDS):
                break
        self._running = False

    def stop(self):
        self._running = False
        self._stop_event.set()
