-- Esquema base del agente comercial La Cuisine (Fase 2).
-- Convenciones:
--   * Claves primarias UUID generadas por la base de datos.
--   * Timestamps con zona horaria (timestamptz).
--   * Estados controlados por CHECK; cambiar un estado requiere una nueva migración.
--   * Ningún secreto (tokens de Meta, claves de Anthropic) se guarda en estas tablas.

-- ---------------------------------------------------------------------------
-- Función auxiliar: mantiene updated_at en tablas mutables.
-- ---------------------------------------------------------------------------
create or replace function public.set_updated_at()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

-- ---------------------------------------------------------------------------
-- leads: prospectos identificados por su wa_id (número de WhatsApp en formato internacional sin "+").
-- Solo se guarda lo necesario para la atención comercial.
-- ---------------------------------------------------------------------------
create table public.leads (
  id                 uuid primary key default gen_random_uuid(),
  wa_contact_id      text not null unique check (wa_contact_id ~ '^[0-9]{6,20}$'),
  display_name       text check (display_name is null or char_length(display_name) <= 120),
  city               text check (city is null or char_length(city) <= 80),
  product_interest   text check (product_interest is null or char_length(product_interest) <= 120),
  budget_cop         integer check (budget_cop is null or budget_cop >= 0),
  status             text not null default 'new'
                       check (status in ('new', 'qualifying', 'qualified', 'quoted', 'handed_off', 'won', 'lost', 'opted_out')),
  acquisition_source text check (acquisition_source is null or char_length(acquisition_source) <= 120),
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now()
);

create trigger leads_set_updated_at
  before update on public.leads
  for each row execute function public.set_updated_at();

create index leads_status_idx on public.leads (status);

-- ---------------------------------------------------------------------------
-- conversations: sesiones de atención. Una sola conversación no cerrada por prospecto.
-- ---------------------------------------------------------------------------
create table public.conversations (
  id                uuid primary key default gen_random_uuid(),
  lead_id           uuid not null references public.leads (id) on delete cascade,
  status            text not null default 'active'
                      check (status in ('active', 'waiting_customer', 'closed')),
  automation_state  text not null default 'automated'
                      check (automation_state in ('automated', 'paused_for_human', 'disabled')),
  started_at        timestamptz not null default now(),
  last_activity_at  timestamptz not null default now(),
  closed_at         timestamptz,
  metadata          jsonb not null default '{}'::jsonb check (jsonb_typeof(metadata) = 'object'),
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now(),
  constraint conversations_closed_consistency
    check ((status = 'closed') = (closed_at is not null))
);

create trigger conversations_set_updated_at
  before update on public.conversations
  for each row execute function public.set_updated_at();

create unique index conversations_one_open_per_lead_idx
  on public.conversations (lead_id)
  where status <> 'closed';

-- ---------------------------------------------------------------------------
-- messages: contexto conversacional. El contenido se guarda con fecha de expiración.
-- ---------------------------------------------------------------------------
create table public.messages (
  id                  uuid primary key default gen_random_uuid(),
  conversation_id     uuid not null references public.conversations (id) on delete cascade,
  direction           text not null check (direction in ('inbound', 'outbound')),
  wa_message_id       text check (wa_message_id is null or char_length(wa_message_id) between 1 and 128),
  message_type        text not null default 'text'
                        check (message_type in ('text', 'image', 'audio', 'video', 'document', 'sticker',
                                                'location', 'contacts', 'interactive', 'button', 'unknown')),
  processing_status   text not null default 'received'
                        check (processing_status in ('received', 'queued', 'processed', 'failed', 'skipped')),
  content_text        text check (content_text is null or char_length(content_text) <= 4000),
  received_at         timestamptz not null default now(),
  content_expires_at  timestamptz,
  created_at          timestamptz not null default now(),
  constraint messages_content_has_expiry
    check (content_text is null or content_expires_at is not null)
);

-- Un mensaje de Meta solo puede registrarse una vez.
create unique index messages_wa_message_id_key
  on public.messages (wa_message_id)
  where wa_message_id is not null;

create index messages_conversation_received_idx
  on public.messages (conversation_id, received_at);

