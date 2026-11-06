"""Presupuesto de informes con IA en PostgreSQL desechable: concurrencia real, idempotencia, estados y separación
respecto al gasto comercial. Sin Anthropic: solo se escriben importes y estados de prueba."""

from __future__ import annotations

import threading
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import psycopg
import pytest
from psycopg import errors

from app.db.pool import create_pool
from app.services.analysis_budget import AnalysisLimits, PostgresAnalysisBudgetLedger
from app.services.budget_ledger import BudgetExceededError, LedgerStateError, PostgresBudgetLedger, ReservationConflictError
from tests.integration.test_postgres_persistence import TEST_DATABASE_URL, _reset_and_migrate

pytestmark = pytest.mark.integration

if not TEST_DATABASE_URL:
    pytest.skip("TEST_DATABASE_URL no configurada", allow_module_level=True)

NOW = datetime(2026, 11, 5, 15, 0, tzinfo=timezone.utc)
WEEK = date(2026, 11, 2)
WORST = Decimal("0.005000")
FP = "a" * 64


def _owner() -> psycopg.Connection:
    return psycopg.connect(TEST_DATABASE_URL, autocommit=True)


@pytest.fixture(scope="module", autouse=True)
def schema_ready():
    _reset_and_migrate()
    yield


@pytest.fixture()
def pool():
    with create_pool(TEST_DATABASE_URL, min_size=1, max_size=8) as p:
        yield p
    with _owner() as conn:
        conn.execute("delete from public.analysis_budget_reservations")
        conn.execute("delete from public.budget_reservations")


def _limits(daily=Decimal("1.00"), monthly=Decimal("10.00"), per=Decimal("0.02")) -> AnalysisLimits:
    return AnalysisLimits(per_report_usd=per, daily_usd=daily, monthly_usd=monthly)


def _key(n: int) -> str:
    return f"analysis:ejecutivo:{WEEK.isoformat()}:{n:032x}"


def test_reserve_then_settle_counts_the_verified_cost(pool):
    ledger = PostgresAnalysisBudgetLedger(pool)
    ledger.reserve(_key(1), "ejecutivo", WEEK, FP, WORST, _limits(), NOW)
    assert ledger.state(_key(1)) == "reserved"
    ledger.settle(_key(1), Decimal("0.003100"))
    assert ledger.state(_key(1)) == "settled"
    assert ledger.counted_between(NOW - timedelta(days=1), NOW) == Decimal("0.003100")


def test_the_same_key_is_never_reserved_twice(pool):
    ledger = PostgresAnalysisBudgetLedger(pool)
    ledger.reserve(_key(2), "ejecutivo", WEEK, FP, WORST, _limits(), NOW)
    with pytest.raises(ReservationConflictError):
        ledger.reserve(_key(2), "ejecutivo", WEEK, FP, WORST, _limits(), NOW)


def test_a_settled_or_uncertain_key_is_not_reusable_but_a_released_one_is(pool):
    ledger = PostgresAnalysisBudgetLedger(pool)
    ledger.reserve(_key(3), "ejecutivo", WEEK, FP, WORST, _limits(), NOW)
    ledger.mark_uncertain(_key(3))
    with pytest.raises(ReservationConflictError):
        ledger.reserve(_key(3), "ejecutivo", WEEK, FP, WORST, _limits(), NOW)
    ledger.reserve(_key(4), "ejecutivo", WEEK, FP, WORST, _limits(), NOW)
    ledger.release(_key(4))
    ledger.reserve(_key(4), "ejecutivo", WEEK, FP, WORST, _limits(), NOW)
    assert ledger.state(_key(4)) == "reserved"


def test_uncertain_counts_against_the_budget_and_released_does_not(pool):
    ledger = PostgresAnalysisBudgetLedger(pool)
    start = NOW - timedelta(days=1)
    ledger.reserve(_key(5), "ejecutivo", WEEK, FP, WORST, _limits(), NOW)
    ledger.mark_uncertain(_key(5))
    assert ledger.counted_between(start, NOW) == WORST
    ledger.reserve(_key(6), "ejecutivo", WEEK, FP, WORST, _limits(), NOW)
    ledger.release(_key(6))
    assert ledger.counted_between(start, NOW) == WORST  # solo la incierta


