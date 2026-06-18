"""WSPOLNE wyszukiwanie subskrypcji po emailu na OBU kontach Stripe.

Jedna implementacja zamiast 5 niespojnych z oryginalu (request-cancellation
100 customerow / admin-pause limit 1 / cleanup limit 1 / admin-extend 10 -
prowizorka #10 ze speca billing-lifecycle.md). Sygnatury ZAMROZONE
w port-kontrakt-2.md sekcja 4 - uzywaja ich [billing-lifecycle] (anulowania,
pauzy, zmiana karty), members.cleanup (has_live_access) i [billing-webhook].

grant_one_time_access (Klarna) zaimplementowal [billing-checkout]
w services/klarna_grant.py - tu re-export, zeby import z kontraktu
(billing.services.subscriptions) tez dzialal.
"""

import time
from dataclasses import dataclass
from typing import Any

from app.core.email import normalize_email
from app.core.stripe_client import (
    StripeAccount,
    configured_accounts,
    get_client,
    search_customers_by_email,
)
from app.modules.billing.services.klarna_grant import grant_one_time_access

__all__ = [
    "CANCELLABLE_STATUSES",
    "KEEP_STATUSES",
    "FoundSubscription",
    "find_subscriptions_by_email",
    "has_live_access",
    "grant_one_time_access",
    "period_end_ts",
    "obj_get",
]

# Statusy "da sie anulowac/spauzowac" - 1:1 z CANCELLABLE_STATUSES oryginalu.
CANCELLABLE_STATUSES = {"active", "trialing", "past_due", "unpaid"}
# Statusy trzymajace dostep (circle-cleanup): stany odzyskiwalne nie wywalaja.
KEEP_STATUSES = {"active", "trialing", "past_due", "unpaid", "incomplete", "paused"}


@dataclass
class FoundSubscription:
    account: StripeAccount
    customer_id: str
    subscription: Any  # surowy obiekt SDK (StripeObject)


def obj_get(obj: Any, key: str, default: Any = None) -> Any:
    """Bezpieczny odczyt pola StripeObject (v15 nie jest dict, brak .get())."""
    if obj is None:
        return default
    try:
        return obj[key]
    except (KeyError, TypeError):
        return default


def _is_ts(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def period_end_ts(sub: Any, *, include_cancel_at: bool = False) -> int | None:
    """Koniec oplaconego okresu suba (unix s). Pulapka basil API: pole
    przeniesione na items[0].current_period_end, fallback na stare pole suba;
    include_cancel_at dorzuca trzeci fallback z confirm-cancellation."""
    items = obj_get(obj_get(sub, "items"), "data") or []
    ts = obj_get(items[0], "current_period_end") if items else None
    if _is_ts(ts):
        return int(ts)
    ts = obj_get(sub, "current_period_end")
    if _is_ts(ts):
        return int(ts)
    if include_cancel_at:
        ts = obj_get(sub, "cancel_at")
        if _is_ts(ts):
            return int(ts)
    return None


async def find_subscriptions_by_email(
    email: str,
    *,
    statuses: set[str] | None = None,
    max_customers: int = 100,
) -> list[FoundSubscription]:
    """OBA konta (current -> legacy), WSZYSCY customerzy po emailu, wszystkie
    suby (status=all, limit 100). statuses=None = bez filtra. Lookup customera
    z fallbackiem customers.search (case-insensitive) - naprawa review 2.1:
    legacy customerzy z wielka litera w emailu tez sa znajdowani."""
    email = normalize_email(email)
    found: list[FoundSubscription] = []
    for account in configured_accounts():
        client = get_client(account)
        customers = await search_customers_by_email(client, email, limit=max_customers)
        for customer in customers:
            subs = await client.v1.subscriptions.list_async(
                params={"customer": customer.id, "status": "all", "limit": 100}
            )
            for sub in subs.data:
                if statuses is None or obj_get(sub, "status") in statuses:
                    found.append(
                        FoundSubscription(
                            account=account, customer_id=customer.id, subscription=sub
                        )
                    )
    return found


async def has_live_access(email: str) -> bool:
    """Logika KEEP z circle-cleanup: zywa suba w KEEP_STATUSES na ktorymkolwiek
    koncie ALBO canceled z oplaconym okresem (current_period_end) w przyszlosci.
    Naprawa prowizorki #10: pelne wyszukiwanie (100 customerow) zamiast limit 1
    z oryginalu (ryzyko falszywego usuniecia przy duplikatach customerow)."""
    now = time.time()
    for item in await find_subscriptions_by_email(email):
        status = obj_get(item.subscription, "status")
        if status in KEEP_STATUSES:
            return True
        if status == "canceled":
            ts = period_end_ts(item.subscription)
            if ts is not None and ts > now:
                return True
    return False
