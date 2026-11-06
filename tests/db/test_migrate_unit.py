"""Herramienta de migraciones: validación de archivos sin base de datos."""

from pathlib import Path

import pytest

from app.db.migrate import MigrationError, load_migrations

REPO_MIGRATIONS = Path(__file__).resolve().parents[2] / "supabase" / "migrations"


def test_repository_migrations_load_in_order_with_valid_names():
    files = load_migrations(REPO_MIGRATIONS)
    names = [f.name for f in files]
    assert names == sorted(names)
    # 13 publicadas + consolidación de analítica (20261105100000) + eventos y ventas comerciales (20261105110000)
    # + políticas RLS de esas tablas (20261105120000) + presupuesto de informes con IA (20261105130000).
    assert len(names) == 17
    assert names[-1] == "20261105130000_business_report_budget.sql"
    assert all(len(f.sha256) == 64 for f in files)


def test_invalid_file_name_is_rejected(tmp_path):
    (tmp_path / "bad-name.sql").write_text("select 1;", encoding="utf-8")
    with pytest.raises(MigrationError) as excinfo:
        load_migrations(tmp_path)
    assert excinfo.value.code == "migration_name_invalid"


def test_hash_changes_when_content_changes(tmp_path):
    path = tmp_path / "20261101000000_a.sql"
    path.write_text("select 1;", encoding="utf-8")
    first = load_migrations(tmp_path)[0].sha256
    path.write_text("select 2;", encoding="utf-8")
    assert load_migrations(tmp_path)[0].sha256 != first
