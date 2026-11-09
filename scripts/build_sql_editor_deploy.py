"""Genera el script transaccional de despliegue de las migraciones 16, 17 y 18 para Supabase SQL Editor.

El script incorpora, sin transcribirlos a mano, los archivos de supabase/migrations tal cual, y las filas del
historial de Supabase CLI generadas por la propia CLI (docs/despliegue/historial-16-18.sql).

Uso:
    python scripts/build_sql_editor_deploy.py                      # producción -> docs/despliegue/despliegue-16-18-sql-editor.sql
    python scripts/build_sql_editor_deploy.py --database X --products N --output RUTA   # variante de prueba desechable

La variante de prueba cambia SOLO el nombre de la base esperada y el número de productos esperado.
"""

from __future__ import annotations

import argparse
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MIGRATIONS = REPO / "supabase" / "migrations"
HISTORY_INSERTS = REPO / "docs" / "despliegue" / "historial-16-18.sql"
DEFAULT_OUTPUT = REPO / "docs" / "despliegue" / "despliegue-16-18-sql-editor.sql"

PREVIOUS_VERSIONS = [
    "20261027100000", "20261027100100", "20261027100200", "20261028100000", "20261028100100",
    "20261028100200", "20261029100000", "20261029100100", "20261030100000", "20261031100000",
    "20261101100000", "20261102100000", "20261103100000", "20261105100000", "20261105110000",
]
NEW_FILES = [
    "20261105120000_commercial_analytics_rls_policies.sql",
    "20261105130000_business_report_budget.sql",
    "20261105140000_commercial_history_fk_restrict.sql",
]
RLS_TABLES = ("'commercial_events','lead_stage_history','sales','sale_products','sale_status_events',"
              "'analytics_daily_counts','analysis_budget_reservations'")
FK_NAMES = ("'commercial_events_conversation_id_fkey','commercial_events_product_id_fkey',"
            "'sales_conversation_id_fkey'")

PRE_CHECKS = """
-- ============================================================================
-- COMPROBACIONES PREVIAS. Si cualquiera falla, se lanza una excepción: la transacción se aborta y nada se aplica.
-- ============================================================================
do $pre$
declare
  v_expected_db text := '{database}';           -- PARAM_DB
  v_expected_products integer := {products};    -- PARAM_PRODUCTS
  v_expected_versions text[] := array[{versions}];
  v_actual_versions text[];
  v_columns text[];
begin
  if current_database() <> v_expected_db then
    raise exception 'PRE: base inesperada (%). Abortado.', current_database();
  end if;
  if current_setting('server_version_num')::int / 10000 <> 17 then
    raise exception 'PRE: versión de PostgreSQL distinta de 17. Abortado.';
  end if;

  select array_agg(column_name || ':' || data_type || ':' || is_nullable order by ordinal_position)
    into v_columns
  from information_schema.columns
  where table_schema = 'supabase_migrations' and table_name = 'schema_migrations';
  if v_columns is distinct from array['version:text:NO', 'statements:ARRAY:YES', 'name:text:YES'] then
    raise exception 'PRE: estructura del historial de Supabase CLI distinta de la esperada: %. Abortado.', v_columns;
  end if;

  select array_agg(version order by version) into v_actual_versions from supabase_migrations.schema_migrations;
  if v_actual_versions is distinct from v_expected_versions then
    raise exception 'PRE: el historial no contiene exactamente las versiones 1–15. Encontrado: %. Abortado.', v_actual_versions;
  end if;

  if to_regclass('public.analysis_budget_reservations') is not null then
    raise exception 'PRE: la tabla de la migración 17 ya existe. Abortado.';
  end if;
  if to_regclass('public.schema_migrations') is not null then
    raise exception 'PRE: existe public.schema_migrations (runner Python). No se crea ni se usa en Cloud. Abortado.';
  end if;
  if exists (select 1 from pg_policies where schemaname = 'public' and tablename in ({rls_tables})) then
    raise exception 'PRE: ya existen políticas sobre las tablas de 16/17. Abortado.';
  end if;
  if exists (select 1 from pg_constraint where conname = 'analysis_budget_released_zero') then
    raise exception 'PRE: la restricción de la 17 ya existe. Abortado.';
  end if;
  if (select count(*) from pg_constraint where conname in ({fk_names}) and confdeltype = 'n') <> 3 then
    raise exception 'PRE: las claves de la 18 no están en ON DELETE SET NULL. Abortado.';
  end if;
  if (select count(*) from public.products) <> v_expected_products then
    raise exception 'PRE: número de productos distinto del esperado. Abortado.';
  end if;

  -- Huella de datos: se compara al final. Nada en este script modifica estas tablas.
  create temp table deploy_baseline on commit drop as
    select (select count(*) from public.products)          as products,
           (select count(*) from public.webhook_events)    as webhook_events,
           (select count(*) from public.conversations)     as conversations,
           (select count(*) from public.commercial_events) as commercial_events,
           (select count(*) from public.sales)             as sales;
end
$pre$;
"""

