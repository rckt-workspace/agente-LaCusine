"""Prueba con procesos de sistema operativo independientes (no hilos) sobre la misma cola PostgreSQL."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest

from tests.integration.test_postgres_persistence import TEST_DATABASE_URL, _reset_and_migrate

pytestmark = pytest.mark.integration

if not TEST_DATABASE_URL:
    pytest.skip("TEST_DATABASE_URL no configurada", allow_module_level=True)

ROOT = Path(__file__).resolve().parents[2]

CLAIMER = r'''
import json, sys, uuid
from app.db.pool import create_pool
from app.repositories.jobs import JobRepository
pool = create_pool(sys.argv[1], max_size=2)
pool.open(wait=True, timeout=10)
jobs = JobRepository(pool, owner=sys.argv[2], lease_seconds=120)
claimed = []
while True:
    batch = jobs.claim(3, uuid.uuid4)
    if not batch:
        break
    claimed.extend(str(c.event_id) for c in batch)
print(json.dumps(claimed))
pool.close()
'''


@pytest.fixture(scope="module", autouse=True)
def schema_ready():
    _reset_and_migrate()


def _insert_events(count: int):
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        for index in range(count):
            payload = {"id": f"wamid.PROC{index}", "from": f"5730090{index:05d}", "type": "text",
                       "text": {"body": "hola"}}
            sha = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
            conn.execute(
                "insert into public.webhook_events (external_event_id, contact_wa_id, payload, payload_sha256, "
                "expires_at, business_phone_id) values (%s, %s, %s::jsonb, %s, now() + interval '7 days', '100200300400')",
                (f"wamid.PROC{index}", f"5730090{index:05d}", json.dumps(payload), sha),
            )


def test_two_independent_processes_claim_disjoint_sets_of_the_same_queue():
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute("truncate public.webhook_events restart identity cascade")
    _insert_events(24)

    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    processes = [
        subprocess.Popen([sys.executable, "-c", CLAIMER, TEST_DATABASE_URL, f"proc-{n}"], cwd=ROOT, env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for n in (1, 2)
    ]
    results = []
    for process in processes:
        out, err = process.communicate(timeout=90)
        assert process.returncode == 0, err
        results.append(set(json.loads(out.strip().splitlines()[-1])))

    first, second = results
    assert first.isdisjoint(second), "dos procesos reclamaron el mismo evento"
    assert len(first | second) == 24, "algún evento quedó sin reclamar"
