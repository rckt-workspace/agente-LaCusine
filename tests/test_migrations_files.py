"""Verificación estática de las migraciones SQL. No requiere base de datos."""

import re
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "supabase" / "migrations"
RLS_TABLES = ("leads", "conversations", "messages", "webhook_events", "products", "human_handoffs", "llm_usage_events")
NAME_PATTERN = re.compile(r"^\d{14}_[a-z0-9_]+\.sql$")


def _migration_files() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def _all_sql() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in _migration_files())


def test_migrations_use_versioned_names_in_order():
    names = [p.name for p in _migration_files()]
    assert names, "no hay migraciones"
    assert all(NAME_PATTERN.match(n) for n in names), names
    assert names == sorted(names)


def test_every_table_enables_row_level_security():
    sql = _all_sql()
    for table in RLS_TABLES:
        assert f"alter table public.{table} enable row level security" in sql, table


def test_every_table_is_revoked_from_public_api_roles():
    sql = _all_sql()
    assert "revoke all on table" in sql
    for role in ("anon", "authenticated"):
        assert f"from {role}" in sql


def _executable_sql() -> str:
    # Sin comentarios: los comentarios pueden explicar qué credenciales NO se guardan.
    lines = (line for line in _all_sql().lower().splitlines() if not line.lstrip().startswith("--"))
    return "\n".join(lines)


def test_no_service_role_or_credentials_in_migrations():
    sql = _executable_sql()
    assert "to service_role" not in sql
    assert "password" not in sql
    assert "api_key" not in sql
    assert "anthropic_api" not in sql
    assert "meta_token" not in sql
    assert "access_token" not in sql


def test_webhook_dedupe_key_is_a_database_constraint():
    sql = _all_sql()
    assert "unique (provider, external_event_id)" in sql
