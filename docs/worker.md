# Worker y procesamiento confiable — Fase 4

Estado: implementado y probado contra PostgreSQL local. **El envío a WhatsApp existe pero está desactivado por defecto** (ver [whatsapp-integration.md](whatsapp-integration.md)): con `WHATSAPP_DRY_RUN=true` o `META_SEND_ENABLED=false` ninguna solicitud sale. No llama a Claude en producción ni se conecta a Supabase Cloud. El proveedor de IA del worker es el simulado (`AGENT_DRY_RUN=true`).

Decisión de cola: [ADR 0001](adr/0001-postgresql-as-job-queue.md).

## Arquitectura en ejecución

```
WhatsApp Cloud API ──► FastAPI /webhook  (valida firma, persiste en webhook_events)
                              │
                              ▼
                    PostgreSQL / Supabase  (cola: webhook_events + contact_leases)
                              │
                              ▼
   python -m app.worker.main  (proceso independiente)
     recover_expired ─► claim (SKIP LOCKED) ─► EventProcessor ─► CommercialAgent.handle()
                                                   │                 │
                                                   │                 ├─ reserva presupuestaria (budget_reservations)
                                                   │                 └─ proveedor simulado / Anthropic (deshabilitado)
                                                   ▼
                                  outbound_messages (pending) + leads.status + human_handoffs
                                                   │
                                                   ▼
                                  Adaptador de envio WhatsApp  ── FASE POSTERIOR, no implementado
```

Procesos separados: la API y el worker comparten base de datos y nada más. El worker **no** se arranca desde FastAPI ni con `--reload`.

## Ejecución

```powershell
# API (sin worker)
.venv\Scripts\python.exe -m uvicorn app.main:app

# Worker: desactivado por defecto
$env:WORKER_ENABLED = "true"
$env:DATABASE_URL = "postgresql://..."   # base LOCAL de prueba; nunca producción sin autorización
.venv\Scripts\python.exe -m app.worker.main
```

Apagado: `Ctrl+C` o `SIGTERM` dejan de reclamar trabajos nuevos y esperan a los que están en curso. Los leases pendientes vencen solos si el proceso muere.

## Ciclo de vida y modo espera (fase 5.1)

| Estado del proceso | Cómo se produce | Consume la cola | Terminal |
|---|---|---|---|
| No creado | El servicio no existe en Render | No | — |
| Detenido | El servicio está parado desde el panel | No | Sí |
| En espera | `WORKER_ENABLED` distinto de `true` | **No** (no abre conexión a la base) | No; registra `worker_standby` cada 10 minutos |
| Consumiendo | `WORKER_ENABLED=true` con la configuración completa | Sí | No |

- **En espera no sale.** Un Background Worker que termina se reinicia en Render. Por eso el proceso permanece vivo sin tocar la base, y solo termina con `SIGTERM` o `Ctrl+C`.
- Con `WORKER_ENABLED=true`, la configuración se valida antes de abrir el pool. Faltan `DATABASE_URL` o `META_PHONE_NUMBER_ID`, o un valor es inválido: el proceso sale con código 2 y no entra en bucle (el panel de Render lo mostrará como fallo de arranque).
- Con `WORKER_ENABLED` distinto de `true`, ningún otro valor se valida. Así una variable numérica mal escrita no provoca reinicios mientras el worker está en espera.
- Activar `WORKER_ENABLED=true` sobre una base real exige comprobar antes los efectos (ver [go-live-checklist.md](go-live-checklist.md)).

Las pruebas de `tests/test_worker_process.py` ejecutan el proceso real: en espera sigue vivo sin consumir, con un valor numérico inválido sigue en espera, con `WORKER_ENABLED=true` sin base sale con código 2 y sin `META_PHONE_NUMBER_ID` también sale con código 2.

## Reglas de la fase 5.1: transferencia, ventana y línea

