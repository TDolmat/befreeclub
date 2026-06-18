"""Port routes/kb.ts. Montowane pod /api/circle-dm/kb (za require_auth).

Baza wiedzy. Listingi NIE selectuja original_data_b64 (duze wiersze);
hasOriginal liczony w SQL. Upload multipart parsowany RECZNIE (komunikaty
bledow dosłownie, w tym dlugi myslnik), tytul z uploadu BEZ limitu 200
znakow (quirk - JSON-owy POST limit ma). DELETE zawsze {ok:true}.
"""

import base64
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Path, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import and_, delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile

from app.core.db import get_session
from app.core.logging import create_logger, to_iso_string
from app.modules.circle_dm.models import KbDocument
from app.modules.circle_dm.schemas import CreateKbManualRequest, UpdateKbRequest
from app.modules.circle_dm.services.knowledge_base import (
    estimate_tokens,
    extract_text_from_upload,
    get_kb_capacity,
    invalidate_kb_cache,
)

log = create_logger("routes:kb")

router = APIRouter()

MAX_FILE_BYTES = 10 * 1024 * 1024

_LIST_COLUMNS = (
    KbDocument.id,
    KbDocument.scope,
    KbDocument.account_id,
    KbDocument.title,
    KbDocument.source_kind,
    KbDocument.original_filename,
    KbDocument.original_data_b64.isnot(None).label("has_original"),
    KbDocument.token_estimate,
    KbDocument.enabled,
    KbDocument.created_at,
    KbDocument.updated_at,
)


def _to_list_item(row) -> dict:
    return {
        "id": row.id,
        "scope": row.scope,
        "adminAccountId": row.account_id,
        "title": row.title,
        "sourceKind": row.source_kind,
        "originalFilename": row.original_filename,
        "hasOriginal": row.has_original,
        "tokenEstimate": row.token_estimate,
        "enabled": row.enabled,
        "createdAt": to_iso_string(row.created_at),
        "updatedAt": to_iso_string(row.updated_at),
    }


@router.get("")
async def list_documents(
    scope: Literal["global", "account"],
    account_id: int | None = Query(default=None, alias="accountId", gt=0),
    db: AsyncSession = Depends(get_session),
):
    if scope == "account" and not account_id:
        return JSONResponse({"error": "accountId required for scope=account"}, status_code=400)

    where = (
        KbDocument.scope == "global"
        if scope == "global"
        else and_(KbDocument.scope == "account", KbDocument.account_id == account_id)
    )
    rows = (
        await db.execute(select(*_LIST_COLUMNS).where(where).order_by(KbDocument.id.asc()))
    ).all()

    capacity = await get_kb_capacity(None if scope == "global" else account_id)
    return {"documents": [_to_list_item(r) for r in rows], "capacity": capacity}


@router.get("/{id}")
async def get_document(id: int = Path(gt=0), db: AsyncSession = Depends(get_session)):
    row = (
        await db.execute(
            select(*_LIST_COLUMNS, KbDocument.body_text).where(KbDocument.id == id).limit(1)
        )
    ).first()
    if row is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {**_to_list_item(row), "bodyText": row.body_text}


@router.get("/{id}/original")
async def get_original(id: int = Path(gt=0), db: AsyncSession = Depends(get_session)):
    row = (
        await db.execute(
            select(
                KbDocument.original_data_b64,
                KbDocument.original_mime,
                KbDocument.original_filename,
            )
            .where(KbDocument.id == id)
            .limit(1)
        )
    ).first()
    if row is None or row.original_data_b64 is None:
        return JSONResponse({"error": "no original file"}, status_code=404)

    data = base64.b64decode(row.original_data_b64)
    filename = row.original_filename if row.original_filename is not None else f"kb-{id}"
    # quote z safe="!*'()" = encodeURIComponent z JS.
    return Response(
        content=data,
        status_code=200,
        media_type=(
            row.original_mime if row.original_mime is not None else "application/octet-stream"
        ),
        headers={
            "Content-Disposition": f'attachment; filename="{quote(filename, safe="!*\'()")}"'
        },
    )


