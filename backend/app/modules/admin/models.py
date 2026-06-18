"""Tabele schematu PG "admin" (stare: auth_accounts, auth_sessions, feedback_items).

Mapa nazw wg docs/ARCHITEKTURA.md: auth_accounts -> admin.users,
auth_sessions -> admin.sessions (auth_account_id -> user_id).
Kolumny/typy/defaulty 1:1 z docs/spec/db-schema.md.
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import ENUM
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

feedback_status_enum = ENUM(
    "open", "done", name="feedback_status", schema="admin", create_type=False
)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("email", name="users_email_unique"),
        {"schema": "admin"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    # Brak triggera updated_at - aktualizuje kod aplikacji.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class Session(Base):
    __tablename__ = "sessions"
    __table_args__ = (
        Index("idx_sessions_expires", "expires_at"),
        {"schema": "admin"},
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("admin.users.id", ondelete="CASCADE", name="sessions_user_id_users_id_fk"),
        nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    ip_addr: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class FeedbackItem(Base):
    __tablename__ = "feedback_items"
    __table_args__ = (
        Index("idx_feedback_status", "status", "created_at"),
        {"schema": "admin"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "admin.users.id", ondelete="CASCADE", name="feedback_items_user_id_users_id_fk"
        ),
        nullable=False,
    )
    scope: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'general'"))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        feedback_status_enum, nullable=False, server_default=text("'open'")
    )
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    # Trigger set_feedback_items_updated_at.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
