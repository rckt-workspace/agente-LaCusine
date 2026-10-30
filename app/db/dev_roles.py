"""Crea los roles de login de DESARROLLO LOCAL y los asocia a los grupos de mínimo privilegio.

Solo para el contenedor de desarrollo. Se niega a ejecutarse fuera de un host local o con una base que no
termine en `_dev` o `_test`. Las contraseñas llegan por variables de entorno y nunca se imprimen.

Uso (dentro del contenedor de migración, después de `app.db.migrate --apply`):
    python -m app.db.dev_roles
"""

from __future__ import annotations

import json
import os
import sys
from urllib.parse import urlsplit

import psycopg
from psycopg import sql

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "db"}
ROLES = (
    ("lacusine_app", "lacusine_backend", "LACUSINE_DEV_APP_PASSWORD"),
    ("lacusine_admin", "lacusine_admin_ops", "LACUSINE_DEV_ADMIN_PASSWORD"),
)


class DevRolesError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def check_target(url: str) -> None:
    parts = urlsplit(url)
    if (parts.hostname or "") not in LOCAL_HOSTS:
        raise DevRolesError("dev_roles_remote_host_refused")
    database = parts.path.lstrip("/")
    if not (database.endswith("_dev") or database.endswith("_test")):
        raise DevRolesError("dev_roles_database_name_refused")
    if os.environ.get("LACUSINE_ENVIRONMENT") != "local":
        raise DevRolesError("dev_roles_environment_refused")


def create_roles(url: str, environ: dict[str, str]) -> list[str]:
    check_target(url)
    created = []
    with psycopg.connect(url, autocommit=True) as conn:
        for login, group, password_var in ROLES:
            password = environ.get(password_var)
            if not password or len(password) < 12:
                raise DevRolesError(f"password_missing:{password_var}")
            exists = conn.execute("select 1 from pg_roles where rolname = %s", (login,)).fetchone()
            # CREATE/ALTER ROLE no admiten parámetros enlazados: la contraseña va como literal escapado.
            verb = "alter role" if exists else "create role"
            conn.execute(
                sql.SQL("{} {} with login password {}").format(
                    sql.SQL(verb), sql.Identifier(login), sql.Literal(password)
                )
            )
            conn.execute(sql.SQL("grant {} to {}").format(sql.Identifier(group), sql.Identifier(login)))
            created.append(login)
    return created


def _main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print(json.dumps({"error": "database_url_missing"}))
        return 2
    try:
        created = create_roles(url, dict(os.environ))
    except DevRolesError as exc:
        print(json.dumps({"error": exc.code}))
        return 1
    except psycopg.Error:
        print(json.dumps({"error": "database_unavailable"}))
        return 1
    print(json.dumps({"roles": created}))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
