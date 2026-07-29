"""HTTP proxy with health-aware + smart routing for 9router requests.

Extends the basic forwarder with:
  - Prompt limiting (truncate oversized prompts)
  - Health-aware gating (skip cooldown providers)
  - Smart router integration (select best model from combo)
  - Automatic health reset for failed-then-succeeded providers
"""

import json
import logging
import re
import sys
import time
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# ── Fix sys.path BEFORE local imports ─────────────────────────────────
# daemon.py inserts prompt-limiter at sys.path[0], which shadows our
# local modules (smart_router.py, metrics_store.py, etc.).
# Fix: put our directory at [0], prompt-limiter at [1].
from config import PROMPT_LIMITER_DIR

_local_dir = str(Path(__file__).parent)
_prompt_dir = str(PROMPT_LIMITER_DIR)

# Force our local directory to be first in sys.path so local modules
# take priority over prompt-limiter (which has a smart_router.py too)
if _local_dir in sys.path:
    sys.path.remove(_local_dir)
sys.path.insert(0, _local_dir)

# Keep prompt-limiter at position 1 so it's still importable
if _prompt_dir in sys.path:
    sys.path.remove(_prompt_dir)
sys.path.insert(1, _prompt_dir)

# ── Local imports (must come after sys.path fix) ──────────────────────
from health_registry import HealthRegistry
from error_parser import parse_error, parse_log_line
from metrics_store import MetricsStore, RequestRecord
from smart_router import SmartRouter
from router_registry import RouterRegistry
from meta_router import MetaRouterSelector, ServiceUnavailable
from response_normalizer import normalize_response, normalize_error, normalize_streaming_body

# ── Rest of config (PROMPT_LIMITER_DIR already imported above) ────────
from config import (
    HEALTH_PROXY_PORT,
    NINEROUTER_URL,
    NINEROUTER_KEY,
    MODEL_LIMITS_FILE,
    COMBO_REFRESH_INTERVAL,
    DOWNSTREAM_ROUTERS,
)

log = logging.getLogger(__name__)

try:
    from prompt_limiter import count_tokens, get_model_limits, truncate_prompt
except ImportError:
    # Fallback: simple implementations
    log.warning("prompt_limiter not available, using fallback token counter")

    def count_tokens(text: str) -> int:
        return len(text) // 4

    def get_model_limits(model_id: str) -> dict:
        return {"tpm": 30000, "rpm": 60, "context": 8192}

    def truncate_prompt(prompt: str, max_tokens: int) -> str:
        lines = prompt.split("\n")
        result = []
        current = 0
        for line in reversed(lines):
            lt = count_tokens(line)
            if current + lt > max_tokens:
                break
            result.insert(0, line)
            current += lt
        return "\n".join(result)


