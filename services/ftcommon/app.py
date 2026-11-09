"""Application factory: middleware for correlation IDs, structured access logs,
metrics, fault injection; health endpoints; safe error handling."""
from __future__ import annotations

import asyncio
import os
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from . import chaos, config
from .db import DatabaseUnavailable, get_db
from .logs import log_event, request_id_var, setup_logging
from .metrics import AUTH_FAILURES, HTTP_LATENCY, HTTP_REQUESTS, IN_FLIGHT, VALIDATION_ERRORS
from .resilience import DependencyError


def create_app(service_name: str) -> FastAPI:
    config.SERVICE_NAME = service_name
    setup_logging()
    config.validate_config()
    app = FastAPI(title=service_name, docs_url=None if config.RESILIENT else "/docs", redoc_url=None,
                  openapi_url=None if config.RESILIENT else "/openapi.json")

    with open("/tmp/app.pid", "w") as fh:  # used by the fault injector to crash this process
        fh.write(str(os.getpid()))

    @app.middleware("http")
    async def observability(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        request_id_var.set(rid)
        request.state.user = "-"
        start = time.perf_counter()
        is_api = request.url.path.startswith(("/api/", "/internal/"))
        IN_FLIGHT.labels(service_name).inc()
        status = 500
        try:
            if is_api and config.CHAOS_ENABLED:
                delay = chaos.get("latency_ms")
                if delay:
                    await asyncio.sleep(delay / 1000)
                if chaos.roll("error_rate"):
                    response = JSONResponse({"error": "injected_fault", "request_id": rid}, status_code=500)
                    status = 500
                    return response
            response = await call_next(request)
            status = response.status_code
            response.headers["X-Request-ID"] = rid
            response.headers["X-Served-By"] = config.INSTANCE_ID
            return response
        finally:
            elapsed = time.perf_counter() - start
            IN_FLIGHT.labels(service_name).dec()
            route = request.scope.get("route")
            route_path = getattr(route, "path", "unmatched")
            if route_path not in ("/metrics", "/health/live", "/health/ready"):
                HTTP_REQUESTS.labels(service_name, request.method, route_path, str(status)).inc()
                HTTP_LATENCY.labels(service_name, route_path).observe(elapsed)
                log_event("http_request", method=request.method, path=request.url.path, route=route_path,
                          status=status, duration_ms=round(elapsed * 1000, 2),
                          user=getattr(request.state, "user", "-"))

    @app.exception_handler(DatabaseUnavailable)
    async def db_unavailable(request: Request, exc: DatabaseUnavailable):
        log_event("database_unavailable", error=str(exc)[:200])
        return JSONResponse({"error": "database_unavailable", "request_id": request_id_var.get()},
                            status_code=503, headers={"Retry-After": "2"})

    @app.exception_handler(DependencyError)
    async def dependency_failed(request: Request, exc: DependencyError):
        log_event("dependency_failed", dependency=exc.dependency, kind=exc.kind)
        return JSONResponse({"error": "dependency_unavailable", "dependency": exc.dependency,
                             "request_id": request_id_var.get()}, status_code=503,
                            headers={"Retry-After": "2"})

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        route = getattr(request.scope.get("route"), "path", "unmatched")
        VALIDATION_ERRORS.labels(service_name, route).inc()
        errors = [{"loc": e.get("loc"), "msg": e.get("msg")} for e in exc.errors()][:10]
        return JSONResponse({"error": "validation_failed", "details": errors}, status_code=422)

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception):
        # safe failure: no stack traces or internals in responses
        log_event("unhandled_exception", error=f"{type(exc).__name__}: {str(exc)[:300]}")
        return JSONResponse({"error": "internal_error", "request_id": request_id_var.get()}, status_code=500)

    @app.get("/health/live")
    def live():
        return {"status": "alive", "service": service_name, "instance": config.INSTANCE_ID}

    @app.get("/health/ready")
    def ready():
        deps = get_db().status()
        if deps.get("db_primary"):
            return {"status": "ready", "dependencies": deps}
        if deps.get("db_replica"):
            return {"status": "degraded", "dependencies": deps}
        return JSONResponse({"status": "not_ready", "dependencies": deps}, status_code=503)

    @app.get("/metrics")
    def metrics():
        return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    app.include_router(chaos.router)

    @app.on_event("startup")
    def startup():
        # pre-create counter series with value 0: a series that appears only at the first
        # event loses that first increase in rate(), which delays or hides alerts
        for reason in ("missing_token", "invalid_token", "expired_token", "bad_credentials", "forbidden_role",
                       "forbidden_object", "bad_service_token"):
            AUTH_FAILURES.labels(service_name, reason)
        get_db()
        log_event("service_started", resilient=config.RESILIENT, auth=config.AUTH_ENABLED,
                  chaos=config.CHAOS_ENABLED)

    return app
