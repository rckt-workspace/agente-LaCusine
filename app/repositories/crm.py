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
        "return_request",
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

    def capture_referral(self, lead_id: UUID, referral: dict[str, str]) -> bool:
        """Guarda la atribución de anuncio solo la primera vez (primer contacto). Devuelve True si se guardó.

        Un mensaje posterior de otro anuncio no sobrescribe el origen comercial original.
        """
        try:
            with self._pool.connection() as conn:
                cursor = conn.execute(
                    """
                    update public.leads
                    set referral_source_type = %s, referral_source_id = %s, referral_source_url = %s,
                        referral_ctwa_clid = %s, referral_captured_at = now()
                    where id = %s and referral_captured_at is null
                    """,
                    (referral.get("source_type"), referral.get("source_id"), referral.get("source_url"),
                     referral.get("ctwa_clid"), lead_id),
                )
                return cursor.rowcount == 1
        except (psycopg.Error, PoolClosed):
            raise StorageError("referral_write_failed") from None


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
        window_trusted: bool = False,
    ) -> bool:
        """Registra un mensaje entrante. Devuelve False si ese wamid ya estaba registrado.

        window_trusted=True solo si received_at es la hora del evento de Meta y es confiable.
        Solo esos mensajes abren la ventana de atención de 24 horas.
        """
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
                         received_at, content_text, content_expires_at, window_trusted)
                    values (%s, 'inbound', %s, %s, %s, %s, %s, %s)
                    on conflict (wa_message_id) where wa_message_id is not null do nothing
                    returning id
                    """,
                    (conversation_id, wa_message_id, message_type, received_at, content_text, content_expires_at,
                     window_trusted),
                ).fetchone()
        except (psycopg.Error, PoolClosed):
            raise StorageError("message_write_failed") from None
        return row is not None


class PostgresHandoffRepository:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def open(self, conversation_id: UUID, reason: str, priority: str = "normal") -> UUID:
        """Abre una solicitud de asesor y transfiere el control de la conversación. Operación atómica.

        En la misma transacción:
          1. Registra la solicitud (idempotente: una activa por conversación).
          2. Pausa la automatización (automated -> paused_for_human). Solo la reactivación administrativa la revierte.
          3. Cancela las respuestas de IA pendientes de esa conversación: son anteriores a la transferencia.
        """
        if reason not in HANDOFF_REASONS:
            raise ValueError("handoff_reason_invalid")
        if priority not in HANDOFF_PRIORITIES:
            raise ValueError("handoff_priority_invalid")
        try:
            with self._pool.connection() as conn:
                with conn.transaction():
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
                    if row is not None:
                        # Misma transacción: una transferencia nunca existe sin su aviso encolado.
                        conn.execute(
                            """
                            insert into public.advisor_notifications (handoff_id, conversation_id, reason, priority)
                            values (%s, %s, %s, %s)
                            on conflict (handoff_id) do nothing
                            """,
                            (row[0], conversation_id, reason, priority),
                        )
                    conn.execute(
                        """
                        update public.conversations
                        set automation_state = 'paused_for_human'
                        where id = %s and status <> 'closed' and automation_state = 'automated'
                        """,
                        (conversation_id,),
                    )
                    conn.execute(
                        """
                        update public.outbound_messages
                        set delivery_status = 'cancelled', last_error_code = 'handoff_opened'
                        where conversation_id = %s and kind = 'reply' and delivery_status = 'pending'
                        """,
                        (conversation_id,),
                    )
        except (psycopg.Error, PoolClosed):
            raise StorageError("handoff_write_failed") from None
        if row is None:
            raise StorageError("handoff_not_found_after_upsert")
        return row[0]


class PostgresConversationStateReader:
    """Lectura del estado de automatización para decidir si el agente puede responder."""

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def automation_state(self, conversation_id: UUID) -> str | None:
        try:
            with self._pool.connection() as conn:
                row = conn.execute(
                    "select automation_state from public.conversations where id = %s", (conversation_id,)
                ).fetchone()
        except (psycopg.Error, PoolClosed):
            raise StorageError("conversation_read_failed") from None
        return row[0] if row else None


class PostgresRecentTurnsReader:
    """Últimos mensajes de UNA conversación con contenido vigente, en orden cronológico."""

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def recent_turns(self, conversation_id: UUID, limit: int) -> list[tuple[str, str, datetime]]:
        try:
            with self._pool.connection() as conn:
                rows = conn.execute(
                    """
                    select direction, content_text, received_at
                    from public.messages
                    where conversation_id = %s
                      and content_text is not null
                      and (content_expires_at is null or content_expires_at > now())
                    order by received_at desc
                    limit %s
                    """,
                    (conversation_id, limit),
                ).fetchall()
        except (psycopg.Error, PoolClosed):
            raise StorageError("message_read_failed") from None
        return [(row[0], row[1], row[2]) for row in reversed(rows)]
