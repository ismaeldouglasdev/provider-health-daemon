"""Tests for alerter — significant transitions + throttle/dedupe."""

import time

import pytest

from alerter import Alerter, SIGNIFICANT_TRANSITIONS


@pytest.fixture
def alerter(monkeypatch):
    monkeypatch.setattr("alerter.subprocess.run", lambda *a, **k: None)
    a = Alerter(throttle_seconds=60)
    # Populate baseline on first call
    a.check_transitions({"providers": {"groq": {"status": "healthy"}}})
    return a


def test_first_call_populates_baseline():
    a = Alerter()
    transitions = a.check_transitions({"providers": {"groq": {"status": "healthy"}}})
    assert transitions == []
    assert a._previous_state == {"groq": "healthy"}


def test_healthy_to_cooldown_is_significant(alerter):
    t = alerter.check_transitions(
        {"providers": {"groq": {"status": "cooldown", "reason": "429"}}}
    )
    assert len(t) == 1
    assert t[0] == {"provider": "groq", "from": "healthy", "to": "cooldown", "reason": "429"}
    assert alerter.alert(t[0]) is True
    assert len(alerter.get_log()) == 1


def test_healthy_to_probing_is_trivial_not_notified(alerter):
    """probing after healthy is a normal degradation — no desktop spam."""
    t = alerter.check_transitions({"providers": {"groq": {"status": "probing"}}})
    assert len(t) == 1
    assert (t[0]["from"], t[0]["to"]) not in SIGNIFICANT_TRANSITIONS
    assert alerter.alert(t[0]) is False
    assert alerter.get_log() == []


def test_prob_ing_to_cooldown_trivial(alerter):
    """probe failed → cooldown already alerted on the healthy→cooldown hop."""
    alerter._previous_state["groq"] = "probing"
    t = alerter.check_transitions({"providers": {"groq": {"status": "cooldown"}}})
    assert (t[0]["from"], t[0]["to"]) not in SIGNIFICANT_TRANSITIONS
    assert alerter.alert(t[0]) is False


def test_cooldown_to_healthy_recovery_notified(alerter):
    alerter._previous_state["groq"] = "cooldown"
    t = alerter.check_transitions({"providers": {"groq": {"status": "healthy"}}})
    assert (t[0]["from"], t[0]["to"]) in SIGNIFICANT_TRANSITIONS
    assert alerter.alert(t[0]) is True


def test_throttle_dedupe_flapping_loop(alerter):
    """Probe loop (cooldown→probing→cooldown→probing) must not re-notify."""
    # healthy → cooldown: fires DOWN
    t1 = alerter.check_transitions({"providers": {"groq": {"status": "cooldown"}}})
    assert alerter.alert(t1[0]) is True
    # cooldown → probing: fires PROBING
    t2 = alerter.check_transitions({"providers": {"groq": {"status": "probing"}}})
    assert alerter.alert(t2[0]) is True
    # probing → cooldown: trivial, not notified
    t3 = alerter.check_transitions({"providers": {"groq": {"status": "cooldown"}}})
    assert alerter.alert(t3[0]) is False
    # cooldown → probing again within window: same target status → throttled
    t4 = alerter.check_transitions({"providers": {"groq": {"status": "probing"}}})
    assert alerter.alert(t4[0]) is False
    assert len(alerter.get_log()) == 2  # only DOWN + first PROBING


def test_new_incident_after_recovery_fires(alerter):
    """DOWN → RECOVERED → DOWN is a new incident: not throttled."""
    t1 = alerter.check_transitions({"providers": {"groq": {"status": "cooldown"}}})
    assert alerter.alert(t1[0]) is True
    t2 = alerter.check_transitions({"providers": {"groq": {"status": "healthy"}}})
    assert alerter.alert(t2[0]) is True  # recovery — different target status
    t3 = alerter.check_transitions({"providers": {"groq": {"status": "cooldown"}}})
    assert alerter.alert(t3[0]) is True  # new incident — fires again
    assert len(alerter.get_log()) == 3


def test_throttle_expires(alerter):
    t1 = alerter.check_transitions({"providers": {"groq": {"status": "cooldown"}}})
    assert alerter.alert(t1[0]) is True
    # Move last alert far into the past
    alerter._last_alert["groq"] = ("cooldown", time.time() - 3600)
    alerter._previous_state["groq"] = "healthy"
    t2 = alerter.check_transitions({"providers": {"groq": {"status": "cooldown"}}})
    assert alerter.alert(t2[0]) is True  # window expired — fires again


def test_new_provider_appears(alerter):
    t = alerter.check_transitions(
        {"providers": {"groq": {"status": "healthy"}, "nvidia": {"status": "healthy"}}}
    )
    # New provider with previous="unknown" → healthy is trivial
    assert len(t) == 1
    assert alerter.alert(t[0]) is False
