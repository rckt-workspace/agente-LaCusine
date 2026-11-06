-- Trazabilidad comercial y ventas confirmadas (Fase 7, inteligencia comercial).
-- Migración incremental: no modifica migraciones publicadas.
--
-- Principios:
--  * Un evento comercial es un HECHO observable (interés, intención, cotización, transferencia). Es append-only.
--  * Una intención de compra NO es una venta. Las ventas solo existen si una operación administrativa o un sistema
--    externo de pedidos las confirma, con fuente, responsable y evidencia.
--  * Nada de datos personales completos: los eventos guardan referencias internas y categorías, no contenido.
--  * Revertir: eliminar estas tablas y funciones (no hay datos de otras migraciones que dependan de ellas).

-- ---------------------------------------------------------------------------
-- Eventos comerciales (append-only). dedupe_key evita duplicados por reintento.
-- ---------------------------------------------------------------------------
create table public.commercial_events (
  id              uuid primary key default gen_random_uuid(),
  occurred_at     timestamptz not null,
  event_type      text not null check (event_type in (
                    'first_contact', 'product_interest', 'price_query', 'budget_declared',
                    'purchase_intent', 'quote_request', 'handoff_to_advisor', 'quote_created')),
  lead_id         uuid not null references public.leads (id) on delete restrict,
  conversation_id uuid references public.conversations (id) on delete set null,
  category        text check (category is null or char_length(category) <= 120),
  product_id      uuid references public.products (id) on delete set null,
  budget_cop      bigint check (budget_cop is null or budget_cop >= 0),
  source_event_id text check (source_event_id is null or char_length(source_event_id) <= 200),
  dedupe_key      text not null unique check (char_length(dedupe_key) between 1 and 300),
  created_at      timestamptz not null default now()
);
create index commercial_events_occurred_idx on public.commercial_events (occurred_at);
create index commercial_events_lead_idx on public.commercial_events (lead_id, occurred_at);

-- ---------------------------------------------------------------------------
-- Historial de etapas del lead (append-only). Permite informes históricos aunque el estado actual cambie.
-- ---------------------------------------------------------------------------
create table public.lead_stage_history (
  id          uuid primary key default gen_random_uuid(),
  lead_id     uuid not null references public.leads (id) on delete restrict,
  from_status text check (from_status is null or char_length(from_status) <= 40),
  to_status   text not null check (char_length(to_status) between 1 and 40),
  reason      text not null check (char_length(reason) between 1 and 200),
  changed_by  text not null check (char_length(changed_by) between 1 and 80),
  changed_at  timestamptz not null default now()
);
create index lead_stage_history_lead_idx on public.lead_stage_history (lead_id, changed_at);

-- ---------------------------------------------------------------------------
-- Ventas confirmadas. El identificador de venta es único. Solo se crean por confirmación administrativa
-- o por un sistema externo verificable. Ni la IA ni el cliente pueden crearlas.
-- ---------------------------------------------------------------------------
create table public.sales (
  id                  uuid primary key default gen_random_uuid(),
  sale_ref            text not null unique check (char_length(sale_ref) between 3 and 100),
  lead_id             uuid not null references public.leads (id) on delete restrict,
  conversation_id     uuid references public.conversations (id) on delete set null,
  amount_cop          bigint not null check (amount_cop > 0),
  currency            text not null default 'COP' check (currency = 'COP'),
  sold_at             timestamptz not null,
  confirmation_source text not null check (confirmation_source in ('admin_manual', 'external_order_system')),
  confirmed_by        text not null check (char_length(confirmed_by) between 1 and 120),
  evidence_ref        text not null check (char_length(evidence_ref) between 3 and 300),
  ai_assisted         boolean not null default false,
  created_at          timestamptz not null default now()
);
create index sales_sold_at_idx on public.sales (sold_at);
create index sales_lead_idx on public.sales (lead_id);

-- Productos de una venta: solo referencias a productos del catálogo verificado.
create table public.sale_products (
  sale_id    uuid not null references public.sales (id) on delete restrict,
  product_id uuid not null references public.products (id) on delete restrict,
  quantity   integer not null check (quantity > 0 and quantity <= 1000),
  primary key (sale_id, product_id)
);

-- Cambios de estado de una venta (confirmada, cancelada, corregida). Append-only: el historial no se borra.
create table public.sale_status_events (
  id          uuid primary key default gen_random_uuid(),
  sale_id     uuid not null references public.sales (id) on delete restrict,
  status      text not null check (status in ('confirmed', 'cancelled')),
  reason      text not null check (char_length(reason) between 1 and 300),
  actor       text not null check (char_length(actor) between 1 and 120),
  occurred_at timestamptz not null default now()
);
create index sale_status_events_sale_idx on public.sale_status_events (sale_id, occurred_at);

-- ---------------------------------------------------------------------------
-- Append-only: ningún rol de la aplicación puede modificar ni borrar historial comercial.
-- ---------------------------------------------------------------------------
create function public.forbid_history_changes()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  raise exception 'historial comercial: operación no permitida (%)', tg_op;
end;
$$;

create trigger commercial_events_append_only
  before update or delete on public.commercial_events
  for each row execute function public.forbid_history_changes();
create trigger lead_stage_history_append_only
  before update or delete on public.lead_stage_history
  for each row execute function public.forbid_history_changes();
create trigger sale_status_events_append_only
  before update or delete on public.sale_status_events
  for each row execute function public.forbid_history_changes();
create trigger sales_append_only
  before update or delete on public.sales
  for each row execute function public.forbid_history_changes();
create trigger sale_products_append_only
  before update or delete on public.sale_products
  for each row execute function public.forbid_history_changes();

-- ---------------------------------------------------------------------------
-- Seguridad: RLS activado, sin acceso público, permisos mínimos por rol.
-- ---------------------------------------------------------------------------
alter table public.commercial_events enable row level security;
alter table public.lead_stage_history enable row level security;
alter table public.sales enable row level security;
alter table public.sale_products enable row level security;
alter table public.sale_status_events enable row level security;

revoke all on table public.commercial_events, public.lead_stage_history, public.sales,
                    public.sale_products, public.sale_status_events from anon, authenticated;

grant select, insert on public.commercial_events, public.lead_stage_history to lacusine_backend;
grant select, insert on public.sales, public.sale_products, public.sale_status_events to lacusine_backend;
grant select on public.commercial_events, public.lead_stage_history, public.sales,
                public.sale_products, public.sale_status_events to lacusine_admin_ops;
