-- Integración de salida con WhatsApp Cloud API (Fase 5).
-- Incremental: no modifica migraciones publicadas.

-- ---------------------------------------------------------------------------
-- outbound_messages: estados reales de mensajería y lease del envío.
--   pending    -> generada, lista para despachar
--   sending    -> reclamada por un dispatcher (lease con token)
--   sent       -> Meta aceptó la solicitud y devolvió un identificador (wamid)
--   delivered  -> confirmación de entrega (webhook de estado)
--   read       -> confirmación de lectura (webhook de estado)
--   failed     -> rechazo definitivo (Meta o validación previa)
--   uncertain  -> no se sabe si Meta aceptó el mensaje; NUNCA se reenvía automáticamente
--   blocked    -> no se envió por política (p. ej. ventana de atención cerrada)
--   cancelled  -> retenida por control humano u otra decisión de negocio
-- ---------------------------------------------------------------------------
alter table public.outbound_messages drop constraint outbound_messages_delivery_status_check;

alter table public.outbound_messages
  add constraint outbound_messages_delivery_status_check
    check (delivery_status in ('pending', 'sending', 'sent', 'delivered', 'read',
                               'failed', 'uncertain', 'blocked', 'cancelled'));

alter table public.outbound_messages
  add column lease_token uuid,
  add column lease_owner text check (lease_owner is null or char_length(lease_owner) <= 80),
  add column lease_expires_at timestamptz,
  add column next_attempt_at timestamptz not null default now(),
  add column last_error_code text check (last_error_code is null or char_length(last_error_code) <= 64),
  add column from_phone_id text check (from_phone_id is null or char_length(from_phone_id) <= 32),
  add column sent_at timestamptz,
  add column delivered_at timestamptz,
  add column read_at timestamptz,
  add column failed_at timestamptz;

-- Un mensaje tiene token de propiedad si y solo si está en envío.
alter table public.outbound_messages
  add constraint outbound_sending_lease_consistency
    check ((delivery_status = 'sending') = (lease_token is not null));

-- El identificador de Meta identifica a un único mensaje saliente.
create unique index outbound_external_message_id_key
  on public.outbound_messages (external_message_id)
  where external_message_id is not null;

create index outbound_dispatch_idx
  on public.outbound_messages (conversation_id, created_at, id)
  where delivery_status in ('pending', 'sending', 'uncertain');

create index outbound_sending_lease_idx
  on public.outbound_messages (lease_expires_at)
  where delivery_status = 'sending';

-- ---------------------------------------------------------------------------
-- webhook_events: los eventos de estado de Meta son una nueva clase de evento.
-- No pertenecen a un contacto de la cola de mensajes: contact_wa_id queda vacío.
-- ---------------------------------------------------------------------------
alter table public.webhook_events drop constraint webhook_events_event_type_check;

alter table public.webhook_events
  add constraint webhook_events_event_type_check
    check (event_type in ('message', 'status'));

alter table public.webhook_events
  add constraint webhook_events_status_has_no_contact
    check (event_type <> 'status' or contact_wa_id is null);
