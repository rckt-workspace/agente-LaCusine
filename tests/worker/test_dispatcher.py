"""Reglas de decisión del dispatcher de salidas. Repositorio en memoria; sin red ni base de datos."""

import random
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.integrations.whatsapp_client import (
    BusyError,
    MetaRejectedError,
    RateLimitedError,
    RecipientNotAllowedError,
    SendDisabledError,
    SimulatedWhatsAppSender,
    UncertainSendError,
)
from app.repositories.errors import StorageError
from app.repositories.outbound import OutboundClaim
from app.worker.dispatcher import OutboxDispatcher, window_open

PHONE = "123456789012"
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
TOKEN = uuid.UUID("00000000-0000-4000-8000-000000000001")


class FakeOutbound:
    def __init__(self, *, state="automated", last_inbound=NOW - timedelta(hours=1), wa="573001112233"):
        self.state = state
        self.last_inbound = last_inbound
        self.wa = wa
        self.pending: list[OutboundClaim] = []
        self.finished: list[dict] = []
        self.expired = 0
        self.lease_lost = False

    def add(self, attempts=1, key="wamid.A:reply"):
        claim = OutboundClaim(uuid.uuid4(), key, uuid.uuid4(), "Hola", attempts, TOKEN,
                              kind="reply", business_phone_id=PHONE)
        self.pending.append(claim)
        return claim

    def expire_leases(self):
        return self.expired

    def claim(self, limit, owner, lease_seconds, token):
        batch, self.pending = self.pending[:limit], self.pending[limit:]
        return batch

    def dispatch_context(self, outbound_id):
        from app.repositories.outbound import DispatchContext
        return DispatchContext(self.state, self.wa, self.last_inbound)

    def finish(self, claim, status, **kwargs):
        self.finished.append({"id": claim.outbound_id, "status": status, **kwargs})
        return not self.lease_lost


class CountingSender:
    def __init__(self, failure=None, enabled=True):
        self.failure = failure
        self.enabled = enabled
        self.calls: list = []

    def send_text(self, request):
        self.calls.append(request)
        if self.failure:
            raise self.failure
        return SimulatedWhatsAppSender().send_text(request)


def _dispatcher(repo, sender, max_attempts=3, clock=NOW):
    return OutboxDispatcher(
        repository=repo, sender=sender, business_phone_id=PHONE, owner="w1", lease_seconds=60,
        max_attempts=max_attempts, backoff_base_seconds=5, backoff_max_seconds=900,
        clock=lambda: clock, rng=random.Random(4), token_factory=lambda: TOKEN,
    )


def test_disabled_sender_claims_nothing_and_never_touches_the_queue():
    repo = FakeOutbound()
    repo.add()
    stats = _dispatcher(repo, CountingSender(enabled=False)).dispatch_once()
    assert stats.claimed == 0
    assert len(repo.pending) == 1 and repo.finished == []


def test_successful_send_stores_meta_id_and_originating_number():
    repo = FakeOutbound()
    repo.add()
    sender = CountingSender()
    stats = _dispatcher(repo, sender).dispatch_once()
    assert stats.sent == 1
    finished = repo.finished[0]
    assert finished["status"] == "sent"
    assert finished["external_message_id"].startswith("wamid.SIM")
    assert finished["from_phone_id"] == PHONE
    assert sender.calls[0].business_phone_id == PHONE


def test_human_controlled_conversation_cancels_without_sending():
    repo = FakeOutbound(state="paused_for_human")
    repo.add()
    sender = CountingSender()
    stats = _dispatcher(repo, sender).dispatch_once()
    assert stats.cancelled == 1
    assert sender.calls == []
    assert repo.finished[0]["status"] == "cancelled"
    assert repo.finished[0]["error_code"] == "human_control"


def test_closed_service_window_blocks_free_text_without_sending():
    repo = FakeOutbound(last_inbound=NOW - timedelta(hours=25))
    repo.add()
    sender = CountingSender()
    stats = _dispatcher(repo, sender).dispatch_once()
    assert stats.blocked == 1
    assert sender.calls == []
    assert repo.finished[0]["error_code"] == "outside_service_window"


