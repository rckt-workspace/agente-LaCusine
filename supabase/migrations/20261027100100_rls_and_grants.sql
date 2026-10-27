-- Seguridad de acceso (Fase 2).
-- Todas las tablas del esquema público tienen RLS activa y ninguna política para anon ni authenticated:
-- los clientes de Supabase (claves públicas) no pueden leer ni escribir prospectos, mensajes ni eventos.
-- El backend accede con una credencial de servidor dedicada (nunca con service_role en frontend).
-- Estas sentencias son idempotentes y funcionan también en PostgreSQL local sin los roles de Supabase.

alter table public.leads enable row level security;
alter table public.conversations enable row level security;
alter table public.messages enable row level security;
alter table public.webhook_events enable row level security;
alter table public.products enable row level security;
alter table public.human_handoffs enable row level security;

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    revoke all on table
      public.leads, public.conversations, public.messages,
      public.webhook_events, public.products, public.human_handoffs
    from anon;
  end if;

  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    revoke all on table
      public.leads, public.conversations, public.messages,
      public.webhook_events, public.products, public.human_handoffs
    from authenticated;
  end if;
end
$$;

revoke all on function public.set_updated_at() from public;

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    revoke all on function public.set_updated_at() from anon;
  end if;

  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    revoke all on function public.set_updated_at() from authenticated;
  end if;
end
$$;
