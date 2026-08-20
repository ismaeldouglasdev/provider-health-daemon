"""HTTP proxy with health-aware + smart routing for 9router requests.

Extends the basic forwarder with:
  - Prompt limiting (truncate oversized prompts)
  - Health-aware gating (skip cooldown providers)
  - Smart router integration (select best model from combo)
  - Automatic health reset for failed-then-succeeded providers
"""

import hashlib
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
from catalog_sync import sync_disable_dead_model
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
    MAX_FALLBACK_RETRIES,
    RESPONSE_CACHE_TTL,
    RESPONSE_CACHE_MAX_BYTES,
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
    from prompt_limiter import count_tokens, get_explicit_limits, truncate_prompt
except ImportError:
    # Fallback: simple implementations
    log.warning("prompt_limiter not available, using fallback token counter")

    def count_tokens(text: str) -> int:
        return len(text) // 4

    def get_explicit_limits(model_id: str) -> dict | None:
        return None

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


# ── Fast-path: trivial messages answered instantly (no LLM round-trip) ──
# The user's "ping" (connectivity test) used to burn ~12s of thinking time
# + tokens on the upstream model. These get a canned, OpenAI-compatible
# reply from the proxy itself in <1ms.
#
# Deliberately limited to ping/pong: greetings ("hi", "oi") and generic
# words ("ok", "test") can be part of real conversations — only the
# canonical connectivity test is unambiguous.

TRIVIAL_REPLIES: dict[str, str] = {
    "ping": "pong",
    "pong": "ping",
}


def _trivial_reply(body: dict) -> str | None:
    """Canned reply if the last user message is an exact trivial word.

    Conservative by design — never intercept real work:
    only role=user with plain str content, no tools, no multimodal parts,
    and the normalized text must be an exact key of TRIVIAL_REPLIES.
    """
    if body.get("tools") or body.get("tool_choice"):
        return None
    messages = body.get("messages") or []
    if not messages:
        return None
    last = messages[-1]
    if not isinstance(last, dict):
        return None
    if last.get("role") != "user":
        return None
    if last.get("tool_calls"):
        return None
    content = last.get("content")
    if not isinstance(content, str):
        return None  # multimodal parts / image blocks → not trivial
    text = content.strip().strip("!?.,;: \t\n").lower()
    if not text:
        return None
    # Multi-word text is real work (e.g. "ping the server at 10.0.0.1") —
    # never auto-reply to sentences, only exact single tokens.
    if " " in text or "\n" in text:
        return None
    return TRIVIAL_REPLIES.get(text)


# ── api.airforce fake-200 detection ─────────────────────────────────
# api.airforce returns HTTP 200 + plain text "The model does not exist in
# https://api.airforce" (instead of a proper 404 error JSON) for model ids
# it does not serve. The health-daemon would record that 200 as SUCCESS →
# the dead model never enters cooldown and keeps being selected forever.
# Detect the marker and treat it as model_not_found so the model gets a
# cooldown + pushed to the 9router disabled registry (same path as a real
# 404 "model does not exist" from any other provider).

AIRFORCE_FAKE_MARKER = "The model does not exist in https://api.airforce"


def _is_airforce_fake_response(resp_body: bytes) -> bool:
    """Detect airforce's fake 200 'model does not exist' (raw JSON or SSE)."""
    if not resp_body:
        return False
    return AIRFORCE_FAKE_MARKER in resp_body.decode("utf-8", errors="replace")


