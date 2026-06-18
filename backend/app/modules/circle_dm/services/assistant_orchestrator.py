"""Port services/assistant-orchestrator.ts - asystent panelu admina.

- kazda tura = swiezy session-id uuid4, transkrypt z DB idzie stdinem
  (BEZ --resume),
- tokeny WS leca SUROWO (wlacznie z fence'em ```action) - czysci frontend,
- cancel: mapa active_turns per conversationId + SIGTERM; tura dokonczona
  sciezka "cancelled" zapisuje czesciowy tekst z markerem _(przerwane)_
  i emituje assistant:complete (nie assistant:error),
- model: app_settings.draft_model z DB, fallback settings.DRAFT_MODEL -
  swiadoma naprawa quirka oryginalu (TS ignorowal ustawienie z DB), ten sam
  wzorzec co draft_orchestrator i compose_orchestrator,
- get_message_for_apply rzuca Exception("message not found") /
  Exception("not your message") jak oryginal - route mapuje oba na 404.
"""

import asyncio
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from sqlalchemy import and_, delete, desc, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.claude_cli import ClaudeResultEvent, ClaudeStreamHandlers, run_claude
from app.core.config import settings
from app.core.db import async_session_maker
from app.core.logging import create_logger, to_iso_string
from app.core.semaphore import Semaphore
from app.core.ws import broadcast
from app.modules.admin.models import User
from app.modules.circle_dm.models import AssistantConversation, AssistantMessage
from app.modules.circle_dm.schemas import StrictId
from app.modules.circle_dm.services.app_settings import (
    get_draft_model,
    get_format_prompt,
    get_global_meta_prompt,
)
from app.modules.circle_dm.services.knowledge_base import build_kb_block

log = create_logger("assistant")
_sem = Semaphore(settings.CLAUDE_MAX_CONCURRENT)


@dataclass
class _ActiveTurn:
    proc: asyncio.subprocess.Process
    cancelled: bool = False


_active_turns: dict[int, _ActiveTurn] = {}
_background_tasks: set[asyncio.Task] = set()

MAX_HISTORY_MESSAGES = 30
MAX_DRAFT_PREVIEW = 8_000
MAX_PERSONA_PREVIEW = 6_000
MAX_KB_IN_CONTEXT = 60_000


# ─── Schemat propozycji akcji (port zod actionProposalSchema) ────────────────


class _ProposalModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _SetDraftParams(_ProposalModel):
    # StrictId = zod z.number().int().positive() bez coerce: "42"/true odpadaja.
    threadId: StrictId
    newText: str = Field(min_length=1)


class _SetDraftProposal(_ProposalModel):
    action: Literal["setDraft"]
    params: _SetDraftParams
    preview: str


class _SetPersonaParams(_ProposalModel):
    accountId: StrictId
    newText: str = Field(min_length=10)


class _SetPersonaProposal(_ProposalModel):
    action: Literal["setPersona"]
    params: _SetPersonaParams
    preview: str


class _SetGlobalMetaPromptParams(_ProposalModel):
    newText: str


class _SetGlobalMetaPromptProposal(_ProposalModel):
    action: Literal["setGlobalMetaPrompt"]
    params: _SetGlobalMetaPromptParams
    preview: str


class _SetFormatPromptParams(_ProposalModel):
    newText: str


class _SetFormatPromptProposal(_ProposalModel):
    action: Literal["setFormatPrompt"]
    params: _SetFormatPromptParams
    preview: str


class _SetKbDocParams(_ProposalModel):
    id: StrictId
    # Odpowiednik zod .optional(): brak klucza OK, jawny null NIE przechodzi
    # (default nie jest walidowany, jawna wartosc musi byc str).
    title: str = Field(default=None, min_length=1, max_length=200)
    bodyText: str = Field(default=None, max_length=500_000)


class _SetKbDocProposal(_ProposalModel):
    action: Literal["setKbDoc"]
    params: _SetKbDocParams
    preview: str


class _CreateKbManualParams(_ProposalModel):
    scope: Literal["global", "account"]
    adminAccountId: StrictId | None = None
    title: str = Field(min_length=1, max_length=200)
    bodyText: str = Field(min_length=1, max_length=500_000)


class _CreateKbManualProposal(_ProposalModel):
    action: Literal["createKbManual"]
    params: _CreateKbManualParams
    preview: str


