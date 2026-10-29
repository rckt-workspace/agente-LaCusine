"""Procesamiento de estados de Meta en el worker: la regla de reintento y las transiciones permitidas."""

import uuid
from datetime import datetime, timezone

import pytest

from app.repositories.jobs import Claim
from app.repositories.outbound import ALLOWED_META_TRANSITIONS, META_STATUSES
from app.worker.errors import PermanentJobError
from app.worker.processor import EventProcessor


class FakeOutboundStatus:
    def __init__(self, result="applied"):
        self.result = result
        self.calls = []

    def apply_meta_status(self, external_message_id, new_status, error_code=None, at=None):
        self.calls.append((external_message_id, new_status, error_code))
        return self.result


def _claim(payload):
    return Claim(
        event_id=uuid.uuid4(), external_event_id="status:wamid.X:delivered", contact_wa_id=None,
        business_phone_id="1", contact_key="event:x", lease_token=uuid.uuid4(), attempts=1,
        payload=payload, received_at=datetime.now(timezone.utc), event_type="status",
    )


def _processor(outbound):
    return EventProcessor(agent=None, leads=None, conversations=None, messages=None, handoffs=None,
                          outbox=None, retention_days=7, outbound=outbound)


def test_status_for_a_known_message_is_applied_and_processed():
    outbound = FakeOutboundStatus("applied")
    result = _processor(outbound).process(_claim({"id": "wamid.X", "status": "delivered"}), lambda: True)
    assert result.status == "processed" and result.error_code == "delivered"
    assert outbound.calls == [("wamid.X", "delivered", None)]


def test_status_arriving_before_the_sent_confirmation_is_retried_not_lost():
    result = _processor(FakeOutboundStatus("unmatched")).process(
        _claim({"id": "wamid.X", "status": "delivered"}), lambda: True)
    assert result.retry is True
    assert result.error_code == "status_unmatched"


def test_duplicate_or_out_of_order_status_is_ignored_as_no_transition():
    result = _processor(FakeOutboundStatus("unchanged")).process(
        _claim({"id": "wamid.X", "status": "sent"}), lambda: True)
    assert result.status == "ignored" and result.error_code == "no_transition"


def test_unknown_status_value_is_ignored_without_touching_state():
    outbound = FakeOutboundStatus()
    result = _processor(outbound).process(_claim({"id": "wamid.X", "status": "played"}), lambda: True)
    assert result.error_code == "unknown_status"
    assert outbound.calls == []


def test_error_code_from_meta_is_stored_without_message_text():
    outbound = FakeOutboundStatus()
    _processor(outbound).process(
        _claim({"id": "wamid.X", "status": "failed",
                "errors": [{"code": 131047, "title": "texto privado del cliente", "message": "detalle"}]}),
        lambda: True)
    assert outbound.calls == [("wamid.X", "failed", "131047")]


@pytest.mark.parametrize("payload", [{"status": "sent"}, {"id": "wamid.X"}, {"id": 5, "status": "sent"}])
def test_malformed_status_payload_is_a_permanent_error(payload):
    with pytest.raises(PermanentJobError):
        _processor(FakeOutboundStatus()).process(_claim(payload), lambda: True)


def test_status_without_outbound_handler_is_a_permanent_configuration_error():
    with pytest.raises(PermanentJobError, match="status_handler_missing"):
        _processor(None).process(_claim({"id": "wamid.X", "status": "sent"}), lambda: True)


def test_transition_table_only_moves_forward():
    assert ("uncertain", "delivered") in ALLOWED_META_TRANSITIONS
    assert ("sent", "read") in ALLOWED_META_TRANSITIONS
    assert ("delivered", "sent") not in ALLOWED_META_TRANSITIONS
    assert ("read", "delivered") not in ALLOWED_META_TRANSITIONS
    assert ("read", "failed") not in ALLOWED_META_TRANSITIONS
    assert META_STATUSES == {"sent", "delivered", "read", "failed"}
