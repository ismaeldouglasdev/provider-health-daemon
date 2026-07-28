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
        cd = error_info.get("cooldown") or error_info
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

        base_duration = timedelta(hours=base_hours, minutes=base_minutes)

        if failures > 1:
            capped_failures = min(failures - 1, 30)
            multiplier = 2 ** capped_failures
            backoff_duration = timedelta(seconds=min(
                base_duration.total_seconds() * multiplier,
                self.max_cooldown.total_seconds(),
            ))
            backoff_applied = backoff_duration > base_duration
        else:
            backoff_duration = base_duration
            backoff_applied = False

        if timedelta(seconds=0) < backoff_duration < timedelta(seconds=30):
            backoff_duration = timedelta(seconds=30)

        until = datetime.now(timezone.utc) + backoff_duration

        # If we already have a cooldown and this is a recheck scenario:
        # use the longer of the two
        if current_cooldown_until:
            try:
                existing = datetime.fromisoformat(current_cooldown_until)
                if existing > until:
                    until = existing
            except (ValueError, TypeError):
                pass  # malformed, ignore

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
        except (ValueError, TypeError):
            return True  # malformed, assume expired
        now = datetime.now(timezone.utc)
        # Guard against extreme dates that cause timedelta overflow
        if until.year < 1970 or until.year > 2100:
            return True
        return now >= until

    def time_remaining(self, until_iso: Optional[str]) -> timedelta:
        """Return time remaining until cooldown expires."""
        if until_iso is None:
            return timedelta.max
        try:
            until = datetime.fromisoformat(until_iso)
        except (ValueError, TypeError):
            return timedelta(0)
        now = datetime.now(timezone.utc)
        # Guard against extreme dates
        if until.year < 1970 or until.year > 2100:
            return timedelta(0)
        remaining = until - now
        # Clamp to reasonable bounds
        if remaining.total_seconds() > 24 * 3600 * 365:  # > 1 year
            return timedelta(0)
        return remaining if remaining.total_seconds() > 0 else timedelta(0)