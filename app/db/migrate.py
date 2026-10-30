"""Aplicación explícita de migraciones SQL. Nunca la ejecutan la API ni el worker.

Uso:
    python -m app.db.migrate --dir supabase/migrations            # lista lo pendiente, no escribe
    python -m app.db.migrate --dir supabase/migrations --apply    # aplica lo pendiente

Reglas:
- Cada archivo se ejecuta en su propia transacción, junto con su registro en schema_migrations.
- Un archivo ya aplicado cuyo contenido cambió (hash distinto) detiene la operación. No se reescribe el historial.
- La URL llega por DATABASE_URL (o --database-url-env). Nunca se imprime.
- Debe ejecutarse con el rol propietario del esquema, no con lacusine_backend.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import psycopg

NAME_PATTERN = re.compile(r"^[0-9]{14}_[a-z0-9_]+\.sql$")

BOOTSTRAP_SQL = """
create table if not exists public.schema_migrations (
  name        text primary key check (char_length(name) between 1 and 200),
  applied_at  timestamptz not null default now(),
  sha256      text not null check (char_length(sha256) = 64)
);
alter table public.schema_migrations enable row level security;
revoke all on public.schema_migrations from public;
"""


class MigrationError(Exception):
    """Error de migración con código estable. El mensaje nunca contiene la URL ni el contenido del SQL."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class MigrationFile:
    name: str
    sha256: str
    sql: str


@dataclass
class MigrationReport:
    applied: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    up_to_date: list[str] = field(default_factory=list)


def load_migrations(directory: Path) -> list[MigrationFile]:
    files = []
    for path in sorted(directory.glob("*.sql")):
        if not NAME_PATTERN.match(path.name):
            raise MigrationError("migration_name_invalid")
        text = path.read_text(encoding="utf-8")
        files.append(MigrationFile(path.name, hashlib.sha256(text.encode("utf-8")).hexdigest(), text))
    names = [f.name for f in files]
    if len(names) != len(set(names)):
        raise MigrationError("migration_name_duplicated")
    return files


def run_migrations(url: str, files: list[MigrationFile], apply: bool) -> MigrationReport:
    report = MigrationReport()
    with psycopg.connect(url, autocommit=True) as conn:
        if apply:
            conn.execute(BOOTSTRAP_SQL)
        registry = conn.execute("select to_regclass('public.schema_migrations')").fetchone()[0]
        # En ensayo (dry-run) solo se lee: si el registro no existe, todo está pendiente y no se crea nada.
        recorded = {}
        if registry is not None:
            recorded = {
                row[0]: row[1]
                for row in conn.execute("select name, sha256 from public.schema_migrations").fetchall()
            }
        for migration in files:
            if migration.name in recorded:
                if recorded[migration.name] != migration.sha256:
                    raise MigrationError("migration_modified_after_apply")
                report.up_to_date.append(migration.name)
                continue
            if not apply:
                report.pending.append(migration.name)
                continue
            try:
                with conn.transaction():
                    conn.execute(migration.sql)
                    conn.execute(
                        "insert into public.schema_migrations (name, sha256) values (%s, %s)",
                        (migration.name, migration.sha256),
                    )
            except psycopg.Error:
                # Se informa el archivo y el código, nunca el detalle del error (puede contener datos).
                raise MigrationError(f"migration_failed:{migration.name}") from None
            report.applied.append(migration.name)
    return report


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Aplica migraciones SQL de forma explícita.")
    parser.add_argument("--dir", required=True, type=Path)
    parser.add_argument("--apply", action="store_true", help="aplica las migraciones pendientes")
    parser.add_argument("--database-url-env", default="DATABASE_URL")
    args = parser.parse_args(argv)

    url = os.environ.get(args.database_url_env)
    if not url:
        print(json.dumps({"error": "database_url_missing"}))
        return 2
    try:
        report = run_migrations(url, load_migrations(args.dir), apply=args.apply)
    except MigrationError as exc:
        print(json.dumps({"error": exc.code}))
        return 1
    except psycopg.Error:
        print(json.dumps({"error": "database_unavailable"}))
        return 1
    print(json.dumps({"applied": report.applied, "pending": report.pending, "up_to_date": report.up_to_date,
                      "mode": "apply" if args.apply else "dry-run"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
