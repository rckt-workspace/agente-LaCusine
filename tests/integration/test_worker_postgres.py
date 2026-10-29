"""Cola, outbox, presupuesto y flujo extremo a extremo contra PostgreSQL real (base protegida por db_guard)."""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import psycopg
import pytest

from app.core.config import Settings
from app.db.pool import create_pool
from app.repositories.crm import PostgresConversationRepository, PostgresLeadRepository, PostgresMessageRepository
from app.repositories.errors import StorageError
from app.repositories.jobs import JobRepository
from app.repositories.outbox import OutboxRepository
from app.repositories.webhook_events import PostgresWebhookEventRepository, WebhookEventRecord
from app.services.budget_ledger import (
    BudgetExceededError,
    BudgetLimits,
    LedgerStateError,
    PostgresBudgetLedger,
    ReservationConflictError,
)
from app.worker.bootstrap import build_worker
from app.worker.config import WorkerSettings
from tests.integration.test_postgres_persistence import TEST_DATABASE_URL, _reset_and_migrate

pytestmark = pytest.mark.integration

if not TEST_DATABASE_URL:
    pytest.skip("TEST_DATABASE_URL no configurada", allow_module_level=True)


@pytest.fixture(scope="module", autouse=True)
def schema_ready():
    _reset_and_migrate()


@pytest.fixture(autouse=True)
def clean(schema_ready):
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute(
            "truncate public.leads, public.products, public.webhook_events, public.llm_usage_events, "
            "public.budget_reservations, public.outbound_messages, public.contact_leases restart identity cascade"
        )


def _pool():
    p = create_pool(TEST_DATABASE_URL, max_size=8)
    p.open(wait=True, timeout=10)
    return p


def _insert(pool, wamid: str, contact: str = "573001112233", phone: str = "1000", body: str = "hola"):
    payload = {"id": wamid, "from": contact, "type": "text", "text": {"body": body}}
    sha = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    repo = PostgresWebhookEventRepository(pool, retention_days=7)
    repo.register(WebhookEventRecord(external_event_id=wamid, contact_wa_id=contact, payload=payload,
                                     payload_sha256=sha, business_phone_id=phone))
    time.sleep(0.003)  # orden de llegada estable en received_at


def _status(wamid: str) -> str:
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        return conn.execute("select processing_status from public.webhook_events where external_event_id = %s",
                            (wamid,)).fetchone()[0]


def _count(sql: str, params: tuple = ()) -> int:
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        return conn.execute(sql, params).fetchone()[0]


# --- Adquisición atómica ---

