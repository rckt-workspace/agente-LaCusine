"""Punto de entrada de la aplicación FastAPI del agente comercial La Cuisine."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse

from app.api.webhook import router as webhook_router
from app.core.config import Settings
from app.db.pool import create_pool
from app.repositories.webhook_events import PostgresWebhookEventRepository, WebhookEventRepository


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    pool = app.state.pool
    if pool is not None:
        # wait=False: el arranque no depende de la base; las peticiones esperan la conexión si hace falta.
        pool.open(wait=False)
    try:
        yield
    finally:
        if pool is not None:
            pool.close()


def create_app(
    settings: Settings | None = None,
    webhook_repository: WebhookEventRepository | None = None,
) -> FastAPI:
    """Ensambla la aplicación. Los repositorios se inyectan; por defecto se usa PostgreSQL si hay DATABASE_URL."""
    settings = settings or Settings.from_env()

    pool = None
    if webhook_repository is None and settings.database_url:
        pool = create_pool(settings.database_url)
        webhook_repository = PostgresWebhookEventRepository(
            pool, retention_days=settings.webhook_event_retention_days
        )

    app = FastAPI(
        title="La Cuisine WhatsApp Agent",
        version="0.2.0",
        docs_url=None,
        redoc_url=None,
        lifespan=_lifespan,
    )
    app.state.settings = settings
    app.state.pool = pool
    app.state.webhook_repository = webhook_repository
    app.include_router(webhook_router)

    @app.get("/health", tags=["ops"])
    def health() -> dict[str, str]:
        # Liveness: el proceso responde. No comprueba dependencias.
        return {"status": "ok"}

    @app.get("/ready", tags=["ops"])
    def ready() -> Response:
        # Readiness: la dependencia crítica (PostgreSQL) responde. Sin detalles de error.
        if pool is None:
            return JSONResponse({"status": "not_configured"}, status_code=503)
        try:
            with pool.connection() as conn:
                conn.execute("select 1")
        except Exception:  # noqa: BLE001 - nunca se expone el detalle del fallo
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return JSONResponse({"status": "ready"})

    return app


app = create_app()
