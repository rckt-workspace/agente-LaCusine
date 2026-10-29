"""Orquestador comercial. El backend controla el flujo; Claude solo propone el texto y la estructura.

Orden de controles (cada paso que puede evitar una llamada facturable va antes de la llamada):
  1. Apagado de emergencia y disponibilidad del proveedor.
  2. Control humano: si un asesor tiene la conversación, el agente no responde.
  3. Sanitización de la entrada y detección de datos sensibles e instrucciones maliciosas (sin llamada).
  4. Idempotencia: un evento ya respondido no vuelve a llamar al modelo.
  5. Solicitud explícita de asesor: escalamiento determinista (sin llamada).
  6. Búsqueda acotada de productos, contexto acotado y presupuesto preventivo.
  7. Llamada al proveedor, registro de costo idempotente.
  8. Validación del esquema, de productos recomendados (solo candidatos reales) y de promesas prohibidas.
  9. Etapa comercial y escalamiento por señales observables.

Nada de esto envía mensajes: el resultado se entrega al llamador, que decide qué hacer con él.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Protocol
from uuid import UUID

from pydantic import ValidationError

from app.agent.conversation_context import ConversationContextService
from app.agent.handoff import decide
from app.agent.knowledge import KnowledgeService
from app.agent.qualification import derive_stage, detect_signals, parse_budget_cop
from app.agent.safety import (
    SAFE_REPLY_HUMAN,
    SAFE_REPLY_INJECTION,
    SAFE_REPLY_SENSITIVE,
    SAFE_REPLY_UNSAFE_OUTPUT,
    inspect_input,
    output_violations,
    sanitize_input,
)
from app.agent.schemas import AgentReply, LeadStage
from app.integrations.llm import ChatMessage, LLMError, LLMProvider, LLMRequest
from app.repositories.errors import StorageError
from app.repositories.products import ProductHit
from app.services.cost_tracker import BudgetExceededError, CostTracker, worst_case_cost
from app.services.product_search import ProductSearchService

logger = logging.getLogger("lacusine.agent")
DESCRIPTION_CHARS = 140
MAX_CANDIDATES_IN_PROMPT = 5


class OutcomeStatus(str, Enum):
    REPLIED = "replied"
    HANDOFF_ONLY = "handoff_only"
    SUPPRESSED_HUMAN_CONTROL = "suppressed_human_control"
    REFUSED_SENSITIVE = "refused_sensitive"
    REFUSED_INJECTION = "refused_injection"
    DUPLICATE_SKIPPED = "duplicate_skipped"
    DISABLED = "disabled"
    BUDGET_BLOCKED = "budget_blocked"
    PROVIDER_ERROR = "provider_error"
    INVALID_OUTPUT = "invalid_output"
    UNSAFE_OUTPUT = "unsafe_output"
    NO_ACTION = "no_action"


@dataclass(frozen=True)
class AgentOutcome:
    status: OutcomeStatus
    reply_text: str | None = None
    intent: str | None = None
    lead_stage: LeadStage | None = None
    product_ids: tuple[UUID, ...] = ()
    requires_human: bool = False
    handoff_reason: str | None = None
    error_code: str | None = None
    dry_run: bool = False
    cost_usd: Decimal = Decimal("0")
    details: dict = field(default_factory=dict)


@dataclass(frozen=True)
class AgentSettings:
    kill_switch: bool = False
    max_output_tokens: int = 350
    max_input_chars: int = 1000
    dry_run: bool = True


class ConversationStateReader(Protocol):
    def automation_state(self, conversation_id: UUID) -> str | None: ...


class HandoffWriter(Protocol):
    def open(self, conversation_id: UUID, reason: str, priority: str = "normal") -> UUID: ...


class ProductLookup(Protocol):
    def get_by_ids(self, ids: list[UUID]) -> dict[UUID, ProductHit]: ...


class CommercialAgent:
    def __init__(
        self,
        *,
        provider: LLMProvider | None,
        cost: CostTracker,
        search: ProductSearchService,
        context: ConversationContextService,
        conversations: ConversationStateReader,
        handoffs: HandoffWriter,
        products: ProductLookup,
        knowledge: KnowledgeService,
        settings: AgentSettings,
    ) -> None:
        self._provider = provider
        self._cost = cost
        self._search = search
        self._context = context
        self._conversations = conversations
        self._handoffs = handoffs
        self._products = products
        self._knowledge = knowledge
        self._settings = settings
        self._system_prompt = knowledge.system_prompt()

    def handle(self, conversation_id: UUID, inbound_text: str, call_id: str) -> AgentOutcome:
        if self._settings.kill_switch or self._provider is None:
            return AgentOutcome(OutcomeStatus.DISABLED, error_code="agent_disabled")

        state = self._conversations.automation_state(conversation_id)
        if state is None:
            return AgentOutcome(OutcomeStatus.DISABLED, error_code="conversation_not_found")
        if state != "automated":
            return AgentOutcome(OutcomeStatus.SUPPRESSED_HUMAN_CONTROL, error_code=state)

        text = sanitize_input(inbound_text, self._settings.max_input_chars)
        if not text:
            return AgentOutcome(OutcomeStatus.NO_ACTION, error_code="empty_message")

        findings = inspect_input(text)
        if findings.sensitive:
            # Los datos sensibles nunca salen hacia el modelo. Solo se registran sus categorías.
            return AgentOutcome(
                OutcomeStatus.REFUSED_SENSITIVE, reply_text=SAFE_REPLY_SENSITIVE, intent="other",
                lead_stage=LeadStage.NEW, details={"sensitive_kinds": findings.sensitive_kinds},
            )
        if findings.injection:
            return AgentOutcome(OutcomeStatus.REFUSED_INJECTION, reply_text=SAFE_REPLY_INJECTION, intent="other",
                                lead_stage=LeadStage.NEW)

        if self._cost.has_success(call_id):
            return AgentOutcome(OutcomeStatus.DUPLICATE_SKIPPED, error_code="already_answered")

        dry_run = self._provider.name == "fake" and self._settings.dry_run

        if findings.human_request:
            self._handoffs.open(conversation_id, "customer_request", "normal")
            return AgentOutcome(
                OutcomeStatus.HANDOFF_ONLY, reply_text=SAFE_REPLY_HUMAN, intent="human_request",
                lead_stage=LeadStage.HUMAN_REQUIRED, requires_human=True, handoff_reason="customer_request",
                dry_run=dry_run,
            )

        budget_cop = parse_budget_cop(text)
        search = self._search.search(text, max_price_cop=budget_cop)
        signals = detect_signals(
            text,
            product_interest=bool(search.tokens),
            confident_product=search.confident,
            human_request=False,
        )
        window = self._context.build(conversation_id)
        request = self._build_request(call_id, text, window, search)

        billable = self._provider.name != "fake"
        if billable:
            try:
                pricing = self._cost.pricing
                if pricing is None:
                    raise BudgetExceededError("pricing_not_configured")
                estimate_tokens = (len(request.system) + sum(len(m.content) for m in request.messages)) // 3 + 50
                self._cost.precheck(worst_case_cost(pricing, estimate_tokens, request.max_output_tokens))
            except BudgetExceededError as exc:
                logger.warning("agent_budget_blocked call_id=%s code=%s", call_id, exc.code)
                return AgentOutcome(OutcomeStatus.BUDGET_BLOCKED, error_code=exc.code)

        try:
            response = self._provider.complete(request)
        except LLMError as exc:
            logger.warning("agent_provider_error call_id=%s code=%s", call_id, exc.code)
            self._record_failure_safely(call_id, conversation_id, exc.code)
            return AgentOutcome(OutcomeStatus.PROVIDER_ERROR, error_code=exc.code, dry_run=dry_run)

        cost = self._cost.record_success(call_id, conversation_id, response)

        if response.truncated:
            self._handoffs.open(conversation_id, "other", "normal")
            return AgentOutcome(OutcomeStatus.INVALID_OUTPUT, error_code="truncated", cost_usd=cost, dry_run=dry_run)

        try:
            reply = parse_reply(response.text)
        except (ValidationError, ValueError):
            logger.warning("agent_output_rejected call_id=%s reason=schema", call_id)
            self._handoffs.open(conversation_id, "other", "normal")
            return AgentOutcome(OutcomeStatus.INVALID_OUTPUT, error_code="schema_invalid", cost_usd=cost, dry_run=dry_run)

        violations = output_violations(reply.reply_text)
        if violations:
            logger.warning("agent_output_rejected call_id=%s reason=policy codes=%s", call_id, ",".join(violations))
            self._handoffs.open(conversation_id, "other", "normal")
            return AgentOutcome(
                OutcomeStatus.UNSAFE_OUTPUT, reply_text=SAFE_REPLY_UNSAFE_OUTPUT, requires_human=True,
                handoff_reason="other", error_code="policy_violation", cost_usd=cost, dry_run=dry_run,
                details={"violations": violations},
            )

        candidate_ids = {hit.id for hit in search.hits}
        allowed = [pid for pid in reply.recommended_product_ids if pid in candidate_ids]
        existing = self._products.get_by_ids(allowed) if allowed else {}
        product_ids = tuple(pid for pid in allowed if pid in existing)

        handoff = decide(signals, reply.requires_human, reply.handoff_reason)
        stage = derive_stage(signals)
        if handoff.required:
            if stage is not LeadStage.HIGH_INTENT:
                stage = LeadStage.HUMAN_REQUIRED
            self._handoffs.open(conversation_id, handoff.reason, handoff.priority)

        return AgentOutcome(
            OutcomeStatus.REPLIED,
            reply_text=reply.reply_text,
            intent=reply.intent,
            lead_stage=stage,
            product_ids=product_ids,
            requires_human=handoff.required,
            handoff_reason=handoff.reason,
            cost_usd=cost,
            dry_run=dry_run,
            details={"dropped_product_ids": len(reply.recommended_product_ids) - len(product_ids)},
        )

    def _build_request(self, call_id: str, text: str, window, search) -> LLMRequest:
        history = "\n".join(
            f"[{'cliente' if turn.role == 'user' else 'agente'}] {turn.text}" for turn in window.turns
        ) or "(sin historial)"
        candidates = "\n".join(_format_candidate(hit) for hit in search.hits[:MAX_CANDIDATES_IN_PROMPT]) or "(sin candidatos)"
        user_content = (
            f"<mensaje_cliente>\n{text}\n</mensaje_cliente>\n"
            f"<historial truncado=\"{str(window.truncated).lower()}\">\n{history}\n</historial>\n"
            f"<catalogo_candidatos busqueda_confiable=\"{str(search.confident).lower()}\">\n{candidates}\n"
            "</catalogo_candidatos>"
        )
        return LLMRequest(
            call_id=call_id,
            system=self._system_prompt,
            messages=(ChatMessage(role="user", content=user_content),),
            max_output_tokens=self._settings.max_output_tokens,
        )

    def _record_failure_safely(self, call_id: str, conversation_id: UUID, code: str) -> None:
        try:
            self._cost.record_failure(call_id, conversation_id, self._provider.name, self._provider.model, code)
        except StorageError:
            logger.error("agent_failure_audit_unavailable call_id=%s", call_id)


def parse_reply(raw_text: str) -> AgentReply:
    """Extrae el objeto JSON de la respuesta y lo valida contra el esquema. Cualquier desviación falla."""
    start = raw_text.find("{")
    end = raw_text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no_json_object")
    payload = json.loads(raw_text[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("not_object")
    return AgentReply.model_validate(payload)


def format_cop(value: Decimal | None) -> str:
    if value is None:
        return "sin precio"
    return f"{int(value):,}".replace(",", ".") + " COP"


def _format_candidate(hit: ProductHit) -> str:
    availability = "sí" if hit.is_available is True else "no" if hit.is_available is False else "desconocido"
    previous = f" | precio_antes: {format_cop(hit.previous_price_cop)}" if hit.previous_price_cop else ""
    note = f" | nota_entrega: {hit.delivery_note}" if hit.delivery_note else ""
    description = (hit.description or "")[:DESCRIPTION_CHARS]
    return (
        f"- id: {hit.id} | nombre: {hit.name or hit.reference} | marca: {hit.brand} | "
        f"referencia: {hit.reference} | precio_ref: {format_cop(hit.price_cop)}{previous} | "
        f"disponible_segun_fuente: {availability}{note} | enlace: {hit.product_url or 'sin enlace'} | "
        f"descripcion: {description}"
    )
