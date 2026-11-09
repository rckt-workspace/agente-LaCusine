"""Recuperación del pool ante la pérdida de conexiones en PostgreSQL desechable (TEST_DATABASE_URL).

Simula un corte: se terminan en servidor las conexiones del pool. Con validación de conexiones, la siguiente
operación debe funcionar sin reiniciar el proceso. No toca datos de negocio.
"""

from __future__ import annotations

import psycopg
import pytest

from app.db.pool import create_pool
from tests.integration.test_postgres_persistence import TEST_DATABASE_URL

pytestmark = pytest.mark.integration

if not TEST_DATABASE_URL:
    pytest.skip("TEST_DATABASE_URL no configurada", allow_module_level=True)


def _terminate_pool_backends(pool, owner_url: str) -> int:
    with pool.connection() as held:
        pids = [row[0] for row in held.execute("select pg_backend_pid()").fetchall()]
    with psycopg.connect(owner_url, autocommit=True) as admin:
        killed = 0
        for pid in pids:
            admin.execute("select pg_terminate_backend(%s)", (pid,)).fetchone()
            killed += 1
    return killed


def test_the_pool_recovers_after_the_server_closes_its_connections():
    pool = create_pool(TEST_DATABASE_URL, min_size=1, max_size=2)
    pool.open(wait=True, timeout=10)
    try:
        with pool.connection() as conn:
            assert conn.execute("select 1").fetchone()[0] == 1
        # Matar la conexión que ya está en el pool: la siguiente petición debe validarla y reemplazarla.
        with pool.connection() as conn:
            pid = conn.execute("select pg_backend_pid()").fetchone()[0]
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as admin:
            admin.execute("select pg_terminate_backend(%s)", (pid,)).fetchone()
        with pool.connection() as conn:
            assert conn.execute("select 1").fetchone()[0] == 1
    finally:
        pool.close()


def test_a_connection_terminated_mid_pool_life_is_replaced_for_the_next_operation():
    pool = create_pool(TEST_DATABASE_URL, min_size=1, max_size=3)
    pool.open(wait=True, timeout=10)
    try:
        killed = _terminate_pool_backends(pool, TEST_DATABASE_URL)
        assert killed >= 1
        for _ in range(3):
            with pool.connection() as conn:
                assert conn.execute("select 1").fetchone()[0] == 1
    finally:
        pool.close()
