from config import MAX_MODEL_CATALOG
from sanitizer import sanitize_model_id


class ModelCatalog:
    def __init__(self, registry):
        self._registry = registry

    def get_catalog(self):
        catalog = self._registry.get_model_catalog()
        if MAX_MODEL_CATALOG and len(catalog) > MAX_MODEL_CATALOG:
            keys = sorted(catalog.keys())[:MAX_MODEL_CATALOG]
            catalog = {k: catalog[k] for k in keys}
        
        sanitized = {}
        for model_id, entry in catalog.items():
            clean_id = sanitize_model_id(model_id)
            if clean_id:
                sanitized[clean_id] = {
                    "model_id": clean_id,
                    "router_origins": entry.get("router_origins", []),
                }
        return sanitized

    def get_models_list(self):
        catalog = self.get_catalog()
        return [entry for entry in catalog.values()]

    def get_model_ids(self):
        return sorted(self.get_catalog().keys())

    def count_models(self):
        return len(self.get_catalog())
