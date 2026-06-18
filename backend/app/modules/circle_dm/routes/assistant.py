"""Port routes/assistant.ts. Montowane pod /api/circle-dm/assistant (za require_auth).

Konwersacje per user panelu (auth.auth_account_id). Quirki 1:1:
- POST /turn -> 202 {ok, userMessageId, assistantMessageId: 0, hasAction: false}
  (placeholdery - prawdziwe wartosci tylko w WS assistant:complete),
- DELETE /conversation/:id i POST /cancel zawsze 200 {ok: bool},
- dismiss nie sprawdza czy wiadomosc ma akcje (nadpisuje applyError).
"""

from fastapi import APIRouter, Depends, Path, Query
from fastapi.responses import JSONResponse

from app.core.logging import create_logger
from app.modules.admin.services.auth import AuthContext, require_auth
from app.modules.circle_dm.schemas import (
    AssistantCancelRequest,
    AssistantTurnRequest,
    dump,
)
from app.modules.circle_dm.services.assistant_actions import apply_action
from app.modules.circle_dm.services.assistant_orchestrator import (
    cancel_turn,
    delete_conversation,
    get_conversation_full,
    get_message_by_id,
    get_message_for_apply,
    get_or_create_current_conversation,
    list_conversations,
    mark_applied,
    run_assistant_turn,
    safe_parse_action_proposal,
    start_new_conversation,
)

log = create_logger("routes:assistant")

router = APIRouter()


@router.get("/conversations")
async def conversations(auth: AuthContext = Depends(require_auth)) -> dict:
    items = await list_conversations(auth.auth_account_id)
    return {"conversations": items}


@router.get("/conversation")
async def conversation(
    auth: AuthContext = Depends(require_auth),
    id: int | None = Query(default=None, gt=0),
):
    try:
        if id is not None:
            full = await get_conversation_full(id, auth.auth_account_id)
            if full is None:
                raise Exception("conversation not found")
        else:
            current = await get_or_create_current_conversation(auth.auth_account_id)
            full = await get_conversation_full(current["id"], auth.auth_account_id)
            if full is None:
                raise Exception("conversation not found")
        return full
    except Exception as err:
        return JSONResponse({"error": str(err)}, status_code=404)


@router.post("/new")
async def new_conversation(auth: AuthContext = Depends(require_auth)) -> dict:
    conv = await start_new_conversation(auth.auth_account_id)
    return {"conversation": conv, "messages": []}


@router.delete("/conversation/{id}")
async def delete_conversation_route(
    id: int = Path(gt=0), auth: AuthContext = Depends(require_auth)
) -> dict:
    ok = await delete_conversation(id, auth.auth_account_id)
    return {"ok": ok}


@router.post("/turn")
async def turn(
    payload: AssistantTurnRequest, auth: AuthContext = Depends(require_auth)
) -> JSONResponse:
    try:
        result = await run_assistant_turn(
            conversation_id=payload.conversation_id,
            auth_account_id=auth.auth_account_id,
            user_text=payload.message,
            context=dump(payload.context),
        )
    except Exception as err:
        m = str(err)
        log.warn(f"turn rejected: {m}")
        return JSONResponse({"error": m}, status_code=400)
    return JSONResponse({"ok": True, **result}, status_code=202)


@router.post("/messages/{id}/apply")
async def apply_message(id: int = Path(gt=0), auth: AuthContext = Depends(require_auth)):
    try:
        msg = await get_message_for_apply(id, auth.auth_account_id)
    except Exception as err:
        return JSONResponse({"error": str(err)}, status_code=404)
    if msg["actionProposal"] is None:
        return JSONResponse({"error": "message has no action"}, status_code=400)
    if msg["appliedAt"]:
        return JSONResponse({"error": "already applied"}, status_code=400)

    proposal, parse_error = safe_parse_action_proposal(msg["actionProposal"])
    if proposal is None:
        await mark_applied(id, f"invalid stored action: {parse_error}")
        return JSONResponse({"error": "invalid action shape"}, status_code=400)
    try:
        await apply_action(proposal)
        await mark_applied(id, None)
    except Exception as err:
        m = str(err)
        await mark_applied(id, m)
        return JSONResponse({"ok": False, "error": m}, status_code=500)
    updated = await get_message_by_id(id, auth.auth_account_id)
    return {"ok": True, "message": updated}


@router.post("/cancel")
async def cancel(
    payload: AssistantCancelRequest, auth: AuthContext = Depends(require_auth)
) -> dict:
    killed = await cancel_turn(payload.conversation_id, auth.auth_account_id)
    return {"ok": killed}


@router.post("/messages/{id}/dismiss")
async def dismiss_message(id: int = Path(gt=0), auth: AuthContext = Depends(require_auth)):
    # Dismiss = sentinel 'dismissed' w applyError przy appliedAt NULL;
    # bez sprawdzania czy wiadomosc w ogole ma akcje (quirk 1:1).
    try:
        await get_message_for_apply(id, auth.auth_account_id)
        await mark_applied(id, "dismissed")
    except Exception as err:
        return JSONResponse({"error": str(err)}, status_code=404)
    return {"ok": True}
