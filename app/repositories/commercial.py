"""Persistencia de eventos comerciales, historial de etapas y ventas confirmadas. Parametrizado y transaccional."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

import psycopg
from psycopg_pool import ConnectionPool, PoolClosed

from app.repositories.errors import StorageError


class SaleConflictError(Exception):
    """Ya existe una venta con ese identificador y datos distintos. No se sobrescribe nada."""

    code = "sale_conflict"


class SaleStateError(Exception):
    """La venta no está en un estado que permita la operación pedida."""

    code = "sale_state_invalid"


@dataclass(frozen=True)
class SaleRecord:
    sale_ref: str
    lead_id: UUID
    amount_cop: int
    sold_at: datetime
    confirmation_source: str
    confirmed_by: str
    evidence_ref: str


class PostgresCommercialRepository:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def record_event(self, *, event_type: str, occurred_at: datetime, lead_id: UUID, dedupe_key: str,
                     conversation_id: UUID | None = None, category: str | None = None,
                     product_id: UUID | None = None, budget_cop: int | None = None,
                     source_event_id: str | None = None) -> bool:
        """Registra un evento. Devuelve False si ya existía (reintento): nunca duplica."""
        try:
            with self._pool.connection() as conn:
                row = conn.execute(
                    """insert into public.commercial_events
                         (occurred_at, event_type, lead_id, conversation_id, category, product_id, budget_cop,
                          source_event_id, dedupe_key)
                       values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                       on conflict (dedupe_key) do nothing
                       returning id""",
                    (occurred_at, event_type, lead_id, conversation_id, category, product_id, budget_cop,
                     source_event_id, dedupe_key),
                ).fetchone()
                return row is not None
        except (psycopg.Error, PoolClosed):
            raise StorageError("commercial_event_write_failed") from None

    def record_stage_change(self, *, lead_id: UUID, from_status: str | None, to_status: str, reason: str,
                            changed_by: str, changed_at: datetime) -> None:
        try:
            with self._pool.connection() as conn:
                conn.execute(
                    """insert into public.lead_stage_history (lead_id, from_status, to_status, reason, changed_by, changed_at)
                       values (%s, %s, %s, %s, %s, %s)""",
                    (lead_id, from_status, to_status, reason, changed_by, changed_at),
                )
        except (psycopg.Error, PoolClosed):
            raise StorageError("stage_history_write_failed") from None

    def confirm_sale(self, sale: SaleRecord, product_quantities: dict[UUID, int]) -> str:
        """Confirma una venta de forma transaccional. Devuelve 'created' o 'duplicate' (mismos datos)."""
        try:
            with self._pool.connection() as conn:
                with conn.transaction():
                    row = conn.execute(
                        """insert into public.sales
                             (sale_ref, lead_id, amount_cop, sold_at, confirmation_source, confirmed_by, evidence_ref)
                           values (%s, %s, %s, %s, %s, %s, %s)
                           on conflict (sale_ref) do nothing
                           returning id""",
                        (sale.sale_ref, sale.lead_id, sale.amount_cop, sale.sold_at, sale.confirmation_source,
                         sale.confirmed_by, sale.evidence_ref),
                    ).fetchone()
                    if row is None:
                        existing = conn.execute(
                            """select lead_id, amount_cop, sold_at, confirmation_source, evidence_ref
                               from public.sales where sale_ref = %s""",
                            (sale.sale_ref,),
                        ).fetchone()
                        same = existing is not None and (
                            existing[0] == sale.lead_id and int(existing[1]) == sale.amount_cop
                            and existing[2] == sale.sold_at and existing[3] == sale.confirmation_source
                            and existing[4] == sale.evidence_ref)
                        if not same:
                            raise SaleConflictError()
                        return "duplicate"
                    sale_id = row[0]
                    for product_id, quantity in product_quantities.items():
                        conn.execute(
                            "insert into public.sale_products (sale_id, product_id, quantity) values (%s, %s, %s)",
                            (sale_id, product_id, quantity),
                        )
                    conn.execute(
                        """insert into public.sale_status_events (sale_id, status, reason, actor)
                           values (%s, 'confirmed', %s, %s)""",
                        (sale_id, "confirmación administrativa", sale.confirmed_by),
                    )
                    return "created"
        except SaleConflictError:
            raise
        except (psycopg.Error, PoolClosed):
            raise StorageError("sale_write_failed") from None

    def cancel_sale(self, sale_ref: str, *, reason: str, actor: str) -> None:
        """Cancela una venta confirmada. Añade un evento; no borra la venta ni su historial."""
        try:
            with self._pool.connection() as conn:
                with conn.transaction():
                    row = conn.execute(
                        "select id from public.sales where sale_ref = %s for update", (sale_ref,)
                    ).fetchone()
                    if row is None:
                        raise SaleStateError()
                    current = conn.execute(
                        """select status from public.sale_status_events where sale_id = %s
                           order by occurred_at desc, id desc limit 1""",
                        (row[0],),
                    ).fetchone()
                    if current is None or current[0] != "confirmed":
                        raise SaleStateError()
                    conn.execute(
                        "insert into public.sale_status_events (sale_id, status, reason, actor) values (%s, 'cancelled', %s, %s)",
                        (row[0], reason, actor),
                    )
        except SaleStateError:
            raise
        except (psycopg.Error, PoolClosed):
            raise StorageError("sale_cancel_failed") from None
