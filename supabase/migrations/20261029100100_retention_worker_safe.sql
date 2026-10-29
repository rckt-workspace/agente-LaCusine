-- Retención compatible con la cola (Fase 4).
-- La versión anterior borraba cualquier evento vencido, incluidos pendientes, en procesamiento o en revisión.
-- Esta versión solo elimina estados finales. Nunca toca trabajos pendientes, con lease, o en revisión manual.

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

  -- Solo estados finales: processed, ignored, dead_letter. Excluye received, queued, retry, processing y manual_review.
  with expired as (
    select id
    from public.webhook_events
    where expires_at < now()
      and processing_status in ('processed', 'ignored', 'dead_letter')
    order by expires_at
    limit p_batch_size
    for update skip locked
  )
  delete from public.webhook_events e
  using expired
  where e.id = expired.id;
  get diagnostics v_events = row_count;

  with expired_contents as (
    select id
    from public.messages
    where content_expires_at < now()
      and content_text is not null
    order by content_expires_at
    limit p_batch_size
    for update skip locked
  )
  update public.messages m
  set content_text = null
  from expired_contents
  where m.id = expired_contents.id;
  get diagnostics v_contents = row_count;

  -- Respuestas ya cerradas (enviadas, entregadas, fallidas o canceladas). Las pendientes se conservan.
  with expired_outbound as (
    select id
    from public.outbound_messages
    where expires_at < now()
      and delivery_status in ('sent', 'delivered', 'failed', 'cancelled')
    order by expires_at
    limit p_batch_size
    for update skip locked
  )
  delete from public.outbound_messages o
  using expired_outbound
  where o.id = expired_outbound.id;
  get diagnostics v_outbound = row_count;

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
