"""DTO modulu admin (auth/feedback/health). Baza CamelModel w app/core/schemas.py."""

from pydantic import Field, field_validator

from app.core.schemas import ZOD_EMAIL_RE, CamelModel, IsoDateTime, dump

__all__ = ["CamelModel", "IsoDateTime", "dump", "LoginRequest", "LoginOkResponse", "MeResponse"]


class LoginRequest(CamelModel):
    """Body POST /api/auth/login - walidacja 1:1 z zod z.string().email() (400 przy bledzie)."""

    email: str = Field(max_length=320)
    password: str = Field(min_length=1, max_length=512)

    @field_validator("email")
    @classmethod
    def _validate_email(cls, v: str) -> str:
        if not ZOD_EMAIL_RE.match(v):
            raise ValueError("Invalid email")
        return v


class LoginOkResponse(CamelModel):
    ok: bool
    email: str


class MeResponse(CamelModel):
    """GET /api/auth/me: pole email POMIJANE (nie null) gdy nieuwierzytelniony -
    serializuj przez dump(model, exclude_none=True)."""

    authenticated: bool
    email: str | None = None
