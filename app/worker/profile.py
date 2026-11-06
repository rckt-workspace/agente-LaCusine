"""Perfil efectivo de un worker para su registro de arranque. Nunca incluye secretos, mensajes ni números completos.

La huella (`configuration_fingerprint`) permite comprobar que dos procesos comparten la misma configuración
sin revelar sus valores: se calcula sobre un conjunto de campos no secretos.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import urlparse

from app.core.config import Settings
from app.worker.config import WorkerSettings


def database_environment(database_url: str | None) -> str:
    """Solo host y nombre de base de datos. Sin usuario, contraseña ni parámetros."""
    if not database_url:
        return "missing"
    parsed = urlparse(database_url)
    host = parsed.hostname or "unknown-host"
    name = (parsed.path or "").lstrip("/") or "default"
    return f"{host}/{name}"


def describe_worker_profile(settings: Settings, worker_settings: WorkerSettings, provider) -> dict[str, Any]:
    provider_name = provider.name if provider is not None else "disabled"
    model = provider.model if provider is not None else None
    send_enabled = bool(settings.meta_send_enabled and not settings.whatsapp_dry_run)
    fields: dict[str, Any] = {
        "worker_id": worker_settings.owner,
        "provider": provider_name,
        "model": model,
        "dry_run": bool(settings.agent_dry_run),
        "send_enabled": send_enabled,
        "database_environment": database_environment(settings.database_url),
        "kill_switch": bool(settings.agent_kill_switch),
        "recipients_allowlisted": len(settings.whatsapp_allowed_recipients),
        "audience_mode": settings.whatsapp_audience_mode,
        "daily_budget_usd": _text(settings.agent_daily_budget_usd),
        "monthly_budget_usd": _text(settings.agent_monthly_budget_usd),
        "interaction_limit_usd": _text(settings.agent_interaction_limit_usd),
        "graph_api_version": settings.meta_graph_api_version,
        "batch_size": worker_settings.batch_size,
        "max_concurrency": worker_settings.max_concurrency,
    }
    fields["configuration_fingerprint"] = configuration_fingerprint(fields)
    return fields


def configuration_fingerprint(fields: dict[str, Any]) -> str:
    """Huella de 16 caracteres sobre los campos de configuración (sin worker_id ni secretos)."""
    stable = {key: value for key, value in fields.items() if key not in {"worker_id", "configuration_fingerprint"}}
    encoded = json.dumps(stable, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def format_profile(fields: dict[str, Any]) -> str:
    """Línea de registro `clave=valor` en un orden fijo y estable."""
    order = (
        "worker_id", "provider", "model", "dry_run", "send_enabled", "database_environment",
        "kill_switch", "audience_mode", "recipients_allowlisted", "daily_budget_usd", "monthly_budget_usd",
        "interaction_limit_usd", "graph_api_version", "batch_size", "max_concurrency", "configuration_fingerprint",
    )
    return " ".join(f"{key}={_text(fields.get(key))}" for key in order)


def _text(value: Any) -> str:
    return "-" if value is None else str(value)
