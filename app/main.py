"""Punto de entrada de la aplicación FastAPI del agente comercial La Cuisine."""

from __future__ import annotations

from fastapi import FastAPI

from app.api.webhook import router as webhook_router
from app.core.config import Settings
from app.services.idempotency import EventStore, InMemoryEventStore


def create_app(settings: Settings | None = None, event_store: EventStore | None = None) -> FastAPI:
    app = FastAPI(title="La Cuisine WhatsApp Agent", version="0.1.0", docs_url=None, redoc_url=None)
    app.state.settings = settings or Settings.from_env()
    app.state.event_store = event_store or InMemoryEventStore()
    app.include_router(webhook_router)

    @app.get("/health", tags=["ops"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
