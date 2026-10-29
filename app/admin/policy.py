"""Política de transiciones administrativas. Funciones puras: sin base de datos, sin red.

Principios:
  * Ninguna transición devuelve un envío a 'pending'. Un mensaje incierto nunca se reenvía desde aquí.
  * Cada transición exige evidencia externa explícita (referencia), salvo las puramente internas.
  * Un costo nunca se modifica libremente: solo se libera con evidencia de no facturación, o se liquida
    con un importe real que no supere la reserva.
  * Las transiciones desde estados finales se rechazan.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


class AdminPolicyError(Exception):
    """Transición rechazada. El código es estable y no contiene datos del cliente."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Transition:
    target_type: str
    from_states: frozenset[str]
    to_state: str
    needs_evidence: bool = False
    needs_amount: bool = False


TRANSITIONS: dict[tuple[str, str], Transition] = {
    # Eventos de webhook en revisión manual.
    ("webhook_event", "dead_letter"): Transition("webhook_event", frozenset({"manual_review"}), "dead_letter"),
    ("webhook_event", "retry"): Transition("webhook_event", frozenset({"manual_review"}), "retry"),
    # Envíos inciertos: solo se confirman con evidencia de Meta o se cierran con evidencia de rechazo.
    ("outbound_message", "sent"): Transition("outbound_message", frozenset({"uncertain"}), "sent", needs_evidence=True),
    ("outbound_message", "failed"): Transition("outbound_message", frozenset({"uncertain"}), "failed",
                                               needs_evidence=True),
    # Reservas presupuestarias pendientes de conciliar.
    ("budget_reservation", "released"): Transition("budget_reservation", frozenset({"reserved", "uncertain"}),
                                                  "released", needs_evidence=True),
    ("budget_reservation", "settled"): Transition("budget_reservation", frozenset({"reserved", "uncertain"}),
                                                  "settled", needs_evidence=True, needs_amount=True),
    # Solicitudes de asesor.
    ("human_handoff", "assigned"): Transition("human_handoff", frozenset({"open"}), "assigned"),
    ("human_handoff", "resolved"): Transition("human_handoff", frozenset({"open", "assigned"}), "resolved"),
    ("human_handoff", "cancelled"): Transition("human_handoff", frozenset({"open", "assigned"}), "cancelled"),
}

# Reactivación de la automatización: solo tras comprobar que no quedan solicitudes activas.
RESUME_FROM = frozenset({"paused_for_human", "disabled"})


def check_transition(target_type: str, to_state: str, current: str) -> Transition:
    transition = TRANSITIONS.get((target_type, to_state))
    if transition is None:
        raise AdminPolicyError("transition_not_allowed")
    if current not in transition.from_states:
        raise AdminPolicyError("state_not_eligible")
    return transition


def check_evidence(transition: Transition, evidence: str | None) -> None:
    if transition.needs_evidence and (evidence is None or len(evidence.strip()) < 3):
        raise AdminPolicyError("evidence_required")


def check_amount(transition: Transition, reserved_usd: Decimal, actual_usd: Decimal | None) -> None:
    if not transition.needs_amount:
        return
    if actual_usd is None:
        raise AdminPolicyError("amount_required")
    if actual_usd < 0:
        raise AdminPolicyError("amount_negative")
    if actual_usd > reserved_usd:
        # Un costo real mayor que el peor caso reservado indica un error de cálculo: no se acepta sin revisión.
        raise AdminPolicyError("amount_exceeds_reservation")


def check_reason(reason: str | None) -> str:
    if reason is None or not 3 <= len(reason.strip()) <= 500:
        raise AdminPolicyError("reason_required")
    return reason.strip()


def check_resume(current: str, active_handoffs: int) -> None:
    if current not in RESUME_FROM:
        raise AdminPolicyError("state_not_eligible")
    if active_handoffs > 0:
        # Mientras haya una solicitud activa, la conversación sigue en manos de un asesor.
        raise AdminPolicyError("active_handoff_exists")
