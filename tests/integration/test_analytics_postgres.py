"""Analítica semanal contra PostgreSQL local desechable: totales estables antes y después de la retención."""

from __future__ import annotations

import hashlib
import uuid
from datetime import date, datetime, timedelta, timezone

import psycopg
import pytest

from app.db.pool import create_pool
from app.repositories.analytics import PostgresAnalyticsRepository
from app.services.analytics import week_containing
from app.services.analytics_report import build_weekly_report
from tests.integration.test_postgres_persistence import TEST_DATABASE_URL, _reset_and_migrate

if not TEST_DATABASE_URL:
    pytest.skip("TEST_DATABASE_URL no configurada", allow_module_level=True)

WEEK = week_containing(date(2026, 11, 5))  # lunes 2 a lunes 9 de noviembre, hora de Bogotá


@pytest.fixture(scope="module", autouse=True)
def schema_ready():
    _reset_and_migrate()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _lead_and_conversation(conn, started: datetime) -> uuid.UUID:
    lead = conn.execute("insert into public.leads (wa_contact_id) values (%s) returning id",
                        (f"5731{uuid.uuid4().int % 10**7:07d}",)).fetchone()[0]
    return conn.execute("insert into public.conversations (lead_id, started_at) values (%s, %s) returning id",
                        (lead, started)).fetchone()[0]


def _seed_week(conn) -> None:
    inside = datetime(2026, 11, 4, 15, 0, tzinfo=timezone.utc)
    monday_midnight_bogota = datetime(2026, 11, 9, 5, 0, tzinfo=timezone.utc)  # pertenece a la semana siguiente
    past = datetime.now(timezone.utc) - timedelta(days=30)  # vencido: la retención puede consolidarlo
    conv = _lead_and_conversation(conn, inside)
    for index in range(3):
        conn.execute(
            """insert into public.messages (conversation_id, direction, wa_message_id, message_type, received_at,
                                              content_text, content_expires_at)
               values (%s, 'inbound', %s, 'text', %s, 'texto sintético', %s)""",
            (conv, f"wamid.IN{index}-{uuid.uuid4().hex}", inside, past),
        )
    conn.execute(
        """insert into public.messages (conversation_id, direction, wa_message_id, message_type, received_at)
           values (%s, 'inbound', %s, 'text', %s)""",
        (conv, f"wamid.EDGE-{uuid.uuid4().hex}", monday_midnight_bogota),
    )
    for status in ("sent", "delivered", "read", "failed"):
        conn.execute(
            """insert into public.outbound_messages (idempotency_key, conversation_id, content_text, delivery_status,
                                                      created_at, expires_at)
               values (%s, %s, 'respuesta sintética', %s, %s, %s)""",
            (f"k-{uuid.uuid4().hex}", conv, status, inside, past),
        )
    for index, status in enumerate(("succeeded", "succeeded", "failed")):
        conn.execute(
            """insert into public.llm_usage_events (call_id, conversation_id, provider, model, purpose, status,
                                                     error_code, input_tokens, output_tokens, estimated_cost_usd,
                                                     created_at)
               values (%s, %s, 'anthropic', 'claude-sonnet-5-5', 'commercial_reply', %s, %s, 4000, 300, 0.01, %s)""",
            (f"wamid.LLM{index}-{uuid.uuid4().hex}", conv, status, None if status == "succeeded" else "timeout", inside),
        )


def _report(pool) -> dict:
    counts = PostgresAnalyticsRepository(pool).weekly_counts(WEEK)
    return build_weekly_report(counts, WEEK)


def test_weekly_totals_are_identical_before_and_after_the_retention_purge():
    pool = create_pool(TEST_DATABASE_URL, min_size=1, max_size=2)
    pool.open(wait=True, timeout=10)
    try:
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
            _seed_week(conn)
        before = _report(pool)
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
            purged = conn.execute("select * from public.purge_expired_personal_data(500)").fetchone()
            assert purged[2] == 4  # las respuestas cerradas se borraron de la tabla viva
            remaining = conn.execute("select count(*) from public.outbound_messages").fetchone()[0]
            assert remaining == 0
        after = _report(pool)
    finally:
        pool.close()

    assert before["actividad"] == after["actividad"]
    assert before["operacion_ia"] == after["operacion_ia"]
    assert after["actividad"]["respuestas_aceptadas_por_meta"] == 3       # sent + delivered + read
    assert after["actividad"]["respuestas_entregadas_o_leidas"] == 2      # delivered + read
    assert after["actividad"]["respuestas_fallidas"] == 1


def test_a_message_at_monday_midnight_bogota_belongs_to_the_following_week():
    pool = create_pool(TEST_DATABASE_URL, min_size=1, max_size=2)
    pool.open(wait=True, timeout=10)
    try:
        report = _report(pool)
        next_week = PostgresAnalyticsRepository(pool).weekly_counts(week_containing(date(2026, 11, 9)))
    finally:
        pool.close()
    assert report["actividad"]["mensajes_entrantes"] == 3
    assert next_week["inbound_messages"] == 1


def test_sales_stay_unavailable_even_with_data_present():
    pool = create_pool(TEST_DATABASE_URL, min_size=1, max_size=2)
    pool.open(wait=True, timeout=10)
    try:
        report = _report(pool)
    finally:
        pool.close()
    assert report["ventas"]["estado"] == "sin_datos"
    assert report["ventas"]["ventas_confirmadas"] is None
