-- Corrección del historial comercial (sucesora de 20261105110000, ya aplicada en Supabase Cloud).
--
-- Defecto: commercial_events.conversation_id, commercial_events.product_id y sales.conversation_id usaban
-- ON DELETE SET NULL. Ese borrado en cascada ejecuta un UPDATE sobre la fila hija, y los triggers
-- forbid_history_changes (append-only) lo rechazan. Resultado: borrar una conversación o un producto con
-- historial comercial falla con "historial comercial: operación no permitida (UPDATE)" de forma opaca.
--
-- Corrección: ON DELETE RESTRICT explícito. Se mantiene la inmutabilidad: el historial nunca se reescribe y
-- el borrado queda bloqueado con un error claro (23503), en lugar de un intento de UPDATE.
--
-- Alcance: solo cambia el comportamiento de borrado de tres claves foráneas. No modifica filas, no cambia
-- triggers, privilegios ni RLS. ALTER TABLE ... ADD CONSTRAINT valida filas existentes con SELECT: no dispara
-- los triggers de UPDATE. lead_id ya es RESTRICT desde 20261105110000 y no se toca aquí.
--
-- Privacidad: RESTRICT no añade bloqueos frente al borrado de prospectos. Todo evento o venta que apunta a una
-- conversación también apunta a su lead (lead_id RESTRICT), así que el borrado de un prospecto con historial
-- ya estaba bloqueado. Ver docs/respaldo-y-restauracion.md, sección "Privacidad y borrado".

alter table public.commercial_events drop constraint if exists commercial_events_conversation_id_fkey;
alter table public.commercial_events
  add constraint commercial_events_conversation_id_fkey
  foreign key (conversation_id) references public.conversations (id) on delete restrict;

alter table public.commercial_events drop constraint if exists commercial_events_product_id_fkey;
alter table public.commercial_events
  add constraint commercial_events_product_id_fkey
  foreign key (product_id) references public.products (id) on delete restrict;

alter table public.sales drop constraint if exists sales_conversation_id_fkey;
alter table public.sales
  add constraint sales_conversation_id_fkey
  foreign key (conversation_id) references public.conversations (id) on delete restrict;

-- Comprobación final: si alguna clave no quedó con RESTRICT, la migración falla y se revierte entera.
do $$
begin
  if exists (
    select 1
    from pg_constraint
    where contype = 'f'
      and conname in ('commercial_events_conversation_id_fkey', 'commercial_events_product_id_fkey',
                      'sales_conversation_id_fkey')
      and confdeltype <> 'r'
  ) then
    raise exception 'la clave foránea comercial no quedó con ON DELETE RESTRICT';
  end if;
end
$$;
