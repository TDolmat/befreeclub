"""DTO modulu circle_dm. Baza CamelModel w app/core/schemas.py.

DTO route'ow dopisuja agenci routes-a / routes-b (kontrakt JSON w
docs/spec/routes-a.md, routes-b.md). Nazwy pol JSON zostaja STARE
(adminAccountId itd.) mimo nowych nazw kolumn DB (account_id) - uzywaj
jawnych aliasow tam, gdzie auto-camelCase nie wystarcza.

Konwencja walidacji (port zod):
- z.coerce.number().int().positive() -> CoercedId (pydantic lax koercuje "5"),
- z.number().int().positive() (bez coerce) -> StrictId (string NIE przechodzi),
- z.string().optional() -> pole typu str z default=None (jawny null odrzucany,
  brak klucza OK) + odczyt przez model_fields_set.
"""

import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, StrictBool, StrictInt, field_validator, model_validator

from app.core.schemas import ZOD_EMAIL_RE, CamelModel, IsoDateTime, NumericStr, dump

__all__ = ["CamelModel", "IsoDateTime", "NumericStr", "dump"]

CoercedId = Annotated[int, Field(gt=0)]
StrictId = Annotated[StrictInt, Field(gt=0)]

# Regex z.string().datetime() z zod 3.25 (precision=null, offset=false, local=false):
# tylko UTC z sufiksem Z (offsety typu +02:00 odrzucane), data kalendarzowo poprawna.
ZOD_DATETIME_RE = re.compile(
    r"^((\d\d[2468][048]|\d\d[13579][26]|\d\d0[48]|[02468][048]00|[13579][26]00)-02-29"
    r"|\d{4}-((0[13578]|1[02])-(0[1-9]|[12]\d|3[01])"
    r"|(0[469]|11)-(0[1-9]|[12]\d|30)"
    r"|(02)-(0[1-9]|1\d|2[0-8])))"
    r"T([01]\d|2[0-3]):[0-5]\d:[0-5]\d(\.\d+)?(Z)$"
)


def parse_zod_datetime(value: str) -> datetime:
    """Odpowiednik new Date(value) w JS: frakcja sekund obcinana do milisekund."""
    base, _, rest = value.partition(".")
    if rest:
        ms = rest[:-1][:3].ljust(3, "0")
        return datetime.fromisoformat(f"{base}.{ms}+00:00")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# ─── drafts (routes-b) ───────────────────────────────────────────────────────


class UpdateDraftRequest(CamelModel):
    draft: str


class SendDraftRequest(CamelModel):
    body: str = Field(min_length=1)


# ─── compose (routes-b) ──────────────────────────────────────────────────────


class ComposeGenerateRequest(CamelModel):
    admin_account_id: CoercedId
    circle_community_member_id: CoercedId


class ComposeSendRequest(CamelModel):
    admin_account_id: CoercedId
    circle_community_member_id: CoercedId
    body: str = Field(min_length=1)


# ─── format (routes-b) ───────────────────────────────────────────────────────


class FormatThreadRequest(CamelModel):
    thread_id: CoercedId
    text: str = Field(min_length=1)


class FormatComposeRequest(CamelModel):
    admin_account_id: CoercedId
    circle_community_member_id: CoercedId
    text: str = Field(min_length=1)


class FormatBulkRequest(CamelModel):
    admin_account_id: CoercedId
    text: str = Field(min_length=1)


# ─── bulk (routes-b) - liczby BEZ coerce, jak w oryginale ────────────────────


class BulkThreadItem(CamelModel):
    kind: Literal["thread"]
    thread_id: StrictId


class BulkMemberItem(CamelModel):
    kind: Literal["member"]
    admin_account_id: StrictId
    member_id: StrictId


BulkSendItem = Annotated[BulkThreadItem | BulkMemberItem, Field(discriminator="kind")]


class BulkSendRequest(CamelModel):
    items: list[BulkSendItem] = Field(min_length=1, max_length=100)
    body: str = Field(min_length=1)


# ─── kb (routes-b) ───────────────────────────────────────────────────────────


class CreateKbManualRequest(CamelModel):
    scope: Literal["global", "account"]
    admin_account_id: StrictId | None = None
    title: str = Field(min_length=1, max_length=200)
    body_text: str = Field(min_length=1, max_length=500_000)


