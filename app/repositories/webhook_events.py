"""Persistencia idempotente de eventos entrantes de Meta.

La deduplicación la garantiza la base de datos (restricción única), no la memoria del proceso:
sobrevive a reinicios y es segura entre múltiples instancias concurrentes.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool, PoolClosed

from app.repositories.errors import StorageError

PROVIDER = "meta_whatsapp"


class RegisterOutcome(str, Enum):
    NEW = "new"
    DUPLICATE = "duplicate"
    # Mismo identificador con contenido distinto: anomalía a investigar, no se sobrescribe.
    CONFLICT = "conflict"


@dataclass(frozen=True)
class WebhookEventRecord:
    external_event_id: str
    contact_wa_id: str | None
    payload: dict[str, Any]
    payload_sha256: str
    event_type: str = "message"

    def __post_init__(self) -> None:
        if not 0 < len(self.external_event_id) <= 128:
            raise ValueError("external_event_id_length")
        if len(self.payload_sha256) != 64:
            raise ValueError("payload_sha256_length")


class WebhookEventRepository(Protocol):
    def register(self, record: WebhookEventRecord) -> RegisterOutcome:
        """Inserta el evento si es nuevo. Lanza StorageError si no puede garantizar el almacenamiento."""
        ...


_INSERT_SQL = """
    insert into public.webhook_events
        (provider, external_event_id, event_type, contact_wa_id, payload, payload_sha256, expires_at)
    values
        (%s, %s, %s, %s, %s, %s, now() + make_interval(days => %s))
    on conflict (provider, external_event_id) do nothing
    returning id
"""

_EXISTING_HASH_SQL = """
    select payload_sha256
    from public.webhook_events
    where provider = %s and external_event_id = %s
"""


class PostgresWebhookEventRepository:
    def __init__(self, pool: ConnectionPool, retention_days: int) -> None:
        if retention_days <= 0:
            raise ValueError("retention_days must be positive")
        self._pool = pool
        self._retention_days = retention_days

    def register(self, record: WebhookEventRecord) -> RegisterOutcome:
        try:
            with self._pool.connection() as conn:
                inserted = conn.execute(
                    _INSERT_SQL,
                    (
                        PROVIDER,
                        record.external_event_id,
                        record.event_type,
                        record.contact_wa_id,
                        Jsonb(record.payload),
                        record.payload_sha256,
                        self._retention_days,
                    ),
                ).fetchone()
                if inserted is not None:
                    return RegisterOutcome.NEW

                existing = conn.execute(_EXISTING_HASH_SQL, (PROVIDER, record.external_event_id)).fetchone()
        except (psycopg.Error, PoolClosed):
            # Se descarta la causa original: puede incluir fragmentos de datos.
            raise StorageError("webhook_event_write_failed") from None

        if existing is None:
            # Purgado entre el conflicto y la lectura: el reintento de Meta lo registrará como nuevo.
            raise StorageError("webhook_event_vanished")
        return RegisterOutcome.DUPLICATE if existing[0] == record.payload_sha256 else RegisterOutcome.CONFLICT
