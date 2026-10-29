"""Cola persistente de eventos de WhatsApp sobre webhook_events (ADR 0001).

Garantías:
  * Adquisición atómica: SELECT ... FOR UPDATE SKIP LOCKED en una transacción corta. Nunca se mantiene
    un bloqueo durante una llamada a IA.
  * Propiedad por token: cada adquisición genera un lease_token. Solo ese token puede renovar,
    finalizar o liberar el trabajo. Un worker antiguo cuyo lease expiró no puede sobrescribir resultados.
  * Orden por contacto: solo se reclama el evento más antiguo no finalizado de cada contacto
    (business_phone_id + wa_id). Además, contact_leases impone exclusión entre workers.
  * Recuperación: un lease vencido vuelve a 'retry' (o a 'manual_review' si se agotaron los intentos).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
from psycopg_pool import ConnectionPool, PoolClosed

from app.repositories.errors import StorageError

FINAL_STATES = frozenset({"processed", "ignored", "dead_letter"})
ALLOWED_FINISH_STATES = frozenset({"processed", "ignored", "retry", "manual_review", "dead_letter"})
PENDING_STATES = ("received", "queued", "retry")

_CONTACT_KEY = "e.business_phone_id || ':' || coalesce(e.contact_wa_id, 'event:' || e.id::text)"

_CANDIDATES_SQL = f"""
    select e.id, e.external_event_id, e.contact_wa_id, e.business_phone_id, e.payload, e.attempts,
           e.received_at, {_CONTACT_KEY} as contact_key
    from public.webhook_events e
    where e.processing_status in ('received', 'queued', 'retry')
      and e.next_attempt_at <= now()
      and not exists (
          select 1 from public.webhook_events o
          where o.business_phone_id = e.business_phone_id
            and coalesce(o.contact_wa_id, 'event:' || o.id::text) = coalesce(e.contact_wa_id, 'event:' || e.id::text)
            and (o.received_at, o.id) < (e.received_at, e.id)
            and o.processing_status in ('received', 'queued', 'retry', 'processing', 'manual_review')
      )
    order by e.received_at, e.id
    limit %(limit)s
    for update of e skip locked
"""

_ACQUIRE_CONTACT_SQL = """
    insert into public.contact_leases (contact_key, lease_token, lease_owner, lease_expires_at)
    values (%(key)s, %(token)s, %(owner)s, now() + make_interval(secs => %(secs)s))
    on conflict (contact_key) do update
        set lease_token = excluded.lease_token,
            lease_owner = excluded.lease_owner,
            lease_expires_at = excluded.lease_expires_at
        where public.contact_leases.lease_expires_at < now()
    returning contact_key
"""

_MARK_PROCESSING_SQL = """
    update public.webhook_events
    set processing_status = 'processing',
        lease_token = %(token)s,
        lease_owner = %(owner)s,
        lease_expires_at = now() + make_interval(secs => %(secs)s),
        attempts = attempts + 1,
        last_attempt_at = now()
    where id = %(id)s and processing_status in ('received', 'queued', 'retry')
    returning id
