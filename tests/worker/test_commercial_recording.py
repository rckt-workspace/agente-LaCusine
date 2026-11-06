"""Registro de eventos comerciales desde el procesador: aislado del camino de respuesta. Sin base de datos."""

import uuid
from datetime import datetime, timezone

from app.agent.orchestrator import AgentOutcome, OutcomeStatus
from app.agent.schemas import LeadStage
from app.worker.processor import EventProcessor
from tests.worker.test_processor import (
    ALWAYS_RENEW,
    FakeAgent,
    FakeConversations,
    FakeHandoffs,
    FakeLeads,
    FakeMessages,
    FakeOutbox,
    _claim,
)

NOW = datetime(2026, 11, 5, 15, 0, tzinfo=timezone.utc)
PRODUCT = uuid.uuid4()


class RecordingCommercial:
    def __init__(self, fail=False):
        self.events = []
        self.fail = fail

    def record_event(self, **kwargs):
        if self.fail:
            raise RuntimeError("base no disponible: detalle que nunca debe registrarse")
        self.events.append(kwargs)
        return True


def _processor(agent, outbox, commercial):
    return EventProcessor(
        agent=agent, leads=FakeLeads(), conversations=FakeConversations(), messages=FakeMessages(),
        handoffs=FakeHandoffs(), outbox=outbox, retention_days=7, expected_business_phone_id="1",
        clock=lambda: NOW, commercial=commercial,
    )


def _agent(outcome):
    return FakeAgent(outcome)


def test_purchase_intent_and_product_interest_are_recorded_with_the_event_time():
    commercial = RecordingCommercial()
    outcome = AgentOutcome(OutcomeStatus.REPLIED, reply_text="Te recomiendo la C3.",
                           lead_stage=LeadStage.HIGH_INTENT, product_ids=(PRODUCT,))
    _processor(_agent(outcome), FakeOutbox(), commercial).process(_claim(external="wamid.R1"), ALWAYS_RENEW)
    types = sorted(e["event_type"] for e in commercial.events)
    assert types == ["product_interest", "purchase_intent"]
    assert all(e["occurred_at"] == NOW for e in commercial.events)
    assert all(e["dedupe_key"].endswith("wamid.R1") or str(PRODUCT) in e["dedupe_key"] for e in commercial.events)


def test_a_failing_recorder_never_blocks_the_customer_reply():
    outbox = FakeOutbox()
    outcome = AgentOutcome(OutcomeStatus.REPLIED, reply_text="Hola, ¿qué buscas?",
                           lead_stage=LeadStage.HIGH_INTENT, product_ids=(PRODUCT,))
    result = _processor(_agent(outcome), outbox, RecordingCommercial(fail=True)).process(_claim(), ALWAYS_RENEW)
    assert result.status == "processed"
    assert outbox.rows[0]["reply_text"] == "Hola, ¿qué buscas?"


def test_failure_details_are_never_written_to_the_logs(caplog):
    import logging
    outcome = AgentOutcome(OutcomeStatus.REPLIED, reply_text="Hola", lead_stage=LeadStage.HIGH_INTENT)
    with caplog.at_level(logging.WARNING, logger="lacusine.worker"):
        _processor(_agent(outcome), FakeOutbox(), RecordingCommercial(fail=True)).process(_claim(), ALWAYS_RENEW)
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "commercial_event_skipped" in text and "detalle que nunca debe registrarse" not in text


def test_without_a_recorder_nothing_is_recorded_and_the_reply_is_unchanged():
    outbox = FakeOutbox()
    outcome = AgentOutcome(OutcomeStatus.REPLIED, reply_text="Hola", lead_stage=LeadStage.DISCOVERY)
    _processor(_agent(outcome), outbox, None).process(_claim(), ALWAYS_RENEW)
    assert outbox.rows[0]["reply_text"] == "Hola"


def test_non_reply_outcomes_record_nothing():
    commercial = RecordingCommercial()
    _processor(_agent(AgentOutcome(OutcomeStatus.DISABLED, error_code="agent_disabled")),
               FakeOutbox(), commercial).process(_claim(), ALWAYS_RENEW)
    assert commercial.events == []
