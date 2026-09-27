"""Canonical model-ID mapping across downstream routers.

Keeps router-specific prefixes out of client-visible response model IDs while
preserving router-level aliases (main-rr, combo-*, kr/auto) verbatim.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

log = logging.getLogger(__name__)

_ROUTER_PREFIXES = {
    "9router": ("groq/", "nvidia/", "kr/", "anthropic/", "ollama/"),
    "omniroute": ("groq/", "nvidia/", "kr/", "anthropic/", "ollama/"),
    "kiro": (),
}

_ROUTER_LEVEL_IDS = {
    "main-rr", "combo-round-robin", "combo-fast", "combo-thinking", "kr/auto"
}


class ModelIdMapper:
    """Map router-specific model IDs to stable canonical IDs and back."""

    def __init__(self, mapping_config: dict | None = None):
        self._prefixes: dict[str, tuple[str, ...]] = dict(_ROUTER_PREFIXES)
        self._catalogs: dict[str, dict[str, str]] = {}
        self._explicit: dict[str, dict[str, str]] = defaultdict(dict)
        self._collisions: list[dict[str, Any]] = []
        if mapping_config:
            self._load_config(mapping_config)

    def _load_config(self, config: dict) -> None:
        for router, value in config.items():
            if not isinstance(value, dict):
                continue
            prefixes = value.get("prefixes")
            if isinstance(prefixes, (list, tuple)):
                self.register_mapping(router, prefixes)
            catalog = value.get("catalog")
            if isinstance(catalog, dict):
                self._catalogs[self._router_key(router)] = dict(catalog)
            mappings = value.get("mappings")
            if isinstance(mappings, dict):
                for canonical, router_id in mappings.items():
                    if isinstance(canonical, str) and isinstance(router_id, str):
                        self._explicit[self._router_key(router)][canonical] = router_id
    @staticmethod
    def _router_key(router_name: str) -> str:
        return str(router_name or "").strip().lower()

    def register_mapping(self, router_name: str, prefixes) -> None:
        """Register prefix stripping rules for a router."""
        self._prefixes[self._router_key(router_name)] = tuple(
            p if p.endswith("/") else p + "/"
            for p in prefixes
            if isinstance(p, str) and p
        )

    def register_catalog(self, router_name: str, models) -> None:
        """Register a router model catalog for reverse lookup."""
        self._catalogs[self._router_key(router_name)] = dict(models or {})

    def to_canonical(self, router_model_id: str) -> str:
        """Convert a router-specific ID to its canonical client-visible ID."""
        if not isinstance(router_model_id, str) or not router_model_id:
            return router_model_id
        if router_model_id in _ROUTER_LEVEL_IDS:
            return router_model_id

        for prefixes in self._prefixes.values():
            for prefix in prefixes:
                if router_model_id.startswith(prefix):
                    return router_model_id[len(prefix):]
        return router_model_id

    def to_router_specific(self, canonical_id: str, router_name: str) -> str:
        """Resolve a canonical ID to an ID exposed by a specific router."""
        if not isinstance(canonical_id, str) or not canonical_id:
            return canonical_id
        if canonical_id in _ROUTER_LEVEL_IDS:
            return canonical_id

        router = self._router_key(router_name)
        explicit = self._explicit.get(router, {})
        if canonical_id in explicit:
            return explicit[canonical_id]

        catalog = self._catalogs.get(router, {})
        if canonical_id in catalog:
            return canonical_id
        for model_id in catalog:
            if self.to_canonical(model_id) == canonical_id:
                return model_id

        for prefix in self._prefixes.get(router, ()):
            candidate = prefix + canonical_id
            if not catalog or candidate in catalog:
                return candidate

        return canonical_id

    def get_mapping_stats(self) -> dict:
        """Return mapping/collision counters for diagnostics."""
        mapped = sum(len(v) for v in self._catalogs.values()) + sum(
            len(v) for v in self._explicit.values()
        )
        canonical_owners: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for router, catalog in self._catalogs.items():
            for model_id in catalog:
                canonical_owners[self.to_canonical(model_id)].append(
                    (router, model_id)
                )

        collisions = 0
        for canonical, owners in canonical_owners.items():
            distinct = {model_id for _, model_id in owners}
            if len(owners) > 1 and len(distinct) > 1:
                collisions += 1
                log.warning("Model ID collision for %r: %s", canonical, owners)

        self._collisions = [
            {"canonical": canonical, "models": owners}
            for canonical, owners in canonical_owners.items()
            if len(owners) > 1 and len({model_id for _, model_id in owners}) > 1
        ]
        return {
            "mapped_models": mapped,
            "unmapped_models": 0,
            "collisions": collisions,
        }
