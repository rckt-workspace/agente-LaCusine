"""Eventos comerciales y ventas confirmadas contra PostgreSQL local desechable."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import psycopg
import pytest

from app.db.pool import create_pool
from app.repositories.commercial import PostgresCommercialRepository, SaleConflictError, SaleRecord, SaleStateError
from app.repositories.errors import StorageError
from tests.integration.test_postgres_persistence import TEST_DATABASE_URL, _reset_and_migrate

if not TEST_DATABASE_URL:
    pytest.skip("TEST_DATABASE_URL no configurada", allow_module_level=True)

NOW = datetime.now(timezone.utc).replace(microsecond=0)


@pytest.fixture(scope="module", autouse=True)
def schema_ready():
    _reset_and_migrate()


@pytest.fixture
def repo():
    pool = create_pool(TEST_DATABASE_URL, min_size=1, max_size=2)
    pool.open(wait=True, timeout=10)
    yield PostgresCommercialRepository(pool)
    pool.close()


def _seed(conn):
    lead = conn.execute("insert into public.leads (wa_contact_id) values (%s) returning id",
                        (f"5730{uuid.uuid4().int % 10**7:07d}",)).fetchone()[0]
    product = conn.execute(
        """insert into public.products (reference, brand, category, price_cop, source_hash)
           values (%s, 'Jura', 'Cafeteras automáticas', 2990000, %s) returning id""",
        (f"REF-{uuid.uuid4().hex[:8]}", uuid.uuid4().hex + uuid.uuid4().hex),
    ).fetchone()[0]
    return lead, product


def _record(lead, **overrides):
    base = dict(sale_ref=f"VTA-{uuid.uuid4().hex[:10]}", lead_id=lead, amount_cop=2_990_000,
                sold_at=NOW - timedelta(hours=2), confirmation_source="admin_manual", confirmed_by="ana",
                evidence_ref="factura FV-100")
    base.update(overrides)
    return SaleRecord(**base)


def test_confirmed_sale_is_created_once_with_products_and_a_confirmation_event(repo):
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        lead, product = _seed(conn)
    sale = _record(lead)
    assert repo.confirm_sale(sale, {product: 2}) == "created"
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        assert conn.execute("select count(*) from public.sales where sale_ref = %s", (sale.sale_ref,)).fetchone()[0] == 1
        assert conn.execute("select quantity from public.sale_products sp join public.sales s on s.id = sp.sale_id "
                            "where s.sale_ref = %s", (sale.sale_ref,)).fetchone()[0] == 2
        assert conn.execute("select status from public.sale_status_events e join public.sales s on s.id = e.sale_id "
                            "where s.sale_ref = %s", (sale.sale_ref,)).fetchone()[0] == "confirmed"


def test_the_same_sale_sent_twice_is_a_harmless_duplicate(repo):
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        lead, product = _seed(conn)
    sale = _record(lead)
    assert repo.confirm_sale(sale, {product: 1}) == "created"
    assert repo.confirm_sale(sale, {product: 1}) == "duplicate"
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        assert conn.execute("select count(*) from public.sales where sale_ref = %s", (sale.sale_ref,)).fetchone()[0] == 1


def test_the_same_reference_with_different_data_is_a_conflict_and_nothing_is_overwritten(repo):
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        lead, product = _seed(conn)
    sale = _record(lead)
    repo.confirm_sale(sale, {product: 1})
    with pytest.raises(SaleConflictError):
        repo.confirm_sale(_record(lead, sale_ref=sale.sale_ref, amount_cop=1), {product: 1})
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        assert conn.execute("select amount_cop from public.sales where sale_ref = %s", (sale.sale_ref,)).fetchone()[0] == 2_990_000


def test_cancellation_adds_an_event_and_cannot_be_repeated(repo):
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        lead, product = _seed(conn)
    sale = _record(lead)
    repo.confirm_sale(sale, {product: 1})
    repo.cancel_sale(sale.sale_ref, reason="devolución acordada", actor="ana")
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        statuses = [r[0] for r in conn.execute(
            "select e.status from public.sale_status_events e join public.sales s on s.id = e.sale_id "
            "where s.sale_ref = %s order by e.occurred_at, e.id", (sale.sale_ref,)).fetchall()]
        assert statuses == ["confirmed", "cancelled"]  # historial en orden; nada se borra
    with pytest.raises(SaleStateError):
        repo.cancel_sale(sale.sale_ref, reason="otra vez", actor="ana")
    with pytest.raises(SaleStateError):
        repo.cancel_sale("VTA-INEXISTENTE", reason="x", actor="ana")


def test_a_product_that_is_not_in_the_catalog_is_refused(repo):
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        lead, _ = _seed(conn)
    with pytest.raises(StorageError):
        repo.confirm_sale(_record(lead), {uuid.uuid4(): 1})


def test_commercial_events_are_deduplicated_by_key(repo):
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        lead, _ = _seed(conn)
    key = f"intent:wamid.{uuid.uuid4().hex}"
    assert repo.record_event(event_type="purchase_intent", occurred_at=NOW, lead_id=lead, dedupe_key=key) is True
    assert repo.record_event(event_type="purchase_intent", occurred_at=NOW, lead_id=lead, dedupe_key=key) is False


def test_history_tables_refuse_updates_and_deletes(repo):
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        lead, _ = _seed(conn)
    key = f"quote:wamid.{uuid.uuid4().hex}"
    repo.record_event(event_type="quote_request", occurred_at=NOW, lead_id=lead, dedupe_key=key)
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("update public.commercial_events set event_type = 'purchase_intent' where dedupe_key = %s", (key,))
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute("delete from public.commercial_events where dedupe_key = %s", (key,))


def _window_report(pool):
    from app.repositories.analytics import PostgresAnalyticsRepository
    from app.services.analytics import local_day, week_containing
    from app.services.analytics_report import build_weekly_report
    window = week_containing(local_day(datetime.now(timezone.utc)))
    return build_weekly_report(PostgresAnalyticsRepository(pool).weekly_counts(window), window)


def test_weekly_report_counts_confirmed_sales_and_keeps_intent_separate(repo):
    from app.db.pool import create_pool as _pool
    pool = _pool(TEST_DATABASE_URL, min_size=1, max_size=2)
    pool.open(wait=True, timeout=10)
    try:
        before = _window_report(pool)
        recent = datetime.now(timezone.utc) - timedelta(minutes=5)
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
            lead_a, product = _seed(conn)
            lead_b, _ = _seed(conn)
        repo.record_event(event_type="purchase_intent", occurred_at=recent, lead_id=lead_a,
                          dedupe_key=f"intent:{uuid.uuid4().hex}")
        repo.record_event(event_type="product_interest", occurred_at=recent, lead_id=lead_a, product_id=product,
                          dedupe_key=f"interest:{uuid.uuid4().hex}")
        amount = 1_500_000
        repo.confirm_sale(_record(lead_a, amount_cop=amount, sold_at=recent), {product: 1})
        cancelled_sale = _record(lead_b, amount_cop=900_000, sold_at=recent)
        repo.confirm_sale(cancelled_sale, {product: 1})
        repo.cancel_sale(cancelled_sale.sale_ref, reason="devuelto antes de entrega", actor="ana")
        after = _window_report(pool)
    finally:
        pool.close()

    assert after["ventas"]["estado"] == "disponible"
    assert after["ventas"]["ventas_confirmadas"] - before["ventas"]["ventas_confirmadas"] == 1
    assert after["ventas"]["valor_confirmado_cop"] - before["ventas"]["valor_confirmado_cop"] == amount
    assert after["ventas"]["ventas_canceladas"] - (before["ventas"]["ventas_canceladas"] or 0) == 1
    assert after["prospectos"]["leads_con_intencion_de_compra"] - (before["prospectos"]["leads_con_intencion_de_compra"] or 0) == 1
    assert after["prospectos"]["productos_mas_consultados"] is not None
