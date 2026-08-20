"""Auto-manage 9router connection proxies.

Periodically renews the free-proxy pool and applies proxies to connections
that lack one (new provider or new account in an existing provider), keeping
the user rule: accounts of the same provider use distinct proxies.

Runs standalone (python3 proxy_manager.py [--dry-run]) or as a daemon loop
(via ProxyManager.ensure_all_proxied, called from daemon.py proxy_loop).
"""

import json
import logging
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from catalog_sync import compute_cli_token
from config import (
    NINEROUTER_URL,
    PROXY_FETCH_TIMEOUT,
    PROXY_MIN_POOL,
    PROXY_PROBE_MAX_WORKERS,
    PROXY_REFRESH_INTERVAL_SECONDS,
    PROXY_SOURCES,
    PROXY_TEST_TIMEOUT,
    PROXY_TEST_URL,
)

log = logging.getLogger(__name__)

DB_PATH = Path.home() / ".9router" / "db" / "data.sqlite"
BACKUP_DIR = Path.home() / ".9router" / "db" / "backups" / "manual"


def fetch_raw_proxies() -> List[str]:
    """Fetch candidate proxies from public lists (plain `ip:port` lines)."""
    candidates: List[str] = []
    seen = set()
    for url in PROXY_SOURCES:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "provider-health-daemon/1.0"})
            with urllib.request.urlopen(req, timeout=PROXY_FETCH_TIMEOUT) as resp:
                if resp.status != 200:
                    continue
                text = resp.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, OSError, ValueError) as e:
            log.debug(f"proxy source {url} failed: {e}")
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or ":" not in line:
                continue
            if "://" not in line:
                line = f"http://{line}"
            if line not in seen:
                seen.add(line)
                candidates.append(line)
    return candidates


def _probe_one(proxy_url: str) -> bool:
    handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
    opener = urllib.request.build_opener(handler)
    req = urllib.request.Request(PROXY_TEST_URL, headers={"User-Agent": "provider-health-daemon/1.0"})
    try:
        with opener.open(req, timeout=PROXY_TEST_TIMEOUT) as resp:
            return resp.status == 200
    except Exception:
        return False


def probe_proxies(proxies: List[str], max_workers: Optional[int] = None) -> List[str]:
    """Return only proxies that successfully served an HTTPS CONNECT request."""
    if not proxies:
        return []
    workers = max_workers or PROXY_PROBE_MAX_WORKERS
    alive: List[str] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = pool.map(_probe_one, proxies)
        for proxy_url, ok in zip(proxies, results):
            if ok:
                alive.append(proxy_url)
    return alive


def fetch_connections(base_url: Optional[str] = None) -> List[Dict[str, Any]]:
    """Fetch raw provider connections from the 9router admin API."""
    url = f"{base_url or NINEROUTER_URL}/api/providers"
    token = compute_cli_token()
    if not token:
        log.warning("proxy manager: no CLI token; connections fetch skipped")
        return []

    headers = {"Content-Type": "application/json", "x-9r-cli-token": token}
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status != 200:
                return []
            body = response.read()
            data = json.loads(body) if body else {}
        connections = data.get("connections", [])
        return [c for c in connections if isinstance(c, dict) and c.get("id")]
    except (urllib.error.URLError, json.JSONDecodeError):
        return []
    except Exception:
        return []


def _group_by_provider(connections: List[Dict[str, Any]]) -> "OrderedDict[str, List[Dict[str, Any]]]":
    grouped: "OrderedDict[str, List[Dict[str, Any]]]" = OrderedDict()
    for conn in connections:
        grouped.setdefault(conn.get("provider", ""), []).append(conn)
    return grouped


def compute_assignments(connections: List[Dict[str, Any]], proxy_pool: List[str]) -> Dict[str, str]:
    """Map connection id -> proxy url (round-robin, distinct within a provider)."""
    assignments: Dict[str, str] = {}
    pool = list(proxy_pool)
    if not pool or not connections:
        return assignments

    pool_idx = 0
    for provider, conns in _group_by_provider(connections).items():
        start = pool_idx % len(pool)
        for i, conn in enumerate(conns):
            assignments[conn["id"]] = pool[(start + i) % len(pool)]
        pool_idx += len(conns)
    return assignments


def _used_proxies_by_provider(connections: List[Dict[str, Any]]) -> Dict[str, set]:
    used: Dict[str, set] = {}
    for conn in connections:
        provider = conn.get("provider", "")
        psd = conn.get("providerSpecificData") or {}
        proxy = (psd.get("connectionProxyUrl") or "").strip()
        if proxy:
            used.setdefault(provider, set()).add(proxy)
    return used


def _pick_unused_proxy(pool: List[str], used: set, offset: int) -> str:
    for i in range(len(pool)):
        candidate = pool[(offset + i) % len(pool)]
        if candidate not in used:
            return candidate
    return pool[offset % len(pool)]


