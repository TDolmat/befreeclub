import pytest
import stripe

from app.core import stripe_client
from app.core.config import settings
from app.core.stripe_client import (
    StripeAccount,
    StripeConfigError,
    configured_accounts,
    find_on_accounts,
    get_client,
    new_idempotency_key,
    request_options,
    webhook_secret_for,
)


@pytest.fixture(autouse=True)
def _reset_client_cache():
    stripe_client.reset_clients()
    yield
    stripe_client.reset_clients()


@pytest.fixture
def both_accounts(monkeypatch):
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", "sk_test_current")
    monkeypatch.setattr(settings, "STRIPE_LEGACY_SECRET_KEY", "sk_test_legacy")


def test_configured_accounts_order(both_accounts):
    assert configured_accounts() == [StripeAccount.CURRENT, StripeAccount.LEGACY]


def test_configured_accounts_only_current(monkeypatch):
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", "sk_test_current")
    monkeypatch.setattr(settings, "STRIPE_LEGACY_SECRET_KEY", None)
    assert configured_accounts() == [StripeAccount.CURRENT]


def test_get_client_caches_per_account(both_accounts):
    client = get_client(StripeAccount.CURRENT)
    assert isinstance(client, stripe.StripeClient)
    assert get_client(StripeAccount.CURRENT) is client
    assert get_client(StripeAccount.LEGACY) is not client


def test_get_client_missing_key_raises(monkeypatch):
    monkeypatch.setattr(settings, "STRIPE_LEGACY_SECRET_KEY", None)
    with pytest.raises(StripeConfigError, match="STRIPE_LEGACY_SECRET_KEY"):
        get_client(StripeAccount.LEGACY)


async def test_find_on_accounts_current_first(both_accounts):
    calls: list[StripeAccount] = []

    async def fn(account: StripeAccount, client: stripe.StripeClient) -> str | None:
        calls.append(account)
        return "hit" if account is StripeAccount.CURRENT else None

    assert await find_on_accounts(fn) == (StripeAccount.CURRENT, "hit")
    assert calls == [StripeAccount.CURRENT]


async def test_find_on_accounts_falls_back_to_legacy(both_accounts):
    calls: list[StripeAccount] = []

    async def fn(account: StripeAccount, client: stripe.StripeClient) -> str | None:
        calls.append(account)
        return "legacy-hit" if account is StripeAccount.LEGACY else None

    assert await find_on_accounts(fn) == (StripeAccount.LEGACY, "legacy-hit")
    assert calls == [StripeAccount.CURRENT, StripeAccount.LEGACY]


async def test_find_on_accounts_nothing_found(both_accounts):
    async def fn(account: StripeAccount, client: stripe.StripeClient) -> str | None:
        return None

    assert await find_on_accounts(fn) is None


async def test_find_on_accounts_no_accounts(monkeypatch):
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", None)
    monkeypatch.setattr(settings, "STRIPE_LEGACY_SECRET_KEY", None)

    async def fn(account: StripeAccount, client: stripe.StripeClient) -> str | None:
        raise AssertionError("nie powinno byc wolane")

    assert await find_on_accounts(fn) is None


def test_request_options():
    assert request_options() == {}
    assert request_options(idempotency_key=None) == {}
    assert request_options(idempotency_key="k1") == {"idempotency_key": "k1"}


def test_new_idempotency_key_prefix_and_uniqueness():
    a = new_idempotency_key("checkout-setup")
    b = new_idempotency_key("checkout-setup")
    assert a.startswith("checkout-setup-")
    assert a != b


def test_webhook_secret_for(monkeypatch):
    monkeypatch.setattr(settings, "STRIPE_WEBHOOK_SECRET", "whsec_current")
    monkeypatch.setattr(settings, "STRIPE_LEGACY_WEBHOOK_SECRET", "whsec_legacy")
    assert webhook_secret_for(StripeAccount.CURRENT) == "whsec_current"
    assert webhook_secret_for(StripeAccount.LEGACY) == "whsec_legacy"
