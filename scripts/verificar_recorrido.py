"""Trazabilidad agregada y de SOLO LECTURA del transporte WhatsApp Web.

Cuenta, desde un instante Unix, los eventos de la línea web en cada etapa de la base. No muestra contenido,
números de contacto ni identificadores de chat. La sesión es de solo lectura.

Uso:
    .\\.venv\\Scripts\\python.exe scripts\\verificar_recorrido.py --since <unix_utc> --line 573153863179
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.env import load_local_env  # noqa: E402


def _database_url() -> str:
    values: dict[str, str] = {}
    load_local_env(ROOT / ".env", environ=values)
    import os

    url = os.environ.get("DATABASE_URL") or values.get("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL no disponible")
    return url


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", type=int, required=True, help="instante Unix UTC desde el que contar")
    parser.add_argument("--line", required=True, help="identificador de la línea web")
    args = parser.parse_args()

    import psycopg

    queries = {
        "webhook_events (recepción registrada)": (
            "select processing_status, count(*) from public.webhook_events "
            "where business_phone_id = %s and received_at >= to_timestamp(%s) group by 1 order by 1",
            (args.line, args.since)),
        "outbound_messages (salida preparada/enviada)": (
            "select delivery_status, count(*) from public.outbound_messages "
            "where business_phone_id = %s and created_at >= to_timestamp(%s) group by 1 order by 1",
            (args.line, args.since)),
        "llm_usage_events (llamadas al modelo)": (
            "select provider, status, count(*) from public.llm_usage_events "
            "where created_at >= to_timestamp(%s) group by 1, 2 order by 1, 2",
            (args.since,)),
    }
    with psycopg.connect(_database_url(), autocommit=True, connect_timeout=15,
                         options="-c default_transaction_read_only=on") as conn:
        for title, (sql, params) in queries.items():
            print(f"== {title}")
            rows = conn.execute(sql, params).fetchall()
            if not rows:
                print("   (sin eventos)")
            for row in rows:
                print("   " + " | ".join(str(v) for v in row))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
