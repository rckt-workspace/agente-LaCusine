-- Consolidación de métricas antes de la retención (Fase 7, analítica comercial).
-- Problema: la limpieza de 20261029100100 BORRA filas terminales de webhook_events y outbound_messages a los 7 días.
-- Sin esta migración, los indicadores semanales de entrega y de eventos desaparecerían de semanas anteriores.
-- Solución: antes de borrar, la misma sentencia suma los conteos por día (America/Bogota) y estado en una tabla
-- agregada. Ninguna fila personal se conserva; solo conteos. Cada fila borrada se cuenta exactamente una vez.
-- Migración incremental: no modifica migraciones publicadas. Revertir: restaurar la función de 20261029100100
-- y eliminar analytics_daily_counts (ver docs/estado-piloto-2026-11-05.md, sección de analítica).

create table public.analytics_daily_counts (
  day    date    not null,
  metric text    not null check (char_length(metric) between 1 and 80),
  value  bigint  not null default 0 check (value >= 0),
  primary key (day, metric)
);

alter table public.analytics_daily_counts enable row level security;
revoke all on table public.analytics_daily_counts from anon, authenticated;
grant select, insert, update on public.analytics_daily_counts to lacusine_backend;
grant select on public.analytics_daily_counts to lacusine_admin_ops;

drop function if exists public.purge_expired_personal_data(integer);
create function public.purge_expired_personal_data(p_batch_size integer default 1000)
returns table (
  webhook_events_deleted integer,
  message_contents_cleared integer,
  outbound_messages_deleted integer
)
language plpgsql
set search_path = ''
as $$
declare
  v_events integer := 0;
  v_contents integer := 0;
  v_outbound integer := 0;
begin
  if p_batch_size is null or p_batch_size <= 0 or p_batch_size > 10000 then
    raise exception 'p_batch_size must be between 1 and 10000';
  end if;

  -- Eventos de webhook en estado final: se cuentan por día y estado en la misma sentencia que los borra.
  with expired as (
    select id
    from public.webhook_events
    where expires_at < now()
      and processing_status in ('processed', 'ignored', 'dead_letter')
    order by expires_at
    limit p_batch_size
    for update skip locked
  ),
  deleted as (
    delete from public.webhook_events e
    using expired
    where e.id = expired.id
    returning e.received_at, e.processing_status
  ),
  rolled as (
    insert into public.analytics_daily_counts (day, metric, value)
    select (received_at at time zone 'America/Bogota')::date,
           'webhook_events:' || processing_status,
           count(*)
    from deleted
    group by 1, 2
    on conflict (day, metric) do update
      set value = public.analytics_daily_counts.value + excluded.value
    returning 1
  )
  select count(*) into v_events from deleted;

  -- Contenido de mensajes: solo se vacía el texto. La fila y el conteo de mensajes se conservan.
  with expired_contents as (
    select id
    from public.messages
    where content_expires_at < now()
      and content_text is not null
    order by content_expires_at
    limit p_batch_size
    for update skip locked
  ),
  cleared as (
    update public.messages m
    set content_text = null
    from expired_contents
    where m.id = expired_contents.id
    returning 1
  )
  select count(*) into v_contents from cleared;

  -- Respuestas salientes cerradas: se cuentan por día y estado antes de borrarlas. Las pendientes se conservan.
  -- 'read' también es estado final; antes de esta migración nunca se borraba y su texto permanecía.
  with expired_outbound as (
    select id
    from public.outbound_messages
    where expires_at < now()
      and delivery_status in ('sent', 'delivered', 'read', 'failed', 'cancelled')
    order by expires_at
    limit p_batch_size
    for update skip locked
  ),
  deleted_outbound as (
    delete from public.outbound_messages o
    using expired_outbound
    where o.id = expired_outbound.id
    returning o.created_at, o.delivery_status
  ),
  rolled_outbound as (
    insert into public.analytics_daily_counts (day, metric, value)
    select (created_at at time zone 'America/Bogota')::date,
           'outbound:' || delivery_status,
           count(*)
    from deleted_outbound
    group by 1, 2
    on conflict (day, metric) do update
      set value = public.analytics_daily_counts.value + excluded.value
    returning 1
  )
  select count(*) into v_outbound from deleted_outbound;

  return query select v_events, v_contents, v_outbound;
end;
$$;

revoke all on function public.purge_expired_personal_data(integer) from public;

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    revoke all on function public.purge_expired_personal_data(integer) from anon;
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    revoke all on function public.purge_expired_personal_data(integer) from authenticated;
  end if;
end
$$;

-- Restaura el permiso de ejecución que la recreación de la función elimina (ver 20261101100000).
grant execute on function public.purge_expired_personal_data(integer) to lacusine_backend;
