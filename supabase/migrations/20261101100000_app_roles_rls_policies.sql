-- Acceso efectivo para roles no propietarios (revisión de preproducción).
--
-- Problema: las tablas tienen RLS activa y sin políticas. Un rol sin BYPASSRLS y que no sea propietario
-- no ve ninguna fila y no puede insertar. Solo el propietario del esquema funcionaba.
--
-- Solución: dos roles de grupo SIN login (sin contraseña, sin superusuario, sin BYPASSRLS):
--   lacusine_backend   -> API y worker: solo los privilegios que usa el código.
--   lacusine_admin_ops -> CLI de conciliación: lectura, actualización acotada y escritura de auditoría.
-- Los roles de login (por ejemplo lacusine_app, lacusine_admin) los crea el responsable FUERA del
-- repositorio y se asocian con: grant lacusine_backend to <login>;  grant lacusine_admin_ops to <login>;
--
-- anon, authenticated y public no reciben políticas ni privilegios.

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'lacusine_backend') then
    create role lacusine_backend nologin nosuperuser nocreatedb nocreaterole nobypassrls;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'lacusine_admin_ops') then
    create role lacusine_admin_ops nologin nosuperuser nocreatedb nocreaterole nobypassrls;
  end if;
end
$$;

grant usage on schema public to lacusine_backend, lacusine_admin_ops;

-- Todas las tablas de negocio deben tener RLS activa, incluida la auditoría.
alter table public.leads enable row level security;
alter table public.conversations enable row level security;
alter table public.messages enable row level security;
alter table public.webhook_events enable row level security;
alter table public.products enable row level security;
alter table public.human_handoffs enable row level security;
alter table public.llm_usage_events enable row level security;
alter table public.contact_leases enable row level security;
alter table public.outbound_messages enable row level security;
alter table public.budget_reservations enable row level security;
alter table public.admin_audit_log enable row level security;

-- Backend: privilegios por tabla según las sentencias reales del código.
grant select, insert, update on
  public.leads, public.conversations, public.messages, public.products,
  public.human_handoffs, public.budget_reservations
to lacusine_backend;
grant select, insert on public.llm_usage_events to lacusine_backend;
grant select, insert, update, delete on
  public.webhook_events, public.outbound_messages, public.contact_leases
to lacusine_backend;

-- Política permisiva solo para el grupo. La restricción real son los GRANT anteriores.
do $$
declare
  t text;
begin
  foreach t in array array[
    'leads', 'conversations', 'messages', 'products', 'human_handoffs', 'budget_reservations',
    'llm_usage_events', 'webhook_events', 'outbound_messages', 'contact_leases'
  ] loop
    execute format('drop policy if exists lacusine_backend_access on public.%I', t);
    execute format(
      'create policy lacusine_backend_access on public.%I for all to lacusine_backend using (true) with check (true)',
      t
    );
  end loop;
end
$$;

-- Función de retención: la invoca el backend. Ejecuta sus DELETE y UPDATE con los privilegios del invocador.
grant execute on function public.purge_expired_personal_data(integer) to lacusine_backend;

-- Disparadores de updated_at: el backend los necesita para ejecutar las funciones que usan.
grant execute on function public.set_updated_at() to lacusine_backend, lacusine_admin_ops;

-- Administración: lectura de estados, actualización acotada a las decisiones de la CLI y auditoría.
grant select on
  public.webhook_events, public.outbound_messages, public.budget_reservations,
  public.human_handoffs, public.conversations, public.admin_audit_log
to lacusine_admin_ops;
grant update on
  public.webhook_events, public.outbound_messages, public.budget_reservations,
  public.human_handoffs, public.conversations
to lacusine_admin_ops;
grant insert on public.admin_audit_log to lacusine_admin_ops;

do $$
declare
  t text;
begin
  foreach t in array array[
    'webhook_events', 'outbound_messages', 'budget_reservations', 'human_handoffs', 'conversations'
  ] loop
    execute format('drop policy if exists lacusine_admin_read on public.%I', t);
    execute format('create policy lacusine_admin_read on public.%I for select to lacusine_admin_ops using (true)', t);
    execute format('drop policy if exists lacusine_admin_write on public.%I', t);
    execute format(
      'create policy lacusine_admin_write on public.%I for update to lacusine_admin_ops using (true) with check (true)',
      t
    );
  end loop;
end
$$;

drop policy if exists lacusine_admin_read on public.admin_audit_log;
create policy lacusine_admin_read on public.admin_audit_log for select to lacusine_admin_ops using (true);
drop policy if exists lacusine_admin_append on public.admin_audit_log;
create policy lacusine_admin_append on public.admin_audit_log for insert to lacusine_admin_ops with check (true);

-- La auditoría es solo de adición para cualquier rol de la aplicación, incluido el propietario en uso normal.
revoke all on public.admin_audit_log from lacusine_backend;
revoke update, delete, truncate on public.admin_audit_log from lacusine_admin_ops;
revoke update, delete, truncate on public.admin_audit_log from public;
