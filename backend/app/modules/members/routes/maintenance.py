"""POST /api/members/sync-circle-ids ([workers]) - port sync-circle-ids.

Jednorazowa operacja uzupelnienia brakujacych circle_member_id (bez ID
cleanup nie umie nikogo usunac z Circle). Celowo NIE worker - decyzja
zadania [workers]. Logika w services/maintenance.sync_circle_ids ([members]).
Montowane w main.py pod /api/members ZA require_auth (osobny plik, zeby
nie kolidowac z routes/admin.py od [admin-api]). Odpowiedz 1:1
z oryginalna edge function: {success, circleTotal, found, notFound,
notFoundEmails}.
"""

from fastapi import APIRouter

from app.modules.members.services import maintenance

router = APIRouter()


@router.post("/sync-circle-ids")
async def sync_circle_ids() -> dict:
    result = await maintenance.sync_circle_ids()
    return {
        "success": True,
        "circleTotal": result.circle_total,
        "found": result.found,
        "notFound": result.not_found,
        "notFoundEmails": result.not_found_emails,
    }
