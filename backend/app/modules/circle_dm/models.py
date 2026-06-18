"""Tabele schematu PG "circle_dm" (tool Circle DM).

Mapa nazw wg docs/ARCHITEKTURA.md (admin_accounts -> circle_dm.accounts,
dm_threads -> circle_dm.threads itd.; admin_account_id -> account_id).
Kolumny/typy/defaulty/indeksy/UNIQUE 1:1 z docs/spec/db-schema.md.
JSON API zostaje przy starych nazwach pol (adminAccountId) - mapuje warstwa DTO.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ENUM, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

_S = "circle_dm"


def _enum(name: str, *values: str) -> ENUM:
    return ENUM(*values, name=name, schema=_S, create_type=False)


chat_room_kind_enum = _enum("chat_room_kind", "direct", "group_chat")
draft_status_enum = _enum(
    "draft_status", "idle", "generating", "has_draft", "polishing", "ready_to_send", "sent", "error"
)
iteration_kind_enum = _enum("iteration_kind", "initial", "user_feedback", "polish")
thread_status_enum = _enum("thread_status", "inbox", "done")
kb_scope_enum = _enum("kb_scope", "global", "account")
kb_source_kind_enum = _enum("kb_source_kind", "pdf", "md", "manual")
assistant_msg_role_enum = _enum("assistant_msg_role", "user", "assistant")
voice_transcript_status_enum = _enum("voice_transcript_status", "pending", "done", "error")
image_description_status_enum = _enum("image_description_status", "pending", "done", "error")


class Account(Base):
    """Konta adminow Circle.so do wysylki DM (stare admin_accounts)."""

    __tablename__ = "accounts"
    __table_args__ = ({"schema": _S},)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    circle_admin_token: Mapped[str] = mapped_column(Text, nullable=False)
    circle_refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    circle_access_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    circle_access_token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    community_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    community_member_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    # Trigger set_accounts_updated_at.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class Thread(Base):
    __tablename__ = "threads"
    __table_args__ = (
        UniqueConstraint("account_id", "circle_chat_room_uuid", name="uniq_account_room"),
        Index("idx_threads_last_msg", "account_id", "last_message_at"),
        Index("idx_threads_unread", "account_id", "unread_messages_count"),
        Index("idx_threads_pinned", "account_id", "pinned_at"),
        Index("idx_threads_status", "account_id", "status"),
        Index("idx_threads_flagged", "account_id", "is_flagged"),
        {"schema": _S},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            f"{_S}.accounts.id", ondelete="CASCADE", name="threads_account_id_accounts_id_fk"
        ),
        nullable=False,
    )
    circle_chat_room_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    circle_chat_room_uuid: Mapped[str] = mapped_column(UUID(as_uuid=False), nullable=False)
    chat_room_kind: Mapped[str] = mapped_column(chat_room_kind_enum, nullable=False)
    chat_room_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    other_participant_email: Mapped[str | None] = mapped_column(Text, nullable=True)
    other_participant_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    other_participant_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    other_participant_avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    unread_messages_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    pinned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(
        thread_status_enum, nullable=False, server_default=text("'inbox'")
    )
    is_flagged: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_message_sender_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_message_sender_is_me: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    last_message_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_payload: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    messages_fetched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class Checkup(Base):
    """Follow-upy per watek (stare thread_checkups)."""

    __tablename__ = "checkups"
    __table_args__ = (
        Index("idx_checkups_thread", "thread_id"),
        # Mimo nazwy NIE jest czesciowy - zwykly btree na due_at (1:1 z oryginalem).
        Index("idx_checkups_pending_due", "due_at"),
        {"schema": _S},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    thread_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            f"{_S}.threads.id", ondelete="CASCADE", name="checkups_thread_id_threads_id_fk"
        ),
        nullable=False,
    )
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("thread_id", "circle_message_id", name="uniq_thread_message"),
        Index("idx_messages_thread_created", "thread_id", "created_at"),
        Index("idx_messages_voice_status", "voice_transcript_status"),
        {"schema": _S},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    thread_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            f"{_S}.threads.id", ondelete="CASCADE", name="messages_thread_id_threads_id_fk"
        ),
        nullable=False,
    )
    circle_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    rich_text_body: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    sender_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sender_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    sender_is_me: Mapped[bool] = mapped_column(Boolean, nullable=False)
    parent_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    chat_thread_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    voice_transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    voice_transcript_status: Mapped[str | None] = mapped_column(
        voice_transcript_status_enum, nullable=True
    )
    voice_transcript_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    voice_transcript_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    voice_duration_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)
    voice_transcribed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class MessageImageDescription(Base):
    __tablename__ = "message_image_descriptions"
    __table_args__ = (
        UniqueConstraint("message_id", "attachment_index", name="uniq_msg_image_idx"),
        Index("idx_image_desc_status", "status"),
        {"schema": _S},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            f"{_S}.messages.id",
            ondelete="CASCADE",
            name="message_image_descriptions_message_id_messages_id_fk",
        ),
        nullable=False,
    )
    attachment_index: Mapped[int] = mapped_column(Integer, nullable=False)
    attachment_url: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        image_description_status_enum, nullable=False, server_default=text("'pending'")
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    described_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DraftSession(Base):
    __tablename__ = "draft_sessions"
    __table_args__ = (
        UniqueConstraint("thread_id", name="draft_sessions_thread_id_unique"),
        Index("idx_draft_sessions_status", "status"),
        {"schema": _S},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    thread_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            f"{_S}.threads.id", ondelete="CASCADE", name="draft_sessions_thread_id_threads_id_fk"
        ),
        nullable=False,
    )
    claude_session_id: Mapped[str] = mapped_column(UUID(as_uuid=False), nullable=False)
    status: Mapped[str] = mapped_column(
        draft_status_enum, nullable=False, server_default=text("'idle'")
    )
    current_draft: Mapped[str | None] = mapped_column(Text, nullable=True)
    iterations_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    # Trigger set_draft_sessions_updated_at.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class DraftIteration(Base):
    __tablename__ = "draft_iterations"
    __table_args__ = ({"schema": _S},)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    draft_session_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            f"{_S}.draft_sessions.id",
            ondelete="CASCADE",
            name="draft_iterations_draft_session_id_draft_sessions_id_fk",
        ),
        nullable=False,
    )
    iteration_kind: Mapped[str] = mapped_column(iteration_kind_enum, nullable=False)
    user_instruction: Mapped[str | None] = mapped_column(Text, nullable=True)
    draft_text: Mapped[str] = mapped_column(Text, nullable=False)
    tokens_used: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class Member(Base):
    """Cache czlonkow spolecznosci (stare community_members)."""

    __tablename__ = "members"
    __table_args__ = (
        UniqueConstraint("account_id", "circle_community_member_id", name="uniq_account_member"),
        Index("idx_members_name", "account_id", "name"),
        {"schema": _S},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            f"{_S}.accounts.id", ondelete="CASCADE", name="members_account_id_accounts_id_fk"
        ),
        nullable=False,
    )
    circle_community_member_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str | None] = mapped_column(Text, nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    headline: Mapped[str | None] = mapped_column(Text, nullable=True)
    bio: Mapped[str | None] = mapped_column(Text, nullable=True)
    location: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_seen_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    can_send_message: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    raw_payload: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class SentMessage(Base):
    __tablename__ = "sent_messages"
    __table_args__ = ({"schema": _S},)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    thread_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            f"{_S}.threads.id", ondelete="CASCADE", name="sent_messages_thread_id_threads_id_fk"
        ),
        nullable=False,
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    circle_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    circle_creation_uuid: Mapped[str | None] = mapped_column(UUID(as_uuid=False), nullable=True)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    draft_session_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            f"{_S}.draft_sessions.id",
            ondelete="SET NULL",
            name="sent_messages_draft_session_id_draft_sessions_id_fk",
        ),
        nullable=True,
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class AppSettings(Base):
    """Singleton ustawien globalnych toola (stare app_settings), zawsze id=1."""

    __tablename__ = "settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="settings_singleton"),
        {"schema": _S},
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, server_default=text("1"), autoincrement=False
    )
    global_meta_prompt: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("''")
    )
    format_prompt: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    draft_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    format_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    no_reply_threshold_days: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("3")
    )
    silence_threshold_days: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("14")
    )
    # Brak triggera - updated_at ustawia kod aplikacji.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class KbDocument(Base):
    __tablename__ = "kb_documents"
    __table_args__ = (
        Index("idx_kb_scope", "scope"),
        Index("idx_kb_account", "account_id"),
        {"schema": _S},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(kb_scope_enum, nullable=False)
    account_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            f"{_S}.accounts.id", ondelete="CASCADE", name="kb_documents_account_id_accounts_id_fk"
        ),
        nullable=True,
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body_text: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    source_kind: Mapped[str] = mapped_column(kb_source_kind_enum, nullable=False)
    original_filename: Mapped[str | None] = mapped_column(Text, nullable=True)
    original_mime: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Potencjalnie duze wiersze (PDF w base64) - listingi maja NIE selectowac tej kolumny.
    original_data_b64: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_estimate: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    # Trigger set_kb_documents_updated_at.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class AssistantConversation(Base):
    __tablename__ = "assistant_conversations"
    __table_args__ = (
        Index("idx_asst_conv_auth", "user_id", "last_message_at"),
        {"schema": _S},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "admin.users.id",
            ondelete="CASCADE",
            name="assistant_conversations_user_id_users_id_fk",
        ),
        nullable=False,
    )
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    # Trigger set_assistant_conversations_updated_at.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class AssistantMessage(Base):
    __tablename__ = "assistant_messages"
    __table_args__ = (
        Index("idx_asst_msg_conv", "conversation_id", "created_at"),
        {"schema": _S},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            f"{_S}.assistant_conversations.id",
            ondelete="CASCADE",
            name="assistant_messages_conversation_id_assistant_conversations_id_fk",
        ),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(assistant_msg_role_enum, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    raw_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    context_snapshot: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    action_proposal: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    apply_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    tokens_used: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    # Trigger set_assistant_messages_updated_at.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
