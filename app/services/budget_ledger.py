"""Libro de reservas presupuestarias. Una reserva existe antes de cada llamada facturable.

Estados y lo que cuenta contra el presupuesto (counted):
  reserved  -> peor caso reservado. Cuenta.
  settled   -> costo real estimado. Cuenta.
  released  -> la API garantizó que no facturó. No cuenta. Puede volver a reservarse.
  uncertain -> no se sabe si facturó. Cuenta hasta conciliar. Nunca se libera automáticamente.

Transiciones permitidas:
  (nada)    -> reserved
  released  -> reserved
  reserved  -> settled | released | uncertain
Cualquier otra transición se rechaza: un call_id no puede volver a facturar sin pasar por revisión.

La implementación PostgreSQL es la de producción; la de memoria sigue la misma semántica (pruebas y dry-run).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from psycopg import Error as PsycopgError
from psycopg_pool import PoolClosed

from app.repositories.errors import StorageError


class BudgetExceededError(Exception):
    """El presupuesto o el límite por interacción impiden reservar. El código no incluye datos."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ReservationConflictError(Exception):
    """Ya existe una reserva activa o liquidada para este call_id."""


class LedgerStateError(Exception):
    """La transición pedida no es válida desde el estado actual."""


@dataclass(frozen=True)
class BudgetLimits:
    daily_usd: Decimal | None = None
    monthly_usd: Decimal | None = None
    interaction_usd: Decimal | None = None


class BudgetLedger(Protocol):
    def reserve(self, call_id: str, conversation_id: UUID | None, worst_case_usd: Decimal, limits: BudgetLimits,
                day_start: datetime, month_start: datetime, now: datetime) -> None: ...
    def state(self, call_id: str) -> str | None: ...
    def settle(self, call_id: str, actual_usd: Decimal) -> None: ...
    def release(self, call_id: str) -> None: ...
    def mark_uncertain(self, call_id: str) -> None: ...
    def counted_between(self, start: datetime, end: datetime) -> Decimal: ...


@dataclass
class _Reservation:
    call_id: str
    conversation_id: UUID | None
    status: str
    reserved_usd: Decimal
    counted_usd: Decimal
    created_at: datetime


class InMemoryBudgetLedger:
    """Misma semántica que PostgresBudgetLedger. Solo para pruebas y modo simulado."""

    def __init__(self) -> None:
        self._rows: dict[str, _Reservation] = {}
        self._lock = threading.Lock()

    def reserve(self, call_id, conversation_id, worst_case_usd, limits, day_start, month_start, now) -> None:
        with self._lock:
            existing = self._rows.get(call_id)
            if existing is not None and existing.status != "released":
                raise ReservationConflictError(call_id)
            if limits.interaction_usd is not None and worst_case_usd > limits.interaction_usd:
                raise BudgetExceededError("interaction_limit")
            if limits.daily_usd is not None and self._counted(day_start, now) + worst_case_usd > limits.daily_usd:
                raise BudgetExceededError("daily_budget")
            if limits.monthly_usd is not None and self._counted(month_start, now) + worst_case_usd > limits.monthly_usd:
                raise BudgetExceededError("monthly_budget")
            self._rows[call_id] = _Reservation(
                call_id=call_id, conversation_id=conversation_id, status="reserved",
                reserved_usd=worst_case_usd, counted_usd=worst_case_usd, created_at=now,
            )

    def state(self, call_id: str) -> str | None:
        with self._lock:
            row = self._rows.get(call_id)
            return row.status if row else None

    def settle(self, call_id: str, actual_usd: Decimal) -> None:
        self._transition(call_id, "settled", actual_usd)

    def release(self, call_id: str) -> None:
        self._transition(call_id, "released", Decimal("0"))

    def mark_uncertain(self, call_id: str) -> None:
        with self._lock:
            row = self._require(call_id)
            if row.status != "reserved":
                raise LedgerStateError(row.status)
            row.status = "uncertain"

    def counted_between(self, start: datetime, end: datetime) -> Decimal:
        with self._lock:
            return self._counted(start, end)

    def add_counted(self, call_id: str, amount: Decimal, at: datetime) -> None:
        """Siembra consumo previo en pruebas."""
        with self._lock:
            self._rows[call_id] = _Reservation(call_id, None, "settled", amount, amount, at)

    def _counted(self, start: datetime, end: datetime) -> Decimal:
        # Ventana cerrada en ambos extremos: una reserva creada exactamente en `end` también cuenta.
        return sum((r.counted_usd for r in self._rows.values() if start <= r.created_at <= end), Decimal("0"))

    def _require(self, call_id: str) -> _Reservation:
        row = self._rows.get(call_id)
        if row is None:
            raise LedgerStateError("missing")
        return row

    def _transition(self, call_id: str, target: str, counted: Decimal) -> None:
        with self._lock:
            row = self._require(call_id)
            if row.status != "reserved":
                raise LedgerStateError(row.status)
            row.status = target
            row.counted_usd = counted


