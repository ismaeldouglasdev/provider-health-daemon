from collections import defaultdict
import logging
import threading

from data_policy import DataSensitivity, allowed, classify_model_policy
from model_id_mapper import ModelIdMapper

log = logging.getLogger(__name__)


class ServiceUnavailable(Exception):
    pass


class PolicyUnavailable(ServiceUnavailable):
    pass


class MetaRouterSelector:
    """Selects a healthy downstream router for each request.

    Uses smooth weighted round-robin (nginx-style): routers with higher
    weight receive proportionally more requests while keeping a balanced
    distribution. Tracks per-router success/failure streaks so a router
    that keeps failing after recovery is excluded until it proves stable.
    """

    def __init__(self, registry, failure_threshold: int = 3):
        self._registry = registry
        self._failure_threshold = failure_threshold
        self._lock = threading.Lock()
        self._current_weights: dict[str, int] = defaultdict(int)
        self._successes: dict[str, int] = defaultdict(int)
        self._failures: dict[str, int] = defaultdict(int)
        self._streaks: dict[str, int] = defaultdict(int)
        self._model_mapper = ModelIdMapper()

    def _eligible_routers(self, model: str = None, sensitivity: DataSensitivity = DataSensitivity.PUBLIC):
        healthy = self._registry.get_healthy_routers()
        if model and sensitivity != DataSensitivity.PUBLIC:
            policy = classify_model_policy(model)
            before = len(healthy)
            healthy = [
                r for r in healthy
                if allowed(policy, sensitivity)
            ]
            if before != len(healthy):
                log.info(
                    "META_POLICY_FILTER model=%s sensitivity=%s policy=%s removed=%s",
                    model, sensitivity.value, policy.value, before - len(healthy),
                )
            if not healthy:
                raise PolicyUnavailable(
                    f"no downstream route allowed for {sensitivity.value} data with policy {policy.value}"
                )
        log.info("META_ELIGIBLE model=%s sensitivity=%s routers=%s", model or "", sensitivity.value, [(r.name, r.health_status, len(r.models)) for r in healthy])
        if not healthy:
            log.warning("META_EMPTY model=%s registry has no healthy routers", model or "")
            raise ServiceUnavailable("all routers unavailable")
        if model:
            canonical_model = self._model_mapper.to_canonical(model)
            for router in healthy:
                self._model_mapper.register_catalog(router.name, {m: m for m in router.models})

            # `kr/*` is the public model namespace for the dedicated local
            # Kiro Gateway. Prefer that downstream router over the direct
            # 9Router Kiro catalog, whose quota/subscription state is separate
            # from the gateway credential. The gateway advertises Kiro model
            # ids without the `kr/` prefix.
            if model.startswith("kr/"):
                bare_model = model.split("/", 1)[1]
                kiro = [r for r in healthy if r.name == "Kiro" and bare_model in r.models]
                if kiro:
                    healthy = kiro
                    log.info("META_KIRO_ROUTE model=%s router=%s", model, [r.name for r in kiro])
                    return healthy

            capable = [
                r for r in healthy
                if any(self._model_mapper.to_canonical(m) == canonical_model for m in r.models)
            ]
            if capable:
                healthy = capable
            else:
                # Model is not in any healthy router's catalog. Do NOT fall
                # back to every healthy router: a router with a small catalog
                # (e.g. Kiro, 13 models) would be picked for models it cannot
                # serve and reject them with "Invalid model ID or insufficient
                # subscription level" (kiro_api_error) — recorded by
                # error_parser as a 24h subscription_level cooldown, poisoning
                # GOOD models (2026-08-14: ag/kr/gemini/cu cascade). Fall back
                # only to routers serving the same provider prefix or the bare
                # model name (Kiro catalogs "minimax-m2.5" bare while requests
                # use "kr/minimax-m2.5"); else fail with ServiceUnavailable.
                provider, sep, rest = canonical_model.partition("/")
                serving = [
                    r for r in healthy
                    if any(
                        (provider and m.startswith(provider + "/"))
                        or (sep and m == rest)
                        for m in r.models
                    )
                ]
                if not serving:
                    raise ServiceUnavailable(f"model '{model}' not served by any healthy router")
                healthy = serving
        return healthy

    def _pick_weighted(self, routers):
        """Smooth weighted round-robin over the given routers."""
        total = sum(r.weight for r in routers)
        best = None
        for r in routers:
            self._current_weights[r.name] += r.weight
            if best is None or self._current_weights[r.name] > self._current_weights[best.name]:
                best = r
        if best is not None:
            self._current_weights[best.name] -= total
        return best

    def model_for_router(self, model: str, router_name: str) -> str:
        """Translate a client/canonical model ID to the selected router catalog ID."""
        if not isinstance(model, str) or not model:
            return model
        router = self._registry.get_router(router_name)
        if router is not None:
            self._model_mapper.register_catalog(router.name, {m: m for m in router.models})
        canonical = self._model_mapper.to_canonical(model)
        return self._model_mapper.to_router_specific(canonical, router_name)

    def select_router(self, model: str = None, sensitivity=DataSensitivity.PUBLIC, exclude=None):
        with self._lock:
            healthy = self._eligible_routers(model, sensitivity)
            excluded = set(exclude or ())
            if excluded:
                healthy = [r for r in healthy if r.name not in excluded]
                if not healthy:
                    raise ServiceUnavailable("all eligible routers excluded")
            threshold = self._failure_threshold
            flapping = [r for r in healthy if self._streaks[r.name] >= threshold]
            if flapping and len(flapping) < len(healthy):
                healthy = [r for r in healthy if r not in flapping]
            selected = self._pick_weighted(healthy)
            if selected is None:
                raise ServiceUnavailable("all routers unavailable")
            return selected

    def on_success(self, router_name):
        with self._lock:
            self._successes[router_name] += 1
            self._streaks[router_name] = 0

    def on_failure(self, router_name, error_type=None, model=None, sensitivity=DataSensitivity.PUBLIC):
        """Record a router failure and return a model-aware fallback when possible."""
        with self._lock:
            self._failures[router_name] += 1
            self._streaks[router_name] += 1
            self._registry.mark_unhealthy(router_name, error_type)
            return self._fallback(
                model=model,
                sensitivity=sensitivity,
                exclude={router_name},
            )

    def _fallback(self, model=None, sensitivity=DataSensitivity.PUBLIC, exclude=None):
        """Pick a healthy fallback, preserving model/policy eligibility."""
        healthy = self._eligible_routers(model=model, sensitivity=sensitivity)
        excluded = set(exclude or ())
        healthy = [r for r in healthy if r.name not in excluded]
        if not healthy:
            raise ServiceUnavailable("all routers unavailable (after fallback)")
        return self._pick_weighted(healthy)

    def get_stats(self) -> dict:
        """Per-router selection/success/failure/streak counters."""
        with self._lock:
            return {
                name: {
                    "successes": self._successes[name],
                    "failures": self._failures[name],
                    "consecutive_failures": self._streaks[name],
                }
                for name in set(self._successes) | set(self._failures) | set(self._streaks)
            }
