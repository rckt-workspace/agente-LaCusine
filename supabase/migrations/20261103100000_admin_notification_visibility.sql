-- Fase 6: el rol administrativo puede consultar el estado de los avisos a asesores (métricas operativas).
-- Solo lectura. La CLI no puede modificar ni borrar avisos; el worker es quien los marca como enviados.

grant select on public.advisor_notifications to lacusine_admin_ops;

drop policy if exists lacusine_admin_read on public.advisor_notifications;
create policy lacusine_admin_read on public.advisor_notifications
  for select to lacusine_admin_ops using (true);
