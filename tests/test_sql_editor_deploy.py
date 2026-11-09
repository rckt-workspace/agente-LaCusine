"""Garantías estáticas del procedimiento de despliegue por SQL Editor (sin base de datos).

La validación funcional (transacción, reversión, CLI) se hizo en PostgreSQL 17 desechable; ver
docs/despliegue/README.md. Estas pruebas fijan que el script committeado es el generado a partir de las
migraciones actuales, que contiene sus archivos íntegros y que el diagnóstico es de solo lectura.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEPLOY_DIR = REPO / "docs" / "despliegue"
DEPLOY = DEPLOY_DIR / "despliegue-16-18-sql-editor.sql"
DIAGNOSTIC = DEPLOY_DIR / "00-diagnostico-solo-lectura.sql"
HISTORY = DEPLOY_DIR / "historial-16-18.sql"
NEW_FILES = [
    "20261105120000_commercial_analytics_rls_policies.sql",
    "20261105130000_business_report_budget.sql",
    "20261105140000_commercial_history_fk_restrict.sql",
]


def _builder():
    spec = importlib.util.spec_from_file_location("builder", REPO / "scripts" / "build_sql_editor_deploy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_committed_deploy_script_matches_the_generator():
    # Si cambia una migración o el historial, hay que regenerar el script: no editarlo a mano.
    assert DEPLOY.read_text(encoding="utf-8") == _builder().build("postgres", 349)


def test_deploy_script_embeds_each_new_migration_verbatim():
    text = DEPLOY.read_text(encoding="utf-8")
    for name in NEW_FILES:
        body = (REPO / "supabase" / "migrations" / name).read_text(encoding="utf-8").rstrip("\n")
        assert body in text, name


def test_deploy_script_is_one_transaction_with_prechecks_and_postchecks():
    text = DEPLOY.read_text(encoding="utf-8")
    code_lines = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("--")]
    assert code_lines[0] == "begin;"
    assert code_lines[-1] == "commit;"
    assert text.count("do $pre$") == 1 and text.count("do $post$") == 1
    # Solo dos líneas de control de transacción a nivel superior. Los 'begin' de los bloques DO son plpgsql.
    assert sum(1 for line in text.splitlines() if line.strip() in ("begin;", "commit;", "rollback;")) == 2
    assert "rollback;" not in text.replace("-- Ante cualquier error: ejecutar 'rollback;'", "")


def test_history_rows_are_the_three_cli_rows_for_16_17_18():
    history = HISTORY.read_text(encoding="utf-8")
    # Cada INSERT ocupa varias líneas (las sentencias contienen saltos de línea): se comprueba sobre el texto completo.
    assert history.count("insert into supabase_migrations.schema_migrations (version, name, statements) values (") == 3
    assert history.count("]::text[]);") == 3
    for version in ("20261105120000", "20261105130000", "20261105140000"):
        assert f"values ('{version}'," in history, version
    # Las mismas filas deben estar en el script de despliegue, byte a byte.
    assert history.split("\n", 2)[2].strip() in DEPLOY.read_text(encoding="utf-8")


def test_diagnostic_script_contains_only_reads():
    forbidden = re.compile(r"^\s*(insert|update|delete|alter|create|drop|grant|revoke|truncate|do|begin|commit|rollback)\b",
                           re.IGNORECASE)
    for line in DIAGNOSTIC.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        assert not forbidden.match(line), line
    assert "select" in DIAGNOSTIC.read_text(encoding="utf-8").lower()
