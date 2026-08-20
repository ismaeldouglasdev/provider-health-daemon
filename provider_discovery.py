"""Provider discovery module for health daemon.

Discovers and probes upstream providers from the 9router CLI admin API,
normalizes provider names to catalog prefixes, and can probe individual
models for health status.
"""

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Any

from catalog_sync import compute_cli_token
from config import NINEROUTER_KEY, NINEROUTER_URL

log = logging.getLogger(__name__)

from provider_aliases import (
    PROVIDER_ALIAS_MAP as _PROVIDER_ALIAS_MAP,
    PREFIX_TO_ALIAS as _PREFIX_TO_ALIAS,
    normalize_provider,
)


def fetch_provider_connections(base_url: Optional[str] = None) -> Dict[str, Dict]:
    """Fetch configured provider connections from 9router admin API.

    Performs GET {base_url}/api/providers with x-9r-cli-token auth header from
    compute_cli_token(). Returns a dict mapping normalized prefixes to consolidated status.

    Args:
        base_url: 9router base URL (defaults to NINEROUTER_URL from config).

    Returns:
        {
          "prefix": {
            "status": str,
            "errorCode": int | None,
            "backoffLevel": int,
            "connection_count": int,
            "model_locks": dict,
            "connections": list
          }
        }
        Empty dict {} on any error; never raises exceptions.
    """
    url = f"{base_url or NINEROUTER_URL}/api/providers"
    token = compute_cli_token()
    if not token:
        log.warning("fetch_provider_connections: no CLI token (machine-id/cli-secret missing)")
        return {}

    headers = {"Content-Type": "application/json", "x-9r-cli-token": token}
    request = urllib.request.Request(url, headers=headers, method="GET")

    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status != 200:
                log.debug(f"fetch_provider_connections: HTTP {response.status} on {url}")
                return {}

            body = response.read()
            data = json.loads(body) if body else {}

            if not isinstance(data, dict) or "connections" not in data:
                log.debug(f"fetch_provider_connections: unexpected response structure or empty connections")
                return {}

            connections_list = data["connections"]
            if not isinstance(connections_list, list):
                log.debug(f"fetch_provider_connections: connections is not a list")
                return {}

            aggregated: Dict[str, Dict] = {}
            for conn in connections_list:
                if not isinstance(conn, dict):
                    continue

                provider_field = conn.get("provider", "")
                if not provider_field:
                    continue

                prefix = normalize_provider(provider_field)

                if prefix not in aggregated:
                    aggregated[prefix] = {
                        "status": conn.get("testStatus", "active"),
                        "errorCode": conn.get("errorCode"),
                        "backoffLevel": conn.get("backoffLevel", 0),
                        "connection_count": 0,
                        "model_locks": {},
                        "connections": []
                    }

                # Accumulate values
                aggregated[prefix]["connection_count"] += 1
                
                # Check for worse status (e.g. if one is error/unavailable, prioritize it or keep default)
                current_status = conn.get("testStatus", "active")
                if current_status in ("error", "unavailable") and aggregated[prefix]["status"] == "active":
                    aggregated[prefix]["status"] = current_status
                
                # Update errorCode if present
                err_code = conn.get("errorCode")
                if err_code is not None:
                    aggregated[prefix]["errorCode"] = err_code

                # Update backoffLevel if higher
                backoff = conn.get("backoffLevel", 0)
                if backoff > aggregated[prefix]["backoffLevel"]:
                    aggregated[prefix]["backoffLevel"] = backoff

                # Add connection name/email to list
                ident = conn.get("name") or conn.get("email")
                if ident:
                    aggregated[prefix]["connections"].append(ident)

                # Check for modelLock_ fields
                for k, v in conn.items():
                    if k.startswith("modelLock_") and v is not None:
                        model_id = k[len("modelLock_"):]
                        aggregated[prefix]["model_locks"][model_id] = v

            return aggregated

    except urllib.error.URLError as e:
        log.debug(f"fetch_provider_connections: network error: {e}")
        return {}
    except json.JSONDecodeError as e:
        log.warning("fetch_provider_connections: Failed to parse JSON response")
        return {}
    except Exception as e:
        log.debug(f"fetch_provider_connections: unexpected error: {e}")
        return {}


