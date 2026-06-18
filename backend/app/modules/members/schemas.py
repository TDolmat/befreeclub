"""DTO modulu members. Fundament zostawia puste - dopisuja agenci
[members] (wyniki provisioningu) i [admin-api] (listing/karta czlonka).
Baza: CamelModel z app.core.schemas."""

from datetime import datetime

from app.core.schemas import CamelModel, IsoDateTime

# ── [members]: listing + akcje admina (/api/members) ─────────────────────────

MEMBER_STATUSES = ("invited", "active", "paused", "pending_removal", "removed", "invite_failed")
MEMBER_SOURCES = ("subscription", "one_time", "manual")


class MemberOut(CamelModel):
    id: int
    email: str
    name: str | None
    circle_member_id: str | None
    status: str
    protected: bool
    source: str
    expires_at: IsoDateTime | None
    created_at: IsoDateTime
    updated_at: IsoDateTime


class MemberListOut(CamelModel):
    members: list[MemberOut]


class MemberCreateIn(CamelModel):
    """Manual provisioning (POST /api/members): source=manual; bez expires_at
    czlonek jest pomijany przez cleanup (naprawa quirka 'manual wywalany
    przez cron'). protected=True od razu chroni konto (uwaga z kontraktu #19)."""

    email: str
    name: str | None = None
    expires_at: datetime | None = None
    skip_invitation: bool = False
    protected: bool = False


class ReinviteIn(CamelModel):
    # Default True jak w UI oryginalnego /admin ("juz ma konto w Circle",
    # invite bez maila zaproszeniowego).
    skip_invitation: bool = True


class ProtectIn(CamelModel):
    protected: bool


class ProvisionOut(CamelModel):
    member_id: int
    email: str
    circle_invited: bool
    circle_member_id: str | None
    already_active: bool

# ── [admin-api]: dopisuje swoje DTO PONIZEJ, nie ruszajac powyzszych ─────────
