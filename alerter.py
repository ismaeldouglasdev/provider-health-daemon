"""Alerter — desktop notifications for provider health transitions.

Stdlib only. Called by daemon every 10s with registry._data.

Only *significant* transitions fire notifications:
  - healthy → cooldown/disabled  (provider down / permanent failure)
  - cooldown → probing           (retry started)
  - probing/cooldown → healthy    (recovered)
Trivial transitions (healthy → probing, probing → cooldown) are detected
for logging but NOT notified, avoiding notify-send spam on flapping.

Per-provider throttle + dedupe: the same provider/target-state pair is
never notified more than once per ALERT_THROTTLE_SECONDS.
"""

import logging
import subprocess
import time

log = logging.getLogger(__name__)

# Transitions worth a desktop notification (from_status, to_status)
SIGNIFICANT_TRANSITIONS = frozenset({
    ("healthy", "cooldown"),   # provider just went down — critical
    ("healthy", "disabled"),   # permanent failure (no auth/credits)
    ("cooldown", "probing"),   # retry attempt started
    ("probing", "healthy"),    # recovered after retry
    ("cooldown", "healthy"),   # recovered directly
})

# Never re-notify the same provider + target status within this window
ALERT_THROTTLE_SECONDS = 300


class Alerter:
    """Detects status transitions and fires notify-send notifications."""

    def __init__(self, throttle_seconds: int = ALERT_THROTTLE_SECONDS) -> None:
        self._previous_state: dict[str, str] = {}
        self._alert_log: list[dict] = []
        # provider -> (to_status, last_notified_epoch) for throttle/dedupe
        self._last_alert: dict[str, tuple[str, float]] = {}
        self._throttle_seconds = throttle_seconds

    def check_transitions(self, registry_data: dict) -> list[dict]:
        """Compare current provider statuses with previous state.

        Returns list of transition dicts where status changed.
        First call (empty _previous_state) just populates, returns [].
        Skips providers named "unknown".
        """
        providers = registry_data.get("providers", {})

        # First call: populate baseline without alerting
        if not self._previous_state:
            for name, entry in providers.items():
                if name == "unknown":
                    continue
                self._previous_state[name] = entry.get("status", "healthy")
            return []

        transitions: list[dict] = []

        for name, entry in providers.items():
            if name == "unknown":
                continue
            current = entry.get("status", "healthy")
            previous = self._previous_state.get(name)

            if previous is None:
                # New provider appeared since startup
                previous = "unknown"

            if current != previous:
                t = {
                    "provider": name,
                    "from": previous,
                    "to": current,
                    "reason": entry.get("reason"),
                }
                transitions.append(t)

            self._previous_state[name] = current

        # Handle providers that disappeared (not in current data)
        for name in list(self._previous_state):
            if name not in providers:
                self._previous_state.pop(name, None)

        return transitions

    def _is_significant(self, transition: dict) -> bool:
        return (transition.get("from"), transition.get("to")) in SIGNIFICANT_TRANSITIONS

    def _is_throttled(self, transition: dict) -> bool:
        """Dedupe: same provider + target status already notified recently."""
        provider = transition.get("provider")
        to_status = transition.get("to")
        last = self._last_alert.get(provider)
        if not last:
            return False
        last_status, last_time = last
        if last_status != to_status:
            return False
        return (time.time() - last_time) < self._throttle_seconds

    def alert(self, transition: dict) -> bool:
        """Send desktop notification for a significant, non-throttled transition.

        Returns True if a notification was fired, False if filtered out
        (trivial transition or throttled duplicate). Only fired alerts are
        appended to the alert log.
        """
        if not self._is_significant(transition):
            return False
        if self._is_throttled(transition):
            log.debug(
                "Alerter throttled: %s → %s (already notified)",
                transition.get("provider"), transition.get("to"),
            )
            return False

        provider = transition.get("provider", "?")
        from_status = transition.get("from", "")
        to_status = transition.get("to", "")
        reason = transition.get("reason") or ""

        if to_status in ("cooldown", "disabled") and from_status == "healthy":
            args = [
                "notify-send", "-a", "Health Proxy", "-u", "critical",
                f"\u26a0 {provider} DOWN", reason,
            ]
        elif to_status == "healthy" and from_status in ("cooldown", "probing"):
            args = [
                "notify-send", "-a", "Health Proxy", "-u", "normal",
                f"\u2705 {provider} RECOVERED", "",
            ]
        elif to_status == "probing" and from_status == "cooldown":
            args = [
                "notify-send", "-a", "Health Proxy", "-u", "low",
                f"\U0001f504 {provider} PROBING", "",
            ]
        else:
            args = [
                "notify-send", "-a", "Health Proxy", "-u", "normal",
                f"\u2022 {provider}: {from_status} \u2192 {to_status}", "",
            ]

        try:
            subprocess.run(args, check=False, capture_output=True)
        except FileNotFoundError:
            log.warning(
                "notify-send not found — install libnotify-bin for desktop alerts"
            )
        except Exception as e:
            log.warning(f"notify-send failed: {e}")

        self._last_alert[provider] = (to_status, time.time())
        self._alert_log.append(transition)
        return True

    def get_log(self) -> list[dict]:
        """Return alert transitions actually emitted so far."""
        return list(self._alert_log)
