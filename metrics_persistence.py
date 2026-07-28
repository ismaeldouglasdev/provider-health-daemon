"""Metrics persistence — snapshot provider state to JSON for charting.

Snapshots the health registry + metrics store every N seconds, keeps a
rolling window of history points, and auto-rotates old files.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

# ── Defaults ──────────────────────────────────────────────────────────
SNAPSHOT_DIR = Path.home() / ".9router" / "metrics_history"
SNAPSHOT_INTERVAL = 60  # seconds between snapshots
MAX_POINTS = 1440  # keep ~24h at 60s interval
ROTATE_AFTER_DAYS = 7  # delete files older than this


class MetricsPersistence:
    """Periodically snapshot registry + metrics store to JSON lines."""

    def __init__(
        self,
        snapshot_dir: Path = SNAPSHOT_DIR,
        interval: int = SNAPSHOT_INTERVAL,
        max_points: int = MAX_POINTS,
        rotate_days: int = ROTATE_AFTER_DAYS,
    ):
        self.snapshot_dir = snapshot_dir
        self.interval = interval
        self.max_points = max_points
        self.rotate_days = rotate_days
        self.history: list[dict[str, Any]] = []
        snapshot_dir.mkdir(parents=True, exist_ok=True)

    def snapshot(
        self,
        providers: dict[str, dict[str, Any]],
        global_stats: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Take a snapshot of current provider states."""
        # Count statuses
        healthy = probing = cooldown = disabled = 0
        for name, p in providers.items():
            if name == "unknown":
                continue
            s = p.get("status", "")
            if s == "healthy":
                healthy += 1
            elif s == "probing":
                probing += 1
            elif s == "cooldown":
                cooldown += 1
            elif s == "disabled":
                disabled += 1

        point = {
            "ts": time.time(),
            "datetime": datetime.now(timezone.utc).isoformat(),
            "healthy": healthy,
            "probing": probing,
            "cooldown": cooldown,
            "disabled": disabled,
            "total": healthy + probing + cooldown + disabled,
            "failures": sum(
                p.get("failures", 0) for p in providers.values() if p.get("failures")
            ),
            "global": global_stats or {},
        }

        self.history.append(point)
        if len(self.history) > self.max_points:
            self.history = self.history[-self.max_points:]

        self._persist_snapshot(point)
        self._cleanup_old()

        return point

    def _persist_snapshot(self, point: dict[str, Any]) -> None:
        """Append one line to today's JSON lines file."""
        filename = f"metrics-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.jsonl"
        path = self.snapshot_dir / filename
        try:
            with open(path, "a") as f:
                f.write(json.dumps(point, default=str) + "\n")
        except OSError as e:
            log.error("Failed to persist snapshot: %s", e)

    def _cleanup_old(self) -> None:
        """Remove JSONL files older than rotate_days."""
        cutoff = time.time() - self.rotate_days * 86400
        try:
            for f in self.snapshot_dir.glob("metrics-*.jsonl"):
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    log.info("Rotated old metrics file: %s", f.name)
        except OSError as e:
            log.error("Cleanup error: %s", e)

    def get_history(
        self,
        minutes: int = 60,
        resolution: int = 60,
    ) -> list[dict[str, Any]]:
        """Return aggregated history points.

        Args:
            minutes: How far back to look (default 60).
            resolution: Bucket size in seconds (default 60, i.e. 1 point/min).

        Returns:
            List of snapshot points, one per bucket.
        """
        cutoff = time.time() - minutes * 60
        # Try memory first
        candidates = [p for p in self.history if p["ts"] >= cutoff]

        # Fallback to reading files
        if not candidates:
            candidates = self._load_from_files(cutoff)

        # Downsample to resolution
        if not candidates:
            return []

        candidates.sort(key=lambda p: p["ts"])
        buckets: list[dict[str, Any]] = []
        bucket_start = candidates[0]["ts"]
        bucket: dict[str, Any] = {}

        for p in candidates:
            if p["ts"] >= bucket_start + resolution:
                if bucket:
                    buckets.append(bucket)
                bucket_start = p["ts"]
                bucket = dict(p)
            else:
                # Merge into current bucket (keep latest)
                bucket = dict(p)

        if bucket:
            buckets.append(bucket)

        return buckets

    def _load_from_files(self, cutoff: float) -> list[dict[str, Any]]:
        """Load history from on-disk JSONL files."""
        points: list[dict[str, Any]] = []
        try:
            for f in sorted(self.snapshot_dir.glob("metrics-*.jsonl")):
                if f.stat().st_mtime < cutoff:
                    continue
                with open(f) as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            p = json.loads(line)
                            if p.get("ts", 0) >= cutoff:
                                points.append(p)
                        except json.JSONDecodeError:
                            continue
        except OSError as e:
            log.error("Failed to load history files: %s", e)

        return points

    def export_csv(self, minutes: int = 60) -> str:
        """Export history as CSV string."""
        rows = self.get_history(minutes=minutes, resolution=60)
        if not rows:
            return "timestamp,datetime,healthy,probing,cooldown,disabled,total,failures\n"

        header = "timestamp,datetime,healthy,probing,cooldown,disabled,total,failures\n"
        lines = [header]
        for r in rows:
            lines.append(
                f'{r.get("ts", "")},{r.get("datetime", "")},'
                f'{r.get("healthy", 0)},{r.get("probing", 0)},'
                f'{r.get("cooldown", 0)},{r.get("disabled", 0)},'
                f'{r.get("total", 0)},{r.get("failures", 0)}\n'
            )
        return "".join(lines)

    def latest_summary(self) -> dict[str, Any]:
        """Return the most recent snapshot."""
        if self.history:
            return dict(self.history[-1])
        return {"ts": 0, "healthy": 0, "probing": 0, "cooldown": 0, "disabled": 0, "total": 0}
