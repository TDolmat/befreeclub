"""GET /health i /health/claude (port z index.ts + claude-health.ts).
Publiczne celowo - monitoring zewnetrzny (n8n, Uptime Kuma)."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.modules.admin.services.claude_health import get_claude_health

router = APIRouter()


@router.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"ok": True, "version": "0.1.0"})


@router.get("/health/claude")
async def health_claude(request: Request) -> JSONResponse:
    # Deep probe tylko gdy query deep ma DOSLOWNIE wartosc "1".
    deep = request.query_params.get("deep") == "1"
    result = await get_claude_health(deep=deep)
    return JSONResponse(result, status_code=200 if result["ok"] else 503)
