import pytest
from meta_router import MetaRouterSelector, ServiceUnavailable
from router_registry import RouterRegistry


@pytest.fixture
def triple_registry():
    cfg = [
        {"name": "r1", "url": "http://a:1", "priority": 1, "weight": 2, "auth": None},
        {"name": "r2", "url": "http://b:1", "priority": 1, "weight": 1, "auth": None},
        {"name": "r3", "url": "http://c:1", "priority": 2, "weight": 1, "auth": None},
    ]
    reg = RouterRegistry(cfg)
    for r in ["r1", "r2", "r3"]:
        reg.mark_healthy(r, ["gpt-4"])
        reg.mark_healthy(r, ["gpt-4"])
    return reg


def test_select_router_returns_healthy(triple_registry):
    sel = MetaRouterSelector(triple_registry)
    r = sel.select_router()
    assert r.health_status == "healthy"


def test_round_robin_alternates(triple_registry):
    sel = MetaRouterSelector(triple_registry)
    first = sel.select_router().name
    second = sel.select_router().name
    third = sel.select_router().name
    fourth = sel.select_router().name
    assert first != second or len([r for r in [first, second, third, fourth] if r == first]) < 4
    assert len({first, second, third}) <= 3


def test_all_down_raises():
    reg = RouterRegistry([
        {"name": "r1", "url": "http://a:1", "priority": 1, "weight": 1, "auth": None},
    ])
    sel = MetaRouterSelector(reg)
    with pytest.raises(ServiceUnavailable):
        sel.select_router()


def test_fallback_after_failure(triple_registry):
    sel = MetaRouterSelector(triple_registry)
    r1 = triple_registry.get_router("r1")
    r1.health_status = "cooldown"
    r = sel.select_router()
    assert r.name != "r1"


def test_on_failure_marks_unhealthy(triple_registry):
    sel = MetaRouterSelector(triple_registry)
    for _ in range(3):  # histerese: cooldown só após ROUTER_UNHEALTHY_STRIKES
        sel.on_failure("r1", "timeout")
    assert triple_registry.get_router("r1").health_status == "cooldown"


def test_single_router():
    reg = RouterRegistry([
        {"name": "r1", "url": "http://a:1", "priority": 1, "weight": 1, "auth": None},
    ])
    reg.mark_healthy("r1", ["gpt-4"])
    reg.mark_healthy("r1", ["gpt-4"])
    sel = MetaRouterSelector(reg)
    r1 = sel.select_router()
    r2 = sel.select_router()
    assert r1.name == r2.name


def test_weighted_selection_prefers_higher_weight(triple_registry):
    """r1 has weight 2, r2/r3 weight 1 — over many picks r1 should lead."""
    sel = MetaRouterSelector(triple_registry)
    picks = [sel.select_router().name for _ in range(40)]
    counts = {name: picks.count(name) for name in set(picks)}
    assert counts["r1"] > counts["r2"]
    assert counts["r1"] > counts["r3"]
    # Ratio should be close to 2:1:1 (weighted, not equal)
    assert abs(counts["r1"] / counts["r2"] - 2.0) < 0.6


def test_on_success_resets_streak(triple_registry):
    sel = MetaRouterSelector(triple_registry)
    sel.on_failure("r1", "timeout")
    assert sel.get_stats()["r1"]["consecutive_failures"] == 1
    sel.on_success("r1")
    assert sel.get_stats()["r1"]["consecutive_failures"] == 0
    assert sel.get_stats()["r1"]["successes"] == 1


def test_on_failure_tracks_stats(triple_registry):
    sel = MetaRouterSelector(triple_registry)
    sel.on_failure("r2", "timeout")
    sel.on_failure("r2", "timeout")
    stats = sel.get_stats()["r2"]
    assert stats["failures"] == 2
    assert stats["consecutive_failures"] == 2


def test_failure_fallback_respects_model_catalog():
    reg = RouterRegistry([
        {"name": "r1", "url": "http://a:1", "weight": 1, "auth": None},
        {"name": "r2", "url": "http://b:1", "weight": 1, "auth": None},
        {"name": "r3", "url": "http://c:1", "weight": 1, "auth": None},
    ])
    reg.mark_healthy("r1", ["groq/target-model"])
    reg.mark_healthy("r1", ["groq/target-model"])
    reg.mark_healthy("r2", ["groq/target-model"])
    reg.mark_healthy("r2", ["groq/target-model"])
    reg.mark_healthy("r3", ["other/model"])
    reg.mark_healthy("r3", ["other/model"])
    sel = MetaRouterSelector(reg)

    fallback = sel.on_failure(
        "r1",
        "connection_error",
        model="target-model",
    )

    assert fallback.name == "r2"
    assert fallback.name != "r3"
    assert sel.model_for_router("target-model", fallback.name) == "groq/target-model"


def test_failure_fallback_raises_when_no_healthy_router_serves_model():
    reg = RouterRegistry([
        {"name": "r1", "url": "http://a:1", "weight": 1, "auth": None},
        {"name": "r2", "url": "http://b:1", "weight": 1, "auth": None},
    ])
    reg.mark_healthy("r1", ["target-model"])
    reg.mark_healthy("r1", ["target-model"])
    reg.mark_healthy("r2", ["other/model"])
    reg.mark_healthy("r2", ["other/model"])
    sel = MetaRouterSelector(reg)

    with pytest.raises(ServiceUnavailable):
        sel.on_failure("r1", "connection_error", model="target-model")


