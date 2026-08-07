"""HTTP proxy with health-aware + smart routing for 9router requests.

Extends the basic forwarder with:
  - Prompt limiting (truncate oversized prompts)
  - Health-aware gating (skip cooldown providers)
  - Smart router integration (select best model from combo)
  - Automatic health reset for failed-then-succeeded providers
"""

import json
import logging
import os
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
from error_parser import parse_error, extract_provider_model
from metrics_store import MetricsStore, RequestRecord
from smart_router import SmartRouter
from router_registry import RouterRegistry
from meta_router import MetaRouterSelector, ServiceUnavailable
from response_normalizer import normalize_response, normalize_error, normalize_sse_chunk, normalize_streaming_body

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


def _strip_anthropic_caching(obj):
    """Remove Anthropic-style cache_control fields recursively.

    The opencode AI SDK emits prompt-caching blocks
    ({"type": "text", ..., "cache_control": {"type": "ephemeral"}}) which
    strict OpenAI-compatible schemas (mistral 422 extra_forbidden) reject.
    """
    if isinstance(obj, dict):
        obj.pop("cache_control", None)
        for value in obj.values():
            _strip_anthropic_caching(value)
    elif isinstance(obj, list):
        for item in obj:
            _strip_anthropic_caching(item)

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


def _has_explicit_limits(model_id: str) -> bool:
    """True if model_id has an explicit entry in model_limits.json."""
    try:
        if MODEL_LIMITS_FILE.exists():
            data = json.loads(MODEL_LIMITS_FILE.read_text())
            return model_id in data.get("models", {})
    except (json.JSONDecodeError, OSError):
        log.warning("Could not read model limits file", extra={"event": "limits_read_error"})
    return False


