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
        # check: valida cada conexión antes de entregarla. Tras un corte de red, una conexión muerta se descarta
        # en lugar de provocar un error en la operación que la recibe.
        check=ConnectionPool.check_connection,
        # Rotación: cierra conexiones inactivas (el pooler de Supabase las cierra) y renueva las antiguas.
        max_idle=60,
        max_lifetime=1800,
        # Reconexión en segundo plano acotada: si la base vuelve, el pool recupera conexiones sin reiniciar el proceso.
        reconnect_timeout=60,
        open=False,
        kwargs={"autocommit": True, "prepare_threshold": None},
    )