class HealthProxyHandler(BaseHTTPRequestHandler):
    """HTTP handler with health-aware routing + smart model selection."""

    registry: HealthRegistry = None  # set by server
    metrics_store: MetricsStore = None
    smart_router: SmartRouter = None
    meta_registry: RouterRegistry = None  # set by server for router-of-routers
    meta_selector: MetaRouterSelector = None  # set by server
    _combo_cache: list[str] = []
    _combo_cache_time: float = 0

    def _normalize_response_body(self, resp_body: bytes, content_type: str) -> bytes:
        if not ('application/json' in content_type or 'text/event-stream' in content_type):
            return resp_body
        try:
            if 'event-stream' in content_type:
                return normalize_streaming_body(resp_body).encode()
            return json.dumps(normalize_response(resp_body)).encode()
        except Exception as e:
            log.warning(f"Response normalization failed: {e}", extra={"event": "normalize_error"})
            return resp_body

    def _get_combo_models(self) -> list[str]:
        """Get cached list of combo models from 9router."""
        now = time.time()
        if now - self._combo_cache_time < COMBO_REFRESH_INTERVAL and self._combo_cache:
            return self._combo_cache
        self._combo_cache = SmartRouter.get_default_combos()
        self._combo_cache_time = now
        return self._combo_cache

    def _forward(self, body=None):
        path = self.path
        headers = {"Content-Type": "application/json"}
        start_time = time.time()

        auth = self.headers.get("Authorization", "")
        if auth:
            headers["Authorization"] = auth
        elif NINEROUTER_KEY:
            headers["Authorization"] = f"Bearer {NINEROUTER_KEY}"

        data = json.dumps(body).encode() if body else None

        # ── Router-of-routers: pick target via meta-router ─────────────
        target_router = None
        fallback_used = False
        if self.meta_selector:
            try:
                target_router = self.meta_selector.select_router()
            except ServiceUnavailable:
                self._respond_unavailable("all routers unavailable")
                return

            if target_router:
                url = target_router.url.rstrip("/") + path
                if target_router.auth:
                    headers[target_router.auth["header"]] = target_router.auth["value"]
            else:
                url = f"{NINEROUTER_URL}{path}"
        else:
            url = f"{NINEROUTER_URL}{path}"

        req = urllib.request.Request(url, data=data, headers=headers, method=self.command)
        req.add_header("Accept", "text/event-stream, application/json")

        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                resp_body = resp.read()
                is_chat = self.path in ("/v1/chat/completions", "/chat/completions")
                if is_chat:
                    resp_body = self._normalize_response_body(resp_body, resp.headers.get('Content-Type', ''))
                
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    if k.lower() not in ("transfer-encoding", "content-encoding", "content-length"):
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(resp_body)))
                if fallback_used:
                    self.send_header("X-Health-Proxy-Fallback", "true")
                self.end_headers()
                self.wfile.write(resp_body)

                if target_router and self.meta_selector:
                    self.meta_selector.on_success(target_router.name)

                if body and resp.status == 200:
                    model = body.get("model", "")
                    if model:
                        provider = model.split("/")[0]
                        self.registry.mark_healthy(provider)
                        self._record_usage(body, resp_body, start_time, provider, model, True)

        except urllib.error.HTTPError as e:
            resp_body = e.read()

            # Router responded — upstream provider failed, NOT a router issue.
            # Do NOT mark router unhealthy; only penalize for connection errors (URLError below).
            # Still attempt fallback to a different router if available.
            if target_router and self.meta_selector and not fallback_used:
                try:
                    fallback_router = self.meta_selector.select_router()
                    if fallback_router and fallback_router.name != target_router.name:
                        fallback_url = fallback_router.url.rstrip("/") + path
                        fallback_req = urllib.request.Request(fallback_url, data=data, headers=headers, method=self.command)
                        fallback_req.add_header("Accept", "text/event-stream, application/json")
                        fallback_resp = urllib.request.urlopen(fallback_req, timeout=180)
                        fb_body = fallback_resp.read()
                        fb_body = self._normalize_response_body(fb_body, fallback_resp.headers.get('Content-Type', ''))
                        
                        self.send_response(fallback_resp.status)
                        for k, v in fallback_resp.headers.items():
                            if k.lower() not in ("transfer-encoding", "content-encoding", "content-length"):
                                self.send_header(k, v)
                        self.send_header("Content-Length", str(len(fb_body)))
                        self.send_header("X-Health-Proxy-Fallback", "true")
                        self.end_headers()
                        self.wfile.write(fb_body)
                        if body and fallback_resp.status == 200:
                            model = body.get("model", "")
                            if model:
                                provider = model.split("/")[0]
                                self.registry.mark_healthy(provider)
                                self._record_usage(body, fb_body, start_time, provider, model, True)
                        return
                except (urllib.error.URLError, urllib.error.HTTPError, ServiceUnavailable):
                    pass

            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            try:
                err_normalized = normalize_error(resp_body)
                resp_body = json.dumps(err_normalized).encode()
            except Exception as norm_err:
                log.warning(f"Error normalization failed: {norm_err}", extra={"event": "normalize_error_failed"})
            self.wfile.write(resp_body)

            body_text = resp_body.decode(errors="replace")
            self._handle_error(e.code, body_text, body)

            # Record failed request in metrics
            if body:
                model = body.get("model", "")
                provider = model.split("/")[0] if "/" in model else model
                self._record_usage(body, resp_body, start_time, provider, model, False, f"http_{e.code}")

        except urllib.error.URLError as e:
            # Attempt fallback via meta-router
            if target_router and self.meta_selector and not fallback_used:
                try:
                    self.meta_selector.on_failure(target_router.name, "connection_error")
                    fallback_router = self.meta_selector.select_router()
                    if fallback_router:
                        fallback_url = fallback_router.url.rstrip("/") + path
                        fallback_req = urllib.request.Request(fallback_url, data=data, headers=headers, method=self.command)
                        fallback_req.add_header("Accept", "text/event-stream, application/json")
                        fallback_resp = urllib.request.urlopen(fallback_req, timeout=180)
                        fb_body = fallback_resp.read()
                        self.send_response(fallback_resp.status)
                        for k, v in fallback_resp.headers.items():
                            if k.lower() not in ("transfer-encoding", "content-encoding", "content-length"):
                                self.send_header(k, v)
                        self.send_header("Content-Length", str(len(fb_body)))
                        self.send_header("X-Health-Proxy-Fallback", "true")
                        self.end_headers()
                        self.wfile.write(fb_body)
                        if body and fallback_resp.status == 200:
                            model = body.get("model", "")
                            if model:
                                provider = model.split("/")[0]
                                self.registry.mark_healthy(provider)
                                self._record_usage(body, fb_body, start_time, provider, model, True)
                        return
                except (urllib.error.URLError, urllib.error.HTTPError, ServiceUnavailable):
                    pass

            self._respond_unavailable(f"Connection error: {e.reason}")
            if body:
                model = body.get("model", "")
                provider = model.split("/")[0] if "/" in model else model
                self._record_usage(body, b"", start_time, provider, model, False, "connection_error")

    def _record_usage(self, request_body: dict, response_body: bytes, start_time: float,
                      provider: str, model: str, success: bool, error_type: str = None):
        """Record request metrics."""
        if not self.metrics_store:
            return

        duration_ms = int((time.time() - start_time) * 1000)

        # Try to extract tokens from response
        tokens_in = 0
        tokens_out = 0
        tokens_cache = 0
        ttft_ms = duration_ms

        try:
            if response_body and response_body.strip():
                text = response_body.decode(errors="replace")
                # Parse SSE or JSON response
                for line in text.split("\n"):
                    if line.startswith("data: ") and "[DONE]" not in line:
                        try:
                            chunk = json.loads(line[6:])
                            usage = chunk.get("usage", {})
                            if usage:
                                tokens_in = usage.get("prompt_tokens", 0) or tokens_in
                                tokens_out = usage.get("completion_tokens", 0) or tokens_out
                                if "prompt_tokens_details" in usage:
                                    tokens_cache = usage["prompt_tokens_details"].get("cached_tokens", 0)
                        except json.JSONDecodeError:
                            pass
                # If no usage found, try full JSON
                if not tokens_in and not tokens_out:
                    try:
                        resp_json = json.loads(text.split("data: ")[1].split("\n")[0].strip())
                        usage = resp_json.get("usage", {})
                        tokens_in = usage.get("prompt_tokens", 0)
                        tokens_out = usage.get("completion_tokens", 0)
                        if "prompt_tokens_details" in usage:
                            tokens_cache = usage["prompt_tokens_details"].get("cached_tokens", 0)
                    except (IndexError, json.JSONDecodeError):
                        pass
        except Exception:
            pass

        record = RequestRecord(
            timestamp=start_time,
            provider=provider,
            model=model,
            duration_ms=duration_ms,
            ttft_ms=ttft_ms,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            tokens_cache=tokens_cache,
            success=success,
            error_type=error_type,
        )
        self.metrics_store.record_request(record)

    def _handle_error(self, status: int, body_text: str, request_body: dict = None):
        """Record error in health registry with smart routing awareness."""
        if not self.registry:
            return

        model = (request_body or {}).get("model", "")
        provider = model.split("/")[0] if "/" in model else model

        error_info = parse_error(status, body_text)

        # Try log-line format if model not detected
        if not provider or provider == model:
            parsed = parse_log_line(f"❌ unknown [{status}]: [{status}]: {body_text}")
            if parsed and parsed.get("provider_hint"):
                provider = parsed["provider_hint"]
                model = parsed.get("model_hint", model)

        if provider:
            self.registry.mark_error(
                provider=provider,
                error_info=error_info,
                model=model if error_info.get("model_specific") else None,
            )

    def _find_healthy_alternative(self, body: dict) -> str | None:
        """Smart routing: find the best performing model from combo."""
        if not self.registry or not self.smart_router:
            return None

        current = body.get("model", "")
        if not current:
            return None

        # For combo models, use smart router to pick the best
        combo_models = self._get_combo_models()
        if combo_models:
            best = self.smart_router.best_model(combo_models, self.registry)
            if best and best != current:
                log.info(f"SmartRouter: {current} → {best} (healthier alternative)")
                return best

        return None

    def _apply_prompt_limit(self, body: dict) -> dict | None:
        """Check if request exceeds model context or TPM limits, truncate if needed."""
        model = body.get("model", "unknown")
        messages = body.get("messages", [])

        limits = get_model_limits(model)
        max_context = limits.get("context", 8192)
        max_tpm = limits.get("tpm", 30000)

        all_text = "\n".join(
            m.get("content", "") or ""
            if isinstance(m.get("content"), str)
            else json.dumps(m.get("content", ""))
            for m in messages
        )
        total = count_tokens(all_text)

        tpm_safe_limit = int(max_tpm * 0.85)
        context_safe_limit = int(max_context * 0.75)
        effective_limit = min(tpm_safe_limit, context_safe_limit)

        if total <= effective_limit:
            return None

        exceeded = "TPM" if total > tpm_safe_limit else "context"
        limit_hit = tpm_safe_limit if exceeded == "TPM" else context_safe_limit
        log.warning(
            "Prompts exceeded model limits",
            extra={
                "event": "prompt_truncated",
                "model": model,
                "original_tokens": total,
                "limit_tokens": limit_hit,
                "exceeded": exceeded,
                "tpm_limit": max_tpm,
                "context_limit": max_context,
            },
        )

        kept: list[dict] = []
        kept_tokens = 0
        for msg in reversed(messages):
            content = msg.get("content", "")
            if isinstance(content, list):
                content = json.dumps(content)
            msg_tokens = count_tokens(content)
            if kept_tokens + msg_tokens > effective_limit:
                if msg.get("role") in ("system", "developer") and kept_tokens < effective_limit * 0.2:
                    kept.insert(0, msg)
                    kept_tokens += msg_tokens
                break
            kept.insert(0, msg)
            kept_tokens += msg_tokens

        new_body = dict(body)
        new_body["messages"] = kept

        log.info(
            "Truncation complete",
            extra={
                "event": "truncation_done",
                "model": model,
                "original": total,
                "truncated": kept_tokens,
                "messages_kept": len(kept),
            },
        )
        return new_body

    # ── HTTP handlers ────────────────────────────────────────────────

    def do_GET(self):
        if self.path == "/health" or self.path == "/v1/health":
            self._respond_status()
            return
        if self.path.startswith("/health/reset/"):
            target = self.path.split("/health/reset/")[-1]
            self._handle_reset(target)
            return
        if self.path == "/health/summary":
            self._respond_summary()
            return
        
        x_router = self.headers.get("X-Router", "").lower()
        if x_router == "combo-round-robin" and self.path in ["/v1/models", "/models"]:
            self._respond_combo_models()
            return
        
        self._forward()

    def _respond_status(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()

        routers_info = {}
        if self.meta_registry:
            for r in self.meta_registry.get_all_routers():
                routers_info[r.name] = {
                    "url": r.url,
                    "status": r.health_status,
                    "models_count": len(r.models),
                    "cooldown_until": r.cooldown_until,
                }

        data = {
            "status": "online",
            "forwarding": NINEROUTER_URL,
            "routers": routers_info,
            "health_file": str(self.registry.filepath) if self.registry else "",
            "summary": self.registry.status_summary() if self.registry else {},
            "providers": {
                name: {"status": e.get("status"), "until": e.get("until"), "reason": e.get("reason")}
                for name, e in self.registry._data.get("providers", {}).items()
            } if self.registry else {},
        }
        self.wfile.write(json.dumps(data, indent=2, default=str).encode())

    def _respond_summary(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(self.registry.status_summary(), default=str).encode())

    def _respond_combo_models(self):
        if not self.meta_registry:
            self._respond_unavailable("meta-registry not available")
            return
        
        healthy_routers = self.meta_registry.get_healthy_routers()
        if not healthy_routers:
            self._respond_unavailable("no healthy routers available")
            return
        
        all_models = []
        seen_ids = set()
        
        for router in healthy_routers:
            for model_id in router.models:
                if model_id not in seen_ids:
                    all_models.append({
                        "id": model_id,
                        "object": "model",
                        "created": int(time.time()),
                        "owned_by": router.name
                    })
                    seen_ids.add(model_id)
        
        response = {
            "object": "list",
            "data": all_models
        }
        
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(response).encode())

    def _handle_reset(self, target: str):
        provider_reg = self.registry.get_provider(target)
        model_reg = self.registry.get_model(target)

        if provider_reg or model_reg:
            if provider_reg:
                self.registry.force_healthy(target)
            elif model_reg:
                self.registry.force_healthy(target.split("/")[0], target)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "reset", "target": target}).encode())
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(json.dumps({"error": "unknown provider or model"}).encode())

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        body = json.loads(raw)

        if "/chat/completions" in self.path or "/v1/chat/completions" in self.path:
            model = body.get("model", "")
            provider = model.split("/")[0] if "/" in model else model

            # Router-level health gate: if all routers are down, return 503
            if self.meta_selector:
                try:
                    self.meta_selector.select_router()
                except ServiceUnavailable:
                    self._respond_unavailable("all routers unavailable")
                    return

            # Health gate: check before forwarding
            if self.registry:
                m_ok = self.registry.is_model_available(model) if model else True
                p_ok = self.registry.is_provider_healthy(provider) if provider else True

                if not m_ok:
                    # Smart routing: find healthy alternative
                    alternative = self._find_healthy_alternative(body)
                    if alternative:
                        old_model = model
                        body["model"] = alternative
                        body["_smart_routed"] = True
                        body["_original_model"] = old_model
                        log.info(f"Smart routed {old_model} → {alternative} (model unavailable)")
                        model = alternative
                        provider = alternative.split("/")[0] if "/" in alternative else alternative
                        # Re-check new model health
                        m_ok = self.registry.is_model_available(model)
                        p_ok = self.registry.is_provider_healthy(provider)

                    if not m_ok:
                        entry = self.registry.get_model(model)
                        self._respond_unavailable(
                            f"Model '{model}' is in cooldown (reason: {entry.get('reason')}, "
                            f"until: {entry.get('until')})"
                        )
                        return
                    if not p_ok:
                        entry = self.registry.get_provider(provider)
                        self._respond_unavailable(
                            f"Provider '{provider}' is in cooldown (reason: {entry.get('reason')}, "
                            f"until: {entry.get('until')})"
                        )
                        return

            # Apply prompt limiting (context window)
            limited = self._apply_prompt_limit(body)
            if limited:
                body = limited

        self._forward(body)

    def _respond_unavailable(self, message: str):
        """Return 503 to OpenCode so it falls back via oh-my-openagent."""
        self.send_response(503)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        payload = json.dumps({"error": {"message": message, "type": "provider_unavailable"}})
        self.wfile.write(payload.encode())

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def log_message(self, fmt, *args):
        log.info(f"{self.client_address[0]} {fmt % args}")