- **Transferencia atómica.** Abrir una transferencia pausa la automatización (`paused_for_human`), cancela las respuestas de IA pendientes y registra la solicitud, todo en una transacción. Abrir la misma transferencia dos veces, o desde dos procesos, produce una sola solicitud y una sola pausa.
- **Una sola confirmación al cliente.** La confirmación de transferencia es un texto fijo (`handoff_notice`), no texto del modelo. Es la única salida que se envía con la automatización pausada, y sigue sujeta a la ventana de 24 horas.
- **Reactivación solo auditada.** Un mensaje nuevo del cliente no reactiva la automatización. Solo `resume-conversation` de la CLI de administración la devuelve, y únicamente sin transferencias activas.
- **Ventana de 24 horas con hora de Meta.** La hora del mensaje es el campo `timestamp` que envía Meta (segundos Unix, unidad inferida de la documentación). Se acepta si es numérica, no anterior a 2020 y no más de 5 minutos en el futuro. Un mensaje sin timestamp confiable se guarda, pero **nunca abre la ventana**. La hora de procesamiento solo sirve para ordenar; un evento atrasado no extiende la ventana.
- **Una línea de negocio.** El MVP admite una sola línea (`META_PHONE_NUMBER_ID`). Eventos y salidas de otra línea se rechazan: el webhook los ignora y el procesador los marca como error permanente, y el dispatcher nunca envía desde una línea distinta de la que generó la salida.

## Herramienta de conciliación

`python -m app.admin.cli` resuelve casos que requieren una persona. Es una herramienta de línea de comandos, sin endpoints HTTP. Subcomandos: `status`, `inspect`, `audit`, `resolve-event`, `resolve-outbound`, `resolve-reservation`, `resolve-handoff`, `resume-conversation`.

- Solo muta si se cumplen todas las guardas: rol propio de base de datos, entorno declarado y coincidente, host de nube bloqueado salvo permiso explícito, mutaciones habilitadas, identificador UUID y confirmación que repite ese identificador.
- Ninguna resolución devuelve un envío a `pending` ni reenvía un mensaje. Un envío `uncertain` solo pasa a `sent` o `failed` con una referencia externa.
- Cada decisión queda en `admin_audit_log` con operador, fecha, motivo y referencia de evidencia, en la misma transacción. No registra texto del cliente ni secretos.
- Detalle de los estados permitidos y de las guardas: [auditoria-fase-5-1.md](auditoria-fase-5-1.md) y el código de `app/admin/policy.py`.

## Estados de un evento

| Estado | Significado | Terminal |
|---|---|---|
| `received` / `queued` | Pendiente de adquisición | No |
| `processing` | Reclamado por un worker, con lease y token de propiedad | No |
| `retry` | Fallo transitorio; `next_attempt_at` marca el próximo intento con backoff | No |
| `processed` | Procesado: respuesta generada o transferencia registrada. Payload eliminado | Sí |
| `ignored` | Descartado con motivo justificado (p. ej. `human_control`). Payload eliminado | Sí |
| `manual_review` | Resultado incierto, reserva ambigua o reintentos agotados. Requiere una persona | No (bloquea el contacto) |
| `dead_letter` | Error permanente (payload inválido, proveedor con autenticación rechazada) | Sí |

Restricciones en la base: `processing` ⇔ tiene `lease_token`; `processed`/`ignored` ⇔ tienen `processed_at`.

## Adquisición segura

- **Atómica:** `SELECT … FOR UPDATE SKIP LOCKED` dentro de una transacción corta. Dos workers nunca reclaman el mismo evento (prueba con dos pools concurrentes).
- **Propiedad por token:** cada adquisición genera un `lease_token`. Solo ese token puede renovar, finalizar o liberar el trabajo.
- **Lease:** `WORKER_LEASE_SECONDS`, siempre mayor que `WORKER_PROCESSING_TIMEOUT_SECONDS` (validado al arrancar).
- **Renovación exige lease vigente:** un worker cuyo lease ya venció no puede extenderlo; se considera perdido.
- **Recuperación:** `recover_expired` devuelve a `retry` (o a `manual_review` si se agotaron intentos) los trabajos con lease vencido.
- **Propietario antiguo rechazado:** `finish` solo escribe si el token coincide y el estado sigue en `processing`. Una prueba verifica que un worker recuperado no puede sobrescribir el resultado del nuevo propietario.
- **Lote limitado:** `WORKER_BATCH_SIZE`. Las transacciones de adquisición no esperan a ningún proveedor.

## Orden por conversación

- Solo se reclama el evento **más antiguo no finalizado** de cada contacto (`business_phone_id` + `wa_id`). Un evento posterior espera a que el anterior termine o pase a `manual_review`.
- `contact_leases` (clave primaria `contact_key`) impone la exclusión entre workers, incluso con carreras.
- Contactos distintos, y la misma persona en líneas de negocio distintas, se procesan en paralelo.
- Las pruebas verifican el orden de llegada con dos workers simultáneos.

## Garantías de idempotencia

Tres niveles, que no deben confundirse:

