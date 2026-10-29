"""Esquemas validados de la respuesta del agente. Claude propone; el backend valida antes de aceptar."""

from __future__ import annotations

from enum import Enum
from typing import Literal, get_args
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_REPLY_CHARS = 1200
MAX_RECOMMENDATIONS = 5

Intent = Literal[
    "product_inquiry", "price_inquiry", "availability_inquiry", "purchase_intent", "quote_request",
    "warranty", "return_request", "human_request", "complaint", "greeting", "other",
]
HandoffReason = Literal[
    "purchase_intent", "quote_request", "installation", "warranty", "return_request",
    "complaint", "unsupported_request", "customer_request", "other",
]


class LeadStage(str, Enum):
    """Estado COMERCIAL del prospecto. Es distinto del estado técnico de procesamiento."""

    NEW = "NEW"
    DISCOVERY = "DISCOVERY"
    QUALIFIED = "QUALIFIED"
    HIGH_INTENT = "HIGH_INTENT"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"
    CLOSED = "CLOSED"


HANDOFF_REASON_VALUES: frozenset[str] = frozenset(get_args(HandoffReason))


class AgentReply(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reply_text: str = Field(min_length=1, max_length=MAX_REPLY_CHARS)
    intent: Intent
    lead_stage: LeadStage
    recommended_product_ids: list[UUID] = Field(default_factory=list, max_length=MAX_RECOMMENDATIONS)
    requires_human: bool
    handoff_reason: HandoffReason | None = None

    @model_validator(mode="after")
    def _handoff_consistency(self) -> "AgentReply":
        if self.requires_human and self.handoff_reason is None:
            raise ValueError("handoff_reason es obligatorio cuando requires_human es true")
        if not self.requires_human and self.handoff_reason is not None:
            raise ValueError("handoff_reason debe ser null cuando requires_human es false")
        if len(set(self.recommended_product_ids)) != len(self.recommended_product_ids):
            raise ValueError("recommended_product_ids no puede repetir identificadores")
        return self
