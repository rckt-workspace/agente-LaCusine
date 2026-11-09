"""Protección del runner frente al historial de Supabase CLI.

Un proyecto con supabase_migrations.schema_migrations lo gestiona la CLI. El runner Python no debe escribir
en él: --apply se rechaza ANTES del bootstrap, y no se crea public.schema_migrations. El dry-run solo lee.
PostgreSQL local desechable (TEST_DATABASE_URL). Solo modifica el esquema supabase_migrations, que limpia al terminar.
"""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from app.db.migrate import MigrationError, load_migrations, run_migrations
from tests.integration.test_postgres_persistence import TEST_DATABASE_URL, _reset_and_migrate

pytestmark = pytest.mark.integration

if not TEST_DATABASE_URL:
    pytest.skip("TEST_DATABASE_URL no configurada", allow_module_level=True)

REPO_MIGRATIONS = Path(__file__).resolve().parents[2] / "supabase" / "migrations"


@pytest.fixture
def cli_managed_database():
    # Mismo punto de partida que el resto de pruebas (esquema public con privilegios de Supabase y migraciones),
    # más el historial de Supabase CLI. Al terminar se elimina el historial y se restaura el estado estándar.
    _reset_and_migrate()
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute("drop schema if exists supabase_migrations cascade")
        conn.execute("create schema supabase_migrations")
        conn.execute(
            "create table supabase_migrations.schema_migrations "
            "(version text primary key, statements text[], name text)"
        )
        conn.execute(
            "insert into supabase_migrations.schema_migrations (version, name) values ('20261027100000', 'core_schema')"
        )
    yield
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute("drop schema if exists supabase_migrations cascade")
    _reset_and_migrate()


def test_apply_is_refused_when_supabase_cli_owns_the_history(cli_managed_database):
    files = load_migrations(REPO_MIGRATIONS)
    with pytest.raises(MigrationError) as excinfo:
        run_migrations(TEST_DATABASE_URL, files, apply=True)
    assert excinfo.value.code == "supabase_cli_history_present"
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        # Ni bootstrap ni registro propio: el runner no debe crear nada en un proyecto gestionado por la CLI.
        assert conn.execute("select to_regclass('public.schema_migrations')").fetchone()[0] is None


def test_dry_run_reports_cli_management_and_writes_nothing(cli_managed_database):
    files = load_migrations(REPO_MIGRATIONS)
    report = run_migrations(TEST_DATABASE_URL, files, apply=False)
    assert report.cli_managed is True
    assert report.applied == []
    assert len(report.pending) == len(files)
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        assert conn.execute("select to_regclass('public.schema_migrations')").fetchone()[0] is None
