"""Notificaciones a asesores: despachador con repositorio en memoria. Ningún canal real."""

import uuid

from app.services.advisor_notifications import (
    AdvisorNotice,
    AdvisorNotificationDispatcher,
    ClaimedNotice,
    NotificationError,
    SimulatedAdvisorNotifier,
)


class MemoryRepository:
    """Doble fiel del repositorio: mismos métodos y mismas reglas de propiedad por token."""

    def __init__(self, notices, max_attempts=3):
        self.rows = {n.notification_id: {"notice": n, "status": "pending", "attempts": 0, "token": None}
                     for n in notices}
        self.max_attempts = max_attempts

    def claim(self, limit, lease_seconds, token):
        claimed = []
        for row in list(self.rows.values())[:limit]:
            if row["status"] == "pending":
                row["status"], row["token"] = "sending", token
                row["attempts"] += 1
                claimed.append(ClaimedNotice(row["notice"], token, row["attempts"]))
        return claimed

    def finish_sent(self, claim):
        row = self.rows[claim.notice.notification_id]
        if row["status"] != "sending" or row["token"] != claim.lease_token:
            return False
        row["status"], row["token"] = "sent", None
        return True

    def finish_failed(self, claim, code, retryable):
        row = self.rows[claim.notice.notification_id]
        if row["status"] != "sending" or row["token"] != claim.lease_token:
            return False
        row["status"] = "pending" if retryable and row["attempts"] < self.max_attempts else "failed"
        row["token"] = None
        row["code"] = code
        return True

    def expire_leases(self):
        return 0


def _notice():
    return AdvisorNotice(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), "warranty", "high")


def _dispatcher(repo, notifier):
    return AdvisorNotificationDispatcher(repo, notifier, owner_token_factory=uuid.uuid4, lease_seconds=60,
                                         batch_size=10)


def test_notice_is_sent_once_and_marked_sent():
    notice = _notice()
    repo = MemoryRepository([notice])
    notifier = SimulatedAdvisorNotifier()
    stats = _dispatcher(repo, notifier).dispatch_once()
    assert stats.sent == 1 and notifier.sent == [notice]
    assert repo.rows[notice.notification_id]["status"] == "sent"
    assert _dispatcher(repo, notifier).dispatch_once().claimed == 0


def test_notice_contains_only_identifiers_reason_and_priority():
    fields = set(AdvisorNotice.__dataclass_fields__)
    assert fields == {"notification_id", "handoff_id", "conversation_id", "reason", "priority"}


def test_retryable_channel_error_returns_to_pending_then_fails_after_max_attempts():
    notice = _notice()
    repo = MemoryRepository([notice], max_attempts=2)
    notifier = SimulatedAdvisorNotifier(script=[NotificationError("channel_busy", retryable=True)] * 2)
    dispatcher = _dispatcher(repo, notifier)
    assert dispatcher.dispatch_once().retry == 1
    assert repo.rows[notice.notification_id]["status"] == "pending"
    assert dispatcher.dispatch_once().retry == 1
    assert repo.rows[notice.notification_id]["status"] == "failed"


def test_non_retryable_channel_error_fails_immediately_with_stable_code():
    notice = _notice()
    repo = MemoryRepository([notice])
    notifier = SimulatedAdvisorNotifier(script=[NotificationError("recipient_rejected", retryable=False)])
    stats = _dispatcher(repo, notifier).dispatch_once()
    assert stats.failed == 1
    assert repo.rows[notice.notification_id]["code"] == "recipient_rejected"


def test_unexpected_channel_exception_never_stops_the_cycle_and_hides_details():
    notice = _notice()
    other = _notice()
    repo = MemoryRepository([notice, other])

    class Broken:
        calls = 0

        def notify(self, n):
            Broken.calls += 1
            if Broken.calls == 1:
                raise RuntimeError("secreto-en-el-mensaje")
            return None

    stats = _dispatcher(repo, Broken()).dispatch_once()
    assert stats.claimed == 2 and stats.sent == 1 and stats.retry == 1
    assert repo.rows[notice.notification_id]["code"] == "notifier_unexpected"
    assert "secreto" not in repo.rows[notice.notification_id]["code"]


def test_stale_owner_cannot_mark_a_notice_sent():
    notice = _notice()
    repo = MemoryRepository([notice])
    stale = repo.claim(1, 60, uuid.uuid4())[0]
    repo.rows[notice.notification_id]["token"] = uuid.uuid4()  # otro propietario tomó el aviso
    assert repo.finish_sent(stale) is False
