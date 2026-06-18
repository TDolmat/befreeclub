"""Port core/claude/spawn.ts + core/claude/stream-parser.ts.

- prompt idzie ZAWSZE stdinem (nie argv - --disallowedTools jest variadic),
- kolejnosc flag CLI dokladnie jak w oryginale,
- BEZ timeoutu - proces moze dzialac dowolnie dlugo, jedyny kill to cancel
  przez uchwyt z on_spawn (proc.terminate() = SIGTERM),
- exit code: Node daje null gdy proces zabity sygnalem; w Pythonie returncode
  jest ujemny (-15 dla SIGTERM) - mapujemy ujemne na None,
- run_claude dodatkowo akumuluje text/session_id/tokens_used/cost_usd
  (wspolny wzorzec wszystkich orchestratorow), ale decyzje o exit code i
  pustej odpowiedzi podejmuje caller (komunikaty bledow sa per orchestrator).
"""

import asyncio
import codecs
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings
from app.core.logging import create_logger

log = create_logger("claude:spawn")

DISALLOWED_TOOLS = ["Bash", "Edit", "Write", "WebSearch", "WebFetch"]


@dataclass
class ClaudeResultEvent:
    total_cost_usd: float | None
    input_tokens: int | None
    output_tokens: int | None
    duration_ms: int | None
    raw: Any


@dataclass
class ClaudeStreamHandlers:
    on_system_init: Callable[[str], None] | None = None
    on_text_delta: Callable[[str], None] | None = None
    on_tool_use: Callable[[str], None] | None = None
    on_result: Callable[[ClaudeResultEvent], None] | None = None
    on_unknown: Callable[[Any], None] | None = None
    on_parse_error: Callable[[str, Exception], None] | None = None


class ClaudeStreamParser:
    """JSONL ze stdout CLI: bufor na niedokonczone linie, flush() na EOF."""

    def __init__(self, handlers: ClaudeStreamHandlers) -> None:
        self._handlers = handlers
        self._buffer = ""

    def feed(self, chunk: str) -> None:
        self._buffer += chunk
        while (nl := self._buffer.find("\n")) != -1:
            line = self._buffer[:nl].strip()
            self._buffer = self._buffer[nl + 1 :]
            if not line:
                continue
            self._parse_line(line)

    def flush(self) -> None:
        last = self._buffer.strip()
        self._buffer = ""
        if last:
            self._parse_line(last)

    def _parse_line(self, line: str) -> None:
        h = self._handlers
        try:
            event = json.loads(line)
        except Exception as err:
            if h.on_parse_error:
                h.on_parse_error(line, err)
            return

        if not isinstance(event, dict):
            if h.on_unknown:
                h.on_unknown(event)
            return

        if event.get("type") == "system" and event.get("subtype") == "init":
            session_id = event.get("session_id")
            if session_id and h.on_system_init:
                h.on_system_init(session_id)
            return

        message = event.get("message")
        if event.get("type") == "assistant" and isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text" and isinstance(block.get("text"), str):
                        if h.on_text_delta:
                            h.on_text_delta(block["text"])
                    elif block.get("type") == "tool_use" and isinstance(block.get("name"), str):
                        if h.on_tool_use:
                            h.on_tool_use(block["name"])
                return

        if event.get("type") == "result":
            if h.on_result:
                usage = event.get("usage")
                usage = usage if isinstance(usage, dict) else {}
                h.on_result(
                    ClaudeResultEvent(
                        total_cost_usd=event.get("total_cost_usd"),
                        input_tokens=usage.get("input_tokens"),
                        output_tokens=usage.get("output_tokens"),
                        duration_ms=event.get("duration_ms"),
                        raw=event,
                    )
                )
            return

        if h.on_unknown:
            h.on_unknown(event)


@dataclass
class RunClaudeResult:
    exit_code: int | None
    stderr: str
    text: str
    session_id: str | None
    tokens_used: int | None
    cost_usd: float | None