_ActionProposal = Annotated[
    _SetDraftProposal
    | _SetPersonaProposal
    | _SetGlobalMetaPromptProposal
    | _SetFormatPromptProposal
    | _SetKbDocProposal
    | _CreateKbManualProposal,
    Field(discriminator="action"),
]

_action_proposal_adapter: TypeAdapter = TypeAdapter(_ActionProposal)


def safe_parse_action_proposal(value: object) -> tuple[dict | None, str | None]:
    """(proposal, None) przy sukcesie, (None, blad) przy zlym ksztalcie."""
    try:
        model = _action_proposal_adapter.validate_python(value)
    except ValidationError as err:
        return None, str(err)
    return model.model_dump(exclude_unset=True), None


def parse_stored_proposal(value: object) -> dict | None:
    if not value:
        return None
    proposal, _ = safe_parse_action_proposal(value)
    return proposal


# ─── Dev seed ────────────────────────────────────────────────────────────────


async def _ensure_auth_account_exists(auth_account_id: int) -> None:
    if settings.NODE_ENV == "production":
        return
    if auth_account_id != 0:
        return
    async with async_session_maker() as session:
        await session.execute(
            pg_insert(User)
            .values(id=0, email="dev@local", password_hash="")
            .on_conflict_do_nothing(index_elements=[User.id])
        )
        await session.commit()


# ─── Serializacja ────────────────────────────────────────────────────────────


def _serialize_conversation(row: AssistantConversation) -> dict:
    return {
        "id": row.id,
        "title": row.title,
        "lastMessageAt": (
            to_iso_string(row.last_message_at) if row.last_message_at is not None else None
        ),
        "createdAt": to_iso_string(row.created_at),
    }


def _serialize_message(row: AssistantMessage) -> dict:
    return {
        "id": row.id,
        "conversationId": row.conversation_id,
        "role": row.role,
        "content": row.content,
        "actionProposal": parse_stored_proposal(row.action_proposal),
        "appliedAt": to_iso_string(row.applied_at) if row.applied_at is not None else None,
        "applyError": row.apply_error,
        "createdAt": to_iso_string(row.created_at),
    }


# ─── Lifecycle konwersacji ───────────────────────────────────────────────────


async def _get_or_create_current_row(auth_account_id: int) -> AssistantConversation:
    await _ensure_auth_account_exists(auth_account_id)
    async with async_session_maker() as session:
        latest = (
            await session.execute(
                select(AssistantConversation)
                .where(AssistantConversation.user_id == auth_account_id)
                .order_by(desc(AssistantConversation.created_at))
                .limit(1)
            )
        ).scalar_one_or_none()
        if latest is not None:
            return latest

        created = AssistantConversation(user_id=auth_account_id)
        session.add(created)
        await session.commit()
        await session.refresh(created)
        return created


async def get_or_create_current_conversation(auth_account_id: int) -> dict:
    return _serialize_conversation(await _get_or_create_current_row(auth_account_id))


async def start_new_conversation(auth_account_id: int) -> dict:
    await _ensure_auth_account_exists(auth_account_id)
    async with async_session_maker() as session:
        created = AssistantConversation(user_id=auth_account_id)
        session.add(created)
        await session.commit()
        await session.refresh(created)
    return _serialize_conversation(created)


async def delete_conversation(conversation_id: int, auth_account_id: int) -> bool:
    async with async_session_maker() as session:
        result = await session.execute(
            delete(AssistantConversation).where(
                and_(
                    AssistantConversation.id == conversation_id,
                    AssistantConversation.user_id == auth_account_id,
                )
            )
        )
        await session.commit()
    return (result.rowcount or 0) > 0