-- ---------------------------------------------------------------------------
-- webhook_events: eventos entrantes recibidos de Meta. Separa recepción de procesamiento.
-- Clave de deduplicación: (provider, external_event_id) con el wamid real de Meta.
-- ---------------------------------------------------------------------------
create table public.webhook_events (
  id                 uuid primary key default gen_random_uuid(),
  provider           text not null default 'meta_whatsapp' check (provider in ('meta_whatsapp')),
  external_event_id  text not null check (char_length(external_event_id) between 1 and 128),
  event_type         text not null default 'message' check (event_type in ('message')),
  contact_wa_id      text check (contact_wa_id is null or contact_wa_id ~ '^[0-9]{6,20}$'),
  payload            jsonb check (payload is null or jsonb_typeof(payload) = 'object'),
  payload_sha256     text not null check (payload_sha256 ~ '^[0-9a-f]{64}$'),
  processing_status  text not null default 'received'
                       check (processing_status in ('received', 'queued', 'processing', 'processed',
                                                    'ignored', 'retry', 'dead_letter')),
  attempts           integer not null default 0 check (attempts >= 0),
  max_attempts       integer not null default 5 check (max_attempts > 0),
  next_attempt_at    timestamptz not null default now(),
  last_error_code    text check (last_error_code is null or char_length(last_error_code) <= 64),
  received_at        timestamptz not null default now(),
  processed_at       timestamptz,
  expires_at         timestamptz not null,
  updated_at         timestamptz not null default now(),
  constraint webhook_events_dedupe_key unique (provider, external_event_id),
  constraint webhook_events_processed_consistency
    check ((processing_status in ('processed', 'ignored')) = (processed_at is not null))
);

create trigger webhook_events_set_updated_at
  before update on public.webhook_events
  for each row execute function public.set_updated_at();

-- Consulta del worker: eventos pendientes por orden de reintento.
create index webhook_events_pending_idx
  on public.webhook_events (next_attempt_at)
  where processing_status in ('received', 'queued', 'retry');

create index webhook_events_expires_idx on public.webhook_events (expires_at);

-- ---------------------------------------------------------------------------
-- products: catálogo comercial importado desde la fuente entregada por La Cuisine.
-- Precios y disponibilidad son referencias de la fuente, no datos en tiempo real.
-- ---------------------------------------------------------------------------
create table public.products (
  id                       uuid primary key default gen_random_uuid(),
  reference                text not null unique check (char_length(reference) between 1 and 64),
  brand                    text not null check (char_length(brand) between 1 and 120),
  category                 text not null check (char_length(category) between 1 and 120),
  description              text check (description is null or char_length(description) <= 2000),
  price_cop                numeric(14, 2) check (price_cop is null or price_cop >= 0),
  price_is_reference       boolean not null default true check (price_is_reference),
  availability_label       text check (availability_label is null or char_length(availability_label) <= 120),
  availability_as_of       date,
  product_url              text check (product_url is null or product_url ~ '^https://'),
  source_hash              text not null check (source_hash ~ '^[0-9a-f]{64}$'),
  created_at               timestamptz not null default now(),
  updated_at               timestamptz not null default now()
);

create trigger products_set_updated_at
  before update on public.products
  for each row execute function public.set_updated_at();

create index products_brand_category_idx on public.products (brand, category);

-- ---------------------------------------------------------------------------
-- human_handoffs: solicitudes de intervención humana. El envío de notificaciones
-- a asesores no se implementa en esta fase.
-- ---------------------------------------------------------------------------
create table public.human_handoffs (
  id                uuid primary key default gen_random_uuid(),
  conversation_id   uuid not null references public.conversations (id) on delete cascade,
  reason            text not null
                      check (reason in ('purchase_intent', 'quote_request', 'installation', 'warranty',
                                        'complaint', 'unsupported_request', 'customer_request', 'other')),
  status            text not null default 'open' check (status in ('open', 'assigned', 'resolved', 'cancelled')),
  priority          text not null default 'normal' check (priority in ('low', 'normal', 'high', 'urgent')),
  assigned_advisor  text check (assigned_advisor is null or char_length(assigned_advisor) <= 120),
  opened_at         timestamptz not null default now(),
  closed_at         timestamptz,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now(),
  constraint human_handoffs_closed_consistency
    check ((status in ('resolved', 'cancelled')) = (closed_at is not null)),
  constraint human_handoffs_assignment_consistency
    check (status <> 'assigned' or assigned_advisor is not null)
);

create trigger human_handoffs_set_updated_at
  before update on public.human_handoffs
  for each row execute function public.set_updated_at();

-- Una sola solicitud activa por conversación.
create unique index human_handoffs_one_active_idx
  on public.human_handoffs (conversation_id)
  where status in ('open', 'assigned');

create index human_handoffs_queue_idx
  on public.human_handoffs (status, priority, opened_at);