class HealthProxyHandler(BaseHTTPRequestHandler):
    """HTTP handler with health-aware routing + smart model selection."""

    protocol_version = "HTTP/1.1"

    registry: HealthRegistry = None  # set by server
    metrics_store: MetricsStore = None
    smart_router: SmartRouter = None
    meta_registry: RouterRegistry = None  # set by server for router-of-routers
    meta_selector: MetaRouterSelector = None  # set by server
    opener: urllib.request.OpenerDirector = None  # connection-pooled opener, set by server
    _combo_cache: list[str] = []
    _combo_cache_time: float = 0
    # AuditMetrics instance injected by HealthProxyServer (set by daemon.main).
    # Kept untyped to avoid a circular import (daemon imports this module).
    audit = None

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

    def _emit_upstream_response(self, resp, is_chat: bool, fallback_used: bool = False) -> bytes:
        """Stream upstream response to client; returns normalized body sent.

        SSE (chat streaming) is forwarded chunk-by-chunk with Transfer-Encoding:
        chunked so the client sees tokens as they arrive (low TTFT) instead of
        waiting for the full body. Non-streaming bodies are buffered, normalized,
        and sent with Content-Length.
        """
        if self.audit:
            self.audit.requests_proxied += 1

        content_type = resp.headers.get("Content-Type", "")
        is_streaming = is_chat and "event-stream" in content_type

        self.send_response(resp.status)
        for k, v in resp.headers.items():
            if k.lower() not in ("transfer-encoding", "content-encoding", "content-length"):
                self.send_header(k, v)
        if fallback_used:
            self.send_header("X-Health-Proxy-Fallback", "true")

        if is_streaming:
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            sent: list[bytes] = []
            for line in resp:
                if line.startswith(b"data: ") and b"[DONE]" not in line:
                    try:
                        chunk = json.loads(line[6:].decode("utf-8", errors="replace"))
                        line = ("data: " + json.dumps(normalize_sse_chunk(chunk), ensure_ascii=False) + "\n").encode()
                    except (json.JSONDecodeError, ValueError):
                        pass
                sent.append(line)
                self.wfile.write(f"{len(line):X}\r\n".encode() + line + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            return b"".join(sent)

        body = resp.read()
        if is_chat:
            body = self._normalize_response_body(body, content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return body

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

        # Strip internal routing metadata before forwarding. These fields are
        # injected by combo substitution / smart routing (see do_POST) and MUST
        # NOT reach upstream providers: strict schemas (mistral, nvidia/glm-5.2)
        # reject unknown fields with 400/422. Also strip Anthropic-style
        # cache_control blocks (opencode AI SDK) — mistral 422 extra_forbidden.
        if body:
            body.pop("_original_model", None)
            body.pop("_smart_routed", None)
            _strip_anthropic_caching(body)

        data = json.dumps(body).encode() if body else None

        # ── Router-of-routers: pick target via meta-router ─────────────
        target_router = None
        fallback_used = False
        if self.meta_selector:
            try:
                model = (body or {}).get("model", "") if body else ""
                target_router = self.meta_selector.select_router(model=model)
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
            opener = self.opener if self.opener is not None else urllib.request.build_opener()
            with opener.open(req, timeout=180) as resp:
                is_chat = self.path in ("/v1/chat/completions", "/chat/completions")
                resp_body = self._emit_upstream_response(resp, is_chat, fallback_used)

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
            router_fallback_tried = False
            if target_router and self.meta_selector and not fallback_used:
                try:
                    fallback_router = self.meta_selector.select_router(model=(body or {}).get("model", "") if body else "")
                    if fallback_router and fallback_router.name != target_router.name:
                        fallback_url = fallback_router.url.rstrip("/") + path
                        fallback_req = urllib.request.Request(fallback_url, data=data, headers=headers, method=self.command)
                        fallback_req.add_header("Accept", "text/event-stream, application/json")
                        fallback_resp = urllib.request.urlopen(fallback_req, timeout=180)
                        is_chat = self.path in ("/v1/chat/completions", "/chat/completions")
                        fb_body = self._emit_upstream_response(fallback_resp, is_chat, fallback_used=True)
                        if body and fallback_resp.status == 200:
                            model = body.get("model", "")
                            if model:
                                provider = model.split("/")[0]
                                self.registry.mark_healthy(provider)
                                self._record_usage(body, fb_body, start_time, provider, model, True)
                        return
                    router_fallback_tried = True
                except (urllib.error.URLError, urllib.error.HTTPError, ServiceUnavailable):
                    router_fallback_tried = True
                    pass

            if body and not fallback_used:
                model = body.get("model", "")
                if "combo" in model or "main-rr" in model:
                    log.warning(f"Combo model '{model}' failed (HTTP {e.code}) — "
                                f"9router combo router exhausted all providers. "
                                f"Response: {resp_body[:200].decode(errors='replace')}")

            # _handle_error must run first: friendly response depends on error_info
            raw_body_text = resp_body.decode(errors="replace")
            error_info = self._handle_error(e.code, raw_body_text, body)
            friendly = self._friendly_error_message(error_info, (body or {}).get("model", ""))

            if friendly:
                access_cd = error_info.get("cooldown") or error_info
                resp_body = json.dumps({
                    "error": {
                        "message": friendly,
                        "type": "account_access_error",
                        "access_type": access_cd.get("type", ""),
                        "code": e.code,
                    }
                }).encode()
            else:
                try:
                    err_normalized = normalize_error(resp_body)
                    resp_body = json.dumps(err_normalized).encode()
                except Exception as norm_err:
                    log.warning(f"Error normalization failed: {norm_err}", extra={"event": "normalize_error_failed"})
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp_body)))
            self.end_headers()
            self.wfile.write(resp_body)

            # Record failed request in metrics
            if body:
                model = body.get("model", "")
                provider = model.split("/")[0] if "/" in model else model
                sem_type = (error_info.get("cooldown") or error_info).get("type") or f"http_{e.code}"
                self._record_usage(body, resp_body, start_time, provider, model, False, sem_type)

        except urllib.error.URLError as e:
            # Attempt fallback via meta-router
            if target_router and self.meta_selector and not fallback_used:
                try:
                    self.meta_selector.on_failure(target_router.name, "connection_error")
                    fallback_router = self.meta_selector.select_router(model=(body or {}).get("model", "") if body else "")
                    if fallback_router:
                        fallback_url = fallback_router.url.rstrip("/") + path
                        fallback_req = urllib.request.Request(fallback_url, data=data, headers=headers, method=self.command)
                        fallback_req.add_header("Accept", "text/event-stream, application/json")
                        opener = self.opener if self.opener is not None else urllib.request.build_opener()
                        fallback_resp = opener.open(fallback_req, timeout=180)
                        is_chat = self.path in ("/v1/chat/completions", "/chat/completions")
                        fb_body = self._emit_upstream_response(fallback_resp, is_chat, fallback_used=True)
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

    def _handle_error(self, status: int, body_text: str, request_body: dict = None) -> dict:
        """Record error in health registry with smart routing awareness.

        Returns parsed error_info (for building a friendly client response).
        """
        error_info = parse_error(status, body_text)
        if not self.registry:
            return error_info

        model = (request_body or {}).get("model", "")
        provider = model.split("/")[0] if "/" in model else model

        # Body may embed '[provider/model] [status]:' — extract real hints
        # (parse_log_line with a fabricated '❌ unknown' prefix would poison
        # provider with 'unknown' and drop model via .get on an existing None)
        if not provider or provider == model:
            p, m = extract_provider_model(body_text)
            if p:
                provider = p
            if m:
                model = m

        if provider:
            self.registry.mark_error(
                provider=provider,
                error_info=error_info,
                model=model if error_info.get("model_specific") else None,
            )
            if self.audit:
                self.audit.cooldowns_applied += 1

        return error_info

    _ACCESS_ERROR_TYPES = {
        "subscription_level",
        "no_credit",
        "no_credentials",
        "model_not_found",
        "monthly_limit",
        "daily_free_exhausted",
        "payment_required",
        "paid_required",
        "auth_invalid",
        "invalid_subscription",
    }

    def _friendly_error_message(self, error_info: dict, model: str) -> str | None:
        """Human-readable message for account/access errors; None otherwise."""
        cd = error_info.get("cooldown") or error_info
        etype = cd.get("type", "")
        if etype not in self._ACCESS_ERROR_TYPES:
            return None

        messages = {
            "subscription_level": (
                f"Model '{model}' requires a paid subscription on its provider. "
                "Your plan does not include it — pick a free model instead."
            ),
            "no_credit": (
                f"Provider for '{model}' is out of credits. "
                "Top up the account or use another provider."
            ),
            "no_credentials": (
                f"No active API credentials for the provider of '{model}'. "
                "Configure the API key or choose another provider."
            ),
            "model_not_found": (
                f"Model '{model}' does not exist in the 9router catalog. "
                "Check the model name (see GET /v1/models)."
            ),
            "monthly_limit": (
                f"Provider for '{model}' reached its monthly request limit. "
                "Try again later or use another provider."
            ),
            "daily_free_exhausted": (
                f"Provider for '{model}' exhausted its daily free allocation. "
                "Try again tomorrow or use another provider."
            ),
            "payment_required": (
                f"Provider for '{model}' requires payment. "
                "Top up the account or choose a free provider."
            ),
            "paid_required": (
                f"Provider for '{model}' requires a paid plan. "
                "Your plan does not include it."
            ),
            "auth_invalid": (
                f"API key for the provider of '{model}' is invalid or expired. "
                "Fix the credentials or choose another provider."
            ),
            "invalid_subscription": (
                f"Provider for '{model}' rejected the request: invalid subscription. "
                "Check the account plan."
            ),
        }
        return messages.get(etype)



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

    def _filter_combo_providers(self, body: dict) -> str | None:
        """Filter combo model to skip permanently disabled providers."""
        if not self.registry:
            return None

        model = body.get("model", "")
        if "combo" not in model and "main-rr" not in model:
            return None

        # Check if watchdog set a forced fallback model
        forced = os.environ.get("OPENCODE_FALLBACK_MODEL", "").strip()
        if forced:
            provider = forced.split("/")[0]
            if self.registry.is_provider_healthy(provider):
                log.info(f"Watchdog forced fallback: {model} → {forced}")
                return forced
            log.info(f"Watchdog forced fallback '{forced}' unavailable, using smart filter")

        # Thinking combos must resolve to a reasoning model; the candidate
        # list has none, so let the 9router pick one of its 300+.
        if "thinking" in model:
            log.info(f"Combo '{model}': thinking combo — no reasoning candidates, passing through")
            return None

        combo_models = self._get_combo_models()
        available = []
        skipped = []
        for cm in combo_models:
            provider = cm.split("/")[0]
            if self.registry.is_provider_healthy(provider):
                available.append(cm)
            else:
                entry = self.registry.get_provider(provider)
                skipped.append(f"{provider}({entry.get('status','?')})")

        if not available:
            log.warning(
                f"Combo '{model}': ALL {len(combo_models)} providers unavailable. "
                f"Skipped: {', '.join(skipped[:10])}"
            )
            return None

        if skipped:
            log.info(
                f"Combo '{model}': filtered {len(skipped)} dead providers "
                f"({', '.join(skipped[:6])}), {len(available)} healthy remaining"
            )

        if self.smart_router:
            best = self.smart_router.best_model(available, self.registry)
            if best:
                return best
        return available[0]

    def _apply_prompt_limit(self, body: dict) -> dict | None:
        """Check if request exceeds model context or TPM limits, truncate if needed."""
        model = body.get("model", "unknown")
        messages = body.get("messages", [])

        limits = get_model_limits(model)
        # Only truncate when the model has an EXPLICIT entry in model_limits.json.
        # Unknown models fall back to the tiny default (8192 ctx → 6144 effective)
        # which destroys legitimate prompts for large-context models.
        if not _has_explicit_limits(model):
            log.debug("Prompt limit: no explicit limits for model '%s', skipping truncation", model)
            return None
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
                for name, e in self.registry.snapshot().get("providers", {}).items()
            } if self.registry else {},
        }
        payload = json.dumps(data, indent=2, default=str).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _respond_summary(self):
        payload = json.dumps(self.registry.status_summary(), default=str).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

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
        payload = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _handle_reset(self, target: str):
        provider_reg = self.registry.get_provider(target)
        model_reg = self.registry.get_model(target)

        if provider_reg or model_reg:
            if provider_reg:
                self.registry.force_healthy(target)
            elif model_reg:
                self.registry.force_healthy(target.split("/")[0], target)
            payload = json.dumps({"status": "reset", "target": target}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            payload = json.dumps({"error": "unknown provider or model"}).encode()
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

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

            # Combo provider filtering: skip permanently disabled providers.
            # Match on model name (combo-round-robin/combo-fast/combo-thinking/
            # main-rr) — `provider == "combo"` never matched these.
            if self.registry and ("combo" in model or "main-rr" in model):
                healthy_model = self._filter_combo_providers(body)
                if healthy_model:
                    body["_original_model"] = model
                    body["model"] = healthy_model
                    log.info(f"Combo {model} → {healthy_model} (skipped dead providers)")
                    model = healthy_model
                    provider = healthy_model.split("/")[0]
                else:
                    # No suitable healthy provider selected — let the 9router
                    # try (its own health state may be fresher than ours).
                    log.warning(
                        f"Combo '{model}': no suitable healthy provider, "
                        "passing through to downstream router"
                    )

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
        if self.audit:
            self.audit.requests_blocked += 1
        payload = json.dumps({"error": {"message": message, "type": "provider_unavailable"}}).encode()
        self.send_response(503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Content-Length", "0")
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
        self.audit = None
        self._server = None
        self._opener = urllib.request.build_opener()

    def get_handler(self):
        """Create handler class with shared registry + metrics + meta-router."""
        registry = self.registry
        metrics = self.metrics_store
        router = self.smart_router
        meta_registry = self.meta_registry
        meta_selector = self.meta_selector
        audit = self.audit

        class HandlerWithRegistry(HealthProxyHandler):
            pass

        HandlerWithRegistry.registry = registry
        HandlerWithRegistry.metrics_store = metrics
        HandlerWithRegistry.smart_router = router
        HandlerWithRegistry.meta_registry = meta_registry
        HandlerWithRegistry.meta_selector = meta_selector
        HandlerWithRegistry.audit = audit
        return HandlerWithRegistry

    def run(self):
        handler = self.get_handler()
        from socketserver import ThreadingMixIn

        class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
            daemon_threads = True

        self._server = ThreadingHTTPServer(("0.0.0.0", self.port), handler)
        HandlerWithRegistry = handler
        HandlerWithRegistry.opener = self._opener

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
            self._server.serve_forever()
        except KeyboardInterrupt:
            self.shutdown()

    def shutdown(self):
        if self._server:
            self._server.shutdown()
            log.info("Health Proxy server shut down")