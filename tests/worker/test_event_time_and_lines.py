"""Hora del evento de Meta, línea de negocio y comportamiento del procesador. Sin base de datos."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.repositories.jobs import Claim
from app.worker.errors import PermanentJobError
from app.worker.processor import MAX_CLOCK_SKEW, EventProcessor, meta_event_time
from tests.worker.test_processor import FakeAgent, FakeConversations, FakeHandoffs, FakeLeads, FakeMessages, FakeOutbox

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
LINE = "100200300400"


def _ts(delta: timedelta) -> str:
    return str(int((NOW + delta).timestamp()))


# --- Hora del evento ---

def test_recent_meta_timestamp_is_trusted_and_used_as_event_time():
    value, trusted = meta_event_time({"timestamp": _ts(-timedelta(minutes=2))}, NOW)
    assert trusted is True
    assert value == NOW - timedelta(minutes=2)


@pytest.mark.parametrize("payload", [
    {},                                  # ausente
    {"timestamp": ""},                   # vacío
    {"timestamp": "ayer"},               # no numérico
    {"timestamp": "12.5"},               # decimal
    {"timestamp": "1" * 13},             # fuera de rango de longitud
    {"timestamp": "1000"},               # anterior a 2020
    {"timestamp": 1761830400},           # entero, no texto (contrato de Meta: texto de dígitos)
])
def test_missing_or_invalid_timestamp_is_not_trusted_and_falls_back_to_processing_time(payload):
    value, trusted = meta_event_time(payload, NOW)
    assert trusted is False
    assert value == NOW


def test_future_timestamp_beyond_clock_skew_is_not_trusted():
    value, trusted = meta_event_time({"timestamp": _ts(MAX_CLOCK_SKEW + timedelta(seconds=30))}, NOW)
    assert trusted is False and value == NOW


def test_small_clock_skew_within_tolerance_is_accepted():
    _, trusted = meta_event_time({"timestamp": _ts(MAX_CLOCK_SKEW - timedelta(seconds=1))}, NOW)
    assert trusted is True


def test_delayed_queue_keeps_the_customer_time_not_the_processing_time():
    # Un mensaje de hace 30 horas procesado ahora conserva su hora real: la ventana ya no está abierta.
    value, trusted = meta_event_time({"timestamp": _ts(-timedelta(hours=30))}, NOW)
    assert trusted is True
    assert NOW - value > timedelta(hours=24)


# --- Línea de negocio ---

def _claim(line: str, payload=None, event_type="message"):
    return Claim(
        event_id=uuid.uuid4(), external_event_id="wamid.L1", contact_wa_id="573001112233",
        business_phone_id=line, contact_key=f"{line}:573001112233", lease_token=uuid.uuid4(), attempts=1,
        payload=payload or {"type": "text", "text": {"body": "hola"}, "timestamp": _ts(-timedelta(minutes=1))},
        received_at=NOW, event_type=event_type,
    )


def _processor(messages=None, outbox=None, handoffs=None):
    from app.agent.orchestrator import AgentOutcome, OutcomeStatus

    return EventProcessor(
        agent=FakeAgent(AgentOutcome(OutcomeStatus.REPLIED, reply_text="Hola")), leads=FakeLeads(), conversations=FakeConversations(),
        messages=messages or FakeMessages(), handoffs=handoffs or FakeHandoffs(), outbox=outbox or FakeOutbox(),
        retention_days=7, expected_business_phone_id=LINE, clock=lambda: NOW,
    )


def test_event_from_another_business_line_is_rejected_permanently_and_stores_nothing():
    messages = FakeMessages()
    with pytest.raises(PermanentJobError, match="foreign_business_line"):
        _processor(messages=messages).process(_claim("999999999999"), lambda: True)
    assert messages.inbound == []


def test_status_from_another_business_line_is_rejected_too():
    with pytest.raises(PermanentJobError, match="foreign_business_line"):
        _processor().process(_claim("999999999999", {"id": "wamid.X", "status": "sent"}, "status"), lambda: True)


def test_processor_requires_an_expected_business_line():
    with pytest.raises(ValueError):
        EventProcessor(agent=None, leads=None, conversations=None, messages=None, handoffs=None, outbox=None,
                       retention_days=7, expected_business_phone_id="")


def test_message_without_timestamp_is_stored_untrusted_so_it_cannot_open_the_window():
    messages = FakeMessages()
    claim = _claim(LINE, {"type": "text", "text": {"body": "hola"}})
    _processor(messages=messages).process(claim, lambda: True)
    assert messages.inbound[0]["window_trusted"] is False
    assert messages.inbound[0]["received_at"] == NOW


def test_trusted_message_is_stored_with_meta_time():
    messages = FakeMessages()
    stamp = NOW - timedelta(minutes=3)
    claim = _claim(LINE, {"type": "text", "text": {"body": "hola"}, "timestamp": str(int(stamp.timestamp()))})
    _processor(messages=messages).process(claim, lambda: True)
    assert messages.inbound[0]["window_trusted"] is True
    assert messages.inbound[0]["received_at"] == stamp


def test_handoff_outcome_sends_the_fixed_notice_not_the_model_text():
    from app.agent.orchestrator import AgentOutcome, OutcomeStatus
    from app.agent.safety import SAFE_REPLY_HUMAN

    outbox = FakeOutbox()
    agent_outcome = AgentOutcome(OutcomeStatus.REPLIED, reply_text="Te prometo un descuento del 50 %",
                                 requires_human=True, handoff_reason="warranty")
    processor = _processor(outbox=outbox)
    processor._agent = FakeAgent(agent_outcome)
    processor.process(_claim(LINE), lambda: True)
    notice = outbox.rows[-1]
    assert notice["kind"] == "handoff_notice"
    assert notice["reply_text"] == SAFE_REPLY_HUMAN
    assert "descuento" not in notice["reply_text"]
