"""Tabele schematu PG "landing" (tresc landinga zarzadzana z panelu admina).

articles: artykuly Wiedza jako markdown w DB (koniec z komponentami React).
content_blocks: opinie / FAQ / liczniki / flagi kampanii jako JSONB per klucz
(koniec z PROMO_CAMPAIGN_ACTIVE w kodzie i deployem po zmiane ceny).
"""

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Index, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class Article(Base):
    __tablename__ = "articles"
    __table_args__ = (
        UniqueConstraint("slug", name="articles_slug_unique"),
        Index("idx_articles_published", "published_at"),
        {"schema": "landing"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    lead: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_md: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    seo: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )  # NULL = draft
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class ContentBlock(Base):
    """Blok tresci per klucz (np. "faq", "testimonials", "promo_campaign")."""

    __tablename__ = "content_blocks"
    __table_args__ = ({"schema": "landing"},)

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
