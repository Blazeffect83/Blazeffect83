"""FastAPI application factory and process entrypoint."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .api.authentication import Auth
from .api.routes import apply_budget_overrides, build_router
from .config import Settings, load_settings
from .dashboard.views import build_dashboard
from .logging_config import setup_logging
from .scheduler.worker import Worker
from .services import Services, build_services

log = logging.getLogger(__name__)
MAX_BODY = 2 * 1024 * 1024

SECURITY_HEADERS = {
    "content-security-policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                               "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
    "x-frame-options": "DENY",
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
    "cache-control": "no-store",
}


def create_app(settings: Settings | None = None, services: Services | None = None,
               start_worker: bool = True) -> FastAPI:
    settings = settings or (services.settings if services else load_settings())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if app.state.worker and start_worker:
            app.state.worker.start()
            log.info("AEGIS worker started")
        yield
        if app.state.worker and start_worker:
            app.state.worker.stop()
            log.info("AEGIS worker stopped")

    app = FastAPI(title="AEGIS", version="1.0.0", lifespan=lifespan, docs_url="/api/docs", redoc_url=None,
                  openapi_url="/api/openapi.json")
    svc = services or build_services(settings)
    apply_budget_overrides(svc)
    app.state.services = svc
    app.state.auth = Auth(svc.db, svc.settings)
    app.state.worker = Worker(svc)

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts or ["*"])

    @app.middleware("http")
    async def guard(request: Request, call_next):
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > MAX_BODY:
            return JSONResponse({"detail": "request body too large"}, status_code=413)
        resp = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            if k == "content-security-policy" and request.url.path.startswith("/api/docs"):
                continue  # Swagger UI loads its own assets
            resp.headers.setdefault(k, v)
        if settings.cookie_secure:
            resp.headers.setdefault("strict-transport-security", "max-age=31536000")
        return resp

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        accepts_html = "text/html" in request.headers.get("accept", "")
        if exc.status_code == 401 and accepts_html and not request.url.path.startswith("/api/"):
            return RedirectResponse("/login", status_code=303)
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "dashboard" / "static")), name="static")
    app.include_router(build_router())
    app.include_router(build_dashboard())
    return app


def run() -> None:  # pragma: no cover - exercised on the device
    import uvicorn
    settings = load_settings()
    settings.ensure_dirs()
    setup_logging(settings)
    if settings.bind_host not in ("127.0.0.1", "::1", "localhost") and not settings.admin_password_hash:
        raise SystemExit("refusing to listen on a non-loopback address without an admin password "
                         "(run `aegis set-password`)")
    app = create_app(settings)
    uvicorn.run(app, host=settings.bind_host, port=settings.bind_port, log_level="info", access_log=False,
                proxy_headers=False, timeout_graceful_shutdown=30)
