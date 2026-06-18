"""Port services/assistant-actions.ts - wykonanie zaakceptowanej propozycji.

Wolane tylko po kliknieciu "Zastosuj". Sukces = cichy return, blad = wyjatek
(route zapisuje go przez mark_applied i zwraca 500).
"""

from datetime import UTC, datetime

from sqlalchemy import insert, update

from app.core.db import async_session_maker
from app.core.logging import create_logger
from app.modules.circle_dm.models import Account, KbDocument
from app.modules.circle_dm.services.app_settings import (
    set_format_prompt,
    set_global_meta_prompt,
)
from app.modules.circle_dm.services.draft_orchestrator import set_draft
from app.modules.circle_dm.services.knowledge_base import estimate_tokens, invalidate_kb_cache

log = create_logger("assistant:apply")


async def apply_action(proposal: dict) -> None:
    action = proposal["action"]
    params = proposal["params"]

    if action == "setDraft":
        await set_draft(params["threadId"], params["newText"])
        log.info(
            f"setDraft applied for thread {params['threadId']} "
            f"({len(params['newText'])} chars)"
        )
        return

    if action == "setPersona":
        async with async_session_maker() as session:
            updated = (
                await session.execute(
                    update(Account)
                    .where(Account.id == params["accountId"])
                    .values(system_prompt=params["newText"], updated_at=datetime.now(UTC))
                    .returning(Account.id)
                )
            ).scalar_one_or_none()
            await session.commit()
        if updated is None:
            raise Exception(f"account {params['accountId']} not found")
        log.info(f"setPersona applied for account {params['accountId']}")
        return

    if action == "setGlobalMetaPrompt":
        await set_global_meta_prompt(params["newText"])
        log.info("setGlobalMetaPrompt applied")
        return

    if action == "setFormatPrompt":
        await set_format_prompt(params["newText"])
        log.info("setFormatPrompt applied")
        return

    if action == "setKbDoc":
        values: dict = {"updated_at": datetime.now(UTC)}
        if "title" in params:
            values["title"] = params["title"]
        if "bodyText" in params:
            values["body_text"] = params["bodyText"]
            values["token_estimate"] = estimate_tokens(params["bodyText"])
        async with async_session_maker() as session:
            updated = (
                await session.execute(
                    update(KbDocument)
                    .where(KbDocument.id == params["id"])
                    .values(**values)
                    .returning(KbDocument.id)
                )
            ).scalar_one_or_none()
            await session.commit()
        if updated is None:
            raise Exception(f"kb doc {params['id']} not found")
        invalidate_kb_cache()
        log.info(f"setKbDoc applied for doc {params['id']}")
        return

    if action == "createKbManual":
        scope = params["scope"]
        admin_account_id = params.get("adminAccountId")
        if scope == "account" and not admin_account_id:
            raise Exception("adminAccountId required for scope=account")
        async with async_session_maker() as session:
            inserted_id = (
                await session.execute(
                    insert(KbDocument)
                    .values(
                        scope=scope,
                        account_id=admin_account_id if scope == "account" else None,
                        title=params["title"],
                        body_text=params["bodyText"],
                        source_kind="manual",
                        token_estimate=estimate_tokens(params["bodyText"]),
                    )
                    .returning(KbDocument.id)
                )
            ).scalar_one()
            await session.commit()
        invalidate_kb_cache()
        log.info(f"createKbManual applied → doc {inserted_id}")
        return
