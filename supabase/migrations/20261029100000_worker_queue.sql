-- Cola persistente del worker, respuestas en outbox y reservas presupuestarias (Fase 4).
-- Migración incremental: no modifica migraciones publicadas, solo amplía restricciones compatibles.

-- ---------------------------------------------------------------------------
-- webhook_events: identidad de la línea de WhatsApp, lease de procesamiento y auditoría de intentos.
-- Estados existentes: received, queued, processing, processed, ignored, retry, dead_letter.
-- Se añade manual_review: resultado incierto o reintentos agotados que requieren revisión humana.
-- ---------------------------------------------------------------------------
alter table public.webhook_events
  add column business_phone_id text not null default '' check (char_length(business_phone_id) <= 32),
  add column lease_token uuid,
  add column lease_owner text check (lease_owner is null or char_length(lease_owner) <= 80),
  add column lease_expires_at timestamptz,
  add column last_attempt_at timestamptz;

alter table public.webhook_events drop constraint webhook_events_processing_status_check;

alter table public.webhook_events
  add constraint webhook_events_processing_status_check
    check (processing_status in ('received', 'queued', 'processing', 'processed', 'ignored',
                                 'retry', 'manual_review', 'dead_letter'));

-- Un evento tiene token de propiedad si y solo si está en procesamiento.
alter table public.webhook_events
  add constraint webhook_events_lease_consistency
    check ((processing_status = 'processing') = (lease_token is not null));

create index webhook_events_claim_idx
  on public.webhook_events (business_phone_id, contact_wa_id, received_at, id)
  where processing_status in ('received', 'queued', 'retry', 'processing', 'manual_review');

create index webhook_events_lease_expiry_idx
  on public.webhook_events (lease_expires_at)
  where processing_status = 'processing';

-- ---------------------------------------------------------------------------
-- contact_leases: exclusión por contacto (línea + wa_id). Una sola fila activa por clave.
-- Se libera al terminar el evento o expira si el worker desaparece.
-- ---------------------------------------------------------------------------
create table public.contact_leases (
  contact_key       text primary key check (char_length(contact_key) between 3 and 200),
  lease_token       uuid not null,
  lease_owner       text not null check (char_length(lease_owner) between 1 and 80),
  lease_expires_at  timestamptz not null
);

alter table public.contact_leases enable row level security;

-- ---------------------------------------------------------------------------
-- outbound_messages: respuestas GENERADAS. Crear una fila no significa enviarla ni entregarla.
-- Estados posteriores (sending, sent, delivered, failed) los gestionará el adaptador de WhatsApp.
-- ---------------------------------------------------------------------------
create table public.outbound_messages (
  id                    uuid primary key default gen_random_uuid(),
  idempotency_key       text not null unique check (char_length(idempotency_key) between 1 and 200),
  webhook_event_id      uuid references public.webhook_events (id) on delete set null,
  conversation_id       uuid not null references public.conversations (id) on delete cascade,
  kind                  text not null default 'reply' check (kind in ('reply')),
  content_text          text not null check (char_length(content_text) between 1 and 1200),
  delivery_status       text not null default 'pending'
                          check (delivery_status in ('pending', 'sending', 'sent', 'delivered', 'failed', 'cancelled')),
  attempts              integer not null default 0 check (attempts >= 0),
  external_message_id   text check (external_message_id is null or char_length(external_message_id) <= 128),
  expires_at            timestamptz not null default (now() + interval '7 days'),
  created_at            timestamptz not null default now(),
  updated_at            timestamptz not null default now()
);

-- Una única intención de respuesta por evento de origen.
create unique index outbound_one_reply_per_event
  on public.outbound_messages (webhook_event_id)
  where webhook_event_id is not null;

create index outbound_pending_idx
  on public.outbound_messages (created_at)
  where delivery_status = 'pending';

create trigger outbound_messages_set_updated_at
  before update on public.outbound_messages
  for each row execute function public.set_updated_at();

alter table public.outbound_messages enable row level security;

-- ---------------------------------------------------------------------------
-- budget_reservations: reserva persistente antes de cada llamada facturable.
-- counted_usd es lo que cuenta contra los presupuestos:
--   reserved   -> peor caso reservado
--   settled    -> costo real estimado
--   released   -> 0 (la API confirmó que no facturó)
--   uncertain  -> peor caso que se mantiene hasta conciliar (no se libera sin evidencia)
-- ---------------------------------------------------------------------------
create table public.budget_reservations (
  id              uuid primary key default gen_random_uuid(),
  call_id         text not null unique check (char_length(call_id) between 1 and 200),
  conversation_id uuid references public.conversations (id) on delete set null,
  status          text not null check (status in ('reserved', 'settled', 'released', 'uncertain')),
  reserved_usd    numeric(12, 6) not null check (reserved_usd >= 0),
  counted_usd     numeric(12, 6) not null check (counted_usd >= 0),
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),
  reconciled_at   timestamptz,
  constraint budget_released_is_free check (status <> 'released' or counted_usd = 0)
);

create index budget_reservations_created_idx on public.budget_reservations (created_at);

create trigger budget_reservations_set_updated_at
  before update on public.budget_reservations
  for each row execute function public.set_updated_at();

alter table public.budget_reservations enable row level security;

-- ---------------------------------------------------------------------------
-- Privilegios: ninguna tabla nueva es accesible desde anon ni authenticated.
-- ---------------------------------------------------------------------------
do $$
declare
  t text;
begin
  foreach t in array array['contact_leases', 'outbound_messages', 'budget_reservations']
  loop
    if exists (select 1 from pg_roles where rolname = 'anon') then
      execute format('revoke all on table public.%I from anon', t);
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
      execute format('revoke all on table public.%I from authenticated', t);
    end if;
  end loop;
end
$$;
