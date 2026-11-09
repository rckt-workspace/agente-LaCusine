"""Cancelación administrativa, explícita, idempotente y auditada de respuestas de salida CONCRETAS que nunca se enviaron.

Solo cancela registros que cumplen TODO lo siguiente, comprobado dentro de la transacción:
  * delivery_status = 'pending', attempts = 0, sin lease, sin external_message_id y sin sent_at.
  * El identificador está en la lista indicada explícitamente (nunca un filtro amplio).

Nunca toca: 'uncertain', 'sending', 'sent', 'delivered', 'read', 'failed' ni otras conversaciones.
Nunca borra filas. La respuesta uncertain se deja para conciliación manual.

Modo por defecto: VISTA PREVIA (solo lectura). Para escribir hay que repetir el comando con --aplicar y el token
exacto que muestra la vista previa. Cualquier cambio entre vista previa y escritura aborta la operación.

Uso:
  .venv\\Scripts\\python.exe scripts\\cancelar_pendientes_outbox.py --id <uuid> [--id <uuid> ...]
  .venv\\Scripts\\python.exe scripts\\cancelar_pendientes_outbox.py --id <uuid> ... --aplicar --token <token>

Nunca imprime contenido de mensajes, números de teléfono ni secretos. La auditoría se escribe en state\\audit\\.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AUDIT_DIR = ROOT / "connector" / "whatsapp-webjs" / "state" / "audit"
CANCEL_REASON = "operator_cancelled_stale"
ELIGIBLE_STATUS = "pending"

SELECT_SQL = """
select id, conversation_id, delivery_status, attempts, lease_owner is not null as leased,
       external_message_id is not null as has_wamid, sent_at is not null as has_sent_at,
       last_error_code, created_at