def _is_empty_chat_response(resp_body: bytes) -> bool:
    """Detect HTTP 200 with no usable content (dead model / exhausted quota).

    Some upstreams (antigravity gemini-pro-default alias, cursor quota) answer
    200 with an empty or content-less body for models they cannot serve. The
    health gate only inspects the HTTP status, so these would be recorded as
    healthy and stay in rotation forever — same class of bug as the airforce
    fake 200. Handles both non-streaming JSON and SSE bodies.
    """
    if not resp_body or not resp_body.strip():
        return True
    text = resp_body.decode("utf-8", errors="replace")
    if "data:" in text:
        # Some upstreams (e.g. 9router → glm) append a trailing "data: [DONE]"
        # to a NON-streaming JSON body. The JSON itself then has no "data: "
        # prefix, so a naive data:-only loop would skip it and flag a live
        # reasoning model as empty. Parse any line that looks like JSON —
        # prefixed or not — and bail as soon as one has usable content.
        for line in text.split("\n"):
            stripped = line.strip()
            if stripped.startswith("data: "):
                stripped = stripped[6:].strip()
            if not stripped or stripped == "[DONE]":
                continue
            if stripped.startswith("{"):
                try:
                    chunk = json.loads(stripped)
                except (json.JSONDecodeError, ValueError):
                    # Trailing "data: [DONE]" may be glued to the JSON on the
                    # same line (no newline) — strip it and retry once.
                    if "data: " in stripped:
                        try:
                            chunk = json.loads(stripped.split("data: ")[0])
                        except (json.JSONDecodeError, ValueError):
                            continue
                    else:
                        continue
            else:
                continue
            for c in chunk.get("choices", []):
                delta = c.get("delta") or c.get("message") or {}
                content = delta.get("content")
                if isinstance(content, list):
                    content = "".join(
                        str(b.get("text", "")) for b in content if isinstance(b, dict)
                    )
                if content:
                    return False
                if delta.get("reasoning") or delta.get("reasoning_content"):
                    return False  # reasoning streamed while content pending
                if c.get("finish_reason") == "length":
                    return False  # truncated mid-generation = model alive
        return True
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return False  # non-JSON error text — normal error path handles it
    if not isinstance(data, dict) or "error" in data:
        return False
    choices = data.get("choices") or []
    if not choices:
        return True
    for c in choices:
        msg = c.get("message") or c.get("delta") or {}
        content = msg.get("content")
        if isinstance(content, list):
            content = "".join(
                str(b.get("text", "")) for b in content if isinstance(b, dict)
            )
        if content and str(content).strip():
            return False
        if msg.get("tool_calls"):
            return False  # tool-call responses may legitimately have empty text
        if msg.get("reasoning") or msg.get("reasoning_content"):
            return False  # reasoning models: content may trail the reasoning block
        if c.get("finish_reason") == "length":
            return False  # truncated mid-generation = model alive, not empty
    return True


def _normalize_sse_line(line: bytes) -> bytes:
    """Normalize one SSE data line (content blocks → string) in place."""
    if line.startswith(b"data: ") and b"[DONE]" not in line:
        try:
            chunk = json.loads(line[6:].decode("utf-8", errors="replace"))
            return ("data: " + json.dumps(normalize_sse_chunk(chunk), ensure_ascii=False) + "\n").encode()
        except (json.JSONDecodeError, ValueError):
            return line
    return line


