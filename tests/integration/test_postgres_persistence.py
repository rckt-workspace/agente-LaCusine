"""Pruebas de integración contra PostgreSQL real.

Requisitos de seguridad: ver tests/integration/db_guard.py (APP_ENV=test, confirmación explícita,
loopback, base lacusine_test exacta). Si TEST_DATABASE_URL está definida pero es insegura, el módulo
FALLA; no se omite. Sin TEST_DATABASE_URL, las pruebas se omiten con el motivo indicado.
"""

from __future__ import annotations

import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
from psycopg import conninfo
import pytest
from tests.integration.db_guard import UnsafeTestDatabaseError, validate_test_database_url
from psycopg.errors import CheckViolation, ForeignKeyViolation

from app.db.pool import create_pool
from app.repositories.catalog import PostgresCatalogRepository
from app.repositories.crm import (
    PostgresConversationRepository,
    PostgresHandoffRepository,
    PostgresLeadRepository,
    PostgresMessageRepository,
)
from app.repositories.errors import StorageError
from app.repositories.webhook_events import (
    PostgresWebhookEventRepository,
    RegisterOutcome,
    WebhookEventRecord,
)
from app.services.catalog_import import import_catalog

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "supabase" / "migrations"
RLS_TABLES = ("leads", "conversations", "messages", "webhook_events", "products", "human_handoffs", "llm_usage_events")
HASH = "a" * 64

pytestmark = pytest.mark.integration

if not TEST_DATABASE_URL:
    pytest.skip("TEST_DATABASE_URL no configurada: pruebas de integración omitidas", allow_module_level=True)


try:
    validate_test_database_url(TEST_DATABASE_URL, dict(os.environ))
except UnsafeTestDatabaseError as exc:
    pytest.fail(f"Pruebas destructivas bloqueadas por seguridad: {exc}", pytrace=False)


