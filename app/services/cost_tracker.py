"""Costos de Claude: estimación, reservas presupuestarias y auditoría idempotente.

Los costos son ESTIMACIONES calculadas con tarifas configuradas. No son facturas de Anthropic.
Sin tarifas configuradas no se reserva ninguna llamada real (falla cerrado).
Si la tarifa de caché no está configurada, se usa la tarifa de entrada: sobreestima, nunca subestima.

Ciclo de una llamada facturable:
  reserve -> complete -> record_success (auditoría) -> settle
  reserve -> complete falla sin facturar -> release
  reserve -> complete falla con resultado incierto -> mark_uncertain (no se reintenta solo)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Callable, Protocol
from uuid import UUID

from app.core.config import Settings
from app.integrations.llm import LLMResponse, LLMUsage
from app.services.budget_ledger import (
    BudgetExceededError,
    BudgetLedger,
    BudgetLimits,
    InMemoryBudgetLedger,
)

__all__ = ["BudgetExceededError", "CostTracker", "Pricing", "UsageEvent", "estimate_cost", "worst_case_cost"]

MILLION = Decimal(1_000_000)
MICRO = Decimal("0.000001")
# Colombia no tiene horario de verano desde 1993: UTC-5 fijo.
BOGOTA = timezone(timedelta(hours=-5))


@dataclass(frozen=True)
class Pricing:
    input_per_mtok: Decimal
    output_per_mtok: Decimal
    cache_write_per_mtok: Decimal
    cache_read_per_mtok: Decimal
    reference: str

    @classmethod
    def from_settings(cls, settings: Settings) -> "Pricing | None":
        if settings.price_input_usd_per_mtok is None or settings.price_output_usd_per_mtok is None:
            return None
        cache_write = settings.price_cache_write_usd_per_mtok or settings.price_input_usd_per_mtok
        cache_read = settings.price_cache_read_usd_per_mtok or settings.price_input_usd_per_mtok
        return cls(
            input_per_mtok=settings.price_input_usd_per_mtok,
            output_per_mtok=settings.price_output_usd_per_mtok,
            cache_write_per_mtok=cache_write,
            cache_read_per_mtok=cache_read,
            reference=settings.pricing_reference or "configured",
        )


def estimate_cost(pricing: Pricing, usage: LLMUsage) -> Decimal:
    total = (
        Decimal(usage.input_tokens) * pricing.input_per_mtok
        + Decimal(usage.output_tokens) * pricing.output_per_mtok
        + Decimal(usage.cache_write_tokens) * pricing.cache_write_per_mtok
        + Decimal(usage.cache_read_tokens) * pricing.cache_read_per_mtok
    ) / MILLION
    return total.quantize(MICRO, rounding=ROUND_HALF_UP)


def worst_case_cost(pricing: Pricing, input_tokens_estimate: int, max_output_tokens: int) -> Decimal:
    total = (
        Decimal(input_tokens_estimate) * pricing.input_per_mtok
        + Decimal(max_output_tokens) * pricing.output_per_mtok
    ) / MILLION
    return total.quantize(MICRO, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class UsageEvent:
    call_id: str
    conversation_id: UUID | None
    provider: str
    model: str
    status: str
    usage: LLMUsage
    cost_usd: Decimal
    error_code: str | None
    pricing_reference: str | None


class UsageStore(Protocol):
    def record_success_once(self, event: UsageEvent) -> bool: ...
    def record_failure(self, event: UsageEvent) -> None: ...
    def has_success(self, call_id: str) -> bool: ...


class CostTracker:
    def __init__(
        self,
        store: UsageStore,
        pricing: Pricing | None,
        daily_budget_usd: Decimal | None = None,
        monthly_budget_usd: Decimal | None = None,
        interaction_limit_usd: Decimal | None = None,
        ledger: BudgetLedger | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._store = store
        self._pricing = pricing
        self._limits = BudgetLimits(
            daily_usd=daily_budget_usd,
            monthly_usd=monthly_budget_usd,
            interaction_usd=interaction_limit_usd,
        )
        self._ledger: BudgetLedger = ledger if ledger is not None else InMemoryBudgetLedger()
        self._clock = clock

    @property
    def pricing(self) -> Pricing | None:
        return self._pricing

    @property
    def ledger(self) -> BudgetLedger:
        return self._ledger

    def reserve(self, call_id: str, conversation_id: UUID | None, worst_case_usd: Decimal) -> None:
        """Reserva el peor caso antes de llamar. Lanza BudgetExceededError si no cabe en algún límite."""
        if self._pricing is None:
            raise BudgetExceededError("pricing_not_configured")
        now = self._clock()
        self._ledger.reserve(
            call_id, conversation_id, worst_case_usd, self._limits,
            day_start=_day_start(now), month_start=_month_start(now), now=now,
        )

    def reservation_state(self, call_id: str) -> str | None:
        return self._ledger.state(call_id)

    def record_success(self, call_id: str, conversation_id: UUID | None, response: LLMResponse) -> Decimal:
        billable = self._pricing is not None and response.provider != "fake"
        cost = estimate_cost(self._pricing, response.usage) if billable else Decimal("0")
        event = UsageEvent(
            call_id=call_id,
            conversation_id=conversation_id,
            provider=response.provider,
            model=response.model,
            status="succeeded",
            usage=response.usage,
            cost_usd=cost,
            error_code=None,
            pricing_reference=self._pricing.reference if billable else None,
        )
        # Primero la auditoría: si falla, la reserva queda 'reserved' y se concilia sin volver a llamar.
        self._store.record_success_once(event)
        if self._ledger.state(call_id) == "reserved":
            self._ledger.settle(call_id, cost)
        return cost

    def release_reservation(self, call_id: str) -> None:
        """Solo para errores que la API garantiza como no facturados."""
        if self._ledger.state(call_id) == "reserved":
            self._ledger.release(call_id)

    def mark_uncertain(self, call_id: str) -> None:
        """Resultado incierto: el peor caso se mantiene contado hasta conciliar."""
        if self._ledger.state(call_id) == "reserved":
            self._ledger.mark_uncertain(call_id)

    def record_failure(self, call_id: str, conversation_id: UUID | None, provider: str, model: str, code: str) -> None:
        self._store.record_failure(
            UsageEvent(
                call_id=call_id,
                conversation_id=conversation_id,
                provider=provider,
                model=model,
                status="failed",
                usage=LLMUsage(),
                cost_usd=Decimal("0"),
                error_code=code,
                pricing_reference=None,
            )
        )

    def has_success(self, call_id: str) -> bool:
        return self._store.has_success(call_id)


def _day_start(now: datetime) -> datetime:
    local = now.astimezone(BOGOTA)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def _month_start(now: datetime) -> datetime:
    local = now.astimezone(BOGOTA)
    return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