class UpdateKbRequest(CamelModel):
    title: str = Field(default=None, min_length=1, max_length=200)
    body_text: str = Field(default=None, max_length=500_000)
    enabled: StrictBool = Field(default=None)

    @model_validator(mode="after")
    def _at_least_one_field(self) -> "UpdateKbRequest":
        if not (self.model_fields_set & {"title", "body_text", "enabled"}):
            raise ValueError("at least one field required")
        return self


# ─── assistant (routes-b) - kontekst tury (port assistantContextSchema) ──────


class AssistantInboxContext(CamelModel):
    kind: Literal["inbox"]
    admin_account_id: StrictId | None
    filter: str
    sort: str
    query: str


class AssistantThreadContext(CamelModel):
    kind: Literal["thread"]
    admin_account_id: StrictId
    thread_id: StrictId
    recipient_name: str | None
    persona: str
    account_label: str
    draft_text: str
    history_excerpt: str


class AssistantComposeContext(CamelModel):
    kind: Literal["compose"]
    admin_account_id: StrictId
    member_id: StrictId
    member_name: str
    persona: str
    account_label: str
    current_text: str
    member_profile: str


class AssistantSettingsContext(CamelModel):
    kind: Literal["settings"]
    meta_prompt: str
    format_prompt: str


class AssistantAccountContext(CamelModel):
    kind: Literal["account"]
    account_id: StrictId
    label: str
    persona_text: str


class AssistantNoneContext(CamelModel):
    kind: Literal["none"]


AssistantContext = Annotated[
    AssistantInboxContext
    | AssistantThreadContext
    | AssistantComposeContext
    | AssistantSettingsContext
    | AssistantAccountContext
    | AssistantNoneContext,
    Field(discriminator="kind"),
]


class AssistantTurnRequest(CamelModel):
    conversation_id: StrictId
    message: str = Field(min_length=1, max_length=4000)
    context: AssistantContext


class AssistantCancelRequest(CamelModel):
    conversation_id: StrictId


# ─── accounts (routes-a) ─────────────────────────────────────────────────────


class CreateAdminAccountBody(CamelModel):
    label: str = Field(min_length=1, max_length=120)
    email: str
    circle_admin_token: str = Field(default=None, min_length=8)
    system_prompt: str = Field(min_length=10)

    @field_validator("email")
    @classmethod
    def _validate_email(cls, v: str) -> str:
        if not ZOD_EMAIL_RE.match(v):
            raise ValueError("Invalid email")
        return v


class UpdateAdminAccountBody(CamelModel):
    label: str = Field(default=None, min_length=1, max_length=120)
    email: str = Field(default=None)
    circle_admin_token: str = Field(default=None, min_length=8)
    system_prompt: str = Field(default=None, min_length=10)
    is_active: StrictBool = Field(default=None)

    @field_validator("email")
    @classmethod
    def _validate_email(cls, v: str) -> str:
        if not ZOD_EMAIL_RE.match(v):
            raise ValueError("Invalid email")
        return v


# ─── threads (routes-a) ──────────────────────────────────────────────────────


class ThreadStatusBody(CamelModel):
    status: Literal["inbox", "done"]


class ThreadFlagBody(CamelModel):
    is_flagged: StrictBool


class BulkActionBody(CamelModel):
    admin_account_id: StrictId
    ids: list[StrictId] = Field(min_length=1, max_length=500)
    action: Literal["done", "inbox", "flag", "unflag"]


class CreateCheckupBody(CamelModel):
    due_at: str
    note: str | None = Field(default=None, max_length=500)

    @field_validator("due_at")
    @classmethod
    def _validate_due_at(cls, v: str) -> str:
        if not ZOD_DATETIME_RE.match(v):
            raise ValueError("Invalid datetime")
        return v


# ─── members (routes-a) ──────────────────────────────────────────────────────


class MembersSyncBody(CamelModel):
    admin_account_id: CoercedId


# ─── settings (routes-a) ─────────────────────────────────────────────────────


class UpdateSettingsBody(CamelModel):
    global_meta_prompt: str = Field(default=None, max_length=10_000)
    format_prompt: str = Field(default=None, max_length=20_000)
    draft_model: str | None = Field(default=None, max_length=120)
    format_model: str | None = Field(default=None, max_length=120)
    no_reply_threshold_days: StrictInt = Field(default=None, ge=1, le=90)
    silence_threshold_days: StrictInt = Field(default=None, ge=1, le=365)

    @model_validator(mode="after")
    def _at_least_one_field(self) -> "UpdateSettingsBody":
        if not self.model_fields_set:
            raise ValueError("at least one field required")
        return self
