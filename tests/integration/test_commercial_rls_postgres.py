"""RLS real para las tablas comerciales y de analítica con roles de login NO propietarios.

Sin políticas, un rol sin BYPASSRLS ve cero filas y sus inserciones fallan. Estas pruebas comprueban que
el backend lee y escribe lo que necesita, que la administración solo lee, y que anon/authenticated no ven nada.
Datos sintéticos en PostgreSQL local desechable. Contraseñas aleatorias, solo durante la prueba.
"""

from __future__ import annotations

import secrets
import uuid
from urllib.parse import urlsplit

import psycopg
import pytest
from psycopg import errors

from tests.integration.test_postgres_persistence import TEST_DATABASE_URL, _reset_and_migrate

pytestmark = pytest.mark.integration

if not TEST_DATABASE_URL:
    pytest.skip("TEST_DATABASE_URL no configurada", allow_module_level=True)

_PARTS = urlsplit(TEST_DATABASE_URL)
APP_LOGIN = "lacusine_app_rls"
ADMIN_LOGIN = "lacusine_admin_rls"
_credentials: dict[str, str] = {}


def _owner() -> psycopg.Connection:
    return psycopg.connect(TEST_DATABASE_URL, autocommit=True)


def _as(login: str) -> psycopg.Connection:
    url = f"postgresql://{login}:{_credentials[login]}@{_PARTS.hostname}:{_PARTS.port}{_PARTS.path}"
    return psycopg.connect(url, autocommit=True)


@pytest.fixture(scope="module", autouse=True)
def roles_ready():
    _reset_and_migrate()
    with _owner() as conn:
        for login, group in ((APP_LOGIN, "lacusine_backend"), (ADMIN_LOGIN, "lacusine_admin_ops")):
            conn.execute(f"drop role if exists {login}")
            _credentials[login] = secrets.token_urlsafe(18)
            conn.execute(f"create role {login} login password '{_credentials[login]}'")
            conn.execute(f"grant {group} to {login}")
    yield
    with _owner() as conn:
        for login in (APP_LOGIN, ADMIN_LOGIN):
            conn.execute(f"drop role if exists {login}")


def _seed_lead(conn: psycopg.Connection) -> uuid.UUID:
    return conn.execute("insert into public.leads (wa_contact_id) values (%s) returning id",
                        (f"5730{secrets.randbelow(10**8):08d}",)).fetchone()[0]


def _seed_sale(conn: psycopg.Connection, lead_id: uuid.UUID) -> uuid.UUID:
    return conn.execute(
        "insert into public.sales (sale_ref, lead_id, amount_cop, sold_at, confirmation_source, confirmed_by, "
        "evidence_ref) values (%s, %s, 2990000, now(), 'admin_manual', 'admin-sintetico', 'factura sintetica') "
        "returning id",
        (f"VTA-{secrets.token_hex(4)}", lead_id),
    ).fetchone()[0]


def test_backend_sees_rows_it_is_allowed_to_read():
    """Caso crítico: sin política el backend vería cero filas sin error. Debe ver las suyas."""
    with _owner() as owner:
        lead = _seed_lead(owner)
        owner.execute("insert into public.commercial_events (occurred_at, event_type, lead_id, dedupe_key) "
                      "values (now(), 'product_interest', %s, %s)", (lead, f"k-{secrets.token_hex(4)}"))
        _seed_sale(owner, lead)
    with _as(APP_LOGIN) as app:
        assert app.execute("select count(*) from public.commercial_events").fetchone()[0] >= 1
        assert app.execute("select count(*) from public.sales").fetchone()[0] >= 1


def test_backend_can_record_events_sales_and_analytics_it_needs():
    with _owner() as owner:
        lead = _seed_lead(owner)
        product = owner.execute(
            """insert into public.products (reference, brand, category, price_cop, source_hash)
               values (%s, 'Jura', 'Cafeteras automáticas', 2990000, %s) returning id""",
            (f"REF-{uuid.uuid4().hex[:8]}", uuid.uuid4().hex + uuid.uuid4().hex),
        ).fetchone()[0]
    with _as(APP_LOGIN) as app:
        app.execute("insert into public.commercial_events (occurred_at, event_type, lead_id, dedupe_key) "
                    "values (now(), 'quote_request', %s, %s)", (lead, f"k-{secrets.token_hex(4)}"))
        app.execute("insert into public.lead_stage_history (lead_id, to_status, reason, changed_by) "
                    "values (%s, 'qualified', 'prueba sintetica', 'sistema')", (lead,))
        sale = app.execute(
            "insert into public.sales (sale_ref, lead_id, amount_cop, sold_at, confirmation_source, confirmed_by, "
            "evidence_ref) values (%s, %s, 1000000, now(), 'admin_manual', 'admin-sintetico', 'factura sintetica') "
            "returning id", (f"VTA-{secrets.token_hex(4)}", lead)).fetchone()[0]
        app.execute("insert into public.sale_products (sale_id, product_id, quantity) values (%s, %s, 1)",
                    (sale, product))
        app.execute("insert into public.sale_status_events (sale_id, status, reason, actor) "
                    "values (%s, 'confirmed', 'prueba sintetica', 'admin-sintetico')", (sale,))
        app.execute("insert into public.analytics_daily_counts (day, metric, value) "
                    "values (current_date, 'mensajes_entrantes', 1)")
        app.execute("update public.analytics_daily_counts set value = value + 1 where metric = 'mensajes_entrantes'")


def test_backend_cannot_rewrite_history_tables():
    with _owner() as owner:
        lead = _seed_lead(owner)
        owner.execute("insert into public.commercial_events (occurred_at, event_type, lead_id, dedupe_key) "
                      "values (now(), 'product_interest', %s, %s)", (lead, f"k-{secrets.token_hex(4)}"))
    with _as(APP_LOGIN) as app:
        with pytest.raises(errors.InsufficientPrivilege):
            app.execute("update public.commercial_events set event_type = 'quote_request'")
        with pytest.raises(errors.InsufficientPrivilege):
            app.execute("delete from public.sales")


def test_admin_reads_commercial_data_but_cannot_write_it():
    with _owner() as owner:
        lead = _seed_lead(owner)
        _seed_sale(owner, lead)
    with _as(ADMIN_LOGIN) as admin:
        assert admin.execute("select count(*) from public.sales").fetchone()[0] >= 1
        with pytest.raises(errors.InsufficientPrivilege):
            admin.execute("insert into public.sales (sale_ref, lead_id, amount_cop, sold_at, confirmation_source, "
                          "confirmed_by, evidence_ref) values ('VTA-X', %s, 1, now(), 'admin_manual', 'x', 'xxx')",
                          (lead,))


@pytest.mark.parametrize("role", ["anon", "authenticated"])
def test_supabase_public_roles_see_no_commercial_data(role):
    with _owner() as owner:
        lead = _seed_lead(owner)
        _seed_sale(owner, lead)
        owner.execute(f"set role {role}")
        try:
            for table in ("commercial_events", "sales", "analytics_daily_counts", "sale_products"):
                with pytest.raises(errors.InsufficientPrivilege):
                    owner.execute(f"select count(*) from public.{table}")
        finally:
            owner.execute("reset role")
