"""FastAPI application factory."""

from __future__ import annotations

import logging
import time
import traceback
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import generate_latest
from sqlalchemy import text
from sqlalchemy.orm.exc import StaleDataError

from .. import __version__
from ..core.config import get_settings
from ..core.db import get_engine
from ..core.errors import DomainError
from ..core.observability import HTTP_LATENCY, HTTP_REQUESTS, QUEUE_DEPTH, REGISTRY, configure_logging, limiter, request_id_var

log = logging.getLogger("monxuplan.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    st = get_settings()
    if st.is_sqlite:
        from ..core.db import create_all

        create_all()
    if st.worker_in_process:
        from ..worker import start_in_process

        start_in_process(st.worker_threads)
    from ..services.webhooks import start_delivery_thread

    start_delivery_thread()
    log.info("MonxuPlan API started", extra={"extra_data": {"version": __version__, "env": st.env}})
    yield


def create_app() -> FastAPI:
    st = get_settings()
    app = FastAPI(
        title="MonxuPlan API",
        version=__version__,
        description="Advanced Planning & Scheduling platform. Authentication: session cookie + CSRF header for browsers, "
        "`Authorization: Bearer <token|api key>` for integrations.",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(st.cors_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        request.state.request_id = rid
        request_id_var.set(rid)
        # rate limiting (per client IP; login has a stricter limit)
        ip = request.client.host if request.client else "?"
        path = request.url.path
        if path.startswith("/api/"):
            limit = st.login_rate_limit_per_minute if path.endswith("/auth/login") else st.rate_limit_per_minute
            key = f"{ip}:{'login' if path.endswith('/auth/login') else 'api'}"
            if not limiter.allow(key, limit):
                return JSONResponse({"error": {"code": "RATE_LIMITED", "message": "Too many requests. Please wait a moment."}}, status_code=429, headers={"Retry-After": "30"})
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > st.max_upload_mb * 1024 * 1024:
            return JSONResponse({"error": {"code": "PAYLOAD_TOO_LARGE", "message": f"Request larger than {st.max_upload_mb} MB."}}, status_code=413)
        t0 = time.perf_counter()
        response = await call_next(request)
        dt = time.perf_counter() - t0
        route = request.scope.get("route")
        route_path = getattr(route, "path", "unmatched")
        HTTP_REQUESTS.labels(request.method, route_path, str(response.status_code)).inc()
        HTTP_LATENCY.labels(request.method, route_path).observe(dt)
        response.headers["X-Request-ID"] = rid
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if st.cookie_secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError):
        return JSONResponse({"error": {"code": exc.code, "message": exc.message, "context": exc.context}}, status_code=exc.status_code)

    @app.exception_handler(StaleDataError)
    async def stale(request: Request, exc: StaleDataError):
        return JSONResponse({"error": {"code": "VERSION_CONFLICT", "message": "This record was changed by someone else meanwhile. Reload and try again."}}, status_code=409)

    @app.exception_handler(RequestValidationError)
    async def validation(request: Request, exc: RequestValidationError):
        errs = [{"field": ".".join(str(x) for x in e.get("loc", [])[1:]), "message": e.get("msg")} for e in exc.errors()]
        return JSONResponse({"error": {"code": "VALIDATION_FAILED", "message": "Some fields are invalid.", "context": {"fields": errs}}}, status_code=422)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        error_id = uuid.uuid4().hex[:10]
        log.error("unhandled error", extra={"error_id": error_id, "extra_data": {"path": request.url.path, "trace": traceback.format_exc()[-6000:]}})
        return JSONResponse(
            {"error": {"code": "INTERNAL_ERROR", "message": f"MonxuPlan couldn't complete the request (error {error_id}). The technical details were logged for the administrator.", "context": {"error_id": error_id}}},
            status_code=500,
        )

    # ---------------------------------------------------------------- health & metrics
    @app.get("/health", tags=["health"])
    def health():
        return {"status": "ok", "version": __version__}

    @app.get("/readiness", tags=["health"])
    def readiness():
        try:
            with get_engine().connect() as c:
                c.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"status": "unavailable", "database": str(exc)[:200]}, status_code=503)
        return {"status": "ready", "database": "ok"}

    @app.get("/metrics", tags=["health"], response_class=PlainTextResponse)
    def metrics():
        try:
            from sqlalchemy import func, select

            from ..core.db import new_session
            from ..models import PlanningRun

            with new_session(None) as s:
                QUEUE_DEPTH.set(s.scalar(select(func.count()).select_from(PlanningRun).where(PlanningRun.status == "QUEUED")) or 0)
        except Exception:  # noqa: BLE001
            pass
        return PlainTextResponse(generate_latest(REGISTRY).decode(), media_type="text/plain; version=0.0.4")

    from .routers import admin, analytics, auth, integrations, masterdata, materials, planning, plans, scenarios, shopfloor, stream

    for r in (auth, masterdata, scenarios, planning, plans, analytics, materials, shopfloor, integrations, admin, stream):
        app.include_router(r.router, prefix="/api/v1")
    return app


app = create_app()
