"""faza 2.2: tabela admin.encrypted_secrets (edytowalne sekrety integracji)

4 klucze API integracji (openai, resend, sender, meta capi) edytowalne w panelu
admina, zaszyfrowane Fernetem (app/core/secret_box.py). W bazie siedzi WYLACZNIE
ciphertext - wartosc jawna nigdy nie trafia do bazy ani logow. Env pozostaje
opcjonalnym fallbackiem (brak wiersza = serwis schodzi na env).

Brak seeda: pusta tabela = wszystko z env (zachowanie 1:1). Trigger updated_at
jak inne tabele (public.update_updated_at_column z 0001).

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-23
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE admin.encrypted_secrets (
            key text PRIMARY KEY,
            ciphertext text NOT NULL,
            updated_at timestamptz NOT NULL DEFAULT now(),
            updated_by_user_id bigint,
            CONSTRAINT encrypted_secrets_updated_by_user_id_fk
                FOREIGN KEY (updated_by_user_id)
                REFERENCES admin.users(id) ON DELETE SET NULL
        )
    """)

    # Trigger updated_at: funkcja public.update_updated_at_column istnieje od 0001.
    op.execute("DROP TRIGGER IF EXISTS set_encrypted_secrets_updated_at ON admin.encrypted_secrets")
    op.execute(
        "CREATE TRIGGER set_encrypted_secrets_updated_at"
        " BEFORE UPDATE ON admin.encrypted_secrets\n"
        "  FOR EACH ROW EXECUTE PROCEDURE public.update_updated_at_column()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS set_encrypted_secrets_updated_at ON admin.encrypted_secrets")
    op.execute("DROP TABLE admin.encrypted_secrets")