class EmptyUpstreamResponse(Exception):
    """HTTP 200 with no usable content (dead model / exhausted quota).

    Raised by _emit_upstream_response BEFORE anything is written to the
    client so _forward can cooldown the model and retry the next healthy
    one. Without this the empty 200 reaches the client and the session
    completes silently (opencode "silent session" bug).
    """

    def __init__(self, body: bytes = b""):
        super().__init__("empty upstream 200 response")
        self.body = body


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

    # Response cache: key → (expires_at, body_bytes). Serves identical
    # retries within TTL without re-hitting upstream. Shared across
    # requests (class-level, like _combo_cache).
    _response_cache: dict = {}
    _response_cache_bytes: int = 0

    # Per-request state (set fresh in do_POST): cache key of the ORIGINAL
    # request body (pre-combo-substitution) so retries with the same combo
    # name hit the same entry; global-fallback attempt counter + tried models.
    _request_cache_key: str = ""
    _fallback_attempts: int = 0
    _tried_models: set = set()

    def _global_fallback_chain(self, force_refresh: bool = False, force_broad: bool = False) -> list[str]:
        """Full-catalog fallback chain: healthy models ranked, best first.

        Unlike _get_combo_models (cached combo list), this ranks against the
        complete catalog so a 5xx from one model can fall back to any other
        healthy model upstream. force_refresh bypasses the combo cache TTL.
        force_broad skips the 9router disabled registry (same as combo pool).
        """
        if not self.registry or not self.smart_router:
            return []
        if force_refresh:
            SmartRouter.invalidate_combo_cache()
        try:
            catalog = SmartRouter.get_default_combos(skip_disabled=force_broad)
        except Exception as e:
            log.warning(f"Global fallback: catalog fetch failed: {e}")
            return []
        if not catalog:
            return []
        if self.registry:
            catalog = self.registry.get_available_models(catalog)
        if not catalog and not force_broad:
            try:
                catalog = SmartRouter.get_default_combos(skip_disabled=True)
                if self.registry:
                    catalog = self.registry.get_available_models(catalog)
            except Exception as e:
                log.warning(f"Global fallback broad catalog failed: {e}")
        if not catalog:
            return []
        return self.smart_router.fallback_chain(catalog, self.registry)

    def _next_fallback_model(self, tried: set) -> str | None:
        """Best catalog model not yet tried, or None when catalog exhausted."""
        chain = self._global_fallback_chain()
        for m in chain:
            if m not in tried:
                return m
        return None

    def _cache_key(self, body: dict) -> str:
        """Stable key: model + messages + sampling params (stream always False)."""
        payload = {
            "model": body.get("model", ""),
            "messages": body.get("messages", []),
            "temperature": body.get("temperature"),
            "top_p": body.get("top_p"),
            "max_tokens": body.get("max_tokens"),
            "stream": False,
        }
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _cache_get(self, body: dict) -> bytes | None:
        if not body or body.get("stream"):
            return None
        return self._cache_get_key(self._cache_key(body))

    def _cache_get_key(self, key: str) -> bytes | None:
        entry = self._response_cache.get(key)
        if not entry:
            return None
        expires_at, cached = entry
        if time.time() > expires_at:
            self._response_cache.pop(key, None)
            self._response_cache_bytes -= len(cached)
            return None
        return cached

    def _cache_put(self, body: dict, resp_body: bytes) -> None:
        if not body or body.get("stream") or not resp_body:
            return
        self._cache_put_key(self._cache_key(body), resp_body)

    def _cache_put_key(self, key: str, resp_body: bytes) -> None:
        if not key or not resp_body:
            return
        # Never cache error bodies — a cached error would poison retries.
        try:
            parsed = json.loads(resp_body)
            if isinstance(parsed, dict) and parsed.get("error"):
                return
        except (json.JSONDecodeError, ValueError):
            return
        now = time.time()

        # Evict expired entries first, then oldest-expiry until under the cap.
        expired = [k for k, (exp, _) in self._response_cache.items() if exp < now]
        for k in expired:
            _, b = self._response_cache.pop(k)
            self._response_cache_bytes -= len(b)
        while self._response_cache and self._response_cache_bytes + len(resp_body) > RESPONSE_CACHE_MAX_BYTES:
            oldest_key = min(self._response_cache, key=lambda k: self._response_cache[k][0])
            _, b = self._response_cache.pop(oldest_key)
            self._response_cache_bytes -= len(b)

        self._response_cache[key] = (now + RESPONSE_CACHE_TTL, resp_body)
        self._response_cache_bytes += len(resp_body)

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

    def _emit_upstream_response(self, resp, is_chat: bool, fallback_used: bool = False,
                                start_time=None) -> tuple[bytes, int]:
        """Stream upstream response to client; returns (normalized body sent, ttft_ms).

        SSE (chat streaming) is forwarded chunk-by-chunk with Transfer-Encoding:
        chunked so the client sees tokens as they arrive (low TTFT) instead of
        waiting for the full body. Non-streaming bodies are buffered, normalized,
        and sent with Content-Length.

        ttft_ms = time to first byte of the body (only meaningful for streaming,
        measured at the first readline; 0 for non-streaming, where the caller
        falls back to duration).

        Empty-200 guard: dead models / exhausted quotas answer HTTP 200 with an
        empty body. The health gate only inspects HTTP status, so emitting it
        here would hand the client a silent empty completion (opencode "session
        didn't respond"). The full body is read and checked BEFORE
        send_response; an empty one raises EmptyUpstreamResponse so _forward
        can fall back to the next healthy model.
        """
        if self.audit:
            self.audit.requests_proxied += 1

        content_type = resp.headers.get("Content-Type", "")
        is_streaming = is_chat and "event-stream" in content_type
        first_line = b""
        ttft_ms = 0
        if is_streaming:
            # Upstreams sometimes advertise text/event-stream but return a
            # plain JSON body (9router->glm glues "data: [DONE]" onto it).
            # Detect real SSE by the "data: " prefix of the first line.
            first_line = resp.readline()
            if start_time is not None:
                ttft_ms = int((time.time() - start_time) * 1000)
            if not first_line.startswith(b"data: "):
                is_streaming = False
                content_type = "application/json"

        # ── Empty-200 guard (chat only) ──────────────────────────────────
        # Read the whole body up-front and raise before any byte reaches the
        # client, so _forward can retry the next healthy model. Non-chat
        # endpoints (embeddings etc.) are forwarded untouched.
        sent: list[bytes] = []
        body: bytes = b""
        if is_chat:
            if is_streaming:
                for line in [first_line] + list(resp):
                    sent.append(_normalize_sse_line(line))
                if _is_empty_chat_response(b"".join(sent)):
                    raise EmptyUpstreamResponse(b"".join(sent))
            else:
                body = first_line + resp.read()
                body = self._normalize_response_body(body, content_type)
                if _is_empty_chat_response(body):
                    raise EmptyUpstreamResponse(body)
        elif not is_streaming:
            body = first_line + resp.read()

        self.send_response(resp.status)
        for k, v in resp.headers.items():
            if k.lower() not in ("transfer-encoding", "content-encoding", "content-length"):
                if k.lower() == "content-type" and not is_streaming:
                    v = "application/json"
                self.send_header(k, v)
        if fallback_used:
            self.send_header("X-Health-Proxy-Fallback", "true")

        if is_streaming:
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for line in sent:
                self.wfile.write(f"{len(line):X}\r\n".encode() + line + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            return b"".join(sent), ttft_ms

        # Response cache: store non-streaming 200 chat responses so identical
        # retries within TTL are served from cache (retry-after-crash on the
        # same prompt). Key was computed in do_POST from the ORIGINAL request
        # body (pre-combo-substitution). Error bodies are skipped in _cache_put_key.
        if is_chat and resp.status == 200 and getattr(self, "_request_cache_key", ""):
            self._cache_put_key(self._request_cache_key, body)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return body, ttft_ms

    def _get_combo_models(self, force_broad: bool = False) -> list[str]:
        """Get cached list of combo models from 9router, health-filtered."""
        now = time.time()
        if (
            not force_broad
            and now - self._combo_cache_time < COMBO_REFRESH_INTERVAL
            and self._combo_cache
        ):
            if self.registry:
                avail = self.registry.get_available_models(self._combo_cache)
                if avail:
                    return avail
                log.warning(
                    "Cached combo pool models are all in cooldown; "
                    "forcing broad refresh with full catalog"
                )
                SmartRouter.invalidate_combo_cache()
                return self._get_combo_models(force_broad=True)
            return self._combo_cache

        models = SmartRouter.get_default_combos(skip_disabled=force_broad)
        if self.registry:
            available = self.registry.get_available_models(models)
            if not available and not force_broad:
                log.warning(
                    "Combo pool empty after disabled filter (%d candidates); "
                    "retrying with full catalog + health filter only",
                    len(models),
                )
                SmartRouter.invalidate_combo_cache()
                return self._get_combo_models(force_broad=True)
            models = available

        self._combo_cache = models
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
                if target_router.auth and target_router.auth.get("value"):
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
                resp_body, ttft_ms = self._emit_upstream_response(resp, is_chat, fallback_used, start_time)

                if target_router and self.meta_selector:
                    self.meta_selector.on_success(target_router.name)

                if body and resp.status == 200:
                    self._record_upstream_health(body, resp_body, start_time, ttft_ms)

        except EmptyUpstreamResponse as e:
            # HTTP 200 with an empty body (dead model / exhausted quota) was
            # detected in _emit_upstream_response BEFORE any byte reached the
            # client — nothing was emitted yet, so this request is replayable.
            # Record health (marks 15m empty_response model cooldown) then
            # retry with the next healthy model via the same global fallback
            # gate used for 5xx/access errors.
            if body:
                self._record_upstream_health(body, e.body, start_time)
                if getattr(self, "_fallback_attempts", 0) < MAX_FALLBACK_RETRIES:
                    tried = set(getattr(self, "_tried_models", set()))
                    tried.add(body.get("model", ""))
                    self._tried_models = tried
                    next_model = self._next_fallback_model(tried)
                    if next_model:
                        self._fallback_attempts = getattr(self, "_fallback_attempts", 0) + 1
                        log.warning(
                            f"Global fallback {self._fallback_attempts}/{MAX_FALLBACK_RETRIES}: "
                            f"Empty 200 on '{body.get('model')}' → retrying with '{next_model}'"
                        )
                        body["model"] = next_model
                        self._router_selected = True
                        self._forward(body)
                        return
            # No healthy model left — fail loudly instead of a silent empty 200
            self._respond_unavailable("empty upstream response")
            return

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
                        fb_body, fb_ttft = self._emit_upstream_response(fallback_resp, is_chat, fallback_used=True, start_time=start_time)
                        if body and fallback_resp.status == 200:
                            self._record_upstream_health(body, fb_body, start_time, fb_ttft)
                        return
                    router_fallback_tried = True
                except (urllib.error.URLError, urllib.error.HTTPError, ServiceUnavailable, EmptyUpstreamResponse):
                    router_fallback_tried = True
                    pass

            # ── Global fallback: retry 5xx / access-error with next healthy model ──
            # A 5xx here means the router's chosen provider failed at runtime.
            # Access errors (no credit, no credentials, model not found,
            # subscription/weekly/monthly limit) are non-transient: routing
            # around them via the catalog is safe and required — otherwise the
            # error leaks to the agent (e.g. 402 insufficient_balance on a dead
            # provider stalls opencode's compaction with "provider temporarily
            # unavailable"). Generic/transient 429 (rate limit) never triggers
            # fallback. HTTPError is raised by opener.open() BEFORE any SSE is
            # emitted, so streaming requests CAN be retried with another model —
            # only mid-stream connection failures (URLError inside
            # _emit_upstream_response) can't be replayed.
            raw_body_text = resp_body.decode(errors="replace")
            error_info = self._handle_error(e.code, raw_body_text, body)
            fallback_etype = (error_info.get("cooldown") or error_info).get("type", "")
            if (
                body
                and (
                    500 <= e.code <= 599
                    or fallback_etype in self._ACCESS_ERROR_TYPES
                    or (
                        getattr(self, "_router_selected", False)
                        and 400 <= e.code <= 499
                        and fallback_etype not in self._RATE_LIMIT_ERROR_TYPES
                    )
                )
                and getattr(self, "_fallback_attempts", 0) < MAX_FALLBACK_RETRIES
            ):
                tried = set(getattr(self, "_tried_models", set()))
                tried.add(body.get("model", ""))
                self._tried_models = tried
                next_model = self._next_fallback_model(tried)
                if next_model:
                    self._fallback_attempts = getattr(self, "_fallback_attempts", 0) + 1
                    log.warning(
                        f"Global fallback {self._fallback_attempts}/{MAX_FALLBACK_RETRIES}: "
                        f"HTTP {e.code} on '{body.get('model')}' → retrying with '{next_model}'"
                    )
                    body["model"] = next_model
                    self._router_selected = True
                    self._forward(body)
                    return

            if body and not fallback_used:
                model = body.get("model", "")
                if "combo" in model or "main-rr" in model:
                    log.warning(f"Combo model '{model}' failed (HTTP {e.code}) — "
                                f"9router combo router exhausted all providers. "
                                f"Response: {resp_body[:200].decode(errors='replace')}")

            # error_info already computed above (fallback decision depends on it)
            friendly = self._friendly_error_message(error_info, (body or {}).get("model", ""))

            # An unclassified 4xx from a router-selected model (combo/smart-
            # routing/global-fallback picked it) must never reach the agent raw
            # — a bare 400/404 from a dead endpoint stalls opencode. Shield as
            # generic 503 when no fallback model is left.
            shielded = False

            if friendly:
                # Anti-leak: access-error detail goes to server log only — the
                # agent gets a generic 503 so oh-my-openagent falls back silently.
                access_cd = error_info.get("cooldown") or error_info
                log.warning(
                    "Access error model=%s type=%s code=%s: %s",
                    (body or {}).get("model", ""),
                    access_cd.get("type", ""),
                    e.code,
                    raw_body_text[:300],
                )
                resp_body = json.dumps({
                    "error": {
                        "message": "provider temporarily unavailable",
                        "type": "provider_unavailable",
                    }
                }).encode()
            else:
                shielded = (
                    getattr(self, "_router_selected", False)
                    and 400 <= e.code <= 499
                    and fallback_etype not in self._RATE_LIMIT_ERROR_TYPES
                )
                if shielded:
                    log.warning(
                        "Router-selected model '%s' failed HTTP %s with no fallback "
                        "— shielding as 503 (anti-leak)",
                        (body or {}).get("model", ""),
                        e.code,
                    )
                    resp_body = json.dumps({
                        "error": {
                            "message": "provider temporarily unavailable",
                            "type": "provider_unavailable",
                        }
                    }).encode()
                else:
                    try:
                        err_normalized = normalize_error(resp_body)
                        resp_body = json.dumps(err_normalized).encode()
                    except Exception as norm_err:
                        log.warning(f"Error normalization failed: {norm_err}", extra={"event": "normalize_error_failed"})
            self.send_response(503 if shielded else e.code)
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
                        fb_body, fb_ttft = self._emit_upstream_response(fallback_resp, is_chat, fallback_used=True, start_time=start_time)
                        if body and fallback_resp.status == 200:
                            self._record_upstream_health(body, fb_body, start_time, fb_ttft)
                        return
                except (urllib.error.URLError, urllib.error.HTTPError, ServiceUnavailable, EmptyUpstreamResponse):
                    pass

            self._respond_unavailable(f"Connection error: {e.reason}")
            if body:
                model = body.get("model", "")
                provider = model.split("/")[0] if "/" in model else model
                self._record_usage(body, b"", start_time, provider, model, False, "connection_error")

    def _record_upstream_health(self, body, resp_body, start_time, ttft_ms=0):
        """Record provider health + usage after a 200, with airforce-fake guard.

        api.airforce answers HTTP 200 + "The model does not exist in
        https://api.airforce" text for model ids it doesn't serve; treating
        that 200 as success keeps dead models in rotation forever. Fake 200s
        are recorded as model_not_found → cooldown + 9router disabled registry.
        """
        if not body or not body.get("model"):
            return
        model = body["model"]
        provider = model.split("/")[0]

        if _is_airforce_fake_response(resp_body):
            fake_text = resp_body.decode("utf-8", errors="replace")
            log.warning(
                "Fake 200 from api.airforce for model=%s — treating as "
                "model_not_found (airforce returns 200 instead of a real 404)",
                model,
            )
            self._handle_error(404, fake_text, body)
            self._record_usage(body, resp_body, start_time, provider, model, False, "model_not_found", ttft_ms)
            return

        if _is_empty_chat_response(resp_body):
            log.warning(
                "Empty 200 from upstream for model=%s — applying 15m model cooldown",
                model,
                extra={"event": "empty_200", "provider": provider, "model": model},
            )
            error_info = {
                "hours": 0, "minutes": 15, "type": "empty_response",
                "model_specific": True, "recheck": True
            }
            if self.registry:
                self.registry.mark_error(provider=provider, error_info=error_info, model=model)
            self._record_usage(body, resp_body, start_time, provider, model, False, "empty_response", ttft_ms)
            return

        self.registry.mark_healthy(provider)
        self._record_usage(body, resp_body, start_time, provider, model, True, ttft_ms=ttft_ms)

    def _record_usage(self, request_body: dict, response_body: bytes, start_time: float,
                      provider: str, model: str, success: bool, error_type: str = None,
                      ttft_ms: int = 0):
        """Record request metrics."""
        if not self.metrics_store:
            return

        duration_ms = int((time.time() - start_time) * 1000)

        # ttft_ms = real time-to-first-byte from _emit_upstream_response
        # (streaming only). Fall back to duration when it wasn't measured
        # (non-streaming buffered responses, error paths).
        if ttft_ms <= 0:
            ttft_ms = duration_ms

        # Try to extract tokens from response
        tokens_in = 0
        tokens_out = 0
        tokens_cache = 0

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
                model = f"{p}/{m}" if p and "/" not in m else m

        # Combo names (combo-round-robin, main-rr, kr/auto, …) have no "/" and
        # are VIRTUAL router names, not real providers/models. Marking them in
        # the health registry creates phantom cooldown entries (e.g. a
        # "combo-thinking" provider) that later make the health gate reject
        # every combo request with a 503 "in cooldown" — freezing opencode.
        # Exception: if the body named the REAL upstream provider (e.g.
        # "No active credentials for provider: openai"), provider was replaced
        # above — mark that provider so the health gate skips it instead of
        # hammering the 404. The virtual combo model is never stored.
        if "/" not in model:
            if not provider or provider == model:
                return error_info
            model = None

        if provider:
            self.registry.mark_error(
                provider=provider,
                error_info=error_info,
                model=model if error_info.get("model_specific") else None,
            )
            if self.audit:
                self.audit.cooldowns_applied += 1

        # Catalog consistency: dead models (404/does-not-exist) are pushed to
        # the 9router disabled registry so its catalog stops advertising them.
        if model and error_info.get("model_specific"):
            sync_disable_dead_model(provider, model, error_info)

        return error_info

    _ACCESS_ERROR_TYPES = {
        "subscription_level",
        "no_credit",
        "no_credentials",
        "model_not_found",
        "monthly_limit",
        "weekly_limit",
        "daily_free_exhausted",
        "payment_required",
        "paid_required",
        "auth_invalid",
        "invalid_subscription",
    }

    _RATE_LIMIT_ERROR_TYPES = {
        "generic_429",
        "unknown_429",
        "rate_limit_rpm",
        "rate_limit_tpd",
        "rate_limit_until",
        "daily_quota_exceeded",
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
            "weekly_limit": (
                f"Model '{model}' reached its weekly usage limit. "
                "Try again next week or use another provider."
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



    def _prompt_tokens(self, body: dict) -> int:
        """Estimate prompt size in tokens from the request body."""
        try:
            all_text = "\n".join(
                m.get("content", "") or ""
                if isinstance(m.get("content"), str)
                else json.dumps(m.get("content", ""))
                for m in (body.get("messages") or [])
            )
            return count_tokens(all_text)
        except Exception:
            return 0

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
            best = self.smart_router.best_model(
                combo_models,
                self.registry,
                {"prompt_tokens": self._prompt_tokens(body)},
            )
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

        # Prompt size in tokens: models whose REAL context window can't fit
        # the request must be skipped. The 9router catalog reports ctx 128000
        # for samba free models, but the upstream only accepts 8192 → 400
        # "Max_len exceeded" on large agentic prompts (and empty compactions).
        prompt_tokens = 0
        try:
            all_text = "\n".join(
                m.get("content", "") or ""
                if isinstance(m.get("content"), str)
                else json.dumps(m.get("content", ""))
                for m in (body.get("messages") or [])
            )
            prompt_tokens = count_tokens(all_text)
        except Exception as exc:
            log.debug("Prompt size estimate failed: %s", exc)

        for cm in combo_models:
            provider = cm.split("/")[0]
            # Skip models whose real context can't hold the prompt: catalog
            # ctx is often inflated (samba → 8192 real vs 128000 catalog);
            # sending an oversized prompt → 400 → combo retry loop. Only
            # filters with EXPLICIT model_limits.json entries (unknown ctx
            # = respect the catalog, don't guess).
            limits = get_explicit_limits(cm)
            ctx = limits.get("context", 0) or 0 if limits else 0
            if ctx and prompt_tokens > int(ctx * 0.75):
                skipped.append(f"{cm}(ctx {ctx} < prompt {prompt_tokens})")
                continue
            # Filter on MODEL availability, not just provider health: a
            # healthy provider can still have specific models in cooldown
            # (e.g. groq healthy but groq/openai/gpt-oss-120b in
            # context_length cooldown). Picking such a model → 503 → the
            # client retries the same combo → infinite retry loop.
            if self.registry.is_model_available(cm):
                available.append(cm)
            else:
                entry = self.registry.get_model(cm) or self.registry.get_provider(provider)
                skipped.append(f"{cm}({entry.get('status','?')})")

        if not available:
            log.warning(
                f"Combo '{model}': ALL {len(combo_models)} models unavailable. "
                f"Skipped: {', '.join(skipped[:10])}"
            )
            # Try broad catalog with force_broad=True to bypass 9router disabled registry
            broad_models = self._get_combo_models(force_broad=True)
            broad_available = [m for m in broad_models if self.registry.is_model_available(m)]
            if broad_available:
                best = self.smart_router.best_model(
                    broad_available,
                    self.registry,
                    {"prompt_tokens": prompt_tokens},
                ) if self.smart_router else broad_available[0]
                log.info(f"Combo '{model}': broad pool fallback → {best} ({len(broad_available)} healthy)")
                return best

            # Global fallback: the combo's candidates are all dead — try the
            # FULL catalog before giving up (pass-through returns a 503 from
            # the 9router combo router, which the client retries in a loop).
            chain = self._global_fallback_chain(force_refresh=True, force_broad=True)
            if chain:
                best = self.smart_router.best_model(
                    chain,
                    self.registry,
                    {"prompt_tokens": prompt_tokens},
                ) if self.smart_router else chain[0]
                log.info(f"Combo '{model}': global fallback → {best} (full catalog, {len(chain)} healthy)")
                return best
            return None

        if skipped:
            log.info(
                f"Combo '{model}': filtered {len(skipped)} unavailable models "
                f"({', '.join(skipped[:6])}), {len(available)} healthy remaining"
            )

        if self.smart_router:
            best = self.smart_router.best_model(
                available,
                self.registry,
                {"prompt_tokens": prompt_tokens},
            )
            if best:
                return best
            # best_model found nothing usable (e.g. all remaining candidates
            # permanently blocked) — pass through instead of blindly returning
            # available[0]: that model may be in cooldown → 503 → retry loop.
            log.warning(
                f"Combo '{model}': smart router found no usable model among "
                f"{len(available)} candidates, passing through to downstream router"
            )
            return None
        return available[0] if available else None

    def _apply_prompt_limit(self, body: dict) -> dict | None:
        """Check if request exceeds model context or TPM limits, truncate if needed."""
        model = body.get("model", "unknown")
        messages = body.get("messages", [])

        limits = get_explicit_limits(model)
        # Only truncate when the model has an EXPLICIT entry in model_limits.json.
        # Unknown models fall back to the tiny default (8192 ctx → 6144 effective)
        # which destroys legitimate prompts for large-context models.
        if not limits:
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
        """List only the filtered combo pool (free models), not the raw catalog.

        The combo virtual models (main-rr / combo-round-robin) pick from
        SmartRouter's filtered pool (free-only). Listing every healthy router's
        full catalog here (which includes paid models like cu/*, cx/*, bpm/*)
        would defeat the free-only policy — the CLI would see paid models as
        selectable. Use the same pool the combo actually routes through.
        """
        models = self._get_combo_models()
        if not models:
            self._respond_unavailable("no combo models available")
            return

        all_models = [
            {
                "id": model_id,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "combo-round-robin",
            }
            for model_id in models
        ]

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

            # Per-request state: reset global-fallback counter and compute the
            # response-cache key from the ORIGINAL body (pre-combo-substitution)
            # — a retry sends the same combo name, so it must map to the same key.
            self._fallback_attempts = 0
            self._tried_models = set()
            self._router_selected = False
            self._request_cache_key = "" if body.get("stream") else self._cache_key(body)

            # Fast-path: trivial single-word messages ("ping") answered by the
            # proxy itself — never reaches the upstream LLM (12s of thinking
            # time + tokens saved). Must run before router/health gates.
            trivial = _trivial_reply(body)
            if trivial is not None:
                log.info(f"Fast-path trivial reply for '{model}' → {trivial!r}")
                self._respond_trivial(trivial, model, bool(body.get("stream")))
                return

            # Response cache: identical retries within TTL served without
            # re-hitting upstream (retry-after-crash on the same prompt). Read
            # BEFORE the router health gate so cache hits survive router outages.
            if self._request_cache_key:
                cached = self._cache_get_key(self._request_cache_key)
                if cached is not None:
                    log.info(f"Response cache hit for '{model}' ({len(cached)} bytes)")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(cached)))
                    self.send_header("X-Health-Proxy-Cache", "hit")
                    self.end_headers()
                    self.wfile.write(cached)
                    return

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
                    self._router_selected = True
                    log.info(f"Combo {model} → {healthy_model} (skipped dead providers)")
                    model = healthy_model
                    provider = healthy_model.split("/")[0]
                else:
                    log.warning(
                        f"Combo '{model}': no healthy model in pool "
                        f"(catalog exhausted or all in cooldown)"
                    )
                    self._respond_unavailable(
                        "no healthy combo models available — all providers in cooldown"
                    )
                    return

            # Health gate: check before forwarding. Combo names (no "/") are
            # virtual router aliases — never gate them on provider/model health
            # (a phantom combo entry would 503 every request and freeze opencode).
            if self.registry and "/" in model:
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
                        self._router_selected = True
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

    def _respond_trivial(self, reply: str, model: str, stream: bool):
        """Answer a trivial message ("ping" → "pong") from the proxy itself.

        Never touches the upstream LLM: <1ms instead of seconds of thinking
        time, zero tokens. Emits an OpenAI-compatible chat.completion body
        (or SSE chunks when the client requested streaming).
        """
        if self.audit:
            self.audit.requests_trivial += 1
        now = int(time.time())
        cid = f"chatcmpl-trivial-{now}-{id(self) & 0xFFFF}"
        if stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            chunks = [
                {
                    "id": cid, "object": "chat.completion.chunk", "created": now,
                    "model": model,
                    "choices": [{"index": 0, "delta": {"role": "assistant", "content": reply}, "finish_reason": None}],
                },
                {
                    "id": cid, "object": "chat.completion.chunk", "created": now,
                    "model": model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                },
            ]
            for chunk in chunks:
                line = ("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode()
                self.wfile.write(f"{len(line):X}\r\n".encode() + line + b"\r\n")
                self.wfile.flush()
            done = b"data: [DONE]\n\n"
            self.wfile.write(f"{len(done):X}\r\n".encode() + done + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            return
        payload = {
            "id": cid, "object": "chat.completion", "created": now, "model": model,
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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