def test_two_workers_never_claim_the_same_event():
    pool_a, pool_b = _pool(), _pool()
    try:
        for i in range(30):
            _insert(pool_a, f"wamid.P{i}", contact=f"5730000{i:05d}")
        worker_a = JobRepository(pool_a, owner="worker-a", lease_seconds=60)
        worker_b = JobRepository(pool_b, owner="worker-b", lease_seconds=60)
        seen: list = []
        lock = threading.Lock()
        barrier = threading.Barrier(2)

        def run(worker):
            barrier.wait()
            while True:
                batch = worker.claim(4, uuid.uuid4)
                if not batch:
                    return
                with lock:
                    seen.extend(c.event_id for c in batch)

        threads = [threading.Thread(target=run, args=(w,)) for w in (worker_a, worker_b)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(seen) == 30
        assert len(set(seen)) == 30  # ninguno reclamado dos veces
        assert _count("select count(*) from public.webhook_events where processing_status = 'processing'") == 30
    finally:
        pool_a.close()
        pool_b.close()


def test_same_contact_is_serialized_across_workers_in_arrival_order():
    pool_a, pool_b = _pool(), _pool()
    try:
        for wamid in ("wamid.S1", "wamid.S2", "wamid.S3"):
            _insert(pool_a, wamid, contact="573009990001")
        a = JobRepository(pool_a, owner="a", lease_seconds=60)
        b = JobRepository(pool_b, owner="b", lease_seconds=60)

        first = a.claim(1, uuid.uuid4)
        assert [c.external_event_id for c in first] == ["wamid.S1"]
        assert b.claim(5, uuid.uuid4) == []  # el segundo espera: el primero está en curso
        assert a.finish(first[0], "processed")

        second = b.claim(5, uuid.uuid4)
        assert [c.external_event_id for c in second] == ["wamid.S2"]  # nunca el tercero antes que el segundo
        assert a.claim(5, uuid.uuid4) == []
        assert b.finish(second[0], "processed")
        third = a.claim(5, uuid.uuid4)
        assert [c.external_event_id for c in third] == ["wamid.S3"]
    finally:
        pool_a.close()
        pool_b.close()


def test_different_contacts_and_different_business_lines_proceed_in_parallel():
    pool_a, pool_b = _pool(), _pool()
    try:
        _insert(pool_a, "wamid.PAR1", contact="573008880001", phone="1000")
        _insert(pool_a, "wamid.PAR2", contact="573008880002", phone="1000")
        _insert(pool_a, "wamid.PAR3", contact="573008880001", phone="2000")  # misma persona, otra línea
        a = JobRepository(pool_a, owner="a", lease_seconds=60)
        b = JobRepository(pool_b, owner="b", lease_seconds=60)
        got_a = a.claim(1, uuid.uuid4)
        got_b = b.claim(1, uuid.uuid4)
        assert len(got_a) == 1 and len(got_b) == 1
        assert got_a[0].external_event_id != got_b[0].external_event_id
    finally:
        pool_a.close()
        pool_b.close()


def test_claim_does_not_return_events_already_finished():
    pool = _pool()
    try:
        _insert(pool, "wamid.FIN")
        jobs = JobRepository(pool, owner="a", lease_seconds=60)
        claim = jobs.claim(1, uuid.uuid4)[0]
        assert jobs.finish(claim, "processed")
        assert jobs.claim(5, uuid.uuid4) == []
    finally:
        pool.close()


# --- Leases, recuperación y propiedad ---

def test_expired_lease_is_recovered_and_stale_owner_is_rejected():
    pool_old, pool_new = _pool(), _pool()
    try:
        _insert(pool_old, "wamid.LEASE")
        old = JobRepository(pool_old, owner="old", lease_seconds=1)
        stale = old.claim(1, uuid.uuid4)[0]
        time.sleep(1.2)  # el worker antiguo "muere" sin finalizar

        new = JobRepository(pool_new, owner="new", lease_seconds=60)
        assert new.recover_expired(max_attempts=5) == 1
        fresh = new.claim(1, uuid.uuid4)[0]
        assert fresh.event_id == stale.event_id
        assert fresh.attempts == 2

        # El propietario antiguo no puede renovar, finalizar ni sobrescribir.
        assert old.renew(stale) is False
        assert old.finish(stale, "processed") is False
        assert _status("wamid.LEASE") == "processing"
        assert new.finish(fresh, "processed") is True
        assert _status("wamid.LEASE") == "processed"
    finally:
        pool_old.close()
        pool_new.close()


def test_expired_lease_past_max_attempts_goes_to_manual_review():
    pool = _pool()
    try:
        _insert(pool, "wamid.EXH")
        jobs = JobRepository(pool, owner="a", lease_seconds=1)
        jobs.claim(1, uuid.uuid4)
        time.sleep(1.2)
        assert jobs.recover_expired(max_attempts=1) == 1
        assert _status("wamid.EXH") == "manual_review"
        assert jobs.claim(5, uuid.uuid4) == []  # no se reintenta solo
    finally:
        pool.close()


def test_renew_keeps_ownership_while_processing():
    pool = _pool()
    try:
        _insert(pool, "wamid.RENEW")
        jobs = JobRepository(pool, owner="a", lease_seconds=1)
        claim = jobs.claim(1, uuid.uuid4)[0]
        assert jobs.renew(claim) is True
        time.sleep(1.2)
        assert jobs.renew(claim) is False  # ya venció: la renovación tardía se rechaza
    finally:
        pool.close()


def test_finish_requires_a_retry_delay_and_retry_sets_next_attempt():
    pool = _pool()
    try:
        _insert(pool, "wamid.RET")
        jobs = JobRepository(pool, owner="a", lease_seconds=60)
        claim = jobs.claim(1, uuid.uuid4)[0]
        with pytest.raises(ValueError):
            jobs.finish(claim, "retry")
        assert jobs.finish(claim, "retry", error_code="busy", retry_after_seconds=30) is True
        assert _status("wamid.RET") == "retry"
        assert jobs.claim(5, uuid.uuid4) == []  # aún no toca reintentar
    finally:
        pool.close()


def test_finish_drops_payload_and_releases_contact_lease():
    pool = _pool()
    try:
        _insert(pool, "wamid.DROP")
        jobs = JobRepository(pool, owner="a", lease_seconds=60)
        claim = jobs.claim(1, uuid.uuid4)[0]
        assert jobs.finish(claim, "processed") is True
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
            payload, processed_at = conn.execute(
                "select payload, processed_at from public.webhook_events where external_event_id = 'wamid.DROP'"
            ).fetchone()
        assert payload is None and processed_at is not None
        assert _count("select count(*) from public.contact_leases") == 0
    finally:
        pool.close()


def test_worker_restart_resumes_pending_events():
    pool = _pool()
    try:
        _insert(pool, "wamid.RESUME1", contact="573007770001")
        _insert(pool, "wamid.RESUME2", contact="573007770002")
        before = JobRepository(pool, owner="before-crash", lease_seconds=1)
        before.claim(2, uuid.uuid4)  # reclama ambos y "muere"
    finally:
        pool.close()
    time.sleep(1.2)

    restarted_pool = _pool()
    try:
        after = JobRepository(restarted_pool, owner="after-restart", lease_seconds=60)
        after.recover_expired(max_attempts=5)
        resumed = after.claim(5, uuid.uuid4)
        assert sorted(c.external_event_id for c in resumed) == ["wamid.RESUME1", "wamid.RESUME2"]
    finally:
        restarted_pool.close()


# --- Constraints y retención ---

def test_queue_check_constraints_reject_inconsistent_lease_state():
    pool = _pool()
    try:
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(
                    "insert into public.webhook_events (external_event_id, payload_sha256, expires_at, "
                    "processing_status) values ('wamid.BADLEASE', %s, now(), 'processing')",
                    ("a" * 64,),
                )
    finally:
        pool.close()


def test_retention_never_deletes_pending_or_review_events():
    pool = _pool()
    try:
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
            for wamid, status, processed in [
                ("wamid.R-PROCESSED", "processed", True),
                ("wamid.R-DEAD", "dead_letter", False),
                ("wamid.R-RECEIVED", "received", False),
                ("wamid.R-RETRY", "retry", False),
                ("wamid.R-REVIEW", "manual_review", False),
            ]:
                conn.execute(
                    "insert into public.webhook_events (external_event_id, payload_sha256, expires_at, "
                    "processing_status, processed_at) values (%s, %s, now() - interval '1 day', %s, "
                    "case when %s then now() else null end)",
                    (wamid, "b" * 64, status, processed),
                )
            deleted, _, _ = conn.execute("select * from public.purge_expired_personal_data(100)").fetchone()
            remaining = {r[0] for r in conn.execute("select external_event_id from public.webhook_events").fetchall()}
        assert deleted == 2  # solo processed y dead_letter
        assert remaining == {"wamid.R-RECEIVED", "wamid.R-RETRY", "wamid.R-REVIEW"}
    finally:
        pool.close()


def test_retention_deletes_only_closed_outbox_messages():
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        lead = conn.execute("insert into public.leads (wa_contact_id) values ('573006660001') returning id").fetchone()[0]
        conv = conn.execute("insert into public.conversations (lead_id) values (%s) returning id", (lead,)).fetchone()[0]
        for key, status in [("k-sent", "sent"), ("k-pending", "pending")]:
            conn.execute(
                "insert into public.outbound_messages (idempotency_key, conversation_id, content_text, delivery_status, "
                "expires_at) values (%s, %s, 'hola', %s, now() - interval '1 day')",
                (key, conv, status),
            )
        _, _, outbound = conn.execute("select * from public.purge_expired_personal_data(100)").fetchone()
        remaining = conn.execute("select idempotency_key from public.outbound_messages").fetchall()
    assert outbound == 1
    assert remaining == [("k-pending",)]


# --- Outbox ---

def _conversation(pool, contact="573005550009"):
    lead_id = PostgresLeadRepository(pool).get_or_create(contact)
    conv_id = PostgresConversationRepository(pool).get_or_open(lead_id)
    return lead_id, conv_id


def test_outbox_persists_pending_reply_once_per_idempotency_key():
    pool = _pool()
    try:
        _insert(pool, "wamid.OUT1")
        lead_id, conv_id = _conversation(pool)
        outbox = OutboxRepository(pool)
        event_id = _event_id(pool, "wamid.OUT1")
        first = outbox.persist_result(event_id=event_id, idempotency_key="wamid.OUT1:reply",
                                      conversation_id=conv_id, lead_id=lead_id, lead_status="qualifying",
                                      reply_text="Tenemos varias opciones.")
        second = outbox.persist_result(event_id=event_id, idempotency_key="wamid.OUT1:reply",
                                       conversation_id=conv_id, lead_id=lead_id, lead_status="qualifying",
                                       reply_text="Tenemos varias opciones.")
        assert (first, second) == (True, False)
        assert _count("select count(*) from public.outbound_messages") == 1
        status = _count("select count(*) from public.outbound_messages where delivery_status = 'pending'")
        assert status == 1  # generada y pendiente: no se marca como enviada ni entregada
    finally:
        pool.close()


def test_outbox_rejects_second_reply_for_same_event_even_with_new_key():
    pool = _pool()
    try:
        _insert(pool, "wamid.OUT2")
        lead_id, conv_id = _conversation(pool, contact="573005550010")
        outbox = OutboxRepository(pool)
        event_id = _event_id(pool, "wamid.OUT2")
        outbox.persist_result(event_id=event_id, idempotency_key="k1", conversation_id=conv_id,
                              lead_id=lead_id, lead_status=None, reply_text="uno")
        created = outbox.persist_result(event_id=event_id, idempotency_key="k2", conversation_id=conv_id,
                                        lead_id=lead_id, lead_status=None, reply_text="dos")
        assert created is False  # una única intención de respuesta por evento
    finally:
        pool.close()


def test_outbox_failure_leaves_no_partial_state():
    pool = _pool()
    try:
        lead_id, _ = _conversation(pool, contact="573005550011")
        outbox = OutboxRepository(pool)
        with pytest.raises(StorageError):
            outbox.persist_result(event_id=None, idempotency_key="k-bad", conversation_id=uuid.uuid4(),
                                  lead_id=lead_id, lead_status="qualified", reply_text="no debe existir")
        assert _count("select count(*) from public.outbound_messages") == 0
        status = _count("select count(*) from public.leads where id = %s and status = 'qualified'", (lead_id,))
        assert status == 0  # la actualización del lead no sobrevive a un fallo de la transacción
    finally:
        pool.close()


def _event_id(pool, wamid: str):
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        return conn.execute("select id from public.webhook_events where external_event_id = %s", (wamid,)).fetchone()[0]


# --- Presupuesto persistente ---

def _day_start():
    bogota = timezone(timedelta(hours=-5))
    local = datetime.now(timezone.utc).astimezone(bogota)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def test_concurrent_reservations_never_exceed_the_daily_budget():
    pool = _pool()
    try:
        ledger = PostgresBudgetLedger(pool)
        limits = BudgetLimits(daily_usd=Decimal("1.00"))
        now = datetime.now(timezone.utc)
        barrier = threading.Barrier(8)
        outcomes: list[str] = []
        lock = threading.Lock()

        def attempt(index):
            barrier.wait()
            try:
                ledger.reserve(f"wamid.BUD{index}", None, Decimal("0.30"), limits, _day_start(), _day_start(), now)
                result = "reserved"
            except BudgetExceededError as exc:
                result = exc.code
            with lock:
                outcomes.append(result)

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert outcomes.count("reserved") == 3  # 3 x 0.30 = 0.90 <= 1.00; el cuarto excedería el límite
        assert outcomes.count("daily_budget") == 5
        assert ledger.counted_between(_day_start(), now + timedelta(seconds=1)) == Decimal("0.90")
    finally:
        pool.close()


def test_reservation_lifecycle_in_database_matches_the_state_machine():
    pool = _pool()
    try:
        ledger = PostgresBudgetLedger(pool)
        now = datetime.now(timezone.utc)
        window = (_day_start(), now + timedelta(seconds=1))
        ledger.reserve("wamid.LC1", None, Decimal("0.5"), BudgetLimits(), window[0], window[0], now)
        assert ledger.state("wamid.LC1") == "reserved"
        assert ledger.counted_between(*window) == Decimal("0.5")

        ledger.settle("wamid.LC1", Decimal("0.2"))
        assert ledger.counted_between(*window) == Decimal("0.2")
        with pytest.raises(LedgerStateError):
            ledger.release("wamid.LC1")  # un liquidado no se libera

        ledger.reserve("wamid.LC2", None, Decimal("0.4"), BudgetLimits(), window[0], window[0], now)
        ledger.release("wamid.LC2")
        assert ledger.counted_between(*window) == Decimal("0.2")  # liberado no cuenta
        ledger.reserve("wamid.LC2", None, Decimal("0.1"), BudgetLimits(), window[0], window[0], now)  # reintento
        assert ledger.state("wamid.LC2") == "reserved"

        ledger.reserve("wamid.LC3", None, Decimal("0.3"), BudgetLimits(), window[0], window[0], now)
        ledger.mark_uncertain("wamid.LC3")
        assert ledger.counted_between(*window) == Decimal("0.2") + Decimal("0.1") + Decimal("0.3")
        with pytest.raises(ReservationConflictError):
            ledger.reserve("wamid.LC3", None, Decimal("0.3"), BudgetLimits(), window[0], window[0], now)
    finally:
        pool.close()


def test_reservation_with_limit_per_interaction_is_rejected_before_writing():
    pool = _pool()
    try:
        ledger = PostgresBudgetLedger(pool)
        now = datetime.now(timezone.utc)
        with pytest.raises(BudgetExceededError) as excinfo:
            ledger.reserve("wamid.INT", None, Decimal("1"), BudgetLimits(interaction_usd=Decimal("0.5")),
                           _day_start(), _day_start(), now)
        assert excinfo.value.code == "interaction_limit"
        assert ledger.state("wamid.INT") is None
    finally:
        pool.close()


# --- Flujo extremo a extremo: webhook -> PostgreSQL -> worker -> agente simulado -> outbox ---

def test_end_to_end_webhook_to_outbox_with_simulated_provider():
    pool = _pool()
    try:
        _insert(pool, "wamid.E2E", contact="573004440099", body="Busco una cafetera Jura")
        settings = Settings(meta_app_secret=None, meta_verify_token=None, database_url=TEST_DATABASE_URL)
        worker_settings = WorkerSettings(enabled=True, owner="e2e", max_concurrency=2, lease_seconds=60,
                                         processing_timeout_seconds=30)
        worker, worker_pool = build_worker(settings, worker_settings)
        worker_pool.open(wait=True, timeout=10)
        try:
            assert worker.run_once() == 1
        finally:
            worker.shutdown()
            worker_pool.close()

        assert _status("wamid.E2E") == "processed"
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
            payload = conn.execute(
                "select payload from public.webhook_events where external_event_id = 'wamid.E2E'").fetchone()[0]
            reply = conn.execute(
                "select content_text, delivery_status from public.outbound_messages").fetchone()
            message = conn.execute(
                "select content_text from public.messages where wa_message_id = 'wamid.E2E'").fetchone()
            lead_status = conn.execute("select status from public.leads").fetchone()[0]
        assert payload is None  # el payload se elimina al finalizar
        assert reply[1] == "pending"  # generada, no enviada
        assert reply[0]  # respuesta validada presente
        assert message[0] == "Busco una cafetera Jura"
        assert lead_status in {"new", "qualifying", "qualified", "handed_off"}
        assert _count("select count(*) from public.contact_leases") == 0
    finally:
        pool.close()
