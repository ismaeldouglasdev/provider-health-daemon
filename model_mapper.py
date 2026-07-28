"""Model ID mapper — translate generic model names to router-specific IDs.

Different downstream routers expose models under different paths/names.
This module maps a logical model name (e.g. "llama-3.3-70b") to the
concrete model ID that each router understands.

Architecture:
  RouterModelMap = {router_name: {generic_alias: "router-specific/model-id"}}

Usage:
  mapper = ModelMapper()
  router_id = mapper.resolve("9router", "llama-3.3-70b")
  # -> "@cf/meta/llama-3.3-70b-instruct-fp8-fast"

  # Best-effort: try all routers for a model, return first match
  any_id = mapper.resolve_any("llama-3.3-70b")
  # -> ("9router", "@cf/meta/llama-3.3-70b-instruct-fp8-fast")
"""

from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)

# ── Registry: each router's known model IDs ─────────────────────────────
# First key = router name, second key = generic alias (user-visible)
# Keep sorted: most-capable/mainline alias first.
_ROUTER_MODELS: dict[str, dict[str, str]] = {
    "9router": {
        "llama-3.3-70b": "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
        "deepseek-r1": "@cf/deepseek-ai/deepseek-r1-distill-qwen-32b",
        "kimi-k2.6": "@cf/moonshotai/kimi-k2.6",
        "qwen2.5-coder-32b": "@cf/qwen/qwen2.5-coder-32b-instruct",
        "gpt-oss:120b": "ollama/gpt-oss:120b",
        "qwen3.5": "ollama/qwen3.5",
    },
    "OmniRoute": {
        "llama-3.3-70b": "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
        "deepseek-r1": "@cf/deepseek-ai/deepseek-r1-distill-qwen-32b",
        "kimi-k2.6": "@cf/moonshotai/kimi-k2.6",
        "qwen2.5-coder-32b": "@cf/qwen/qwen2.5-coder-32b-instruct",
        "minimax-m3": "nvidia/minimaxai/minimax-m3",
        "glm-5.2": "nvidia/z-ai/glm-5.2",
    },
    "Kiro": {
        "llama-3.3-70b": "groq/llama-3.3-70b-versatile",
        "deepseek-r1": "groq/deepseek-r1-distill-llama-70b",
        "qwen2.5-coder-32b": "groq/qwen-2.5-coder-32b",
    },
}


class ModelMapper:
    """Resolve generic model aliases to router-specific model IDs."""

    def __init__(self, router_map: Optional[dict[str, dict[str, str]]] = None):
        self._router_map = router_map or _ROUTER_MODELS

    def resolve(self, router_name: str, alias: str) -> Optional[str]:
        """Resolve a model alias for a specific router.

        Args:
            router_name: Downstream router name (e.g. "9router", "Kiro").
            alias: Generic model alias (e.g. "llama-3.3-70b").

        Returns:
            Router-specific model ID, or None if unknown.
        """
        router_models = self._router_map.get(router_name)
        if router_models is None:
            return None
        return router_models.get(alias)

    def resolve_any(self, alias: str) -> Optional[tuple[str, str]]:
        """Resolve a model alias across all routers — returns first match.

        Returns:
            (router_name, model_id) tuple, or None if no router knows this alias.
        """
        for router_name, models in self._router_map.items():
            if alias in models:
                return router_name, models[alias]
        return None

    def register(self, router_name: str, alias: str, model_id: str) -> None:
        """Register or update a model alias for a router."""
        self._router_map.setdefault(router_name, {})[alias] = model_id

    def aliases_for(self, router_name: str) -> dict[str, str]:
        """Return all known aliases for a router."""
        return dict(self._router_map.get(router_name, {}))

    def routers_for(self, alias: str) -> list[tuple[str, str]]:
        """Return all routers that expose a given alias."""
        return [
            (rn, mid)
            for rn, models in self._router_map.items()
            for ga, mid in models.items()
            if ga == alias
        ]

    def reverse_map(self, model_id: str) -> Optional[str]:
        """Reverse-lookup: given a router-specific model ID, return the generic alias.

        Useful for normalizing responses — strip the router prefix so the
        downstream client sees a consistent model name.
        """
        for router_name, models in self._router_map.items():
            for alias, mid in models.items():
                if mid == model_id or model_id.endswith(mid.split("/")[-1]):
                    return alias
        return None