"""


@dataclass(frozen=True)
class Claim:
    event_id: UUID
    external_event_id: str
    contact_wa_id: str | None
    business_phone_id: str
    contact_key: str
    lease_token: UUID
    attempts: int
    payload: dict[str, Any]
    received_at: datetime


class JobRepository:
    def __init__(self, pool: ConnectionPool, owner: str, lease_seconds: int) -> None:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        self._pool = pool
        self._owner = owner
        self._lease_seconds = lease_seconds

    @property
    def owner(self) -> str:
        return self._owner

    def claim(self, limit: int, token_factory) -> list[Claim]:
        """Reclama hasta `limit` eventos. Transacción corta: no se llama a ningún proveedor aquí."""
        if limit < 1:
            return []
        claims: list[Claim] = []
        try:
            with self._pool.connection() as conn:
                with conn.transaction():
                    candidates = conn.execute(_CANDIDATES_SQL, {"limit": limit}).fetchall()
                    for row in candidates:
                        event_id, external_id, contact, phone, payload, attempts, received_at, key = row
                        token = token_factory()
                        lease = conn.execute(
                            _ACQUIRE_CONTACT_SQL,
                            {"key": key, "token": token, "owner": self._owner, "secs": self._lease_seconds},
                        ).fetchone()
                        if lease is None:
                            # Otro worker sostiene el contacto: este evento espera.
                            continue
                        marked = conn.execute(
                            _MARK_PROCESSING_SQL,
                            {"id": event_id, "token": token, "owner": self._owner, "secs": self._lease_seconds},
                        ).fetchone()
                        if marked is None:
                            raise StorageError("claim_state_changed")
                        claims.append(Claim(
                            event_id=event_id, external_event_id=external_id, contact_wa_id=contact,
                            business_phone_id=phone, contact_key=key, lease_token=token,
                            attempts=attempts + 1, payload=payload or {}, received_at=received_at,
                        ))
        except (psycopg.Error, PoolClosed):
            raise StorageError("job_claim_failed") from None
        return claims

    def renew(self, claim: Claim) -> bool:
        """Renueva un lease VIGENTE. Devuelve False si este worker ya no es el propietario o su lease venció."""
        try:
            with self._pool.connection() as conn:
                with conn.transaction():
                    event = conn.execute(
                        "update public.webhook_events set lease_expires_at = now() + make_interval(secs => %s) "
                        "where id = %s and lease_token = %s and processing_status = 'processing' "
                        "and lease_expires_at > now() returning id",
                        (self._lease_seconds, claim.event_id, claim.lease_token),
                    ).fetchone()
                    if event is None:
                        return False
                    conn.execute(
                        "update public.contact_leases set lease_expires_at = now() + make_interval(secs => %s) "
                        "where contact_key = %s and lease_token = %s and lease_expires_at > now()",
                        (self._lease_seconds, claim.contact_key, claim.lease_token),
                    )
                    return True
        except (psycopg.Error, PoolClosed):
            raise StorageError("job_renew_failed") from None

    def finish(self, claim: Claim, status: str, error_code: str | None = None,
               retry_after_seconds: int | None = None, drop_payload: bool = False) -> bool:
        """Finaliza el trabajo solo si este token sigue siendo el propietario. Devuelve False si fue recuperado."""
        if status not in ALLOWED_FINISH_STATES:
            raise ValueError("finish_status_invalid")
        if status == "retry" and retry_after_seconds is None:
            raise ValueError("retry_requires_delay")
        try:
            with self._pool.connection() as conn:
                with conn.transaction():
                    row = conn.execute(
                        """
                        update public.webhook_events
                        set processing_status = %(status)s,
                            lease_token = null, lease_owner = null, lease_expires_at = null,
                            last_error_code = %(error)s,
                            next_attempt_at = case when %(retry)s::int is null then next_attempt_at
                                                   else now() + make_interval(secs => %(retry)s::int) end,
                            processed_at = case when %(status)s in ('processed', 'ignored') then now() else null end,
                            payload = case when %(drop)s or %(status)s in ('processed', 'ignored') then null else payload end
                        where id = %(id)s and lease_token = %(token)s and processing_status = 'processing'
                        returning id
                        """,
                        {"status": status, "error": error_code, "retry": retry_after_seconds,
                         "drop": drop_payload, "id": claim.event_id, "token": claim.lease_token},
                    ).fetchone()
                    if row is None:
                        return False
                    conn.execute(
                        "delete from public.contact_leases where contact_key = %s and lease_token = %s",
                        (claim.contact_key, claim.lease_token),
                    )
                    return True
        except (psycopg.Error, PoolClosed):
            raise StorageError("job_finish_failed") from None

    def recover_expired(self, max_attempts: int) -> int:
        """Devuelve a 'retry' (o 'manual_review' si se agotaron intentos) los trabajos con lease vencido."""
        try:
            with self._pool.connection() as conn:
                with conn.transaction():
                    expired = conn.execute(
                        """
                        with expired as (
                            select id from public.webhook_events
                            where processing_status = 'processing' and lease_expires_at < now()
                            for update skip locked
                        )
                        update public.webhook_events e
                        set processing_status = case when e.attempts >= %s then 'manual_review' else 'retry' end,
                            lease_token = null, lease_owner = null, lease_expires_at = null,
                            last_error_code = 'lease_expired', next_attempt_at = now()
                        from expired
                        where e.id = expired.id
                        returning e.id
                        """,
                        (max_attempts,),
                    ).fetchall()
                    conn.execute("delete from public.contact_leases where lease_expires_at < now()")
                    return len(expired)
        except (psycopg.Error, PoolClosed):
            raise StorageError("job_recover_failed") from None

    def counts(self) -> dict[str, int]:
        try:
            with self._pool.connection() as conn:
                rows = conn.execute(
                    "select processing_status, count(*) from public.webhook_events group by processing_status"
                ).fetchall()
                leases = conn.execute(
                    "select count(*) from public.webhook_events where processing_status = 'processing' "
                    "and lease_expires_at < now()"
                ).fetchone()
        except (psycopg.Error, PoolClosed):
            raise StorageError("job_metrics_failed") from None
        result = {status: int(count) for status, count in rows}
        result["expired_leases"] = int(leases[0])
        return result
