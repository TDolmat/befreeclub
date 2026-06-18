"""initial: schematy admin + circle_dm, 16 tabel, enumy, triggery, seedy

Stan koncowy starej bazy bfc_admin (migracje 0000-0014 + triggery z migrate.ts),
przepisany na nowe nazwy wg docs/ARCHITEKTURA.md. Triggery updated_at w oryginale
byly instalowane poza migracjami przy kazdym boocie - tu sa czescia migracji.

Revision ID: 0001
Revises:
Create Date: 2026-06-10
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Seed format_prompt z oryginalnej migracji 0007 - DOSLOWNIE (em-dashe, "1–4").
FORMAT_PROMPT_SEED = """Twoje zadanie: wziąć tekst od użytkownika i przerobić go w finalną wiadomość DM do drugiej osoby, zgodnie z personą i kontekstem rozmowy.

Tekst od użytkownika może być:
- Roboczym draftem (gotowym do polerowania)
- Brain dumpem z dyktowania (luźne notatki, ad-hoc gramatyka)
- Krótką instrukcją tego co chcę napisać (np. "zaproś go na spotkanie we wtorek")

Wytyczne:
- Zachowaj naturalny, mówiony ton z persony — to nie ma być korpomowa.
- Popraw gramatykę i interpunkcję, ale **nie wygładzaj do bezpłciowego stylu**.
- Jeśli to brain dump — zrekonstruuj wiadomość w pierwszej osobie zgodnie z personą.
- Jeśli to gotowy draft — popraw co trzeba, ale zachowaj sens.
- Krótko (zwykle 1–4 zdania).
- Zwróć WYŁĄCZNIE finalną treść wiadomości — bez prefiksu "Oto:", bez wyjaśnień, bez cudzysłowów."""

TRIGGER_TABLES = [
    ("set_feedback_items_updated_at", "admin.feedback_items"),
    ("set_accounts_updated_at", "circle_dm.accounts"),
    ("set_draft_sessions_updated_at", "circle_dm.draft_sessions"),
    ("set_kb_documents_updated_at", "circle_dm.kb_documents"),
    ("set_assistant_conversations_updated_at", "circle_dm.assistant_conversations"),
    ("set_assistant_messages_updated_at", "circle_dm.assistant_messages"),
]


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS admin")
    op.execute("CREATE SCHEMA IF NOT EXISTS circle_dm")

    op.execute("CREATE TYPE admin.feedback_status AS ENUM ('open', 'done')")
    op.execute("CREATE TYPE circle_dm.chat_room_kind AS ENUM ('direct', 'group_chat')")
    op.execute(
        "CREATE TYPE circle_dm.draft_status AS ENUM "
        "('idle', 'generating', 'has_draft', 'polishing', 'ready_to_send', 'sent', 'error')"
    )
    op.execute(
        "CREATE TYPE circle_dm.iteration_kind AS ENUM ('initial', 'user_feedback', 'polish')"
    )
    op.execute("CREATE TYPE circle_dm.thread_status AS ENUM ('inbox', 'done')")
    op.execute("CREATE TYPE circle_dm.kb_scope AS ENUM ('global', 'account')")
    op.execute("CREATE TYPE circle_dm.kb_source_kind AS ENUM ('pdf', 'md', 'manual')")
    op.execute("CREATE TYPE circle_dm.assistant_msg_role AS ENUM ('user', 'assistant')")
    op.execute(
        "CREATE TYPE circle_dm.voice_transcript_status AS ENUM ('pending', 'done', 'error')"
    )
    op.execute(
        "CREATE TYPE circle_dm.image_description_status AS ENUM ('pending', 'done', 'error')"
    )

    # ── admin ────────────────────────────────────────────────────────────────

    op.execute("""
        CREATE TABLE admin.users (
            id bigserial PRIMARY KEY,
            email text NOT NULL,
            password_hash text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT users_email_unique UNIQUE (email)
        )
    """)

    op.execute("""
        CREATE TABLE admin.sessions (
            id text PRIMARY KEY,
            user_id bigint NOT NULL,
            expires_at timestamptz NOT NULL,
            last_seen_at timestamptz NOT NULL DEFAULT now(),
            ip_addr text,
            user_agent text,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT sessions_user_id_users_id_fk
                FOREIGN KEY (user_id) REFERENCES admin.users(id) ON DELETE CASCADE
        )
    """)
    op.execute("CREATE INDEX idx_sessions_expires ON admin.sessions (expires_at)")

    op.execute("""
        CREATE TABLE admin.feedback_items (
            id bigserial PRIMARY KEY,
            user_id bigint NOT NULL,
            scope text NOT NULL DEFAULT 'general',
            body text NOT NULL,
            status admin.feedback_status NOT NULL DEFAULT 'open',
            done_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT feedback_items_user_id_users_id_fk
                FOREIGN KEY (user_id) REFERENCES admin.users(id) ON DELETE CASCADE
        )
    """)
    op.execute("CREATE INDEX idx_feedback_status ON admin.feedback_items (status, created_at)")

    # ── circle_dm ────────────────────────────────────────────────────────────

    op.execute("""
        CREATE TABLE circle_dm.accounts (
            id bigserial PRIMARY KEY,
            label text NOT NULL,
            email text NOT NULL,
            circle_admin_token text NOT NULL,
            circle_refresh_token text,
            circle_access_token text,
            circle_access_token_expires_at timestamptz,
            community_id bigint,
            community_member_id bigint,
            system_prompt text NOT NULL,
            is_active boolean NOT NULL DEFAULT true,
            last_synced_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
    """)

    op.execute("""
        CREATE TABLE circle_dm.threads (
            id bigserial PRIMARY KEY,
            account_id bigint NOT NULL,
            circle_chat_room_id bigint NOT NULL,
            circle_chat_room_uuid uuid NOT NULL,
            chat_room_kind circle_dm.chat_room_kind NOT NULL,
            chat_room_name text,
            other_participant_email text,
            other_participant_name text,
            other_participant_id bigint,
            other_participant_avatar_url text,
            unread_messages_count integer NOT NULL DEFAULT 0,
            pinned_at timestamptz,
            status circle_dm.thread_status NOT NULL DEFAULT 'inbox',
            is_flagged boolean NOT NULL DEFAULT false,
            last_message_at timestamptz,
            last_message_sender_id bigint,
            last_message_sender_is_me boolean NOT NULL DEFAULT false,
            last_message_preview text,
            raw_payload jsonb,
            messages_fetched_at timestamptz,
            fetched_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT threads_account_id_accounts_id_fk
                FOREIGN KEY (account_id) REFERENCES circle_dm.accounts(id) ON DELETE CASCADE,
            CONSTRAINT uniq_account_room UNIQUE (account_id, circle_chat_room_uuid)
        )
    """)
    op.execute(
        "CREATE INDEX idx_threads_last_msg ON circle_dm.threads (account_id, last_message_at)"
    )
    op.execute(
        "CREATE INDEX idx_threads_unread ON circle_dm.threads (account_id, unread_messages_count)"
    )
    op.execute("CREATE INDEX idx_threads_pinned ON circle_dm.threads (account_id, pinned_at)")
    op.execute("CREATE INDEX idx_threads_status ON circle_dm.threads (account_id, status)")
    op.execute("CREATE INDEX idx_threads_flagged ON circle_dm.threads (account_id, is_flagged)")

    op.execute("""
        CREATE TABLE circle_dm.checkups (
            id bigserial PRIMARY KEY,
            thread_id bigint NOT NULL,
            due_at timestamptz NOT NULL,
            note text,
            done_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT checkups_thread_id_threads_id_fk
                FOREIGN KEY (thread_id) REFERENCES circle_dm.threads(id) ON DELETE CASCADE
        )
    """)
    op.execute("CREATE INDEX idx_checkups_thread ON circle_dm.checkups (thread_id)")
    # Mimo nazwy NIE jest indeksem czesciowym - zwykly btree (1:1 z oryginalem).
    op.execute("CREATE INDEX idx_checkups_pending_due ON circle_dm.checkups (due_at)")

    op.execute("""
        CREATE TABLE circle_dm.messages (
            id bigserial PRIMARY KEY,
            thread_id bigint NOT NULL,
            circle_message_id bigint NOT NULL,
            body text NOT NULL,
            rich_text_body jsonb,
            sender_id bigint,
            sender_name text,
            sender_is_me boolean NOT NULL,
            parent_message_id bigint,
            chat_thread_id bigint,
            created_at timestamptz NOT NULL,
            edited_at timestamptz,
            fetched_at timestamptz NOT NULL DEFAULT now(),
            voice_transcript text,
            voice_transcript_status circle_dm.voice_transcript_status,
            voice_transcript_error text,
            voice_transcript_attempts integer NOT NULL DEFAULT 0,
            voice_duration_sec integer,
            voice_transcribed_at timestamptz,
            CONSTRAINT messages_thread_id_threads_id_fk
                FOREIGN KEY (thread_id) REFERENCES circle_dm.threads(id) ON DELETE CASCADE,
            CONSTRAINT uniq_thread_message UNIQUE (thread_id, circle_message_id)
        )
    """)
    op.execute(
        "CREATE INDEX idx_messages_thread_created ON circle_dm.messages (thread_id, created_at)"
    )
    op.execute(
        "CREATE INDEX idx_messages_voice_status ON circle_dm.messages (voice_transcript_status)"
    )

    op.execute("""
        CREATE TABLE circle_dm.message_image_descriptions (
            id bigserial PRIMARY KEY,
            message_id bigint NOT NULL,
            attachment_index integer NOT NULL,
            attachment_url text NOT NULL,
            description text,
            status circle_dm.image_description_status NOT NULL DEFAULT 'pending',
            error text,
            attempts integer NOT NULL DEFAULT 0,
            created_at timestamptz NOT NULL DEFAULT now(),
            described_at timestamptz,
            CONSTRAINT message_image_descriptions_message_id_messages_id_fk
                FOREIGN KEY (message_id) REFERENCES circle_dm.messages(id) ON DELETE CASCADE,
            CONSTRAINT uniq_msg_image_idx UNIQUE (message_id, attachment_index)
        )
    """)
    op.execute(
        "CREATE INDEX idx_image_desc_status ON circle_dm.message_image_descriptions (status)"
    )

    op.execute("""
        CREATE TABLE circle_dm.draft_sessions (
            id bigserial PRIMARY KEY,
            thread_id bigint NOT NULL,
            claude_session_id uuid NOT NULL,
            status circle_dm.draft_status NOT NULL DEFAULT 'idle',
            current_draft text,
            iterations_count integer NOT NULL DEFAULT 0,
            last_error text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT draft_sessions_thread_id_threads_id_fk
                FOREIGN KEY (thread_id) REFERENCES circle_dm.threads(id) ON DELETE CASCADE,
            CONSTRAINT draft_sessions_thread_id_unique UNIQUE (thread_id)
        )
    """)
    op.execute("CREATE INDEX idx_draft_sessions_status ON circle_dm.draft_sessions (status)")

    op.execute("""
        CREATE TABLE circle_dm.draft_iterations (
            id bigserial PRIMARY KEY,
            draft_session_id bigint NOT NULL,
            iteration_kind circle_dm.iteration_kind NOT NULL,
            user_instruction text,
            draft_text text NOT NULL,
            tokens_used integer,
            cost_usd numeric(10,6),
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT draft_iterations_draft_session_id_draft_sessions_id_fk
                FOREIGN KEY (draft_session_id)
                REFERENCES circle_dm.draft_sessions(id) ON DELETE CASCADE
        )
    """)

    op.execute("""
        CREATE TABLE circle_dm.members (
            id bigserial PRIMARY KEY,
            account_id bigint NOT NULL,
            circle_community_member_id bigint NOT NULL,
            name text NOT NULL,
            email text,
            avatar_url text,
            headline text,
            bio text,
            location text,
            last_seen_text text,
            status text,
            is_admin boolean NOT NULL DEFAULT false,
            can_send_message boolean NOT NULL DEFAULT true,
            raw_payload jsonb,
            fetched_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT members_account_id_accounts_id_fk
                FOREIGN KEY (account_id) REFERENCES circle_dm.accounts(id) ON DELETE CASCADE,
            CONSTRAINT uniq_account_member UNIQUE (account_id, circle_community_member_id)
        )
    """)
    op.execute("CREATE INDEX idx_members_name ON circle_dm.members (account_id, name)")

    op.execute("""
        CREATE TABLE circle_dm.sent_messages (
            id bigserial PRIMARY KEY,
            thread_id bigint NOT NULL,
            body text NOT NULL,
            circle_message_id bigint,
            circle_creation_uuid uuid,
            sent_at timestamptz NOT NULL DEFAULT now(),
            draft_session_id bigint,
            error text,
            CONSTRAINT sent_messages_thread_id_threads_id_fk
                FOREIGN KEY (thread_id) REFERENCES circle_dm.threads(id) ON DELETE CASCADE,
            CONSTRAINT sent_messages_draft_session_id_draft_sessions_id_fk
                FOREIGN KEY (draft_session_id)
                REFERENCES circle_dm.draft_sessions(id) ON DELETE SET NULL
        )
    """)

    op.execute("""
        CREATE TABLE circle_dm.settings (
            id integer PRIMARY KEY DEFAULT 1,
            global_meta_prompt text NOT NULL DEFAULT '',
            format_prompt text NOT NULL DEFAULT '',
            draft_model text,
            format_model text,
            no_reply_threshold_days integer NOT NULL DEFAULT 3,
            silence_threshold_days integer NOT NULL DEFAULT 14,
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT settings_singleton CHECK (id = 1)
        )
    """)

    op.execute("""
        CREATE TABLE circle_dm.kb_documents (
            id bigserial PRIMARY KEY,
            scope circle_dm.kb_scope NOT NULL,
            account_id bigint,
            title text NOT NULL,
            body_text text NOT NULL DEFAULT '',
            source_kind circle_dm.kb_source_kind NOT NULL,
            original_filename text,
            original_mime text,
            original_data_b64 text,
            token_estimate integer NOT NULL DEFAULT 0,
            enabled boolean NOT NULL DEFAULT true,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT kb_documents_account_id_accounts_id_fk
                FOREIGN KEY (account_id) REFERENCES circle_dm.accounts(id) ON DELETE CASCADE
        )
    """)
    op.execute("CREATE INDEX idx_kb_scope ON circle_dm.kb_documents (scope)")
    op.execute("CREATE INDEX idx_kb_account ON circle_dm.kb_documents (account_id)")

    op.execute("""
        CREATE TABLE circle_dm.assistant_conversations (
            id bigserial PRIMARY KEY,
            user_id bigint NOT NULL,
            title text,
            last_message_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT assistant_conversations_user_id_users_id_fk
                FOREIGN KEY (user_id) REFERENCES admin.users(id) ON DELETE CASCADE
        )
    """)
    op.execute(
        "CREATE INDEX idx_asst_conv_auth"
        " ON circle_dm.assistant_conversations (user_id, last_message_at)"
    )

    op.execute("""
        CREATE TABLE circle_dm.assistant_messages (
            id bigserial PRIMARY KEY,
            conversation_id bigint NOT NULL,
            role circle_dm.assistant_msg_role NOT NULL,
            content text NOT NULL,
            raw_content text,
            context_snapshot jsonb,
            action_proposal jsonb,
            applied_at timestamptz,
            apply_error text,
            tokens_used integer,
            cost_usd numeric(10,6),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT assistant_messages_conversation_id_assistant_conversations_id_fk
                FOREIGN KEY (conversation_id)
                REFERENCES circle_dm.assistant_conversations(id) ON DELETE CASCADE
        )
    """)
    op.execute(
        "CREATE INDEX idx_asst_msg_conv"
        " ON circle_dm.assistant_messages (conversation_id, created_at)"
    )

    # ── funkcja + triggery updated_at (doslownie jak w migrate.ts) ───────────

    op.execute("""
        CREATE OR REPLACE FUNCTION public.update_updated_at_column()
        RETURNS TRIGGER AS $$
        BEGIN
          NEW.updated_at = NOW();
          RETURN NEW;
        END;
        $$ language 'plpgsql'
    """)
    for trigger_name, table in TRIGGER_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {trigger_name} ON {table}")
        op.execute(
            f"CREATE TRIGGER {trigger_name} BEFORE UPDATE ON {table}\n"
            "  FOR EACH ROW EXECUTE PROCEDURE public.update_updated_at_column()"
        )

    # ── seedy ────────────────────────────────────────────────────────────────

    op.execute("INSERT INTO circle_dm.settings (id) VALUES (1) ON CONFLICT DO NOTHING")
    # Tylko gdy nadal pusty - celowo wyczyszczony ma zostac pusty.
    op.execute(
        "UPDATE circle_dm.settings SET format_prompt = $seed$"
        + FORMAT_PROMPT_SEED
        + "$seed$ WHERE id = 1 AND format_prompt = ''"
    )


def downgrade() -> None:
    for trigger_name, table in TRIGGER_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {trigger_name} ON {table}")
    op.execute("DROP FUNCTION IF EXISTS public.update_updated_at_column()")
    op.execute("DROP SCHEMA circle_dm CASCADE")
    op.execute("DROP SCHEMA admin CASCADE")
