import json
import time
import urllib.error
import urllib.request
from config import PROBER_INTERVAL_SECONDS, PROBE_TIMEOUT, PROBE_MAX_WORKERS
from concurrent.futures import ThreadPoolExecutor, as_completed


class RouterProbe:
    def __init__(self, registry):
        self._registry = registry
        self._running = False

    def probe_router(self, router):
        try:
            url = router.url.rstrip("/") + router.health_check_path
            req = urllib.request.Request(url, method="GET")
            if router.auth:
                req.add_header(router.auth["header"], router.auth["value"])
            req.add_header("Accept", "application/json")
            resp = urllib.request.urlopen(req, timeout=PROBE_TIMEOUT)
            if resp.status != 200:
                self._registry.mark_unhealthy(router.name, f"http_{resp.status}")
                return False
            body = resp.read().decode()
            data = json.loads(body)
            if "data" in data and isinstance(data["data"], list):
                models = []
                for item in data["data"]:
                    mid = item.get("id")
                    if mid:
                        models.append(mid)
            elif isinstance(data, list):
                models = [item.get("id") for item in data if isinstance(item, dict) and item.get("id")]
            else:
                models = []
            self._registry.mark_healthy(router.name, models)
            return True
        except json.JSONDecodeError:
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
        self._running = True
        while self._running:
            results = self.probe_all()
            if callback:
                callback(results)
            time.sleep(PROBER_INTERVAL_SECONDS)

    def stop(self):
        self._running = False
