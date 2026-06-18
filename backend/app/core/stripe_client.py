"""Wrapper na DWA konta Stripe: current (STRIPE_SECRET_KEY) i legacy
(STRIPE_LEGACY_SECRET_KEY).

Konwencje (docs/spec-landing/port-kontrakt-2.md):
- Uzywamy WYLACZNIE metod async oficjalnego SDK przez namespace v1:
  `await client.v1.customers.list_async(params={...})`. Stary namespace
  (`client.customers`) jest deprecated w stripe>=15.
- apiVersion przypiety do "2025-08-27.basil" - ta sama wersja co wszystkie
  edge functions oryginalu (pulapki basil: current_period_end na items[],
  invoice.parent.subscription_details).
- Kazda operacja "znajdz po emailu" musi przeszukac OBA konta w kolejnosci
  current, legacy - do tego jest find_on_accounts().
- Create'y w Stripe MAJA dostawac Idempotency-Key (naprawa #6 z PLAN_LANDING):
  `options=request_options(idempotency_key=...)`.
- Webhooki: kazde konto ma WLASNY signing secret (webhook_secret_for).
"""

import uuid
from collections.abc import Awaitable, Callable
from enum import StrEnum

import stripe

from app.core.config import settings

STRIPE_API_VERSION = "2025-08-27.basil"


class StripeAccount(StrEnum):
    CURRENT = "current"
    LEGACY = "legacy"


class StripeConfigError(Exception):
    """Brak klucza API dla zadanego konta Stripe."""


_clients: dict[StripeAccount, stripe.StripeClient] = {}


def _api_key_for(account: StripeAccount) -> str | None:
    if account is StripeAccount.CURRENT:
        return settings.STRIPE_SECRET_KEY
    return settings.STRIPE_LEGACY_SECRET_KEY


def webhook_secret_for(account: StripeAccount) -> str | None:
    """Signing secret webhooka danego konta (rozne sekrety per endpoint)."""
    if account is StripeAccount.CURRENT:
        return settings.STRIPE_WEBHOOK_SECRET
    return settings.STRIPE_LEGACY_WEBHOOK_SECRET


def is_configured(account: StripeAccount) -> bool:
    return _api_key_for(account) is not None


def configured_accounts() -> list[StripeAccount]:
    """Skonfigurowane konta w kolejnosci wyszukiwania: current, potem legacy."""
    return [a for a in (StripeAccount.CURRENT, StripeAccount.LEGACY) if is_configured(a)]


def get_client(account: StripeAccount) -> stripe.StripeClient:
    """Klient Stripe dla konta. Lazy singleton per konto (HTTPXClient pod spodem)."""
    client = _clients.get(account)
    if client is not None:
        return client
    api_key = _api_key_for(account)
    if api_key is None:
        env_name = (
            "STRIPE_SECRET_KEY" if account is StripeAccount.CURRENT else "STRIPE_LEGACY_SECRET_KEY"
        )
        raise StripeConfigError(f"{env_name} is not configured")
    client = stripe.StripeClient(
        api_key,
        stripe_version=STRIPE_API_VERSION,
        http_client=stripe.HTTPXClient(allow_sync_methods=True),
    )
    _clients[account] = client
    return client


def reset_clients() -> None:
    """Czysci cache klientow (testy / rotacja kluczy)."""
    _clients.clear()


async def find_on_accounts[T](
    fn: Callable[[StripeAccount, stripe.StripeClient], Awaitable[T | None]],
) -> tuple[StripeAccount, T] | None:
    """Woła fn kolejno na koncie current, potem legacy; pierwszy nie-None wygrywa.

    Wzorzec "spytaj current, potem legacy" z oryginalu (request-cancellation,
    pause, cleanup). fn zwraca None = "nie znaleziono na tym koncie".
    """
    for account in configured_accounts():
        result = await fn(account, get_client(account))
        if result is not None:
            return account, result
    return None


async def search_customers_by_email(
    client: stripe.StripeClient, email: str, *, limit: int = 100
) -> list:
    """Customerzy po emailu: customers.list + fallback customers.search.

    Filtr `email` w customers.list jest CASE-SENSITIVE (kontrakt Stripe),
    a oryginal tworzyl customerow z emailem jak wpisal user - czlonkowie
    z wielka litera w emailu nie wpadliby w list po znormalizowanym emailu
    (ryzyko duplikatu customera i DRUGIEJ pelnoplatnej suby). Fallback:
    customers.search `email:'...'` jest exact-match case-INSENSITIVE; lag
    indeksu (do ~1 min) nie szkodzi, bo fallback ma znajdowac STARYCH
    customerow. Bledy Stripe propagują (fail-closed).
    """
    listed = await client.v1.customers.list_async(params={"email": email, "limit": limit})
    if listed.data:
        return list(listed.data)
    escaped = email.replace("\\", "\\\\").replace("'", "\\'")
    found = await client.v1.customers.search_async(
        params={"query": f"email:'{escaped}'", "limit": min(limit, 100)}
    )
    return list(found.data)


def request_options(*, idempotency_key: str | None = None) -> dict:
    """RequestOptions do create'ow: client.v1.x.create_async(params=..., options=...)."""
    options: dict = {}
    if idempotency_key:
        options["idempotency_key"] = idempotency_key
    return options


def new_idempotency_key(prefix: str) -> str:
    """Idempotency-Key dla create'ow bez naturalnego klucza (np. 'checkout-setup')."""
    return f"{prefix}-{uuid.uuid4()}"
