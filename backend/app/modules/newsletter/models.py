"""Tabele schematu PG "newsletter".

Tylko contact_messages (port public.contact_messages 1:1 + indeks na created_at,
bo wiadomosci beda czytane w panelu admina, nie przez dashboard Supabase).
Lista newslettera zyje w Sender.net (DOI stateless HMAC) - bez tabeli.
Martwa public.newsletter_subscribers NIE jest portowana.
"""

import uuid as uuid_mod
from datetime import datetime

from sqlalchemy import DateTime, Index, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class ContactMessage(Base):
    __tablename__ = "contact_messages"
    __table_args__ = (
        Index("idx_contact_messages_created", "created_at"),
        {"schema": "newsletter"},
    )

    id: Mapped[uuid_mod.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
