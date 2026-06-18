"""Tabele schematu PG "members" (stan czlonkostwa Circle).

Zastepuje public.circle_members: enum statusu zamiast bool active (naprawa #4 -
koniec z re-invitowaniem wyrzuconych), flaga protected zamiast hardcoded
PROTECTED_EMAILS, events jako historia zmian stanu.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ENUM, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

member_status_enum = ENUM(
    "invited",
    "active",
    "paused",
    "pending_removal",
    "removed",
    "invite_failed",
    name="member_status",
    schema="members",
    create_type=False,
)
member_source_enum = ENUM(
    "subscription",
    "one_time",
    "manual",
    name="member_source",
    schema="members",
    create_type=False,
)


class Member(Base):
    """Czlonek klubu zarzadzany przez landing/backend (rejestr provisioning Circle).

    Semantyka statusow:
    - invited: zaproszony do Circle, czeka na akceptacje
    - active: w Circle, dostep aktywny
    - paused: pauza (Stripe pause_collection / akcja admina)
    - pending_removal: do usuniecia przy najblizszym cleanupie
    - removed: usuniety z Circle (cleanup / anulowanie / refund)
    - invite_failed: zaproszenie do Circle nie wyszlo (retry tylko TEN status)
    """

    __tablename__ = "members"
    __table_args__ = (
        UniqueConstraint("email", name="members_email_unique"),
        # Normalizacja wymuszona na poziomie DB (naprawa #5): kod i tak robi
        # normalize_email, ale duplikat "Jan@x.pl" nie ma prawa powstac.
        CheckConstraint("email = lower(btrim(email))", name="members_email_normalized_check"),
        Index("idx_members_status", "status"),
        Index(
            "idx_members_expires_at",
            "expires_at",
            postgresql_where=text("expires_at IS NOT NULL"),
        ),
        {"schema": "members"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(Text, nullable=False)  # znormalizowany (CHECK)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    circle_member_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        member_status_enum, nullable=False, server_default=text("'invited'")
    )
    protected: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    source: Mapped[str] = mapped_column(
        member_source_enum, nullable=False, server_default=text("'subscription'")
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )  # tylko one_time; NULL = wg Stripe
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class MemberEvent(Base):
    """Historia zmian stanu czlonka (timeline w panelu admina)."""

    __tablename__ = "events"
    __table_args__ = (
        Index("idx_member_events_member_created", "member_id", "created_at"),
        {"schema": "members"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    member_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("members.members.id", ondelete="CASCADE", name="events_member_id_fk"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)  # np. "invited", "removed", "paused"
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
