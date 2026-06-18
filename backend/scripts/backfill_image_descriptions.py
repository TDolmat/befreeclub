"""Port scripts/backfill-image-descriptions.ts.

Dosypuje wiersze 'pending' do message_image_descriptions dla istniejacych
wiadomosci z obrazkami. Idempotentny (UNIQUE message_id+attachment_index,
insert z ON CONFLICT DO NOTHING). Z --force-errors retry'uje tez wiersze 'error'
- UWAGA, zachowany quirk oryginalu: reset idzie po message_id, nie po parze
(message_id, attachment_index), wiec cofa do 'pending' WSZYSTKIE wiersze opisow
danej wiadomosci (takze 'done').

Usage:
  uv run python scripts/backfill_image_descriptions.py             # dry-run
  uv run python scripts/backfill_image_descriptions.py --apply     # write
  uv run python scripts/backfill_image_descriptions.py --apply --force-errors
"""

import asyncio
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select, update  # noqa: E402
from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: E402

from app.core.db import async_session_maker, engine  # noqa: E402
from app.modules.circle_dm.circle.attachments import extract_attachments  # noqa: E402
from app.modules.circle_dm.models import Message, MessageImageDescription  # noqa: E402

BATCH = 500


async def main() -> None:
    argv = sys.argv[1:]
    apply = "--apply" in argv
    force_errors = "--force-errors" in argv

    async with async_session_maker() as session:
        rows = (await session.execute(select(Message.id, Message.rich_text_body))).all()

    candidates: list[dict] = []
    for r in rows:
        atts = extract_attachments(r.rich_text_body)
        for idx, a in enumerate(atts):
            if a.kind == "image":
                candidates.append(
                    {
                        "message_id": r.id,
                        "attachment_index": idx,
                        "attachment_url": a.full_url if a.full_url is not None else a.url,
                    }
                )

    async with async_session_maker() as session:
        existing = (
            await session.execute(
                select(
                    MessageImageDescription.message_id,
                    MessageImageDescription.attachment_index,
                    MessageImageDescription.status,
                )
            )
        ).all()
    seen = {f"{e.message_id}:{e.attachment_index}" for e in existing}
    error_keys = {f"{e.message_id}:{e.attachment_index}" for e in existing if e.status == "error"}

    to_insert = [c for c in candidates if f"{c['message_id']}:{c['attachment_index']}" not in seen]
    to_retry = (
        [c for c in candidates if f"{c['message_id']}:{c['attachment_index']}" in error_keys]
        if force_errors
        else []
    )

    print(f"Scanned {len(rows)} messages, {len(candidates)} image attachments total.")
    print(f"New to insert: {len(to_insert)}")
    if force_errors:
        print(f"Errors to retry: {len(to_retry)}")
    if len(to_insert) == 0 and len(to_retry) == 0:
        return
    if not apply:
        print("Dry-run. Re-run with --apply to enqueue.")
        return

    async with async_session_maker() as session:
        for i in range(0, len(to_insert), BATCH):
            chunk = to_insert[i : i + BATCH]
            await session.execute(
                pg_insert(MessageImageDescription).values(chunk).on_conflict_do_nothing()
            )
            await session.commit()
            print(f"  inserted {min(i + BATCH, len(to_insert))}/{len(to_insert)}")
        if len(to_retry) > 0:
            ids = list(
                (
                    await session.execute(
                        select(MessageImageDescription.id).where(
                            MessageImageDescription.message_id.in_(
                                [c["message_id"] for c in to_retry]
                            )
                        )
                    )
                ).scalars()
            )
            for i in range(0, len(ids), BATCH):
                chunk = ids[i : i + BATCH]
                await session.execute(
                    update(MessageImageDescription)
                    .where(MessageImageDescription.id.in_(chunk))
                    .values(status="pending", error=None, attempts=0)
                )
                await session.commit()
            print(f"  reset {len(ids)} error rows to pending")
    print("Done. Worker will pick these up on next tick.")


async def _run() -> None:
    try:
        await main()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(_run())
        sys.exit(0)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
