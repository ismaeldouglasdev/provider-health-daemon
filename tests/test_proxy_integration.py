"""Real HTTP integration test — start proxy + mock routers and verify the full stack.

Launches:
  1. A HealthProxyServer on a random port
  2. Mock downstream router HTTP servers on random ports
  3. Tests the full request flow: client -> proxy -> meta-router -> mock router
"""

import json
import logging
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import pytest

logging.disable(logging.CRITICAL)


def _make_mock_handler(models: list[str], fail_count: int = 0):
    """Factory: returns a MockRouterHandler subclass with instance-scoped state."""
    class _MockHandler(BaseHTTPRequestHandler):
        _models = list(models)
        _fail_count = fail_count
        _call_count = 0

        def _respond_json(self, data: dict, status: int = 200):
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path in ("/v1/models", "/models"):
                self._respond_json({
                    "object": "list",
                    "data": [{"id": m, "object": "model"} for m in self._models],
                })
            else:
                self._respond_json({"error": "not found"}, 404)

        def do_POST(self):
            content_length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(content_length)) if content_length else {}

            _MockHandler._call_count += 1
            if _MockHandler._call_count <= self._fail_count:
                self._respond_json(
                    {"error": {"message": "simulated failure", "type": "server_error"}},
                    503,
                )
                return

            model = body.get("model", "unknown")
            self._respond_json({
                "id": "mock-chat-1",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "mock response"},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
            })

        def log_message(self, fmt, *args):
            pass

    return _MockHandler


class ThreadingMockServer(ThreadingMixIn, HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


@pytest.fixture
def mock_router_servers():
    """Start 2 mock router servers on random ports and return their configs + meta."""
    servers = []
    configs = []
    all_models = {}
    for name, models, fail_count in [
        ("router-mock-a", ["model-a/v1", "model-a/v2"], 0),
        ("router-mock-b", ["model-b/v1"], 1),
    ]:
        handler_cls = _make_mock_handler(models, fail_count)
        server = ThreadingMockServer(("127.0.0.1", 0), handler_cls)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append(server)
        configs.append({
            "name": name,
            "url": f"http://127.0.0.1:{port}",
            "priority": 1,
            "weight": 1,
            "health_check_path": "/v1/models",
            "timeout": 5.0,
            "auth": None,
        })
        all_models[name] = models

    yield configs, all_models

    for s in servers:
        s.shutdown()


@pytest.fixture
def proxy_server(mock_router_servers):
    """Start HealthProxyServer with mock routers."""
    from proxy_handler import HealthProxyServer
    from router_probe import RouterProbe

    configs, all_models = mock_router_servers

    # Patch DOWNSTREAM_ROUTERS in proxy_handler module (from config import DOWNSTREAM_ROUTERS
    # creates a local reference, so patching config.DOWNSTREAM_ROUTERS is not enough)
    import proxy_handler as ph_mod
    import config as cfg_mod
    original_ph_routers = ph_mod.DOWNSTREAM_ROUTERS
    original_cfg_routers = cfg_mod.DOWNSTREAM_ROUTERS
    ph_mod.DOWNSTREAM_ROUTERS = configs
    cfg_mod.DOWNSTREAM_ROUTERS = configs

    try:
        proxy = HealthProxyServer(port=0)
        handler = proxy.get_handler()

        class ThreadingProxy(ThreadingMixIn, HTTPServer):
            allow_reuse_address = True
            daemon_threads = True

        server = ThreadingProxy(("127.0.0.1", 0), handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        # Run probe twice: unknown -> probing -> healthy
        probe = RouterProbe(proxy.meta_registry)
        probe.probe_all()
        probe.probe_all()

        for router_name, models in all_models.items():
            for model_id in models:
                provider = model_id.split("/")[0]
                proxy.registry.mark_healthy(provider, model_id)

        yield f"http://127.0.0.1:{port}", proxy, all_models

        server.shutdown()
    finally:
        ph_mod.DOWNSTREAM_ROUTERS = original_ph_routers
        cfg_mod.DOWNSTREAM_ROUTERS = original_cfg_routers


class TestRealHTTPIntegration:
    """Full-stack test with real HTTP calls against mock routers."""

    def test_health_endpoint_shows_routers(self, proxy_server):
        url, proxy, _ = proxy_server
        resp = urlopen(f"{url}/health")
        data = json.loads(resp.read())
        assert data["status"] == "online"
        assert "router-mock-a" in data["routers"]
        assert "router-mock-b" in data["routers"]

    def test_chat_completion_forwarded(self, proxy_server):
        url, proxy, _ = proxy_server
        req_body = json.dumps({
            "model": "model-a/v1",
            "messages": [{"role": "user", "content": "hello"}],
        }).encode()

        req = Request(
            f"{url}/v1/chat/completions",
            data=req_body,
            headers={"Content-Type": "application/json"},
        )
        resp = urlopen(req)
        data = json.loads(resp.read())
        assert data["choices"][0]["message"]["content"] == "mock response"
        assert resp.status == 200

    def test_model_not_available_on_any_router(self, proxy_server):
        url, proxy, _ = proxy_server
        # Disable all routers so meta-router has no healthy target → 503
        for r in proxy.meta_registry.get_all_routers():
            proxy.meta_registry.mark_unhealthy(r.name, "admin_disable")
        req_body = json.dumps({
            "model": "unknown-model/v1",
            "messages": [{"role": "user", "content": "test"}],
        }).encode()

        req = Request(
            f"{url}/v1/chat/completions",
            data=req_body,
            headers={"Content-Type": "application/json"},
        )
        try:
            urlopen(req)
            pytest.fail("Expected 503")
        except HTTPError as e:
            assert e.code == 503
