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
    tmp_registry.mark_healthy("sample-provider")
    assert tmp_registry.is_provider_healthy("sample-provider")
    entry = tmp_registry.get_provider("sample-provider")
    assert entry["status"] == "healthy"
    assert entry["failures"] == 0


def test_mark_healthy_with_model(tmp_registry: HealthRegistry):
    tmp_registry.mark_healthy("sample-provider", model="test-model")
    model_entry = tmp_registry.get_model("test-model")
    assert model_entry["status"] == "healthy"
    assert tmp_registry.is_model_available("test-model")


def test_is_provider_healthy_unknown(tmp_registry: HealthRegistry):
    """Unknown providers should be assumed healthy."""
    assert tmp_registry.is_provider_healthy("nonexistent")


def test_mark_error_provider(tmp_registry: HealthRegistry):
    tmp_registry.mark_error("sample-provider", {
        "type": "rate_limit",
        "status": 429,
        "model_specific": False,
        "cooldown": {"type": "rate_limit", "hours": 1},
    })
    assert not tmp_registry.is_provider_healthy("sample-provider")
    entry = tmp_registry.get_provider("sample-provider")
    assert entry["status"] in ("cooldown", "disabled")
    assert entry["failures"] >= 1


def test_mark_error_model_specific(tmp_registry: HealthRegistry):
    tmp_registry.mark_error("sample-provider", {
        "type": "rate_limit",
        "status": 429,
        "model_specific": True,
        "cooldown": {"type": "rate_limit", "hours": 1},
    }, model="sample-provider/some-model")
    assert tmp_registry.is_provider_healthy("sample-provider")  # provider still healthy
    assert not tmp_registry.is_model_available("sample-provider/some-model")  # model in cooldown


def test_force_healthy(tmp_registry: HealthRegistry):
    tmp_registry.mark_error("sample-provider", {
        "type": "rate_limit",
        "status": 429,
        "model_specific": False,
        "cooldown": {"type": "rate_limit", "hours": 24},
    })
    tmp_registry.force_healthy("sample-provider")
    assert tmp_registry.is_provider_healthy("sample-provider")


def test_cleanup_expired(tmp_registry: HealthRegistry):
    tmp_registry.mark_error("sample-provider", {
        "type": "rate_limit",
        "status": 429,
        "model_specific": False,
        "cooldown": {"type": "rate_limit", "duration_hours": 0},
    })
    promoted = tmp_registry.cleanup_expired()
    assert promoted >= 1
    entry = tmp_registry.get_provider("sample-provider")
    assert entry["status"] == "probing"


def test_cleanup_promotion_sets_fresh_probing_window(tmp_registry: HealthRegistry):
    """Cooldown → probing must refresh `until` so the recovery prober has
    time to test before the next cleanup run would treat it as orphaned."""
    from datetime import datetime, timezone

    tmp_registry.mark_error("sample-provider", {
        "type": "rate_limit",
        "status": 429,
        "model_specific": False,
        "cooldown": {"type": "rate_limit", "duration_hours": 0},
    })
    tmp_registry.cleanup_expired()
    entry = tmp_registry.get_provider("sample-provider")
    assert entry["status"] == "probing"
    until = datetime.fromisoformat(entry["until"])
    assert until > datetime.now(timezone.utc), "probing `until` must be in the future"

    removed = tmp_registry.cleanup_expired()
    assert tmp_registry.get_provider("sample-provider")["status"] == "probing"


def test_cleanup_removes_orphaned_probing(tmp_registry: HealthRegistry):
    """Probing entries whose window expired long ago (removed from catalog,
    never re-tested) must be dropped instead of lingering forever."""
    from datetime import datetime, timedelta, timezone

    stale = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    tmp_registry._data[tmp_registry.PROVIDERS]["ghost"] = {
        "status": "probing",
        "until": stale,
        "reason": "unknown_401 (probing)",
        "failures": 1,
        "models": [],
    }

    removed = tmp_registry.cleanup_expired()
    assert removed >= 1
    assert tmp_registry.get_provider("ghost") == {}


