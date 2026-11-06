"""Configura la contraseña del rol de aplicación lacusine_app en Supabase y actualiza DATABASE_URL en .env local.

Ejecución (en tu PC, PowerShell, desde la raíz del repositorio):
    .\\.venv\\Scripts\\python.exe -m app.db.app_role_setup --project-ref <ref> --pooler-host <host>

Garantías:
  * La contraseña administrativa se pide con entrada oculta. Nunca se imprime, se guarda ni se pasa por argumentos.
  * Antes de cambiar nada se verifica que la sesión administrativa puede crear roles. Si no, se sale sin cambios.
  * La contraseña nueva es aleatoria (secrets). Se escribe en .env solo después de comprobar el inicio de sesión
    como lacusine_app. Nunca se usa SUPABASE_SECRET_KEY como contraseña de PostgreSQL.
  * DATABASE_URL se reemplaza en su sitio. Si la variable aparece más de una vez, se rechaza para no duplicarla.
  * Solo se imprimen códigos y conteos. Nunca cadenas de conexión ni contraseñas.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import secrets
import sys
from pathlib import Path
from urllib.parse import quote

import psycopg
from psycopg import sql

APP_ROLE = "lacusine_app"
BACKEND_GROUP = "lacusine_backend"
ADMIN_DATABASE = "postgres"
DEFAULT_PORT = 5432
ENV_KEY = "DATABASE_URL"


class SetupError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def build_app_database_url(host: str, port: int, project_ref: str, password: str) -> str:
    """Session Pooler de Supabase: el usuario del pooler es <rol>.<ref>. SSL obligatorio."""
    user = quote(f"{APP_ROLE}.{project_ref}", safe="")
    secret = quote(password, safe="")
    return f"postgresql://{user}:{secret}@{host}:{port}/{ADMIN_DATABASE}?sslmode=require"


def set_env_value(text: str, key: str, value: str) -> str:
    """Reemplaza una única ocurrencia de KEY=... conservando el resto del archivo. Añade la clave si no existe."""
    lines = text.splitlines(keepends=True)
    positions = [i for i, line in enumerate(lines) if line.lstrip().startswith(f"{key}=")]
    if len(positions) > 1:
        raise SetupError("env_key_duplicated")
    newline = "\r\n" if any(line.endswith("\r\n") for line in lines) else "\n"
    if not positions:
        prefix = text if text.endswith(("\n", "\r")) or not text else text + newline
        return f"{prefix}{key}={value}{newline}"
    lines[positions[0]] = f"{key}={value}{newline}"
    return "".join(lines)


def _connect_admin(host: str, port: int, project_ref: str, admin_password: str) -> psycopg.Connection:
    return psycopg.connect(
        host=host, port=port, dbname=ADMIN_DATABASE, user=f"postgres.{project_ref}",
        password=admin_password, sslmode="require", connect_timeout=10, autocommit=True,
    )


def _verify_admin(conn: psycopg.Connection) -> None:
    row = conn.execute("select rolcreaterole from pg_roles where rolname = current_user").fetchone()
    if row is None or not row[0]:
        raise SetupError("admin_cannot_create_roles")
    for role in (APP_ROLE, BACKEND_GROUP):
        if conn.execute("select 1 from pg_roles where rolname = %s", (role,)).fetchone() is None:
            raise SetupError(f"role_missing:{role}")


def _set_app_password(conn: psycopg.Connection, password: str) -> None:
    conn.execute(sql.SQL("alter role {} with login password {}").format(
        sql.Identifier(APP_ROLE), sql.Literal(password)))


def _verify_app(host: str, port: int, project_ref: str, password: str) -> dict[str, object]:
    with psycopg.connect(
        host=host, port=port, dbname=ADMIN_DATABASE, user=f"{APP_ROLE}.{project_ref}",
        password=password, sslmode="require", connect_timeout=10,
    ) as conn:
        current = conn.execute("select current_user").fetchone()[0]
        attrs = conn.execute(
            "select rolsuper, rolbypassrls from pg_roles where rolname = current_user"
        ).fetchone()
        rls_tables = conn.execute(
            "select count(*) from pg_class c join pg_namespace n on n.oid = c.relnamespace "
            "where n.nspname = 'public' and c.relkind = 'r' and c.relrowsecurity"
        ).fetchone()[0]
    return {"current_user": current, "rolsuper": attrs[0], "rolbypassrls": attrs[1], "rls_tables": int(rls_tables)}


def run(host: str, port: int, project_ref: str, env_path: Path, admin_password: str) -> dict[str, object]:
    password = secrets.token_urlsafe(32)
    with _connect_admin(host, port, project_ref, admin_password) as admin:
        _verify_admin(admin)
        _set_app_password(admin, password)

    report = _verify_app(host, port, project_ref, password)
    if report["current_user"] != APP_ROLE or report["rolsuper"] or report["rolbypassrls"]:
        raise SetupError("app_role_privileges_unexpected")

    url = build_app_database_url(host, port, project_ref, password)
    original = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    updated = set_env_value(original, ENV_KEY, url)
    tmp = env_path.with_suffix(env_path.suffix + ".tmp")
    tmp.write_text(updated, encoding="utf-8")
    os.replace(tmp, env_path)
    report["env_updated"] = True
    return report


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project-ref", required=True)
    parser.add_argument("--pooler-host", required=True, help="Host del Session Pooler, sin protocolo")
    parser.add_argument("--pooler-port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--env-file", default=".env", type=Path)
    args = parser.parse_args(argv)

    host = args.pooler_host.strip()
    if host.startswith(("http://", "https://")) or "/" in host or "@" in host:
        print(json.dumps({"error": "pooler_host_invalid"}))
        return 2

    admin_password = getpass.getpass("Contraseña administrativa de Supabase (no se muestra): ")
    if not admin_password:
        print(json.dumps({"error": "admin_password_empty"}))
        return 2
    try:
        report = run(host, args.pooler_port, args.project_ref.strip(), args.env_file, admin_password)
    except SetupError as exc:
        print(json.dumps({"error": exc.code, "changed": False}))
        return 1
    except psycopg.OperationalError:
        print(json.dumps({"error": "connection_or_auth_failed", "changed": False}))
        return 1
    except psycopg.Error as exc:
        print(json.dumps({"error": "database_error", "sqlstate": exc.sqlstate, "changed": False}))
        return 1
    finally:
        admin_password = ""
    print(json.dumps({
        "current_user": report["current_user"], "rolsuper": report["rolsuper"],
        "rolbypassrls": report["rolbypassrls"], "rls_tables": report["rls_tables"],
        "env_updated": report["env_updated"], "database_url": "(configurada, no mostrada)",
    }))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
