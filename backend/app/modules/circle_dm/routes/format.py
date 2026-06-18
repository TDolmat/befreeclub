"""Port routes/format.ts. Montowane pod /api/circle-dm/format (za require_auth).

"Formatuj z AI" - trzy warianty, wszystkie synchroniczne, bez WS.
Bledy serwisu/CLI -> globalny handler 500 {"error": ...}.
"""

from fastapi import APIRouter

from app.modules.circle_dm.schemas import (
    FormatBulkRequest,
    FormatComposeRequest,
    FormatThreadRequest,
)
from app.modules.circle_dm.services.format_orchestrator import (
    format_for_bulk,
    format_for_compose,
    format_for_thread,
)

router = APIRouter()


@router.post("/thread")
async def thread(payload: FormatThreadRequest) -> dict:
    return await format_for_thread(payload.thread_id, payload.text)


@router.post("/compose")
async def compose(payload: FormatComposeRequest) -> dict:
    return await format_for_compose(
        payload.admin_account_id, payload.circle_community_member_id, payload.text
    )


@router.post("/bulk")
async def bulk(payload: FormatBulkRequest) -> dict:
    return await format_for_bulk(payload.admin_account_id, payload.text)
