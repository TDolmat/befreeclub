"""DTO modulu newsletter - agent [newsletter-contact].
Baza: CamelModel z app.core.schemas.

Pola wejsciowe maja defaulty "" - walidacja z komunikatami PL 1:1
z oryginalu zyje w handlerach (routes/public.py), nie w pydantic
(brak pola = "Niepoprawne imię", nie generyczny "Invalid request").
"""

import re
import uuid

from app.core.schemas import CamelModel, IsoDateTime

# Regex emaila 1:1 z edge functions (newsletter-subscribe, send-contact-email).
SIMPLE_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class SubscribeIn(CamelModel):
    name: str = ""
    email: str = ""


class ConfirmIn(CamelModel):
    token: str = ""


class ContactIn(CamelModel):
    name: str = ""
    email: str = ""
    message: str = ""


class ContactMessageOut(CamelModel):
    id: uuid.UUID
    name: str
    email: str
    message: str
    created_at: IsoDateTime