class PostgresBudgetLedger:
    """Reservas en PostgreSQL. La reserva se hace bajo un bloqueo de asesor de transacción, de muy corta duración."""

    def __init__(self, pool) -> None:
        self._pool = pool

    def reserve(self, call_id, conversation_id, worst_case_usd, limits, day_start, month_start, now) -> None:


        try:
            with self._pool.connection() as conn:
                with conn.transaction():
                    # Serializa las reservas de todos los workers. Se libera al terminar la transacción.
                    conn.execute("select pg_advisory_xact_lock(hashtext('lacusine.budget'))")
                    if limits.interaction_usd is not None and worst_case_usd > limits.interaction_usd:
                        raise BudgetExceededError("interaction_limit")
                    if limits.daily_usd is not None and self._counted(conn, day_start, now) + worst_case_usd > limits.daily_usd:
                        raise BudgetExceededError("daily_budget")
                    if limits.monthly_usd is not None and self._counted(conn, month_start, now) + worst_case_usd > limits.monthly_usd:
                        raise BudgetExceededError("monthly_budget")
                    row = conn.execute(
                        """
                        insert into public.budget_reservations
                            (call_id, conversation_id, status, reserved_usd, counted_usd, created_at)
                        values (%s, %s, 'reserved', %s, %s, %s)
                        on conflict (call_id) do update
                            set status = 'reserved', reserved_usd = excluded.reserved_usd,
                                counted_usd = excluded.counted_usd, created_at = excluded.created_at,
                                reconciled_at = null
                            where public.budget_reservations.status = 'released'
                        returning id
                        """,
                        (call_id, conversation_id, worst_case_usd, worst_case_usd, now),
                    ).fetchone()
                    if row is None:
                        raise ReservationConflictError(call_id)
        except (PsycopgError, PoolClosed):
            raise StorageError("budget_write_failed") from None

    def state(self, call_id: str) -> str | None:


        try:
            with self._pool.connection() as conn:
                row = conn.execute("select status from public.budget_reservations where call_id = %s", (call_id,)).fetchone()
        except (PsycopgError, PoolClosed):
            raise StorageError("budget_read_failed") from None
        return row[0] if row else None

    def settle(self, call_id: str, actual_usd: Decimal) -> None:
        self._transition(call_id, "settled", actual_usd)

    def release(self, call_id: str) -> None:
        self._transition(call_id, "released", Decimal("0"))

    def mark_uncertain(self, call_id: str) -> None:


        try:
            with self._pool.connection() as conn:
                row = conn.execute(
                    "update public.budget_reservations set status = 'uncertain' "
                    "where call_id = %s and status = 'reserved' returning id",
                    (call_id,),
                ).fetchone()
        except (PsycopgError, PoolClosed):
            raise StorageError("budget_write_failed") from None
        if row is None:
            raise LedgerStateError("not_reserved")

    def counted_between(self, start: datetime, end: datetime) -> Decimal:


        try:
            with self._pool.connection() as conn:
                return self._counted(conn, start, end)
        except (PsycopgError, PoolClosed):
            raise StorageError("budget_read_failed") from None

    def _counted(self, conn, start: datetime, end: datetime) -> Decimal:
        row = conn.execute(
            "select coalesce(sum(counted_usd), 0) from public.budget_reservations "
            "where created_at >= %s and created_at <= %s",
            (start, end),
        ).fetchone()
        return Decimal(row[0])

    def _transition(self, call_id: str, target: str, counted: Decimal) -> None:


        try:
            with self._pool.connection() as conn:
                row = conn.execute(
                    "update public.budget_reservations "
                    "set status = %s, counted_usd = %s, reconciled_at = now() "
                    "where call_id = %s and status = 'reserved' returning id",
                    (target, counted, call_id),
                ).fetchone()
        except (PsycopgError, PoolClosed):
            raise StorageError("budget_write_failed") from None
        if row is None:
            raise LedgerStateError("not_reserved")
