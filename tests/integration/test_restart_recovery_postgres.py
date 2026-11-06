"""Reinicio y recuperación del worker contra PostgreSQL local desechable. Sin proveedores reales."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from app.db.pool import create_pool
from app.repositories.jobs import JobRepository
from tests.integration.test_postgres_persistence import TEST_DATABASE_URL, _reset_and_migrate

if not TEST_DATABASE_URL:
    pytest.skip("TEST_DATABASE_URL no configurada", allow_module_level=True)

NOW = datetime.now(timezone.utc)
PHONE = "1234567890"


@pytest.fixture(scope="module", autouse=True)
def schema_ready():
    _reset_and_migrate()


@pytest.fixture
def pool():
    p = create_pool(TEST_DATABASE_URL, min_size=1, max_size=3)
    p.open(wait=True, timeout=10)
    yield p
    p.close()


@pytest.fixture(autouse=True)
def empty_queue():
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute("delete from public.webhook_events")
        conn.execute("delete from public.contact_leases")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _event(conn, *, status, contact, received, lease=None, attempts=0, processed=False, error=None) -> uuid.UUID:
    ext = f"wamid.{uuid.uuid4().hex}"
    token = uuid.uuid4() if status == "processing" else None
    lease_expires = lease
    return conn.execute(
        """insert into public.webhook_events
             (external_event_id, contact_wa_id, business_phone_id, payload_sha256, processing_status,
              received_at, next_attempt_at, expires_at, attempts, processed_at, last_error_code,
              lease_token, lease_owner, lease_expires_at, payload)
           values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, '{"type":"text","text":{"body":"x"}}')
           returning id""",
        (ext, contact, PHONE, _sha(ext), status, received, received, received + timedelta(days=7), attempts,
         received if processed else None, error, token, "worker-anterior" if token else None, lease_expires),
    ).fetchone()[0]


def _status(conn, event_id) -> str:
    return conn.execute("select processing_status from public.webhook_events where id = %s", (event_id,)).fetchone()[0]


def _claimed_ids(repo: JobRepository) -> set:
    return {c.event_id for c in repo.claim(20, uuid.uuid4)}


def test_incident_scenario_recovers_the_expired_lease_and_keeps_history():
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        expired = _event(conn, status="processing", contact="573001112233", received=NOW - timedelta(hours=1),
                         lease=NOW - timedelta(minutes=49), attempts=1)
        dead = _event(conn, status="dead_letter", contact="573001112244", received=NOW - timedelta(hours=2),
                      attempts=1, processed=False, error="missing_sender")  # dead_letter: sin processed_at
        reviews = [_event(conn, status="manual_review", contact=None, received=NOW - timedelta(hours=3),
                          attempts=5, error="status_unmatched_attempts_exhausted") for _ in range(3)]
        recent = [_event(conn, status="received", contact=f"5730000{i:05d}", received=NOW - timedelta(minutes=5))
                  for i in range(4)]

    repo = JobRepository(create_pool(TEST_DATABASE_URL, min_size=1, max_size=2), owner="worker-nuevo", lease_seconds=120)
    repo._pool.open(wait=True, timeout=10)
    try:
        assert repo.recover_expired(max_attempts=5) == 1
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
            assert _status(conn, expired) == "retry"
            assert _status(conn, dead) == "dead_letter"  # histórico: no se reencola
            assert all(_status(conn, r) == "manual_review" for r in reviews)  # no se reencolan
        claimed = _claimed_ids(repo)
        assert expired in claimed
        assert set(recent) <= claimed  # los mensajes nuevos de otros clientes sí avanzan
        assert dead not in claimed and not (set(reviews) & claimed)
    finally:
        repo._pool.close()


def test_a_live_lease_is_never_recovered_so_two_workers_do_not_overlap():
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        live = _event(conn, status="processing", contact="573001112255", received=NOW - timedelta(minutes=1),
                      lease=NOW + timedelta(minutes=2), attempts=1)
    repo = JobRepository(create_pool(TEST_DATABASE_URL, min_size=1, max_size=2), owner="otro", lease_seconds=120)
    repo._pool.open(wait=True, timeout=10)
    try:
        assert repo.recover_expired(max_attempts=5) == 0
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
            assert _status(conn, live) == "processing"
        assert live not in _claimed_ids(repo)
    finally:
        repo._pool.close()


def test_recovery_is_idempotent_and_does_not_reclaim_twice_in_one_cycle():
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        _event(conn, status="processing", contact="573001112266", received=NOW - timedelta(hours=1),
               lease=NOW - timedelta(minutes=10), attempts=1)
    repo = JobRepository(create_pool(TEST_DATABASE_URL, min_size=1, max_size=2), owner="w1", lease_seconds=120)
    repo._pool.open(wait=True, timeout=10)
    try:
        assert repo.recover_expired(max_attempts=5) == 1
        assert repo.recover_expired(max_attempts=5) == 0
        first = _claimed_ids(repo)
        second = _claimed_ids(repo)
        assert len(first) == 1 and second == set()
    finally:
        repo._pool.close()


def test_recovery_past_the_attempt_limit_sends_the_event_to_review_not_back_to_the_queue():
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        exhausted = _event(conn, status="processing", contact="573001112277", received=NOW - timedelta(hours=1),
                           lease=NOW - timedelta(minutes=10), attempts=5)
    repo = JobRepository(create_pool(TEST_DATABASE_URL, min_size=1, max_size=2), owner="w1", lease_seconds=120)
    repo._pool.open(wait=True, timeout=10)
    try:
        assert repo.recover_expired(max_attempts=5) == 1
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
            assert _status(conn, exhausted) == "manual_review"
        assert exhausted not in _claimed_ids(repo)
    finally:
        repo._pool.close()


def test_a_manual_review_holds_only_its_own_contact():
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        held = _event(conn, status="manual_review", contact="573001112288", received=NOW - timedelta(hours=2),
                      attempts=5, error="reservation_uncertain")
        blocked_follow_up = _event(conn, status="received", contact="573001112288", received=NOW - timedelta(hours=1))
        other_customer = _event(conn, status="received", contact="573001112299", received=NOW - timedelta(minutes=30))
    repo = JobRepository(create_pool(TEST_DATABASE_URL, min_size=1, max_size=2), owner="w1", lease_seconds=120)
    repo._pool.open(wait=True, timeout=10)
    try:
        claimed = _claimed_ids(repo)
        assert other_customer in claimed
        assert blocked_follow_up not in claimed  # la misma conversación queda retenida
        assert held not in claimed
    finally:
        repo._pool.close()


def test_two_claims_at_the_same_time_never_return_the_same_event():
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        _event(conn, status="received", contact="573001112300", received=NOW - timedelta(minutes=3))
    repo_a = JobRepository(create_pool(TEST_DATABASE_URL, min_size=1, max_size=2), owner="a", lease_seconds=120)
    repo_b = JobRepository(create_pool(TEST_DATABASE_URL, min_size=1, max_size=2), owner="b", lease_seconds=120)
    repo_a._pool.open(wait=True, timeout=10)
    repo_b._pool.open(wait=True, timeout=10)
    try:
        got_a = repo_a.claim(5, uuid.uuid4)
        got_b = repo_b.claim(5, uuid.uuid4)
        ids = [c.event_id for c in got_a + got_b]
        assert len(ids) == len(set(ids)) == 1
    finally:
        repo_a._pool.close()
        repo_b._pool.close()
