"""GET /api/billing/plans - publiczny cennik dla landinga (zamiast hardcode
w Pricing.tsx). Zaimplementowane w fundamencie."""

from fastapi import APIRouter

from app.core.schemas import dump
from app.modules.billing.schemas import PlanOut
from app.modules.billing.services import plans as plans_service

router = APIRouter()


@router.get("")
async def list_plans() -> dict:
    rows = await plans_service.list_active()
    return {"plans": [dump(PlanOut.model_validate(row)) for row in rows]}