def test_conversation_started_by_company_has_no_window_and_is_blocked():
    repo = FakeOutbound(last_inbound=None)
    repo.add()
    _dispatcher(repo, CountingSender()).dispatch_once()
    assert repo.finished[0]["status"] == "blocked"


@pytest.mark.parametrize("delta, expected", [
    (timedelta(hours=23, minutes=59), True),
    (timedelta(hours=24), True),
    (timedelta(hours=24, seconds=1), False),
])
def test_service_window_boundary(delta, expected):
    assert window_open(NOW - delta, NOW) is expected
    assert window_open(None, NOW) is False


def test_missing_destination_fails_without_sending():
    repo = FakeOutbound(wa="")
    repo.add()
    sender = CountingSender()
    _dispatcher(repo, sender).dispatch_once()
    assert sender.calls == []
    assert repo.finished[0]["error_code"] == "missing_destination"


def test_retryable_error_schedules_pending_with_backoff_below_limit():
    repo = FakeOutbound()
    repo.add(attempts=1)
    _dispatcher(repo, CountingSender(failure=RateLimitedError())).dispatch_once()
    finished = repo.finished[0]
    assert finished["status"] == "pending"
    assert finished["retry_after_seconds"] >= 1


def test_retryable_error_at_attempt_limit_becomes_failed():
    repo = FakeOutbound()
    repo.add(attempts=3)
    _dispatcher(repo, CountingSender(failure=RateLimitedError()), max_attempts=3).dispatch_once()
    assert repo.finished[0]["status"] == "failed"
    assert repo.finished[0]["error_code"] == "rate_limited_attempts_exhausted"


def test_uncertain_result_is_never_retried_automatically():
    repo = FakeOutbound()
    repo.add(attempts=1)
    stats = _dispatcher(repo, CountingSender(failure=UncertainSendError("timeout"))).dispatch_once()
    assert stats.uncertain == 1
    assert repo.finished[0]["status"] == "uncertain"
    assert repo.finished[0].get("retry_after_seconds") is None


@pytest.mark.parametrize("error", [RecipientNotAllowedError(), MetaRejectedError("http_400")])
def test_definitive_rejection_fails_without_retry(error):
    repo = FakeOutbound()
    repo.add(attempts=1)
    _dispatcher(repo, CountingSender(failure=error)).dispatch_once()
    assert repo.finished[0]["status"] == "failed"


def test_busy_sender_is_retryable_not_failed():
    repo = FakeOutbound()
    repo.add(attempts=1)
    _dispatcher(repo, CountingSender(failure=BusyError())).dispatch_once()
    assert repo.finished[0]["status"] == "pending"


def test_send_disabled_releases_back_to_pending_after_fixed_delay():
    repo = FakeOutbound()
    repo.add(attempts=1)
    _dispatcher(repo, CountingSender(failure=SendDisabledError())).dispatch_once()
    assert repo.finished[0]["status"] == "pending"
    assert repo.finished[0]["retry_after_seconds"] == 300


def test_lost_lease_after_send_is_counted_not_crashed():
    repo = FakeOutbound()
    repo.lease_lost = True
    repo.add()
    stats = _dispatcher(repo, CountingSender()).dispatch_once()
    assert stats.sent == 0
    assert stats.codes.get("lease_lost") == 1


def test_expired_leases_are_recovered_every_cycle_as_uncertain_upstream():
    repo = FakeOutbound()
    repo.expired = 2
    stats = _dispatcher(repo, CountingSender()).dispatch_once()
    assert stats.expired == 2


def test_storage_error_during_send_result_propagates_for_the_runner_to_handle():
    class BrokenFinish(FakeOutbound):
        def finish(self, claim, status, **kwargs):
            raise StorageError("outbound_finish_failed")

    repo = BrokenFinish()
    repo.add()
    with pytest.raises(StorageError):
        _dispatcher(repo, CountingSender()).dispatch_once()
