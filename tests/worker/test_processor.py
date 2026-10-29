"""Procesamiento de eventos: mapeo de resultados del agente a persistencia y decisiones. Sin base de datos."""

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.agent.orchestrator import AgentOutcome, OutcomeStatus
from app.agent.safety import SAFE_REPLY_HUMAN
from app.agent.schemas import LeadStage
from app.repositories.errors import StorageError
from app.repositories.jobs import Claim
from app.worker.errors import ManualReviewRequired, PermanentJobError
from app.worker.processor import UNSUPPORTED_TYPE_REPLY, EventProcessor


class FakeAgent:
    def __init__(self, outcome=None, error=None):
        self.outcome = outcome
        self.error = error
        self.calls: list[tuple] = []

    def handle(self, conversation_id, text, call_id):
        self.calls.append((conversation_id, text, call_id))
        if self.error:
            raise self.error
        return self.outcome


class FakeLeads:
    def __init__(self):
        self.created: list[str] = []
        self.id = uuid.uuid4()

    def get_or_create(self, wa_id):
        self.created.append(wa_id)
        return self.id


class FakeConversations:
    def __init__(self):
        self.id = uuid.uuid4()

    def get_or_open(self, lead_id):
        return self.id


class FakeMessages:
    def __init__(self):
        self.inbound: list[dict] = []

    def record_inbound(self, conversation_id, wa_message_id, message_type, received_at, content_text=None,
                       content_expires_at=None):
        self.inbound.append({"wa": wa_message_id, "type": message_type, "text": content_text,
                             "expires": content_expires_at})
        return True


class FakeHandoffs:
    def __init__(self):
        self.opened: list[tuple] = []

    def open(self, conversation_id, reason, priority="normal"):
        self.opened.append((reason, priority))
        return uuid.uuid4()


class FakeOutbox:
    def __init__(self, error=None):
        self.rows: list[dict] = []
        self.error = error

    def persist_result(self, **kwargs):
        if self.error:
            raise self.error
        self.rows.append(kwargs)
        return True


def _claim(payload=None, contact="573001112233", external="wamid.P1"):
    return Claim(
        event_id=uuid.uuid4(), external_event_id=external, contact_wa_id=contact, business_phone_id="1",
        contact_key="1:" + str(contact), lease_token=uuid.uuid4(), attempts=1,
        payload=payload if payload is not None else {"type": "text", "text": {"body": "busco una nevera"}},
        received_at=datetime.now(timezone.utc),
    )


def _processor(agent, outbox=None, handoffs=None, messages=None):
    return EventProcessor(
        agent=agent, leads=FakeLeads(), conversations=FakeConversations(), messages=messages or FakeMessages(),
        handoffs=handoffs or FakeHandoffs(), outbox=outbox or FakeOutbox(), retention_days=7,
    )


def _outcome(status, **kwargs):
    return AgentOutcome(status=status, **kwargs)


ALWAYS_RENEW = lambda: True  # noqa: E731


def test_text_message_is_persisted_with_expiry_and_reply_goes_to_outbox_pending():
    outbox = FakeOutbox()
    messages = FakeMessages()
    agent = FakeAgent(_outcome(OutcomeStatus.REPLIED, reply_text="Tenemos varias opciones.",
                               lead_stage=LeadStage.DISCOVERY))
    result = _processor(agent, outbox=outbox, messages=messages).process(_claim(), ALWAYS_RENEW)
    assert result.status == "processed"
    assert messages.inbound[0]["text"] == "busco una nevera"
    assert messages.inbound[0]["expires"] is not None
    assert outbox.rows[0]["reply_text"] == "Tenemos varias opciones."
    assert outbox.rows[0]["idempotency_key"] == "wamid.P1:reply"
    assert outbox.rows[0]["lead_status"] == "qualifying"


@pytest.mark.parametrize("stage, expected", [
    (LeadStage.NEW, "new"), (LeadStage.QUALIFIED, "qualified"),
    (LeadStage.HIGH_INTENT, "qualified"), (LeadStage.HUMAN_REQUIRED, "handed_off"),
])
def test_commercial_stage_maps_to_lead_status(stage, expected):
    outbox = FakeOutbox()
    agent = FakeAgent(_outcome(OutcomeStatus.REPLIED, reply_text="ok", lead_stage=stage))
    _processor(agent, outbox=outbox).process(_claim(), ALWAYS_RENEW)
    assert outbox.rows[0]["lead_status"] == expected


def test_non_text_message_gets_fixed_policy_reply_without_calling_model():
    outbox = FakeOutbox()
    agent = FakeAgent(_outcome(OutcomeStatus.REPLIED, reply_text="nunca"))
    messages = FakeMessages()
    result = _processor(agent, outbox=outbox, messages=messages).process(
        _claim(payload={"type": "image", "image": {"id": "x"}}), ALWAYS_RENEW)
    assert result.status == "processed" and result.error_code == "unsupported_type"
    assert agent.calls == []
    assert messages.inbound[0]["type"] == "image" and messages.inbound[0]["text"] is None
    assert outbox.rows[0]["reply_text"] == UNSUPPORTED_TYPE_REPLY


