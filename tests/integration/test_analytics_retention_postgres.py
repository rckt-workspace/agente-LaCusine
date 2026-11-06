"""Retención sin pérdida de métricas: la limpieza consolida conteos antes de borrar. Solo PostgreSQL local desechable."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from tests.integration.test_postgres_persistence import TEST_DATABASE_URL, _reset_and_migrate

if not TEST_DATABASE_URL:
    pytest.skip("TEST_DATABASE_URL no configurada", allow_module_level=True)


@pytest.fixture(scope="module", autouse=True)
def schema_ready():
    _reset_and_migrate()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _seed_conversation(conn) -> uuid.UUID:
    lead_id = conn.execute(
        "insert into public.leads (wa_contact_id) values (%s) returning id", (f"5730{uuid.uuid4().int % 10**7:07d}",)
    ).fetchone()[0]
    return conn.execute(
        "insert into public.conversations (lead_id) values (%s) returning id", (lead_id,)
    ).fetchone()[0]


def _seed_webhook(conn, status: str, received: datetime, expires: datetime) -> None:
    ext = f"wamid.{uuid.uuid4().hex}"
    processed_at = received if status in ("processed", "ignored", "dead_letter") else None
    conn.execute(
        """insert into public.webhook_events
             (external_event_id, contact_wa_id, payload_sha256, processing_status, received_at, expires_at, processed_at)
           values (%s, %s, %s, %s, %s, %s, %s)""",
        (ext, "573001112233", _sha(ext), status, received, expires, processed_at),
    )


def _seed_outbound(conn, conversation_id, status: str, created: datetime, expires: datetime) -> None:
    conn.execute(
        """insert into public.outbound_messages
             (idempotency_key, conversation_id, content_text, delivery_status, created_at, expires_at)
           values (%s, %s, %s, %s, %s, %s)""",
        (f"k-{uuid.uuid4().hex}", conversation_id, "respuesta sintética", status, created, expires),
    )


def _count(conn, sql: str, *args) -> int:
    return int(conn.execute(sql, args).fetchone()[0])


def test_purge_keeps_weekly_counts_after_deleting_the_rows():
    now = datetime.now(timezone.utc)
    past = now - timedelta(days=30)
    # 4 de noviembre de 2026 a las 23:30 en Bogotá (= 5 de noviembre 04:30 UTC): cuenta en el día 2026-11-04.
    late_night_bogota = datetime(2026, 11, 5, 4, 30, tzinfo=timezone.utc)
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conv = _seed_conversation(conn)
        _seed_webhook(conn, "processed", late_night_bogota, past)
        _seed_webhook(conn, "processed", late_night_bogota, past)
        _seed_webhook(conn, "ignored", late_night_bogota, past)
        _seed_outbound(conn, conv, "sent", late_night_bogota, past)
        _seed_outbound(conn, conv, "delivered", late_night_bogota, past)

        result = conn.execute("select * from public.purge_expired_personal_data(100)").fetchone()
        assert result[0] == 3 and result[2] == 2

        assert _count(conn, "select count(*) from public.webhook_events") == 0
        assert _count(conn, "select count(*) from public.outbound_messages") == 0
        rolled = dict(conn.execute(
            "select metric, value from public.analytics_daily_counts where day = '2026-11-04' order by metric"
        ).fetchall())
        assert rolled == {"outbound:delivered": 1, "outbound:sent": 1, "webhook_events:ignored": 1,
                          "webhook_events:processed": 2}


def test_a_second_purge_does_not_double_count_rows_already_consolidated():
    now = datetime.now(timezone.utc)
    past = now - timedelta(days=30)
    created = datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conv = _seed_conversation(conn)
        _seed_outbound(conn, conv, "sent", created, past)
        conn.execute("select * from public.purge_expired_personal_data(100)")
        before = _count(conn, "select value from public.analytics_daily_counts where metric = 'outbound:sent' and day = '2026-11-03'")
        again = conn.execute("select * from public.purge_expired_personal_data(100)").fetchone()
        after = _count(conn, "select value from public.analytics_daily_counts where metric = 'outbound:sent' and day = '2026-11-03'")
        assert again[2] == 0 and before == after == 1


def test_pending_and_in_process_work_is_never_consolidated_or_deleted():
    now = datetime.now(timezone.utc)
    past = now - timedelta(days=30)
    created = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conv = _seed_conversation(conn)
        _seed_outbound(conn, conv, "pending", created, past)
        _seed_webhook(conn, "queued", created, past)
        conn.execute("select * from public.purge_expired_personal_data(100)")
        assert _count(conn, "select count(*) from public.outbound_messages where delivery_status = 'pending'") == 1
        assert _count(conn, "select count(*) from public.webhook_events where processing_status = 'queued'") == 1
        assert _count(conn, "select count(*) from public.analytics_daily_counts where metric like %s", "%pending%") == 0


def test_message_rows_are_kept_when_only_their_text_is_cleared():
    past = datetime.now(timezone.utc) - timedelta(days=30)
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conv = _seed_conversation(conn)
        conn.execute(
            """insert into public.messages
                 (conversation_id, direction, wa_message_id, message_type, received_at, content_text, content_expires_at)
               values (%s, 'inbound', %s, 'text', %s, 'texto personal sintético', %s)""",
            (conv, f"wamid.{uuid.uuid4().hex}", past, past),
        )
        result = conn.execute("select * from public.purge_expired_personal_data(100)").fetchone()
        assert result[1] == 1
        assert _count(conn, "select count(*) from public.messages") == 1
        assert _count(conn, "select count(*) from public.messages where content_text is null") == 1