@router.post("")
async def create_manual(
    payload: CreateKbManualRequest, db: AsyncSession = Depends(get_session)
):
    if payload.scope == "account" and not payload.admin_account_id:
        return JSONResponse(
            {"error": "adminAccountId required for scope=account"}, status_code=400
        )
    inserted_id = (
        await db.execute(
            insert(KbDocument)
            .values(
                scope=payload.scope,
                account_id=payload.admin_account_id if payload.scope == "account" else None,
                title=payload.title,
                body_text=payload.body_text,
                source_kind="manual",
                token_estimate=estimate_tokens(payload.body_text),
            )
            .returning(KbDocument.id)
        )
    ).scalar_one()
    await db.commit()
    invalidate_kb_cache()
    log.info(f"kb manual entry {inserted_id} created ({payload.scope})")
    return JSONResponse({"id": inserted_id}, status_code=201)


@router.post("/upload")
async def upload(request: Request, db: AsyncSession = Depends(get_session)):
    form = await request.form()
    file = form.get("file")
    scope = form.get("scope")
    title_raw = form.get("title")
    account_id_raw = form.get("adminAccountId")

    if not isinstance(file, UploadFile):
        return JSONResponse({"error": "file missing"}, status_code=400)
    if scope != "global" and scope != "account":
        return JSONResponse({"error": "scope must be global|account"}, status_code=400)

    # Odpowiednik Number(accountIdRaw): nieparsowalne/0 -> brak wartosci.
    account_id: int | None = None
    if isinstance(account_id_raw, str) and account_id_raw:
        try:
            parsed = float(account_id_raw)
        except ValueError:
            parsed = float("nan")
        if parsed == parsed and parsed != 0:
            account_id = int(parsed)
    if scope == "account" and not account_id:
        return JSONResponse(
            {"error": "adminAccountId required for scope=account"}, status_code=400
        )

    buf = await file.read()
    if len(buf) > MAX_FILE_BYTES:
        return JSONResponse({"error": "plik za duży (max 10 MB)"}, status_code=400)

    filename = file.filename or ""
    try:
        extracted = await extract_text_from_upload(filename, file.content_type or "", buf)
    except Exception as err:
        log.warn(f"extraction failed for {filename}: {err}")
        return JSONResponse(
            {"error": f"nie udało się odczytać pliku: {err}"}, status_code=400
        )
    if not extracted["text"]:
        return JSONResponse(
            {
                "error": (
                    "nie wyciągnąłem tekstu (skan, grafika albo format binarny np. docx) "
                    "— zapisz jako .txt/.md/.pdf albo wklej treść ręcznie"
                )
            },
            status_code=400,
        )

    title = (
        title_raw.strip()
        if isinstance(title_raw, str) and title_raw.strip()
        else filename
    )

    inserted_id = (
        await db.execute(
            insert(KbDocument)
            .values(
                scope=scope,
                account_id=account_id if scope == "account" else None,
                title=title,
                body_text=extracted["text"],
                source_kind=extracted["sourceKind"],
                original_filename=filename,
                original_mime=file.content_type or None,
                original_data_b64=base64.b64encode(buf).decode("ascii"),
                token_estimate=estimate_tokens(extracted["text"]),
            )
            .returning(KbDocument.id)
        )
    ).scalar_one()
    await db.commit()
    invalidate_kb_cache()
    log.info(f"kb upload {inserted_id} ({filename}, {extracted['sourceKind']})")
    return JSONResponse(
        {"id": inserted_id, "tokenEstimate": estimate_tokens(extracted["text"])},
        status_code=201,
    )


@router.patch("/{id}")
async def update_document(
    payload: UpdateKbRequest, id: int = Path(gt=0), db: AsyncSession = Depends(get_session)
):
    values: dict = {"updated_at": datetime.now(UTC)}
    if "title" in payload.model_fields_set:
        values["title"] = payload.title
    if "enabled" in payload.model_fields_set:
        values["enabled"] = payload.enabled
    if "body_text" in payload.model_fields_set:
        values["body_text"] = payload.body_text
        values["token_estimate"] = estimate_tokens(payload.body_text)

    updated = (
        await db.execute(
            update(KbDocument).where(KbDocument.id == id).values(**values).returning(KbDocument.id)
        )
    ).scalar_one_or_none()
    await db.commit()
    if updated is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    invalidate_kb_cache()
    return {"ok": True}


@router.delete("/{id}")
async def delete_document(id: int = Path(gt=0), db: AsyncSession = Depends(get_session)):
    await db.execute(delete(KbDocument).where(KbDocument.id == id))
    await db.commit()
    invalidate_kb_cache()
    return {"ok": True}
