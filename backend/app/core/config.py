"""Port core/env.ts. Nazwy zmiennych env identyczne ze starym backendem Node.

Walidacja przy imporcie modulu: niepoprawne env = exit(1) zanim cokolwiek wstanie,
tak jak safeParse + process.exit(1) w oryginale.
"""

import getpass
import re
import sys
from typing import Literal

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

_EMPTY_AS_NONE_FIELDS = (
    "DATABASE_URL",
    "BOOTSTRAP_ADMIN_LABEL",
    "BOOTSTRAP_ADMIN_EMAIL",
    "BOOTSTRAP_ADMIN_TOKEN",
    "WEB_DIST_PATH",
    "SECRETS_MASTER_KEY",
    "OPENAI_API_KEY",
    # Faza 2 (landing) - wszystkie opcjonalne, pusty string = brak.
    "STRIPE_SECRET_KEY",
    "STRIPE_LEGACY_SECRET_KEY",
    "STRIPE_WEBHOOK_SECRET",
    "STRIPE_LEGACY_WEBHOOK_SECRET",
    "CIRCLE_API_TOKEN",
    "CIRCLE_COMMUNITY_ID",
    "RESEND_API_KEY",
    "CANCELLATION_DOI_SECRET",
    "CANCELLATION_FROM_EMAIL",
    "NEWSLETTER_DOI_SECRET",
    "NEWSLETTER_FROM_EMAIL",
    "CONFIRM_URL_BASE",
    "FRONTEND_URL",
    "SENDER_API_TOKEN",
    "SENDER_GROUP_IDS",
    "META_PIXEL_ID",
    "META_CAPI_TOKEN",
    "EBOOK_FILE_PATH",
    # Tryb lokalny / mocki - puste = auto (decyzja w app/core/dev_mode.py).
    "MOCK_EMAIL",
    "MOCK_SENDER",
    "MOCK_CIRCLE_MEMBERS",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    NODE_ENV: Literal["development", "production", "test"] = "development"
    PORT: int = Field(default=3000, ge=1, le=65535)
    LOG_LEVEL: Literal["debug", "info", "warn", "error"] = "info"

    # Stary backend mial tylko DATABASE_URL; nowy sklada DSN z DB_* (peer-auth
    # Postgres.app lokalnie = pusty DB_PASS). DATABASE_URL ma priorytet, zeby
    # stare pliki .env dzialaly bez zmian.
    DATABASE_URL: str | None = None
    DB_HOST: str = "localhost"
    DB_PORT: int = Field(default=5432, ge=1, le=65535)
    DB_USER: str = Field(default_factory=getpass.getuser)
    DB_PASS: str = ""
    DB_NAME: str = "befreeclub"

    CLAUDE_BIN_PATH: str = Field(min_length=1)
    DRAFT_MODEL: str = "claude-sonnet-4-6"
    POLISH_MODEL: str = "claude-opus-4-7"
    CLAUDE_MAX_CONCURRENT: int = Field(default=2, ge=1, le=8)
    POLLING_INTERVAL_MS: int = Field(default=30000, ge=5000)

    # W oryginale stale w knowledge-base.ts; tu konfigurowalne z tymi samymi defaultami.
    KB_BUDGET_TOKENS: int = 60_000
    KB_HARD_CEILING_TOKENS: int = 90_000

    BOOTSTRAP_ADMIN_LABEL: str | None = None
    BOOTSTRAP_ADMIN_EMAIL: str | None = None
    BOOTSTRAP_ADMIN_TOKEN: str | None = None
    WEB_DIST_PATH: str | None = None

    # Klucz master Fernet do szyfrowania edytowalnych sekretow integracji
    # (admin.encrypted_secrets). 44-znakowy klucz url-safe base64. Brak/zly klucz =
    # bezpieczny fallback na env, nigdy crash (patrz app/core/secret_box.py).
    SECRETS_MASTER_KEY: str | None = None

    OPENAI_API_KEY: str | None = None
    OPENAI_WHISPER_MODEL: str = "whisper-1"
    OPENAI_VISION_MODEL: str = "gpt-4o-mini"
    VOICE_TRANSCRIPT_INTERVAL_MS: int = Field(default=20000, ge=5000)
    IMAGE_DESCRIPTION_INTERVAL_MS: int = Field(default=20000, ge=5000)

    # ── Faza 2: landing befreeclub.pl ─────────────────────────────────────
    # Nazwy IDENTYCZNE z sekretami Supabase (docs/spec-landing/config-deploy.md).
    # Defaulty biznesowe (np. FRONTEND_URL -> https://befreeclub.pl) aplikuja
    # moduly w miejscu uzycia - tak jak edge functions robily `?? fallback`.
    STRIPE_SECRET_KEY: str | None = None  # konto current
    STRIPE_LEGACY_SECRET_KEY: str | None = None  # konto legacy
    STRIPE_WEBHOOK_SECRET: str | None = None  # signing secret webhooka current
    STRIPE_LEGACY_WEBHOOK_SECRET: str | None = None  # NOWY: webhook konta legacy
    CIRCLE_API_TOKEN: str | None = None
    CIRCLE_COMMUNITY_ID: str | None = None  # int jako string, jak w Supabase
    RESEND_API_KEY: str | None = None
    CANCELLATION_DOI_SECRET: str | None = None  # HMAC magic linkow (anulowanie, zmiana karty)
    CANCELLATION_FROM_EMAIL: str | None = None
    NEWSLETTER_DOI_SECRET: str | None = None  # HMAC double opt-in newslettera
    NEWSLETTER_FROM_EMAIL: str | None = None
    CONFIRM_URL_BASE: str | None = None
    FRONTEND_URL: str | None = None
    SENDER_API_TOKEN: str | None = None
    SENDER_GROUP_IDS: str | None = None  # CSV id grup Sender.net
    META_PIXEL_ID: str | None = None  # NOWY: Meta Conversions API
    META_CAPI_TOKEN: str | None = None  # NOWY: Meta Conversions API
    EBOOK_FILE_PATH: str | None = None  # NOWY: sciezka PDF na dysku (zamiast bucketa)

    # ── Tryb lokalny / mocki (app/core/dev_mode.py) ───────────────────────
    # Puste = auto: mock gdy NODE_ENV != production i brak klucza serwisu.
    # true = wymus mock nawet z kluczem; false = nigdy nie mockuj (testy).
    # Na produkcji ignorowane - nic sie nie mockuje, brak klucza = twardy blad.
    MOCK_EMAIL: bool | None = None
    MOCK_SENDER: bool | None = None
    MOCK_CIRCLE_MEMBERS: bool | None = None

    # Workery fazy 2 (semantyka jak POLLING_INTERVAL_MS). Default cleanupu 6 h -
    # oryginalny harmonogram zyje TYLKO w prod DB Supabase (cron.job), do
    # potwierdzenia przy migracji (PLAN_LANDING, migracja pkt 1).
    MEMBERSHIP_CLEANUP_INTERVAL_MS: int = Field(default=21_600_000, ge=5000)
    KLARNA_RECONCILE_INTERVAL_MS: int = Field(default=3_600_000, ge=5000)  # sweep 7 dni co 1 h
    INVITE_RETRY_INTERVAL_MS: int = Field(default=3_600_000, ge=5000)

    @field_validator(*_EMPTY_AS_NONE_FIELDS, mode="before")
    @classmethod
    def _empty_string_is_unset(cls, v: object) -> object:
        if isinstance(v, str) and len(v) == 0:
            return None
        return v

    @field_validator("BOOTSTRAP_ADMIN_EMAIL")
    @classmethod
    def _validate_email(cls, v: str | None) -> str | None:
        if v is not None and not _EMAIL_RE.match(v):
            raise ValueError("invalid email")
        return v

    @field_validator("DATABASE_URL")
    @classmethod
    def _validate_url(cls, v: str | None) -> str | None:
        if v is not None and "://" not in v:
            raise ValueError("must be a valid URL")
        return v


def guard_dev_stripe_keys(s: Settings) -> None:
    """Poza produkcja klucz live Stripe (sk_live_) blokuje start aplikacji.

    Stripe NIGDY nie jest mockowany - dev uzywa Stripe test mode (sk_test_),
    to oficjalna piaskownica. Guard chroni przed odpaleniem lokalnego kodu
    na prawdziwych platnosciach i klientach.
    """
    if s.NODE_ENV == "production":
        return
    for field in ("STRIPE_SECRET_KEY", "STRIPE_LEGACY_SECRET_KEY"):
        value = getattr(s, field)
        if value is not None and value.startswith("sk_live_"):
            print(
                f"❌ {field} to klucz LIVE (sk_live_...) przy NODE_ENV={s.NODE_ENV}.\n"
                "W dev uzywaj wylacznie kluczy testowych Stripe (sk_test_...).\n"
                "Stripe nie jest mockowany - test mode to oficjalna piaskownica Stripe.",
                file=sys.stderr,
            )
            raise SystemExit(1)


def _load_settings() -> Settings:
    try:
        loaded = Settings()
    except ValidationError as err:
        field_errors: dict[str, list[str]] = {}
        for e in err.errors():
            key = ".".join(str(p) for p in e["loc"]) or "(root)"
            field_errors.setdefault(key, []).append(e["msg"])
        print("❌ Invalid environment variables:\n", field_errors, file=sys.stderr)
        raise SystemExit(1) from None
    guard_dev_stripe_keys(loaded)
    return loaded


settings = _load_settings()
