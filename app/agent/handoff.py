"""Decisión de escalamiento humano. El backend decide por señales; el modelo solo puede añadir escalamientos."""

from __future__ import annotations

from dataclasses import dataclass

from app.agent.qualification import Signals

# Orden de prioridad: el primer motivo que aplique determina la razón registrada.
_REASON_ORDER: tuple[tuple[str, str, str], ...] = (
    ("complaint", "complaint", "high"),
    ("warranty", "warranty", "high"),
    ("return_request", "return_request", "high"),
    ("purchase_intent", "purchase_intent", "high"),
    ("installation", "installation", "normal"),
    ("quote_request", "quote_request", "normal"),
    ("human_request", "customer_request", "normal"),
)


@dataclass(frozen=True)
class HandoffDecision:
    required: bool
    reason: str | None = None
    priority: str = "normal"


def decide(signals: Signals, model_requires_human: bool, model_reason: str | None) -> HandoffDecision:
    for attribute, reason, priority in _REASON_ORDER:
        if getattr(signals, attribute):
            return HandoffDecision(required=True, reason=reason, priority=priority)
    if model_requires_human:
        return HandoffDecision(required=True, reason=model_reason or "other", priority="normal")
    return HandoffDecision(required=False)
