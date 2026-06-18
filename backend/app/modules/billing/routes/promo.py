"""POST /api/billing/promo/validate - port validate-promo (1:1).

Odpowiedz ZAWSZE 200 z {"valid": ...} - to odpowiedz biznesowa, nie blad
(kontrakt 1.1, jedyny wyjatek od kodow 4xx). Lookup tylko konto current.
"""

from fastapi import APIRouter, Request

from app.modules.billing.schemas import PromoValidateIn
from app.modules.billing.services import promo as promo_service
from app.modules.billing.services import rate_limit as checkout_rate_limit

router = APIRouter()


@router.post("/validate")
async def validate_promo(body: PromoValidateIn, request: Request) -> dict:
    checkout_rate_limit.enforce(request, "promo-validate")
    return await promo_service.validate_code(body.code)
