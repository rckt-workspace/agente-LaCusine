-- Devoluciones y cambios son un motivo de escalamiento propio (Fase 3).
-- Se amplía el enumerado sin tocar las filas existentes.

alter table public.human_handoffs drop constraint human_handoffs_reason_check;

alter table public.human_handoffs
  add constraint human_handoffs_reason_check
    check (reason in ('purchase_intent', 'quote_request', 'installation', 'warranty', 'return_request',
                      'complaint', 'unsupported_request', 'customer_request', 'other'));
