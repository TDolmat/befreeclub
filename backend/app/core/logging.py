"""Port core/util/logger.ts. Format linii identyczny:

[{ISO8601}] {LEVEL padEnd(5)} {scope padEnd(16)} {msg}[ {extra}]
"""

import json
import sys
from datetime import UTC, datetime

from app.core.config import settings

LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40}

_threshold = LEVELS[settings.LOG_LEVEL]

_UNSET = object()


def to_iso_string(dt: datetime) -> str:
    """Date.prototype.toISOString z Node: UTC, milisekundy, sufiks Z."""
    dt = dt.astimezone(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _fmt(level: str, scope: str, msg: str, extra: object = _UNSET) -> str:
    ts = to_iso_string(datetime.now(UTC))
    base = f"[{ts}] {level.upper():<5} {scope:<16} {msg}"
    if extra is _UNSET:
        return base
    try:
        rendered = extra if isinstance(extra, str) else json.dumps(
            extra, ensure_ascii=False, separators=(",", ":"), default=str
        )
        return f"{base} {rendered}"
    except Exception:
        return base


class Logger:
    def __init__(self, scope: str) -> None:
        self._scope = scope

    def debug(self, msg: str, extra: object = _UNSET) -> None:
        if LEVELS["debug"] >= _threshold:
            print(_fmt("debug", self._scope, msg, extra), file=sys.stdout, flush=True)

    def info(self, msg: str, extra: object = _UNSET) -> None:
        if LEVELS["info"] >= _threshold:
            print(_fmt("info", self._scope, msg, extra), file=sys.stdout, flush=True)

    def warn(self, msg: str, extra: object = _UNSET) -> None:
        if LEVELS["warn"] >= _threshold:
            print(_fmt("warn", self._scope, msg, extra), file=sys.stderr, flush=True)

    def error(self, msg: str, extra: object = _UNSET) -> None:
        if LEVELS["error"] >= _threshold:
            print(_fmt("error", self._scope, msg, extra), file=sys.stderr, flush=True)


def create_logger(scope: str) -> Logger:
    return Logger(scope)
