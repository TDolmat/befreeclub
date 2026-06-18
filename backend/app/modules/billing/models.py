"""Tabele schematu PG "billing" (faza 2: landing befreeclub.pl).

Nowe tabele (plans, webhook_events, checkout_attributions, audit_log) +
porty z Supabase (ebook_orders, ebook_download_tokens, cancellation_reasons)
wg docs/spec-landing/db-schema.md z porzadkami: enumy zamiast golego text,
UNIQUE na stripe_payment_intent_id, NOT NULL gdzie sie da.

Konwencje: porty z Supabase zachowuja uuid PK (latwiejsza migracja danych),
nowe tabele bigserial jak w fazie 1. updated_at ustawia KOD (bez triggerow).
"""

import uuid as uuid_mod
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ENUM, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

stripe_account_enum = ENUM(
    "current", "legacy", name="stripe_account", schema="billing", create_type=False
)
plan_interval_enum = ENUM(
    "month",
    "quarter",
    "half_year",
    "year",
    "one_time",
    name="plan_interval",
    schema="billing",
    create_type=False,
)
attribution_kind_enum = ENUM(
    "subscription", "klarna", "ebook", name="attribution_kind", schema="billing", create_type=False
)
ebook_order_status_enum = ENUM(
    "pending", "paid", "refunded", name="ebook_order_status", schema="billing", create_type=False
)
cancellation_action_enum = ENUM(
    "cancelled", "frozen", name="cancellation_action", schema="billing", create_type=False
)


