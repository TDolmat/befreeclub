"""Port scripts/backfill-voice-transcripts.ts.

Oznacza istniejace wiadomosci z glosowka jako 'pending', worker zbierze je na
nastepnym ticku. Idempotentny: pomija wiersze ze statusem (done/error/pending),
chyba ze --force-errors, ktory retry'uje tez wiersze 'error'.

Usage:
  uv run python scripts/backfill_voice_transcripts.py             # dry-run preview
  uv run python scripts/backfill_voice_transcripts.py --apply     # write
  uv run python scripts/backfill_voice_transcripts.py --apply --force-errors
"""

import asyncio
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import or_, select, update  # noqa: E402

from app.core.db import async_session_maker, engine  # noqa: E402
from app.modules.circle_dm.circle.attachments import extract_attachments  # noqa: E402
from app.modules.circle_dm.models import Message  # noqa: E402

BATCH = 500


async def main() -> None:
    argv = sys.argv[1:]
    apply = "--apply" in argv
    force_errors = "--force-errors" in argv

    if force_errors:
        where = or_(
            Message.voice_transcript_status.is_(None),
            Message.voice_transcript_status == "error",
        )
    else:
        where = Message.voice_transcript_status.is_(None)

    async with async_session_maker() as session:
        rows = (
            await session.execute(
                select(
                    Message.id, Message.rich_text_body, Message.voice_transcript_status
                ).where(where)
            )
        ).all()

    to_mark: list[int] = []
    for r in rows:
        atts = extract_attachments(r.rich_text_body)
        if any(a.kind == "audio" and a.voice_message for a in atts):
            to_mark.append(r.id)

    print(f"Scanned {len(rows)} rows, found {len(to_mark)} with voice attachments.")
    if len(to_mark) == 0:
        return
    if not apply:
        print("Dry-run. Re-run with --apply to enqueue them as pending.")
        print("Sample ids:", to_mark[:10])
        return

    done = 0
    async with async_session_maker() as session:
        for i in range(0, len(to_mark), BATCH):
            chunk = to_mark[i : i + BATCH]
            await session.execute(
                update(Message)
                .where(Message.id.in_(chunk))
                .values(
                    voice_transcript_status="pending",
                    voice_transcript_error=None,
                    voice_transcript_attempts=0,
                )
            )
            await session.commit()
            done += len(chunk)
            print(f"  marked {done}/{len(to_mark)}")
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
