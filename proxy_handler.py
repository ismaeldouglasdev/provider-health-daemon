"""HTTP proxy with health-aware routing for 9router requests.

Inherits core forwarding from prompt-limiter/proxy/proxy_server.py
with health registry integration.
"""

import json
import logging
import re
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler

from config import (
    HEALTH_PROXY_PORT,
    NINEROUTER_URL,
    NINEROUTER_KEY,
    MODEL_LIMITS_FILE,
    PROMPT_LIMITER_DIR,
)
from health_registry import HealthRegistry
from error_parser import parse_error, parse_log_line

log = logging.getLogger(__name__)

# ── Prompt limiter import ────────────────────────────────────────────
import sys

if str(PROMPT_LIMITER_DIR) not in sys.path:
    sys.path.insert(0, str(PROMPT_LIMITER_DIR))

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
    """HTTP handler with health-aware routing + prompt limiting."""

    registry: HealthRegistry = None  # set by server

    def _forward(self, body=None):
        """Forward to 9router, intercepting health info on errors."""
        path = self.path
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {NINEROUTER_KEY}",
        }
        data = json.dumps(body).encode() if body else None
        url = f"{NINEROUTER_URL}{path}"

        req = urllib.request.Request(url, data=data, headers=headers, method=self.command)
        # Enable streaming for /chat/completions
        req.add_header("Accept", "text/event-stream, application/json")

        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                resp_body = resp.read()
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    if k.lower() not in ("transfer-encoding", "content-encoding", "content-length"):
                        self.send_header(k, v)
                self.send_header("Content-Length", str(len(resp_body)))
                self.end_headers()
                self.wfile.write(resp_body)

                # Store health info if possible
                if self.registry and body and resp.status == 200:
                    model = body.get("model", "")
                    if model:
                        provider = model.split("/")[0]
                        self.registry.mark_healthy(provider)

        except urllib.error.HTTPError as e:
            resp_body = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(resp_body)

            # Parse error for health registry
            self._handle_error(e.code, resp_body.decode(errors="replace"), body)

    def _handle_error(self, status: int, body_text: str, request_body: dict = None):
        """Record error in health registry."""
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

    def _apply_prompt_limit(self, body: dict) -> dict | None:
        """Check if request exceeds model limits and truncate if needed."""
        model = body.get("model", "unknown")
        messages = body.get("messages", [])

        limits = get_model_limits(model)
        max_context = limits.get("context", 8192)
        safe_limit = int(max_context * 0.75)

        all_text = "\n".join(
            m.get("content", "") or ""
            if isinstance(m.get("content"), str)
            else json.dumps(m.get("content", ""))
            for m in messages
        )
        total = count_tokens(all_text)

        if total <= safe_limit:
            return None

        log.warning(f"⚠ {model}: {total}t exceeds {max_context} context — truncating")

        # Concatenate all messages as single text stream for truncation
        truncated = truncate_prompt(all_text, safe_limit)
        remaining_tokens = count_tokens(truncated)

        # Rebuild messages from the tail end - keep system message + as many recent as fit
        # Simple approach: keep last N messages whose total fits
        kept = []
        kept_tokens = 0
        for msg in reversed(messages):
            content = msg.get("content", "")
            if isinstance(content, list):
                content = json.dumps(content)
            msg_tokens = count_tokens(content)
            if kept_tokens + msg_tokens > safe_limit:
                # Keep system/developer messages unconditionally if still room
                if msg.get("role") in ("system", "developer") and kept_tokens < safe_limit * 0.2:
                    kept.insert(0, msg)
                    kept_tokens += msg_tokens
                break
            kept.insert(0, msg)
            kept_tokens += msg_tokens

        new_body = dict(body)
        new_body["messages"] = kept
        new_body["_health_proxy_truncated"] = True
        new_body["_original_tokens"] = total
        new_body["_limited_tokens"] = kept_tokens

        log.info(f"  → {total}t → {kept_tokens}t ({len(kept)} msgs kept)")
        return new_body

    def _find_healthy_alternative(self, body: dict) -> str | None:
        """Given request body with model, find a healthy alternative model."""
        if not self.registry:
            return None

        current = body.get("model", "")
        if not current:
            return None

        # Extract provider
        provider = current.split("/")[0]

        # Check if current model is healthy
        if self.registry.is_model_available(current):
            return None  # already healthy

        # Try to find another model from same combo/fallback info
        # For combo requests, 9router sends model list? Unlikely in request body.
        # Strategy: return None — 9router combo handles its own fallback.
        # We just inform that model is unavailable.
        return None  # let 9router's combo mechanism handle alternatives

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
        self._forward()

    def _respond_status(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        data = {
            "status": "online",
            "forwarding": NINEROUTER_URL,
            "health_file": str(self.registry.filepath),
            "summary": self.registry.status_summary(),
            "providers": {
                name: {"status": e.get("status"), "until": e.get("until"), "reason": e.get("reason")}
                for name, e in self.registry._data.get("providers", {}).items()
            },
        }
        self.wfile.write(json.dumps(data, indent=2, default=str).encode())

    def _respond_summary(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(self.registry.status_summary(), default=str).encode())

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

            # Health gate: check before forwarding
            if self.registry:
                m_ok = self.registry.is_model_available(model) if model else True
                p_ok = self.registry.is_provider_healthy(provider) if provider else True

                if not m_ok:
                    # model-specific cooldown — return 503 with info
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
    """Main server that ties proxy + health registry together."""

    def __init__(self, port: int = HEALTH_PROXY_PORT):
        self.port = port
        self.registry = HealthRegistry()

    def get_handler(self):
        """Create handler class with shared registry."""
        registry = self.registry

        class HandlerWithRegistry(HealthProxyHandler):
            pass

        HandlerWithRegistry.registry = registry
        return HandlerWithRegistry

    def run(self):
        handler = self.get_handler()
        server = HTTPServer(("0.0.0.0", self.port), handler)

        log.info(f"🛡️  Health Proxy → http://localhost:{self.port}")
        log.info(f"   Forwarding → {NINEROUTER_URL}")
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