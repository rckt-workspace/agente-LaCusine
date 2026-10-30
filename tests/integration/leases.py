"""Envejece los leases en la base de pruebas en lugar de esperar con time.sleep.

La espera de Python y el reloj de las transacciones de PostgreSQL no coinciden de forma fiable:
una prueba que dormía 1.2 s con un lease de 1 s fallaba de forma intermitente. Un lease vencido
se simula ahora con una sentencia, de modo que el resultado no depende de la velocidad de la máquina.
"""

import psycopg

EXPIRE_SQL = (
    "update public.outbound_messages set lease_expires_at = now() - interval '1 second' "
    "where lease_token is not null",
    "update public.webhook_events set lease_expires_at = now() - interval '1 second' "
    "where lease_token is not null",
    "update public.contact_leases set lease_expires_at = now() - interval '1 second'",
)


def age_all_leases(url: str) -> None:
    with psycopg.connect(url, autocommit=True) as conn:
        for sql in EXPIRE_SQL:
            conn.execute(sql)
