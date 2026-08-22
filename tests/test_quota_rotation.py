"""Testes determinísticos para a rotação quota-aware (todo 6)."""

from unittest.mock import MagicMock

import pytest

from smart_router import SmartRouter


class FakeUsageCache:
    def __init__(self, data=None, raise_error=False):
        self.data = data or {}
        self.raise_error = raise_error
        self.calls = 0

    def get(self):
        self.calls += 1
        if self.raise_error:
            raise RuntimeError("boom")
        return self.data


def make_router(usage_cache=None):
    return SmartRouter(metrics_store=MagicMock(), usage_cache=usage_cache)


def _ranked():
    # provA score melhor, mas dentro da banda de spread de provB
    return [
        ("model-a", "provA", {"total": 100}),
        ("model-b", "provB", {"total": 95}),
    ]


def test_zero_usage_provider_boosted_over_lru():
    router = make_router(FakeUsageCache({"provA": {"tokens": 999, "requests": 5}}))
    # provA foi escolhido há mais tempo (LRU puro escolheria provA)...
    router._last_selected["provA"] = 10.0
    router._last_selected["provB"] = 20.0

    chosen = router._spread_select(_ranked())

    # ...mas provB não usou cota hoje → boost de ordenação vence o LRU.
    assert chosen[1] == "provB"


def test_no_usage_cache_keeps_pure_lru():
    router = make_router(None)
    router._last_selected["provA"] = 10.0
    router._last_selected["provB"] = 20.0

    chosen = router._spread_select(_ranked())

    assert chosen[1] == "provA"  # comportamento antigo preservado


def test_usage_cache_error_degrades_to_lru():
    router = make_router(FakeUsageCache(raise_error=True))
    router._last_selected["provA"] = 10.0
    router._last_selected["provB"] = 20.0

    chosen = router._spread_select(_ranked())  # não pode lançar

    assert chosen[1] == "provA"


def test_both_unused_orders_by_lru():
    router = make_router(FakeUsageCache({}))
    router._last_selected["provA"] = 30.0
    router._last_selected["provB"] = 20.0

    chosen = router._spread_select(_ranked())

    assert chosen[1] == "provB"  # ambos sem uso hoje → LRU decide