def _probe_error_info(probe_result: dict) -> dict:
    """Map a failed probe to error_info compatible with HealthRegistry.mark_error."""
    status = probe_result.get("status", 0)
    if status in (401, 403):
        return {"type": "no_credentials", "model_specific": False}
    if status == 402:
        return {"type": "no_credit", "model_specific": False}
    if status == 429:
        return {"type": "rate_limit", "model_specific": False}
    return {"type": "probe_failed", "model_specific": False}


class ProviderDiscovery:
    """Discover and probe 9router providers and their health.

    Maintains a bidirectional alias map and can probe providers with minimal
    chat completions requests to verify they are reachable and functional.
    """

    def __init__(self, base_url: Optional[str] = None, registry=None):
        """Initialize provider discovery.

        Args:
            base_url: Base URL for 9router admin API (defaults to NINEROUTER_URL).
            registry: Optional HealthRegistry instance for state tracking (future use).
        """
        self.base_url = base_url or NINEROUTER_URL
        self.registry = registry
        log.info(f"ProviderDiscovery initialized for {self.base_url}")

    def sync_connections(self) -> Dict[str, Dict]:
        """Sync connections from admin API and expand alias map.

        Calls fetch_provider_connections(), registers any NEW unknown provider
        prefixes into the alias map (alias → itself), returns normalized connections dict.

        Returns:
            {normalized_prefix: status_dict} as returned by fetch_provider_connections.
        """
        connections = fetch_provider_connections(self.base_url)

        # Expand alias map with any new prefixes discovered
        known_prefixes = set(_PREFIX_TO_ALIAS.keys())
        for prefix in connections.keys():
            if prefix not in known_prefixes:
                # Register as alias → itself (provider name may be unknown)
                _PROVIDER_ALIAS_MAP[prefix] = prefix
                _PREFIX_TO_ALIAS[prefix] = prefix
                log.info(f"Registered new provider: {prefix}")

        return connections

    def probe_provider(
        self, provider_prefix: str, model_id: str
    ) -> Dict[str, Any]:
        """Probe a provider/model with a minimal chat request.

        Sends POST {base_url}/v1/chat/completions with a ping payload,
        Authorization Bearer NINEROUTER_KEY (from config), and a 10s timeout.

        Args:
            provider_prefix: Catalog prefix (e.g., "kr", "nvidia").
            model_id: Full model ID (e.g., "nvidia/some-model").

        Returns:
            {
                "ok": bool,
                "status": int,
                "error": str | None,
                "latency_ms": int
            }

        Never raises exceptions; errors are reflected in the result dict.
        """
        if not NINEROUTER_KEY:
            return {
                "ok": False,
                "status": 500,
                "error": "NINEROUTER_KEY not set",
                "latency_ms": 0,
            }

        url = f"{self.base_url}/v1/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {NINEROUTER_KEY}",
        }
        payload = {
            "model": model_id,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
        }
        start = time.time()
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=10) as resp:
                latency_ms = int((time.time() - start) * 1000)
                status = resp.getcode()
                ok = 200 <= status < 300
                return {
                    "ok": ok,
                    "status": status,
                    "error": None if ok else f"HTTP {status}",
                    "latency_ms": latency_ms,
                }
        except urllib.error.HTTPError as e:
            latency_ms = int((time.time() - start) * 1000)
            return {
                "ok": False,
                "status": e.code,
                "error": str(e.reason),
                "latency_ms": latency_ms,
            }
        except (urllib.error.URLError, TimeoutError) as e:
            latency_ms = int((time.time() - start) * 1000)
            return {
                "ok": False,
                "status": 0,
                "error": str(e),
                "latency_ms": latency_ms,
            }
        except Exception as e:
            latency_ms = int((time.time() - start) * 1000)
            return {
                "ok": False,
                "status": 0,
                "error": f"Unexpected: {e}",
                "latency_ms": latency_ms,
            }

    def _catalog_models_by_prefix(self) -> Dict[str, List[str]]:
        """Fetch catalog models via GET /v1/models and group them by provider prefix."""
        if not NINEROUTER_KEY:
            return {}

        catalog_url = f"{self.base_url}/v1/models"
        headers = {"Authorization": f"Bearer {NINEROUTER_KEY}"}
        req = urllib.request.Request(catalog_url, headers=headers, method="GET")
        catalog_models = []
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.getcode() == 200:
                    body = resp.read()
                    data = json.loads(body) if body else {}
                    catalog_models = data.get("data", [])
        except Exception as e:
            log.debug(f"_catalog_models_by_prefix: failed to fetch catalog: {e}")
            return {}

        prefix_to_models: Dict[str, List[str]] = {}
        for model in catalog_models:
            model_id = model.get("id", "")
            if "/" in model_id:
                p_prefix = model_id.split("/", 1)[0]
                if p_prefix not in prefix_to_models:
                    prefix_to_models[p_prefix] = []
                prefix_to_models[p_prefix].append(model_id)
        return prefix_to_models

    def run_discovery_once(self) -> Dict[str, Any]:
        """Run a complete discovery cycle.

        Performs sync_connections() + fetches catalog models + probes at most 10 active providers.

        Returns:
            {
                "connections": int,
                "new_providers": list,
                "status_by_provider": dict,
            }
        """
        connections = self.sync_connections()
        
        # Get list of prior known prefixes before sync to check new_providers
        prior_prefixes = set(_PREFIX_TO_ALIAS.keys())

        # Sync connections again (or use the one we just got) to find any new prefixes
        # (sync_connections also registers them in _PREFIX_TO_ALIAS)
        new_providers = [p for p in connections.keys() if p not in prior_prefixes]

        status_by_provider: Dict[str, Any] = {}

        prefix_to_models = self._catalog_models_by_prefix()

        # Probe matching providers (cap total probes at 10)
        probes_sent = 0
        for prefix in connections.keys():
            models = prefix_to_models.get(prefix, [])
            if not models:
                status_by_provider[prefix] = {
                    "ok": None,
                    "status": None,
                    "error": "no catalog models",
                    "latency_ms": 0,
                }
                continue

            if probes_sent >= 10:
                status_by_provider[prefix] = {
                    "ok": None,
                    "status": None,
                    "error": "skipped (probe limit reached)",
                    "latency_ms": 0,
                }
                continue

            # Pick the first model for this prefix and probe it
            model_id = models[0]
            probe_res = self.probe_provider(prefix, model_id)
            status_by_provider[prefix] = probe_res
            probes_sent += 1

        return {
            "connections": len(connections),
            "new_providers": new_providers,
            "status_by_provider": status_by_provider,
        }

    def discover_new_providers(self, known_prefixes: set) -> Dict[str, Any]:
        """Probe providers absent from known_prefixes and register results in the registry.

        Args:
            known_prefixes: prefixes already tracked (registry state or prior runs).

        Returns:
            {
                "new_providers": list,
                "status_by_provider": dict,
            }
        """
        connections = self.sync_connections()
        new_providers = sorted(set(connections.keys()) - set(known_prefixes))
        if not new_providers:
            return {"new_providers": [], "status_by_provider": {}}

        prefix_to_models = self._catalog_models_by_prefix()
        status_by_provider: Dict[str, Any] = {}
        for prefix in new_providers:
            models = prefix_to_models.get(prefix, [])
            if not models:
                status_by_provider[prefix] = {
                    "ok": None,
                    "status": None,
                    "error": "no catalog models",
                    "latency_ms": 0,
                }
                continue

            probe_res = self.probe_provider(prefix, models[0])
            status_by_provider[prefix] = probe_res
            if self.registry:
                if probe_res.get("ok"):
                    self.registry.mark_healthy(prefix)
                else:
                    self.registry.mark_error(prefix, _probe_error_info(probe_res))

        return {"new_providers": new_providers, "status_by_provider": status_by_provider}