async def get_conversation_full(conversation_id: int, auth_account_id: int) -> dict | None:
    async with async_session_maker() as session:
        conv = (
            await session.execute(
                select(AssistantConversation)
                .where(
                    and_(
                        AssistantConversation.id == conversation_id,
                        AssistantConversation.user_id == auth_account_id,
                    )
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if conv is None:
            return None

        rows = (
            (
                await session.execute(
                    select(AssistantMessage)
                    .where(AssistantMessage.conversation_id == conv.id)
                    .order_by(AssistantMessage.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
    return {
        "conversation": _serialize_conversation(conv),
        "messages": [_serialize_message(r) for r in rows],
    }


async def list_conversations(auth_account_id: int) -> list[dict]:
    await _ensure_auth_account_exists(auth_account_id)
    async with async_session_maker() as session:
        rows = (
            (
                await session.execute(
                    select(AssistantConversation)
                    .where(AssistantConversation.user_id == auth_account_id)
                    .order_by(desc(AssistantConversation.created_at))
                )
            )
            .scalars()
            .all()
        )
    return [_serialize_conversation(r) for r in rows]


async def cancel_turn(conversation_id: int, auth_account_id: int) -> bool:
    async with async_session_maker() as session:
        conv_user_id = (
            await session.execute(
                select(AssistantConversation.user_id)
                .where(AssistantConversation.id == conversation_id)
                .limit(1)
            )
        ).scalar_one_or_none()
    if conv_user_id is None or conv_user_id != auth_account_id:
        return False
    entry = _active_turns.get(conversation_id)
    if entry is None:
        return False
    entry.cancelled = True
    try:
        entry.proc.terminate()
    except Exception as err:
        log.warn(f"kill failed: {err}")
    return True


# ─── Wykonanie tury ──────────────────────────────────────────────────────────

ASSISTANT_SYSTEM_PROMPT = """Jesteś asystentem panelu admina Be Free Club.
Pomagasz Tomaszowi i Krystianowi z DM-ami na Circle: drafty, persona, prompty
globalne, baza wiedzy. Mów po polsku, krótko, naturalnie, bez emoji, bez
korpomowy, zero długich myślników (-).

ZAWSZE mów wprost skąd masz info: "z historii wątku", "z bazy wiedzy doc X",
"z persony konta", "z meta-promptu". Jak czegoś nie wiesz, powiedz że nie
wiesz, NIE zgaduj.

Gdy user prosi o edycję czegoś w aplikacji (draft, persona, prompt globalny,
prompt formatowania, baza wiedzy), na samym końcu wiadomości dodaj jeden blok:

```action
{"action":"<nazwa>","params":{...},"preview":"krótki opis zmiany"}
```

Wspierane action:
- setDraft         params: { threadId: number, newText: string }
- setPersona       params: { accountId: number, newText: string }
- setGlobalMetaPrompt  params: { newText: string }
- setFormatPrompt  params: { newText: string }
- setKbDoc         params: { id: number, title?: string, bodyText?: string }
- createKbManual   params: { scope: "global"|"account", adminAccountId?: number, title: string, bodyText: string }

NIE zmieniasz niczego sam. Apka renderuje propozycję z przyciskami "Zastosuj"
i "Odrzuć". Max 1 akcja na turę. Jak nie potrzeba akcji, po prostu odpowiedz
tekstem.

Nie używaj bloków kodu poza protokołem action."""


async def run_assistant_turn(
    *, conversation_id: int, auth_account_id: int, user_text: str, context: dict
) -> dict:
    async with async_session_maker() as session:
        conv = (
            await session.execute(
                select(AssistantConversation)
                .where(
                    and_(
                        AssistantConversation.id == conversation_id,
                        AssistantConversation.user_id == auth_account_id,
                    )
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if conv is None:
            raise Exception("conversation not found")

        user_msg = AssistantMessage(
            conversation_id=conversation_id,
            role="user",
            content=user_text,
            context_snapshot=context,
        )
        session.add(user_msg)
        await session.commit()

        await session.execute(
            update(AssistantConversation)
            .where(AssistantConversation.id == conversation_id)
            .values(
                last_message_at=datetime.now(UTC),
                title=conv.title if conv.title is not None else user_text[:60],
            )
        )
        await session.commit()

        history = (
            (
                await session.execute(
                    select(AssistantMessage)
                    .where(AssistantMessage.conversation_id == conversation_id)
                    .order_by(desc(AssistantMessage.created_at))
                    .limit(MAX_HISTORY_MESSAGES)
                )
            )
            .scalars()
            .all()
        )

    transcript = "\n\n".join(f"[{m.role}]: {m.content}" for m in reversed(history))
    context_block = await _build_context_block(context)
    prompt = f"<conversation>\n{transcript}\n</conversation>\n\n{context_block}"

    session_id = str(uuid.uuid4())
    result = {"userMessageId": user_msg.id, "assistantMessageId": 0, "hasAction": False}

    task = asyncio.create_task(
        _run_turn_background(
            conversation_id=conversation_id, prompt=prompt, session_id=session_id
        )
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return result


async def _run_turn_background(*, conversation_id: int, prompt: str, session_id: str) -> None:
    release = await _sem.acquire()
    acc_parts: list[str] = []
    tokens_used: int | None = None
    cost_usd_str: str | None = None
    try:

        def on_spawn(proc: asyncio.subprocess.Process) -> None:
            _active_turns[conversation_id] = _ActiveTurn(proc=proc, cancelled=False)

        def on_text_delta(text: str) -> None:
            acc_parts.append(text)
            broadcast(
                {"type": "assistant:token", "conversationId": conversation_id, "chunk": text}
            )

        def on_result(r: ClaudeResultEvent) -> None:
            nonlocal tokens_used, cost_usd_str
            if r.total_cost_usd is not None:
                cost_usd_str = str(r.total_cost_usd)
            tokens_used = (
                r.input_tokens + r.output_tokens
                if r.input_tokens is not None and r.output_tokens is not None
                else None
            )

        db_model = await get_draft_model()
        run_result = await run_claude(
            prompt,
            session_id=session_id,
            append_system_prompt=ASSISTANT_SYSTEM_PROMPT,
            model=db_model if db_model is not None else settings.DRAFT_MODEL,
            on_spawn=on_spawn,
            handlers=ClaudeStreamHandlers(on_text_delta=on_text_delta, on_result=on_result),
        )
        acc = "".join(acc_parts)

        entry = _active_turns.get(conversation_id)
        cancelled = entry.cancelled if entry is not None else False
        if not cancelled:
            if run_result.exit_code != 0:
                raise Exception(
                    f"claude exited {run_result.exit_code}: {run_result.stderr[:300]}"
                )
            if not acc.strip():
                raise Exception("empty response")

        if cancelled:
            visible_content = f"{_strip_action_block(acc).strip()}\n\n_(przerwane)_"
            proposal: dict | None = None
        else:
            visible_content, proposal = _extract_action_from_content(acc)

        async with async_session_maker() as session:
            assistant_msg = AssistantMessage(
                conversation_id=conversation_id,
                role="assistant",
                content=visible_content or "_(przerwane)_",
                raw_content=acc,
                action_proposal=proposal,
                tokens_used=tokens_used,
                cost_usd=Decimal(cost_usd_str) if cost_usd_str is not None else None,
            )
            session.add(assistant_msg)
            await session.commit()

            await session.execute(
                update(AssistantConversation)
                .where(AssistantConversation.id == conversation_id)
                .values(last_message_at=datetime.now(UTC))
            )
            await session.commit()

        broadcast(
            {
                "type": "assistant:complete",
                "conversationId": conversation_id,
                "messageId": assistant_msg.id,
                "hasAction": proposal is not None,
            }
        )
    except Exception as err:
        message = str(err)
        log.error(f"assistant turn failed (conv {conversation_id})", message)
        broadcast(
            {"type": "assistant:error", "conversationId": conversation_id, "error": message}
        )
    finally:
        _active_turns.pop(conversation_id, None)
        release()


# ─── Blok kontekstu ──────────────────────────────────────────────────────────


def _truncate(s: str, max_len: int) -> str:
    if len(s) <= max_len:
        return s
    return f"{s[:max_len]}\n... [skrócone, oryginał {len(s)} znaków]"


async def _build_context_block(ctx: dict) -> str:
    lines: list[str] = []
    meta = (await get_global_meta_prompt()).strip()
    if meta:
        lines.append(f"globalMetaPrompt:\n{meta}")
    format_p = (await get_format_prompt()).strip()
    if format_p:
        lines.append(f"formatPrompt:\n{format_p[:4000]}")

    kb_block = ""
    kind = ctx["kind"]

    if kind == "thread":
        kb_block = await build_kb_block(ctx["adminAccountId"])
        recipient = ctx["recipientName"] if ctx["recipientName"] is not None else "(brak nazwy)"
        lines.extend(
            [
                f"threadId: {ctx['threadId']}",
                f"recipient: {recipient}",
                f"account: {ctx['accountLabel']} (id {ctx['adminAccountId']})",
                f"persona:\n{_truncate(ctx['persona'], MAX_PERSONA_PREVIEW)}",
                "currentDraft (z textarea, NIE z DB):\n"
                + (_truncate(ctx["draftText"], MAX_DRAFT_PREVIEW) or "(pusty)"),
                f"history (ostatnie wiadomości):\n{_truncate(ctx['historyExcerpt'], 12_000)}",
            ]
        )
    elif kind == "compose":
        kb_block = await build_kb_block(ctx["adminAccountId"])
        lines.extend(
            [
                f"memberId: {ctx['memberId']}",
                f"memberName: {ctx['memberName']}",
                f"account: {ctx['accountLabel']} (id {ctx['adminAccountId']})",
                f"persona:\n{_truncate(ctx['persona'], MAX_PERSONA_PREVIEW)}",
                "memberProfile:\n" + (ctx["memberProfile"] or "(brak)"),
                "currentText:\n" + (_truncate(ctx["currentText"], MAX_DRAFT_PREVIEW) or "(pusty)"),
            ]
        )
    elif kind == "settings":
        lines.extend(
            [
                "currentMetaPrompt:\n" + (ctx["metaPrompt"] or "(pusty)"),
                "currentFormatPrompt:\n" + (ctx["formatPrompt"] or "(pusty)"),
            ]
        )
    elif kind == "account":
        lines.extend(
            [
                f"accountId: {ctx['accountId']}",
                f"label: {ctx['label']}",
                f"personaText:\n{_truncate(ctx['personaText'], MAX_PERSONA_PREVIEW)}",
            ]
        )
    elif kind == "inbox":
        account_value = (
            ctx["adminAccountId"] if ctx["adminAccountId"] is not None else "(brak aktywnego)"
        )
        lines.extend(
            [
                f"filter: {ctx['filter']}",
                f"sort: {ctx['sort']}",
                "query: " + (ctx["query"] or "(brak)"),
                f"account: {account_value}",
            ]
        )

    body = "\n\n".join(lines)
    kb_slice = f"\n\n{_truncate(kb_block, MAX_KB_IN_CONTEXT)}" if kb_block else ""
    return f'<context kind="{kind}">\n{body}{kb_slice}\n</context>'


# ─── Parsowanie bloku action ─────────────────────────────────────────────────

_ACTION_FENCE = re.compile(r"```action\s*\n(.*?)```", re.DOTALL)


def _strip_action_block(text: str) -> str:
    return _ACTION_FENCE.sub("", text).rstrip()


def _extract_action_from_content(raw: str) -> tuple[str, dict | None]:
    matches = list(_ACTION_FENCE.finditer(raw))
    if len(matches) == 0:
        return raw.strip(), None
    json_text = matches[0].group(1).strip()
    try:
        parsed = json.loads(json_text)
    except Exception as err:
        log.warn(f"action JSON parse failed: {err}")
        return _strip_action_block(raw).strip(), None
    proposal, validation_error = safe_parse_action_proposal(parsed)
    if proposal is None:
        log.warn(f"action JSON shape invalid: {validation_error}")
        return _strip_action_block(raw).strip(), None
    return _strip_action_block(raw).strip(), proposal


# ─── Apply ───────────────────────────────────────────────────────────────────


async def _get_message_row_for_apply(
    message_id: int, auth_account_id: int
) -> AssistantMessage:
    async with async_session_maker() as session:
        row = (
            await session.execute(
                select(AssistantMessage, AssistantConversation.user_id)
                .join(
                    AssistantConversation,
                    AssistantConversation.id == AssistantMessage.conversation_id,
                )
                .where(AssistantMessage.id == message_id)
                .limit(1)
            )
        ).first()
    if row is None:
        raise Exception("message not found")
    msg, conv_user_id = row
    if conv_user_id != auth_account_id:
        raise Exception("not your message")
    return msg


async def get_message_for_apply(message_id: int, auth_account_id: int) -> dict | None:
    msg = await _get_message_row_for_apply(message_id, auth_account_id)
    return {
        "id": msg.id,
        "conversationId": msg.conversation_id,
        "role": msg.role,
        "content": msg.content,
        "rawContent": msg.raw_content,
        "contextSnapshot": msg.context_snapshot,
        "actionProposal": msg.action_proposal,
        "appliedAt": msg.applied_at,
        "applyError": msg.apply_error,
        "tokensUsed": msg.tokens_used,
        "costUsd": msg.cost_usd,
        "createdAt": msg.created_at,
        "updatedAt": msg.updated_at,
    }


async def mark_applied(message_id: int, error: str | None) -> None:
    async with async_session_maker() as session:
        await session.execute(
            update(AssistantMessage)
            .where(AssistantMessage.id == message_id)
            .values(applied_at=None if error else datetime.now(UTC), apply_error=error)
        )
        await session.commit()


async def get_message_by_id(message_id: int, auth_account_id: int) -> dict | None:
    try:
        msg = await _get_message_row_for_apply(message_id, auth_account_id)
    except Exception:
        return None
    return _serialize_message(msg)
