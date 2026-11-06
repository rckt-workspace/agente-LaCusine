"""Servicio comercial: confirmación de ventas con validación y permiso, y eventos derivados del resultado del agente.

Reglas:
  * Solo el rol `admin` puede confirmar o cancelar ventas. Ni la IA ni el cliente llegan a este servicio.
  * Una intención de compra registra un evento; NUNCA crea una venta.
  * Los eventos se derivan solo de señales que el backend ya calculó (etapa, motivo de transferencia, productos
    validados contra el catálogo). El texto del modelo no se usa para registrar nada.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from uuid import UUID

from app.repositories.commercial import SaleRecord

ROLE_ADMIN = "admin"
CONFIRMATION_SOURCES = frozenset({"admin_manual", "external_order_system"})
MAX_AMOUNT_COP = 10_000_000_000  # tope de validación de entrada; cualquier venta mayor requiere revisión manual
FUTURE_TOLERANCE = timedelta(days=1)


class ForbiddenError(Exception):
    code = "forbidden"


class ValidationFailed(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def require_admin(role: str | None) -> None:
    if role != ROLE_ADMIN:
        raise ForbiddenError()


def validate_sale(*, sale_ref: str, lead_id: UUID, amount_cop, sold_at: datetime, source: str,
                  confirmed_by: str, evidence_ref: str, products: dict[UUID, int], now: datetime) -> SaleRecord:
    """Valida todos los campos antes de tocar la base. Devuelve el registro listo para persistir."""
    if not isinstance(sale_ref, str) or not (3 <= len(sale_ref.strip()) <= 100):
        raise ValidationFailed("sale_ref_invalid")
    if isinstance(amount_cop, bool) or not isinstance(amount_cop, int):
        raise ValidationFailed("amount_not_integer_cop")
    if not (1 <= amount_cop <= MAX_AMOUNT_COP):
        raise ValidationFailed("amount_out_of_range")
    if sold_at.tzinfo is None:
        raise ValidationFailed("sold_at_without_timezone")
    if sold_at > now + FUTURE_TOLERANCE:
        raise ValidationFailed("sold_at_in_future")
    if source not in CONFIRMATION_SOURCES:
        raise ValidationFailed("confirmation_source_invalid")
    if not isinstance(confirmed_by, str) or not (1 <= len(confirmed_by.strip()) <= 120):
        raise ValidationFailed("confirmed_by_invalid")
    if not isinstance(evidence_ref, str) or not (3 <= len(evidence_ref.strip()) <= 300):
        raise ValidationFailed("evidence_ref_required")
    for quantity in products.values():
        if isinstance(quantity, bool) or not isinstance(quantity, int) or not (1 <= quantity <= 1000):
            raise ValidationFailed("product_quantity_invalid")
    return SaleRecord(
        sale_ref=sale_ref.strip(), lead_id=lead_id, amount_cop=amount_cop, sold_at=sold_at.astimezone(timezone.utc),
        confirmation_source=source, confirmed_by=confirmed_by.strip(), evidence_ref=evidence_ref.strip(),
    )


class SalesService:
    def __init__(self, repository, clock=lambda: datetime.now(timezone.utc)) -> None:
        self._repository = repository
        self._clock = clock

    def confirm(self, *, role: str | None, sale_ref: str, lead_id: UUID, amount_cop, sold_at: datetime,
                source: str, confirmed_by: str, evidence_ref: str, products: dict[UUID, int] | None = None) -> str:
        require_admin(role)
        record = validate_sale(
            sale_ref=sale_ref, lead_id=lead_id, amount_cop=amount_cop, sold_at=sold_at, source=source,
            confirmed_by=confirmed_by, evidence_ref=evidence_ref, products=products or {}, now=self._clock(),
        )
        return self._repository.confirm_sale(record, products or {})

    def cancel(self, *, role: str | None, sale_ref: str, reason: str, actor: str) -> None:
        require_admin(role)
        if not reason or not reason.strip() or len(reason.strip()) > 300:
            raise ValidationFailed("reason_invalid")
        self._repository.cancel_sale(sale_ref.strip(), reason=reason.strip(), actor=actor.strip())


@dataclass(frozen=True)
class CommercialEvent:
    event_type: str
    dedupe_key: str
    category: str | None = None
    product_id: UUID | None = None
    budget_cop: int | None = None


@dataclass(frozen=True)
class OutcomeSignals:
    """Señales ya calculadas por el backend para un resultado del agente."""

    lead_stage: str | None          # etapa derivada por el backend (p. ej. HIGH_INTENT)
    handoff_reason: str | None      # motivo de transferencia validado por el backend
    product_ids: tuple[UUID, ...] = field(default_factory=tuple)
    product_categories: dict = field(default_factory=dict)  # product_id -> categoría verificada


def events_from_outcome(signals: OutcomeSignals, external_event_id: str) -> list[CommercialEvent]:
    """Eventos derivados de señales del backend. Cada clave es determinista: reintentos no duplican."""
    events: list[CommercialEvent] = []
    for product_id in signals.product_ids:
        events.append(CommercialEvent(
            event_type="product_interest", dedupe_key=f"interest:{external_event_id}:{product_id}",
            product_id=product_id, category=signals.product_categories.get(product_id),
        ))
    if signals.lead_stage == "HIGH_INTENT":
        events.append(CommercialEvent(event_type="purchase_intent", dedupe_key=f"intent:{external_event_id}"))
    if signals.handoff_reason == "quote_request":
        events.append(CommercialEvent(event_type="quote_request", dedupe_key=f"quote:{external_event_id}"))
    if signals.handoff_reason is not None:
        events.append(CommercialEvent(event_type="handoff_to_advisor",
                                      dedupe_key=f"handoff:{external_event_id}"))
    return events
