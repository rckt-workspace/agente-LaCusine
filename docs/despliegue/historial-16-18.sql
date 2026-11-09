-- Filas del historial de Supabase CLI para 16, 17 y 18, tal como las escribe la propia CLI
-- (generadas en una base desechable con 'migration repair', no escritas a mano).
insert into supabase_migrations.schema_migrations (version, name, statements) values ('20261105120000', 'commercial_analytics_rls_policies', array['-- Políticas RLS para las tablas comerciales y de analítica creadas en 20261105100000 y 20261105110000.
--
-- Motivo: esas migraciones activan RLS pero no crean políticas. Los roles de la aplicación no son
-- propietarios ni tienen BYPASSRLS, así que con RLS activo y sin política el backend no puede leer ni
-- insertar (los GRANT solos no bastan). Esta migración solo añade políticas; no cambia tablas, datos ni privilegios.
--
-- Mínimo privilegio: las tablas de historial siguen siendo solo-anexar (triggers forbid_history_changes),
-- por eso no reciben política de update ni delete. analytics_daily_counts sí necesita update (consolidación).

-- Historial comercial: backend lee e inserta; administración solo lee.
drop policy if exists lacusine_backend_select on public.commercial_events','drop policy if exists lacusine_backend_insert on public.commercial_events','drop policy if exists lacusine_admin_read on public.commercial_events','create policy lacusine_backend_select on public.commercial_events for select to lacusine_backend using (true)','create policy lacusine_backend_insert on public.commercial_events for insert to lacusine_backend with check (true)','create policy lacusine_admin_read on public.commercial_events for select to lacusine_admin_ops using (true)','drop policy if exists lacusine_backend_select on public.lead_stage_history','drop policy if exists lacusine_backend_insert on public.lead_stage_history','drop policy if exists lacusine_admin_read on public.lead_stage_history','create policy lacusine_backend_select on public.lead_stage_history for select to lacusine_backend using (true)','create policy lacusine_backend_insert on public.lead_stage_history for insert to lacusine_backend with check (true)','create policy lacusine_admin_read on public.lead_stage_history for select to lacusine_admin_ops using (true)','-- Ventas confirmadas: el backend registra y lee; la administración solo consulta.
drop policy if exists lacusine_backend_select on public.sales','drop policy if exists lacusine_backend_insert on public.sales','drop policy if exists lacusine_admin_read on public.sales','create policy lacusine_backend_select on public.sales for select to lacusine_backend using (true)','create policy lacusine_backend_insert on public.sales for insert to lacusine_backend with check (true)','create policy lacusine_admin_read on public.sales for select to lacusine_admin_ops using (true)','drop policy if exists lacusine_backend_select on public.sale_products','drop policy if exists lacusine_backend_insert on public.sale_products','drop policy if exists lacusine_admin_read on public.sale_products','create policy lacusine_backend_select on public.sale_products for select to lacusine_backend using (true)','create policy lacusine_backend_insert on public.sale_products for insert to lacusine_backend with check (true)','create policy lacusine_admin_read on public.sale_products for select to lacusine_admin_ops using (true)','drop policy if exists lacusine_backend_select on public.sale_status_events','drop policy if exists lacusine_backend_insert on public.sale_status_events','drop policy if exists lacusine_admin_read on public.sale_status_events','create policy lacusine_backend_select on public.sale_status_events for select to lacusine_backend using (true)','create policy lacusine_backend_insert on public.sale_status_events for insert to lacusine_backend with check (true)','create policy lacusine_admin_read on public.sale_status_events for select to lacusine_admin_ops using (true)','-- Analítica agregada: el backend lee, inserta y consolida; la administración solo lee.
drop policy if exists lacusine_backend_select on public.analytics_daily_counts','drop policy if exists lacusine_backend_insert on public.analytics_daily_counts','drop policy if exists lacusine_backend_update on public.analytics_daily_counts','drop policy if exists lacusine_admin_read on public.analytics_daily_counts','create policy lacusine_backend_select on public.analytics_daily_counts for select to lacusine_backend using (true)','create policy lacusine_backend_insert on public.analytics_daily_counts for insert to lacusine_backend with check (true)','create policy lacusine_backend_update on public.analytics_daily_counts for update to lacusine_backend using (true) with check (true)','create policy lacusine_admin_read on public.analytics_daily_counts for select to lacusine_admin_ops using (true)']::text[]);
insert into supabase_migrations.schema_migrations (version, name, statements) values ('20261105130000', 'business_report_budget', array['-- Presupuesto de análisis narrativo con IA para informes de gerencia (purpose = ''business_report'').
--
-- Separado de budget_reservations (agente comercial): los gastos de un informe no cuentan contra las respuestas
-- de WhatsApp y viceversa. Mismo ciclo de vida que el libro comercial:
--   reserved   -> peor caso reservado antes de llamar. Cuenta.
--   settled    -> coste real según el uso verificado. Cuenta.
--   released   -> la API garantizó que no facturó. No cuenta.
--   uncertain  -> no se sabe si facturó. Cuenta hasta conciliar. Nunca se libera automáticamente.
--
-- call_id = clave de idempotencia: tipo de informe + semana + huella del payload agregado. Una misma
-- versión de datos no se paga dos veces. Esta tabla NO guarda prompts, respuestas ni datos personales.

create table if not exists public.analysis_budget_reservations (
  id           uuid primary key default gen_random_uuid(),
  call_id      text not null unique check (char_length(call_id) between 1 and 200),
  purpose      text not null default ''business_report'' check (purpose = ''business_report''),
  report_kind  text not null check (report_kind in (''ejecutivo'')),
  week_start   date not null,
  fingerprint  text not null check (char_length(fingerprint) = 64),
  status       text not null check (status in (''reserved'', ''settled'', ''released'', ''uncertain'')),
  reserved_usd numeric(12, 6) not null check (reserved_usd >= 0),
  counted_usd  numeric(12, 6) not null default 0 check (counted_usd >= 0),
  created_at   timestamptz not null default now(),
  reconciled_at timestamptz,
  -- Una reserva liberada no cuenta: su importe contable debe ser cero (mismo invariante que budget_reservations).
  constraint analysis_budget_released_zero check (status <> ''released'' or counted_usd = 0)
)','create index if not exists analysis_budget_created_idx on public.analysis_budget_reservations (created_at)','alter table public.analysis_budget_reservations enable row level security','revoke all on table public.analysis_budget_reservations from public, anon, authenticated','-- Backend: registra, liquida y concilia. Administración: solo consulta.
grant select, insert, update on public.analysis_budget_reservations to lacusine_backend','grant select on public.analysis_budget_reservations to lacusine_admin_ops','drop policy if exists lacusine_backend_all on public.analysis_budget_reservations','drop policy if exists lacusine_admin_read on public.analysis_budget_reservations','create policy lacusine_backend_all on public.analysis_budget_reservations for all to lacusine_backend
  using (true) with check (true)','create policy lacusine_admin_read on public.analysis_budget_reservations for select to lacusine_admin_ops
  using (true)']::text[]);
insert into supabase_migrations.schema_migrations (version, name, statements) values ('20261105140000', 'commercial_history_fk_restrict', array['-- Corrección del historial comercial (sucesora de 20261105110000, ya aplicada en Supabase Cloud).
--
-- Defecto: commercial_events.conversation_id, commercial_events.product_id y sales.conversation_id usaban
-- ON DELETE SET NULL. Ese borrado en cascada ejecuta un UPDATE sobre la fila hija, y los triggers
-- forbid_history_changes (append-only) lo rechazan. Resultado: borrar una conversación o un producto con
-- historial comercial falla con "historial comercial: operación no permitida (UPDATE)" de forma opaca.
--
-- Corrección: ON DELETE RESTRICT explícito. Se mantiene la inmutabilidad: el historial nunca se reescribe y
-- el borrado queda bloqueado con un error claro (23503), en lugar de un intento de UPDATE.
--
-- Alcance: solo cambia el comportamiento de borrado de tres claves foráneas. No modifica filas, no cambia
-- triggers, privilegios ni RLS. ALTER TABLE ... ADD CONSTRAINT valida filas existentes con SELECT: no dispara
-- los triggers de UPDATE. lead_id ya es RESTRICT desde 20261105110000 y no se toca aquí.
--
-- Privacidad: RESTRICT no añade bloqueos frente al borrado de prospectos. Todo evento o venta que apunta a una
-- conversación también apunta a su lead (lead_id RESTRICT), así que el borrado de un prospecto con historial
-- ya estaba bloqueado. Ver docs/respaldo-y-restauracion.md, sección "Privacidad y borrado".

alter table public.commercial_events drop constraint if exists commercial_events_conversation_id_fkey','alter table public.commercial_events
  add constraint commercial_events_conversation_id_fkey
  foreign key (conversation_id) references public.conversations (id) on delete restrict','alter table public.commercial_events drop constraint if exists commercial_events_product_id_fkey','alter table public.commercial_events
  add constraint commercial_events_product_id_fkey
  foreign key (product_id) references public.products (id) on delete restrict','alter table public.sales drop constraint if exists sales_conversation_id_fkey','alter table public.sales
  add constraint sales_conversation_id_fkey
  foreign key (conversation_id) references public.conversations (id) on delete restrict','-- Comprobación final: si alguna clave no quedó con RESTRICT, la migración falla y se revierte entera.
do $$
begin
  if exists (
    select 1
    from pg_constraint
    where contype = ''f''
      and conname in (''commercial_events_conversation_id_fkey'', ''commercial_events_product_id_fkey'',
                      ''sales_conversation_id_fkey'')
      and confdeltype <> ''r''
  ) then
    raise exception ''la clave foránea comercial no quedó con ON DELETE RESTRICT'';
  end if;
end
$$']::text[]);
