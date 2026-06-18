"""Port services/draft-orchestrator.ts - drafty AI dla istniejacych watkow.

- ZAWSZE rotacja claude_session_id (swiezy uuid4) przed spawnem - sesja CLI
  na dysku nie moze byc reuzyta (--session-id z istniejacym uuid wisi/failuje),
- statusy draft_sessions: idle -> generating -> has_draft/error; markSent -> sent;
  resetDraft = DELETE wiersza (kaskada na iteracje),
- iterations_count przeliczane SELECT-em po INSERT (nie inkrement),
- eventy WS: draft:token / draft:tool_use / draft:status / draft:complete.
"""

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import delete, func, select, update

from app.core.claude_cli import ClaudeStreamHandlers, run_claude
from app.core.config import settings
from app.core.db import async_session_maker
from app.core.logging import create_logger
from app.core.semaphore import Semaphore
from app.core.ws import broadcast
from app.modules.circle_dm.models import Account, DraftIteration, DraftSession, Thread
from app.modules.circle_dm.services.app_settings import (
    compose_system_prompt,
    get_draft_model,
    get_global_meta_prompt,
)
from app.modules.circle_dm.services.history_formatter import format_thread_history_for_claude
from app.modules.circle_dm.services.knowledge_base import build_kb_block

log = create_logger("draft")

_sem = Semaphore(settings.CLAUDE_MAX_CONCURRENT)


