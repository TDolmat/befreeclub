"""Limiter publicznych endpointow CHECKOUTU (port-kontrakt-2.md sekcja 1.4).

Kopia wzorca _Bucket z app/modules/admin/services/rate_limit.py z innymi
parametrami: 30 prob / 15 min -> lock 15 min (domyslne 5/15min limitera
logowania byloby za malo przy retry po bledzie karty). Klucz bucketa:
"<endpoint>|<ip>". In-memory, jeden proces, stan ginie przy restarcie.
"""

import math
import time
from dataclasses import dataclass, field

from fastapi import HTTPException, Request

from app.modules.admin.services.rate_limit import client_ip

FAILURE_WINDOW_MS = 15 * 60 * 1000
MAX_FAILURES = 30
LOCK_DURATION_MS = 15 * 60 * 1000

RATE_LIMIT_MESSAGE = "Zbyt wiele prób. Spróbuj ponownie później."


@dataclass
class _Bucket:
    failures: list[int] = field(default_factory=list)
    locked_until: int = 0


_buckets: dict[str, _Bucket] = {}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _get_bucket(key: str) -> _Bucket:
    bucket = _buckets.get(key)
    if bucket is None:
        bucket = _Bucket()
        _buckets[key] = bucket
    cutoff = _now_ms() - FAILURE_WINDOW_MS
    bucket.failures = [t for t in bucket.failures if t > cutoff]
    return bucket


def is_locked(key: str) -> dict:
    bucket = _get_bucket(key)
    now = _now_ms()
    if bucket.locked_until > now:
        return {"locked": True, "retry_after_sec": math.ceil((bucket.locked_until - now) / 1000)}
    return {"locked": False}


def record_failure(key: str) -> dict:
    bucket = _get_bucket(key)
    bucket.failures.append(_now_ms())
    if len(bucket.failures) >= MAX_FAILURES:
        bucket.locked_until = _now_ms() + LOCK_DURATION_MS
        bucket.failures = []
        return {"locked_now": True, "retry_after_sec": math.ceil(LOCK_DURATION_MS / 1000)}
    return {"locked_now": False}


def enforce(request: Request, endpoint: str) -> None:
    """Wzorzec z kontraktu: kazdy request zuzywa probe, lock -> 429."""
    key = f"{endpoint}|{client_ip(request)}"
    if is_locked(key)["locked"]:
        raise HTTPException(429, RATE_LIMIT_MESSAGE)
    record_failure(key)


def reset() -> None:
    """Czysci stan limitera (testy)."""
    _buckets.clear()
