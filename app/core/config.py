"""Configuración cargada exclusivamente desde variables de entorno.

Valores por defecto seguros: sin llamadas facturables a Claude, sin envíos, presupuesto cerrado.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

_TRUE = {"1", "true", "yes", "si", "sí", "on"}


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in _TRUE


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} debe ser un entero") from None


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"{name} debe ser numérico") from None


def _csv(name: str) -> tuple[str, ...]:
    raw = os.environ.get(name) or ""
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _decimal(name: str) -> Decimal | None:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise ValueError(f"{name} debe ser decimal") from None
    if value < 0:
        raise ValueError(f"{name} no puede ser negativo")
    return value


@dataclass(frozen=True)
class Settings:
    meta_app_secret: str | None
    meta_verify_token: str | None
    database_url: str | None = None
    max_body_bytes: int = 256 * 1024
    max_messages_per_request: int = 100
    webhook_event_retention_days: int = 7

    # WhatsApp Cloud API (salida). Envío real deshabilitado por defecto.
    meta_access_token: str | None = field(default=None, repr=False)
    meta_phone_number_id: str | None = None
    meta_graph_api_version: str | None = None
    meta_send_enabled: bool = False
    whatsapp_dry_run: bool = True
    whatsapp_allowed_recipients: tuple[str, ...] = ()
    whatsapp_timeout_seconds: float = 10.0
    whatsapp_max_concurrency: int = 2

    # Agente y Claude. Por defecto todo deshabilitado.
    anthropic_enabled: bool = False
    agent_dry_run: bool = True
    anthropic_api_key: str | None = field(default=None, repr=False)
    anthropic_model: str | None = None
    anthropic_max_output_tokens: int = 350
    anthropic_timeout_seconds: float = 20.0
    anthropic_max_retries: int = 1
    anthropic_max_concurrency: int = 2
    anthropic_prompt_caching: bool = False
    agent_kill_switch: bool = False
    demo_enabled: bool = False  # solo para la demostración en entorno aislado. Nunca en producción.
    demo_token: str | None = field(default=None, repr=False)
    demo_public_origin: str | None = None  # p. ej. https://lacusine-demo.onrender.com
    advisor_notifier: str = "simulated"  # único canal disponible hoy; cualquier otro valor falla al arrancar
    agent_max_input_chars: int = 1000
    agent_max_context_chars: int = 3000

    # Tarifas USD por millón de tokens. Sin tarifas configuradas no se permite ninguna llamada real.
    price_input_usd_per_mtok: Decimal | None = None
    price_output_usd_per_mtok: Decimal | None = None
    price_cache_write_usd_per_mtok: Decimal | None = None
    price_cache_read_usd_per_mtok: Decimal | None = None
    pricing_reference: str | None = None

    # Presupuestos en USD estimados. None = sin límite de ese tipo (no recomendado en producción).
    agent_daily_budget_usd: Decimal | None = None
    agent_monthly_budget_usd: Decimal | None = None
    agent_interaction_limit_usd: Decimal | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            meta_app_secret=os.environ.get("META_APP_SECRET") or None,
            meta_verify_token=os.environ.get("META_VERIFY_TOKEN") or None,
            database_url=os.environ.get("DATABASE_URL") or None,
            max_body_bytes=_int("MAX_WEBHOOK_BODY_BYTES", 256 * 1024),
            webhook_event_retention_days=_int("WEBHOOK_EVENT_RETENTION_DAYS", 7),
            meta_access_token=os.environ.get("META_ACCESS_TOKEN") or None,
            meta_phone_number_id=os.environ.get("META_PHONE_NUMBER_ID") or None,
            meta_graph_api_version=os.environ.get("META_GRAPH_API_VERSION") or None,
            meta_send_enabled=_bool("META_SEND_ENABLED", False),
            whatsapp_dry_run=_bool("WHATSAPP_DRY_RUN", True),
            whatsapp_allowed_recipients=_csv("WHATSAPP_ALLOWED_RECIPIENTS"),
            whatsapp_timeout_seconds=_float("WHATSAPP_TIMEOUT_SECONDS", 10.0),
            whatsapp_max_concurrency=_int("WHATSAPP_MAX_CONCURRENCY", 2),
            anthropic_enabled=_bool("ANTHROPIC_ENABLED", False),
            agent_dry_run=_bool("AGENT_DRY_RUN", True),
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
            anthropic_model=os.environ.get("ANTHROPIC_MODEL") or None,
            anthropic_max_output_tokens=_int("ANTHROPIC_MAX_OUTPUT_TOKENS", 350),
            anthropic_timeout_seconds=_float("ANTHROPIC_TIMEOUT_SECONDS", 20.0),
            anthropic_max_retries=_int("ANTHROPIC_MAX_RETRIES", 1),
            anthropic_max_concurrency=_int("ANTHROPIC_MAX_CONCURRENCY", 2),
            anthropic_prompt_caching=_bool("ANTHROPIC_PROMPT_CACHING", False),
            agent_kill_switch=_bool("AGENT_KILL_SWITCH", False),
            demo_enabled=_bool("DEMO_ENABLED", False),
            demo_token=os.environ.get("DEMO_TOKEN") or None,
            demo_public_origin=os.environ.get("DEMO_PUBLIC_ORIGIN") or None,
            advisor_notifier=(os.environ.get("ADVISOR_NOTIFIER") or "simulated").strip().lower(),
            agent_max_input_chars=_int("AGENT_MAX_INPUT_CHARS", 1000),
            agent_max_context_chars=_int("AGENT_MAX_CONTEXT_CHARS", 3000),
            price_input_usd_per_mtok=_decimal("ANTHROPIC_PRICE_INPUT_USD_PER_MTOK"),
            price_output_usd_per_mtok=_decimal("ANTHROPIC_PRICE_OUTPUT_USD_PER_MTOK"),
            price_cache_write_usd_per_mtok=_decimal("ANTHROPIC_PRICE_CACHE_WRITE_USD_PER_MTOK"),
            price_cache_read_usd_per_mtok=_decimal("ANTHROPIC_PRICE_CACHE_READ_USD_PER_MTOK"),
            pricing_reference=os.environ.get("ANTHROPIC_PRICING_REFERENCE") or None,
            agent_daily_budget_usd=_decimal("AGENT_DAILY_BUDGET_USD"),
            agent_monthly_budget_usd=_decimal("AGENT_MONTHLY_BUDGET_USD"),
            agent_interaction_limit_usd=_decimal("AGENT_INTERACTION_LIMIT_USD"),
        )
