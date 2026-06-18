"""Port services/format-orchestrator.ts - "Formatuj z AI".

Trzy warianty (thread/compose/bulk), wspolny silnik: bloki user promptu
laczone "\\n\\n---\\n\\n" w kolejnosci KB -> historia -> profil -> kontekst ->
tekst -> instrukcja. Synchroniczne, bez WS. Model: format_model z DB albo
POLISH_MODEL z env. Prompty i contextHinty DOSLOWNIE (dlugie myslniki celowe).
"""

import asyncio
import uuid

from sqlalchemy import and_, select

from app.core.claude_cli import run_claude
from app.core.config import settings
from app.core.db import async_session_maker
from app.core.logging import create_logger
from app.core.semaphore import Semaphore
from app.modules.circle_dm.models import Account, Member, Thread
from app.modules.circle_dm.services.app_settings import (
    compose_format_system_prompt,
    get_format_model,
    get_format_prompt,
    get_global_meta_prompt,
)
from app.modules.circle_dm.services.history_formatter import format_thread_history_for_claude
from app.modules.circle_dm.services.knowledge_base import build_kb_block

log = create_logger("format")
_sem = Semaphore(settings.CLAUDE_MAX_CONCURRENT)

DEFAULT_FORMAT_PROMPT = """Twoje zadanie: wziąć tekst od użytkownika i przerobić go w finalną wiadomość DM do drugiej osoby, zgodnie z personą i kontekstem rozmowy.

Tekst od użytkownika może być:
- Roboczym draftem (gotowym do polerowania)
- Brain dumpem z dyktowania (luźne notatki, ad-hoc gramatyka)
- Krótką instrukcją tego co chcę napisać (np. "zaproś go na spotkanie we wtorek")

Wytyczne:
- Zachowaj naturalny, mówiony ton z persony — to nie ma być korpomowa.
- Popraw gramatykę i interpunkcję, ale **nie wygładzaj do bezpłciowego stylu**.
- Jeśli to brain dump — zrekonstruuj wiadomość w pierwszej osobie zgodnie z personą.
- Jeśli to gotowy draft — popraw co trzeba, ale zachowaj sens.
- Krótko (zwykle 1–4 zdania).
- Zwróć WYŁĄCZNIE finalną treść wiadomości — bez prefiksu "Oto:", bez wyjaśnień, bez cudzysłowów."""


async def _run_formatting(
    *,
    persona: str,
    user_text: str,
    recipient_name: str,
    kb_account_id: int,
    model: str,
    history: str | None = None,
    context_hint: str | None = None,
    recipient_profile: str | None = None,
) -> dict:
    meta_prompt, format_prompt_raw, kb_block = await asyncio.gather(
        get_global_meta_prompt(), get_format_prompt(), build_kb_block(kb_account_id)
    )
    format_prompt = format_prompt_raw if format_prompt_raw.strip() else DEFAULT_FORMAT_PROMPT

    blocks: list[str] = []
    if kb_block:
        blocks.append(kb_block)
    if history:
        blocks.append(f"Historia rozmowy:\n\n{history}")
    if recipient_profile:
        blocks.append(recipient_profile)
    ctx_lines = [f"Wiadomość będzie wysłana do: {recipient_name}"]
    if context_hint:
        ctx_lines.append(context_hint)
    blocks.append("\n".join(ctx_lines))
    blocks.append(f"Tekst do przerobienia:\n\n{user_text}")
    blocks.append("Zwróć WYŁĄCZNIE finalną treść wiadomości.")
    user_prompt = "\n\n---\n\n".join(blocks)

    release = await _sem.acquire()
    try:
        result = await run_claude(
            user_prompt,
            session_id=str(uuid.uuid4()),
            append_system_prompt=compose_format_system_prompt(
                persona, meta_prompt, format_prompt
            ),
            model=model,
        )
        if result.exit_code != 0:
            raise Exception(
                f"claude exited with code {result.exit_code}: {result.stderr[:500]}"
            )
        if not result.text.strip():
            raise Exception("claude returned empty result")
        return {
            "text": result.text.strip(),
            "tokensUsed": result.tokens_used,
            "costUsd": result.cost_usd,
        }
    finally:
        release()


