"""Política de transiciones administrativas. Pura: sin base de datos."""

from decimal import Decimal

import pytest

from app.admin.policy import (
    AdminPolicyError,
    check_amount,
    check_evidence,
    check_reason,
    check_resume,
    check_transition,
)


def _code(fn, *args):
    with pytest.raises(AdminPolicyError) as excinfo:
        fn(*args)
    return excinfo.value.code


# Un envío incierto nunca vuelve a pending: no hay ninguna transición que lo permita.
@pytest.mark.parametrize("to_state", ["pending", "sending", "delivered", "read", "cancelled", "blocked"])
def test_uncertain_outbound_cannot_be_moved_to_states_that_could_resend_or_invent_delivery(to_state):
    assert _code(check_transition, "outbound_message", to_state, "uncertain") == "transition_not_allowed"


def test_uncertain_outbound_may_only_be_confirmed_sent_or_closed_failed():
    assert check_transition("outbound_message", "sent", "uncertain").to_state == "sent"
    assert check_transition("outbound_message", "failed", "uncertain").to_state == "failed"


def test_outbound_already_sent_cannot_be_reconciled_again():
    assert _code(check_transition, "outbound_message", "sent", "sent") == "state_not_eligible"


@pytest.mark.parametrize("current", ["pending", "sent", "delivered", "failed", "cancelled"])
def test_outbound_sent_or_failed_requires_uncertain_state(current):
    assert _code(check_transition, "outbound_message", "failed", current) == "state_not_eligible"


def test_sent_and_failed_require_external_evidence():
    transition = check_transition("outbound_message", "sent", "uncertain")
    assert _code(check_evidence, transition, None) == "evidence_required"
    assert _code(check_evidence, transition, "  ") == "evidence_required"
    check_evidence(transition, "wamid.HBgNNTczMDAx")


def test_budget_release_requires_evidence_of_non_billing():
    transition = check_transition("budget_reservation", "released", "uncertain")
    assert _code(check_evidence, transition, None) == "evidence_required"


def test_budget_settle_requires_actual_amount_not_above_reservation():
    transition = check_transition("budget_reservation", "settled", "reserved")
    assert _code(check_amount, transition, Decimal("0.5"), None) == "amount_required"
    assert _code(check_amount, transition, Decimal("0.5"), Decimal("0.6")) == "amount_exceeds_reservation"
    assert _code(check_amount, transition, Decimal("0.5"), Decimal("-0.1")) == "amount_negative"
    check_amount(transition, Decimal("0.5"), Decimal("0.2"))


def test_released_budget_cannot_be_settled_afterwards():
    assert _code(check_transition, "budget_reservation", "settled", "released") == "state_not_eligible"


def test_event_manual_review_can_be_closed_or_requeued_only():
    assert check_transition("webhook_event", "retry", "manual_review").to_state == "retry"
    assert check_transition("webhook_event", "dead_letter", "manual_review").to_state == "dead_letter"
    assert _code(check_transition, "webhook_event", "processed", "manual_review") == "transition_not_allowed"


def test_handoff_closure_is_terminal_and_assignment_needs_open_state():
    assert check_transition("human_handoff", "assigned", "open").to_state == "assigned"
    assert _code(check_transition, "human_handoff", "assigned", "resolved") == "state_not_eligible"


def test_reason_is_mandatory_and_bounded():
    assert _code(check_reason, None) == "reason_required"
    assert _code(check_reason, "x") == "reason_required"
    assert _code(check_reason, "x" * 501) == "reason_required"
    assert check_reason("  revisado con el proveedor  ") == "revisado con el proveedor"


def test_resume_requires_paused_state_and_no_active_handoff():
    check_resume("paused_for_human", 0)
    assert _code(check_resume, "automated", 0) == "state_not_eligible"
    assert _code(check_resume, "paused_for_human", 1) == "active_handoff_exists"


def test_unknown_transition_is_rejected():
    assert _code(check_transition, "conversation", "automated", "paused_for_human") == "transition_not_allowed"