POST_CHECKS = """
-- ============================================================================
-- COMPROBACIONES POSTERIORES. Si cualquiera falla, la transacción se revierte completa.
-- ============================================================================
do $post$
declare
  v_versions text[];
  v_expected text[] := array[{versions}, '20261105120000', '20261105130000', '20261105140000'];
  v_cur record;
  v_base record;
begin
  if (select count(*) from pg_policies where schemaname = 'public' and tablename in ({rls_tables})) <> 21 then
    raise exception 'POST: se esperaban 21 políticas RLS. Revertido.';
  end if;
  if exists (select 1 from pg_policies where schemaname = 'public' and tablename in ({rls_tables})
             and roles && array['anon', 'authenticated', 'public']::name[]) then
    raise exception 'POST: alguna política aplica a anon, authenticated o public. Revertido.';
  end if;
  if (select count(*) from pg_constraint where conname = 'analysis_budget_released_zero' and contype = 'c') <> 1 then
    raise exception 'POST: falta la restricción presupuestaria de la 17. Revertido.';
  end if;
  if (select count(*) from pg_constraint where conname in ({fk_names}) and confdeltype = 'r') <> 3 then
    raise exception 'POST: las tres claves de la 18 no están en ON DELETE RESTRICT. Revertido.';
  end if;
  if to_regclass('public.schema_migrations') is not null then
    raise exception 'POST: apareció public.schema_migrations. Revertido.';
  end if;

  select array_agg(version order by version) into v_versions from supabase_migrations.schema_migrations;
  if v_versions is distinct from (select array_agg(x order by x) from unnest(v_expected) as x) then
    raise exception 'POST: el historial no contiene exactamente 1–18. Revertido.';
  end if;
  if exists (select 1 from supabase_migrations.schema_migrations
             where version in ('20261105120000','20261105130000','20261105140000') and statements is null) then
    raise exception 'POST: alguna fila nueva del historial no tiene sentencias registradas. Revertido.';
  end if;

  select (select count(*) from public.products)          as products,
         (select count(*) from public.webhook_events)    as webhook_events,
         (select count(*) from public.conversations)     as conversations,
         (select count(*) from public.commercial_events) as commercial_events,
         (select count(*) from public.sales)             as sales
    into v_cur;
  select * into v_base from deploy_baseline;
  if v_cur is distinct from v_base then
    raise exception 'POST: los conteos de datos cambiaron durante el despliegue. Revertido.';
  end if;
end
$post$;
"""


def build(database: str, products: int) -> str:
    versions = ", ".join(f"'{v}'" for v in PREVIOUS_VERSIONS)
    parts = [
        "-- DESPLIEGUE TRANSACCIONAL de las migraciones 16, 17 y 18 (Supabase SQL Editor).",
        "-- GENERADO por scripts/build_sql_editor_deploy.py. No editar a mano: regenerar.",
        "-- Requisitos: diagnóstico 00-diagnostico-solo-lectura.sql revisado; respaldo verificado; worker detenido.",
        "-- Ante cualquier error: ejecutar 'rollback;' y NO continuar. Un error revierte toda la transacción.",
        "",
        "begin;",
        PRE_CHECKS.format(database=database, products=products, versions=versions,
                          rls_tables=RLS_TABLES, fk_names=FK_NAMES),
    ]
    for name in NEW_FILES:
        text = (MIGRATIONS / name).read_text(encoding="utf-8")
        parts.append(f"\n-- ===== INICIO {name} (copia íntegra de supabase/migrations) =====\n")
        parts.append(text.rstrip("\n") + "\n")
        parts.append(f"-- ===== FIN {name} =====\n")
    parts.append("\n-- ============================================================================\n"
                 "-- REGISTRO EN EL HISTORIAL DE SUPABASE CLI (filas generadas por la propia CLI)\n"
                 "-- ============================================================================\n")
    parts.append(HISTORY_INSERTS.read_text(encoding="utf-8").rstrip("\n") + "\n")
    parts.append(POST_CHECKS.format(versions=versions, rls_tables=RLS_TABLES, fk_names=FK_NAMES))
    parts.append("\ncommit;\n")
    return "\n".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default="postgres")
    parser.add_argument("--products", type=int, default=349)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(build(args.database, args.products), encoding="utf-8", newline="\n")
    print(f"escrito: {args.output}")


if __name__ == "__main__":
    main()
