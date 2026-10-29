"""Persistencia de la auditoría de uso de Claude. Sin prompts ni respuestas: solo metadatos y conteos."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import psycopg
from psycopg_pool import ConnectionPool, PoolClosed

from app.repositories.errors import StorageError
from app.services.cost_tracker import UsageEvent


class PostgresUsageStore:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def record_success_once(self, event: UsageEvent) -> bool:
        """Devuelve True si el registro es nuevo. Un call_id ya exitoso no se vuelve a contar."""
        try:
            with self._pool.connection() as conn:
                row = conn.execute(
                    """
                    insert into public.llm_usage_events
                        (call_id, conversation_id, provider, model, purpose, status,
                         input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
                         estimated_cost_usd, pricing_reference)
                    values (%s, %s, %s, %s, 'commercial_reply', 'succeeded', %s, %s, %s, %s, %s, %s)
                    on conflict (call_id) where status = 'succeeded' do nothing
                    returning id
                    """,
                    (
                        event.call_id, event.conversation_id, event.provider, event.model,
                        event.usage.input_tokens, event.usage.output_tokens,
                        event.usage.cache_read_tokens, event.usage.cache_write_tokens,
                        event.cost_usd, event.pricing_reference,
                    ),
                ).fetchone()
        except (psycopg.Error, PoolClosed):
            raise StorageError("usage_write_failed") from None
        return row is not None

    def record_failure(self, event: UsageEvent) -> None:
        try:
            with self._pool.connection() as conn:
                conn.execute(
                    """
                    insert into public.llm_usage_events
                        (call_id, conversation_id, provider, model, purpose, status, error_code)
                    values (%s, %s, %s, %s, 'commercial_reply', 'failed', %s)
                    """,
                    (event.call_id, event.conversation_id, event.provider, event.model, event.error_code),
                )
        except (psycopg.Error, PoolClosed):
            raise StorageError("usage_write_failed") from None

    def has_success(self, call_id: str) -> bool:
        try:
            with self._pool.connection() as conn:
                row = conn.execute(
                    "select 1 from public.llm_usage_events where call_id = %s and status = 'succeeded'",
                    (call_id,),
                ).fetchone()
        except (psycopg.Error, PoolClosed):
            raise StorageError("usage_read_failed") from None
        return row is not None

    def spent_between(self, start: datetime, end: datetime) -> Decimal:
        try:
            with self._pool.connection() as conn:
                row = conn.execute(
                    """
                    select coalesce(sum(estimated_cost_usd), 0)
                    from public.llm_usage_events
                    where status = 'succeeded' and created_at >= %s and created_at < %s
                    """,
                    (start, end),
                ).fetchone()
        except (psycopg.Error, PoolClosed):
            raise StorageError("usage_read_failed") from None
        return Decimal(row[0])
