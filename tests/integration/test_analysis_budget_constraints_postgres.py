"""Restricciones de estado e importe de analysis_budget_reservations (migración 20261105130000).

Una reserva liberada no puede tener importe contable. Estados válidos aceptados; combinaciones inconsistentes
rechazadas tanto en INSERT como en UPDATE. Solo importes de prueba, PostgreSQL local desechable.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import psycopg
import pytest
from psycopg import errors

from app.db.pool import create_pool
from app.services.analysis_budget import AnalysisLimits, PostgresAnalysisBudgetLedger
from tests.integration.test_postgres_persistence import TEST_DATABASE_URL, _reset_and_migrate

pytestmark = pytest.mark.integration

if not TEST_DATABASE_URL:
    pytest.skip("TEST_DATABASE_URL no configurada", allow_module_level=True)

NOW = datetime(2026, 11, 5, 15, 0, tzinfo=timezone.utc)
WEEK = date(2026, 11, 2)
FP = "b" * 64


@pytest.fixture(scope="module", autouse=True)
def schema_ready():
    _reset_and_migrate()
    yield


@pytest.fixture()
def owner():
    conn = psycopg.connect(TEST_DATABASE_URL, autocommit=True)
    yield conn
    conn.execute("delete from public.analysis_budget_reservations")
    conn.close()


def _insert(conn, call_id: str, status: str, reserved: str, counted: str) -> None:
    conn.execute(
        "insert into public.analysis_budget_reservations "
        "(call_id, report_kind, week_start, fingerprint, status, reserved_usd, counted_usd) "
        "values (%s, 'ejecutivo', %s, %s, %s, %s, %s)",
        (call_id, WEEK, FP, status, Decimal(reserved), Decimal(counted)),
    )


@pytest.mark.parametrize(
    ("status", "reserved", "counted"),
    [
        ("reserved", "0.005", "0.005"),
        ("settled", "0.005", "0.002"),
        ("uncertain", "0.005", "0.005"),
        ("released", "0.005", "0"),
    ],
)
def test_consistent_states_are_accepted(owner, status, reserved, counted):
    _insert(owner, f"ok-{status}", status, reserved, counted)
    assert owner.execute(
        "select status from public.analysis_budget_reservations where call_id = %s", (f"ok-{status}",)
    ).fetchone()[0] == status


def test_released_with_counted_amount_is_rejected_on_insert(owner):
    with pytest.raises(errors.CheckViolation):
        _insert(owner, "bad-insert", "released", "0.005", "0.005")


def test_marking_a_reserved_row_released_without_zeroing_is_rejected(owner):
    _insert(owner, "bad-update", "reserved", "0.005", "0.005")
    with pytest.raises(errors.CheckViolation):
        owner.execute(
            "update public.analysis_budget_reservations set status = 'released' where call_id = 'bad-update'"
        )
    # La fila no cambió.
    assert owner.execute(
        "select status, counted_usd from public.analysis_budget_reservations where call_id = 'bad-update'"
    ).fetchone() == ("reserved", Decimal("0.005000"))


def test_the_application_release_path_satisfies_the_constraint():
    with create_pool(TEST_DATABASE_URL, min_size=1, max_size=2) as pool:
        ledger = PostgresAnalysisBudgetLedger(pool)
        limits = AnalysisLimits(per_report_usd=Decimal("0.02"), daily_usd=Decimal("1"), monthly_usd=Decimal("10"))
        ledger.reserve("app-release", "ejecutivo", WEEK, FP, Decimal("0.005"), limits, NOW)
        ledger.release("app-release")
        assert ledger.state("app-release") == "released"
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        assert conn.execute(
            "select counted_usd from public.analysis_budget_reservations where call_id = 'app-release'"
        ).fetchone()[0] == Decimal("0")
        conn.execute("delete from public.analysis_budget_reservations where call_id = 'app-release'")
