"""Port core/auth/rate-limit.ts. In-memory limiter logowania (jeden proces,
stan ginie przy restarcie - akceptowane). Klucz bucketa: "email|ip"."""

import math
import time
from dataclasses import dataclass, field

from fastapi import Request

FAILURE_WINDOW_MS = 15 * 60 * 1000
MAX_FAILURES = 5
LOCK_DURATION_MS = 60 * 60 * 1000


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


def record_success(key: str) -> None:
    _buckets.pop(key, None)


def client_ip(request: Request) -> str:
    headers = request.headers
    value = headers.get("cf-connecting-ip")
    if value is not None:
        return value
    value = headers.get("x-real-ip")
    if value is not None:
        return value
    value = headers.get("x-forwarded-for")
    if value is not None:
        return value.split(",")[0].strip()
    return "unknown"
