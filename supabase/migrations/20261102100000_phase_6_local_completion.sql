-- Fase 6 (cierre local): atribución de Meta Ads, notificaciones a asesores y control de migraciones.
--
-- 1. Atribución: campos del objeto `referral` que Meta incluye SOLO en el primer mensaje de un anuncio
--    Click-to-WhatsApp. Se guardan tal cual, con longitud acotada. Nunca se inventan.
-- 2. Notificaciones: una fila por solicitud de asesor, creada en la misma transacción que la transferencia.
--    Sin datos personales: solo identificadores, motivo y prioridad.
-- 3. El registro de migraciones (schema_migrations) lo crea la herramienta `app.db.migrate`, no esta migración.

alter table public.leads
  add column referral_source_type text,
  add column referral_source_id text,
  add column referral_source_url text,
  add column referral_ctwa_clid text,
  add column referral_captured_at timestamptz,
  add constraint leads_referral_lengths check (
    (referral_source_type is null or char_length(referral_source_type) <= 20)
    and (referral_source_id is null or char_length(referral_source_id) <= 64)
    and (referral_source_url is null or char_length(referral_source_url) <= 500)
    and (referral_ctwa_clid is null or char_length(referral_ctwa_clid) <= 256)
  ),
  add constraint leads_referral_captured_consistency check (
    (referral_captured_at is null) = (referral_source_type is null and referral_source_id is null
                                      and referral_source_url is null and referral_ctwa_clid is null)
  );

create table public.advisor_notifications (
  id                 uuid primary key default gen_random_uuid(),
  handoff_id         uuid not null unique references public.human_handoffs (id) on delete cascade,
  conversation_id    uuid not null references public.conversations (id) on delete cascade,
  reason             text not null,
  priority           text not null check (priority in ('low', 'normal', 'high', 'urgent')),
  status             text not null default 'pending'
                       check (status in ('pending', 'sending', 'sent', 'failed')),
  attempts           integer not null default 0 check (attempts >= 0),
  lease_token        uuid,
  lease_expires_at   timestamptz,
  last_error_code    text check (last_error_code is null or char_length(last_error_code) <= 60),
  created_at         timestamptz not null default now(),
  updated_at         timestamptz not null default now(),
  sent_at            timestamptz,
  constraint advisor_notifications_lease_consistency check (
    (status = 'sending') = (lease_token is not null)
  ),
  constraint advisor_notifications_sent_consistency check (
    (status = 'sent') = (sent_at is not null)
  )
);

create index advisor_notifications_pending_idx
  on public.advisor_notifications (created_at)
  where status in ('pending', 'sending');

alter table public.advisor_notifications enable row level security;

grant select, insert, update on public.advisor_notifications to lacusine_backend;

drop policy if exists lacusine_backend_access on public.advisor_notifications;
create policy lacusine_backend_access on public.advisor_notifications
  for all to lacusine_backend using (true) with check (true);

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    revoke all on public.advisor_notifications from anon;
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    revoke all on public.advisor_notifications from authenticated;
  end if;
end
$$;
