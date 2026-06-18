"""faza 2: schematy billing + members + newsletter + landing, enumy, tabele, seed planow

Port schematu z Supabase (docs/spec-landing/db-schema.md) z porzadkami
wpisanymi w plan (PLAN_LANDING "Naprawy wpisane w port"): enumy zamiast
golego text, idempotencja webhookow (webhook_events), atrybucja UTM/Meta
(checkout_attributions), enum statusu czlonka zamiast bool active.

Seed billing.plans: realne plany i price ID konta current
(docs/spec-landing/billing-checkout.md sekcja 2 + ebook-newsletter-misc.md 1.1).

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-10
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS billing")
    op.execute("CREATE SCHEMA IF NOT EXISTS members")
    op.execute("CREATE SCHEMA IF NOT EXISTS newsletter")
    op.execute("CREATE SCHEMA IF NOT EXISTS landing")

    # ── enumy ────────────────────────────────────────────────────────────────

    op.execute("CREATE TYPE billing.stripe_account AS ENUM ('current', 'legacy')")
    op.execute(
        "CREATE TYPE billing.plan_interval AS ENUM "
        "('month', 'quarter', 'half_year', 'year', 'one_time')"
    )
    op.execute(
        "CREATE TYPE billing.attribution_kind AS ENUM ('subscription', 'klarna', 'ebook')"
    )
    op.execute("CREATE TYPE billing.ebook_order_status AS ENUM ('pending', 'paid', 'refunded')")
    op.execute("CREATE TYPE billing.cancellation_action AS ENUM ('cancelled', 'frozen')")
    op.execute(
        "CREATE TYPE members.member_status AS ENUM "
        "('invited', 'active', 'paused', 'pending_removal', 'removed', 'invite_failed')"
    )
    op.execute(
        "CREATE TYPE members.member_source AS ENUM ('subscription', 'one_time', 'manual')"
    )

    # ── billing ──────────────────────────────────────────────────────────────

    op.execute("""
        CREATE TABLE billing.plans (
            id bigserial PRIMARY KEY,
            slug text NOT NULL,
            name text NOT NULL,
            stripe_price_id text NOT NULL,
            stripe_account billing.stripe_account NOT NULL DEFAULT 'current',
            amount_pln integer NOT NULL,
            "interval" billing.plan_interval NOT NULL,
            active boolean NOT NULL DEFAULT true,
            sort integer NOT NULL DEFAULT 0,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT plans_slug_unique UNIQUE (slug)
        )
    """)
    op.execute("CREATE INDEX idx_plans_active_sort ON billing.plans (active, sort)")

    op.execute("""
        CREATE TABLE billing.webhook_events (
            id bigserial PRIMARY KEY,
            stripe_account billing.stripe_account NOT NULL,
            event_id text NOT NULL,
            type text NOT NULL,
            payload jsonb NOT NULL,
            processed_at timestamptz,
            error text,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT webhook_events_event_id_unique UNIQUE (event_id)
        )
    """)
    op.execute("CREATE INDEX idx_webhook_events_created ON billing.webhook_events (created_at)")
    op.execute(
        "CREATE INDEX idx_webhook_events_unprocessed ON billing.webhook_events (created_at) "
        "WHERE processed_at IS NULL"
    )

    op.execute("""
        CREATE TABLE billing.checkout_attributions (
            id bigserial PRIMARY KEY,
            kind billing.attribution_kind NOT NULL,
            email text,
            stripe_object_id text NOT NULL,
            utm_source text,
            utm_medium text,
            utm_campaign text,
            utm_term text,
            utm_content text,
            fbclid text,
            fbp text,
            fbc text,
            referrer text,
            landing_page text,
            client_ip text,
            client_ua text,
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute(
        "CREATE INDEX idx_checkout_attributions_object "
        "ON billing.checkout_attributions (stripe_object_id)"
    )
    op.execute(
        "CREATE INDEX idx_checkout_attributions_email ON billing.checkout_attributions (email)"
    )
    op.execute(
        "CREATE INDEX idx_checkout_attributions_created "
        "ON billing.checkout_attributions (created_at)"
    )

    op.execute("""
        CREATE TABLE billing.ebook_orders (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            email text NOT NULL,
            stripe_session_id text,
            stripe_payment_intent_id text,
            amount_paid integer NOT NULL,
            currency text NOT NULL DEFAULT 'pln',
            status billing.ebook_order_status NOT NULL DEFAULT 'pending',
            wants_invoice boolean NOT NULL DEFAULT false,
            nip text,
            invoice_name text,
            email_sent_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            paid_at timestamptz,
            CONSTRAINT ebook_orders_stripe_session_id_unique UNIQUE (stripe_session_id),
            CONSTRAINT ebook_orders_stripe_payment_intent_id_unique
                UNIQUE (stripe_payment_intent_id)
        )
    """)
    op.execute("CREATE INDEX idx_ebook_orders_email ON billing.ebook_orders (email)")

    op.execute("""
        CREATE TABLE billing.ebook_download_tokens (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            order_id uuid NOT NULL,
            token text NOT NULL,
            email text NOT NULL,
            expires_at timestamptz NOT NULL,
            download_count integer NOT NULL DEFAULT 0,
            max_downloads integer NOT NULL DEFAULT 10,
            revoked_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            last_downloaded_at timestamptz,
            CONSTRAINT ebook_download_tokens_token_unique UNIQUE (token),
            CONSTRAINT ebook_download_tokens_order_id_fk
                FOREIGN KEY (order_id) REFERENCES billing.ebook_orders(id) ON DELETE CASCADE
        )
    """)
    op.execute(
        "CREATE INDEX idx_ebook_download_tokens_order ON billing.ebook_download_tokens (order_id)"
    )

    op.execute("""
        CREATE TABLE billing.cancellation_reasons (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            email text NOT NULL,
            reason text NOT NULL,
            action billing.cancellation_action NOT NULL DEFAULT 'cancelled',
            freeze_days integer,
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute(
        "CREATE INDEX idx_cancellation_reasons_created ON billing.cancellation_reasons (created_at)"
    )

    op.execute("""
        CREATE TABLE billing.audit_log (
            id bigserial PRIMARY KEY,
            admin_user_id bigint,
            action text NOT NULL,
            target_email text,
            payload jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT audit_log_admin_user_id_fk
                FOREIGN KEY (admin_user_id) REFERENCES admin.users(id) ON DELETE SET NULL
        )
    """)
    op.execute("CREATE INDEX idx_audit_log_created ON billing.audit_log (created_at)")
    op.execute("CREATE INDEX idx_audit_log_target_email ON billing.audit_log (target_email)")

    # ── members ──────────────────────────────────────────────────────────────

    op.execute("""
        CREATE TABLE members.members (
            id bigserial PRIMARY KEY,
            email text NOT NULL,
            name text,
            circle_member_id text,
            status members.member_status NOT NULL DEFAULT 'invited',
            protected boolean NOT NULL DEFAULT false,
            source members.member_source NOT NULL DEFAULT 'subscription',
            expires_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT members_email_unique UNIQUE (email),
            CONSTRAINT members_email_normalized_check CHECK (email = lower(btrim(email)))
        )
    """)
    op.execute("CREATE INDEX idx_members_status ON members.members (status)")
    op.execute(
        "CREATE INDEX idx_members_expires_at ON members.members (expires_at) "
        "WHERE expires_at IS NOT NULL"
    )

    op.execute("""
        CREATE TABLE members.events (
            id bigserial PRIMARY KEY,
            member_id bigint NOT NULL,
            kind text NOT NULL,
            detail jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT events_member_id_fk
                FOREIGN KEY (member_id) REFERENCES members.members(id) ON DELETE CASCADE
        )
    """)
    op.execute(
        "CREATE INDEX idx_member_events_member_created ON members.events (member_id, created_at)"
    )

    # ── newsletter ───────────────────────────────────────────────────────────

    op.execute("""
        CREATE TABLE newsletter.contact_messages (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            name text NOT NULL,
            email text NOT NULL,
            message text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute(
        "CREATE INDEX idx_contact_messages_created ON newsletter.contact_messages (created_at)"
    )

    # ── landing ──────────────────────────────────────────────────────────────

    op.execute("""
        CREATE TABLE landing.articles (
            id bigserial PRIMARY KEY,
            slug text NOT NULL,
            title text NOT NULL,
            lead text,
            body_md text NOT NULL DEFAULT '',
            seo jsonb NOT NULL DEFAULT '{}'::jsonb,
            published_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT articles_slug_unique UNIQUE (slug)
        )
    """)
    op.execute("CREATE INDEX idx_articles_published ON landing.articles (published_at)")

    op.execute("""
        CREATE TABLE landing.content_blocks (
            key text PRIMARY KEY,
            value jsonb NOT NULL,
            updated_at timestamptz NOT NULL DEFAULT now()
        )
    """)

    # ── seed billing.plans ───────────────────────────────────────────────────
    # Kwoty w GROSZACH, price ID konta current - 1:1 ze specow (billing-checkout
    # sekcja 2: 639/879/1489 zl; ebook-newsletter-misc 1.1: 249 zl).

    op.execute("""
        INSERT INTO billing.plans
            (slug, name, stripe_price_id, stripe_account, amount_pln, "interval", active, sort)
        VALUES
            ('quarterly', 'Starter', 'price_1TdVWjDlsrz5Z08F0gc9nskb',
             'current', 63900, 'quarter', true, 1),
            ('semiannual', 'Pro', 'price_1TdVWkDlsrz5Z08FydX2azl9',
             'current', 87900, 'half_year', true, 2),
            ('annual', 'Master', 'price_1T8aHeDlsrz5Z08FmgzIUyTB',
             'current', 148900, 'year', true, 3),
            ('ebook', 'Ebook: Na swoich zasadach jako freelancer',
             'price_1TToiWDlsrz5Z08F1DBx2KTQ', 'current', 24900, 'one_time', true, 4)
    """)


def downgrade() -> None:
    op.execute("DROP SCHEMA landing CASCADE")
    op.execute("DROP SCHEMA newsletter CASCADE")
    op.execute("DROP SCHEMA members CASCADE")
    op.execute("DROP SCHEMA billing CASCADE")
