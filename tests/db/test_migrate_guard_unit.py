"""Guardia del runner frente al historial de Supabase CLI: fallo seguro sin base de datos real.

Con una conexión doble se comprueba que, si el estado del historial no puede determinarse, el runner se detiene
con cli_history_unknown tanto en ensayo como en aplicación, y que nunca ejecuta sentencias de escritura.
"""

from __future__ import annotations

import psycopg
import pytest

from app.db import migrate
from app.db.migrate import MigrationError, run_migrations


class _Result:
    def __init__(self, value):
        self._value = value

    def fetchone(self):
        return (self._value,)

    def fetchall(self):
        return []


class _FakeConn:
    def __init__(self, check_outcome):
        # check_outcome: bool (valor devuelto), "error" (la consulta falla) o "garbage" (valor no booleano).
        self._check_outcome = check_outcome
        self.statements: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, *args):
        self.statements.append(sql)
        if "supabase_migrations" in sql:
            if self._check_outcome == "error":
                raise psycopg.OperationalError("fallo simulado")
            if self._check_outcome == "garbage":
                return _Result("no-booleano")
            return _Result(self._check_outcome)
        if "to_regclass('public.schema_migrations')" in sql:
            return _Result(None)
        return _Result(None)

    def writes(self) -> list[str]:
        return [s for s in self.statements if any(k in s.lower() for k in ("create", "insert", "alter", "drop"))]


def _install(monkeypatch, conn: _FakeConn) -> None:
    monkeypatch.setattr(migrate.psycopg, "connect", lambda *a, **k: conn)


@pytest.mark.parametrize("apply", [False, True])
def test_unknown_history_state_stops_without_writing(monkeypatch, apply):
    conn = _FakeConn("error")
    _install(monkeypatch, conn)
    with pytest.raises(MigrationError) as excinfo:
        run_migrations("postgresql://fake", [], apply=apply)
    assert excinfo.value.code == "cli_history_unknown"
    assert conn.writes() == []


@pytest.mark.parametrize("apply", [False, True])
def test_non_boolean_history_answer_is_treated_as_unknown(monkeypatch, apply):
    conn = _FakeConn("garbage")
    _install(monkeypatch, conn)
    with pytest.raises(MigrationError) as excinfo:
        run_migrations("postgresql://fake", [], apply=apply)
    assert excinfo.value.code == "cli_history_unknown"
    assert conn.writes() == []


def test_cli_managed_apply_is_refused_before_bootstrap(monkeypatch):
    conn = _FakeConn(True)
    _install(monkeypatch, conn)
    with pytest.raises(MigrationError) as excinfo:
        run_migrations("postgresql://fake", [], apply=True)
    assert excinfo.value.code == "supabase_cli_history_present"
    assert conn.writes() == []


def test_cli_managed_dry_run_reports_without_writing(monkeypatch):
    conn = _FakeConn(True)
    _install(monkeypatch, conn)
    report = run_migrations("postgresql://fake", [], apply=False)
    assert report.cli_managed is True
    assert conn.writes() == []


def test_no_cli_history_and_clean_database_allows_apply(monkeypatch):
    conn = _FakeConn(False)
    _install(monkeypatch, conn)
    report = run_migrations("postgresql://fake", [], apply=True)
    assert report.cli_managed is False
    assert any("schema_migrations" in s and "create table" in s.lower() for s in conn.statements)
