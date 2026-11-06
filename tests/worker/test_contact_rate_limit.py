"""Límite de frecuencia por conversación: sin llamadas al modelo ni respuestas al exceder. Sin base de datos."""

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


def _processor(messages, outbox, agent, limits=(20, 60)):
    return EventProcessor(
        agent=agent, leads=FakeLeads(), conversations=FakeConversations(), messages=messages,
        handoffs=FakeHandoffs(), outbox=outbox, retention_days=7, expected_business_phone_id="1",
        contact_limits=limits,
    )


def _reply_agent():
    return FakeAgent(AgentOutcome(OutcomeStatus.REPLIED, reply_text="Hola, ¿qué buscas?",
                                  lead_stage=LeadStage.DISCOVERY))


def test_contact_over_the_hourly_limit_is_ignored_without_calling_the_model_or_replying():
    messages, outbox, agent = FakeMessages(), FakeOutbox(), _reply_agent()
    messages.inbound_count = 21  # más de 20 en la última hora
    result = _processor(messages, outbox, agent).process(_claim(), ALWAYS_RENEW)
    assert result.status == "ignored" and result.error_code == "contact_rate_limited"
    assert agent.calls == []
    assert outbox.rows[0]["reply_text"] is None


def test_contact_over_the_daily_limit_is_also_ignored():
    messages, outbox, agent = FakeMessages(), FakeOutbox(), _reply_agent()
    messages.inbound_count = 61  # el conteo diario supera 60
    result = _processor(messages, outbox, agent, limits=(1000, 60)).process(_claim(), ALWAYS_RENEW)
    assert result.error_code == "contact_rate_limited"
    assert agent.calls == []


def test_contact_under_the_limits_is_answered_normally():
    messages, outbox, agent = FakeMessages(), FakeOutbox(), _reply_agent()
    messages.inbound_count = 3
    result = _processor(messages, outbox, agent).process(_claim(), ALWAYS_RENEW)
    assert result.status == "processed"
    assert len(agent.calls) == 1
    assert outbox.rows[0]["reply_text"] == "Hola, ¿qué buscas?"


def test_without_configured_limits_nothing_is_counted_or_blocked():
    messages, outbox, agent = FakeMessages(), FakeOutbox(), _reply_agent()
    messages.inbound_count = 10_000
    result = _processor(messages, outbox, agent, limits=None).process(_claim(), ALWAYS_RENEW)
    assert result.status == "processed"


def test_a_limited_contact_does_not_change_another_contacts_handling():
    messages, outbox, agent = FakeMessages(), FakeOutbox(), _reply_agent()
    messages.inbound_count = 21
    _processor(messages, outbox, agent).process(_claim(contact="573001112233", external="wamid.A1"), ALWAYS_RENEW)
    messages.inbound_count = 1
    _processor(messages, outbox, agent).process(_claim(contact="573009998877", external="wamid.B1"), ALWAYS_RENEW)
    assert len(agent.calls) == 1
    assert outbox.rows[-1]["reply_text"] == "Hola, ¿qué buscas?"
