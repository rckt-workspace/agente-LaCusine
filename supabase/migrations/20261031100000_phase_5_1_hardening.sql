-- Estabilización previa a producción (Fase 5.1). Incremental: no modifica migraciones publicadas.

-- ---------------------------------------------------------------------------
-- messages: solo un mensaje con marca de tiempo confiable de Meta abre la ventana de atención.
-- received_at es la hora del evento de Meta cuando es confiable; si no lo es, es la hora de procesamiento
-- y window_trusted = false (nunca abre ventana).
-- ---------------------------------------------------------------------------
alter table public.messages
  add column window_trusted boolean not null default false;

create index messages_window_idx
  on public.messages (conversation_id, received_at)
  where direction = 'inbound' and window_trusted;

-- ---------------------------------------------------------------------------
-- outbound_messages:
--   kind 'handoff_notice': confirmación única de transferencia. Es la única salida que no se cancela
--   por control humano, porque anuncia precisamente la transferencia.
--   business_phone_id: línea de negocio del contexto. El dispatcher solo envía desde esa línea.
-- ---------------------------------------------------------------------------
alter table public.outbound_messages drop constraint outbound_messages_kind_check;

alter table public.outbound_messages
  add constraint outbound_messages_kind_check check (kind in ('reply', 'handoff_notice'));

alter table public.outbound_messages
  add column business_phone_id text check (business_phone_id is null or char_length(business_phone_id) <= 32);

-- ---------------------------------------------------------------------------
-- products: separación explícita entre precio de referencia y precio vigente confirmado.
-- price_is_reference sigue siendo verdadero hasta que un responsable confirme el precio con fecha.
-- ---------------------------------------------------------------------------
alter table public.products
  add column price_confirmed_at timestamptz;

alter table public.products
  add constraint products_confirmed_price_has_price
    check (price_confirmed_at is null or price_cop is not null);

-- ---------------------------------------------------------------------------
-- admin_audit_log: cada intervención administrativa. Sin texto completo del cliente ni secretos.
-- ---------------------------------------------------------------------------
create table public.admin_audit_log (
  id            uuid primary key default gen_random_uuid(),
  created_at    timestamptz not null default now(),
  operator      text not null check (char_length(operator) between 1 and 120),
  action        text not null check (char_length(action) between 1 and 80),
  target_type   text not null check (target_type in ('webhook_event', 'outbound_message', 'budget_reservation',
                                                     'human_handoff', 'conversation')),
  target_id     uuid not null,
  from_state    text check (from_state is null or char_length(from_state) <= 40),
  to_state      text check (to_state is null or char_length(to_state) <= 40),
  reason        text not null check (char_length(reason) between 3 and 500),
  evidence_ref  text check (evidence_ref is null or char_length(evidence_ref) <= 200)
);

create index admin_audit_target_idx on public.admin_audit_log (target_type, target_id, created_at);

alter table public.admin_audit_log enable row level security;

-- Los registros de auditoría no se modifican ni se borran desde la aplicación.
revoke update, delete, truncate on public.admin_audit_log from public;

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    revoke all on table public.admin_audit_log from anon;
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    revoke all on table public.admin_audit_log from authenticated;
  end if;
end
$$;