1. **Idempotencia interna** (base de datos): `webhook_events (provider, external_event_id)` único; `messages.wa_message_id` único; `outbound_messages.idempotency_key` único y `webhook_event_id` único por evento; `human_handoffs` con una sola solicitud activa por conversación. Un reintento de la misma entrada no duplica nada.
2. **Llamadas externas:** una llamada al modelo se reintenta **solo** si la API garantiza que no facturó (`rate_limited`, `busy`, `auth_failed`, `disabled`). Un timeout, un 5xx o una conexión interrumpida dejan la reserva en `uncertain` y el caso va a revisión manual. Nunca se reenvía automáticamente.
3. **Facturación:** cada llamada facturable tiene una reserva persistente antes de ejecutarse. Ver "Presupuesto".

Limitación explícita: ningún sistema puede garantizar que un proveedor externo ejecutó exactamente una vez si devuelve un resultado incierto. Por eso la política es **no repetir** y **escalar a una persona**.

## Respuestas: generada ≠ enviada ≠ entregada

- Un resultado de IA crea una fila en `outbound_messages` con `delivery_status = 'pending'`. El dispatcher la reclama, aplica las reglas de control humano y ventana, y la envía solo si el envío está autorizado (ver [whatsapp-integration.md](whatsapp-integration.md)).
- `sent`, `delivered`, `failed` y `cancelled` existen en el modelo, pero solo los escribirá el adaptador de WhatsApp en una fase posterior.
- La respuesta y el estado comercial del lead se escriben en **una sola transacción**. Si la inserción falla, la actualización del lead no se aplica (prueba de integración).
- Una respuesta por evento, como máximo.

## Presupuesto con reservas

La fase 3 estimaba el gasto antes de cada llamada leyendo totales. Eso no era atómico entre workers. Ahora:

| Estado de la reserva | Cuenta para el presupuesto | Cómo llega |
|---|---|---|
| `reserved` | Sí, por el peor caso | Antes de llamar al proveedor |
| `settled` | Sí, por el costo real estimado | La llamada tuvo éxito y la auditoría se registró |
| `released` | No | Error garantizado como no facturado |
| `uncertain` | Sí, hasta conciliar | Timeout, 5xx, conexión interrumpida |

- La reserva se hace bajo `pg_advisory_xact_lock`, que dura solo la transacción de reserva; **nunca durante la llamada al modelo**.
- Límites diario (hora de Colombia), mensual y por interacción. Una prueba con 8 hilos verifica que nunca se supera el límite diario.
- Un `call_id` con reserva `reserved` o `uncertain` **no vuelve a llamar** al modelo: el caso pasa a `manual_review`.
- Si la auditoría falla después de una llamada exitosa, la reserva queda `reserved` y no se repite la llamada.

## Errores y reintentos

| Clase | Ejemplos | Tratamiento |
|---|---|---|
| Transitoria | Error de base, `busy`, `rate_limited` | `retry` con backoff exponencial y jitter; tras `WORKER_MAX_ATTEMPTS` → `manual_review` |
| Permanente | Texto vacío o ausente, remitente inválido, autenticación del proveedor rechazada | `dead_letter` sin reintentos |
| Incierta | Timeout, 5xx, conexión interrumpida, reserva ambigua | `manual_review` sin reintento automático |

Los códigos de error son estables y no contienen datos del cliente. Los logs registran el tipo de excepción, nunca su mensaje.

## Retención y recuperación

La retención de la fase 2 borraba eventos por fecha, sin mirar su estado. Se sustituyó (migración `20261029100100`) por una versión que **solo elimina estados finales**: `processed`, `ignored`, `dead_letter`. Nunca elimina `received`, `queued`, `retry`, `processing` ni `manual_review`. Las respuestas en outbox solo se eliminan si están cerradas (`sent`, `delivered`, `failed`, `cancelled`).

El payload se elimina al finalizar (`processed`/`ignored`). El contenido de los mensajes sigue la fecha de expiración de la fase 2.

## Observabilidad

- Logs estructurados por evento: `job_finished event=<uuid> outcome=<estado> code=<código> attempt=<n>`. Sin contactos, sin textos.
- `Worker.metrics`: procesados, ignorados, reintentos, dead letter, revisión manual, leases perdidos, recuperados, lentos.
- `JobRepository.counts()`: conteo por estado y leases vencidos, para consulta directa.
- `GET /health`: liveness, sin dependencias.
- `GET /ready`: readiness; comprueba PostgreSQL y devuelve 503 sin detalles si no responde.

