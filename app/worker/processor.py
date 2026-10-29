"""Procesa un evento reclamado. Reutiliza CommercialAgent.handle(); no contiene un segundo orquestador.

Devuelve un ProcessResult. El runner es quien finaliza el trabajo con el token de propiedad.
Ningún paso llama a WhatsApp: las respuestas quedan en el outbox como 'pending'.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from app.agent.orchestrator import AgentOutcome, OutcomeStatus
from app.agent.safety import SAFE_REPLY_HUMAN
from app.agent.schemas import LeadStage
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


@dataclass(frozen=True)
class ProcessResult:
    status: str                      # "processed" | "ignored" | "manual_review" | "lease_lost"
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
        outbound=None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._outbound = outbound
        self._agent = agent
        self._leads = leads
        self._conversations = conversations
        self._messages = messages
        self._handoffs = handoffs
        self._outbox = outbox
        self._retention = timedelta(days=retention_days)
        self._clock = clock

    def process(self, claim: Claim, renew: Callable[[], bool]) -> ProcessResult:
        if claim.event_type == "status":
            return self._process_status(claim)
        message_type = str(claim.payload.get("type", "unknown"))
        contact = claim.contact_wa_id
        if not contact:
            raise PermanentJobError("missing_sender")

        now = self._clock()
        lead_id = self._leads.get_or_create(contact)
        conversation_id = self._conversations.get_or_open(lead_id)

        if message_type != "text":
            self._messages.record_inbound(
                conversation_id, claim.external_event_id,
                message_type if message_type in SUPPORTED_MESSAGE_TYPES else "unknown", now,
            )
            return self._persist(claim, conversation_id, lead_id, None, UNSUPPORTED_TYPE_REPLY, now,
                                 error_code="unsupported_type")

        body = _text_body(claim.payload)
        self._messages.record_inbound(
            conversation_id, claim.external_event_id, "text", now,
            content_text=body, content_expires_at=now + self._retention,
        )

        if not renew():
            return ProcessResult(status="lease_lost")

        outcome: AgentOutcome = self._agent.handle(conversation_id, body, claim.external_event_id)
        return self._apply_outcome(claim, conversation_id, lead_id, outcome, now)

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
                return self._hand_off(claim, conversation_id, lead_id, "agent_unavailable", now)
            if code == "auth_failed":
                raise PermanentJobError("provider_auth_failed")
            # Resultado incierto (timeout, 5xx, conexión): no se reenvía automáticamente.
            raise ManualReviewRequired(code)

        if status is OutcomeStatus.SUPPRESSED_HUMAN_CONTROL:
            return self._persist(claim, conversation_id, lead_id, None, None, now, ignored_reason="human_control")

        if status is OutcomeStatus.DISABLED:
            return self._hand_off(claim, conversation_id, lead_id, "agent_unavailable", now)

        if status is OutcomeStatus.BUDGET_BLOCKED:
            return self._hand_off(claim, conversation_id, lead_id, "budget_blocked", now)

        if status is OutcomeStatus.DUPLICATE_SKIPPED:
            return self._persist(claim, conversation_id, lead_id, None, None, now, ignored_reason="already_answered")

        lead_stage = outcome.lead_stage
        lead_status = LEAD_STATUS_BY_STAGE.get(lead_stage) if lead_stage is not None else None
        return self._persist(claim, conversation_id, lead_id, lead_status, outcome.reply_text, now)

    def _hand_off(self, claim, conversation_id, lead_id, code: str, now) -> ProcessResult:
        self._handoffs.open(conversation_id, "other", "normal")
        return self._persist(claim, conversation_id, lead_id, LEAD_STATUS_BY_STAGE[LeadStage.HUMAN_REQUIRED],
                             SAFE_REPLY_HUMAN, now, ignored_reason=None, error_code=code)

    def _persist(self, claim, conversation_id, lead_id, lead_status, reply_text, now,
                 ignored_reason: str | None = None, error_code: str | None = None) -> ProcessResult:
        # Respuesta y estado comercial en una sola transacción. La clave de idempotencia evita duplicados.
        self._outbox.persist_result(
            event_id=claim.event_id,
            idempotency_key=f"{claim.external_event_id}:reply",
            conversation_id=conversation_id,
            lead_id=lead_id,
            lead_status=lead_status,
            reply_text=reply_text,
        )
        if ignored_reason is not None:
            return ProcessResult(status="ignored", error_code=ignored_reason)
        return ProcessResult(status="processed", error_code=error_code)


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
