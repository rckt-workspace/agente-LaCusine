-- DIAGNÓSTICO DE SOLO LECTURA para el despliegue de las migraciones 16, 17 y 18 desde Supabase SQL Editor.
--
-- Ejecutar en el SQL Editor del proyecto vpnnripzdfdwjexkgdnk. Solo contiene SELECT: no crea, modifica ni borra nada.
-- Pegar el bloque completo y revisar cada resultado antes de decidir. No continuar con el despliegue si algo no coincide.

-- 1. Identidad de la base y del servidor.
select current_database() as base,
       current_user as usuario,
       current_setting('server_version') as version_servidor,
       current_setting('server_version_num')::int / 10000 as version_mayor;

-- 2. Estructura real del historial de Supabase CLI (no se asume).
select column_name, data_type, is_nullable, column_default
from information_schema.columns
where table_schema = 'supabase_migrations' and table_name = 'schema_migrations'
order by ordinal_position;

-- 3. Constraints y claves primarias del historial.
select conname, contype, pg_get_constraintdef(oid) as definicion
from pg_constraint
where conrelid = 'supabase_migrations.schema_migrations'::regclass
order by conname;

-- 4. Registros existentes del historial (versión, nombre y número de sentencias registradas).
select version, name, coalesce(array_length(statements, 1), -1) as sentencias_registradas
from supabase_migrations.schema_migrations
order by version;

-- 5. Estado de las migraciones 16, 17 y 18 en el esquema (deben estar ausentes o sin efecto).
select
  to_regclass('public.analysis_budget_reservations') as tabla_17,
  to_regclass('public.schema_migrations') as registro_runner_python,
  (select count(*) from pg_policies where schemaname = 'public'
     and tablename in ('commercial_events','lead_stage_history','sales','sale_products',
                       'sale_status_events','analytics_daily_counts')) as politicas_16_actuales,
  (select string_agg(conname || '=' || confdeltype::text, ', ' order by conname) from pg_constraint
     where conname in ('commercial_events_conversation_id_fkey','commercial_events_product_id_fkey',
                       'sales_conversation_id_fkey')) as claves_18_actuales,
  (select count(*) from pg_constraint where conname = 'analysis_budget_released_zero') as restriccion_17_actual;
-- Esperado: tabla_17 = null, registro_runner_python = null, politicas_16_actuales = 0,
--           claves_18_actuales = las tres con 'n', restriccion_17_actual = 0.

-- 6. Conteos de referencia (no se modifican durante el despliegue).
select 'products' as tabla, count(*) from public.products
union all select 'webhook_events', count(*) from public.webhook_events
union all select 'conversations', count(*) from public.conversations
union all select 'commercial_events', count(*) from public.commercial_events
union all select 'sales', count(*) from public.sales;
-- Esperado: products = 349. Los demás valores se anotan y deben conservarse tal cual tras el despliegue.
