"""Dobles de prueba para pruebas unitarias. No forman parte de la aplicación."""

from __future__ import annotations

import threading

from app.repositories.errors import StorageError
from app.repositories.webhook_events import RegisterOutcome, WebhookEventRecord


class FakeWebhookEventRepository:
    """Imita la restricción única de PostgreSQL. Solo para pruebas unitarias."""

    def __init__(self) -> None:
        self.rows: dict[str, WebhookEventRecord] = {}
        self.fail_with: Exception | None = None
        self.calls = 0
        self._lock = threading.Lock()

    def register(self, record: WebhookEventRecord) -> RegisterOutcome:
        with self._lock:
            self.calls += 1
            if self.fail_with is not None:
                raise self.fail_with
            existing = self.rows.get(record.external_event_id)
            if existing is None:
                self.rows[record.external_event_id] = record
                return RegisterOutcome.NEW
            if existing.payload_sha256 == record.payload_sha256:
                return RegisterOutcome.DUPLICATE
            return RegisterOutcome.CONFLICT


class FailingWebhookEventRepository:
    """Simula una base caída. El mensaje de error imita un fallo real sin datos del payload."""

    def __init__(self) -> None:
        self.calls = 0

    def register(self, record: WebhookEventRecord) -> RegisterOutcome:
        self.calls += 1
        raise StorageError("webhook_event_write_failed")
