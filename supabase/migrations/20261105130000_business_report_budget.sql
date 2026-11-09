-- Presupuesto de análisis narrativo con IA para informes de gerencia (purpose = 'business_report').
--
-- Separado de budget_reservations (agente comercial): los gastos de un informe no cuentan contra las respuestas
-- de WhatsApp y viceversa. Mismo ciclo de vida que el libro comercial:
--   reserved   -> peor caso reservado antes de llamar. Cuenta.
--   settled    -> coste real según el uso verificado. Cuenta.
--   released   -> la API garantizó que no facturó. No cuenta.
--   uncertain  -> no se sabe si facturó. Cuenta hasta conciliar. Nunca se libera automáticamente.
--
-- call_id = clave de idempotencia: tipo de informe + semana + huella del payload agregado. Una misma
-- versión de datos no se paga dos veces. Esta tabla NO guarda prompts, respuestas ni datos personales.

create table if not exists public.analysis_budget_reservations (
  id           uuid primary key default gen_random_uuid(),
  call_id      text not null unique check (char_length(call_id) between 1 and 200),
  purpose      text not null default 'business_report' check (purpose = 'business_report'),
  report_kind  text not null check (report_kind in ('ejecutivo')),
  week_start   date not null,
  fingerprint  text not null check (char_length(fingerprint) = 64),
  status       text not null check (status in ('reserved', 'settled', 'released', 'uncertain')),
  reserved_usd numeric(12, 6) not null check (reserved_usd >= 0),
  counted_usd  numeric(12, 6) not null default 0 check (counted_usd >= 0),
  created_at   timestamptz not null default now(),
  reconciled_at timestamptz,
  -- Una reserva liberada no cuenta: su importe contable debe ser cero (mismo invariante que budget_reservations).
  constraint analysis_budget_released_zero check (status <> 'released' or counted_usd = 0)
);

create index if not exists analysis_budget_created_idx on public.analysis_budget_reservations (created_at);

alter table public.analysis_budget_reservations enable row level security;
revoke all on table public.analysis_budget_reservations from public, anon, authenticated;

-- Backend: registra, liquida y concilia. Administración: solo consulta.
grant select, insert, update on public.analysis_budget_reservations to lacusine_backend;
grant select on public.analysis_budget_reservations to lacusine_admin_ops;

drop policy if exists lacusine_backend_all on public.analysis_budget_reservations;
drop policy if exists lacusine_admin_read on public.analysis_budget_reservations;
create policy lacusine_backend_all on public.analysis_budget_reservations for all to lacusine_backend
  using (true) with check (true);
create policy lacusine_admin_read on public.analysis_budget_reservations for select to lacusine_admin_ops
  using (true);
