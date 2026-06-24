"""faza 2.2 fundament: tabela admin.settings (centralny store) + bezpieczne seedy

Generyczny key/value store ustawien panelu admina - podwalina przyszlej sekcji
"Ustawienia". Pierwszy uzytkownik: bramki destrukcyjnych workerow czlonkostw
(cleanup / klarna_reconcile / invite_retry), patrz docs/spec-landing/cleanup-controls.md.

ZELAZNA ZASADA: swiezy deploy NIE moze nikogo usunac. Seed wpisuje bezpieczne
domysly (enabled=false, dryRun=true) i jest WARUNKOWY (ON CONFLICT DO NOTHING),
zeby ponowny upgrade nie nadpisal recznych zmian usera w panelu.

Trigger updated_at jak inne tabele (public.update_updated_at_column z 0001).

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-18
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE admin.settings (
            key text PRIMARY KEY,
            value jsonb NOT NULL,
            updated_at timestamptz NOT NULL DEFAULT now(),
            updated_by_user_id bigint,
            CONSTRAINT settings_updated_by_user_id_fk
                FOREIGN KEY (updated_by_user_id)
                REFERENCES admin.users(id) ON DELETE SET NULL
        )
    """)

    # Trigger updated_at: funkcja public.update_updated_at_column istnieje od 0001.
    op.execute("DROP TRIGGER IF EXISTS set_settings_updated_at ON admin.settings")
    op.execute(
        "CREATE TRIGGER set_settings_updated_at BEFORE UPDATE ON admin.settings\n"
        "  FOR EACH ROW EXECUTE PROCEDURE public.update_updated_at_column()"
    )

    # ── seed bezpiecznych domyslow (warunkowy) ───────────────────────────────
    # ON CONFLICT DO NOTHING: ponowny upgrade NIE nadpisze tego, co user wlaczyl
    # recznie w panelu. Brak wiersza = serwis i tak zwraca bezpieczny default.
    op.execute("""
        INSERT INTO admin.settings (key, value) VALUES
            ('members.cleanup', '{"enabled": false, "dryRun": true}'::jsonb),
            ('members.klarna_reconcile', '{"enabled": false}'::jsonb),
            ('members.invite_retry', '{"enabled": false}'::jsonb)
        ON CONFLICT (key) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS set_settings_updated_at ON admin.settings")
    op.execute("DROP TABLE admin.settings")
