"""Port core/health/claude-health.ts. Plytkie checki (binary, credentials,
--version) rownolegle; deep probe (haiku "ping") z cache 1h in-memory, ktory
CELOWO trzyma takze porazki (oszczednosc limitu Max przy monitoringu)."""

import asyncio
import os
import time
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import settings
from app.core.logging import to_iso_string

DEEP_PROBE_CACHE_MS = 60 * 60 * 1000

_deep_probe_cache: dict | None = None  # {"ok": bool, "detail": str | None, "at": int ms}


async def _run_process(
    args: list[str], *, timeout_ms: int, stdin_data: bytes | None = None
) -> tuple[int | None, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(stdin_data), timeout=timeout_ms / 1000
        )
    except TimeoutError:
        # Node spawn z opcja timeout wysyla SIGTERM; exit code = null -> tu -15.
        proc.terminate()
        await proc.wait()
        return proc.returncode, "", ""
    return (
        proc.returncode,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


async def _check_binary() -> dict:
    if os.path.exists(settings.CLAUDE_BIN_PATH):
        return {"ok": True, "detail": settings.CLAUDE_BIN_PATH}
    return {"ok": False, "detail": f"binary not found at {settings.CLAUDE_BIN_PATH}"}


async def _check_credentials() -> dict:
    cred_path = Path.home() / ".claude" / ".credentials.json"
    if not cred_path.exists():
        return {"ok": False, "detail": "credentials file missing - run `claude login`"}
    try:
        size = cred_path.stat().st_size
    except OSError as err:
        return {"ok": False, "detail": str(err)}
    if size < 50:
        return {"ok": False, "detail": f"credentials file suspiciously small ({size} bytes)"}
    return {"ok": True, "detail": f"{size} bytes"}


async def _check_version() -> dict:
    try:
        code, stdout, stderr = await _run_process(
            [settings.CLAUDE_BIN_PATH, "--version"], timeout_ms=5000
        )
    except OSError as err:
        return {"ok": False, "detail": str(err)}
    if code != 0:
        return {"ok": False, "detail": f"exit {code}: {(stderr or stdout)[:120].strip()}"}
    return {"ok": True, "detail": stdout.strip()[:60]}


async def _run_deep_probe() -> dict:
    try:
        code, stdout, stderr = await _run_process(
            [
                settings.CLAUDE_BIN_PATH,
                "--print",
                "--model",
                "claude-haiku-4-5",
                "--input-format",
                "text",
            ],
            timeout_ms=20_000,
            stdin_data=b"ping",
        )
    except OSError as err:
        return {"ok": False, "detail": str(err)}
    if code != 0:
        return {"ok": False, "detail": f"exit {code}: {(stderr or stdout)[:200].strip()}"}
    out = stdout.strip()
    if not out:
        return {"ok": False, "detail": "claude returned empty output"}
    return {"ok": True, "detail": out[:60]}


async def get_claude_health(*, deep: bool = False) -> dict:
    global _deep_probe_cache

    binary, credentials, version = await asyncio.gather(
        _check_binary(), _check_credentials(), _check_version()
    )

    result: dict = {
        "ok": binary["ok"] and credentials["ok"] and version["ok"],
        "checks": {"binary": binary, "credentials": credentials, "version": version},
    }

    if deep:
        now = int(time.time() * 1000)
        if _deep_probe_cache is None or now - _deep_probe_cache["at"] > DEEP_PROBE_CACHE_MS:
            probe = await _run_deep_probe()
            _deep_probe_cache = {"ok": probe["ok"], "detail": probe.get("detail"), "at": now}
        deep_probe: dict = {"ok": _deep_probe_cache["ok"]}
        if _deep_probe_cache["detail"] is not None:
            deep_probe["detail"] = _deep_probe_cache["detail"]
        deep_probe["cachedFor"] = round((now - _deep_probe_cache["at"]) / 1000)
        deep_probe["lastRunAt"] = to_iso_string(
            datetime.fromtimestamp(_deep_probe_cache["at"] / 1000, tz=UTC)
        )
        result["checks"]["deepProbe"] = deep_probe
        result["ok"] = result["ok"] and _deep_probe_cache["ok"]

    return result
