"""Port routes/bulk.ts. Montowane pod /api/circle-dm/bulk (za require_auth).

Ta sama tresc do mieszanej listy (watki + nowi odbiorcy). Sekwencyjnie
i synchronicznie (celowo rate-friendly), zawsze 200. Liczby w body BEZ
coerce (stringi nie przechodza walidacji) - jak w oryginale.
"""

from fastapi import APIRouter

from app.modules.circle_dm.schemas import BulkSendRequest, dump
from app.modules.circle_dm.services.bulk_send import send_to_multiple

router = APIRouter()


@router.post("/send")
async def send(payload: BulkSendRequest) -> dict:
    items = [dump(item) for item in payload.items]
    results = await send_to_multiple(items, payload.body)
    ok_count = sum(1 for r in results if r["ok"])
    return {"totalCount": len(results), "okCount": ok_count, "results": results}
