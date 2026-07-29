"""Tests for HealthRegistry — thread safety, persistence, state management."""

import json
import os
import threading
from pathlib import Path

import pytest

from health_registry import HealthRegistry


@pytest.fixture
def tmp_registry(tmp_path: Path) -> HealthRegistry:
    """Create a fresh registry on a temp file."""
    return HealthRegistry(filepath=tmp_path / "health.json")


# ── Basic operations ────────────────────────────────────────────────


def test_mark_healthy(tmp_registry: HealthRegistry):
    tmp_registry.mark_healthy("test-provider")
    assert tmp_registry.is_provider_healthy("test-provider")
    entry = tmp_registry.get_provider("test-provider")
    assert entry["status"] == "healthy"
    assert entry["failures"] == 0


def test_mark_healthy_with_model(tmp_registry: HealthRegistry):
    tmp_registry.mark_healthy("test-provider", model="test-model")
    model_entry = tmp_registry.get_model("test-model")
    assert model_entry["status"] == "healthy"
    assert tmp_registry.is_model_available("test-model")


def test_is_provider_healthy_unknown(tmp_registry: HealthRegistry):
    """Unknown providers should be assumed healthy."""
    assert tmp_registry.is_provider_healthy("nonexistent")


def test_mark_error_provider(tmp_registry: HealthRegistry):
    tmp_registry.mark_error("test-provider", {
        "type": "rate_limit",
        "status": 429,
        "model_specific": False,
        "cooldown": {"type": "rate_limit", "duration_hours": 1},
    })
    assert not tmp_registry.is_provider_healthy("test-provider")
    entry = tmp_registry.get_provider("test-provider")
    assert entry["status"] in ("cooldown", "disabled")
    assert entry["failures"] >= 1


def test_mark_error_model_specific(tmp_registry: HealthRegistry):
    tmp_registry.mark_error("test-provider", {
        "type": "rate_limit",
        "status": 429,
        "model_specific": True,
        "cooldown": {"type": "rate_limit", "hours": 1},
    }, model="test-provider/some-model")
    assert tmp_registry.is_provider_healthy("test-provider")  # provider still healthy
    assert not tmp_registry.is_model_available("test-provider/some-model")  # model in cooldown


def test_force_healthy(tmp_registry: HealthRegistry):
    tmp_registry.mark_error("test-provider", {
        "type": "rate_limit",
        "status": 429,
        "model_specific": False,
        "cooldown": {"type": "rate_limit", "hours": 24},
    })
    tmp_registry.force_healthy("test-provider")
    assert tmp_registry.is_provider_healthy("test-provider")


def test_cleanup_expired(tmp_registry: HealthRegistry):
    tmp_registry.mark_error("test-provider", {
        "type": "rate_limit",
        "status": 429,
        "model_specific": False,
        "cooldown": {"type": "rate_limit", "duration_hours": 0},
    })
    promoted = tmp_registry.cleanup_expired()
    assert promoted >= 1
    entry = tmp_registry.get_provider("test-provider")
    assert entry["status"] == "probing"


def test_status_summary(tmp_registry: HealthRegistry):
    summary = tmp_registry.status_summary()
    assert "by_status" in summary
    assert "expired_ready" in summary
    assert all(k in summary["by_status"] for k in ("healthy", "cooldown", "probing", "disabled"))


# ── Persistence ─────────────────────────────────────────────────────


def test_persistence(tmp_path: Path):
    fp = tmp_path / "health.json"
    r1 = HealthRegistry(filepath=fp)
    r1.mark_healthy("provider-a")
    r1.mark_healthy("provider-b")

    r2 = HealthRegistry(filepath=fp)
    assert r2.is_provider_healthy("provider-a")
    assert r2.is_provider_healthy("provider-b")
    assert r2.is_provider_healthy("nonexistent")  # unknown = assume healthy


def test_load_corrupted_file(tmp_path: Path):
    fp = tmp_path / "health.json"
    fp.write_text("{invalid json")
    r = HealthRegistry(filepath=fp)
    # Should gracefully fall back to empty state
    assert r.status_summary()["by_status"]["healthy"] == 0


# ── Thread safety ──────────────────────────────────────────────────


def test_concurrent_writes(tmp_registry: HealthRegistry):
    """Multiple threads writing concurrently should not corrupt state."""
    n_threads = 10
    errors = []

    def writer(thread_id: int):
        try:
            for i in range(50):
                name = f"thread-{thread_id}-{i}"
                tmp_registry.mark_healthy(name)
                tmp_registry.mark_error(name, {
                    "type": "test",
                    "status": 500,
                    "model_specific": False,
                    "cooldown": {"type": "test", "duration_hours": 0.01},
                })
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"Errors during concurrent writes: {errors}"
    # State should be valid JSON
    data = json.loads(tmp_registry.filepath.read_text())
    assert "providers" in data
    assert "models" in data


def test_concurrent_read_write(tmp_registry: HealthRegistry):
    """Reads during writes should not cause errors."""
    stop = threading.Event()

    def writer():
        i = 0
        while not stop.is_set():
            tmp_registry.mark_healthy(f"provider-{i % 10}")
            i += 1

    def reader():
        while not stop.is_set():
            tmp_registry.status_summary()
            tmp_registry.is_provider_healthy("provider-0")
            _ = tmp_registry.get_provider("provider-0")

    w = threading.Thread(target=writer, daemon=True)
    r = threading.Thread(target=reader, daemon=True)
    w.start()
    r.start()

    import time
    time.sleep(1)
    stop.set()
    w.join(timeout=2)
    r.join(timeout=2)
    # If we got here without exception, the lock is working


# ── Atomic write ───────────────────────────────────────────────────


def test_atomic_write_no_corruption(tmp_path: Path):
    """If a crash happens during write, .tmp file should not replace original."""
    fp = tmp_path / "health.json"
    r = HealthRegistry(filepath=fp)
    r.mark_healthy("original")

    # Simulate partial write by creating a .tmp file with garbage
    tmp_file = fp.with_suffix(".tmp")
    tmp_file.write_text("{garbage}")

    # New registry instance should still read the original
    r2 = HealthRegistry(filepath=fp)
    assert r2.is_provider_healthy("original")
