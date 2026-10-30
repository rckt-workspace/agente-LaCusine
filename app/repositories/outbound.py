"""Repositorio del outbox de salida: reclamo con lease, transiciones de estado y expiración de envíos.

Garantías:
  * Reclamo atómico (SKIP LOCKED) y propiedad por token: solo el dueño puede cerrar el envío.
  * Orden por conversación: no se reclama un mensaje si hay uno anterior de la misma conversación
    pendiente, en envío o incierto.
  * Un envío con lease vencido pasa a 'uncertain': nunca vuelve a 'pending' por sí solo, porque
    la solicitud pudo haber llegado a Meta.
  * Las transiciones de estado de Meta solo avanzan según una tabla explícita. Los estados
    fuera de orden nunca retroceden.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import psycopg
from psycopg_pool import ConnectionPool, PoolClosed

from app.repositories.errors import StorageError

# Transiciones que Meta puede provocar: (estado actual, estado nuevo).
# 'uncertain' significa que no sabemos si Meta aceptó; un estado de Meta lo confirma o lo corrige.
ALLOWED_META_TRANSITIONS: frozenset[tuple[str, str]] = frozenset({
    ("uncertain", "sent"), ("uncertain", "delivered"), ("uncertain", "read"), ("uncertain", "failed"),
    ("sent", "delivered"), ("sent", "read"), ("sent", "failed"),
    ("delivered", "read"),
})
META_STATUSES = frozenset({"sent", "delivered", "read", "failed"})
FINISH_STATES = frozenset({"sent", "failed", "uncertain", "blocked", "cancelled", "pending"})


@dataclass(frozen=True)
class OutboundClaim:
    outbound_id: UUID
    idempotency_key: str
    conversation_id: UUID
    content_text: str
    attempts: int
    lease_token: UUID
    kind: str = "reply"
    business_phone_id: str | None = None


@dataclass(frozen=True)
class DispatchContext:
    automation_state: str
    wa_contact_id: str
    last_inbound_at: datetime | None


class OutboundRepository:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def claim(self, limit: int, owner: str, lease_seconds: int, token: UUID) -> list[OutboundClaim]:
        if limit < 1:
            return []
        try:
            with self._pool.connection() as conn:
                with conn.transaction():
                    rows = conn.execute(
                        """
                        with candidates as (
                            select o.id
                            from public.outbound_messages o
                            where o.delivery_status = 'pending'
                              and o.next_attempt_at <= now()
                              and not exists (
                                  select 1 from public.outbound_messages p
                                  where p.conversation_id = o.conversation_id
                                    and (p.created_at, p.id) < (o.created_at, o.id)
                                    and p.delivery_status in ('pending', 'sending', 'uncertain')
                              )
                            order by o.created_at, o.id
                            limit %(limit)s
                            for update skip locked
                        )
                        update public.outbound_messages m
                        set delivery_status = 'sending',
                            lease_token = %(token)s,
                            lease_owner = %(owner)s,
                            lease_expires_at = now() + make_interval(secs => %(secs)s),
                            attempts = m.attempts + 1
                        from candidates
                        where m.id = candidates.id
                        returning m.id, m.idempotency_key, m.conversation_id, m.content_text, m.attempts,
                                  m.kind, m.business_phone_id
                        """,
                        {"limit": limit, "token": token, "owner": owner, "secs": lease_seconds},
                    ).fetchall()
        except (psycopg.Error, PoolClosed):
            raise StorageError("outbound_claim_failed") from None
        return [OutboundClaim(r[0], r[1], r[2], r[3], r[4], token, kind=r[5], business_phone_id=r[6]) for r in rows]

    def expire_leases(self) -> int:
        """Envíos con lease vencido: el resultado es desconocido, así que pasan a 'uncertain'."""
        try:
            with self._pool.connection() as conn:
                rows = conn.execute(
                    """
                    update public.outbound_messages
                    set delivery_status = 'uncertain', lease_token = null, lease_owner = null,
                        lease_expires_at = null, last_error_code = 'lease_expired'
                    where delivery_status = 'sending' and lease_expires_at < now()
                    returning id
                    """
                ).fetchall()
        except (psycopg.Error, PoolClosed):
            raise StorageError("outbound_expire_failed") from None
        return len(rows)

    def dispatch_context(self, outbound_id: UUID) -> DispatchContext | None:
        try:
            with self._pool.connection() as conn:
                row = conn.execute(
                    """
                    select c.automation_state, l.wa_contact_id,
                           (select max(m.received_at) from public.messages m
                             where m.conversation_id = o.conversation_id
                               and m.direction = 'inbound' and m.window_trusted)
                    from public.outbound_messages o
                    join public.conversations c on c.id = o.conversation_id
                    join public.leads l on l.id = c.lead_id
                    where o.id = %s
                    """,
                    (outbound_id,),
                ).fetchone()
        except (psycopg.Error, PoolClosed):
            raise StorageError("outbound_read_failed") from None
        if row is None:
            return None
        return DispatchContext(automation_state=row[0], wa_contact_id=row[1], last_inbound_at=row[2])

    def finish(
        self,
        claim: OutboundClaim,
        status: str,
        *,
        external_message_id: str | None = None,
        error_code: str | None = None,
        retry_after_seconds: int | None = None,
        from_phone_id: str | None = None,
    ) -> bool:
        """Cierra el envío solo si este token sigue siendo el dueño. Devuelve False si el lease se perdió."""
        if status not in FINISH_STATES:
            raise ValueError("finish_status_invalid")
        if status == "pending" and retry_after_seconds is None:
            raise ValueError("retry_requires_delay")
        try:
            with self._pool.connection() as conn:
                with conn.transaction():
                    row = conn.execute(
                        """
                        update public.outbound_messages
                        set delivery_status = %(status)s,
                            external_message_id = coalesce(%(external)s, external_message_id),
                            last_error_code = %(error)s,
                            from_phone_id = coalesce(%(phone)s, from_phone_id),
                            next_attempt_at = case when %(retry)s::int is null then next_attempt_at
                                                   else now() + make_interval(secs => %(retry)s::int) end,
                            sent_at = case when %(status)s = 'sent' then now() else sent_at end,
                            failed_at = case when %(status)s = 'failed' then now() else failed_at end,
                            lease_token = null, lease_owner = null, lease_expires_at = null
                        where id = %(id)s and lease_token = %(token)s and delivery_status = 'sending'
                        returning id
                        """,
                        {"status": status, "external": external_message_id, "error": error_code,
                         "phone": from_phone_id, "retry": retry_after_seconds, "id": claim.outbound_id,
                         "token": claim.lease_token},
                    ).fetchone()
                    return row is not None
        except (psycopg.Error, PoolClosed):
            raise StorageError("outbound_finish_failed") from None

    def apply_meta_status(self, external_message_id: str, new_status: str, error_code: str | None = None,
                          at: datetime | None = None) -> str:
        """Aplica un estado de Meta. Devuelve 'applied', 'unchanged' o 'unmatched' (aún sin identificador)."""
        if new_status not in META_STATUSES:
            return "unchanged"
        try:
            with self._pool.connection() as conn:
                with conn.transaction():
                    row = conn.execute(
                        "select id, delivery_status from public.outbound_messages "
                        "where external_message_id = %s for update",
                        (external_message_id,),
                    ).fetchone()
                    if row is None:
                        return "unmatched"
                    outbound_id, current = row
                    if (current, new_status) not in ALLOWED_META_TRANSITIONS:
                        return "unchanged"
                    conn.execute(
                        """
                        update public.outbound_messages
                        set delivery_status = %(new)s,
                            sent_at = coalesce(sent_at, case when %(new)s = 'sent' then now() end),
                            delivered_at = case when %(new)s = 'delivered' then now() else delivered_at end,
                            read_at = case when %(new)s = 'read' then now() else read_at end,
                            failed_at = case when %(new)s = 'failed' then now() else failed_at end,
                            last_error_code = coalesce(%(error)s, last_error_code)
                        where id = %(id)s
                        """,
                        {"new": new_status, "error": error_code, "id": outbound_id},
                    )
                    return "applied"
        except (psycopg.Error, PoolClosed):
            raise StorageError("outbound_status_failed") from None

    def counts(self) -> dict[str, int]:
        try:
            with self._pool.connection() as conn:
                rows = conn.execute(
                    "select delivery_status, count(*) from public.outbound_messages group by delivery_status"
                ).fetchall()
        except (psycopg.Error, PoolClosed):
            raise StorageError("outbound_metrics_failed") from None
        return {status: int(count) for status, count in rows}
