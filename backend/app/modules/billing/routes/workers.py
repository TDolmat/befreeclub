"""Reczne triggery workerow fazy 2 ([workers]).

POST /api/billing/admin/workers/{name}/run - montowane w main.py ZA
require_auth (sesja panelu admina z fazy 1). Zwraca wynik przebiegu
w ksztalcie 1:1 z odpowiedziami oryginalnych edge functions (+ nowe
liczniki napraw). Workery:
  membership_cleanup  -> members/services/cleanup_worker (port circle-cleanup)
  klarna_reconcile    -> billing/services/klarna_reconcile_worker
                         (port reconcile-klarna-checkouts)
  invite_retry        -> members/services/invite_retry_worker
                         (port retry-circle-invites)

sync-circle-ids celowo NIE jest workerem - jednorazowa operacja pod
POST /api/members/sync-circle-ids (members/routes/maintenance.py).
"""

from fastapi import APIRouter, HTTPException

from app.modules.billing.services import klarna_reconcile_worker
from app.modules.members.services import cleanup_worker, invite_retry_worker

router = APIRouter()


async def _run_membership_cleanup() -> dict:
    # Reczny trigger admina: tryb (dryRun) brany z admin.settings
    # `members.cleanup`. Bramka `enabled` go NIE blokuje (swiadoma akcja).
    result = await cleanup_worker.run_now()
    # {success, checked, removed} 1:1 z circle-cleanup + tryb/wouldRemove
    # + decyzje per czlonek (panel admina widzi, czemu kto zostal/wylecial).
    return {
        "success": True,
        "checked": result.checked,
        "removed": result.removed,
        "wouldRemove": result.would_remove,
        "dryRun": result.dry_run,
        "decisions": [
            {
                "memberId": d.member_id,
                "email": d.email,
                "decision": d.decision,
                "removed": d.removed,
            }
            for d in result.decisions
        ],
    }


async def _run_klarna_reconcile() -> dict:
    summary = await klarna_reconcile_worker.run_now()
    return summary.as_dict()


async def _run_invite_retry() -> dict:
    results = await invite_retry_worker.run_now()
    # {results: [{email, success, circleMemberId, error}]} 1:1 z retry-circle-invites.
    return {
        "results": [
            {
                "email": r.email,
                "success": r.success,
                "circleMemberId": r.circle_member_id,
                "error": r.error,
            }
            for r in results
        ]
    }


_RUNNERS = {
    "membership_cleanup": _run_membership_cleanup,
    "klarna_reconcile": _run_klarna_reconcile,
    "invite_retry": _run_invite_retry,
}


@router.post("/{name}/run")
async def run_worker(name: str) -> dict:
    runner = _RUNNERS.get(name)
    if runner is None:
        raise HTTPException(status_code=404, detail=f"Unknown worker: {name}")
    return await runner()
