"""Port tools/circle-dm/routes/accounts.ts (1:1 wg docs/spec/routes-a.md sekcja 2).
Montowane pod /api/circle-dm/accounts (za require_auth).

Quirki 1:1: DELETE bez sprawdzenia istnienia zawsze {"ok":true};
test-connection przy padzie 400 z ok:false; sync przy padzie 500 z ok:false
(inny ksztalt niz globalny handler); fire-and-forget sync po POST i po
/:id/sync (bledy tylko warn-log).

Swiadoma naprawa (docs/spec/port-odstepstwa.md): PATCH /:id zwraca 404
"account not found" dla nieistniejacego konta (oryginal falszywe {ok:true}).
"""

import asyncio
from collections.abc import Coroutine
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Path
from fastapi.responses import JSONResponse
from sqlalchemy import delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_session
from app.core.logging import create_logger, to_iso_string
from app.modules.circle_dm.circle.client import exchange_admin_token_for_jwt
from app.modules.circle_dm.models import Account
from app.modules.circle_dm.schemas import CreateAdminAccountBody, UpdateAdminAccountBody
from app.modules.circle_dm.services.thread_sync import (
    sync_messages_for_thread,
    sync_threads_for_account,
)

log = create_logger("routes:accounts")

router = APIRouter()

_bg_tasks: set[asyncio.Task[None]] = set()


def _fire_and_forget(coro: Coroutine[Any, Any, object], warn_prefix: str) -> None:
    async def run() -> None:
        try:
            await coro
        except Exception as err:
            log.warn(f"{warn_prefix}: {err}")

    task = asyncio.create_task(run())
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


def _parse_circle_date(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _serialize_account(row: Account) -> dict:
    return {
        "id": row.id,
        "label": row.label,
        "email": row.email,
        "hasToken": len(row.circle_admin_token) > 0,
        "communityId": row.community_id,
        "communityMemberId": row.community_member_id,
        "systemPrompt": row.system_prompt,
        "isActive": row.is_active,
        "lastSyncedAt": to_iso_string(row.last_synced_at)
        if row.last_synced_at is not None
        else None,
        "createdAt": to_iso_string(row.created_at),
        "updatedAt": to_iso_string(row.updated_at),
    }


@router.get("")
async def list_accounts(db: AsyncSession = Depends(get_session)) -> dict:
    rows = (await db.execute(select(Account))).scalars().all()
    return {"accounts": [_serialize_account(row) for row in rows]}


@router.post("")
async def create_account(
    payload: CreateAdminAccountBody,
    db: AsyncSession = Depends(get_session),
) -> JSONResponse:
    token = (
        payload.circle_admin_token
        if payload.circle_admin_token is not None
        else settings.BOOTSTRAP_ADMIN_TOKEN
    )
    if not token:
        return JSONResponse(
            {"error": "circleAdminToken missing and BOOTSTRAP_ADMIN_TOKEN is not set in env"},
            status_code=400,
        )
    inserted_id = (
        await db.execute(
            insert(Account)
            .values(
                label=payload.label,
                email=payload.email,
                circle_admin_token=token,
                system_prompt=payload.system_prompt,
            )
            .returning(Account.id)
        )
    ).scalar_one()
    await db.commit()
    log.info(f"created account {inserted_id} ({payload.email})")
    _fire_and_forget(
        sync_threads_for_account(inserted_id), f"initial sync failed for {inserted_id}"
    )
    return JSONResponse({"id": inserted_id}, status_code=201)


@router.patch("/{account_id}")
async def update_account(
    payload: UpdateAdminAccountBody,
    account_id: int = Path(gt=0),
    db: AsyncSession = Depends(get_session),
):
    provided = payload.model_fields_set
    values: dict[str, Any] = {"updated_at": datetime.now(UTC)}
    if "label" in provided:
        values["label"] = payload.label
    if "email" in provided:
        values["email"] = payload.email
    if "circle_admin_token" in provided:
        values["circle_admin_token"] = payload.circle_admin_token
        # Wymuszenie re-auth do Circle przy nastepnym wywolaniu.
        values["circle_access_token"] = None
        values["circle_access_token_expires_at"] = None
    if "system_prompt" in provided:
        values["system_prompt"] = payload.system_prompt
    if "is_active" in provided:
        values["is_active"] = payload.is_active

    updated_id = (
        await db.execute(
            update(Account).where(Account.id == account_id).values(**values).returning(Account.id)
        )
    ).scalar_one_or_none()
    await db.commit()
    if updated_id is None:
        return JSONResponse({"error": "account not found"}, status_code=404)
    return {"ok": True}


@router.delete("/{account_id}")
async def delete_account(
    account_id: int = Path(gt=0),
    db: AsyncSession = Depends(get_session),
) -> dict:
    await db.execute(delete(Account).where(Account.id == account_id))
    await db.commit()
    return {"ok": True}


@router.post("/{account_id}/test-connection")
async def test_connection(
    account_id: int = Path(gt=0),
    db: AsyncSession = Depends(get_session),
):
    account = (
        await db.execute(select(Account).where(Account.id == account_id).limit(1))
    ).scalar_one_or_none()
    if account is None:
        return JSONResponse({"error": "account not found"}, status_code=404)

    try:
        auth = await exchange_admin_token_for_jwt(account.circle_admin_token, account.email)
        await db.execute(
            update(Account)
            .where(Account.id == account_id)
            .values(
                circle_access_token=auth["access_token"],
                circle_access_token_expires_at=_parse_circle_date(
                    auth["access_token_expires_at"]
                ),
                circle_refresh_token=auth["refresh_token"],
                community_id=auth["community_id"],
                community_member_id=auth["community_member_id"],
            )
        )
        await db.commit()
        return {
            "ok": True,
            "communityId": auth["community_id"],
            "communityMemberId": auth["community_member_id"],
        }
    except Exception as err:
        return JSONResponse(
            {"ok": False, "communityId": None, "communityMemberId": None, "error": str(err)},
            status_code=400,
        )


@router.post("/{account_id}/sync")
async def sync_account(account_id: int = Path(gt=0)):
    try:
        result = await sync_threads_for_account(account_id)
        # Reczny sync dociaga tez historie wiadomosci watkow, ktorym poprzedni
        # tick zaktualizowal metadane, ale pominal wiadomosci.
        for thread_id in result["stale_message_thread_ids"]:
            _fire_and_forget(
                sync_messages_for_thread(thread_id),
                f"message sync failed for thread {thread_id}",
            )
        return {
            "ok": True,
            "changedThreadIds": result["changed_thread_ids"],
            "newUnreadThreadIds": result["new_unread_thread_ids"],
            "staleMessageThreadIds": result["stale_message_thread_ids"],
        }
    except Exception as err:
        return JSONResponse({"ok": False, "error": str(err)}, status_code=500)
