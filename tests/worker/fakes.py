"""Dobles en memoria del repositorio de trabajos y del procesador. Solo lógica del runner; el SQL real se prueba aparte."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.repositories.jobs import Claim


@dataclass
class Row:
    event_id: uuid.UUID
    contact_key: str
    external_id: str
    payload: dict
    status: str = "received"
    attempts: int = 0
    token: uuid.UUID | None = None
    delay: int | None = None
    error: str | None = None
    payload_dropped: bool = False
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class InMemoryJobs:
    """Misma propiedad por token y exclusión por contacto que el repositorio PostgreSQL."""

    def __init__(self) -> None:
        self.rows: list[Row] = []
        self._lock = threading.Lock()
        self.renew_allowed = True

    def add(self, contact_key: str, external_id: str, payload: dict | None = None) -> Row:
        row = Row(uuid.uuid4(), contact_key, external_id, payload or {"type": "text", "text": {"body": "hola"}})
        self.rows.append(row)
        return row

    def claim(self, limit: int, token_factory) -> list[Claim]:
        claims: list[Claim] = []
        with self._lock:
            for row in sorted(self.rows, key=lambda r: r.received_at):
                if len(claims) >= limit:
                    break
                if row.status not in ("received", "queued", "retry"):
                    continue
                earlier_unfinished = [r for r in self.rows if r.contact_key == row.contact_key
                                      and r.received_at < row.received_at
                                      and r.status in ("received", "queued", "retry", "processing", "manual_review")]
                if earlier_unfinished:
                    continue
                if any(r.status == "processing" and r.contact_key == row.contact_key for r in self.rows):
                    continue
                token = token_factory()
                row.status = "processing"
                row.token = token
                row.attempts += 1
                claims.append(Claim(
                    event_id=row.event_id, external_event_id=row.external_id, contact_wa_id="573001112233",
                    business_phone_id="", contact_key=row.contact_key, lease_token=token,
                    attempts=row.attempts, payload=row.payload, received_at=row.received_at,
                ))
        return claims

    def renew(self, claim: Claim) -> bool:
        row = self._by_id(claim.event_id)
        return self.renew_allowed and row.status == "processing" and row.token == claim.lease_token

    def finish(self, claim: Claim, status: str, error_code=None, retry_after_seconds=None, drop_payload=False) -> bool:
        row = self._by_id(claim.event_id)
        if row.status != "processing" or row.token != claim.lease_token:
            return False
        row.status = status
        row.token = None
        row.error = error_code
        row.delay = retry_after_seconds
        if drop_payload or status in ("processed", "ignored"):
            row.payload = {}
            row.payload_dropped = True
        return True

    def recover_expired(self, max_attempts: int) -> int:
        return 0

    def _by_id(self, event_id) -> Row:
        return next(r for r in self.rows if r.event_id == event_id)
