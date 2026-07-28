"""Alerter — desktop notifications for provider health transitions.

Stdlib only. Called by daemon every 10s with registry._data.
"""

import logging
import subprocess

log = logging.getLogger(__name__)


class Alerter:
    """Detects status transitions and fires notify-send notifications."""

    def __init__(self) -> None:
        self._previous_state: dict[str, str] = {}
        self._alert_log: list[dict] = []

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
                self._alert_log.append(t)

            self._previous_state[name] = current

        # Handle providers that disappeared (not in current data)
        for name in list(self._previous_state):
            if name not in providers:
                self._previous_state.pop(name, None)

        return transitions

    def alert(self, transition: dict) -> None:
        """Send desktop notification for a transition via notify-send."""
        provider = transition.get("provider", "?")
        from_status = transition.get("from", "")
        to_status = transition.get("to", "")
        reason = transition.get("reason") or ""

        if to_status == "cooldown" and from_status == "healthy":
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

    def get_log(self) -> list[dict]:
        """Return all alert transitions emitted so far."""
        return list(self._alert_log)
