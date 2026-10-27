"""Pool de conexiones a PostgreSQL (Supabase u otra instancia compatible)."""

from __future__ import annotations

from psycopg_pool import ConnectionPool


def create_pool(database_url: str, min_size: int = 1, max_size: int = 5) -> ConnectionPool:
    # prepare_threshold=None: el pooler de transacciones de Supabase no admite sentencias preparadas.
    # El pool no se abre aquí; lo abre el ciclo de vida de la aplicación.
    return ConnectionPool(
        conninfo=database_url,
        min_size=min_size,
        max_size=max_size,
        timeout=5,
        open=False,
        kwargs={"autocommit": True, "prepare_threshold": None},
    )
