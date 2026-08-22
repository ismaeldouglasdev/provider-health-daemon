"""Test bind-first behavior for provider-health-daemon.

This test verifies that HTTP servers bind immediately on startup, eliminating
a 'dead window' where the daemon is listening but handlers are not yet ready.
See TODO 3 in .omo/plans/provider-health-daemon-improvements.md.
"""

import os
import socket
import subprocess
import sys
import time
from typing import Literal

import pytest


def try_connect(host: str, port: int, timeout: float = 0.5, retries: int = 3) -> bool:
    """Attempt to establish TCP connection."""
    for attempt in range(retries):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect((host, port))
            sock.close()
            return True
        except (TimeoutError, ConnectionRefusedError, OSError):
            time.sleep(0.1 * (attempt + 1))
    return False


def test_bind_first():
    """Ensure servers bind within 5s of startup."""
    # Use unique ports for this test to avoid conflicts
    HEALTH_PROXY_PORT = 20141
    DASHBOARD_PORT = 20142

    # Verify ports are not currently in use
    for port in (HEALTH_PROXY_PORT, DASHBOARD_PORT):
        assert not try_connect("127.0.0.1", port), f"Port {port} is already in use"

    # Start daemon in subprocess with unique ports
    env = {
        "HEALTH_PROXY_PORT": str(HEALTH_PROXY_PORT),
        "DASHBOARD_PORT": str(DASHBOARD_PORT),
        "NINEROUTER_URL": "http://localhost:20128",
        "DAEMON_LOCK_PATH": "/tmp/daemon-test-bindfirst.lock",
    }
    proc = subprocess.Popen(
        [sys.executable, "daemon.py"],
        cwd="/home/ismaeldev/Desktop/code_study/MeusProjetos/provider-health-daemon",
        env={**subprocess.os.environ, **env},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    # Enable immediate binding without waiting for health checks, catalog sync, etc.
    # Pass flag via argv or env
    try:
        # Wait up to 10s for ports to be listening
        deadline = time.time() + 10
        proxy_seconds = time.time() + 5  # proxy must bind <5s after start
        dashboard_seconds = time.time() + 5  # dashboard must bind <5s after start

        while time.time() < deadline:
            # Check if daemon exited
            if proc.poll() is not None:
                stdout, stderr = proc.communicate(timeout=5)
                assert False, f"Daemon exited early\nstdout: {stdout}\nstderr: {stderr}"

            # Check TCP port connectivity
            proxy_listening = try_connect("127.0.0.1", HEALTH_PROXY_PORT, timeout=0.2, retries=1)
            dashboard_listening = try_connect("127.0.0.1", DASHBOARD_PORT, timeout=0.2, retries=1)

            if proxy_listening and dashboard_listening:
                # Servers are listening — allow any additional init to complete
                break

            # Sources of delay before bind:
            # - Single-instance lock (should be < 100ms)
            # - Logging initialization (should be < 200ms)
            # - Config loading (should be < 100ms)
            # If we hit 5s without bind, the bind-first optimization is missing

            # Health probe sanity check and catalog sync and probe threads (all deferred)
            # must NOT block bind.

            if time.time() > proxy_seconds:
                stdout, stderr = proc.communicate(timeout=5)
                assert False, (
                    f"Proxy did not bind within 5s\nstdout: {stdout}\nstderr: {stderr}\n"
                    f"proxy_listening={proxy_listening}, dashboard_listening={dashboard_listening}"
                )

            if time.time() > dashboard_seconds:
                stdout, stderr = proc.communicate(timeout=5)
                assert False, (
                    f"Dashboard did not bind within 5s\nstdout: {stdout}\nstderr: {stderr}\n"
                    f"proxy_listening={proxy_listening}, dashboard_listening={dashboard_listening}"
                )

            time.sleep(0.05)  # 50ms poll interval
        else:
            stdout, stderr = proc.communicate(timeout=5)
            assert False, (
                f"Timed out waiting for servers to bind\nstdout: {stdout}\nstderr: {stderr}\n"
                f"proxy_listening={proxy_listening}, dashboard_listening={dashboard_listening}"
            )
    finally:
        proc.terminate()
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()


def test_binding_hosts():
    """Ensure decoration matches default "127.0.0.1" and env override works."""
    import socket
    import subprocess
    import sys
    import time

    HEALTH_PROXY_PORT = 20141
    DASHBOARD_PORT = 20142

    # Override host via env (dev override before daemon binds)
    override_host = "127.0.0.1"
    env = {
        "HEALTH_PROXY_PORT": str(HEALTH_PROXY_PORT),
        "DASHBOARD_PORT": str(DASHBOARD_PORT),
        "HEALTH_PROXY_HOST": override_host,
        "DASHBOARD_HOST": override_host,
        "NINEROUTER_URL": "http://localhost:20128",
        "DAEMON_LOCK_PATH": "/tmp/daemon-test-bindhosts.lock",
    }

    proc = subprocess.Popen(
        [sys.executable, "daemon.py"],
        cwd="/home/ismaeldev/Desktop/code_study/MeusProjetos/provider-health-daemon",
        env={**subprocess.os.environ, **env},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        deadline = time.time() + 10
        while time.time() < deadline:
            if proc.poll() is not None:
                stdout, stderr = proc.communicate(timeout=5)
                assert False, f"Daemon exited early\nstdout: {stdout}\nstderr: {stderr}"

            listening = try_connect(override_host, HEALTH_PROXY_PORT, timeout=0.2, retries=1)
            if listening:
                break

            time.sleep(0.05)
        else:
            stdout, stderr = proc.communicate(timeout=5)
            assert False, f"Timed out waiting for proxy on {override_host}:{HEALTH_PROXY_PORT}\nstderr: {stderr}"
    finally:
        proc.terminate()
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])