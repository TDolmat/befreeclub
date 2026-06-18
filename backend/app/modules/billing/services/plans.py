"""Serwis planow (billing.plans) - zrodlo prawdy o planach/cenach/price ID.

Zastepuje PRICE_MAP / PLAN_CONFIG / PLAN_INFO hardcodowane w 5 miejscach
oryginalu. Sygnatury zamrozone w port-kontrakt-2.md sekcja 3 - uzywaja ich
[billing-checkout], [billing-ebook], [billing-webhook], [admin-api].
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import async_session_maker
from app.modules.billing.models import Plan


async def get_by_slug(slug: str, *, session: AsyncSession | None = None) -> Plan | None:
    """Plan po slugu (quarterly|semiannual|annual|ebook). Takze nieaktywne -
    caller decyduje, czy 'active' jest wymagane (checkout TAK, webhook NIE,
    bo platnosc za wycofany plan dalej trzeba obsluzyc)."""
    stmt = select(Plan).where(Plan.slug == slug).limit(1)
    if session is not None:
        return (await session.execute(stmt)).scalar_one_or_none()
    async with async_session_maker() as own:
        return (await own.execute(stmt)).scalar_one_or_none()


async def list_active(*, session: AsyncSession | None = None) -> list[Plan]:
    """Aktywne plany w kolejnosci sort (publiczny cennik + checkout)."""
    stmt = select(Plan).where(Plan.active.is_(True)).order_by(Plan.sort, Plan.id)
    if session is not None:
        return list((await session.execute(stmt)).scalars().all())
    async with async_session_maker() as own:
        return list((await own.execute(stmt)).scalars().all())
