"""Presupuesto acumulado del análisis narrativo de informes (purpose = 'business_report').

Separado del libro comercial: nada de lo que gasta un informe cuenta contra las respuestas de WhatsApp.
Reglas:
  * Límite por informe, diario (America/Bogota) y mensual. Cualquiera que se supere bloquea la llamada.
  * La reserva (peor caso) se escribe ANTES de llamar, bajo un bloqueo de asesor de transacción.
  * Una clave de idempotencia (call_id) no vuelve a facturar: si ya está reservada, liquidada o incierta, no se llama.
  * Liquidación según el uso verificado que devuelve la API. Si la llamada falla después de salir, queda 'uncertain'
    y cuenta hasta conciliar: nunca se libera automáticamente.
  * Solo se guardan importes y estados. Sin prompts, respuestas ni datos personales.

La implementación de memoria sigue la misma semántica; se usa en pruebas unitarias.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from psycopg import Error as PsycopgError
from psycopg_pool import PoolClosed

from app.repositories.errors import StorageError
from app.services.budget_ledger import (
    BudgetExceededError,
    LedgerStateError,
    ReservationConflictError,
)
from app.services.cost_tracker import _day_start, _month_start

PURPOSE = "business_report"


@dataclass(frozen=True)
class AnalysisLimits:
    per_report_usd: Decimal
    daily_usd: Decimal | None = None
    monthly_usd: Decimal | None = None


class PostgresAnalysisBudgetLedger:
    def __init__(self, pool) -> None:
        self._pool = pool

    def reserve(self, call_id: str, report_kind: str, week_start: date, fingerprint: str,
                worst_case_usd: Decimal, limits: AnalysisLimits, now: datetime) -> None:
        try:
            with self._pool.connection() as conn:
                with conn.transaction():
                    # Serializa las reservas de informes. Se libera al terminar la transacción.
                    conn.execute("select pg_advisory_xact_lock(hashtext('lacusine.analysis_budget'))")
                    if worst_case_usd > limits.per_report_usd:
                        raise BudgetExceededError("report_limit")
                    if limits.daily_usd is not None and (
                            self._counted(conn, _day_start(now), now) + worst_case_usd > limits.daily_usd):
                        raise BudgetExceededError("daily_budget")
                    if limits.monthly_usd is not None and (
                            self._counted(conn, _month_start(now), now) + worst_case_usd > limits.monthly_usd):
                        raise BudgetExceededError("monthly_budget")
                    row = conn.execute(
                        """
                        insert into public.analysis_budget_reservations
                            (call_id, purpose, report_kind, week_start, fingerprint, status,
                             reserved_usd, counted_usd, created_at)
                        values (%s, %s, %s, %s, %s, 'reserved', %s, %s, %s)
                        on conflict (call_id) do update
                            set status = 'reserved', reserved_usd = excluded.reserved_usd,
                                counted_usd = excluded.counted_usd, created_at = excluded.created_at,
                                reconciled_at = null
                            where public.analysis_budget_reservations.status = 'released'
                        returning id
                        """,
                        (call_id, PURPOSE, report_kind, week_start, fingerprint,
                         worst_case_usd, worst_case_usd, now),
                    ).fetchone()
                    if row is None:
                        raise ReservationConflictError(call_id)
        except (PsycopgError, PoolClosed):
            raise StorageError("analysis_budget_write_failed") from None

    def state(self, call_id: str) -> str | None:
        try:
            with self._pool.connection() as conn:
                row = conn.execute("select status from public.analysis_budget_reservations where call_id = %s",
                                   (call_id,)).fetchone()
        except (PsycopgError, PoolClosed):
            raise StorageError("analysis_budget_read_failed") from None
        return row[0] if row else None

    def settle(self, call_id: str, actual_usd: Decimal) -> None:
        self._transition(call_id, "settled", actual_usd)

    def release(self, call_id: str) -> None:
        self._transition(call_id, "released", Decimal("0"))

    def mark_uncertain(self, call_id: str) -> None:
        self._transition(call_id, "uncertain", None)

    def counted_between(self, start: datetime, end: datetime) -> Decimal:
        try:
            with self._pool.connection() as conn:
                return self._counted(conn, start, end)
        except (PsycopgError, PoolClosed):
            raise StorageError("analysis_budget_read_failed") from None

    def _counted(self, conn, start: datetime, end: datetime) -> Decimal:
        # Cuentan reservada, liquidada e incierta. Solo 'released' queda fuera.
        row = conn.execute(
            "select coalesce(sum(counted_usd), 0) from public.analysis_budget_reservations "
            "where status <> 'released' and created_at >= %s and created_at <= %s",
            (start, end),
        ).fetchone()
        return Decimal(row[0])

    def _transition(self, call_id: str, target: str, counted: Decimal | None) -> None:
        try:
            with self._pool.connection() as conn:
                if counted is None:  # uncertain: el importe reservado se mantiene
                    row = conn.execute(
                        "update public.analysis_budget_reservations set status = %s, reconciled_at = now() "
                        "where call_id = %s and status = 'reserved' returning id",
                        (target, call_id),
                    ).fetchone()
                else:
                    row = conn.execute(
                        "update public.analysis_budget_reservations "
                        "set status = %s, counted_usd = %s, reconciled_at = now() "
                        "where call_id = %s and status = 'reserved' returning id",
                        (target, counted, call_id),
                    ).fetchone()
        except (PsycopgError, PoolClosed):
            raise StorageError("analysis_budget_write_failed") from None
        if row is None:
            raise LedgerStateError("not_reserved")


class InMemoryAnalysisBudgetLedger:
    """Misma semántica que PostgresAnalysisBudgetLedger. Solo para pruebas."""

    def __init__(self) -> None:
        self._rows: dict[str, dict] = {}
        self._lock = threading.Lock()

    def reserve(self, call_id, report_kind, week_start, fingerprint, worst_case_usd, limits, now) -> None:
        with self._lock:
            existing = self._rows.get(call_id)
            if existing is not None and existing["status"] != "released":
                raise ReservationConflictError(call_id)
            if worst_case_usd > limits.per_report_usd:
                raise BudgetExceededError("report_limit")
            if limits.daily_usd is not None and self._counted(_day_start(now), now) + worst_case_usd > limits.daily_usd:
                raise BudgetExceededError("daily_budget")
            if limits.monthly_usd is not None and self._counted(_month_start(now), now) + worst_case_usd > limits.monthly_usd:
                raise BudgetExceededError("monthly_budget")
            self._rows[call_id] = {"status": "reserved", "counted": worst_case_usd, "created_at": now,
                                   "kind": report_kind, "week_start": week_start, "fingerprint": fingerprint}

    def state(self, call_id: str) -> str | None:
        with self._lock:
            row = self._rows.get(call_id)
            return row["status"] if row else None

    def settle(self, call_id: str, actual_usd: Decimal) -> None:
        self._set(call_id, "settled", actual_usd)

    def release(self, call_id: str) -> None:
        self._set(call_id, "released", Decimal("0"))

    def mark_uncertain(self, call_id: str) -> None:
        self._set(call_id, "uncertain", None)

    def counted_between(self, start: datetime, end: datetime) -> Decimal:
        with self._lock:
            return self._counted(start, end)

    def _counted(self, start: datetime, end: datetime) -> Decimal:
        return sum((r["counted"] for r in self._rows.values()
                    if r["status"] != "released" and start <= r["created_at"] <= end), Decimal("0"))

    def _set(self, call_id: str, target: str, counted: Decimal | None) -> None:
        with self._lock:
            row = self._rows.get(call_id)
            if row is None or row["status"] != "reserved":
                raise LedgerStateError("not_reserved")
            row["status"] = target
            if counted is not None:
                row["counted"] = counted
