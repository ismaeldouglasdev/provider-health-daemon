from config import MAX_MODEL_CATALOG


class ModelCatalog:
    def __init__(self, registry):
        self._registry = registry

    def get_catalog(self):
        catalog = self._registry.get_model_catalog()
        if MAX_MODEL_CATALOG and len(catalog) > MAX_MODEL_CATALOG:
            keys = sorted(catalog.keys())[:MAX_MODEL_CATALOG]
            catalog = {k: catalog[k] for k in keys}
        return catalog

    def get_models_list(self):
        catalog = self.get_catalog()
        return [entry for entry in catalog.values()]

    def get_model_ids(self):
        return sorted(self.get_catalog().keys())

    def count_models(self):
        return len(self.get_catalog())
