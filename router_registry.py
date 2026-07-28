import json
import time
from config import ROUTER_STATE_FILE


class RouterState:
    __slots__ = (
        "name", "url", "priority", "weight", "health_status",
        "models", "last_success", "last_failure", "cooldown_until",
        "consecutive_probes_ok", "failure_count", "auth", "health_check_path", "timeout",
    )

    def __init__(self, name, url, priority=1, weight=1, health_check_path="/v1/models",
                 timeout=2.0, auth=None):
        self.name = name
        self.url = url
        self.priority = priority
        self.weight = weight
        self.health_check_path = health_check_path
        self.timeout = timeout
        self.auth = auth
        self.health_status = "unknown"
        self.models = []
        self.last_success = None
        self.last_failure = None
        self.cooldown_until = None
        self.consecutive_probes_ok = 0
        self.failure_count = 0


class RouterRegistry:
    def __init__(self, routers_config):
        self._routers = {}
        for cfg in routers_config:
            s = RouterState(
                name=cfg["name"],
                url=cfg["url"],
                priority=cfg.get("priority", 1),
                weight=cfg.get("weight", 1),
                health_check_path=cfg.get("health_check_path", "/v1/models"),
                timeout=cfg.get("timeout", 2.0),
                auth=cfg.get("auth"),
            )
            self._routers[s.name] = s

    def get_router(self, name):
        return self._routers.get(name)

    def get_all_routers(self):
        return list(self._routers.values())

    def get_healthy_routers(self):
        healthy = [r for r in self._routers.values() if r.health_status == "healthy"]
        healthy.sort(key=lambda r: (r.priority, -r.weight))
        return healthy

    def mark_healthy(self, name, models):
        r = self._routers.get(name)
        if r is None:
            return
        r.models = list(models) if models else []
        now = time.time()
        r.last_success = now
        r.cooldown_until = None
        if r.health_status == "unknown" or r.health_status == "cooldown":
            r.health_status = "probing"
            r.consecutive_probes_ok = 1
        elif r.health_status == "probing":
            r.consecutive_probes_ok += 1
            if r.consecutive_probes_ok >= 2:
                r.health_status = "healthy"
                r.consecutive_probes_ok = 0
        elif r.health_status == "healthy":
            r.consecutive_probes_ok = 0

    def mark_unhealthy(self, name, error_type=None):
        r = self._routers.get(name)
        if r is None:
            return
        r.health_status = "cooldown"
        r.last_failure = time.time()
        r.failure_count += 1
        backoff = min(60 * (2 ** (r.failure_count - 1)), 86400)
        r.cooldown_until = r.last_failure + backoff

    def mark_probing(self, name):
        r = self._routers.get(name)
        if r is None:
            return
        r.health_status = "probing"

    def refresh_models_from_router(self, name, models):
        r = self._routers.get(name)
        if r is None:
            return
        r.models = list(models) if models else []

    def get_model_catalog(self):
        catalog = {}
        for r in self._routers.values():
            for model_id in r.models:
                if model_id in catalog:
                    if r.name not in catalog[model_id]["router_origins"]:
                        catalog[model_id]["router_origins"].append(r.name)
                else:
                    catalog[model_id] = {
                        "model_id": model_id,
                        "router_origins": [r.name],
                    }
        return catalog

    def get_provider_aggregation(self):
        """Aggregate provider info across all router model catalogs.

        Groups models from all routers by provider (the prefix before '/' in model ID),
        showing which routers expose which models for each provider.

        Returns a dict keyed by provider name:
          {provider_name: {provider, models: [{model_id, routers: [...]}], router_count, total_models}}
        """
        aggregated: dict[str, dict] = {}

        for r in self._routers.values():
            for model_id in r.models:
                provider = model_id.split("/")[0] if "/" in model_id else "unknown"
                if provider not in aggregated:
                    aggregated[provider] = {
                        "provider": provider,
                        "models": {},
                        "router_count": 0,
                        "total_models": 0,
                        "routers": set(),
                    }
                aggregated[provider]["routers"].add(r.name)
                if model_id not in aggregated[provider]["models"]:
                    aggregated[provider]["models"][model_id] = {
                        "model_id": model_id,
                        "routers": [],
                    }
                if r.name not in aggregated[provider]["models"][model_id]["routers"]:
                    aggregated[provider]["models"][model_id]["routers"].append(r.name)

        # Convert sets to sorted lists and count
        result = {}
        for provider, info in aggregated.items():
            result[provider] = {
                "provider": provider,
                "models": sorted(
                    info["models"].values(),
                    key=lambda m: m["model_id"],
                ),
                "router_count": len(info["routers"]),
                "total_models": len(info["models"]),
                "routers": sorted(info["routers"]),
            }
        return result

    def save_state(self):
        data = []
        for r in self._routers.values():
            data.append({
                "name": r.name,
                "url": r.url,
                "priority": r.priority,
                "weight": r.weight,
                "health_status": r.health_status,
                "models": r.models,
                "last_success": r.last_success,
                "last_failure": r.last_failure,
                "cooldown_until": r.cooldown_until,
                "consecutive_probes_ok": r.consecutive_probes_ok,
                "failure_count": r.failure_count,
                "auth": r.auth,
                "health_check_path": r.health_check_path,
                "timeout": r.timeout,
            })
        ROUTER_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(ROUTER_STATE_FILE, "w") as f:
            json.dump(data, f, indent=2, default=str)

    def load_state(self):
        if not ROUTER_STATE_FILE.exists():
            return
        try:
            with open(ROUTER_STATE_FILE) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return
        restored = {entry["name"]: entry for entry in data}
        for r in self._routers.values():
            saved = restored.get(r.name)
            if saved:
                r.health_status = saved.get("health_status", "unknown")
                r.cooldown_until = saved.get("cooldown_until")
                r.failure_count = saved.get("failure_count", 0)
