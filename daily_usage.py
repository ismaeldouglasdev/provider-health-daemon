"""Agregação de uso diário por provider a partir do access.log do 9router.

Fonte: parse_line() de access_parser.py sobre ~/.9router/logs/access.log.
O log só carrega timestamp HH:MM:SS (data de hoje) — consultas por datas
passadas retornam {} (rotação de log cuida do histórico).
"""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from access_parser import parse_line

LOG_PATH = Path.home() / ".9router" / "logs" / "access.log"
TZ = ZoneInfo("America/Sao_Paulo")


def _today_sp() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


def daily_provider_usage(date: str | None = None, log_path: str | None = None) -> dict:
    """Soma tokens e requests do dia por provider.

    Tokens de um evento `done` são atribuídos ao último provider visto em um
    evento `request` (ordem do stream). Requests sem done contam como 1 request,
    0 tokens. Data alvo diferente de hoje retorna {} (log não tem data histórica).
    """
    target = date or _today_sp()
    if target != _today_sp():
        return {}

    path = Path(log_path) if log_path else LOG_PATH
    if not path.exists():
        return {}

    usage: dict[str, dict] = {}
    last_provider: str | None = None
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            event = parse_line(line)
            if not isinstance(event, dict):
                continue
            etype = event.get("type")
            if etype == "request":
                # Normaliza "github|user_01..." -> "github" para alinhar com
                # os nomes de provider usados pelo smart_router.
                raw = str(event.get("provider") or "unknown")
                provider = raw.split("|")[0] or "unknown"
                last_provider = provider
                entry = usage.setdefault(provider, {"tokens": 0, "requests": 0})
                entry["requests"] += 1
            elif etype == "done" and last_provider is not None:
                entry = usage.setdefault(last_provider, {"tokens": 0, "requests": 0})
                entry["tokens"] += (
                    int(event.get("tokens_in") or 0)
                    + int(event.get("tokens_out") or 0)
                    + int(event.get("tokens_cache") or 0)
                )
    return usage


if __name__ == "__main__":
    import json

    print(json.dumps(daily_provider_usage(), indent=1, ensure_ascii=False))


class CachedProviderUsage:
    """Cache com TTL para daily_provider_usage — evita re-ler o access.log
    inteiro a cada request do proxy (log cresce durante o dia)."""

    def __init__(self, ttl_seconds: int = 60, log_path: str | None = None):
        self.ttl_seconds = ttl_seconds
        self.log_path = log_path
        self._cache: dict | None = None
        self._cached_at = 0.0
        import threading

        self._lock = threading.Lock()

    def get(self) -> dict:
        import time as _time

        now = _time.monotonic()
        with self._lock:
            if self._cache is not None and now - self._cached_at < self.ttl_seconds:
                return self._cache
        data = daily_provider_usage(log_path=self.log_path)
        with self._lock:
            self._cache = data
            self._cached_at = now
        return data