def test_unknown_message_type_is_stored_as_unknown():
    messages = FakeMessages()
    _processor(FakeAgent(), messages=messages).process(
        _claim(payload={"type": "hologram"}), ALWAYS_RENEW)
    assert messages.inbound[0]["type"] == "unknown"


def test_human_controlled_conversation_is_ignored_without_reply():
    outbox = FakeOutbox()
    agent = FakeAgent(_outcome(OutcomeStatus.SUPPRESSED_HUMAN_CONTROL, error_code="paused_for_human"))
    result = _processor(agent, outbox=outbox).process(_claim(), ALWAYS_RENEW)
    assert result.status == "ignored" and result.error_code == "human_control"
    assert outbox.rows[0]["reply_text"] is None


def test_budget_blocked_escalates_to_human_with_safe_message():
    outbox = FakeOutbox()
    handoffs = FakeHandoffs()
    agent = FakeAgent(_outcome(OutcomeStatus.BUDGET_BLOCKED, error_code="daily_budget"))
    result = _processor(agent, outbox=outbox, handoffs=handoffs).process(_claim(), ALWAYS_RENEW)
    assert result.status == "processed"
    assert handoffs.opened == [("other", "normal")]
    assert outbox.rows[0]["reply_text"] == SAFE_REPLY_HUMAN
    assert outbox.rows[0]["lead_status"] == "handed_off"


def test_disabled_agent_escalates_to_human():
    handoffs = FakeHandoffs()
    agent = FakeAgent(_outcome(OutcomeStatus.DISABLED, error_code="agent_disabled"))
    _processor(agent, handoffs=handoffs).process(_claim(), ALWAYS_RENEW)
    assert handoffs.opened == [("other", "normal")]


def test_rate_limit_not_billed_requests_retry():
    agent = FakeAgent(_outcome(OutcomeStatus.PROVIDER_ERROR, error_code="rate_limited", billed_state="not_billed"))
    result = _processor(agent).process(_claim(), ALWAYS_RENEW)
    assert result.retry is True and result.error_code == "rate_limited"


def test_uncertain_provider_result_is_manual_review_never_retry():
    agent = FakeAgent(_outcome(OutcomeStatus.PROVIDER_ERROR, error_code="timeout", billed_state="uncertain"))
    with pytest.raises(ManualReviewRequired) as excinfo:
        _processor(agent).process(_claim(), ALWAYS_RENEW)
    assert excinfo.value.code == "timeout"


def test_auth_failure_is_permanent():
    agent = FakeAgent(_outcome(OutcomeStatus.PROVIDER_ERROR, error_code="auth_failed", billed_state="not_billed"))
    with pytest.raises(PermanentJobError) as excinfo:
        _processor(agent).process(_claim(), ALWAYS_RENEW)
    assert excinfo.value.code == "provider_auth_failed"


def test_reservation_conflict_outcome_requires_reconciliation():
    agent = FakeAgent(_outcome(OutcomeStatus.REQUIRES_RECONCILIATION, error_code="reservation_reserved"))
    with pytest.raises(ManualReviewRequired) as excinfo:
        _processor(agent).process(_claim(), ALWAYS_RENEW)
    assert excinfo.value.code == "reservation_reserved"


def test_lost_lease_stops_before_any_model_call():
    agent = FakeAgent(_outcome(OutcomeStatus.REPLIED, reply_text="x"))
    result = _processor(agent).process(_claim(), lambda: False)
    assert result.status == "lease_lost"
    assert agent.calls == []


def test_missing_sender_is_permanent_error():
    with pytest.raises(PermanentJobError) as excinfo:
        _processor(FakeAgent()).process(_claim(contact=None), ALWAYS_RENEW)
    assert excinfo.value.code == "missing_sender"


@pytest.mark.parametrize("payload", [
    {"type": "text"},
    {"type": "text", "text": {"body": "   "}},
    {"type": "text", "text": "no es objeto"},
])
def test_text_without_body_is_permanent_error(payload):
    with pytest.raises(PermanentJobError) as excinfo:
        _processor(FakeAgent()).process(_claim(payload=payload), ALWAYS_RENEW)
    assert excinfo.value.code == "invalid_text_payload"


def test_storage_error_from_outbox_propagates_as_transient():
    agent = FakeAgent(_outcome(OutcomeStatus.REPLIED, reply_text="ok"))
    with pytest.raises(StorageError):
        _processor(agent, outbox=FakeOutbox(error=StorageError("outbox_write_failed"))).process(_claim(), ALWAYS_RENEW)


def test_agent_cost_is_not_part_of_outbox_decision():
    outbox = FakeOutbox()
    agent = FakeAgent(_outcome(OutcomeStatus.REPLIED, reply_text="ok", cost_usd=Decimal("0.001")))
    _processor(agent, outbox=outbox).process(_claim(), ALWAYS_RENEW)
    assert "cost" not in outbox.rows[0]
