"""Wspolna baza DTO. JSON API w camelCase, zamrozony 1:1 ze starym Hono.

Zasady serializacji (patrz docs/spec/port-kontrakt.md):
- datetime -> Date.toISOString() z Node: YYYY-MM-DDTHH:MM:SS.sssZ (typ IsoDateTime),
- Decimal (numeric w PG) -> string, jak postgres-js (typ NumericStr),
- pola nullable obecne jako null; pola opcjonalne (TS `field?`) pomijane -
  rozrozniaj przez exclude_unset/reczne dicty, NIE exclude_none.
"""

import re
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, PlainSerializer
from pydantic.alias_generators import to_camel

from app.core.logging import to_iso_string

# Regex z.string().email() z zod 3.25 - 1:1 z walidacja oryginalu.
# Lookaheady nie dzialaja w pydantic Field(pattern=...) (silnik Rust) -
# uzywaj field_validatora z ZOD_EMAIL_RE.match(...).
ZOD_EMAIL_RE = re.compile(
    r"^(?!\.)(?!.*\.\.)([A-Z0-9_'+\-\.]*)[A-Z0-9_+-]@([A-Z0-9][A-Z0-9\-]*\.)+[A-Z]{2,}$",
    re.IGNORECASE,
)

IsoDateTime = Annotated[datetime, PlainSerializer(to_iso_string, return_type=str, when_used="json")]

NumericStr = Annotated[Decimal, PlainSerializer(lambda v: str(v), return_type=str, when_used="json")]


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)


def dump(model: BaseModel, **kwargs: Any) -> dict[str, Any]:
    """Serializacja DTO do JSON-owalnego dicta z aliasami camelCase."""
    return model.model_dump(mode="json", by_alias=True, **kwargs)
