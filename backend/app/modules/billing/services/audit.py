"""Audyt akcji adminow na subskrybentach -> billing.audit_log.

Sygnatura ZAMROZONA w port-kontrakt-2.md sekcja 4 (plik z kontraktu
[admin-api]; powstal przy [billing-lifecycle], bo akcje pauzy/przedluzenia/
anulowania musza byc audytowane od pierwszego dnia).
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.billing.models import AuditLog


async def log_action(
    session: AsyncSession,
    *,
    admin_user_id: int | None,
    action: str,
    target_email: str | None,
    payload: dict,
) -> None:
    """INSERT bez commitu - caller commituje razem z reszta operacji.

    W dev require_auth daje DEV_FAKE_AUTH (id=0, nie istnieje w admin.users) -
    wtedy przekazuj admin_user_id=None (kolumna nullable wlasnie po to).
    """
    session.add(
        AuditLog(
            admin_user_id=admin_user_id,
            action=action,
            target_email=target_email,
            payload=payload,
        )
    )
