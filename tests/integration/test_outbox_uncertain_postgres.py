"""El envío incierto no bloquea la conversación; no se reintenta nunca; el orden y la exclusión se conservan.

Se ejecuta SOLO contra una base aislada de pruebas (guardia de tests/integration/db_guard.py). Sin Meta, sin Anthropic.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from app.db.pool import create_pool
from app.repositories.outbound import OutboundRepository
from tests.integration.test_postgres_persistence import TEST_DATABASE_URL, _reset_and_migrate

pytestmark = pytest.mark.integration

if not TEST_DATABASE_URL:
    pytest.skip("TEST_DATABASE_URL no configurada", allow_module_level=True)

PHONE_ID = "100200300400"


@pytest.fixture(scope="module", autouse=True)
def schema_ready():
    _reset_and_migrate()


@pytest.fixture(autouse=True)
def clean(schema_ready):
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute(
            "truncate public.leads, public.products, public.webhook_events, public.llm_usage_events, "
            "public.budget_reservations, public.outbound_messages, public.contact_leases restart identity cascade"
        )


def _repo():
    pool = create_pool(TEST_DATABASE_URL, max_size=4)
    pool.open(wait=True, timeout=10)
    return OutboundRepository(pool), pool


def _conversation(contact: str) -> uuid.UUID:
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        lead = conn.execute("insert into public.leads (wa_contact_id) values (%s) returning id", (contact,)).fetchone()[0]
        return conn.execute("insert into public.conversations (lead_id, automation_state) values (%s, 'automated') "
                            "returning id", (lead,)).fetchone()[0]


def _outbound(conv, key: str, created: datetime, status: str = "pending", attempts: int = 0,
              last_error: str | None = None) -> uuid.UUID:
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        return conn.execute(
            "insert into public.outbound_messages (idempotency_key, conversation_id, content_text, created_at, "
            "next_attempt_at, delivery_status, attempts, last_error_code, business_phone_id) "
            "values (%s, %s, 'respuesta', %s, %s, %s, %s, %s, %s) returning id",
            (key, conv, created, created, status, attempts, last_error, PHONE_ID),
        ).fetchone()[0]


def _row(outbound_id):
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        return conn.execute("select delivery_status, attempts, lease_owner, external_message_id "
                            "from public.outbound_messages where id = %s", (outbound_id,)).fetchone()


def _mark_sent(outbound_id, wamid: str) -> None:
    """Cierre de un envío exitoso (simulado): el worker habría recibido el identificador de Meta."""
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute("update public.outbound_messages set delivery_status = 'sent', external_message_id = %s, "
                     "sent_at = now(), lease_token = null, lease_owner = null, lease_expires_at = null where id = %s",
                     (wamid, outbound_id))


def test_an_uncertain_send_never_blocks_the_next_replies_and_is_never_resent():
    repo, pool = _repo()
    try:
        conv = _conversation("573001112233")
        t0 = datetime.now(timezone.utc) - timedelta(minutes=5)
        incierta = _outbound(conv, "k-incierta", t0, status="uncertain", attempts=1, last_error="send_uncertain")
        segunda = _outbound(conv, "k-2", t0 + timedelta(seconds=10))
        tercera = _outbound(conv, "k-3", t0 + timedelta(seconds=20))

        # 1) La segunda respuesta avanza aunque la primera siga incierta.
        first_claim = repo.claim(5, "worker-a", 120, uuid.uuid4())
        assert [c.outbound_id for c in first_claim] == [segunda]

        # 2) La tercera espera a que la segunda termine (orden y exclusión dentro de la conversación).
        assert repo.claim(5, "worker-b", 120, uuid.uuid4()) == [], "un envío en curso bloquea los posteriores"
        _mark_sent(segunda, "wamid.SEGUNDA")

        # 3) Con la segunda cerrada, la tercera se reclama.
        third_claim = repo.claim(5, "worker-a", 120, uuid.uuid4())
        assert [c.outbound_id for c in third_claim] == [tercera]
        _mark_sent(tercera, "wamid.TERCERA")

        # 4) Cero reintentos sobre la respuesta incierta: sigue igual y nunca se reclama.
        assert repo.claim(5, "worker-a", 120, uuid.uuid4()) == []
        assert _row(incierta) == ("uncertain", 1, None, None)
    finally:
        pool.close()


def test_two_conversations_are_independent():
    repo, pool = _repo()
    try:
        a = _conversation("573001110001")
        b = _conversation("573001110002")
        t0 = datetime.now(timezone.utc) - timedelta(minutes=5)
        _outbound(a, "a-1", t0, status="uncertain", attempts=1, last_error="send_uncertain")
        _outbound(b, "b-1", t0 + timedelta(seconds=1))
        claimed = repo.claim(5, "worker-a", 120, uuid.uuid4())
        assert len(claimed) == 1, "la conversación B no queda bloqueada por la incierta de A"
    finally:
        pool.close()


def test_an_older_pending_reply_still_blocks_the_newer_one():
    repo, pool = _repo()
    try:
        conv = _conversation("573001110003")
        t0 = datetime.now(timezone.utc) - timedelta(minutes=5)
        anterior = _outbound(conv, "p-1", t0)
        _outbound(conv, "p-2", t0 + timedelta(seconds=5))
        claimed = repo.claim(5, "worker-a", 120, uuid.uuid4())
        assert [c.outbound_id for c in claimed] == [anterior]
    finally:
        pool.close()
