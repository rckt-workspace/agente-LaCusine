"""Conteo de mensajes entrantes por conversación contra PostgreSQL local desechable."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from app.db.pool import create_pool
from app.repositories.crm import PostgresMessageRepository
from tests.integration.test_postgres_persistence import TEST_DATABASE_URL, _reset_and_migrate

if not TEST_DATABASE_URL:
    pytest.skip("TEST_DATABASE_URL no configurada", allow_module_level=True)


@pytest.fixture(scope="module", autouse=True)
def schema_ready():
    _reset_and_migrate()


def test_inbound_count_is_per_conversation_and_respects_the_window():
    now = datetime.now(timezone.utc)
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        lead_a = conn.execute("insert into public.leads (wa_contact_id) values ('573001110001') returning id").fetchone()[0]
        lead_b = conn.execute("insert into public.leads (wa_contact_id) values ('573001110002') returning id").fetchone()[0]
        conv_a = conn.execute("insert into public.conversations (lead_id) values (%s) returning id", (lead_a,)).fetchone()[0]
        conv_b = conn.execute("insert into public.conversations (lead_id) values (%s) returning id", (lead_b,)).fetchone()[0]
        for index, received in enumerate([now - timedelta(minutes=5), now - timedelta(minutes=50),
                                          now - timedelta(hours=3)]):
            conn.execute(
                """insert into public.messages (conversation_id, direction, wa_message_id, message_type, received_at)
                   values (%s, 'inbound', %s, 'text', %s)""",
                (conv_a, f"wamid.RL-A{index}-{uuid.uuid4().hex}", received),
            )
        conn.execute(
            """insert into public.messages (conversation_id, direction, wa_message_id, message_type, received_at)
               values (%s, 'inbound', %s, 'text', %s)""",
            (conv_b, f"wamid.RL-B-{uuid.uuid4().hex}", now - timedelta(minutes=1)),
        )
    pool = create_pool(TEST_DATABASE_URL, min_size=1, max_size=2)
    pool.open(wait=True, timeout=10)
    try:
        repo = PostgresMessageRepository(pool)
        assert repo.count_inbound_since(conv_a, now - timedelta(hours=1)) == 2
        assert repo.count_inbound_since(conv_a, now - timedelta(days=1)) == 3
        assert repo.count_inbound_since(conv_b, now - timedelta(hours=1)) == 1
    finally:
        pool.close()
