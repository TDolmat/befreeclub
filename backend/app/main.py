"""Port apps/server/src/index.ts: create_app + lifespan.

Kolejnosc bootu wg docs/spec/core-infra.md. Migracje NIE leca przy starcie
appki - robi to entrypoint kontenera (alembic upgrade head && uvicorn).
Architektura zaklada JEDEN proces/worker (WS broker, semafory, cache in-memory).
"""

import asyncio
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import settings
from app.core.db import async_session_maker, engine
from app.core.dev_mode import log_startup_mode
from app.core.logging import create_logger
from app.core.ws import broker
from app.modules.admin.routes import auth as auth_routes
from app.modules.admin.routes import feedback as feedback_routes
from app.modules.admin.routes import health as health_routes
from app.modules.admin.services.auth import require_auth
from app.modules.admin.services.sessions import purge_expired_sessions
from app.modules.billing.routes import admin as billing_admin
from app.modules.billing.routes import (
    cancellation as billing_cancellation,
)
from app.modules.billing.routes import (
    checkout as billing_checkout,
)
from app.modules.billing.routes import (
    ebook as billing_ebook,
)
from app.modules.billing.routes import (
    payment_method as billing_payment_method,
)
from app.modules.billing.routes import (
    plans as billing_plans,
)
from app.modules.billing.routes import (
    promo as billing_promo,
)
from app.modules.billing.routes import (
    webhooks as billing_webhooks,
)
from app.modules.billing.routes import (
    workers as billing_workers,
)
from app.modules.billing.services.klarna_reconcile_worker import (
    start_klarna_reconcile_worker,
    stop_klarna_reconcile_worker,
)
from app.modules.circle_dm.models import Account
from app.modules.circle_dm.routes import (
    accounts,
    assistant,
    bulk,
    compose,
    drafts,
    format,
    kb,
    members,
    messages,
    threads,
)
from app.modules.circle_dm.routes import (
    settings as settings_routes,
)
from app.modules.circle_dm.services.image_description_worker import (
    start_image_description_worker,
    stop_image_description_worker,
)
from app.modules.circle_dm.services.polling_worker import start_polling, stop_polling
from app.modules.circle_dm.services.voice_transcript_worker import (
    start_voice_transcript_worker,
    stop_voice_transcript_worker,
)
from app.modules.landing.routes import public as landing_public
from app.modules.members.routes import admin as members_admin
from app.modules.members.routes import maintenance as members_maintenance
from app.modules.members.services.cleanup_worker import (
    start_cleanup_worker,
    stop_cleanup_worker,
)
from app.modules.members.services.invite_retry_worker import (
    start_invite_retry_worker,
    stop_invite_retry_worker,
)
from app.modules.newsletter.routes import public as newsletter_public

log = create_logger("server")

DEFAULT_PERSONA = """Jesteś sprawnie piszącym współzałożycielem klubu Be Free Club.
- Piszesz po polsku, mówionym tonem, w pierwszej osobie.
- Krótko i naturalnie. Nie korpomowa, nie chatbotowy "rozumiem, że...".
- Bez pompatycznego "z chęcią", bez emoji w UI, bez wykrzykników na siłę.
- Pomagasz, ale stawiasz granice. Pisze człowiek do człowieka."""


async def bootstrap_admin_if_requested() -> None:
    if not settings.BOOTSTRAP_ADMIN_EMAIL or not settings.BOOTSTRAP_ADMIN_TOKEN:
        return

    async with async_session_maker() as session:
        existing = (
            await session.execute(
                select(Account.id).where(Account.email == settings.BOOTSTRAP_ADMIN_EMAIL).limit(1)
            )
        ).first()
        if existing:
            log.info(f"Bootstrap admin already exists ({settings.BOOTSTRAP_ADMIN_EMAIL}), skipping")
            return

        session.add(
            Account(
                label=settings.BOOTSTRAP_ADMIN_LABEL or settings.BOOTSTRAP_ADMIN_EMAIL,
                email=settings.BOOTSTRAP_ADMIN_EMAIL,
                circle_admin_token=settings.BOOTSTRAP_ADMIN_TOKEN,
                system_prompt=DEFAULT_PERSONA,
            )
        )
        await session.commit()
    log.info(f"Bootstrapped admin account for {settings.BOOTSTRAP_ADMIN_EMAIL}")