def test_flapping_router_excluded_until_streak_reset(triple_registry):
    """Router with failures >= threshold is skipped while others are healthy."""
    sel = MetaRouterSelector(triple_registry, failure_threshold=3)
    # r1 gets 3 consecutive failures (but stays "healthy" in registry — simulate
    # a router that recovered per probes but keeps failing at request time)
    for _ in range(3):
        sel.on_failure("r1", "timeout")
        sel.on_success("r2")
        sel.on_success("r3")
    triple_registry.get_router("r1").health_status = "healthy"
    picks = [sel.select_router().name for _ in range(20)]
    assert "r1" not in picks
    # After a success, r1 is back in rotation
    sel.on_success("r1")
    picks_after = {sel.select_router().name for _ in range(20)}
    assert "r1" in picks_after


def test_get_stats_empty_initially(triple_registry):
    sel = MetaRouterSelector(triple_registry)
    assert sel.get_stats() == {}



def test_selection_is_thread_safe_and_does_not_corrupt_state(triple_registry):
    import threading

    sel = MetaRouterSelector(triple_registry)
    errors = []
    picks = []
    lock = threading.Lock()

    def worker():
        try:
            local = [sel.select_router("gpt-4").name for _ in range(100)]
            with lock:
                picks.extend(local)
        except Exception as exc:
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert len(picks) == 1000
    assert set(picks) <= {"r1", "r2", "r3"}


def test_model_selection_never_uses_router_without_model(triple_registry):
    triple_registry.get_router("r3").models = ["other/model"]
    sel = MetaRouterSelector(triple_registry)
    picks = [sel.select_router("gpt-4").name for _ in range(30)]
    assert "r3" not in picks


def test_selection_can_exclude_current_router(triple_registry):
    sel = MetaRouterSelector(triple_registry)
    for _ in range(10):
        picked = sel.select_router("gpt-4", exclude={"r1"})
        assert picked.name != "r1"

def test_canonical_model_matches_prefixed_groq_router():
    reg = RouterRegistry([
        {"name": "OmniRoute", "url": "http://a:1", "weight": 1, "auth": None},
        {"name": "Kiro", "url": "http://b:1", "weight": 1, "auth": None},
    ])
    reg.mark_healthy("OmniRoute", ["groq/llama-3.3-70b-versatile"])
    reg.mark_healthy("OmniRoute", ["groq/llama-3.3-70b-versatile"])
    reg.mark_healthy("Kiro", ["other/model"])
    reg.mark_healthy("Kiro", ["other/model"])
    sel = MetaRouterSelector(reg)
    assert sel.select_router("llama-3.3-70b-versatile").name == "OmniRoute"
    assert sel.model_for_router("llama-3.3-70b-versatile", "OmniRoute") == "groq/llama-3.3-70b-versatile"


def test_canonical_nested_nvidia_model_matches_prefixed_router():
    reg = RouterRegistry([
        {"name": "OmniRoute", "url": "http://a:1", "weight": 1, "auth": None},
    ])
    model = "nvidia/deepseek-ai/deepseek-v4-pro"
    reg.mark_healthy("OmniRoute", [model])
    reg.mark_healthy("OmniRoute", [model])
    sel = MetaRouterSelector(reg)
    canonical = "deepseek-ai/deepseek-v4-pro"
    assert sel.select_router(canonical).name == "OmniRoute"
    assert sel.model_for_router(canonical, "OmniRoute") == model


def test_kiro_bare_claude_model_stays_bare():
    reg = RouterRegistry([
        {"name": "Kiro", "url": "http://b:1", "weight": 1, "auth": None},
    ])
    reg.mark_healthy("Kiro", ["claude-sonnet-4.5"])
    reg.mark_healthy("Kiro", ["claude-sonnet-4.5"])
    sel = MetaRouterSelector(reg)
    assert sel.select_router("claude-sonnet-4.5").name == "Kiro"
    assert sel.model_for_router("claude-sonnet-4.5", "Kiro") == "claude-sonnet-4.5"


def test_router_fallback_retranslates_from_client_canonical_id():
    reg = RouterRegistry([
        {"name": "OmniRoute", "url": "http://a:1", "weight": 1, "auth": None},
        {"name": "Kiro", "url": "http://b:1", "weight": 1, "auth": None},
    ])
    reg.mark_healthy("OmniRoute", ["groq/llama-3.3-70b-versatile"])
    reg.mark_healthy("OmniRoute", ["groq/llama-3.3-70b-versatile"])
    reg.mark_healthy("Kiro", ["llama-3.3-70b-versatile"])
    reg.mark_healthy("Kiro", ["llama-3.3-70b-versatile"])
    sel = MetaRouterSelector(reg)
    first = sel.select_router("llama-3.3-70b-versatile")
    other = sel.select_router("llama-3.3-70b-versatile", exclude={first.name})
    assert sel.model_for_router("llama-3.3-70b-versatile", first.name) == "groq/llama-3.3-70b-versatile"
    assert sel.model_for_router("llama-3.3-70b-versatile", other.name) == "llama-3.3-70b-versatile"


def test_kr_auto_passes_through():
    reg = RouterRegistry([
        {"name": "Kiro", "url": "http://b:1", "weight": 1, "auth": None},
    ])
    reg.mark_healthy("Kiro", ["kr/auto"])
    reg.mark_healthy("Kiro", ["kr/auto"])
    sel = MetaRouterSelector(reg)
    assert sel.model_for_router("kr/auto", "Kiro") == "kr/auto"
