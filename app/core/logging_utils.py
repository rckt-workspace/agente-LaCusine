"""Logging seguro: registra metadatos del evento, nunca contenido ni datos personales."""

from __future__ import annotations

import logging

logger = logging.getLogger("lacusine.webhook")


def mask_phone(value: str | None) -> str:
    """Devuelve los últimos 4 dígitos para trazabilidad sin exponer el número completo."""
    if not value:
        return "unknown"
    return f"***{value[-4:]}"
