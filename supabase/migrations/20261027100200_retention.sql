-- Retención de datos personales (Fase 2).
-- Política:
--   * webhook_events: se eliminan al expirar expires_at (por defecto 7 días desde la recepción).
--   * messages.content_text: se anula al expirar content_expires_at; el metadato del mensaje se conserva.
--   * leads: la eliminación por solicitud del titular se hace con DELETE sobre leads;
--     las claves en cascada eliminan conversaciones, mensajes y solicitudes asociadas.
--     Los eventos de webhook se borran también por contact_wa_id (ver docs/modelo-datos.md).
-- La función se ejecuta desde el backend o un job programado con una credencial de servidor.

create or replace function public.purge_expired_personal_data(p_batch_size integer default 1000)
returns table (webhook_events_deleted integer, message_contents_cleared integer)
language plpgsql
set search_path = ''
as $$
declare
  v_events integer := 0;
  v_contents integer := 0;
begin
  if p_batch_size is null or p_batch_size <= 0 or p_batch_size > 10000 then
    raise exception 'p_batch_size must be between 1 and 10000';
  end if;

  with expired as (
    select id
    from public.webhook_events
    where expires_at < now()
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

  return query select v_events, v_contents;
end;
$$;

-- Supabase concede EXECUTE a anon y authenticated sobre funciones de public por defecto; se retira explícitamente.
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
