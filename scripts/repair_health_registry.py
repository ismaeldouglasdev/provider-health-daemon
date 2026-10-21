#!/usr/bin/env python3
"""Requeue falsely-disabled health entries after verifying they answer live.

Usage:
    python3 repair_health_registry.py --dry-run
    python3 repair_health_registry.py --apply

Why: a permanent `disabled` entry (until=null) has NO recovery path — the
recovery prober only rotates `cooldown`/`probing`, never `disabled`. Old false
positives (e.g. `context_length (max_failures)` recorded from one oversized
probe) therefore remove a live model from every future combo forever.
This script re-probes each disabled provider/model with a small prompt and
forces the ones that answer back to healthy.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HEALTH_PATH = Path(os.environ.get("HEALTH_PATH", str(Path.home() / ".9router" / "health.json")))
GATEWAY = os.environ.get("NINEROUTER_URL", "http://127.0.0.1:20128")
KEY = os.environ.get("NINEROUTER_KEY", "sk-47cf0a5d2c5c4000-zjk3df-94c6d9ba")
PROBE_TIMEOUT = 60
BACKUP_SUFFIX = f".bak-registry-repair-{datetime.now().strftime('%Y%m%d-%H%M%S')}"


def probe_chat(model: str) -> tuple[bool, str]:
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "say ok"}],
        "max_tokens": 8,
        "stream": False,
    }).encode()
    req = urllib.request.Request(
        f"{GATEWAY.rstrip('/')}/v1/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            if resp.status == 200 and '"choices"' in body:
                return True, "200 ok"
            return False, f"HTTP {resp.status}: {body[:120]}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read()[:120].decode(errors='replace')}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def disabled_entries(data: dict) -> list[tuple[str, str]]:
    """Return (kind, key) for every disabled provider and model."""
    out = []
    for name, entry in data.get("providers", {}).items():
        if isinstance(entry, dict) and entry.get("status") == "disabled":
            out.append(("provider", name))
    for name, entry in data.get("models", {}).items():
        if isinstance(entry, dict) and entry.get("status") == "disabled":
            out.append(("model", name))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not args.apply and not args.dry_run:
        ap.error("pass --dry-run or --apply")

    data = json.loads(HEALTH_PATH.read_text())
    targets = disabled_entries(data)
    print(f"health.json: {HEALTH_PATH}")
    print(f"disabled entries: {len(targets)}")

    repaired, still_dead = [], []
    for kind, name in targets:
        if kind == "provider":
            models = [
                m for m in data.get("models", {})
                if m.split("/")[0] == name
            ]
            if not models:
                still_dead.append((kind, name, "no model to probe"))
                continue
            probe_model = models[0]
        else:
            probe_model = name

        ok, detail = probe_chat(probe_model)
        tag = "ALIVE" if ok else "DEAD "
        print(f"  {tag} [{kind}] {name} (probe: {probe_model}) :: {detail}")
        if ok:
            repaired.append((kind, name))
        else:
            still_dead.append((kind, name, detail))

    print(f"\nalive-and-disabled (to repair): {len(repaired)}")
    for kind, name in repaired:
        print(f"  + {kind} {name}")
    print(f"still dead (left disabled): {len(still_dead)}")
    for kind, name, why in still_dead:
        print(f"  - {kind} {name} :: {why}")

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return 0

    if not repaired:
        print("\nnothing to repair — no write.")
        return 0

    backup = HEALTH_PATH.with_name(HEALTH_PATH.name + BACKUP_SUFFIX)
    backup.write_text(json.dumps(data, indent=2))
    print(f"\nbackup: {backup}")

    now = datetime.now(timezone.utc).isoformat()
    for kind, name in repaired:
        if kind == "provider":
            data["providers"].pop(name, None)
            for m in list(data.get("models", {}).keys()):
                if m.split("/")[0] == name:
                    data["models"].pop(m, None)
        else:
            data["models"].pop(name, None)
    HEALTH_PATH.write_text(json.dumps(data, indent=2))
    print(f"applied at {now}: {len(repaired)} entries reset to healthy")
    print("restart the daemon so the in-memory registry reloads:")
    print("  systemctl --user restart provider-health-daemon.service")
    return 0


if __name__ == "__main__":
    sys.exit(main())
