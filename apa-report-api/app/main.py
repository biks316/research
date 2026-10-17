"""FastAPI application: routers, exception handlers, lifespan, health checks."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any

import structlog
import yaml
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api import output, plan, progress, reports
from app.config import Settings, get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import RequestContextMiddleware, configure_logging
from app.core.resources import api_helpers, open_resources
from app.core.security import TokenVerifier
from app.services.export_service import ExportService
from app.services.outbox import OutboxRelay

SPEC_PATH = Path(__file__).resolve().parent.parent / "openapi.yaml"
log = structlog.get_logger(__name__)


@lru_cache
def load_spec() -> dict[str, Any]:
    """openapi.yaml is the contract; it is served as-is at /openapi.json."""
    return yaml.safe_load(SPEC_PATH.read_text())


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    async with AsyncExitStack() as stack:
        resources = await open_resources(settings, stack)
        app.state.resources = resources
        app.state.idempotency, app.state.rate_limiter = api_helpers(resources)
        app.state.exporter = ExportService(resources.reports, resources.storage)
        relay = OutboxRelay(resources.dispatcher, settings)
        if app.state.start_outbox_relay:
            relay.start()
            stack.push_async_callback(relay.stop)
        log.info("api_started", env=settings.APP_ENV, prefix=settings.API_PREFIX)
        yield
    log.info("api_stopped")


def create_app(settings: Settings | None = None, *, start_outbox_relay: bool = True) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.LOG_LEVEL)
    spec = load_spec()
    app = FastAPI(
        title=spec["info"]["title"],
        version=spec["info"]["version"],
        summary=spec["info"].get("summary"),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.verifier = TokenVerifier(settings)
    app.state.start_outbox_relay = start_outbox_relay

    register_exception_handlers(app)
    app.add_middleware(RequestContextMiddleware)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["GET", "POST", "PUT", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID"],
            expose_headers=["Location", "Retry-After", "Content-Disposition", "X-Request-ID"],
        )

    for module in (reports, progress, plan, output):
        app.include_router(module.router, prefix=settings.API_PREFIX)

    def openapi() -> dict[str, Any]:
        return load_spec()

    app.openapi = openapi  # type: ignore[method-assign]

    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready", include_in_schema=False)
    async def ready(request: Request) -> JSONResponse:
        resources = request.app.state.resources
        checks: dict[str, str] = {}

        async def check(name: str, probe) -> None:
            try:
                await asyncio.wait_for(probe(), timeout=3)
                checks[name] = "ok"
            except Exception as exc:  # noqa: BLE001 - report every dependency's state
                checks[name] = f"error: {type(exc).__name__}"

        async def db_probe() -> None:
            async with resources.db.session() as session:
                await session.execute(text("SELECT 1"))

        async def sqs_probe() -> None:
            for url in (settings.SQS_PLANNING_QUEUE_URL, settings.SQS_GENERATION_QUEUE_URL):
                await resources.sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])

        await asyncio.gather(check("database", db_probe), check("redis", resources.redis.ping), check("sqs", sqs_probe))
        ok = all(v == "ok" for v in checks.values())
        return JSONResponse({"status": "ok" if ok else "unavailable", "checks": checks}, status_code=200 if ok else 503)

    return app


def get_app() -> FastAPI:
    """Uvicorn factory: `uvicorn app.main:get_app --factory`."""
    return create_app()
