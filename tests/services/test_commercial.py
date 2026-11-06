"""Servicio comercial: permisos, validación de ventas y eventos derivados. Sin base de datos."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.services.commercial import (
    ForbiddenError,
    OutcomeSignals,
    SalesService,
    ValidationFailed,
    events_from_outcome,
    validate_sale,
)

NOW = datetime(2026, 11, 5, 15, 0, tzinfo=timezone.utc)
LEAD = uuid.uuid4()
PRODUCT = uuid.uuid4()


class RecordingRepo:
    def __init__(self):
        self.sales = []
        self.cancelled = []

    def confirm_sale(self, record, products):
        self.sales.append((record, products))
        return "created"

    def cancel_sale(self, sale_ref, *, reason, actor):
        self.cancelled.append((sale_ref, reason, actor))


def _service(repo=None):
    return SalesService(repo or RecordingRepo(), clock=lambda: NOW)


def _confirm(service, role="admin", **overrides):
    args = dict(sale_ref="VTA-0001", lead_id=LEAD, amount_cop=2_990_000, sold_at=NOW - timedelta(hours=1),
                source="admin_manual", confirmed_by="ana", evidence_ref="factura FV-100", products={PRODUCT: 1})
    args.update(overrides)
    return service.confirm(role=role, **args)


# --- Permisos ---

@pytest.mark.parametrize("role", ["operator", None, "", "ADMIN", "agent"])
def test_only_the_admin_role_can_confirm_a_sale(role):
    repo = RecordingRepo()
    with pytest.raises(ForbiddenError):
        _confirm(_service(repo), role=role)
    assert repo.sales == []


def test_admin_can_confirm_and_the_record_is_normalised():
    repo = RecordingRepo()
    _confirm(_service(repo), evidence_ref="  factura FV-100  ")
    record, products = repo.sales[0]
    assert record.evidence_ref == "factura FV-100" and record.amount_cop == 2_990_000
    assert record.sold_at.tzinfo is not None and products == {PRODUCT: 1}


def test_operators_cannot_cancel_sales_either():
    with pytest.raises(ForbiddenError):
        _service().cancel(role="operator", sale_ref="VTA-0001", reason="error", actor="x")


# --- Validación de cada campo antes de persistir ---

@pytest.mark.parametrize("overrides, code", [
    ({"amount_cop": 0}, "amount_out_of_range"),
    ({"amount_cop": -5}, "amount_out_of_range"),
    ({"amount_cop": 2990000.5}, "amount_not_integer_cop"),
    ({"amount_cop": True}, "amount_not_integer_cop"),
    ({"amount_cop": 10_000_000_001}, "amount_out_of_range"),
    ({"sold_at": datetime(2026, 11, 5, 15, 0)}, "sold_at_without_timezone"),
    ({"sold_at": NOW + timedelta(days=3)}, "sold_at_in_future"),
    ({"source": "ia_detected"}, "confirmation_source_invalid"),
    ({"evidence_ref": "  "}, "evidence_ref_required"),
    ({"confirmed_by": ""}, "confirmed_by_invalid"),
    ({"sale_ref": "AB"}, "sale_ref_invalid"),
    ({"products": {PRODUCT: 0}}, "product_quantity_invalid"),
])
def test_invalid_sale_fields_are_rejected_before_any_write(overrides, code):
    repo = RecordingRepo()
    with pytest.raises(ValidationFailed) as info:
        _confirm(_service(repo), **overrides)
    assert info.value.code == code
    assert repo.sales == []


def test_naive_datetime_is_refused_instead_of_guessing_the_timezone():
    with pytest.raises(ValidationFailed):
        validate_sale(sale_ref="VTA-1", lead_id=LEAD, amount_cop=1, sold_at=datetime(2026, 11, 5), source="admin_manual",
                      confirmed_by="a", evidence_ref="doc", products={}, now=NOW)


def test_cancel_requires_a_reason():
    with pytest.raises(ValidationFailed):
        _service().cancel(role="admin", sale_ref="VTA-0001", reason="   ", actor="ana")


# --- Eventos derivados: intención no es venta ---

def test_purchase_intent_produces_an_event_and_never_a_sale():
    signals = OutcomeSignals(lead_stage="HIGH_INTENT", handoff_reason=None)
    events = events_from_outcome(signals, "wamid.X1")
    assert [e.event_type for e in events] == ["purchase_intent"]
    assert all(e.event_type != "sale_confirmed" for e in events)


def test_quote_and_handoff_are_separate_events_with_stable_keys():
    signals = OutcomeSignals(lead_stage="HUMAN_REQUIRED", handoff_reason="quote_request")
    events = events_from_outcome(signals, "wamid.Q1")
    assert [e.event_type for e in events] == ["quote_request", "handoff_to_advisor"]
    assert events_from_outcome(signals, "wamid.Q1") == events  # determinista: reintentos no cambian las claves


def test_product_interest_uses_only_validated_product_ids():
    signals = OutcomeSignals(lead_stage="DISCOVERY", handoff_reason=None, product_ids=(PRODUCT,),
                             product_categories={PRODUCT: "Cafeteras automáticas"})
    events = events_from_outcome(signals, "wamid.P1")
    assert len(events) == 1 and events[0].product_id == PRODUCT and events[0].category == "Cafeteras automáticas"


def test_a_plain_greeting_produces_no_commercial_event():
    assert events_from_outcome(OutcomeSignals(lead_stage="NEW", handoff_reason=None), "wamid.G1") == []
