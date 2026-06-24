"""Status polaczen z zewnetrznymi API (sekcja "Polaczenia API" panelu admina).

Montowane pod /api/admin/connections ZA require_auth (main.py).

4 integracje sa EDYTOWALNE (openai, resend, sender, metaCapi): klucz da sie
ustawic/wyczyscic w panelu (zaszyfrowany Fernetem), env jest fallbackiem. GET
status zwraca tylko MASKE efektywnej wartosci - pelna wartosc wychodzi WYLACZNIE
przez swiadomy reveal endpoint za auth. Stripe/Circle sa status-only z env.

- GET    /api/admin/connections               status wszystkich API (tani listing).
         ?test=1 dodatkowo odpala test-call dla kazdego API z dostepnym testem.
- POST   /api/admin/connections/{key}/test     reczny pojedynczy test polaczenia.
- PUT    /api/admin/connections/{key}/secret   {value} ustaw sekret (tylko edytowalne).
- DELETE /api/admin/connections/{key}/secret   wyczysc sekret -> powrot na env.
- GET    /api/admin/connections/{key}/secret/reveal  pelna wartosc (swiadomy odczyt).
"""

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from app.core import secret_box
from app.core.logging import create_logger
from app.modules.admin.services import connections as svc
from app.modules.admin.services.auth import AuthContext, require_auth

log = create_logger("routes:admin-connections")

router = APIRouter()


@router.get("")
async def get_connections(request: Request) -> JSONResponse:
    run_tests = request.query_params.get("test") == "1"
    results = await svc.list_connections(run_tests=run_tests)
    return JSONResponse({"connections": [r.to_json() for r in results]})


@router.post("/{key}/test")
async def post_connection_test(key: str) -> JSONResponse:
    result = await svc.test_connection(key)
    if result is None:
        raise HTTPException(status_code=404, detail="Connection not found")
    return JSONResponse({"connection": result.to_json()})


@router.put("/{key}/secret")
async def put_connection_secret(
    key: str,
    body: dict[str, Any] = Body(default_factory=dict),
    auth: AuthContext = Depends(require_auth),
) -> JSONResponse:
    """Ustaw klucz edytowalnej integracji. Odpowiedz = status integracji z maska,
    NIGDY pelna wartosc. Nieedytowalny/nieznany key -> 404, pusta wartosc -> 400,
    brak master key (szyfrowanie niedostepne) -> 400 bez crash."""
    value = body.get("value")
    if not isinstance(value, str) or not value.strip():
        return JSONResponse({"error": "wartosc nie moze byc pusta"}, status_code=400)
    try:
        await svc.set_secret(key, value.strip(), auth.auth_account_id)
    except svc.ConnectionNotEditable:
        raise HTTPException(status_code=404, detail="Connection not found") from None
    except secret_box.SecretBoxUnavailable:
        return JSONResponse(
            {"error": "szyfrowanie sekretow niedostepne (brak SECRETS_MASTER_KEY)"},
            status_code=400,
        )
    log.info(f"secret {key} set by {auth.email}")
    result = await svc.get_connection_status(key)
    if result is None:  # nie powinno sie zdarzyc po udanym set
        raise HTTPException(status_code=404, detail="Connection not found")
    return JSONResponse({"connection": result.to_json()})


@router.delete("/{key}/secret")
async def delete_connection_secret(
    key: str,
    auth: AuthContext = Depends(require_auth),
) -> JSONResponse:
    """Wyczysc klucz edytowalnej integracji -> powrot na env. Odpowiedz = status."""
    try:
        await svc.clear_secret(key, auth.auth_account_id)
    except svc.ConnectionNotEditable:
        raise HTTPException(status_code=404, detail="Connection not found") from None
    log.info(f"secret {key} cleared by {auth.email}")
    result = await svc.get_connection_status(key)
    if result is None:
        raise HTTPException(status_code=404, detail="Connection not found")
    return JSONResponse({"connection": result.to_json()})


@router.get("/{key}/secret/reveal")
async def reveal_connection_secret(
    key: str,
    auth: AuthContext = Depends(require_auth),
) -> JSONResponse:
    """Pelna efektywna wartosc sekretu - swiadomy odczyt za auth. 404 dla
    nieedytowalnych (stripe/circle)."""
    try:
        value = await svc.reveal_secret(key)
    except svc.ConnectionNotEditable:
        raise HTTPException(status_code=404, detail="Connection not found") from None
    log.info(f"secret {key} revealed by {auth.email}")
    return JSONResponse({"value": value})