Pendiente: exportar métricas a un sistema de monitoreo (fase de despliegue).

## Preparación para Render (no desplegado)

Tres servicios, con la misma configuración de base de datos:

| Servicio | Comando | Tipo |
|---|---|---|
| API | `uvicorn app.main:app --host 0.0.0.0 --port $PORT` | Web Service |
| Worker | `python -m app.worker.main` | Background Worker |
| Base de datos | Supabase PostgreSQL (externo) | — |

Antes de desplegar se requiere: autorización, proyecto Supabase real con las migraciones aplicadas tras revisión, rol de base de datos de mínimo privilegio, `ANTHROPIC_*` y tarifas revisadas, y `/ready` verificado.

## Limitaciones conocidas

1. **Presupuesto y auditoría en la misma base, no en la misma transacción que la llamada externa.** Un fallo entre la llamada y la liquidación deja la reserva en `reserved`, que se concilia a mano. No hay conciliación automática todavía.
2. **Timeout de procesamiento.** No se pueden matar hilos de Python. `WORKER_PROCESSING_TIMEOUT_SECONDS` se aplica como advertencia (`job_slow`) y como límite del proveedor; si el proceso se queda colgado, el lease vence y el trabajo se recupera.
3. **Un contacto con `manual_review` queda bloqueado.** Es intencional (no responder fuera de orden). La resolución se hace con la CLI de conciliación, con motivo y auditoría; no hay una interfaz gráfica.
4. **Envío real deshabilitado.** El adaptador y el dispatcher están implementados y probados con transporte simulado. Hasta la autorización de la fase de salida, las respuestas quedan `pending`.
5. **Vocabulario de búsqueda en memoria por proceso.** Tras reimportar el catálogo hay que reiniciar el worker.
6. **Volumen.** PostgreSQL es suficiente para el volumen previsto. Si la tabla de eventos crece demasiado, el ADR 0001 describe la migración del `JobRepository` a otro broker sin tocar el procesamiento.
7. **Pruebas de carga no realizadas.** La concurrencia se verificó con 2 a 8 hilos, no con cargas de producción.

## Pruebas

- Unitarias del runner (reintentos, agotamiento, propiedad, apagado, orden por contacto), del procesador (cada resultado del agente) y de la máquina de estados de las reservas.
- Integración con PostgreSQL local: adquisición con dos workers, orden por contacto, líneas de negocio en paralelo, recuperación por lease vencido, propietario antiguo rechazado, reinicio, outbox idempotente, atomicidad del outbox, reservas concurrentes del presupuesto, ciclo de vida de las reservas, retención segura, flujo extremo a extremo.
- Fase 5.1: transferencia atómica y pruebas de concurrencia (`tests/integration/test_hardening_postgres.py`), dos procesos independientes sobre la misma cola (`tests/integration/test_independent_processes.py`), ciclo de vida del proceso real (`tests/test_worker_process.py`), tiempo de evento y líneas (`tests/worker/test_event_time_and_lines.py`).
- La regla de propiedad se comprobó con una mutación sobre el doble en memoria: al quitar la comprobación de token, la prueba de propietario antiguo falla. La versión SQL se verifica con la prueba de integración de lease vencido.

## Notificaciones a asesores (fase 6)

- Cada transferencia encola un aviso en `public.advisor_notifications` **dentro de la misma transacción** que la abre. Una transferencia nunca existe sin su aviso.
- El despachador corre en el ciclo del worker, después del outbox. Usa leases: un aviso con lease vencido vuelve a `pending`, nunca se marca como enviado.
- El aviso contiene solo identificadores, motivo y prioridad. No contiene nombre, teléfono ni texto del cliente.
- Hoy solo existe el canal simulado. Un canal real debe implementar `AdvisorNotifier` y deduplicar por `notification_id`.
- Errores del canal: reintentables (vuelven a `pending` hasta agotar intentos) o no reintentables (fallan de inmediato). Un error inesperado no detiene el ciclo y su detalle no se registra.

## Herramienta de migraciones (fase 6)

`python -m app.db.migrate --dir supabase/migrations` lista lo pendiente. Con `--apply` aplica cada archivo en su propia transacción y registra su hash. Un archivo ya aplicado cuyo contenido cambió detiene la operación. Ni la API ni el worker la ejecutan.

## Entorno local Docker (fase 6)

Guía paso a paso en [local-development.md](local-development.md). Desarrollo y validación sin servicios externos. El worker arranca en espera.
