"""Procesa un evento reclamado. Reutiliza CommercialAgent.handle(); no contiene un segundo orquestador.

Devuelve un ProcessResult. El runner es quien finaliza el trabajo con el token de propiedad.
Ningún paso llama a WhatsApp: las respuestas quedan en el outbox como 'pending'.

Reglas de esta fase:
  * Línea de negocio única (MVP): solo se procesan eventos de META_PHONE_NUMBER_ID. Otros eventos son
    permanentes (dead_letter). Así dos líneas nunca comparten lead ni conversación por error.
  * Ventana de atención: se mide con la hora del evento de Meta (campo "timestamp"), en UTC. Un
    timestamp ausente, inválido o futuro produce un mensaje que NO abre ventana. La hora de
    procesamiento nunca reinicia la ventana.
  * Transferencia: cuando hay escalamiento, el cliente recibe una única confirmación de texto fijo,
    no el texto del modelo. Las respuestas de IA pendientes se cancelan.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from app.agent.orchestrator import AgentOutcome, OutcomeStatus
from app.services.commercial import OutcomeSignals, events_from_outcome
from app.agent.safety import SAFE_REPLY_HUMAN, SAFE_REPLY_RETRY
from app.agent.schemas import LeadStage
from app.models.referral import sanitize_referral

logger = logging.getLogger("lacusine.worker")
from app.repositories.jobs import Claim
from app.repositories.outbound import META_STATUSES
from app.worker.errors import ManualReviewRequired, PermanentJobError

UNSUPPORTED_TYPE_REPLY = (
    "Por ahora solo puedo leer mensajes de texto. ¿Qué electrodoméstico estás buscando?"
)
SUPPORTED_MESSAGE_TYPES = frozenset({
    "text", "image", "audio", "video", "document", "sticker", "location", "contacts", "interactive", "button",
})
LEAD_STATUS_BY_STAGE = {
    LeadStage.NEW: "new",
    LeadStage.DISCOVERY: "qualifying",
    LeadStage.QUALIFIED: "qualified",
    LeadStage.HIGH_INTENT: "qualified",
    LeadStage.HUMAN_REQUIRED: "handed_off",
}
# Tolerancia de reloj conservadora. Un mensaje "del futuro" más allá de esto no es confiable.
MAX_CLOCK_SKEW = timedelta(minutes=5)
MIN_TRUSTED_EVENT_TIME = datetime(2020, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class ProcessResult:
    status: str                      # "processed" | "ignored" | "retry" | "manual_review" | "lease_lost"
    error_code: str | None = None
    retry: bool = False              # True: transitorio, el runner reintentará con backoff


class EventProcessor:
    def __init__(
        self,
        *,
        agent,
        leads,
        conversations,
        messages,
        handoffs,
        outbox,
        retention_days: int,
        expected_business_phone_id: str,
        outbound=None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        typing=None,
        contact_limits: tuple[int, int] | None = None,
        commercial=None,
    ) -> None:
        if not expected_business_phone_id:
            raise ValueError("expected_business_phone_id_required")
        self._expected_line = expected_business_phone_id
        self._outbound = outbound
        self._typing = typing  # None en dry-run o sin proveedor real: nunca se muestra "escribiendo..."
        self._contact_limits = contact_limits  # (por hora, por día); None = sin límite
        self._commercial = commercial  # registro de eventos comerciales; None = desactivado
        self._agent = agent
        self._leads = leads
        self._conversations = conversations
        self._messages = messages
        self._handoffs = handoffs
        self._outbox = outbox
        self._retention = timedelta(days=retention_days)
        self._clock = clock

    def process(self, claim: Claim, renew: Callable[[], bool]) -> ProcessResult:
        self._check_business_line(claim)
        if claim.event_type == "status":
            return self._process_status(claim)
        message_type = str(claim.payload.get("type", "unknown"))
        contact = claim.contact_wa_id
        if not contact:
            raise PermanentJobError("missing_sender")

        now = self._clock()
        event_time, trusted = meta_event_time(claim.payload, now)
        lead_id = self._leads.get_or_create(contact)
        conversation_id = self._conversations.get_or_open(lead_id)
        referral = sanitize_referral(claim.payload.get("referral"))
        if referral is not None:
            self._leads.capture_referral(lead_id, referral)

        if message_type != "text":
            self._messages.record_inbound(
                conversation_id, claim.external_event_id,
                message_type if message_type in SUPPORTED_MESSAGE_TYPES else "unknown",
                event_time, window_trusted=trusted,
            )
            return self._persist(claim, conversation_id, lead_id, None, UNSUPPORTED_TYPE_REPLY, now,
                                 error_code="unsupported_type")

        body = _text_body(claim.payload)
        self._messages.record_inbound(
            conversation_id, claim.external_event_id, "text", event_time,
            content_text=body, content_expires_at=now + self._retention, window_trusted=trusted,
        )

        if not renew():
            return ProcessResult(status="lease_lost")

        if self._contact_limits is not None and self._over_contact_limit(conversation_id, now):
            # Abuso o consumo excesivo: no se responde ni se llama a la IA. Queda registrado el motivo.
            logger.warning("contact_rate_limited event=%s", claim.event_id)
            return self._persist(claim, conversation_id, lead_id, None, None, now, ignored_reason="contact_rate_limited")

        if self._typing is not None:
            # Se muestra solo al iniciar una respuesta real. Un fallo no cambia el resultado.
            self._typing.start(conversation_id, contact, claim.external_event_id)

        outcome: AgentOutcome = self._agent.handle(conversation_id, body, claim.external_event_id)
        self._record_commercial(claim, conversation_id, lead_id, outcome, now)
        return self._apply_outcome(claim, conversation_id, lead_id, outcome, now)

    def _check_business_line(self, claim: Claim) -> None:
        if claim.business_phone_id != self._expected_line:
            raise PermanentJobError("foreign_business_line")

    def _process_status(self, claim: Claim) -> ProcessResult:
        """Estado de entrega de Meta. Si el envío aún no tiene identificador, se reintenta: nunca se pierde."""
        message_id = claim.payload.get("id")
        status = claim.payload.get("status")
        if not isinstance(message_id, str) or not isinstance(status, str):
            raise PermanentJobError("invalid_status_payload")
        if self._outbound is None:
            raise PermanentJobError("status_handler_missing")
        if status not in META_STATUSES:
            return ProcessResult(status="ignored", error_code="unknown_status")
        result = self._outbound.apply_meta_status(message_id, status, _status_error_code(claim.payload))
        if result == "unmatched":
            return ProcessResult(status="retry", error_code="status_unmatched", retry=True)
        if result == "applied":
            # Estado que Meta confirma para una respuesta ya enviada (sent, delivered, read, failed).
            logger.info("reply_status_from_meta status=%s", status)
            return ProcessResult(status="processed", error_code=status)
        return ProcessResult(status="ignored", error_code="no_transition")

    def _apply_outcome(self, claim, conversation_id, lead_id, outcome: AgentOutcome, now) -> ProcessResult:
        status = outcome.status

        if status is OutcomeStatus.REQUIRES_RECONCILIATION:
            raise ManualReviewRequired(outcome.error_code or "reconciliation_required")

        if status is OutcomeStatus.PROVIDER_ERROR:
            code = outcome.error_code or "provider_error"
            if outcome.billed_state == "not_billed" and code in ("rate_limited", "busy"):
                return ProcessResult(status="retry", error_code=code, retry=True)
            if code == "disabled":
                return self._recoverable(claim, conversation_id, lead_id, "agent_unavailable", now)
            if code == "auth_failed":
                raise PermanentJobError("provider_auth_failed")
            # Resultado incierto (timeout, 5xx, conexión): no se reenvía automáticamente.
            raise ManualReviewRequired(code)

        if status is OutcomeStatus.SUPPRESSED_HUMAN_CONTROL:
            return self._persist(claim, conversation_id, lead_id, None, None, now, ignored_reason="human_control")

        # Errores recuperables: mensaje neutro, sin transferencia permanente ni pausa de la conversación.
        if status is OutcomeStatus.DISABLED:
            return self._recoverable(claim, conversation_id, lead_id, "agent_unavailable", now)

        if status is OutcomeStatus.BUDGET_BLOCKED:
            return self._recoverable(claim, conversation_id, lead_id, "budget_blocked", now)

        if status is OutcomeStatus.INVALID_OUTPUT:
            return self._recoverable(claim, conversation_id, lead_id, outcome.error_code or "invalid_output", now)

        if status is OutcomeStatus.DUPLICATE_SKIPPED:
            return self._persist(claim, conversation_id, lead_id, None, None, now, ignored_reason="already_answered")

        lead_stage = outcome.lead_stage
        lead_status = LEAD_STATUS_BY_STAGE.get(lead_stage) if lead_stage is not None else None
        if outcome.requires_human:
            # El agente ya pausó la conversación. Se envía una única confirmación fija, no el texto del modelo.
            return self._persist(claim, conversation_id, lead_id, lead_status, SAFE_REPLY_HUMAN, now,
                                 kind="handoff_notice", error_code="handoff")
        return self._persist(claim, conversation_id, lead_id, lead_status, outcome.reply_text, now)

    def _record_commercial(self, claim, conversation_id, lead_id, outcome, now: datetime) -> None:
        """Eventos comerciales derivados de señales del backend. Nunca bloquea ni altera la respuesta al cliente."""
        if self._commercial is None or outcome.status not in (OutcomeStatus.REPLIED, OutcomeStatus.HANDOFF_ONLY):
            return
        try:
            stage = outcome.lead_stage.value if outcome.lead_stage is not None else None
            signals = OutcomeSignals(lead_stage=stage, handoff_reason=outcome.handoff_reason,
                                     product_ids=tuple(outcome.product_ids))
            for event in events_from_outcome(signals, claim.external_event_id):
                self._commercial.record_event(
                    event_type=event.event_type, occurred_at=now, lead_id=lead_id, dedupe_key=event.dedupe_key,
                    conversation_id=conversation_id, category=event.category, product_id=event.product_id,
                    budget_cop=event.budget_cop, source_event_id=claim.external_event_id,
                )
        except Exception as exc:  # noqa: BLE001 - la analítica nunca debe impedir responder
            logger.warning("commercial_event_skipped code=%s", getattr(exc, "code", type(exc).__name__))

    def _over_contact_limit(self, conversation_id, now: datetime) -> bool:
        per_hour, per_day = self._contact_limits
        # Cuenta también el mensaje actual, que ya fue persistido antes de este punto.
        return (self._messages.count_inbound_since(conversation_id, now - timedelta(hours=1)) > per_hour
                or self._messages.count_inbound_since(conversation_id, now - timedelta(days=1)) > per_day)

    def _recoverable(self, claim, conversation_id, lead_id, code: str, now) -> ProcessResult:
        """Error que no es una petición de asesor: se responde con un mensaje neutro y la conversación sigue activa."""
        logger.info("recoverable_reply event=%s code=%s", claim.event_id, code)
        return self._persist(claim, conversation_id, lead_id, None, SAFE_REPLY_RETRY, now, error_code=code)

    def _persist(self, claim, conversation_id, lead_id, lead_status, reply_text, now,
                 ignored_reason: str | None = None, error_code: str | None = None,
                 kind: str = "reply") -> ProcessResult:
        # Respuesta y estado comercial en una sola transacción. La clave de idempotencia evita duplicados.
        self._outbox.persist_result(
            event_id=claim.event_id,
            idempotency_key=f"{claim.external_event_id}:{kind}",
            conversation_id=conversation_id,
            lead_id=lead_id,
            lead_status=lead_status,
            reply_text=reply_text,
            kind=kind,
            business_phone_id=claim.business_phone_id,
        )
        if reply_text is not None:
            logger.info("reply_queued event=%s kind=%s", claim.event_id, kind)
        if ignored_reason is not None:
            return ProcessResult(status="ignored", error_code=ignored_reason)
        return ProcessResult(status="processed", error_code=error_code)


def meta_event_time(payload: dict, now: datetime) -> tuple[datetime, bool]:
    """Hora del evento según Meta (segundos Unix en texto). Devuelve (hora, confiable).

    No confiable si falta, no es numérica, es anterior a 2020 o está más de MAX_CLOCK_SKEW en el futuro.
    En ese caso la hora de referencia es la de procesamiento, pero el mensaje no abre ventana.
    """
    raw = payload.get("timestamp")
    if isinstance(raw, str) and raw.isdigit() and len(raw) <= 12:
        value = datetime.fromtimestamp(int(raw), tz=timezone.utc)
        if MIN_TRUSTED_EVENT_TIME <= value <= now + MAX_CLOCK_SKEW:
            return value, True
    return now, False


def _status_error_code(payload: dict) -> str | None:
    errors = payload.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict) and errors[0].get("code") is not None:
        return str(errors[0]["code"])[:64]
    return None


def _text_body(payload: dict) -> str:
    text = payload.get("text")
    body = text.get("body") if isinstance(text, dict) else None
    if not isinstance(body, str) or not body.strip():
        raise PermanentJobError("invalid_text_payload")
    return body.strip()