def assignments_for_unproxied(connections: List[Dict[str, Any]], proxy_pool: List[str]) -> Dict[str, str]:
    """Map unproxied connection id -> proxy, avoiding proxies already used by same-provider accounts."""
    assignments: Dict[str, str] = {}
    pool = list(proxy_pool)
    if not pool or not connections:
        return assignments

    used = _used_proxies_by_provider(connections)
    offset = 0
    for provider, conns in _group_by_provider(connections).items():
        for conn in conns:
            psd = conn.get("providerSpecificData") or {}
            if (psd.get("connectionProxyUrl") or "").strip():
                continue
            proxy = _pick_unused_proxy(pool, used.setdefault(provider, set()), offset)
            used[provider].add(proxy)
            assignments[conn["id"]] = proxy
            offset += 1
    return assignments


def _backup_db() -> Optional[str]:
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        dest = BACKUP_DIR / f"data.sqlite.pre-proxy-{datetime.now():%Y%m%d-%H%M%S}"
        import shutil
        shutil.copy2(DB_PATH, dest)
        return str(dest)
    except OSError as e:
        log.warning(f"proxy manager: backup failed: {e}")
        return None


def apply_assignments(assignments: Dict[str, str], backup: bool = True) -> int:
    """Persist proxy assignments into providerConnections.data (legacy fields)."""
    if not assignments:
        return 0
    if backup:
        _backup_db()

    db = sqlite3.connect(str(DB_PATH))
    changed = 0
    now = datetime.now().isoformat()
    try:
        for cid, proxy in assignments.items():
            row = db.execute(
                "SELECT data FROM providerConnections WHERE id=?", (cid,)
            ).fetchone()
            if row is None:
                continue
            try:
                d = json.loads(row[0]) if row[0] else {}
            except json.JSONDecodeError:
                log.debug(f"proxy manager: invalid JSON data for {cid}, skipping")
                continue

            psd = d.setdefault("providerSpecificData", {})
            old_url = psd.get("connectionProxyUrl", "")
            if old_url == proxy and psd.get("connectionProxyEnabled") is True:
                continue

            psd["connectionProxyEnabled"] = True
            psd["connectionProxyUrl"] = proxy
            db.execute(
                "UPDATE providerConnections SET data=?, updatedAt=? WHERE id=?",
                (json.dumps(d), now, cid),
            )
            changed += 1
        db.commit()
    finally:
        db.close()
    return changed


class ProxyManager:
    """Owns the proxy pool and applies it to 9router connections on demand."""

    def __init__(self) -> None:
        self._pool: List[str] = []
        self._pool_fetched_at: float = 0.0

    def pool_needs_refresh(self) -> bool:
        if not self._pool:
            return True
        return (time.monotonic() - self._pool_fetched_at) >= PROXY_REFRESH_INTERVAL_SECONDS

    def _load_pool(self) -> List[str]:
        candidates = fetch_raw_proxies()
        alive = probe_proxies(candidates)
        if len(alive) < PROXY_MIN_POOL:
            log.warning(
                f"proxy manager: only {len(alive)}/{len(candidates)} proxies alive "
                f"(min {PROXY_MIN_POOL}) — keeping current pool",
                extra={"event": "proxy_pool_too_small", "alive": len(alive), "candidates": len(candidates)},
            )
            return list(self._pool)
        self._pool = alive
        self._pool_fetched_at = time.monotonic()
        log.info(
            f"proxy manager: pool refreshed ({len(alive)} alive)",
            extra={"event": "proxy_pool_refreshed", "count": len(alive)},
        )
        return alive

    def ensure_all_proxied(self, dry_run: bool = False) -> Dict[str, Any]:
        """Apply proxies to any connection missing one; renew pool when stale.

        Returns a summary dict:
        {connections, unproxied, applied, pool, refreshed, dry_run}
        """
        connections = fetch_connections()
        if not connections:
            return {"connections": 0, "unproxied": 0, "applied": 0, "pool": len(self._pool), "refreshed": False, "dry_run": dry_run}

        unproxied = [
            c for c in connections
            if not ((c.get("providerSpecificData") or {}).get("connectionProxyUrl") or "").strip()
        ]

        refreshed = False
        if self.pool_needs_refresh():
            self._load_pool()
            refreshed = True

        if not self._pool:
            return {"connections": len(connections), "unproxied": len(unproxied), "applied": 0, "pool": 0, "refreshed": refreshed, "dry_run": dry_run}

        if refreshed:
            assignments = compute_assignments(connections, self._pool)
        else:
            assignments = assignments_for_unproxied(connections, self._pool)

        if dry_run:
            log.info(f"proxy manager: dry-run, {len(assignments)} would change")
            return {"connections": len(connections), "unproxied": len(unproxied), "applied": len(assignments), "pool": len(self._pool), "refreshed": refreshed, "dry_run": dry_run}

        applied = apply_assignments(assignments)
        return {"connections": len(connections), "unproxied": len(unproxied), "applied": applied, "pool": len(self._pool), "refreshed": refreshed, "dry_run": dry_run}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    dry_run = "--dry-run" in sys.argv
    manager = ProxyManager()
    result = manager.ensure_all_proxied(dry_run=dry_run)
    log.info(f"proxy manager summary: {json.dumps(result)}")


if __name__ == "__main__":
    main()
