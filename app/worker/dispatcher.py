"""Despacho de respuestas pendientes hacia WhatsApp Cloud API.

Orden de decisiones por cada mensaje reclamado (la primera regla que aplique gana):
  1. Destino no resoluble                -> failed (no se envía)
  2. Conversación bajo control humano    -> cancelled (una respuesta de IA ya no es válida)
  3. Ventana de atención cerrada         -> blocked (no hay mensajes libres fuera de ventana)
  4. Envío a Meta:
       éxito con identificador           -> sent (guarda wamid)
       not_sent reintentable (429, red)  -> pending con backoff; agotados los intentos -> failed
       not_sent definitivo (4xx, datos)  -> failed
       uncertain (timeout, 5xx, sin ID)  -> uncertain: NUNCA se reenvía automáticamente

El dispatcher no actúa si el envío real está deshabilitado: con WHATSAPP_DRY_RUN=true
las respuestas permanecen en 'pending'.
"""

from __future__ import annotations

import logging
import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from app.integrations.whatsapp_client import SendRequest, WhatsAppError
from app.repositories.errors import StorageError
from app.repositories.outbound import OutboundClaim
from app.worker.retry import backoff_seconds

logger = logging.getLogger("lacusine.dispatcher")

SERVICE_WINDOW = timedelta(hours=24)
DISABLED_RETRY_SECONDS = 300


@dataclass
class DispatchStats:
    claimed: int = 0
    sent: int = 0
    pending_retry: int = 0
    failed: int = 0
    uncertain: int = 0
    cancelled: int = 0
    blocked: int = 0
    expired: int = 0
    codes: dict[str, int] = field(default_factory=dict)

    def bump(self, code: str) -> None:
        self.codes[code] = self.codes.get(code, 0) + 1


def window_open(last_inbound_at: datetime | None, now: datetime) -> bool:
    """La ventana de atención se abre con un mensaje del cliente y dura 24 horas."""
    return last_inbound_at is not None and now - last_inbound_at <= SERVICE_WINDOW


class OutboxDispatcher:
    def __init__(
        self,
        *,
        repository,
        sender,
        business_phone_id: str | None,
        owner: str,
        lease_seconds: int,
        max_attempts: int,
        backoff_base_seconds: int,
        backoff_max_seconds: int,
        batch_size: int = 10,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        rng: random.Random | None = None,
        token_factory: Callable[[], uuid.UUID] = uuid.uuid4,
    ) -> None:
        self._repo = repository
        self._sender = sender
        self._phone = business_phone_id
        self._owner = owner
        self._lease = lease_seconds
        self._max_attempts = max_attempts
        self._base = backoff_base_seconds
        self._cap = backoff_max_seconds
        self._batch = batch_size
        self._clock = clock
        self._rng = rng or random.Random()
        self._token = token_factory

    @property
    def enabled(self) -> bool:
        return bool(getattr(self._sender, "enabled", True))

    def dispatch_once(self) -> DispatchStats:
        stats = DispatchStats()
        if not self.enabled:
            return stats
        stats.expired = self._repo.expire_leases()
        claims = self._repo.claim(self._batch, self._owner, self._lease, self._token())
        stats.claimed = len(claims)
        for claim in claims:
            self._handle(claim, stats)
        return stats

    def _handle(self, claim: OutboundClaim, stats: DispatchStats) -> None:
        now = self._clock()
        context = self._repo.dispatch_context(claim.outbound_id)
        if context is None or not context.wa_contact_id:
            self._close(claim, "failed", stats, "missing_destination")
            return
        if context.automation_state != "automated":
            self._close(claim, "cancelled", stats, "human_control")
            return
        if not window_open(context.last_inbound_at, now):
            self._close(claim, "blocked", stats, "outside_service_window")
            return

        request = SendRequest(
            to=context.wa_contact_id,
            text=claim.content_text,
            business_phone_id=self._phone or "",
            idempotency_key=claim.idempotency_key,
        )
        try:
            result = self._sender.send_text(request)
        except WhatsAppError as exc:
            self._on_error(claim, exc, stats)
            return
        except StorageError:
            raise
        finished = self._repo.finish(claim, "sent", external_message_id=result.message_id,
                                     from_phone_id=self._phone)
        if finished:
            stats.sent += 1
            stats.bump("sent")
        else:
            stats.bump("lease_lost")
            logger.warning("dispatch_lease_lost_after_send outbound=%s", claim.outbound_id)

    def _on_error(self, claim: OutboundClaim, exc: WhatsAppError, stats: DispatchStats) -> None:
        if exc.outcome == "uncertain":
            self._close(claim, "uncertain", stats, exc.code)
            return
        if exc.code == "send_disabled":
            self._close(claim, "pending", stats, exc.code, retry_after=DISABLED_RETRY_SECONDS)
            return
        if exc.retryable:
            if claim.attempts >= self._max_attempts:
                self._close(claim, "failed", stats, f"{exc.code}_attempts_exhausted")
                return
            delay = max(1, int(backoff_seconds(claim.attempts, self._base, self._cap, self._rng)))
            self._close(claim, "pending", stats, exc.code, retry_after=delay)
            return
        self._close(claim, "failed", stats, exc.code)

    def _close(self, claim: OutboundClaim, status: str, stats: DispatchStats, code: str,
               retry_after: int | None = None) -> None:
        finished = self._repo.finish(claim, status, error_code=code, retry_after_seconds=retry_after)
        bucket = {"pending": "pending_retry", "failed": "failed", "uncertain": "uncertain",
                  "cancelled": "cancelled", "blocked": "blocked"}[status]
        setattr(stats, bucket, getattr(stats, bucket) + 1)
        stats.bump(code if finished else "lease_lost")
        logger.info("dispatch_closed outbound=%s status=%s code=%s attempt=%d", claim.outbound_id, status, code,
                    claim.attempts)
