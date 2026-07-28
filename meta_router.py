import threading


class ServiceUnavailable(Exception):
    pass


class MetaRouterSelector:
    def __init__(self, registry):
        self._registry = registry
        self._index = 0
        self._lock = threading.Lock()

    def select_router(self):
        with self._lock:
            healthy = self._registry.get_healthy_routers()
            if not healthy:
                raise ServiceUnavailable("all routers unavailable")
            idx = self._index % len(healthy)
            self._index = idx + 1
            return healthy[idx]

    def on_success(self, router_name):
        pass

    def on_failure(self, router_name, error_type=None):
        self._registry.mark_unhealthy(router_name, error_type)
        return self._fallback()

    def _fallback(self):
        healthy = self._registry.get_healthy_routers()
        if not healthy:
            raise ServiceUnavailable("all routers unavailable (after fallback)")
        return healthy[0]