def test_cleanup_keeps_active_probing(tmp_registry: HealthRegistry):
    """Probing entries with a still-valid window must NOT be removed."""
    from datetime import datetime, timedelta, timezone

    fresh = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    tmp_registry._data[tmp_registry.PROVIDERS]["active"] = {
        "status": "probing",
        "until": fresh,
        "reason": "generic_429 (probing)",
        "failures": 2,
        "models": [],
    }

    removed = tmp_registry.cleanup_expired()
    assert tmp_registry.get_provider("active")["status"] == "probing"


def test_cleanup_removes_orphaned_probing_models(tmp_registry: HealthRegistry):
    """Model-level probing entries with expired windows are cleaned too."""
    from datetime import datetime, timedelta, timezone

    stale = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    tmp_registry._data[tmp_registry.MODELS]["ghost-model"] = {
        "status": "probing",
        "until": stale,
        "reason": "generic_429 (probing)",
        "failures": 1,
        "provider": "ghost",
        "model": "ghost-model",
    }

    removed = tmp_registry.cleanup_expired()
    assert removed >= 1
    assert tmp_registry.get_model("ghost-model") == {}


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


def test_load_migrates_duplicate_alias_keys(tmp_path: Path):
    fp = tmp_path / "health.json"
    fp.write_text(json.dumps({
        "providers": {
            "bzl": {"status": "cooldown", "until": "2099-01-01T00:00:00+00:00",
                    "reason": "no_credit", "failures": 1, "models": []},
            "bazaarlink": {"status": "healthy", "failures": 0},
            "samba": {"status": "healthy", "failures": 0},
            "sambanova": {},
        },
        "models": {},
    }))
    r = HealthRegistry(filepath=fp)
    providers = r.snapshot()["providers"]
    assert "bazaarlink" not in providers
    assert "sambanova" not in providers
    # Most restrictive status wins: bzl cooldown preserved over healthy
    assert providers["bzl"]["status"] == "cooldown"
    assert providers["bzl"]["reason"] == "no_credit"
    assert providers["samba"]["status"] == "healthy"
    # Aliased lookups resolve to the canonical key
    assert r.get_provider("bazaarlink")["status"] == "cooldown"
    assert r.get_provider("sambanova")["status"] == "healthy"


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


# ── Disabled-provider re-integration (2026-08-31) ───────────────────


def test_reprobe_disabled_ignores_non_disabled(tmp_registry: HealthRegistry):
    tmp_registry.mark_healthy("sample-provider")
    entry = tmp_registry.get_provider("sample-provider")
    assert not tmp_registry.reprobe_disabled_due(entry)


def test_reprobe_disabled_due_when_no_timestamp(tmp_registry: HealthRegistry):
    # Simulate a disabled entry with no last_probe_at/updated_at -> due once
    tmp_registry._data["providers"]["sample-provider"] = {
        "status": "disabled", "failures": 10, "reason": "no_credit",
    }
    assert tmp_registry.reprobe_disabled_due(
        tmp_registry.get_provider("sample-provider")
    )


def test_reprobe_disabled_not_due_after_recent_probe(tmp_registry: HealthRegistry):
    from datetime import datetime, timedelta, timezone
    tmp_registry._data["providers"]["sample-provider"] = {
        "status": "disabled", "failures": 10,
        "last_probe_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
    }
    assert not tmp_registry.reprobe_disabled_due(
        tmp_registry.get_provider("sample-provider")
    )


def test_reprobe_disabled_due_after_backoff(tmp_registry: HealthRegistry):
    from datetime import datetime, timedelta, timezone
    tmp_registry._data["providers"]["sample-provider"] = {
        "status": "disabled", "failures": 10,
        "last_probe_at": (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat(),
    }
    assert tmp_registry.reprobe_disabled_due(
tmp_registry.get_provider("sample-provider")
)


def test_record_disabled_probe_stamps_and_delays_reprobe(tmp_registry: HealthRegistry):
    from datetime import datetime, timedelta, timezone

    from health_registry import HealthRegistry
    tmp_registry._data["providers"]["sample-provider"] = {
        "status": "disabled", "failures": 10,
        "last_probe_at": (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat(),
    }
    assert tmp_registry.reprobe_disabled_due(
        tmp_registry.get_provider("sample-provider")
    )
    tmp_registry.record_disabled_probe("sample-provider")
    entry = tmp_registry.get_provider("sample-provider")
    assert "last_probe_at" in entry
    assert not tmp_registry.reprobe_disabled_due(entry)
