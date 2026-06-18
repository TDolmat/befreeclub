"""Port tools/circle-dm/services/bulk-send.ts (1:1 wg docs/spec/services-sync.md
sekcja 6). Petla SCISLE sekwencyjna (rate-friendly wobec Circle), bez przerwania
na bledzie; klucz 'error' w wyniku obecny tylko przy porazce (undefined w TS
= pomijany w JSON)."""

from sqlalchemy import select

from app.core.db import async_session_maker
from app.core.logging import create_logger
from app.modules.circle_dm.models import Thread
from app.modules.circle_dm.services.compose_orchestrator import send_compose_draft
from app.modules.circle_dm.services.send import send_draft

log = create_logger("bulk-send")


async def send_to_multiple(items: list[dict], body: str) -> list[dict]:
    results: list[dict] = []
    log.info(f"bulk send to {len(items)} recipients")

    for item in items:
        if item["kind"] == "thread":
            thread_id = item["threadId"]
            async with async_session_maker() as session:
                thread = (
                    await session.execute(select(Thread.id).where(Thread.id == thread_id).limit(1))
                ).first()
            if thread is None:
                results.append(
                    {
                        "kind": "thread",
                        "threadId": thread_id,
                        "memberId": None,
                        "ok": False,
                        "circleMessageId": None,
                        "error": "Thread not found",
                    }
                )
                continue
            try:
                r = await send_draft(thread_id, body)
                entry: dict = {
                    "kind": "thread",
                    "threadId": thread_id,
                    "memberId": None,
                    "ok": r["ok"],
                    "circleMessageId": r["circleMessageId"],
                }
                if "error" in r:
                    entry["error"] = r["error"]
                results.append(entry)
            except Exception as err:
                results.append(
                    {
                        "kind": "thread",
                        "threadId": thread_id,
                        "memberId": None,
                        "ok": False,
                        "circleMessageId": None,
                        "error": str(err),
                    }
                )
            continue

        # kind == 'member': find-or-create chat roomu + wysylka przez compose-orchestrator.
        try:
            r = await send_compose_draft(item["adminAccountId"], item["memberId"], body)
            if r["ok"]:
                results.append(
                    {
                        "kind": "member",
                        "threadId": r["threadId"],
                        "memberId": item["memberId"],
                        "ok": True,
                        "circleMessageId": None,
                    }
                )
            else:
                results.append(
                    {
                        "kind": "member",
                        "threadId": None,
                        "memberId": item["memberId"],
                        "ok": False,
                        "circleMessageId": None,
                        "error": r["error"],
                    }
                )
        except Exception as err:
            results.append(
                {
                    "kind": "member",
                    "threadId": None,
                    "memberId": item["memberId"],
                    "ok": False,
                    "circleMessageId": None,
                    "error": str(err),
                }
            )

    return results
