"""Repositorios de prospectos, conversaciones, mensajes y solicitudes de asesor humano.

Cada operación es idempotente frente a concurrencia: las restricciones únicas parciales de la base
de datos resuelven las carreras; el código solo devuelve el identificador existente.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import psycopg
from psycopg_pool import ConnectionPool, PoolClosed

from app.repositories.errors import StorageError

HANDOFF_REASONS = frozenset(
    {
        "purchase_intent",
        "quote_request",
        "installation",
        "warranty",
        "complaint",
        "unsupported_request",
        "customer_request",
        "other",
    }
)
HANDOFF_PRIORITIES = frozenset({"low", "normal", "high", "urgent"})
MESSAGE_TYPES = frozenset(
    {"text", "image", "audio", "video", "document", "sticker", "location", "contacts", "interactive", "button", "unknown"}
)


class PostgresLeadRepository:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def get_or_create(self, wa_contact_id: str) -> UUID:
        # Dos sentencias: la segunda ve siempre la fila confirmada por la primera (autocommit por sentencia).
        try:
            with self._pool.connection() as conn:
                conn.execute(
                    "insert into public.leads (wa_contact_id) values (%s) on conflict (wa_contact_id) do nothing",
                    (wa_contact_id,),
                )
                row = conn.execute(
                    "select id from public.leads where wa_contact_id = %s", (wa_contact_id,)
                ).fetchone()
        except (psycopg.Error, PoolClosed):
            raise StorageError("lead_write_failed") from None
        if row is None:
            raise StorageError("lead_not_found_after_upsert")
        return row[0]


class PostgresConversationRepository:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def get_or_open(self, lead_id: UUID) -> UUID:
        """Devuelve la conversación abierta del prospecto o abre una nueva."""
        try:
            with self._pool.connection() as conn:
                conn.execute(
                    """
                    insert into public.conversations (lead_id) values (%s)
                    on conflict (lead_id) where status <> 'closed' do nothing
                    """,
                    (lead_id,),
                )
                row = conn.execute(
                    "select id from public.conversations where lead_id = %s and status <> 'closed'",
                    (lead_id,),
                ).fetchone()
        except (psycopg.Error, PoolClosed):
            raise StorageError("conversation_write_failed") from None
        if row is None:
            raise StorageError("conversation_not_found_after_upsert")
        return row[0]


class PostgresMessageRepository:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def record_inbound(
        self,
        conversation_id: UUID,
        wa_message_id: str,
        message_type: str,
        received_at: datetime,
        content_text: str | None = None,
        content_expires_at: datetime | None = None,
    ) -> bool:
        """Registra un mensaje entrante. Devuelve False si ese wamid ya estaba registrado."""
        if message_type not in MESSAGE_TYPES:
            raise ValueError("message_type_invalid")
        if content_text is not None and content_expires_at is None:
            # Regla de retención: todo contenido debe tener fecha de expiración.
            raise ValueError("content_requires_expiry")
        try:
            with self._pool.connection() as conn:
                row = conn.execute(
                    """
                    insert into public.messages
                        (conversation_id, direction, wa_message_id, message_type,
                         received_at, content_text, content_expires_at)
                    values (%s, 'inbound', %s, %s, %s, %s, %s)
                    on conflict (wa_message_id) where wa_message_id is not null do nothing
                    returning id
                    """,
                    (conversation_id, wa_message_id, message_type, received_at, content_text, content_expires_at),
                ).fetchone()
        except (psycopg.Error, PoolClosed):
            raise StorageError("message_write_failed") from None
        return row is not None


class PostgresHandoffRepository:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def open(self, conversation_id: UUID, reason: str, priority: str = "normal") -> UUID:
        """Abre una solicitud de asesor. Si ya existe una activa para la conversación, devuelve esa."""
        if reason not in HANDOFF_REASONS:
            raise ValueError("handoff_reason_invalid")
        if priority not in HANDOFF_PRIORITIES:
            raise ValueError("handoff_priority_invalid")
        try:
            with self._pool.connection() as conn:
                conn.execute(
                    """
                    insert into public.human_handoffs (conversation_id, reason, priority) values (%s, %s, %s)
                    on conflict (conversation_id) where status in ('open', 'assigned') do nothing
                    """,
                    (conversation_id, reason, priority),
                )
                row = conn.execute(
                    """
                    select id from public.human_handoffs
                    where conversation_id = %s and status in ('open', 'assigned')
                    """,
                    (conversation_id,),
                ).fetchone()
        except (psycopg.Error, PoolClosed):
            raise StorageError("handoff_write_failed") from None
        if row is None:
            raise StorageError("handoff_not_found_after_upsert")
        return row[0]
