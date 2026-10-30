"""Operaciones de conciliación. Cada cambio es transaccional y deja auditoría en la misma transacción.

Patrón de cada operación:
  1. Bloquear la fila (SELECT ... FOR UPDATE). Dos operadores no pueden decidir sobre el mismo caso a la vez.
  2. Validar la transición contra el estado leído bajo bloqueo (política pura de policy.py).
  3. Aplicar el cambio con una condición sobre el estado esperado.
  4. Insertar la auditoría (operador, motivo, referencia de evidencia; nunca texto del cliente).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg
from psycopg.errors import UniqueViolation
from psycopg_pool import ConnectionPool

from app.admin.policy import (
    AdminPolicyError,
    check_amount,
    check_evidence,
    check_reason,
    check_resume,
    check_transition,
)

TARGET_TYPES = ("webhook_event", "outbound_message", "budget_reservation", "human_handoff", "conversation")


@dataclass(frozen=True)
class AdminContext:
    operator: str
    environment: str

    def __post_init__(self) -> None:
        if not 1 <= len(self.operator.strip()) <= 120:
            raise AdminPolicyError("operator_required")


class AdminService:
    def __init__(self, pool: ConnectionPool, context: AdminContext) -> None:
        self._pool = pool
        self._ctx = context

    # --- Solo lectura ---------------------------------------------------------

    def counts(self) -> dict[str, dict[str, int]]:
        with self._pool.connection() as conn:
            queries = {
                "webhook_events": "select processing_status, count(*) from public.webhook_events group by 1",
                "outbound_messages": "select delivery_status, count(*) from public.outbound_messages group by 1",
                "budget_reservations": "select status, count(*) from public.budget_reservations group by 1",
                "human_handoffs": "select status, count(*) from public.human_handoffs group by 1",
                "conversations": "select automation_state, count(*) from public.conversations group by 1",
                "advisor_notifications": "select status, count(*) from public.advisor_notifications group by 1",
            }
            return {name: {str(k): int(v) for k, v in conn.execute(sql).fetchall()} for name, sql in queries.items()}

    def inspect(self, target_type: str, target_id: UUID | str) -> dict[str, Any]:
        """Metadatos del caso. Nunca incluye texto del cliente, payloads ni datos de contacto."""
        if target_type not in TARGET_TYPES:
            raise AdminPolicyError("target_type_invalid")
        if target_type != "budget_reservation" and not isinstance(target_id, UUID):
            raise AdminPolicyError("id_invalid")
        with self._pool.connection() as conn:
            if target_type == "webhook_event":
                row = conn.execute(
                    "select id, event_type, processing_status, attempts, last_error_code, received_at, "
                    "next_attempt_at, lease_expires_at, (payload is not null) from public.webhook_events where id = %s",
                    (target_id,),
                ).fetchone()
                keys = ("id", "event_type", "status", "attempts", "last_error_code", "received_at",
                        "next_attempt_at", "lease_expires_at", "payload_retained")
            elif target_type == "outbound_message":
                row = conn.execute(
                    "select id, kind, delivery_status, attempts, last_error_code, external_message_id, sent_at, "
                    "delivered_at, read_at, failed_at, length(content_text), business_phone_id "
                    "from public.outbound_messages where id = %s",
                    (target_id,),
                ).fetchone()
                keys = ("id", "kind", "status", "attempts", "last_error_code", "external_message_id", "sent_at",
                        "delivered_at", "read_at", "failed_at", "content_length", "business_phone_id")
            elif target_type == "budget_reservation":
                # Una reserva se identifica por call_id (texto) o por su UUID interno.
                if isinstance(target_id, UUID):
                    where, value = "id = %s", target_id
                else:
                    where, value = "call_id = %s", str(target_id)
                row = conn.execute(
                    "select id, call_id, status, reserved_usd, counted_usd, created_at, reconciled_at "
                    f"from public.budget_reservations where {where}",
                    (value,),
                ).fetchone()
                keys = ("id", "call_id", "status", "reserved_usd", "counted_usd", "created_at", "reconciled_at")
            elif target_type == "human_handoff":
                row = conn.execute(
                    "select id, conversation_id, reason, status, priority, assigned_advisor, opened_at, closed_at "
                    "from public.human_handoffs where id = %s",
                    (target_id,),
                ).fetchone()
                keys = ("id", "conversation_id", "reason", "status", "priority", "assigned_advisor", "opened_at",
                        "closed_at")
            else:
                row = conn.execute(
                    "select id, automation_state, status, last_activity_at from public.conversations where id = %s",
                    (target_id,),
                ).fetchone()
                keys = ("id", "automation_state", "status", "last_activity_at")
        if row is None:
            raise AdminPolicyError("not_found")
        return {key: _safe(value) for key, value in zip(keys, row)}

    def audit_trail(self, target_type: str, target_id: UUID) -> list[dict[str, Any]]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "select created_at, operator, action, from_state, to_state, reason, evidence_ref "
                "from public.admin_audit_log where target_type = %s and target_id = %s order by created_at",
                (target_type, target_id),
            ).fetchall()
        return [dict(zip(("at", "operator", "action", "from", "to", "reason", "evidence"), map(_safe, r))) for r in rows]

    # --- Transiciones ---------------------------------------------------------

    def resolve_event(self, event_id: UUID, to_state: str, reason: str) -> None:
        reason = check_reason(reason)
        with self._transaction() as conn:
            row = _lock(conn, "webhook_events", event_id, "processing_status")
            check_transition("webhook_event", to_state, row)
            if to_state == "retry":
                # No se reencola un evento cuyo cálculo presupuestario sigue sin conciliar.
                pending = conn.execute(
                    "select count(*) from public.budget_reservations where call_id = "
                    "(select external_event_id from public.webhook_events where id = %s) "
                    "and status in ('reserved', 'uncertain')",
                    (event_id,),
                ).fetchone()[0]
                if pending:
                    raise AdminPolicyError("budget_unreconciled")
                conn.execute(
                    "update public.webhook_events set processing_status = 'retry', attempts = 0, "
                    "last_error_code = 'admin_requeued', next_attempt_at = now() "
                    "where id = %s and processing_status = 'manual_review'",
                    (event_id,),
                )
            else:
                conn.execute(
                    "update public.webhook_events set processing_status = 'dead_letter', "
                    "last_error_code = 'admin_closed' where id = %s and processing_status = 'manual_review'",
                    (event_id,),
                )
            self._audit(conn, "webhook_event", event_id, "resolve", row, to_state, reason, None)

    def resolve_outbound(self, outbound_id: UUID, to_state: str, reason: str, evidence: str | None) -> None:
        reason = check_reason(reason)
        with self._transaction() as conn:
            row = _lock(conn, "outbound_messages", outbound_id, "delivery_status")
            transition = check_transition("outbound_message", to_state, row)
            check_evidence(transition, evidence)
            try:
                updated = conn.execute(
                    "update public.outbound_messages set delivery_status = %s, "
                    "external_message_id = case when %s = 'sent' then %s else external_message_id end, "
                    "sent_at = case when %s = 'sent' then now() else sent_at end, "
                    "failed_at = case when %s = 'failed' then now() else failed_at end, "
                    "last_error_code = 'admin_reconciled' "
                    "where id = %s and delivery_status = 'uncertain' returning id",
                    (to_state, to_state, evidence.strip() if to_state == "sent" else None, to_state, to_state,
                     outbound_id),
                ).fetchone()
            except UniqueViolation:
                raise AdminPolicyError("external_id_already_used") from None
            if updated is None:
                raise AdminPolicyError("state_changed_concurrently")
            self._audit(conn, "outbound_message", outbound_id, "reconcile", row, to_state, reason, evidence)

    def resolve_reservation(self, reservation_id: UUID, to_state: str, reason: str, evidence: str | None,
                            actual_usd: Decimal | None = None) -> None:
        reason = check_reason(reason)
        with self._transaction() as conn:
            row = conn.execute(
                "select status, reserved_usd from public.budget_reservations where id = %s for update",
                (reservation_id,),
            ).fetchone()
            if row is None:
                raise AdminPolicyError("not_found")
            current, reserved = row
            transition = check_transition("budget_reservation", to_state, current)
            check_evidence(transition, evidence)
            check_amount(transition, Decimal(reserved), actual_usd)
            counted = Decimal("0") if to_state == "released" else (actual_usd if to_state == "settled" else reserved)
            updated = conn.execute(
                "update public.budget_reservations set status = %s, counted_usd = %s, reconciled_at = now() "
                "where id = %s and status = %s returning id",
                (to_state, counted, reservation_id, current),
            ).fetchone()
            if updated is None:
                raise AdminPolicyError("state_changed_concurrently")
            self._audit(conn, "budget_reservation", reservation_id, "reconcile", current, to_state, reason, evidence)

    def resolve_handoff(self, handoff_id: UUID, to_state: str, reason: str, advisor: str | None = None) -> None:
        reason = check_reason(reason)
        with self._transaction() as conn:
            row = conn.execute(
                "select status, conversation_id from public.human_handoffs where id = %s for update",
                (handoff_id,),
            ).fetchone()
            if row is None:
                raise AdminPolicyError("not_found")
            current, _conversation = row
            check_transition("human_handoff", to_state, current)
            if to_state == "assigned" and not (advisor and advisor.strip()):
                raise AdminPolicyError("advisor_required")
            closes = to_state in ("resolved", "cancelled")
            updated = conn.execute(
                "update public.human_handoffs set status = %s, "
                "assigned_advisor = coalesce(%s, assigned_advisor), "
                "closed_at = case when %s then now() else closed_at end "
                "where id = %s and status = %s returning id",
                (to_state, advisor.strip() if advisor else None, closes, handoff_id, current),
            ).fetchone()
            if updated is None:
                raise AdminPolicyError("state_changed_concurrently")
            # Cerrar una solicitud NO reactiva la IA. Eso exige resume_conversation.
            self._audit(conn, "human_handoff", handoff_id, "transition", current, to_state, reason, None)

    def resume_conversation(self, conversation_id: UUID, reason: str) -> None:
        reason = check_reason(reason)
        with self._transaction() as conn:
            row = _lock(conn, "conversations", conversation_id, "automation_state")
            active = conn.execute(
                "select count(*) from public.human_handoffs where conversation_id = %s "
                "and status in ('open', 'assigned')",
                (conversation_id,),
            ).fetchone()[0]
            check_resume(row, active)
            updated = conn.execute(
                "update public.conversations set automation_state = 'automated' "
                "where id = %s and automation_state = %s returning id",
                (conversation_id, row),
            ).fetchone()
            if updated is None:
                raise AdminPolicyError("state_changed_concurrently")
            self._audit(conn, "conversation", conversation_id, "resume_automation", row, "automated", reason, None)

    # --- Internos -------------------------------------------------------------

    def _transaction(self):
        return _Tx(self._pool)

    def _audit(self, conn, target_type, target_id, action, from_state, to_state, reason, evidence) -> None:
        conn.execute(
            "insert into public.admin_audit_log (operator, action, target_type, target_id, from_state, to_state, "
            "reason, evidence_ref) values (%s, %s, %s, %s, %s, %s, %s, %s)",
            (self._ctx.operator.strip(), action, target_type, target_id, str(from_state), str(to_state), reason,
             evidence.strip()[:200] if evidence else None),
        )


class _Tx:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool
        self._conn_cm = None
        self._tx_cm = None

    def __enter__(self):
        self._conn_cm = self._pool.connection()
        conn = self._conn_cm.__enter__()
        self._tx_cm = conn.transaction()
        self._tx_cm.__enter__()
        return conn

    def __exit__(self, exc_type, exc, tb):
        self._tx_cm.__exit__(exc_type, exc, tb)
        self._conn_cm.__exit__(exc_type, exc, tb)
        return False


def _lock(conn: psycopg.Connection, table: str, row_id: UUID, column: str) -> str:
    # Nombres de tabla y columna fijos en código: nunca proceden de la entrada del operador.
    if (table, column) not in {
        ("webhook_events", "processing_status"), ("outbound_messages", "delivery_status"),
        ("conversations", "automation_state"),
    }:
        raise AdminPolicyError("internal_lock_target")
    row = conn.execute(f"select {column} from public.{table} where id = %s for update", (row_id,)).fetchone()
    if row is None:
        raise AdminPolicyError("not_found")
    return row[0]


def _safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value
