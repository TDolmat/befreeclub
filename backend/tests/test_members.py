"""Modul members: provisioning, cleanup, retry zaproszen, sync, API admina.

Testy na realnym Postgresie (wlasna baza befreeclub_members_test, tworzona
i niszczona przez fixture) + respx dla Circle API. Stripe NIE jest tu
odpytywany - cleanup deleguje do billing.services.subscriptions
([billing-lifecycle]); testy podmieniaja cleanup._has_live_access.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import httpx
import pytest
import respx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.core.db import Base, get_session
from app.modules.members.models import Member, MemberEvent
from app.modules.members.services import circle, cleanup, maintenance, provisioning

TEST_DB_NAME = "befreeclub_members_test"
MEMBERS_URL = "https://app.circle.so/api/admin/v2/community_members"


def _dsn(db_name: str) -> str:
    auth = quote(settings.DB_USER, safe="")
    if settings.DB_PASS:
        auth += ":" + quote(settings.DB_PASS, safe="")
    return f"postgresql+asyncpg://{auth}@{settings.DB_HOST}:{settings.DB_PORT}/{db_name}"


async def _create_test_db() -> None:
    admin = create_async_engine(_dsn("postgres"), isolation_level="AUTOCOMMIT", poolclass=NullPool)
    async with admin.connect() as conn:
        await conn.execute(text(f"DROP DATABASE IF EXISTS {TEST_DB_NAME} WITH (FORCE)"))
        await conn.execute(text(f"CREATE DATABASE {TEST_DB_NAME}"))
    await admin.dispose()

    engine = create_async_engine(_dsn(TEST_DB_NAME), poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE SCHEMA members"))
        await conn.execute(
            text(
                "CREATE TYPE members.member_status AS ENUM ('invited', 'active', 'paused', "
                "'pending_removal', 'removed', 'invite_failed')"
            )
        )
        await conn.execute(
            text("CREATE TYPE members.member_source AS ENUM ('subscription', 'one_time', 'manual')")
        )
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn, tables=[Member.__table__, MemberEvent.__table__]
            )
        )
    await engine.dispose()


async def _drop_test_db() -> None:
    admin = create_async_engine(_dsn("postgres"), isolation_level="AUTOCOMMIT", poolclass=NullPool)
    async with admin.connect() as conn:
        await conn.execute(text(f"DROP DATABASE IF EXISTS {TEST_DB_NAME} WITH (FORCE)"))
    await admin.dispose()


@pytest.fixture(scope="session")
def members_db():
    asyncio.run(_create_test_db())
    yield
    asyncio.run(_drop_test_db())


@pytest.fixture
async def maker(members_db, monkeypatch):
    engine = create_async_engine(_dsn(TEST_DB_NAME), poolclass=NullPool)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE members.events, members.members RESTART IDENTITY"))
    for module in (provisioning, cleanup, maintenance):
        monkeypatch.setattr(module, "async_session_maker", session_maker)
    yield session_maker
    await engine.dispose()


@pytest.fixture(autouse=True)
def fast_sleep(monkeypatch):
    async def _noop(_seconds):
        return None

    monkeypatch.setattr(circle, "_sleep", _noop)


@pytest.fixture
def circle_configured(monkeypatch):
    monkeypatch.setattr(settings, "CIRCLE_API_TOKEN", "circle_test_token")
    monkeypatch.setattr(settings, "CIRCLE_COMMUNITY_ID", "123")


@pytest.fixture
def live_access(monkeypatch):
    """Podmienia odczyt Stripe (billing.services.subscriptions) w cleanupie.
    Ustawia tez oba klucze Stripe - guard konfiguracji run_cleanup (review
    2.1) wymaga ich PRZED przetworzeniem kogokolwiek."""
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", "sk_test_current")
    monkeypatch.setattr(settings, "STRIPE_LEGACY_SECRET_KEY", "sk_test_legacy")
    calls: list[str] = []
    result = {"value": False}

    async def _fake(email: str) -> bool:
        calls.append(email)
        return result["value"]

    monkeypatch.setattr(cleanup, "_has_live_access", _fake)
    return {"calls": calls, "result": result}


async def add_member(maker, **kwargs) -> Member:
    async with maker() as session:
        member = Member(**kwargs)
        session.add(member)
        await session.commit()
        await session.refresh(member)
        return member


async def get_member(maker, member_id: int) -> Member:
    async with maker() as session:
        return await session.get(Member, member_id)


async def get_events(maker, member_id: int) -> list[MemberEvent]:
    async with maker() as session:
        stmt = (
            select(MemberEvent)
            .where(MemberEvent.member_id == member_id)
            .order_by(MemberEvent.id)
        )
        return list((await session.execute(stmt)).scalars().all())


def _future(days: int = 30) -> datetime:
    return datetime.now(UTC) + timedelta(days=days)


def _past(days: int = 1) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


# ── provision ────────────────────────────────────────────────────────────────


@respx.mock
async def test_provision_new_member_invites_and_activates(maker, circle_configured):
    route = respx.post(MEMBERS_URL).mock(return_value=httpx.Response(200, json={"id": 777}))

    result = await provisioning.provision("  Jan@X.PL ", "Jan", source="subscription")

    assert result.circle_invited is True
    assert result.circle_member_id == "777"
    assert result.already_active is False
    assert route.call_count == 1

    member = await get_member(maker, result.member_id)
    assert member.email == "jan@x.pl"  # znormalizowany (CHECK w DB)
    assert member.status == "active"
    assert member.source == "subscription"
    assert member.circle_member_id == "777"
    events = await get_events(maker, member.id)
    assert [e.kind for e in events] == ["invited"]


@respx.mock
async def test_provision_invite_failure_sets_invite_failed(maker, circle_configured):
    respx.post(MEMBERS_URL).mock(return_value=httpx.Response(422, text="nope"))

    result = await provisioning.provision("jan@x.pl", None, source="subscription")

    assert result.circle_invited is False
    member = await get_member(maker, result.member_id)
    assert member.status == "invite_failed"
    assert member.circle_member_id is None
    events = await get_events(maker, member.id)
    assert [e.kind for e in events] == ["invite_failed"]


@respx.mock
async def test_provision_already_active_skips_invite_and_bumps_expires_up(
    maker, circle_configured
):
    # Brak mocka POST - kazde wywolanie Circle wywaliloby test.
    old_expires = _future(10)
    member = await add_member(
        maker,
        email="jan@x.pl",
        status="active",
        source="subscription",
        circle_member_id="5",
        expires_at=old_expires,
    )

    later = _future(400)
    result = await provisioning.provision("jan@x.pl", None, source="one_time", expires_at=later)
    assert result.already_active is True
    assert result.circle_invited is False
    assert result.member_id == member.id

    refreshed = await get_member(maker, member.id)
    assert refreshed.source == "one_time"
    assert refreshed.expires_at == later

    # expires_at TYLKO w gore: wczesniejsza data nie cofa dostepu
    await provisioning.provision("jan@x.pl", None, source="one_time", expires_at=_future(5))
    refreshed = await get_member(maker, member.id)
    assert refreshed.expires_at == later


@respx.mock
async def test_provision_concurrent_same_email_serialized(maker, circle_configured):
    """REGRESJA (review 2.1): webhook checkout.session.completed i browserowy
    confirm Klarny odpalaja provision rownolegle. pg_advisory_xact_lock
    serializuje per email: JEDEN invite do Circle, zero IntegrityError,
    przegrany widzi already_active."""
    route = respx.post(MEMBERS_URL).mock(return_value=httpx.Response(200, json={"id": 777}))

    results = await asyncio.gather(
        provisioning.provision("jan@x.pl", None, source="one_time", expires_at=_future(180)),
        provisioning.provision("jan@x.pl", None, source="one_time", expires_at=_future(180)),
    )

    assert route.call_count == 1  # drugi grant NIE robi drugiego invite
    assert sorted(r.already_active for r in results) == [False, True]
    assert results[0].member_id == results[1].member_id


@respx.mock
async def test_provision_keeps_one_time_source_until_expiry(maker, circle_configured):
    """Review 2.1: aktywny czlonek z oplacona Klarna (one_time, expires
    w przyszlosci) dokupujacy sube NIE traci source=one_time - inaczej
    cleanup wyrzucilby go w trakcie oplaconego okresu, gdy suba padnie.
    Po wygasnieciu okna degradacja JEST dozwolona (swiezy zakup suba)."""
    member = await add_member(
        maker,
        email="jan@x.pl",
        status="active",
        source="one_time",
        circle_member_id="5",
        expires_at=_future(100),
    )

    result = await provisioning.provision("jan@x.pl", None, source="subscription")
    assert result.already_active is True
    refreshed = await get_member(maker, member.id)
    assert refreshed.source == "one_time"  # NIE zdegradowane
    assert refreshed.expires_at is not None

    # Wygasly one_time + nowa suba = nadpisanie OK (czlonek zyje z suby).
    async with maker() as session:
        row = await session.get(Member, member.id)
        row.expires_at = _past()
        await session.commit()
    await provisioning.provision("jan@x.pl", None, source="subscription")
    assert (await get_member(maker, member.id)).source == "subscription"


@respx.mock
async def test_provision_already_active_no_change_no_event(maker, circle_configured):
    """Review 2.1: powtorny grant z tym samym terminem (np. tick reconcile
    co godzine) NIE dopisuje eventu "extended" - timeline bez szumu."""
    expires = _future(180)
    member = await add_member(
        maker,
        email="jan@x.pl",
        status="active",
        source="one_time",
        circle_member_id="5",
        expires_at=expires,
    )

    await provisioning.provision("jan@x.pl", None, source="one_time", expires_at=expires)
    await provisioning.provision("jan@x.pl", None, source="one_time", expires_at=expires)

    assert await get_events(maker, member.id) == []


async def test_set_pause_state_flips_active_and_paused(maker):
    member = await add_member(maker, email="jan@x.pl", status="active", source="subscription")

    assert await provisioning.set_pause_state("Jan@X.pl ", True, by="admin@x.pl") is True
    refreshed = await get_member(maker, member.id)
    assert refreshed.status == "paused"

    # idempotencja: drugi raz nic nie zmienia
    assert await provisioning.set_pause_state("jan@x.pl", True) is False

    assert await provisioning.set_pause_state("jan@x.pl", False, by="stripe-webhook") is True
    assert (await get_member(maker, member.id)).status == "active"

    events = await get_events(maker, member.id)
    assert [e.kind for e in events] == ["paused", "resumed"]
    assert events[0].detail == {"by": "admin@x.pl"}

    # brak czlonka / status nie-flipowalny = no-op False
    assert await provisioning.set_pause_state("ghost@x.pl", False) is False


async def test_cleanup_has_live_access_real_import_path(maker, monkeypatch):
    """Smoke test linii lazy importu w cleanup._has_live_access (review 2.1):
    bez patchowania samej funkcji - mockujemy dopiero
    subscriptions.find_subscriptions_by_email pod spodem."""
    from app.modules.billing.services import subscriptions as subscriptions_service

    async def fake_find(email, *, statuses=None, max_customers=100):
        return []

    monkeypatch.setattr(subscriptions_service, "find_subscriptions_by_email", fake_find)
    assert await cleanup._has_live_access("jan@x.pl") is False


@respx.mock
async def test_provision_reinvites_removed_member(maker, circle_configured):
    route = respx.post(MEMBERS_URL).mock(return_value=httpx.Response(200, json={"id": 99}))
    member = await add_member(
        maker, email="jan@x.pl", status="removed", source="subscription", circle_member_id="5"
    )

    result = await provisioning.provision("jan@x.pl", None, source="subscription")

    assert result.circle_invited is True
    assert route.call_count == 1
    refreshed = await get_member(maker, member.id)
    assert refreshed.status == "active"
    assert refreshed.circle_member_id == "99"


# ── schedule_removal / is_protected / set_protected / remove_member ─────────


async def test_schedule_removal(maker):
    member = await add_member(maker, email="jan@x.pl", status="active", source="subscription")
    assert await provisioning.schedule_removal("Jan@X.pl ", reason="refund") is True
    refreshed = await get_member(maker, member.id)
    assert refreshed.status == "pending_removal"
    events = await get_events(maker, member.id)
    assert events[-1].kind == "removal_scheduled"
    assert events[-1].detail == {"reason": "refund"}


async def test_schedule_removal_protected_and_missing(maker):
    member = await add_member(
        maker, email="vip@x.pl", status="active", source="subscription", protected=True
    )
    assert await provisioning.schedule_removal("vip@x.pl", reason="refund") is False
    assert (await get_member(maker, member.id)).status == "active"
    assert await provisioning.schedule_removal("ghost@x.pl", reason="refund") is False


async def test_is_protected(maker):
    await add_member(maker, email="vip@x.pl", status="active", protected=True)
    await add_member(maker, email="jan@x.pl", status="active")
    assert await provisioning.is_protected("VIP@x.pl") is True
    assert await provisioning.is_protected("jan@x.pl") is False
    assert await provisioning.is_protected("ghost@x.pl") is False


@respx.mock
async def test_remove_member_admin_action(maker, circle_configured):
    respx.delete(url__startswith=MEMBERS_URL).mock(return_value=httpx.Response(200, json={}))
    member = await add_member(
        maker, email="jan@x.pl", status="active", source="subscription", circle_member_id="7"
    )

    outcome = await provisioning.remove_member(member.id, reason="admin_delete", by="admin@x.pl")

    assert outcome.ok is True
    assert outcome.circle_removed is True
    refreshed = await get_member(maker, member.id)
    assert refreshed.status == "removed"
    events = await get_events(maker, member.id)
    assert events[-1].kind == "removed"
    assert events[-1].detail["by"] == "admin@x.pl"


async def test_remove_member_without_circle_id_marks_removed(maker):
    member = await add_member(maker, email="jan@x.pl", status="active", source="manual")
    outcome = await provisioning.remove_member(member.id, reason="admin_delete")
    assert outcome.ok is True
    assert outcome.circle_removed is False
    assert (await get_member(maker, member.id)).status == "removed"


async def test_remove_member_protected_refused(maker):
    member = await add_member(maker, email="vip@x.pl", status="active", protected=True)
    outcome = await provisioning.remove_member(member.id, reason="admin_delete")
    assert outcome.ok is False
    assert outcome.code == "protected"
    assert (await get_member(maker, member.id)).status == "active"


# ── cleanup ──────────────────────────────────────────────────────────────────


@respx.mock
async def test_cleanup_one_time_expired_removed(maker, circle_configured, live_access):
    delete_route = respx.delete(url__startswith=MEMBERS_URL).mock(
        return_value=httpx.Response(200, json={})
    )
    member = await add_member(
        maker,
        email="jan@x.pl",
        status="active",
        source="one_time",
        circle_member_id="7",
        expires_at=_past(),
    )

    result = await cleanup.run_cleanup()

    assert result.checked == 1
    assert result.removed == 1
    assert result.decisions[0].decision == "one_time_expired"
    assert delete_route.call_count == 1
    # Review 2.1: przed usunieciem wygaslego one_time pytamy Stripe,
    # czy email nie ma zywej suby (tu: nie ma -> usuniecie).
    assert live_access["calls"] == ["jan@x.pl"]
    refreshed = await get_member(maker, member.id)
    assert refreshed.status == "removed"
    events = await get_events(maker, member.id)
    assert events[-1].kind == "removed"
    assert events[-1].detail["by"] == "cleanup"


@respx.mock
async def test_cleanup_one_time_expired_with_live_sub_kept(maker, circle_configured, live_access):
    """REGRESJA (review 2.1, HIGH): aktywny subskrybent, ktoremu zakup
    w torze Klarna nadpisal source na one_time, NIE wylatuje z Circle po
    wygasnieciu okna Klarny, dopoki jego suba zyje i placi."""
    live_access["result"]["value"] = True
    member = await add_member(
        maker,
        email="subklarna@x.pl",
        status="active",
        source="one_time",
        circle_member_id="7",
        expires_at=_past(),
    )

    result = await cleanup.run_cleanup()

    assert result.removed == 0
    assert result.decisions[0].decision == "one_time_expired_live_sub_keep"
    assert live_access["calls"] == ["subklarna@x.pl"]
    assert (await get_member(maker, member.id)).status == "active"


async def test_cleanup_aborts_without_stripe_keys(maker, circle_configured, monkeypatch):
    """REGRESJA (review 2.1, BLOCKER): brak ktoregokolwiek klucza Stripe =
    abort przebiegu PRZED przetworzeniem kogokolwiek (jak throw oryginalu).
    Bez guarda deploy bez klucza legacy wyrzucalby wszystkich czlonkow
    z suba tylko na legacy (cichy skip konta w configured_accounts)."""
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", "sk_test_current")
    monkeypatch.setattr(settings, "STRIPE_LEGACY_SECRET_KEY", None)
    member = await add_member(
        maker, email="legacyonly@x.pl", status="active", source="subscription",
        circle_member_id="7",
    )

    with pytest.raises(cleanup.CleanupConfigError, match="STRIPE_LEGACY_SECRET_KEY"):
        await cleanup.run_cleanup()

    assert (await get_member(maker, member.id)).status == "active"
    assert await get_events(maker, member.id) == []


async def test_cleanup_aborts_without_circle_config(maker, monkeypatch):
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", "sk_test_current")
    monkeypatch.setattr(settings, "STRIPE_LEGACY_SECRET_KEY", "sk_test_legacy")
    monkeypatch.setattr(settings, "CIRCLE_API_TOKEN", None)
    with pytest.raises(cleanup.CleanupConfigError, match="CIRCLE_API_TOKEN"):
        await cleanup.run_cleanup()


@respx.mock
async def test_cleanup_one_time_still_valid_kept(maker, circle_configured, live_access):
    member = await add_member(
        maker,
        email="jan@x.pl",
        status="active",
        source="one_time",
        circle_member_id="7",
        expires_at=_future(),
    )
    result = await cleanup.run_cleanup()
    assert result.removed == 0
    assert result.decisions[0].decision == "one_time_active_keep"
    assert (await get_member(maker, member.id)).status == "active"


@respx.mock
async def test_cleanup_subscription_live_on_legacy_kept(maker, circle_configured, live_access):
    # has_live_access zwraca True = zywa suba na KTORYMKOLWIEK koncie
    # (np. tylko legacy) - czlonek zostaje, zero DELETE do Circle.
    live_access["result"]["value"] = True
    member = await add_member(
        maker, email="jan@x.pl", status="active", source="subscription", circle_member_id="7"
    )

    result = await cleanup.run_cleanup()

    assert result.removed == 0
    assert result.decisions[0].decision == "subscription_live_keep"
    assert live_access["calls"] == ["jan@x.pl"]
    assert (await get_member(maker, member.id)).status == "active"


@respx.mock
async def test_cleanup_subscription_dead_removed(maker, circle_configured, live_access):
    respx.delete(url__startswith=MEMBERS_URL).mock(return_value=httpx.Response(200, json={}))
    member = await add_member(
        maker, email="jan@x.pl", status="active", source="subscription", circle_member_id="7"
    )
    result = await cleanup.run_cleanup()
    assert result.removed == 1
    assert result.decisions[0].decision == "subscription_dead"
    assert (await get_member(maker, member.id)).status == "removed"


@respx.mock
async def test_cleanup_protected_skipped(maker, circle_configured, live_access):
    member = await add_member(
        maker,
        email="vip@x.pl",
        status="active",
        source="one_time",
        circle_member_id="7",
        expires_at=_past(),
        protected=True,
    )
    result = await cleanup.run_cleanup()
    assert result.removed == 0
    assert result.decisions[0].decision == "protected_skip"
    assert live_access["calls"] == []
    refreshed = await get_member(maker, member.id)
    assert refreshed.status == "active"
    # Review 2.1: czysta decyzja keep NIE zasmieca timeline'u (tylko logger).
    assert await get_events(maker, member.id) == []


@respx.mock
async def test_cleanup_manual_without_expires_skipped(maker, circle_configured, live_access):
    # Naprawa quirka: reczny dostep bez expires_at NIE jest weryfikowany
    # w Stripe i nie jest wywalany.
    member = await add_member(
        maker, email="manual@x.pl", status="active", source="manual", circle_member_id="7"
    )
    result = await cleanup.run_cleanup()
    assert result.removed == 0
    assert result.decisions[0].decision == "manual_no_expires_skip"
    assert live_access["calls"] == []
    assert (await get_member(maker, member.id)).status == "active"


@respx.mock
async def test_cleanup_manual_with_expires_expires_like_one_time(
    maker, circle_configured, live_access
):
    respx.delete(url__startswith=MEMBERS_URL).mock(return_value=httpx.Response(200, json={}))
    member = await add_member(
        maker,
        email="manual@x.pl",
        status="active",
        source="manual",
        circle_member_id="7",
        expires_at=_past(),
    )
    result = await cleanup.run_cleanup()
    assert result.removed == 1
    assert result.decisions[0].decision == "one_time_expired"
    # Review 2.1: wygasly manual tez sprawdza zywa sube przed usunieciem
    # (przywrocona semantyka oryginalu, ktory dla manual pytal Stripe).
    assert live_access["calls"] == ["manual@x.pl"]
    assert (await get_member(maker, member.id)).status == "removed"


@respx.mock
async def test_cleanup_pending_removal_removed_without_stripe(
    maker, circle_configured, live_access
):
    respx.delete(url__startswith=MEMBERS_URL).mock(return_value=httpx.Response(200, json={}))
    member = await add_member(
        maker,
        email="refund@x.pl",
        status="pending_removal",
        source="subscription",
        circle_member_id="7",
    )
    result = await cleanup.run_cleanup()
    assert result.removed == 1
    assert result.decisions[0].decision == "pending_removal"
    assert live_access["calls"] == []  # decyzja juz zapadla przy schedule_removal
    assert (await get_member(maker, member.id)).status == "removed"


@respx.mock
async def test_cleanup_no_circle_id_left_for_sync(maker, circle_configured, live_access):
    # 1:1 z oryginalem: bez circle_member_id nie ma jak usunac - wiersz
    # zostaje (sync-circle-ids uzupelni ID).
    member = await add_member(
        maker, email="zombie@x.pl", status="active", source="subscription", circle_member_id=None
    )
    result = await cleanup.run_cleanup()
    assert result.removed == 0
    assert result.decisions[0].decision == "subscription_dead:no_circle_member_id"
    assert (await get_member(maker, member.id)).status == "active"


@respx.mock
async def test_cleanup_circle_failure_leaves_member_for_retry(
    maker, circle_configured, live_access
):
    respx.delete(url__startswith=MEMBERS_URL).mock(return_value=httpx.Response(500, text="boom"))
    member = await add_member(
        maker,
        email="jan@x.pl",
        status="active",
        source="one_time",
        circle_member_id="7",
        expires_at=_past(),
    )
    result = await cleanup.run_cleanup()
    assert result.removed == 0
    assert result.decisions[0].decision == "one_time_expired:remove_failed"
    assert (await get_member(maker, member.id)).status == "active"


@respx.mock
async def test_cleanup_ignores_removed_and_invite_failed(maker, circle_configured, live_access):
    await add_member(maker, email="gone@x.pl", status="removed", source="subscription")
    await add_member(maker, email="failed@x.pl", status="invite_failed", source="subscription")
    result = await cleanup.run_cleanup()
    assert result.checked == 0


# ── retry_failed_invites ─────────────────────────────────────────────────────


@respx.mock
async def test_retry_only_touches_invite_failed_never_removed(maker, circle_configured):
    route = respx.post(MEMBERS_URL).mock(return_value=httpx.Response(200, json={"id": 555}))
    failed = await add_member(
        maker, email="failed@x.pl", status="invite_failed", source="subscription"
    )
    removed = await add_member(
        maker, email="removed@x.pl", status="removed", source="subscription"
    )
    no_id = await add_member(
        # Oryginal lapal tez circle_member_id IS NULL - nowy retry NIE.
        maker,
        email="noid@x.pl",
        status="active",
        source="subscription",
        circle_member_id=None,
    )

    results = await maintenance.retry_failed_invites()

    assert [r.email for r in results] == ["failed@x.pl"]
    assert results[0].success is True
    assert results[0].circle_member_id == "555"
    assert route.call_count == 1  # JEDEN invite - usunieci/aktywni nietknieci

    assert (await get_member(maker, failed.id)).status == "active"
    assert (await get_member(maker, failed.id)).circle_member_id == "555"
    assert (await get_member(maker, removed.id)).status == "removed"
    assert (await get_member(maker, no_id.id)).circle_member_id is None


@respx.mock
async def test_retry_failure_keeps_invite_failed(maker, circle_configured):
    respx.post(MEMBERS_URL).mock(return_value=httpx.Response(500, text="boom"))
    member = await add_member(
        maker, email="failed@x.pl", status="invite_failed", source="subscription"
    )
    results = await maintenance.retry_failed_invites()
    assert results[0].success is False
    assert "500" in (results[0].error or "")
    assert (await get_member(maker, member.id)).status == "invite_failed"


# ── sync_circle_ids ──────────────────────────────────────────────────────────


@respx.mock
async def test_sync_circle_ids_fills_missing_ids(maker, circle_configured):
    respx.get(url__startswith=MEMBERS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "records": [
                    {"id": 11, "email": "Jan@X.pl"},
                    {"id": 22, "email": "anna@x.pl"},
                ],
                "has_next_page": False,
            },
        )
    )
    jan = await add_member(
        maker, email="jan@x.pl", status="active", source="subscription", circle_member_id=None
    )
    ghost = await add_member(
        maker, email="ghost@x.pl", status="active", source="subscription", circle_member_id=None
    )
    has_id = await add_member(
        maker, email="anna@x.pl", status="active", source="subscription", circle_member_id="22"
    )

    result = await maintenance.sync_circle_ids()

    assert result.circle_total == 2
    assert result.found == 1
    assert result.not_found == 1
    assert result.not_found_emails == ["ghost@x.pl"]
    assert (await get_member(maker, jan.id)).circle_member_id == "11"
    assert (await get_member(maker, ghost.id)).circle_member_id is None
    assert (await get_member(maker, has_id.id)).circle_member_id == "22"


# ── API admina (/api/members) ────────────────────────────────────────────────


@pytest.fixture
async def client(maker, monkeypatch):
    monkeypatch.setattr(settings, "NODE_ENV", "development")  # require_auth -> DEV_FAKE_AUTH
    from app.main import create_app

    app = create_app()

    async def _override():
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _override
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def test_list_members_with_filters(maker, client):
    await add_member(maker, email="a@x.pl", status="active", source="subscription")
    await add_member(maker, email="b@x.pl", status="removed", source="one_time")
    await add_member(maker, email="c@x.pl", status="active", source="manual", protected=True)

    response = await client.get("/api/members")
    assert response.status_code == 200
    members = response.json()["members"]
    assert {m["email"] for m in members} == {"a@x.pl", "b@x.pl", "c@x.pl"}
    assert {"id", "email", "name", "circleMemberId", "status", "protected", "source",
            "expiresAt", "createdAt", "updatedAt"} <= set(members[0].keys())

    response = await client.get("/api/members", params={"status": "active"})
    assert {m["email"] for m in response.json()["members"]} == {"a@x.pl", "c@x.pl"}

    response = await client.get("/api/members", params={"source": "manual"})
    assert {m["email"] for m in response.json()["members"]} == {"c@x.pl"}

    response = await client.get("/api/members", params={"protected": "true"})
    assert {m["email"] for m in response.json()["members"]} == {"c@x.pl"}

    response = await client.get("/api/members", params={"status": "bogus"})
    assert response.status_code == 400


@respx.mock
async def test_manual_provisioning_endpoint(maker, client, circle_configured):
    respx.post(MEMBERS_URL).mock(return_value=httpx.Response(200, json={"id": 31}))

    response = await client.post(
        "/api/members",
        json={"email": " Nowy@X.pl", "name": "Nowy", "protected": True},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "nowy@x.pl"
    assert body["circleInvited"] is True
    assert body["circleMemberId"] == "31"

    member = await get_member(maker, body["memberId"])
    assert member.source == "manual"
    assert member.protected is True
    assert member.expires_at is None  # bez expires_at = cleanup go pomija


async def test_manual_provisioning_requires_email(client):
    response = await client.post("/api/members", json={"email": "   "})
    assert response.status_code == 400
    assert response.json() == {"error": "email required"}


@respx.mock
async def test_reinvite_endpoint_keeps_source(maker, client, circle_configured):
    route = respx.post(MEMBERS_URL).mock(return_value=httpx.Response(200, json={"id": 41}))
    member = await add_member(
        maker, email="jan@x.pl", status="removed", source="subscription", circle_member_id=None
    )

    response = await client.post(f"/api/members/{member.id}/reinvite", json={})

    assert response.status_code == 200
    assert response.json()["circleInvited"] is True
    # Default skip_invitation=True jak w UI oryginalnego /admin.
    assert json.loads(route.calls.last.request.content)["skip_invitation"] is True
    refreshed = await get_member(maker, member.id)
    assert refreshed.status == "active"
    assert refreshed.source == "subscription"  # source NIE przeskakuje na manual

    response = await client.post("/api/members/999999/reinvite", json={})
    assert response.status_code == 404


async def test_protect_endpoint(maker, client):
    member = await add_member(maker, email="jan@x.pl", status="active", source="subscription")
    response = await client.post(f"/api/members/{member.id}/protect", json={"protected": True})
    assert response.status_code == 200
    assert response.json()["protected"] is True
    assert (await get_member(maker, member.id)).protected is True

    response = await client.post(f"/api/members/{member.id}/protect", json={"protected": False})
    assert response.json()["protected"] is False


@respx.mock
async def test_delete_endpoint(maker, client, circle_configured):
    respx.delete(url__startswith=MEMBERS_URL).mock(return_value=httpx.Response(200, json={}))
    member = await add_member(
        maker, email="jan@x.pl", status="active", source="subscription", circle_member_id="7"
    )
    vip = await add_member(maker, email="vip@x.pl", status="active", protected=True)

    response = await client.delete(f"/api/members/{member.id}")
    assert response.status_code == 200
    assert response.json() == {"success": True, "circleRemoved": True}
    assert (await get_member(maker, member.id)).status == "removed"

    response = await client.delete(f"/api/members/{vip.id}")
    assert response.status_code == 409
    assert response.json() == {"error": "Member is protected"}

    response = await client.delete("/api/members/999999")
    assert response.status_code == 404
