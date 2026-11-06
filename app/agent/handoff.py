"""Decisión de escalamiento humano. El backend decide: las señales observables mandan y el modelo solo puede
añadir escalamientos por motivos concretos y justificables. Un `requires_human=true` sin motivo aceptado se ignora.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.agent.qualification import Signals

# Orden de prioridad: el primer motivo que aplique determina la razón registrada.
_REASON_ORDER: tuple[tuple[str, str, str], ...] = (
    ("complaint", "complaint", "high"),
    ("warranty", "warranty", "high"),
    ("return_request", "return_request", "high"),
    ("payment_request", "payment_request", "high"),
    ("installation", "installation", "normal"),
    ("quote_request", "quote_request", "normal"),
    ("human_request", "customer_request", "normal"),
)

# Motivos que el modelo puede usar por sí solo. "customer_request" exige la señal del backend (petición explícita).
# "unsupported_request", "other", "purchase_intent" y motivos desconocidos NO transfieren: el saludo o una
# pregunta de catálogo nunca debe pausar la conversación porque el modelo lo marque.
MODEL_ACCEPTED_REASONS = frozenset({"complaint", "warranty", "return_request", "installation", "quote_request"})


@dataclass(frozen=True)
class HandoffDecision:
    required: bool
    reason: str | None = None
    priority: str = "normal"
    source: str | None = None  # "signal" | "model" | None
    ignored_model_reason: str | None = None  # motivo que el modelo pidió y el backend rechazó


def decide(signals: Signals, model_requires_human: bool, model_reason: str | None) -> HandoffDecision:
    for attribute, reason, priority in _REASON_ORDER:
        if getattr(signals, attribute):
            return HandoffDecision(required=True, reason=reason, priority=priority, source="signal")
    if model_requires_human:
        if model_reason in MODEL_ACCEPTED_REASONS:
            return HandoffDecision(required=True, reason=model_reason, priority="normal", source="model")
        return HandoffDecision(required=False, ignored_model_reason=model_reason or "none")
    return HandoffDecision(required=False)