async def _get_or_create_session(thread_id: int) -> DraftSession:
    async with async_session_maker() as session:
        existing = (
            await session.execute(
                select(DraftSession).where(DraftSession.thread_id == thread_id).limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing

        created = DraftSession(
            thread_id=thread_id, claude_session_id=str(uuid.uuid4()), status="idle"
        )
        session.add(created)
        await session.commit()
        return created


async def _get_account_for_thread(thread_id: int):
    async with async_session_maker() as session:
        row = (
            await session.execute(
                select(Thread.account_id, Account.system_prompt)
                .select_from(Thread)
                .join(Account, Account.id == Thread.account_id)
                .where(Thread.id == thread_id)
                .limit(1)
            )
        ).first()
    if row is None:
        raise Exception(f"thread {thread_id} not found")
    return row


@dataclass
class _RunStreamingResult:
    draft: str
    tokens_used: int | None
    cost_usd: float | None


async def _run_streaming(
    *,
    thread_id: int,
    iteration_kind: str,
    prompt: str,
    session_id: str | None = None,
    resume_session_id: str | None = None,
    append_system_prompt: str | None = None,
    model: str | None = None,
) -> _RunStreamingResult:
    def on_text_delta(text: str) -> None:
        broadcast(
            {
                "type": "draft:token",
                "threadId": thread_id,
                "chunk": text,
                "iterationKind": iteration_kind,
            }
        )

    def on_tool_use(name: str) -> None:
        broadcast({"type": "draft:tool_use", "threadId": thread_id, "toolName": name})

    def on_parse_error(line: str, err: Exception) -> None:
        log.warn(f"stream parse error: {err}", line[:200])

    result = await run_claude(
        prompt,
        session_id=session_id,
        resume_session_id=resume_session_id,
        append_system_prompt=append_system_prompt,
        model=model,
        handlers=ClaudeStreamHandlers(
            on_text_delta=on_text_delta,
            on_tool_use=on_tool_use,
            on_parse_error=on_parse_error,
        ),
    )

    if result.exit_code != 0:
        raise Exception(f"claude exited with code {result.exit_code}: {result.stderr[:500]}")
    if not result.text.strip():
        raise Exception("claude returned empty draft")

    return _RunStreamingResult(
        draft=result.text.strip(), tokens_used=result.tokens_used, cost_usd=result.cost_usd
    )


async def _persist_iteration(
    draft_session_id: int,
    thread_id: int,
    iteration_kind: str,
    user_instruction: str | None,
    result: _RunStreamingResult,
) -> None:
    async with async_session_maker() as session:
        session.add(
            DraftIteration(
                draft_session_id=draft_session_id,
                iteration_kind=iteration_kind,
                user_instruction=user_instruction,
                draft_text=result.draft,
                tokens_used=result.tokens_used,
                cost_usd=Decimal(str(result.cost_usd)) if result.cost_usd is not None else None,
            )
        )
        await session.commit()

    async with async_session_maker() as session:
        count = (
            await session.execute(
                select(func.count())
                .select_from(DraftIteration)
                .where(DraftIteration.draft_session_id == draft_session_id)
            )
        ).scalar_one()
        await session.execute(
            update(DraftSession)
            .where(DraftSession.id == draft_session_id)
            .values(
                current_draft=result.draft,
                status="ready_to_send" if iteration_kind == "polish" else "has_draft",
                iterations_count=count,
                last_error=None,
            )
        )
        await session.commit()

    broadcast(
        {
            "type": "draft:complete",
            "threadId": thread_id,
            "iterationKind": iteration_kind,
            "draft": result.draft,
            "tokensUsed": result.tokens_used,
            "costUsd": result.cost_usd,
        }
    )


async def _set_status(
    session_id: int, thread_id: int, status: str, error: str | None = None
) -> None:
    async with async_session_maker() as session:
        await session.execute(
            update(DraftSession)
            .where(DraftSession.id == session_id)
            .values(status=status, last_error=error if error is not None else None)
        )
        await session.commit()
    event: dict = {"type": "draft:status", "threadId": thread_id, "status": status}
    if error is not None:
        event["error"] = error
    broadcast(event)


async def generate_initial_draft(thread_id: int) -> None:
    session_row = await _get_or_create_session(thread_id)
    account = await _get_account_for_thread(thread_id)
    fmt = await format_thread_history_for_claude(thread_id)

    fresh_claude_session_id = str(uuid.uuid4())
    async with async_session_maker() as session:
        await session.execute(
            update(DraftSession)
            .where(DraftSession.id == session_row.id)
            .values(
                claude_session_id=fresh_claude_session_id, current_draft=None, last_error=None
            )
        )
        await session.commit()

    base_prompt = (
        f"Historia rozmowy DM (Circle):\n\n{fmt['history']}\n\n"
        f'Wcielasz się w "{fmt["adminLabel"]}". '
        f"Wygeneruj draft kolejnej wiadomości do {fmt['otherLabel']}. "
        "Pisz po polsku, naturalnie, w pierwszej osobie, zgodnie z personą. "
        "Zwróć WYŁĄCZNIE treść wiadomości — bez prefiksu, bez wyjaśnień, "
        "bez cudzysłowów, bez bloków kodu."
    )

    kb_block = await build_kb_block(account.account_id)
    user_prompt = f"{kb_block}\n\n---\n\n{base_prompt}" if kb_block else base_prompt

    meta_prompt = await get_global_meta_prompt()
    await _set_status(session_row.id, thread_id, "generating")
    release = await _sem.acquire()
    try:
        db_model = await get_draft_model()
        result = await _run_streaming(
            thread_id=thread_id,
            iteration_kind="initial",
            prompt=user_prompt,
            session_id=fresh_claude_session_id,
            append_system_prompt=compose_system_prompt(account.system_prompt, meta_prompt),
            model=db_model if db_model is not None else settings.DRAFT_MODEL,
        )
        await _persist_iteration(session_row.id, thread_id, "initial", None, result)
    except Exception as err:
        message = str(err)
        log.error(f"initial draft failed for thread {thread_id}", message)
        await _set_status(session_row.id, thread_id, "error", message)
        raise
    finally:
        release()


async def set_draft(thread_id: int, draft: str) -> None:
    session_row = await _get_or_create_session(thread_id)
    async with async_session_maker() as session:
        await session.execute(
            update(DraftSession)
            .where(DraftSession.id == session_row.id)
            .values(current_draft=draft, status="has_draft" if len(draft) > 0 else "idle")
        )
        await session.commit()


async def reset_draft(thread_id: int) -> None:
    async with async_session_maker() as session:
        await session.execute(delete(DraftSession).where(DraftSession.thread_id == thread_id))
        await session.commit()


async def mark_sent(thread_id: int) -> None:
    async with async_session_maker() as session:
        await session.execute(
            update(DraftSession)
            .where(DraftSession.thread_id == thread_id)
            .values(status="sent", current_draft=None)
        )
        await session.commit()
    broadcast({"type": "draft:status", "threadId": thread_id, "status": "sent"})
