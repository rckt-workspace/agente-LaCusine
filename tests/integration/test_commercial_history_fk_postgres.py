"""Integridad referencial del historial comercial tras la migración 20261105140000.

Borrar una conversación, un producto o un prospecto con historial debe fallar con un error de clave foránea
(23503), no con un intento de UPDATE sobre tablas inmutables, y el historial debe quedar intacto.
PostgreSQL local desechable. Solo datos sintéticos.
"""

from __future__ import annotations

import secrets
import uuid

import psycopg
import pytest
from psycopg import errors

from tests.integration.test_postgres_persistence import TEST_DATABASE_URL, _reset_and_migrate

pytestmark = pytest.mark.integration

if not TEST_DATABASE_URL:
    pytest.skip("TEST_DATABASE_URL no configurada", allow_module_level=True)


@pytest.fixture(scope="module", autouse=True)
def schema_ready():
    _reset_and_migrate()
    yield


@pytest.fixture()
def conn():
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as c:
        yield c


def _lead(conn) -> str:
    wa = "9" + str(secrets.randbelow(10**10)).zfill(10)
    return conn.execute("insert into public.leads (wa_contact_id) values (%s) returning id", (wa,)).fetchone()[0]


def _conversation(conn, lead_id) -> str:
    return conn.execute(
        "insert into public.conversations (lead_id) values (%s) returning id", (lead_id,)
    ).fetchone()[0]


def _product(conn) -> str:
    return conn.execute(
        "insert into public.products (brand, category, reference, source_hash, name) "
        "values ('marca-prueba', 'categoria-prueba', %s, %s, 'producto de prueba') returning id",
        (uuid.uuid4().hex[:20], secrets.token_hex(32)),
    ).fetchone()[0]


def _event(conn, lead_id, conversation_id=None, product_id=None) -> str:
    return conn.execute(
        "insert into public.commercial_events "
        "(occurred_at, event_type, lead_id, conversation_id, product_id, dedupe_key) "
        "values (now(), 'product_interest', %s, %s, %s, %s) returning id",
        (lead_id, conversation_id, product_id, f"prueba-{uuid.uuid4().hex}"),
    ).fetchone()[0]


def _sale(conn, lead_id, conversation_id) -> str:
    return conn.execute(
        "insert into public.sales (sale_ref, lead_id, conversation_id, amount_cop, sold_at, "
        "confirmation_source, confirmed_by, evidence_ref) "
        "values (%s, %s, %s, 1000, now(), 'admin_manual', 'prueba', 'evidencia-prueba') returning id",
        (f"VT-{uuid.uuid4().hex[:12]}", lead_id, conversation_id),
    ).fetchone()[0]


def test_the_three_history_references_are_restrict(conn):
    rows = conn.execute(
        "select conname, confdeltype from pg_constraint where contype = 'f' and conname in "
        "('commercial_events_conversation_id_fkey', 'commercial_events_product_id_fkey', "
        "'sales_conversation_id_fkey') order by conname"
    ).fetchall()
    assert rows == [
        ("commercial_events_conversation_id_fkey", "r"),
        ("commercial_events_product_id_fkey", "r"),
        ("sales_conversation_id_fkey", "r"),
    ]


def test_conversation_with_commercial_event_cannot_be_deleted_and_history_stays(conn):
    lead = _lead(conn)
    conv = _conversation(conn, lead)
    event = _event(conn, lead, conversation_id=conv)
    with pytest.raises((errors.ForeignKeyViolation, errors.RestrictViolation)):
        conn.execute("delete from public.conversations where id = %s", (conv,))
    assert conn.execute(
        "select conversation_id from public.commercial_events where id = %s", (event,)
    ).fetchone()[0] == conv


def test_conversation_with_sale_cannot_be_deleted_and_sale_stays(conn):
    lead = _lead(conn)
    conv = _conversation(conn, lead)
    sale = _sale(conn, lead, conv)
    with pytest.raises((errors.ForeignKeyViolation, errors.RestrictViolation)):
        conn.execute("delete from public.conversations where id = %s", (conv,))
    assert conn.execute("select conversation_id from public.sales where id = %s", (sale,)).fetchone()[0] == conv


def test_product_with_commercial_event_cannot_be_deleted_and_history_stays(conn):
    lead = _lead(conn)
    product = _product(conn)
    event = _event(conn, lead, product_id=product)
    with pytest.raises((errors.ForeignKeyViolation, errors.RestrictViolation)):
        conn.execute("delete from public.products where id = %s", (product,))
    assert conn.execute(
        "select product_id from public.commercial_events where id = %s", (event,)
    ).fetchone()[0] == product


def test_conversation_without_commercial_history_can_still_be_deleted(conn):
    lead = _lead(conn)
    conv = _conversation(conn, lead)
    conn.execute("delete from public.conversations where id = %s", (conv,))
    assert conn.execute("select count(*) from public.conversations where id = %s", (conv,)).fetchone()[0] == 0


def test_lead_with_commercial_history_still_cannot_be_deleted(conn):
    # Comportamiento preexistente (lead_id RESTRICT, migración 20261105110000): se fija aquí para que
    # cualquier cambio sea deliberado. Ver docs/respaldo-y-restauracion.md, "Privacidad y borrado".
    lead = _lead(conn)
    conv = _conversation(conn, lead)
    _event(conn, lead, conversation_id=conv)
    with pytest.raises((errors.ForeignKeyViolation, errors.RestrictViolation)):
        conn.execute("delete from public.leads where id = %s", (lead,))


def test_commercial_history_is_still_append_only(conn):
    lead = _lead(conn)
    event = _event(conn, lead)
    with pytest.raises(psycopg.Error, match="historial comercial"):
        conn.execute("update public.commercial_events set category = 'x' where id = %s", (event,))
    with pytest.raises(psycopg.Error, match="historial comercial"):
        conn.execute("delete from public.commercial_events where id = %s", (event,))
