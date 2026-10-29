"""Outbox transaccional de respuestas.

Distinción obligatoria:
  respuesta GENERADA  -> fila en outbound_messages con delivery_status = 'pending' (esta fase termina aquí)
  respuesta ENVIADA   -> el adaptador de WhatsApp la marca 'sent' (fase posterior)
  respuesta ENTREGADA -> confirmada por Meta ('delivered')
Crear la fila nunca significa que el cliente recibió el mensaje.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

import psycopg
from psycopg_pool import ConnectionPool, PoolClosed

from app.repositories.errors import StorageError

MAX_REPLY_CHARS = 1200


class OutboxRepository:
    def __init__(self, pool: ConnectionPool, retention_days: int = 7) -> None:
        self._pool = pool
        self._retention = timedelta(days=retention_days)

    def persist_result(
        self,
        *,
        event_id: UUID,
        idempotency_key: str,
        conversation_id: UUID,
        lead_id: UUID,
        lead_status: str | None,
        reply_text: str | None,
    ) -> bool:
        """Crea la respuesta pendiente y actualiza el estado comercial en UNA transacción.

        Devuelve True si se creó una respuesta nueva; False si ya existía (reintento tras reinicio).
        La restricción única sobre idempotency_key y sobre webhook_event_id impide duplicados.
        """
        if reply_text is not None and not 0 < len(reply_text) <= MAX_REPLY_CHARS:
            raise ValueError("reply_text_length")
        try:
            with self._pool.connection() as conn:
                with conn.transaction():
                    created = False
                    if reply_text is not None:
                        row = conn.execute(
                            """
                            insert into public.outbound_messages
                                (idempotency_key, webhook_event_id, conversation_id, content_text, expires_at)
                            values (%s, %s, %s, %s, now() + make_interval(days => %s))
                            on conflict do nothing
                            returning id
                            """,
                            (idempotency_key, event_id, conversation_id, reply_text, self._retention.days),
                        ).fetchone()
                        created = row is not None
                    if lead_status is not None:
                        conn.execute(
                            "update public.leads set status = %s where id = %s",
                            (lead_status, lead_id),
                        )
                    return created
        except (psycopg.Error, PoolClosed):
            raise StorageError("outbox_write_failed") from None

    def pending_count(self) -> int:
        try:
            with self._pool.connection() as conn:
                row = conn.execute(
                    "select count(*) from public.outbound_messages where delivery_status = 'pending'"
                ).fetchone()
        except (psycopg.Error, PoolClosed):
            raise StorageError("outbox_read_failed") from None
        return int(row[0])
