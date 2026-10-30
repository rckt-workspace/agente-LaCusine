"""Notificaciones a asesores tras una transferencia humana.

Diseño:
- La solicitud de notificación se crea en la MISMA transacción que la transferencia (ver crm.py). Una transferencia
  nunca existe sin su aviso encolado.
- El envío pasa por la interfaz `AdvisorNotifier`. Hoy solo existe el adaptador simulado: no envía nada real.
  Un canal futuro (correo, mensajería interna) se añade implementando esa interfaz, sin tocar el orquestador.
- El aviso contiene solo identificadores, motivo y prioridad. Nunca el nombre, el teléfono ni el texto del cliente.
- Un lease vencido nunca marca un aviso como enviado. Vuelve a `pending` hasta agotar intentos.
  Un canal real debe deduplicar por `notification_id`, que se entrega como clave de idempotencia.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

import psycopg
from psycopg.errors import Error as PsycopgError
from psycopg_pool import ConnectionPool, PoolClosed

from app.repositories.errors import StorageError


class NotificationError(Exception):
    """Fallo del canal. El código es estable y no contiene datos del aviso."""

    def __init__(self, code: str, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class AdvisorNotice:
    notification_id: UUID
    handoff_id: UUID
    conversation_id: UUID
    reason: str
    priority: str


class AdvisorNotifier(Protocol):
    def notify(self, notice: AdvisorNotice) -> None: ...


@dataclass
class SimulatedAdvisorNotifier:
    """Adaptador local. No envía correos, mensajes ni notificaciones reales. Registra los avisos en memoria."""

    script: list[Exception] = field(default_factory=list)
    sent: list[AdvisorNotice] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def notify(self, notice: AdvisorNotice) -> None:
        with self._lock:
            if self.script:
                raise self.script.pop(0)
            self.sent.append(notice)


@dataclass(frozen=True)
class ClaimedNotice:
    notice: AdvisorNotice
    lease_token: UUID
    attempts: int


@dataclass
class NotificationStats:
    claimed: int = 0
    sent: int = 0
    retry: int = 0
    failed: int = 0


class PostgresAdvisorNotificationRepository:
    def __init__(self, pool: ConnectionPool, max_attempts: int) -> None:
        self._pool = pool
        self._max_attempts = max_attempts

    def claim(self, limit: int, lease_seconds: int, token: UUID) -> list[ClaimedNotice]:
        try:
            with self._pool.connection() as conn:
                with conn.transaction():
                    rows = conn.execute(
                        """
                        with candidates as (
                          select id from public.advisor_notifications
                          where status = 'pending'
                          order by created_at
                          for update skip locked
                          limit %s
                        )
                        update public.advisor_notifications n
                        set status = 'sending', lease_token = %s,
                            lease_expires_at = now() + make_interval(secs => %s),
                            attempts = n.attempts + 1, updated_at = now()
                        from candidates c
                        where n.id = c.id
                        returning n.id, n.handoff_id, n.conversation_id, n.reason, n.priority, n.attempts
                        """,
                        (limit, token, lease_seconds),
                    ).fetchall()
        except (PsycopgError, PoolClosed):
            raise StorageError("notification_claim_failed") from None
        return [
            ClaimedNotice(AdvisorNotice(r[0], r[1], r[2], r[3], r[4]), token, r[5]) for r in rows
        ]

    def finish_sent(self, claim: ClaimedNotice) -> bool:
        return self._finish(
            "update public.advisor_notifications set status = 'sent', sent_at = now(), lease_token = null, "
            "lease_expires_at = null, last_error_code = null, updated_at = now() "
            "where id = %s and lease_token = %s and status = 'sending'",
            (claim.notice.notification_id, claim.lease_token),
        )

    def finish_failed(self, claim: ClaimedNotice, code: str, retryable: bool) -> bool:
        status_expr = "case when %s and attempts < %s then 'pending' else 'failed' end"
        return self._finish(
            f"update public.advisor_notifications set status = {status_expr}, lease_token = null, "
            "lease_expires_at = null, last_error_code = %s, updated_at = now() "
            "where id = %s and lease_token = %s and status = 'sending'",
            (retryable, self._max_attempts, code, claim.notice.notification_id, claim.lease_token),
        )

    def expire_leases(self) -> int:
        try:
            with self._pool.connection() as conn:
                rows = conn.execute(
                    """
                    update public.advisor_notifications
                    set status = case when attempts >= %s then 'failed' else 'pending' end,
                        lease_token = null, lease_expires_at = null, last_error_code = 'lease_expired',
                        updated_at = now()
                    where status = 'sending' and lease_expires_at < now()
                    returning id
                    """,
                    (self._max_attempts,),
                ).fetchall()
        except (PsycopgError, PoolClosed):
            raise StorageError("notification_expire_failed") from None
        return len(rows)

    def _finish(self, sql: str, params: tuple) -> bool:
        try:
            with self._pool.connection() as conn:
                return conn.execute(sql, params).rowcount == 1
        except (PsycopgError, PoolClosed):
            raise StorageError("notification_finish_failed") from None


class AdvisorNotificationDispatcher:
    def __init__(self, repository: PostgresAdvisorNotificationRepository, notifier: AdvisorNotifier, *,
                 owner_token_factory, lease_seconds: int = 60, batch_size: int = 10) -> None:
        self._repo = repository
        self._notifier = notifier
        self._token_factory = owner_token_factory
        self._lease_seconds = lease_seconds
        self._batch_size = batch_size

    def dispatch_once(self) -> NotificationStats:
        stats = NotificationStats()
        self._repo.expire_leases()  # avisos con lease vencido vuelven a pending o pasan a failed
        claims = self._repo.claim(self._batch_size, self._lease_seconds, self._token_factory())
        stats.claimed = len(claims)
        for claim in claims:
            try:
                self._notifier.notify(claim.notice)
            except NotificationError as exc:
                self._repo.finish_failed(claim, exc.code, exc.retryable)
                if exc.retryable:
                    stats.retry += 1
                else:
                    stats.failed += 1
                continue
            except Exception:  # noqa: BLE001 - un canal inesperado no detiene el ciclo; el detalle no se registra
                self._repo.finish_failed(claim, "notifier_unexpected", True)
                stats.retry += 1
                continue
            self._repo.finish_sent(claim)
            stats.sent += 1
        return stats
