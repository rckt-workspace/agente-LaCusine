-- Auditoría de uso y costo estimado de Claude (Fase 3).
-- Sin prompts ni respuestas: solo metadatos y conteos de tokens.
-- call_id es la clave de idempotencia de los registros exitosos (índice parcial): un evento reintentado
-- no duplica costos, mientras que los fallos pueden registrarse tantas veces como ocurran.

create table public.llm_usage_events (
  id                      uuid primary key default gen_random_uuid(),
  call_id                 text not null check (char_length(call_id) between 1 and 200),
  conversation_id         uuid references public.conversations (id) on delete set null,
  provider                text not null default 'anthropic' check (provider in ('anthropic', 'fake')),
  model                   text not null check (char_length(model) between 1 and 100),
  purpose                 text not null check (purpose in ('commercial_reply')),
  status                  text not null check (status in ('succeeded', 'failed')),
  error_code              text check (error_code is null or char_length(error_code) <= 64),
  input_tokens            integer not null default 0 check (input_tokens >= 0),
  output_tokens           integer not null default 0 check (output_tokens >= 0),
  cache_read_tokens       integer not null default 0 check (cache_read_tokens >= 0),
  cache_write_tokens      integer not null default 0 check (cache_write_tokens >= 0),
  estimated_cost_usd      numeric(12, 6) not null default 0 check (estimated_cost_usd >= 0),
  pricing_reference       text check (pricing_reference is null or char_length(pricing_reference) <= 100),
  created_at              timestamptz not null default now(),
  constraint llm_usage_error_consistency check ((status = 'failed') = (error_code is not null))
);

create index llm_usage_created_idx on public.llm_usage_events (created_at);

create unique index llm_usage_one_success_per_call
  on public.llm_usage_events (call_id)
  where status = 'succeeded';

alter table public.llm_usage_events enable row level security;

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    revoke all on table public.llm_usage_events from anon;
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    revoke all on table public.llm_usage_events from authenticated;
  end if;
end
$$;
