"""Centralna sekcja Ustawienia panelu admina - GET wszystkiego + PUT per grupa.

Kontrakt: docs/spec-landing/ustawienia-katalog.md sekcja 7. Montowane pod
/api/admin/settings (za require_auth). Knoby zyja w katalogu (settings_catalog.py);
ten route tylko montuje GET/PUT i tlumaczy bledy walidacji na 400 {"error":...}.

Prompty/modele/progi circle_dm maja WLASNE API (/api/circle-dm/settings) i NIE sa
duplikowane tutaj - jedno zrodlo prawdy. Sekrety nie wychodza (osobny endpoint
statusu polaczen).
"""

from typing import Any

from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse

from app.core.logging import create_logger
from app.modules.admin.services import settings_catalog as catalog
from app.modules.admin.services.auth import AuthContext, require_auth

log = create_logger("routes:admin-settings")

router = APIRouter()

_GROUPS = set(catalog.CATALOG.keys())


@router.get("")
async def get_settings() -> dict:
    """Wszystkie edytowalne knoby (TOGGLE + TUNABLE) pogrupowane po sekcjach
    panelu, z efektywnymi wartosciami (DB > env > safe default). Bez sekretow."""
    return await catalog.build_all_groups()


@router.put("/{group}")
async def update_settings_group(
    group: str,
    body: dict[str, Any] = Body(default_factory=dict),
    auth: AuthContext = Depends(require_auth),
) -> Any:
    """Czesciowy patch jednej grupy. Pomijasz klucz = bez zmian. Zwraca stan grupy
    po zapisie. Zly typ/zakres/nieznany klucz -> 400 {"error": <realny powod>}.

    Komunikat walidacji odmaskowany (np. "must be >= 5000", "must be a valid URL"):
    panel mapuje go na podpowiedz glosem marki. Walidatory nie dotykaja sekretow,
    wiec tresc bledu jest bezpieczna do oddania."""
    if group not in _GROUPS:
        return JSONResponse({"error": "Unknown settings group"}, status_code=404)
    try:
        # auth_account_id z dev bypassu (0) traktujemy jak brak - set_setting mapuje na NULL.
        result = await catalog.apply_patch(group, body, auth.auth_account_id)  # type: ignore[arg-type]
    except catalog.SettingsValidationError as err:
        log.warn(f"settings PUT {group} rejected: {err}")
        return JSONResponse({"error": str(err)}, status_code=400)
    log.info(f"settings group {group} updated by {auth.email} ({list(body.keys())})")
    return result