class HealthProxyServer:
    """Main server that ties proxy + health registry + metrics + smart router + meta-router together."""

    def __init__(self, port: int = HEALTH_PROXY_PORT, metrics_store: MetricsStore = None):
        self.port = port
        self.registry = HealthRegistry()
        self.metrics_store = metrics_store or MetricsStore()
        self.smart_router = SmartRouter(self.metrics_store)
        self.meta_registry = RouterRegistry(DOWNSTREAM_ROUTERS)
        self.meta_selector = MetaRouterSelector(self.meta_registry)

    def get_handler(self):
        """Create handler class with shared registry + metrics + meta-router."""
        registry = self.registry
        metrics = self.metrics_store
        router = self.smart_router
        meta_registry = self.meta_registry
        meta_selector = self.meta_selector

        class HandlerWithRegistry(HealthProxyHandler):
            pass

        HandlerWithRegistry.registry = registry
        HandlerWithRegistry.metrics_store = metrics
        HandlerWithRegistry.smart_router = router
        HandlerWithRegistry.meta_registry = meta_registry
        HandlerWithRegistry.meta_selector = meta_selector
        return HandlerWithRegistry

    def run(self):
        handler = self.get_handler()
        from socketserver import ThreadingMixIn

        class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
            daemon_threads = True

        server = ThreadingHTTPServer(("0.0.0.0", self.port), handler)

        log.info(f"🛡️  Health Proxy → http://localhost:{self.port}")
        log.info(f"   Forwarding → {len(DOWNSTREAM_ROUTERS)} routers (via meta-router)")
        log.info(f"   Health file → {self.registry.filepath}")
        log.info("")
        log.info("   Status:")
        summary = self.registry.status_summary()
        log.info(f"     healthy:   {summary['by_status']['healthy']}")
        log.info(f"     cooldown:  {summary['by_status']['cooldown']}")
        log.info(f"     probing:   {summary['by_status']['probing']}")
        log.info(f"     disabled:  {summary['by_status']['disabled']}")
        log.info(f"     expired:   {summary['expired_ready']}")
        log.info("")
        log.info("   Activate:")
        log.info(f'     "baseURL": "http://127.0.0.1:{self.port}/v1"')
        log.info("")

        try:
            server.serve_forever()
        except KeyboardInterrupt:
            server.shutdown()
            log.info("Health Proxy encerrado.")