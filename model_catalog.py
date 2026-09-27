import time

from config import MAX_MODEL_CATALOG
from model_id_mapper import ModelIdMapper


def _escape_model_id(model_id: str) -> str:
    if not model_id or not isinstance(model_id, str):
        return ""
    return model_id.replace("<", "&lt;").replace(">", "&gt;").strip()


class ModelCatalog:
    def __init__(self, registry):
        self._registry = registry
        self._mapper = ModelIdMapper()
        self._cache = {}

    def refresh_from_registry(self):
        """Rebuild and retain a canonical catalog snapshot from the registry."""
        self._cache = self.get_catalog()
        return self._cache

    def get_models(self):
        """Return the public OpenAI-compatible model-list response."""
        catalog = self.get_catalog()
        now = int(time.time())
        data = []
        for model_id, entry in catalog.items():
            origins = [
                _escape_model_id(origin)
                for origin in entry.get("router_origins", [])
                if isinstance(origin, str) and origin
            ]
            owned_by = origins[0] if origins else "proxy"
            data.append({
                "id": model_id,
                "object": "model",
                "created": now,
                "owned_by": owned_by,
                "router_origins": origins,
            })
        return {"object": "list", "data": data}

    def get_catalog(self):
        raw_catalog = self._registry.get_model_catalog()
        priorities = {
            router.name: router.priority
            for router in self._registry.get_all_routers()
        }
        catalog = {}
        for model_id, entry in raw_catalog.items():
            clean_source = _escape_model_id(model_id)
            if not clean_source:
                continue
            clean_id = _escape_model_id(self._mapper.to_canonical(clean_source))
            if not clean_id:
                continue
            origins = list(entry.get("router_origins", []))
            if clean_id not in catalog:
                catalog[clean_id] = {
                    "model_id": clean_id,
                    "router_origins": origins,
                    "_best_priority": min(
                        (priorities.get(router, float("inf")) for router in origins),
                        default=float("inf"),
                    ),
                }
            else:
                for router in origins:
                    if router not in catalog[clean_id]["router_origins"]:
                        catalog[clean_id]["router_origins"].append(router)
                catalog[clean_id]["_best_priority"] = min(
                    catalog[clean_id]["_best_priority"],
                    *(priorities.get(router, float("inf")) for router in origins),
                )

        # The plan's cap is intentionally applied after canonical dedup and
        # by router priority, not alphabetically. A higher-priority router's
        # models therefore survive the public catalog cap first.
        ordered = sorted(
            catalog.values(),
            key=lambda item: (item["_best_priority"], item["model_id"]),
        )
        if MAX_MODEL_CATALOG and len(ordered) > MAX_MODEL_CATALOG:
            ordered = ordered[:MAX_MODEL_CATALOG]

        return {
            item["model_id"]: {
                "model_id": item["model_id"],
                "router_origins": item["router_origins"],
            }
            for item in ordered
        }

    def get_models_list(self):
        return [entry for entry in self.get_catalog().values()]

    def get_models_by_router(self, router_name: str) -> list[str]:
        router = self._registry.get_router(router_name)
        if router is None:
            return []
        return sorted(
            {
                self._mapper.to_canonical(model_id)
                for model_id in router.models
                if isinstance(model_id, str) and model_id
            }
        )

    def get_model_ids(self) -> list[str]:
        return list(self.get_catalog().keys())

    def count_models(self) -> int:
        return len(self.get_catalog())
