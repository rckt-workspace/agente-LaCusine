-- Políticas RLS para las tablas comerciales y de analítica creadas en 20261105100000 y 20261105110000.
--
-- Motivo: esas migraciones activan RLS pero no crean políticas. Los roles de la aplicación no son
-- propietarios ni tienen BYPASSRLS, así que con RLS activo y sin política el backend no puede leer ni
-- insertar (los GRANT solos no bastan). Esta migración solo añade políticas; no cambia tablas, datos ni privilegios.
--
-- Mínimo privilegio: las tablas de historial siguen siendo solo-anexar (triggers forbid_history_changes),
-- por eso no reciben política de update ni delete. analytics_daily_counts sí necesita update (consolidación).

-- Historial comercial: backend lee e inserta; administración solo lee.
drop policy if exists lacusine_backend_select on public.commercial_events;
drop policy if exists lacusine_backend_insert on public.commercial_events;
drop policy if exists lacusine_admin_read on public.commercial_events;
create policy lacusine_backend_select on public.commercial_events for select to lacusine_backend using (true);
create policy lacusine_backend_insert on public.commercial_events for insert to lacusine_backend with check (true);
create policy lacusine_admin_read on public.commercial_events for select to lacusine_admin_ops using (true);

drop policy if exists lacusine_backend_select on public.lead_stage_history;
drop policy if exists lacusine_backend_insert on public.lead_stage_history;
drop policy if exists lacusine_admin_read on public.lead_stage_history;
create policy lacusine_backend_select on public.lead_stage_history for select to lacusine_backend using (true);
create policy lacusine_backend_insert on public.lead_stage_history for insert to lacusine_backend with check (true);
create policy lacusine_admin_read on public.lead_stage_history for select to lacusine_admin_ops using (true);

-- Ventas confirmadas: el backend registra y lee; la administración solo consulta.
drop policy if exists lacusine_backend_select on public.sales;
drop policy if exists lacusine_backend_insert on public.sales;
drop policy if exists lacusine_admin_read on public.sales;
create policy lacusine_backend_select on public.sales for select to lacusine_backend using (true);
create policy lacusine_backend_insert on public.sales for insert to lacusine_backend with check (true);
create policy lacusine_admin_read on public.sales for select to lacusine_admin_ops using (true);

drop policy if exists lacusine_backend_select on public.sale_products;
drop policy if exists lacusine_backend_insert on public.sale_products;
drop policy if exists lacusine_admin_read on public.sale_products;
create policy lacusine_backend_select on public.sale_products for select to lacusine_backend using (true);
create policy lacusine_backend_insert on public.sale_products for insert to lacusine_backend with check (true);
create policy lacusine_admin_read on public.sale_products for select to lacusine_admin_ops using (true);

drop policy if exists lacusine_backend_select on public.sale_status_events;
drop policy if exists lacusine_backend_insert on public.sale_status_events;
drop policy if exists lacusine_admin_read on public.sale_status_events;
create policy lacusine_backend_select on public.sale_status_events for select to lacusine_backend using (true);
create policy lacusine_backend_insert on public.sale_status_events for insert to lacusine_backend with check (true);
create policy lacusine_admin_read on public.sale_status_events for select to lacusine_admin_ops using (true);

-- Analítica agregada: el backend lee, inserta y consolida; la administración solo lee.
drop policy if exists lacusine_backend_select on public.analytics_daily_counts;
drop policy if exists lacusine_backend_insert on public.analytics_daily_counts;
drop policy if exists lacusine_backend_update on public.analytics_daily_counts;
drop policy if exists lacusine_admin_read on public.analytics_daily_counts;
create policy lacusine_backend_select on public.analytics_daily_counts for select to lacusine_backend using (true);
create policy lacusine_backend_insert on public.analytics_daily_counts for insert to lacusine_backend with check (true);
create policy lacusine_backend_update on public.analytics_daily_counts for update to lacusine_backend using (true) with check (true);
create policy lacusine_admin_read on public.analytics_daily_counts for select to lacusine_admin_ops using (true);