from public.outbound_messages
where id = any(%s::uuid[])
order by created_at, id
for update
"""


def eligibility(row: dict) -> tuple[bool, str]:
    """Función pura: decide si un registro puede cancelarse y explica por qué no."""
    if row["delivery_status"] != ELIGIBLE_STATUS:
        return False, f"estado_{row['delivery_status']}"
    if row["attempts"] != 0:
        return False, "con_intentos"
    if row["leased"]:
        return False, "con_lease"
    if row["has_wamid"]:
        return False, "con_identificador_de_meta"
    if row["has_sent_at"]:
        return False, "con_fecha_de_envio"
    if row["last_error_code"] is not None:
        return False, "con_error_previo"
    return True, "elegible"


def fingerprint(rows: list[dict]) -> str:
    """Huella de la vista previa: cualquier cambio de estado o de conjunto altera el token."""
    material = "|".join(f"{r['id']}:{r['delivery_status']}:{r['attempts']}:{int(r['leased'])}" for r in sorted(rows, key=lambda r: str(r["id"])))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _row_dict(record) -> dict:
    keys = ("id", "conversation_id", "delivery_status", "attempts", "leased", "has_wamid", "has_sent_at",
            "last_error_code", "created_at")
    return dict(zip(keys, record))


def _short(value) -> str:
    return str(value)[:8]


def preview(conn, ids: list[str]) -> tuple[list[dict], list[dict]]:
    with conn.transaction():
        records = conn.execute(SELECT_SQL, (ids,)).fetchall()
    rows = [_row_dict(r) for r in records]
    missing = sorted(set(ids) - {str(r["id"]) for r in rows})
    return rows, [{"id": m, "motivo": "no_existe"} for m in missing]


def apply(conn, ids: list[str], token: str, operator: str) -> dict:
    """Escritura en UNA transacción. Recalcula elegibilidad y huella con los registros bloqueados (FOR UPDATE)."""
    with conn.transaction():
        rows = [_row_dict(r) for r in conn.execute(SELECT_SQL, (ids,)).fetchall()]
        if len(rows) != len(ids):
            raise SystemExit("abortado: algún identificador ya no existe")
        if fingerprint(rows) != token:
            raise SystemExit("abortado: el estado cambió desde la vista previa; repita la vista previa")
        blocked = [(str(r["id"]), eligibility(r)[1]) for r in rows if not eligibility(r)[0]]
        if blocked:
            raise SystemExit(f"abortado: registros no elegibles {blocked}")
        cancelled = []
        for r in rows:
            cur = conn.execute(
                "update public.outbound_messages set delivery_status = 'cancelled', last_error_code = %s, "
                "updated_at = now() where id = %s and delivery_status = 'pending' and attempts = 0 "
                "and lease_owner is null and external_message_id is null and sent_at is null",
                (CANCEL_REASON, r["id"]),
            )
            if cur.rowcount == 1:
                cancelled.append(str(r["id"]))
        if len(cancelled) != len(ids):
            raise SystemExit("abortado: una actualización no afectó exactamente a un registro")
    return {"cancelled": cancelled, "fingerprint": token}


def audit(event: dict) -> Path:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    path = AUDIT_DIR / f"outbox-cancelacion-{datetime.now(timezone.utc):%Y%m%d}.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--id", action="append", required=True, help="UUID de outbound_messages a cancelar (repetible)")
    parser.add_argument("--aplicar", action="store_true", help="Escribe en la base (requiere --token)")
    parser.add_argument("--token", help="Huella que muestra la vista previa")
    args = parser.parse_args(argv)

    try:
        ids = sorted({str(uuid.UUID(i)) for i in args.id})
    except ValueError:
        print("identificador no válido")
        return 2

    sys.path.insert(0, str(ROOT))
    from app.core.config import Settings
    from app.core.env import load_if_enabled
    import psycopg

    load_if_enabled(ROOT / ".env")
    settings = Settings.from_env()
    if not settings.database_url:
        print("DATABASE_URL ausente")
        return 2

    with psycopg.connect(settings.database_url, connect_timeout=10, autocommit=False) as conn:
        rows, missing = preview(conn, ids)
        conn.rollback()
        print(f"== Vista previa: {len(rows)} registro(s) encontrado(s), {len(missing)} no encontrado(s)")
        plan = []
        for r in rows:
            ok, reason = eligibility(r)
            plan.append({"id": _short(r["id"]), "conv": _short(r["conversation_id"]), "estado": r["delivery_status"],
                         "intentos": r["attempts"], "elegible": ok, "motivo": reason})
            print(f"  id={_short(r['id'])} conv={_short(r['conversation_id'])} estado={r['delivery_status']} "
                  f"intentos={r['attempts']} elegible={ok} motivo={reason}")
        for m in missing:
            print(f"  id={_short(m['id'])} no_existe")
        token = fingerprint(rows) if rows else ""
        eligible_ids = [str(r["id"]) for r in rows if eligibility(r)[0]]
        print(f"Elegibles: {len(eligible_ids)} de {len(ids)} solicitados. Token de esta vista previa: {token}")

        if not args.aplicar:
            print("Solo vista previa: no se ha escrito nada. Para cancelar, repita con --aplicar --token <token>.")
            return 0
        if missing or len(eligible_ids) != len(ids):
            print("abortado: hay identificadores no encontrados o no elegibles; no se escribe nada.")
            return 1
        if args.token != token:
            print("abortado: el token no coincide con la vista previa actual.")
            return 1
        operator = getpass.getuser()
        before = {str(r["id"]): r["delivery_status"] for r in rows}
        result = apply(conn, ids, token, operator)
        conn.commit()
        path = audit({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "operador": operator,
            "accion": "cancelar_pendientes_obsoletos",
            "ids": ids,
            "estado_previo": before,
            "huella": token,
            "canceladas": result["cancelled"],
            "motivo": CANCEL_REASON,
        })
        print(f"Cancelados: {len(result['cancelled'])}. Auditoría: {path}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