async def format_for_thread(thread_id: int, user_text: str) -> dict:
    async with async_session_maker() as session:
        thread = (
            await session.execute(
                select(
                    Thread.id,
                    Thread.account_id,
                    Thread.other_participant_name,
                    Thread.chat_room_name,
                )
                .where(Thread.id == thread_id)
                .limit(1)
            )
        ).first()
        if thread is None:
            raise Exception(f"thread {thread_id} not found")

        account = (
            await session.execute(
                select(Account).where(Account.id == thread.account_id).limit(1)
            )
        ).scalar_one_or_none()
        if account is None:
            raise Exception(f"admin_account {thread.account_id} not found")

    fmt = await format_thread_history_for_claude(thread_id)
    if thread.other_participant_name is not None:
        recipient = thread.other_participant_name
    elif thread.chat_room_name is not None:
        recipient = thread.chat_room_name
    else:
        recipient = "odbiorca"

    log.info(f"format for thread {thread_id} ({len(user_text)} chars)")
    db_model = await get_format_model()
    return await _run_formatting(
        persona=account.system_prompt,
        history=fmt["history"],
        user_text=user_text,
        recipient_name=recipient,
        kb_account_id=thread.account_id,
        model=db_model if db_model is not None else settings.POLISH_MODEL,
    )


async def format_for_compose(
    account_id: int, circle_community_member_id: int, user_text: str
) -> dict:
    async with async_session_maker() as session:
        account = (
            await session.execute(select(Account).where(Account.id == account_id).limit(1))
        ).scalar_one_or_none()
        if account is None:
            raise Exception(f"admin_account {account_id} not found")

        member = (
            await session.execute(
                select(Member)
                .where(
                    and_(
                        Member.account_id == account_id,
                        Member.circle_community_member_id == circle_community_member_id,
                    )
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if member is None:
            raise Exception(f"member {circle_community_member_id} not cached")

    profile_lines: list[str] = []
    if member.headline:
        profile_lines.append(f"Headline: {member.headline}")
    if member.bio:
        profile_lines.append(f"Bio: {member.bio}")
    if member.location:
        profile_lines.append(f"Lokalizacja: {member.location}")
    recipient_profile = (
        "Co wiemy o tej osobie:\n" + "\n".join(profile_lines) if profile_lines else None
    )

    log.info(f"format compose to {member.name} ({len(user_text)} chars)")
    db_model = await get_format_model()
    return await _run_formatting(
        persona=account.system_prompt,
        recipient_profile=recipient_profile,
        context_hint="To PIERWSZA wiadomość do tej osoby — nigdy wcześniej nie pisaliście.",
        user_text=user_text,
        recipient_name=member.name,
        kb_account_id=account_id,
        model=db_model if db_model is not None else settings.POLISH_MODEL,
    )


async def format_for_bulk(account_id: int, user_text: str) -> dict:
    async with async_session_maker() as session:
        account = (
            await session.execute(select(Account).where(Account.id == account_id).limit(1))
        ).scalar_one_or_none()
        if account is None:
            raise Exception(f"admin_account {account_id} not found")

    log.info(f"format bulk ({len(user_text)} chars)")
    db_model = await get_format_model()
    return await _run_formatting(
        persona=account.system_prompt,
        context_hint=(
            "Wiadomość pójdzie do wielu osób ze społeczności — "
            "pisz neutralnie, bez personalizacji per osoba."
        ),
        user_text=user_text,
        recipient_name="członek społeczności",
        kb_account_id=account_id,
        model=db_model if db_model is not None else settings.POLISH_MODEL,
    )
