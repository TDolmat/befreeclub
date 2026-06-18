"""Port routes/compose.ts. Montowane pod /api/circle-dm/compose (za require_auth).

Pierwsza wiadomosc do NOWEGO odbiorcy. Generate synchroniczny (bez WS),
send: 200 przy sukcesie, 502 przy porazce Circle; braki konto/member
rzucaja -> globalny handler 500.
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.modules.circle_dm.schemas import ComposeGenerateRequest, ComposeSendRequest
from app.modules.circle_dm.services.compose_orchestrator import (
    generate_compose_draft,
    send_compose_draft,
)

router = APIRouter()


@router.post("/generate")
async def generate(payload: ComposeGenerateRequest) -> dict:
    return await generate_compose_draft(
        payload.admin_account_id, payload.circle_community_member_id
    )


@router.post("/send")
async def send(payload: ComposeSendRequest) -> JSONResponse:
    result = await send_compose_draft(
        payload.admin_account_id, payload.circle_community_member_id, payload.body
    )
    return JSONResponse(result, status_code=200 if result["ok"] else 502)