@dataclass
class _Accumulator:
    parts: list[str] = field(default_factory=list)
    session_id: str | None = None
    tokens_used: int | None = None
    cost_usd: float | None = None


async def run_claude(
    prompt: str,
    *,
    session_id: str | None = None,
    resume_session_id: str | None = None,
    append_system_prompt: str | None = None,
    model: str | None = None,
    on_spawn: Callable[[asyncio.subprocess.Process], None] | None = None,
    handlers: ClaudeStreamHandlers | None = None,
) -> RunClaudeResult:
    handlers = handlers or ClaudeStreamHandlers()

    args: list[str] = ["--print", "--verbose", "--output-format", "stream-json"]
    if resume_session_id:
        args += ["--resume", resume_session_id]
    elif session_id:
        args += ["--session-id", session_id]
    if append_system_prompt:
        args += ["--append-system-prompt", append_system_prompt]
    if model:
        args += ["--model", model]
    args += ["--permission-mode", "bypassPermissions"]
    args += ["--disallowedTools", ",".join(DISALLOWED_TOOLS)]
    args += ["--input-format", "text"]
    # Prompt is piped via stdin (NOT as positional arg) to avoid being
    # consumed by variadic flags like --disallowedTools.

    acc = _Accumulator()

    def _on_system_init(sid: str) -> None:
        acc.session_id = sid
        if handlers.on_system_init:
            handlers.on_system_init(sid)

    def _on_text_delta(text: str) -> None:
        acc.parts.append(text)
        if handlers.on_text_delta:
            handlers.on_text_delta(text)

    def _on_result(result: ClaudeResultEvent) -> None:
        if result.input_tokens is not None and result.output_tokens is not None:
            acc.tokens_used = result.input_tokens + result.output_tokens
        else:
            acc.tokens_used = None
        acc.cost_usd = result.total_cost_usd
        if handlers.on_result:
            handlers.on_result(result)

    parser = ClaudeStreamParser(
        ClaudeStreamHandlers(
            on_system_init=_on_system_init,
            on_text_delta=_on_text_delta,
            on_tool_use=handlers.on_tool_use,
            on_result=_on_result,
            on_unknown=handlers.on_unknown,
            on_parse_error=handlers.on_parse_error,
        )
    )

    log.debug(
        f"spawn {settings.CLAUDE_BIN_PATH}",
        {
            "mode": "resume" if resume_session_id else "new",
            "sessionId": resume_session_id or session_id,
            "model": model,
            "promptPreview": prompt[:100],
        },
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            settings.CLAUDE_BIN_PATH,
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as err:
        log.error("spawn error", str(err))
        raise

    if on_spawn:
        on_spawn(proc)

    assert proc.stdin and proc.stdout and proc.stderr

    try:
        proc.stdin.write(prompt.encode("utf-8"))
        await proc.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    try:
        proc.stdin.close()
    except Exception:
        pass

    stderr_parts: list[str] = []

    async def _read_stdout() -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while True:
            chunk = await proc.stdout.read(65536)  # type: ignore[union-attr]
            if not chunk:
                break
            text = decoder.decode(chunk)
            if text:
                parser.feed(text)
        tail = decoder.decode(b"", final=True)
        if tail:
            parser.feed(tail)

    async def _read_stderr() -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while True:
            chunk = await proc.stderr.read(65536)  # type: ignore[union-attr]
            if not chunk:
                break
            text = decoder.decode(chunk)
            if text:
                stderr_parts.append(text)
                log.debug("stderr", text[:200])

    await asyncio.gather(_read_stdout(), _read_stderr())
    code = await proc.wait()
    parser.flush()
    log.debug(f"claude exited with code {code}")

    exit_code = code if code >= 0 else None

    return RunClaudeResult(
        exit_code=exit_code,
        stderr="".join(stderr_parts),
        text="".join(acc.parts),
        session_id=acc.session_id,
        tokens_used=acc.tokens_used,
        cost_usd=acc.cost_usd,
    )