async def _session_purge_loop() -> None:
    # Pierwszy tick dopiero PO godzinie (jak setInterval w Node), nie przy boocie.
    while True:
        await asyncio.sleep(60 * 60)
        try:
            n = await purge_expired_sessions()
            if n > 0:
                log.info(f"purged {n} expired session(s)")
        except Exception as err:
            log.warn(f"session purge failed: {err}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await bootstrap_admin_if_requested()
    except Exception as err:
        log.error("fatal startup error", str(err))
        raise

    log.info(f"HTTP server listening on http://localhost:{settings.PORT}")
    # Tryb lokalny: jeden WARN per zmockowany serwis (email/sender/circle
    # members) + status Meta CAPI. Na prod nic sie nie mockuje.
    log_startup_mode()

    start_polling()
    start_voice_transcript_worker()
    start_image_description_worker()
    # Workery fazy 2 (landing): zamiast publicznych cron-endpointow Supabase.
    start_cleanup_worker()
    start_klarna_reconcile_worker()
    start_invite_retry_worker()
    purge_task = asyncio.create_task(_session_purge_loop())

    yield

    async def _shutdown() -> None:
        stop_polling()
        stop_voice_transcript_worker()
        stop_image_description_worker()
        stop_cleanup_worker()
        stop_klarna_reconcile_worker()
        stop_invite_retry_worker()
        purge_task.cancel()
        await broker.close()
        await engine.dispose()

    log.info("Received shutdown, shutting down...")
    try:
        await asyncio.wait_for(_shutdown(), timeout=5)
    except TimeoutError:
        log.error("shutdown deadline exceeded")
        os._exit(1)


def create_app() -> FastAPI:
    app = FastAPI(lifespan=lifespan, openapi_url=None, docs_url=None, redoc_url=None)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["GET", "POST", "PATCH", "DELETE", "PUT", "OPTIONS"],
        allow_headers=["Content-Type"],
        allow_credentials=True,
    )

    @app.middleware("http")
    async def request_logger(request: Request, call_next):
        log.debug(f"<-- {request.method} {request.url.path}")
        start = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = round((time.perf_counter() - start) * 1000)
        log.debug(f"--> {request.method} {request.url.path} {response.status_code} {elapsed_ms}ms")
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        return JSONResponse({"error": "Invalid request"}, status_code=400)

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        detail = exc.detail if isinstance(exc.detail, str) else "Error"
        return JSONResponse({"error": detail}, status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception):
        log.error("unhandled error", str(exc))
        return JSONResponse({"error": str(exc)}, status_code=500)

    # Publiczne: /health, /health/claude, /api/auth/* (login bez sesji,
    # /me zwraca {authenticated:false} zamiast 401).
    app.include_router(health_routes.router)
    app.include_router(auth_routes.router, prefix="/api/auth")

    # Chronione: require_auth jako dependency (nie globalny middleware -
    # /api/auth musi zostac publiczne).
    protected = [Depends(require_auth)]
    app.include_router(feedback_routes.router, prefix="/api/feedback", dependencies=protected)
    for segment, module in (
        ("accounts", accounts),
        ("threads", threads),
        ("messages", messages),
        ("members", members),
        ("settings", settings_routes),
        ("drafts", drafts),
        ("compose", compose),
        ("format", format),
        ("bulk", bulk),
        ("kb", kb),
        ("assistant", assistant),
    ):
        app.include_router(
            module.router, prefix=f"/api/circle-dm/{segment}", dependencies=protected
        )

    # ── Faza 2: landing befreeclub.pl ─────────────────────────────────────
    # Publiczne (rate limit robia handlery per endpoint, patrz
    # docs/spec-landing/port-kontrakt-2.md sekcja 5):
    for segment, module in (
        ("plans", billing_plans),
        ("checkout", billing_checkout),
        ("promo", billing_promo),
        ("payment-method", billing_payment_method),
        ("ebook", billing_ebook),
        ("cancellation", billing_cancellation),
    ):
        app.include_router(module.router, prefix=f"/api/billing/{segment}")
    # Webhooki Stripe: publiczne, auth = podpis Stripe weryfikowany w handlerze.
    app.include_router(billing_webhooks.router, prefix="/api/billing/webhooks")
    app.include_router(newsletter_public.router, prefix="/api/newsletter")
    app.include_router(landing_public.router, prefix="/api/landing")
    # Admin (sesja panelu z fazy 1): akcje billingowe + caly modul members.
    app.include_router(billing_admin.router, prefix="/api/billing/admin", dependencies=protected)
    # Reczne triggery workerow ([workers]): POST .../workers/{name}/run.
    app.include_router(
        billing_workers.router, prefix="/api/billing/admin/workers", dependencies=protected
    )
    app.include_router(members_admin.router, prefix="/api/members", dependencies=protected)
    # One-off sync circle_member_id ([workers]): POST /api/members/sync-circle-ids.
    app.include_router(members_maintenance.router, prefix="/api/members", dependencies=protected)

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket) -> None:
        await broker.handle(websocket)

    if settings.WEB_DIST_PATH:
        root = Path(settings.WEB_DIST_PATH)
        log.info(f"Serving SPA from {root}")
        app.mount("/assets", StaticFiles(directory=root / "assets", check_dir=False))

        @app.get("/favicon.svg")
        async def favicon() -> FileResponse:
            return FileResponse(root / "favicon.svg")

        @app.get("/{full_path:path}")
        async def spa_fallback(request: Request, full_path: str):
            path = request.url.path
            if path.startswith("/api/") or path == "/ws":
                return PlainTextResponse("404 Not Found", status_code=404)
            # index.html czytany z dysku przy KAZDYM requeście (podmiana builda
            # bez restartu) - celowo bez cache.
            html = (root / "index.html").read_text(encoding="utf-8")
            return HTMLResponse(html)

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    # Jeden worker (stan in-memory) i WYLACZONE protokolowe pingi WS -
    # biblioteka ws w Node nie pinguje, port ma sie zachowywac tak samo.
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=settings.PORT,
        workers=1,
        ws_ping_interval=None,
        ws_ping_timeout=None,
        log_level="warning",
    )
