"""Cooldown calculation with exponential backoff."""

from datetime import datetime, timedelta, timezone
from typing import Optional


class CooldownCalculator:
    """Calculate cooldown duration based on error type and failure history."""

    def __init__(self, max_hours: int = 24):
        self.max_cooldown = timedelta(hours=max_hours)

    def calculate(
        self,
        error_info: dict,
        current_failures: int = 0,
        current_cooldown_until: Optional[datetime] = None,
    ) -> dict:
        """Calculate final cooldown.

        error_info from error_parser.parse_error():
            cooldown: {hours, minutes, type}
            permanent: bool
            model_specific: bool

        Returns:
            {
                "until": datetime | None,
                "duration_hours": float,
                "type": str,
                "permanent": bool,
                "model_specific": bool,
                "backoff_applied": bool,
                "failures": int,
            }
        """
        # Support both flat and nested error_info formats
        cd = error_info.get("cooldown", error_info)
        base_hours = float(cd.get("hours", 0))
        base_minutes = float(cd.get("minutes", 0))
        error_type = str(cd.get("type", "unknown"))
        permanent = bool(error_info.get("permanent", False))
        model_specific = bool(error_info.get("model_specific", False))
        recheck = bool(error_info.get("recheck", False))

        failures = current_failures + 1

        if permanent:
            return {
                "until": None,  # never
                "duration_hours": float("inf"),
                "type": error_type,
                "permanent": True,
                "model_specific": model_specific,
                "backoff_applied": False,
                "failures": failures,
                "recheck": False,
            }

        # Base duration
        base_duration = timedelta(hours=base_hours, minutes=base_minutes)

        # Exponential backoff: double each failure, up to max
        if failures > 1:
            multiplier = 2 ** (failures - 1)
            backoff_duration = base_duration * multiplier
            if backoff_duration > self.max_cooldown:
                backoff_duration = self.max_cooldown
            backoff_applied = backoff_duration > base_duration
        else:
            backoff_duration = base_duration
            backoff_applied = False

        # Ensure minimum 30s cooldown for anything non-zero
        if backoff_duration.total_seconds() > 0 and backoff_duration.total_seconds() < 30:
            backoff_duration = timedelta(seconds=30)

        until = datetime.now(timezone.utc) + backoff_duration

        # If we already have a cooldown and this is a recheck scenario:
        # use the longer of the two
        if current_cooldown_until and current_cooldown_until > until:
            until = current_cooldown_until

        return {
            "until": until.isoformat(),
            "duration_hours": backoff_duration.total_seconds() / 3600,
            "type": error_type,
            "permanent": False,
            "model_specific": model_specific,
            "backoff_applied": backoff_applied,
            "failures": failures,
            "recheck": recheck,
        }

    def is_expired(self, until_iso: Optional[str]) -> bool:
        """Check if cooldown has expired."""
        if until_iso is None:
            return False  # permanent = never expires
        try:
            until = datetime.fromisoformat(until_iso)
        except ValueError:
            return True  # malformed, assume expired
        return datetime.now(timezone.utc) >= until

    def time_remaining(self, until_iso: Optional[str]) -> timedelta:
        """Return time remaining until cooldown expires."""
        if until_iso is None:
            return timedelta.max
        try:
            until = datetime.fromisoformat(until_iso)
        except ValueError:
            return timedelta(0)
        remaining = until - datetime.now(timezone.utc)
        return remaining if remaining.total_seconds() > 0 else timedelta(0)