def test_transitions_are_guarded(pool):
    ledger = PostgresAnalysisBudgetLedger(pool)
    with pytest.raises(LedgerStateError):
        ledger.settle(_key(7), Decimal("0"))  # no existe
    ledger.reserve(_key(8), "ejecutivo", WEEK, FP, WORST, _limits(), NOW)
    ledger.settle(_key(8), Decimal("0.001"))
    with pytest.raises(LedgerStateError):
        ledger.mark_uncertain(_key(8))  # ya liquidada


def test_per_report_limit_blocks_without_writing_anything(pool):
    ledger = PostgresAnalysisBudgetLedger(pool)
    with pytest.raises(BudgetExceededError) as exc:
        ledger.reserve(_key(9), "ejecutivo", WEEK, FP, WORST, _limits(per=Decimal("0.001")), NOW)
    assert exc.value.code == "report_limit"
    assert ledger.state(_key(9)) is None


def test_daily_and_monthly_limits_block(pool):
    ledger = PostgresAnalysisBudgetLedger(pool)
    tight_day = _limits(daily=WORST + Decimal("0.0001"))
    ledger.reserve(_key(10), "ejecutivo", WEEK, FP, WORST, tight_day, NOW)
    with pytest.raises(BudgetExceededError) as exc:
        ledger.reserve(_key(11), "ejecutivo", WEEK, FP, WORST, tight_day, NOW)
    assert exc.value.code == "daily_budget"
    next_day = NOW + timedelta(days=1)
    ledger.reserve(_key(12), "ejecutivo", WEEK, FP, WORST, _limits(daily=WORST + Decimal("0.0001"),
                                                                    monthly=Decimal("10")), next_day)
    with pytest.raises(BudgetExceededError) as exc:
        ledger.reserve(_key(13), "ejecutivo", WEEK, FP, WORST, _limits(monthly=WORST + Decimal("0.0001")),
                       NOW + timedelta(days=2))
    assert exc.value.code == "monthly_budget"


def test_concurrent_reservations_never_exceed_the_daily_limit(pool):
    """Diez hilos piden a la vez con un límite diario para tres peores casos: exactamente tres entran."""
    ledger = PostgresAnalysisBudgetLedger(pool)
    limits = _limits(daily=WORST * 3 + Decimal("0.0001"))
    barrier = threading.Barrier(10)
    results: list[str] = []
    lock = threading.Lock()

    def attempt(n: int) -> None:
        barrier.wait()
        try:
            ledger.reserve(_key(100 + n), "ejecutivo", WEEK, FP, WORST, limits, NOW)
            outcome = "ok"
        except BudgetExceededError as exc:
            outcome = exc.code
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=attempt, args=(n,)) for n in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert results.count("ok") == 3
    assert results.count("daily_budget") == 7
    assert ledger.counted_between(NOW - timedelta(days=1), NOW) == WORST * 3


def test_commercial_spend_and_analysis_spend_do_not_count_against_each_other(pool):
    with _owner() as conn:
        conn.execute("insert into public.budget_reservations (call_id, status, reserved_usd, counted_usd, created_at) "
                     "values ('commercial-prueba', 'settled', 5, 5, %s)", (NOW,))
    analysis = PostgresAnalysisBudgetLedger(pool)
    assert analysis.counted_between(NOW - timedelta(days=1), NOW) == Decimal("0")
    analysis.reserve(_key(200), "ejecutivo", WEEK, FP, WORST, _limits(daily=Decimal("0.01")), NOW)  # no lo ve
    commercial = PostgresBudgetLedger(pool)
    assert commercial.counted_between(NOW - timedelta(days=1), NOW) == Decimal("5")


def test_the_table_stores_no_prompt_or_response_text(pool):
    with _owner() as conn:
        cols = {r[0] for r in conn.execute(
            "select column_name from information_schema.columns where table_schema='public' "
            "and table_name='analysis_budget_reservations'").fetchall()}
    assert cols == {"id", "call_id", "purpose", "report_kind", "week_start", "fingerprint", "status",
                    "reserved_usd", "counted_usd", "created_at", "reconciled_at"}


@pytest.mark.parametrize("role", ["anon", "authenticated"])
def test_public_supabase_roles_cannot_touch_the_report_budget(role):
    with _owner() as conn:
        conn.execute(f"set role {role}")
        try:
            with pytest.raises(errors.InsufficientPrivilege):
                conn.execute("select count(*) from public.analysis_budget_reservations")
        finally:
            conn.execute("reset role")