def _reset_and_migrate() -> None:
    """Reinicia el esquema público y aplica las migraciones en orden, cada una en una transacción.

    Simula los roles de Supabase (anon, authenticated) con privilegios por defecto amplios,
    para verificar que las migraciones los retiran.
    """
    validate_test_database_url(TEST_DATABASE_URL, dict(os.environ))  # defensa en profundidad
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute("drop schema if exists public cascade")
        conn.execute("create schema public")
        conn.execute("grant usage on schema public to public")
        for role in ("anon", "authenticated"):
            conn.execute(
                f"do $$ begin if not exists (select 1 from pg_roles where rolname = '{role}') "
                f"then create role {role} nologin; end if; end $$"
            )
        # Privilegios por defecto que Supabase aplica a objetos nuevos de public. Deben existir ANTES de
        # las migraciones para que estas tengan algo que revocar.
        conn.execute(
            "alter default privileges in schema public grant all on tables to anon, authenticated"
        )
        conn.execute(
            "alter default privileges in schema public grant all on functions to anon, authenticated"
        )
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            with conn.transaction():
                conn.execute(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module", autouse=True)
def migrated_database():
    _reset_and_migrate()


@pytest.fixture
def pool():
    p = create_pool(TEST_DATABASE_URL)
    p.open(wait=True, timeout=10)
    yield p
    p.close()


@pytest.fixture(autouse=True)
def clean_tables(migrated_database):
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute("truncate public.leads, public.products, public.webhook_events restart identity cascade")
    yield


def _record(event_id: str = "wamid.INT1", payload: dict | None = None, sha: str = HASH) -> WebhookEventRecord:
    return WebhookEventRecord(
        external_event_id=event_id,
        contact_wa_id="573001112233",
        payload=payload or {"id": event_id, "type": "text"},
        payload_sha256=sha,
    )


def _count(sql: str, params: tuple = ()) -> int:
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        return conn.execute(sql, params).fetchone()[0]


# --- Seguridad: RLS y privilegios ---

def test_all_public_tables_have_row_level_security_enabled():
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        rows = conn.execute(
            "select relname, relrowsecurity from pg_class c join pg_namespace n on n.oid = c.relnamespace "
            "where n.nspname = 'public' and relkind = 'r' and relname = any(%s)",
            (list(RLS_TABLES),),
        ).fetchall()
    assert sorted(name for name, _ in rows) == sorted(RLS_TABLES)
    assert all(enabled for _, enabled in rows)


def test_anon_and_authenticated_cannot_access_tables_or_purge_function():
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        for role in ("anon", "authenticated"):
            for table in RLS_TABLES:
                granted = conn.execute(
                    "select has_table_privilege(%s, %s, 'SELECT') or has_table_privilege(%s, %s, 'INSERT')",
                    (role, f"public.{table}", role, f"public.{table}"),
                ).fetchone()[0]
                assert granted is False, f"{role} conserva privilegios sobre {table}"
            executable = conn.execute(
                "select has_function_privilege(%s, 'public.purge_expired_personal_data(integer)', 'EXECUTE')",
                (role,),
            ).fetchone()[0]
            assert executable is False


# --- Idempotencia de eventos entrantes ---

def test_new_event_is_stored_once_and_duplicate_is_detected(pool):
    repo = PostgresWebhookEventRepository(pool, retention_days=7)
    assert repo.register(_record()) is RegisterOutcome.NEW
    assert repo.register(_record()) is RegisterOutcome.DUPLICATE
    assert _count("select count(*) from public.webhook_events") == 1


def test_concurrent_registration_of_same_event_inserts_exactly_once(pool):
    repo = PostgresWebhookEventRepository(pool, retention_days=7)
    barrier = threading.Barrier(20)
    outcomes: list[RegisterOutcome] = []
    lock = threading.Lock()
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait()
            outcome = repo.register(_record("wamid.RACE"))
            with lock:
                outcomes.append(outcome)
        except BaseException as exc:  # noqa: BLE001 - se reporta en el hilo principal
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert outcomes.count(RegisterOutcome.NEW) == 1
    assert outcomes.count(RegisterOutcome.DUPLICATE) == 19
    assert _count("select count(*) from public.webhook_events where external_event_id = 'wamid.RACE'") == 1


def test_conflicting_payload_for_same_event_is_detected_without_overwrite(pool):
    repo = PostgresWebhookEventRepository(pool, retention_days=7)
    repo.register(_record("wamid.CONF", payload={"text": "original"}, sha="b" * 64))
    outcome = repo.register(_record("wamid.CONF", payload={"text": "alterado"}, sha="c" * 64))
    assert outcome is RegisterOutcome.CONFLICT
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        stored = conn.execute(
            "select payload->>'text' from public.webhook_events where external_event_id = 'wamid.CONF'"
        ).fetchone()[0]
    assert stored == "original"


def test_event_survives_application_restart(pool):
    PostgresWebhookEventRepository(pool, retention_days=7).register(_record("wamid.RESTART"))
    pool.close()

    restarted = create_pool(TEST_DATABASE_URL)
    restarted.open(wait=True, timeout=10)
    try:
        outcome = PostgresWebhookEventRepository(restarted, retention_days=7).register(_record("wamid.RESTART"))
    finally:
        restarted.close()
    assert outcome is RegisterOutcome.DUPLICATE


def test_event_is_stored_with_expiry_and_received_status(pool):
    PostgresWebhookEventRepository(pool, retention_days=3).register(_record("wamid.EXP"))
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        status, days_left = conn.execute(
            "select processing_status, extract(epoch from (expires_at - received_at)) / 86400 "
            "from public.webhook_events where external_event_id = 'wamid.EXP'"
        ).fetchone()
    assert status == "received"
    assert round(float(days_left)) == 3


def test_webhook_event_check_constraints_reject_invalid_values(pool):
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        with pytest.raises(CheckViolation):
            conn.execute(
                "insert into public.webhook_events (external_event_id, payload_sha256, expires_at, processing_status) "
                "values ('wamid.BAD1', %s, now(), 'bogus')",
                (HASH,),
            )
        with pytest.raises(CheckViolation):
            conn.execute(
                "insert into public.webhook_events (external_event_id, payload_sha256, expires_at, contact_wa_id) "
                "values ('wamid.BAD2', %s, now(), '+57 300')",
                (HASH,),
            )
        with pytest.raises(CheckViolation):
            conn.execute(
                "insert into public.webhook_events (external_event_id, payload_sha256, expires_at, processing_status) "
                "values ('wamid.BAD3', %s, now(), 'processed')",
                (HASH,),
            )


FAKE_SECRET = "contrasena-ficticia-para-prueba"


def test_storage_failure_does_not_leak_connection_details():
    unreachable = conninfo.make_conninfo(
        host="127.0.0.1", port=1, dbname="lacusine_test", user="lacusine_user", password=FAKE_SECRET
    )
    bad_pool = create_pool(unreachable)
    bad_pool.open(wait=False)
    try:
        with pytest.raises(StorageError) as excinfo:
            PostgresWebhookEventRepository(bad_pool, retention_days=7).register(_record("wamid.DOWN"))
    finally:
        bad_pool.close()
    message = str(excinfo.value)
    assert FAKE_SECRET not in message
    assert "127.0.0.1" not in message
    assert message == "webhook_event_write_failed"


# --- Retención ---

def test_purge_deletes_expired_events_and_clears_expired_message_content(pool):
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute(
            "insert into public.webhook_events (external_event_id, payload_sha256, expires_at) "
            "values ('wamid.OLD', %s, now() - interval '1 day'), ('wamid.NEW', %s, now() + interval '1 day')",
            (HASH, HASH),
        )
        lead_id = conn.execute(
            "insert into public.leads (wa_contact_id) values ('573005550001') returning id"
        ).fetchone()[0]
        conv_id = conn.execute(
            "insert into public.conversations (lead_id) values (%s) returning id", (lead_id,)
        ).fetchone()[0]
        conn.execute(
            "insert into public.messages (conversation_id, direction, message_type, received_at, "
            "content_text, content_expires_at) values (%s, 'inbound', 'text', now(), 'vencido', now() - interval '1 hour')",
            (conv_id,),
        )
        deleted, cleared = conn.execute("select * from public.purge_expired_personal_data(100)").fetchone()
        remaining = conn.execute("select external_event_id from public.webhook_events").fetchall()
        content = conn.execute("select content_text from public.messages").fetchone()[0]

    assert (deleted, cleared) == (1, 1)
    assert remaining == [("wamid.NEW",)]
    assert content is None


def test_purge_rejects_invalid_batch_size():
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("select * from public.purge_expired_personal_data(0)")


# --- CRM: prospectos, conversaciones, mensajes, solicitudes ---

def test_lead_get_or_create_returns_same_id_under_concurrency(pool):
    repo = PostgresLeadRepository(pool)
    barrier = threading.Barrier(10)
    ids: list[uuid.UUID] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        lead_id = repo.get_or_create("573004440001")
        with lock:
            ids.append(lead_id)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(set(ids)) == 1
    assert _count("select count(*) from public.leads where wa_contact_id = '573004440001'") == 1


def test_only_one_open_conversation_per_lead(pool):
    leads = PostgresLeadRepository(pool)
    conversations = PostgresConversationRepository(pool)
    lead_id = leads.get_or_create("573004440002")

    first = conversations.get_or_open(lead_id)
    second = conversations.get_or_open(lead_id)
    assert first == second

    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute("update public.conversations set status = 'closed', closed_at = now() where id = %s", (first,))
    reopened = conversations.get_or_open(lead_id)
    assert reopened != first


def test_closed_conversation_requires_closed_at():
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        lead_id = conn.execute(
            "insert into public.leads (wa_contact_id) values ('573004440003') returning id"
        ).fetchone()[0]
        conv_id = conn.execute(
            "insert into public.conversations (lead_id) values (%s) returning id", (lead_id,)
        ).fetchone()[0]
        with pytest.raises(CheckViolation):
            conn.execute("update public.conversations set status = 'closed' where id = %s", (conv_id,))


def test_inbound_message_wamid_is_recorded_once(pool):
    leads = PostgresLeadRepository(pool)
    conversations = PostgresConversationRepository(pool)
    messages = PostgresMessageRepository(pool)
    conv_id = conversations.get_or_open(leads.get_or_create("573004440004"))
    now = datetime.now(timezone.utc)
    expiry = now + timedelta(days=7)

    assert messages.record_inbound(conv_id, "wamid.MSG1", "text", now, "hola", expiry) is True
    assert messages.record_inbound(conv_id, "wamid.MSG1", "text", now, "hola", expiry) is False
    assert _count("select count(*) from public.messages where wa_message_id = 'wamid.MSG1'") == 1


def test_message_content_requires_expiry(pool):
    messages = PostgresMessageRepository(pool)
    with pytest.raises(ValueError):
        messages.record_inbound(uuid.uuid4(), "wamid.NOEXP", "text", datetime.now(timezone.utc), "texto", None)


def test_message_requires_existing_conversation(pool):
    messages = PostgresMessageRepository(pool)
    with pytest.raises(StorageError):
        messages.record_inbound(uuid.uuid4(), "wamid.ORPHAN", "text", datetime.now(timezone.utc))


def test_handoff_is_single_active_per_conversation(pool):
    leads = PostgresLeadRepository(pool)
    conversations = PostgresConversationRepository(pool)
    handoffs = PostgresHandoffRepository(pool)
    conv_id = conversations.get_or_open(leads.get_or_create("573004440005"))

    first = handoffs.open(conv_id, "quote_request", priority="high")
    assert handoffs.open(conv_id, "purchase_intent") == first

    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute(
            "update public.human_handoffs set status = 'resolved', closed_at = now() where id = %s", (first,)
        )
    assert handoffs.open(conv_id, "complaint") != first


def test_handoff_rejects_unknown_reason_before_writing(pool):
    handoffs = PostgresHandoffRepository(pool)
    with pytest.raises(ValueError):
        handoffs.open(uuid.uuid4(), "unknown_reason")


def test_deleting_a_lead_erases_its_conversations_messages_and_handoffs(pool):
    leads = PostgresLeadRepository(pool)
    conversations = PostgresConversationRepository(pool)
    messages = PostgresMessageRepository(pool)
    handoffs = PostgresHandoffRepository(pool)
    lead_id = leads.get_or_create("573004440006")
    conv_id = conversations.get_or_open(lead_id)
    now = datetime.now(timezone.utc)
    messages.record_inbound(conv_id, "wamid.ERASE", "text", now, "dato", now + timedelta(days=1))
    handoffs.open(conv_id, "other")

    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute("delete from public.leads where id = %s", (lead_id,))
    assert _count("select count(*) from public.messages") == 0
    assert _count("select count(*) from public.conversations") == 0
    assert _count("select count(*) from public.human_handoffs") == 0


def test_message_cannot_reference_missing_conversation_at_database_level():
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        with pytest.raises(ForeignKeyViolation):
            conn.execute(
                "insert into public.messages (conversation_id, direction, received_at) "
                "values (%s, 'inbound', now())",
                (uuid.uuid4(),),
            )


# --- Catálogo ---

CATALOG_CSV = (
    "referencia,marca,categoria,descripcion,precio_cop,disponibilidad,fecha_disponibilidad,enlace\n"
    "TEST-SKU-1,MarcaPrueba,Nevera,Fixture sintético,1299000,Disponible según fuente,2026-10-27,https://example.com/1\n"
    "TEST-SKU-2,MarcaPrueba,Horno,Fixture sintético 2,,,,\n"
)


def test_catalog_import_is_repeatable_without_duplicates(pool):
    import io

    repo = PostgresCatalogRepository(pool)
    first = import_catalog(io.StringIO(CATALOG_CSV), repo)
    second = import_catalog(io.StringIO(CATALOG_CSV), repo)

    assert (first.inserted, first.updated, first.unchanged) == (2, 0, 0)
    assert (second.inserted, second.updated, second.unchanged) == (0, 0, 2)
    assert _count("select count(*) from public.products") == 2


def test_catalog_changed_row_is_updated_in_place(pool):
    import io

    repo = PostgresCatalogRepository(pool)
    import_catalog(io.StringIO(CATALOG_CSV), repo)
    changed = CATALOG_CSV.replace("1299000", "1350000")
    report = import_catalog(io.StringIO(changed), repo)

    assert (report.inserted, report.updated, report.unchanged) == (0, 1, 1)
    assert _count("select count(*) from public.products") == 2
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        price, reference_flag = conn.execute(
            "select price_cop, price_is_reference from public.products where reference = 'TEST-SKU-1'"
        ).fetchone()
    assert str(price) == "1350000.00"
    assert reference_flag is True


def test_catalog_price_keeps_two_decimal_precision(pool):
    import io

    csv_text = CATALOG_CSV.replace("1299000,", "1299000.5,")
    import_catalog(io.StringIO(csv_text), PostgresCatalogRepository(pool))
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        price = conn.execute("select price_cop from public.products where reference = 'TEST-SKU-1'").fetchone()[0]
    assert str(price) == "1299000.50"
