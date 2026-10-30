"""Utilidad administrativa interna de conciliación. No es un endpoint HTTP.

Uso (PowerShell):
  $env:LACUSINE_ENVIRONMENT = "pilot"
  $env:LACUSINE_ADMIN_DATABASE_URL = "postgresql://..."      # rol de mínimo privilegio, distinto de DATABASE_URL
  python -m app.admin.cli --environment pilot --operator "nombre" status

Guardas (todas obligatorias para mutar; la lectura exige solo entorno y rol):
  1. LACUSINE_ADMIN_DATABASE_URL definida y distinta de DATABASE_URL.
  2. --environment coincide con LACUSINE_ENVIRONMENT (doble confirmación del entorno).
  3. Un host de nube (Supabase) exige LACUSINE_ADMIN_ALLOW_REMOTE=true.
  4. Para mutar: LACUSINE_ADMIN_ENABLED=true, LACUSINE_ADMIN_ROLE igual al usuario de la conexión,
     --confirm igual al identificador del caso, y en 'production' además LACUSINE_ADMIN_ALLOW_PRODUCTION=true.
  5. Una confirmación textual sola no basta: las guardas 1 a 4 se comprueban juntas.

La salida solo contiene identificadores, estados, códigos y conteos. Nunca texto de clientes ni secretos.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from decimal import Decimal, InvalidOperation
from uuid import UUID

from psycopg import conninfo

from app.admin.policy import AdminPolicyError
from app.admin.service import AdminContext, AdminService
from app.db.pool import create_pool

ENVIRONMENTS = ("local", "test", "pilot", "production")
MUTATIONS = {"resolve-event", "resolve-outbound", "resolve-reservation", "resolve-handoff", "resume-conversation"}
CLOUD_HOST_MARKERS = ("supabase",)


class GuardError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _env_true(name: str, environ: dict[str, str]) -> bool:
    return environ.get(name, "").strip().lower() in {"1", "true", "yes", "si", "sí", "on"}


def check_guards(args: argparse.Namespace, environ: dict[str, str]) -> str:
    """Devuelve la URL administrativa si todas las guardas aplican. Lanza GuardError si no."""
    url = environ.get("LACUSINE_ADMIN_DATABASE_URL")
    if not url:
        raise GuardError("admin_database_url_missing")
    if url == environ.get("DATABASE_URL"):
        raise GuardError("admin_url_must_differ_from_app_url")
    if environ.get("LACUSINE_ENVIRONMENT") not in ENVIRONMENTS:
        raise GuardError("environment_not_set")
    if args.environment != environ.get("LACUSINE_ENVIRONMENT"):
        raise GuardError("environment_mismatch")
    host = str(conninfo.conninfo_to_dict(url).get("host") or "").lower()
    if any(marker in host for marker in CLOUD_HOST_MARKERS) and not _env_true("LACUSINE_ADMIN_ALLOW_REMOTE", environ):
        raise GuardError("remote_host_not_allowed")
    if args.command in MUTATIONS:
        if not isinstance(args.id, UUID):
            raise GuardError("id_must_be_uuid")
        if not _env_true("LACUSINE_ADMIN_ENABLED", environ):
            raise GuardError("mutations_disabled")
        if args.environment == "production" and not _env_true("LACUSINE_ADMIN_ALLOW_PRODUCTION", environ):
            raise GuardError("production_not_allowed")
        if not environ.get("LACUSINE_ADMIN_ROLE"):
            raise GuardError("admin_role_missing")
        if not args.confirm or args.confirm.strip() != str(args.id):
            raise GuardError("confirmation_mismatch")
    return url


def _role_matches(pool, expected: str) -> bool:
    with pool.connection() as conn:
        return conn.execute("select current_user").fetchone()[0] == expected


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.admin.cli", description="Conciliación operativa de RCKT.")
    parser.add_argument("--environment", required=True, choices=ENVIRONMENTS)
    parser.add_argument("--operator", required=True, help="Identidad de la persona que decide. Queda en auditoría.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Conteos por estado (solo lectura).")

    inspect = sub.add_parser("inspect", help="Metadatos de un caso (solo lectura).")
    inspect.add_argument("--type", required=True, choices=("webhook_event", "outbound_message",
                                                           "budget_reservation", "human_handoff", "conversation"))
    inspect.add_argument("--id", required=True, type=_uuid_or_text)

    audit = sub.add_parser("audit", help="Historial de intervenciones de un caso (solo lectura).")
    audit.add_argument("--type", required=True)
    audit.add_argument("--id", required=True, type=UUID)

    for name in ("resolve-event", "resolve-outbound", "resolve-reservation", "resolve-handoff",
                 "resume-conversation"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--id", required=True, type=_uuid_or_text)
        cmd.add_argument("--confirm", required=True, help="Debe repetir exactamente el identificador del caso.")
        cmd.add_argument("--reason", required=True)
        if name in ("resolve-event", "resolve-outbound", "resolve-reservation", "resolve-handoff"):
            cmd.add_argument("--to", required=True)
        if name in ("resolve-outbound", "resolve-reservation"):
            cmd.add_argument("--evidence", help="Referencia externa: wamid de Meta, prueba de no facturación, etc.")
        if name == "resolve-reservation":
            cmd.add_argument("--actual-usd", type=_decimal)
        if name == "resolve-handoff":
            cmd.add_argument("--advisor")
    return parser


def _uuid_or_text(value: str) -> UUID | str:
    try:
        return UUID(value)
    except ValueError:
        return value


def _decimal(value: str) -> Decimal:
    try:
        return Decimal(value)
    except InvalidOperation:
        raise argparse.ArgumentTypeError("importe inválido") from None


def run(argv: list[str] | None = None, environ: dict[str, str] | None = None, out=sys.stdout) -> int:
    env = dict(os.environ) if environ is None else environ
    args = build_parser().parse_args(argv)
    try:
        url = check_guards(args, env)
    except GuardError as exc:
        print(json.dumps({"ok": False, "error": exc.code}), file=out)
        return 2

    pool = create_pool(url, min_size=1, max_size=2)
    pool.open(wait=True, timeout=10)
    try:
        if args.command in MUTATIONS and not _role_matches(pool, env["LACUSINE_ADMIN_ROLE"]):
            print(json.dumps({"ok": False, "error": "role_mismatch"}), file=out)
            return 2
        service = AdminService(pool, AdminContext(operator=args.operator, environment=args.environment))
        result = _dispatch(service, args)
        print(json.dumps({"ok": True, **result}, ensure_ascii=False, default=str), file=out)
        return 0
    except AdminPolicyError as exc:
        print(json.dumps({"ok": False, "error": exc.code}), file=out)
        return 1
    finally:
        pool.close()


def _dispatch(service: AdminService, args: argparse.Namespace) -> dict:
    if args.command == "status":
        return {"counts": service.counts()}
    if args.command == "inspect":
        return {"case": service.inspect(args.type, args.id)}
    if args.command == "audit":
        return {"trail": service.audit_trail(args.type, args.id)}
    if args.command == "resolve-event":
        service.resolve_event(args.id, args.to, args.reason)
    elif args.command == "resolve-outbound":
        service.resolve_outbound(args.id, args.to, args.reason, args.evidence)
    elif args.command == "resolve-reservation":
        service.resolve_reservation(args.id, args.to, args.reason, args.evidence, args.actual_usd)
    elif args.command == "resolve-handoff":
        service.resolve_handoff(args.id, args.to, args.reason, args.advisor)
    elif args.command == "resume-conversation":
        service.resume_conversation(args.id, args.reason)
    return {"applied": args.command, "id": str(args.id)}


if __name__ == "__main__":
    raise SystemExit(run())
