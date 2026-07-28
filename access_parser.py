"""Real-time parser for 9router access.log — extracts metrics from every request.

Parses lines like:
  🟢 ▶ POST main-rr → nvidia/minimaxai/minimax-m3 · STREAM · 2 MSG · 43 TOOL · ACC:nvidia
  🟢 📊 DONE 8908ms · TTFT 8414ms · IN 51007 (CACHE ↻128) · OUT 15
  ℹ️  [COMBO] Trying model 1/8: nvidia/minimaxai/minimax-m3
  ℹ️  [COMBO] Model nvidia/minimaxai/minimax-m3 succeeded
  ❌ [groq] [404]: model not found
"""

import re
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

# ── Regex patterns ───────────────────────────────────────────────────

# "▶ POST main-rr → nvidia/minimaxai/minimax-m3 · STREAM · 2 MSG · 43 TOOL · ACC:nvidia"
RE_REQUEST = re.compile(
    r"[▶].*?(?P<direction>→)\s*(?P<model>\S+?)\s*(?:·\s*.*?)?(?:ACC:\s*(?P<provider>\S+))?"
)

# "📊 DONE 8908ms · TTFT 8414ms · IN 51007 (CACHE ↻128) · OUT 15"
RE_DONE = re.compile(
    r"DONE\s+(?P<duration_ms>\d+)ms\s*·\s*TTFT\s+(?P<ttft_ms>\d+)ms"
    r"(?:\s*·\s*IN\s+(?P<tokens_in>\d+))?"
    r"(?:\s*\(CACHE\s*↻\s*(?P<tokens_cache>\d+)\))?"
    r"(?:\s*·\s*OUT\s+(?P<tokens_out>\d+))?"
)

# "[COMBO] Trying model X/Y: <model>"
RE_COMBO_TRY = re.compile(r"\[COMBO\]\s+Trying\s+model\s+\d+/\d+:\s*(\S+)")

# "[COMBO] Model <model> succeeded|failed"
RE_COMBO_RESULT = re.compile(r"\[COMBO\]\s+Model\s+(\S+)\s+(succeeded|failed)")

# "❌ [provider] [status]: body"
RE_ERROR = re.compile(r"❌\s*\[(\w+)\]\s*\[(\d+)\]\s*:\s*(.+)")

# Timestamp prefix: [HH:MM:SS]
RE_TS = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\]")


@dataclass
class RequestEvent:
    """A single request tracked through its lifecycle."""
    timestamp: datetime
    model: str
    provider: str
    combo_name: str
    duration_ms: Optional[int] = None
    ttft_ms: Optional[int] = None
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cache: int = 0
    success: bool = True
    error: Optional[str] = None

    @property
    def total_tokens(self) -> int:
        return self.tokens_in + self.tokens_out


@dataclass
class ComboEvent:
    """A combo round-robin event."""
    timestamp: datetime
    model: str
    provider: str
    attempt: int  # which number in the rotation
    success: bool


def _parse_timestamp(ts_str: str) -> datetime:
    """Parse [HH:MM:SS] into a datetime (today's date)."""
    now = datetime.now(timezone.utc)
    try:
        h, m, s = ts_str.split(":")
        return now.replace(hour=int(h), minute=int(m), second=int(s), microsecond=0)
    except (ValueError, AttributeError):
        return now


def parse_line(line: str) -> Optional[dict]:
    """Parse a single access.log line into an event dict, or None if not parseable.

    Returns one of:
      {"type": "request", "model": ..., "provider": ..., "combo": ...}
      {"type": "done", "model": ..., "provider": ..., "duration_ms": ..., "tokens_in": ..., ...}
      {"type": "combo_try", "model": ...}
      {"type": "combo_result", "model": ..., "success": bool}
      {"type": "error", "provider": ..., "status": int, "body": ...}
    """
    stripped = line.strip()
    if not stripped:
        return None

    # Extract timestamp if present
    ts = datetime.now(timezone.utc)
    ts_m = RE_TS.match(stripped)
    if ts_m:
        ts = _parse_timestamp(ts_m.group(1))

    # ── Error lines ──────────────────────────────────────────────────
    m = RE_ERROR.search(stripped)
    if m:
        return {
            "type": "error",
            "timestamp": ts,
            "provider": m.group(1),
            "status": int(m.group(2)),
            "body": m.group(3)[:200],
        }

    # ── Combo lines ──────────────────────────────────────────────────
    m = RE_COMBO_TRY.search(stripped)
    if m:
        model = m.group(1)
        provider = model.split("/")[0] if "/" in model else model
        return {
            "type": "combo_try",
            "timestamp": ts,
            "model": model,
            "provider": provider,
        }

    m = RE_COMBO_RESULT.search(stripped)
    if m:
        model = m.group(1)
        provider = model.split("/")[0] if "/" in model else model
        success = m.group(2) == "succeeded"
        return {
            "type": "combo_result",
            "timestamp": ts,
            "model": model,
            "provider": provider,
            "success": success,
        }

    # ── Request lines (▶) ────────────────────────────────────────────
    m = RE_REQUEST.search(stripped)
    if m:
        model = m.group("model")
        provider_from_acc = m.group("provider") or (model.split("/")[0] if "/" in model else model)
        return {
            "type": "request",
            "timestamp": ts,
            "model": model,
            "provider": provider_from_acc,
            "combo": model,  # might be main-rr or combo-round-robin
        }

    # ── Done lines (📊 DONE) ─────────────────────────────────────────
    m = RE_DONE.search(stripped)
    if m:
        return {
            "type": "done",
            "timestamp": ts,
            "duration_ms": int(m.group("duration_ms")),
            "ttft_ms": int(m.group("ttft_ms")),
            "tokens_in": int(m.group("tokens_in") or 0),
            "tokens_out": int(m.group("tokens_out") or 0),
            "tokens_cache": int(m.group("tokens_cache") or 0),
        }

    return None


def format_event(event: dict) -> str:
    """Human-readable summary of a parsed event."""
    typ = event.get("type", "?")
    if typ == "request":
        return f"REQ {event.get('model','?')}"
    elif typ == "done":
        return f"DONE {event.get('duration_ms',0)}ms · IN:{event.get('tokens_in',0)} OUT:{event.get('tokens_out',0)}"
    elif typ == "combo_try":
        return f"COMBO→ {event.get('model','?')}"
    elif typ == "combo_result":
        return f"COMBO{'✅' if event.get('success') else '❌'} {event.get('model','?')}"
    elif typ == "error":
        return f"ERROR {event.get('provider','?')} [{event.get('status','?')}]"
    return str(event)
