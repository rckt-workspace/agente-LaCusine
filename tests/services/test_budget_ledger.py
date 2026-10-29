"""Máquina de estados de reservas presupuestarias (implementación en memoria; la PostgreSQL se prueba en integración)."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.services.budget_ledger import (
    BudgetExceededError,
    BudgetLimits,
    InMemoryBudgetLedger,
    LedgerStateError,
    ReservationConflictError,
)

NOW = datetime(2026, 10, 29, 15, 0, tzinfo=timezone.utc)
DAY = NOW - timedelta(hours=2)
MONTH = NOW - timedelta(days=3)


def _reserve(ledger, call_id, amount="1", limits=None):
    ledger.reserve(call_id, None, Decimal(amount), limits or BudgetLimits(), DAY, MONTH, NOW)


def test_reserve_counts_worst_case_until_settled():
    ledger = InMemoryBudgetLedger()
    _reserve(ledger, "c1", "0.5")
    assert ledger.state("c1") == "reserved"
    assert ledger.counted_between(DAY, NOW + timedelta(seconds=1)) == Decimal("0.5")
    ledger.settle("c1", Decimal("0.1"))
    assert ledger.state("c1") == "settled"
    assert ledger.counted_between(DAY, NOW + timedelta(seconds=1)) == Decimal("0.1")


def test_release_stops_counting_and_allows_a_new_reservation_for_the_same_call():
    ledger = InMemoryBudgetLedger()
    _reserve(ledger, "c2", "0.5")
    ledger.release("c2")
    assert ledger.state("c2") == "released"
    assert ledger.counted_between(DAY, NOW + timedelta(seconds=1)) == Decimal("0")
    _reserve(ledger, "c2", "0.3")  # reintento tras liberación: permitido
    assert ledger.state("c2") == "reserved"


def test_uncertain_keeps_counting_and_cannot_be_released_or_reserved_again():
    ledger = InMemoryBudgetLedger()
    _reserve(ledger, "c3", "0.5")
    ledger.mark_uncertain("c3")
    assert ledger.state("c3") == "uncertain"
    assert ledger.counted_between(DAY, NOW + timedelta(seconds=1)) == Decimal("0.5")
    with pytest.raises(LedgerStateError):
        ledger.release("c3")
    with pytest.raises(ReservationConflictError):
        _reserve(ledger, "c3")


def test_active_or_settled_reservation_blocks_second_reservation_for_same_call():
    ledger = InMemoryBudgetLedger()
    _reserve(ledger, "c4")
    with pytest.raises(ReservationConflictError):
        _reserve(ledger, "c4")
    ledger.settle("c4", Decimal("0.2"))
    with pytest.raises(ReservationConflictError):
        _reserve(ledger, "c4")


@pytest.mark.parametrize("operation", ["settle", "release", "mark_uncertain"])
def test_transitions_from_non_reserved_states_are_rejected(operation):
    ledger = InMemoryBudgetLedger()
    _reserve(ledger, "c5")
    ledger.settle("c5", Decimal("0.1"))
    call = {"settle": lambda: ledger.settle("c5", Decimal("0.1")),
            "release": lambda: ledger.release("c5"),
            "mark_uncertain": lambda: ledger.mark_uncertain("c5")}[operation]
    with pytest.raises(LedgerStateError):
        call()


def test_transition_for_unknown_call_is_rejected():
    ledger = InMemoryBudgetLedger()
    with pytest.raises(LedgerStateError):
        ledger.settle("nunca-reservado", Decimal("0.1"))


def test_daily_limit_blocks_reservation_that_would_exceed_it():
    ledger = InMemoryBudgetLedger()
    _reserve(ledger, "a", "0.6")
    with pytest.raises(BudgetExceededError) as excinfo:
        _reserve(ledger, "b", "0.5", BudgetLimits(daily_usd=Decimal("1")))
    assert excinfo.value.code == "daily_budget"
    assert ledger.state("b") is None  # nada se reservó


def test_monthly_limit_and_interaction_limit_are_enforced_in_order():
    ledger = InMemoryBudgetLedger()
    with pytest.raises(BudgetExceededError) as excinfo:
        _reserve(ledger, "x", "2", BudgetLimits(interaction_usd=Decimal("1"), daily_usd=Decimal("1")))
    assert excinfo.value.code == "interaction_limit"
    _reserve(ledger, "y", "0.9")
    with pytest.raises(BudgetExceededError) as excinfo:
        _reserve(ledger, "z", "0.2", BudgetLimits(monthly_usd=Decimal("1")))
    assert excinfo.value.code == "monthly_budget"


def test_limit_allows_exact_fit():
    ledger = InMemoryBudgetLedger()
    _reserve(ledger, "p", "0.4", BudgetLimits(daily_usd=Decimal("1")))
    _reserve(ledger, "q", "0.6", BudgetLimits(daily_usd=Decimal("1")))
    assert ledger.counted_between(DAY, NOW + timedelta(seconds=1)) == Decimal("1.0")


def test_reservations_outside_the_window_do_not_count():
    ledger = InMemoryBudgetLedger()
    ledger.add_counted("old", Decimal("50"), DAY - timedelta(days=1))
    _reserve(ledger, "new", "0.5", BudgetLimits(daily_usd=Decimal("1")))