class Plan(Base):
    """Plany sprzedazowe - koniec z hardcode price ID/kwot w 5 miejscach."""

    __tablename__ = "plans"
    __table_args__ = (
        UniqueConstraint("slug", name="plans_slug_unique"),
        Index("idx_plans_active_sort", "active", "sort"),
        {"schema": "billing"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(Text, nullable=False)  # quarterly|semiannual|annual|ebook
    name: Mapped[str] = mapped_column(Text, nullable=False)  # nazwa marketingowa (Starter...)
    stripe_price_id: Mapped[str] = mapped_column(Text, nullable=False)
    stripe_account: Mapped[str] = mapped_column(
        stripe_account_enum, nullable=False, server_default=text("'current'")
    )
    amount_pln: Mapped[int] = mapped_column(Integer, nullable=False)  # GROSZE
    interval: Mapped[str] = mapped_column(plan_interval_enum, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    sort: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class WebhookEvent(Base):
    """Idempotencja + historia webhookow Stripe (naprawa #1: event_id UNIQUE).

    processed_at NULL = odebrany, nieprzetworzony; error = czemu obsluga padla.
    Panel "komu cos nie przeszlo i czemu" czyta z tej tabeli, nie ze Stripe.
    """

    __tablename__ = "webhook_events"
    __table_args__ = (
        UniqueConstraint("event_id", name="webhook_events_event_id_unique"),
        Index("idx_webhook_events_created", "created_at"),
        Index(
            "idx_webhook_events_unprocessed",
            "created_at",
            postgresql_where=text("processed_at IS NULL"),
        ),
        {"schema": "billing"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    stripe_account: Mapped[str] = mapped_column(stripe_account_enum, nullable=False)
    event_id: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class CheckoutAttribution(Base):
    """Atrybucja UTM/Meta per checkout (twarde wymaganie analityki).

    Wiersz powstaje przy KAZDYM utworzeniu obiektu platnosci w Stripe
    (SetupIntent suba, Checkout Session Klarny, PaymentIntent ebooka).
    stripe_object_id laczy atrybucje z eventem webhooka przy strzale CAPI.
    """

    __tablename__ = "checkout_attributions"
    __table_args__ = (
        Index("idx_checkout_attributions_object", "stripe_object_id"),
        Index("idx_checkout_attributions_email", "email"),
        Index("idx_checkout_attributions_created", "created_at"),
        {"schema": "billing"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(attribution_kind_enum, nullable=False)
    email: Mapped[str | None] = mapped_column(Text, nullable=True)  # znormalizowany; Klarna: NULL
    stripe_object_id: Mapped[str] = mapped_column(Text, nullable=False)  # seti_|cs_|pi_
    utm_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    utm_medium: Mapped[str | None] = mapped_column(Text, nullable=True)
    utm_campaign: Mapped[str | None] = mapped_column(Text, nullable=True)
    utm_term: Mapped[str | None] = mapped_column(Text, nullable=True)
    utm_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    fbclid: Mapped[str | None] = mapped_column(Text, nullable=True)
    fbp: Mapped[str | None] = mapped_column(Text, nullable=True)
    fbc: Mapped[str | None] = mapped_column(Text, nullable=True)
    referrer: Mapped[str | None] = mapped_column(Text, nullable=True)
    landing_page: Mapped[str | None] = mapped_column(Text, nullable=True)
    client_ip: Mapped[str | None] = mapped_column(Text, nullable=True)
    client_ua: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class EbookOrder(Base):
    """Port public.ebook_orders. Porzadki: UNIQUE stripe_payment_intent_id,
    NOT NULL na amount_paid/currency/wants_invoice, status jako enum
    (+ 'refunded' - refund unieważnia tokeny, naprawa #3)."""

    __tablename__ = "ebook_orders"
    __table_args__ = (
        UniqueConstraint("stripe_session_id", name="ebook_orders_stripe_session_id_unique"),
        UniqueConstraint(
            "stripe_payment_intent_id", name="ebook_orders_stripe_payment_intent_id_unique"
        ),
        Index("idx_ebook_orders_email", "email"),
        {"schema": "billing"},
    )

    id: Mapped[uuid_mod.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    email: Mapped[str] = mapped_column(Text, nullable=False)  # znormalizowany
    stripe_session_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    stripe_payment_intent_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    amount_paid: Mapped[int] = mapped_column(Integer, nullable=False)  # grosze
    currency: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pln'"))
    status: Mapped[str] = mapped_column(
        ebook_order_status_enum, nullable=False, server_default=text("'pending'")
    )
    wants_invoice: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    nip: Mapped[str | None] = mapped_column(Text, nullable=True)
    invoice_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    email_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EbookDownloadToken(Base):
    """Port public.ebook_download_tokens + revoked_at (unieważnienie po refundzie)."""

    __tablename__ = "ebook_download_tokens"
    __table_args__ = (
        UniqueConstraint("token", name="ebook_download_tokens_token_unique"),
        Index("idx_ebook_download_tokens_order", "order_id"),
        {"schema": "billing"},
    )

    id: Mapped[uuid_mod.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    order_id: Mapped[uuid_mod.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "billing.ebook_orders.id",
            ondelete="CASCADE",
            name="ebook_download_tokens_order_id_fk",
        ),
        nullable=False,
    )
    token: Mapped[str] = mapped_column(Text, nullable=False)  # 64 hex
    email: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    download_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_downloads: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("10"))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    last_downloaded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class CancellationReason(Base):
    """Port public.cancellation_reasons (audyt log akcji retencyjnych)."""

    __tablename__ = "cancellation_reasons"
    __table_args__ = (
        Index("idx_cancellation_reasons_created", "created_at"),
        {"schema": "billing"},
    )

    id: Mapped[uuid_mod.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    email: Mapped[str] = mapped_column(Text, nullable=False)  # znormalizowany
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(
        cancellation_action_enum, nullable=False, server_default=text("'cancelled'")
    )
    freeze_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class AuditLog(Base):
    """Audyt akcji adminow na subskrybentach (kto, kiedy, co - wymog planu).

    admin_user_id nullable: w dev require_auth zwraca DEV_FAKE_AUTH (id=0,
    nie istnieje w admin.users) - wtedy zapisujemy NULL.
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("idx_audit_log_created", "created_at"),
        Index("idx_audit_log_target_email", "target_email"),
        {"schema": "billing"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    admin_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("admin.users.id", ondelete="SET NULL", name="audit_log_admin_user_id_fk"),
        nullable=True,
    )
    action: Mapped[str] = mapped_column(Text, nullable=False)  # np. "pause_subscription"
    target_email: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
