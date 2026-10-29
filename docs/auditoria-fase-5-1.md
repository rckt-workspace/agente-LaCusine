# Auditoría de estabilización — Fases 4 y 5 (5.1)

Registro de hallazgos. Cada uno incluye la evidencia en el código original (commit `271e95e`), la corrección y la prueba que la demuestra. Los hallazgos se clasifican así:

- **Crítico:** impedía una operación segura.
- **Alto:** podía causar un error comercial, una pérdida de mensajes o una violación de política.
- **Medio:** afectaba mantenimiento, operación u observabilidad.
- **Bajo:** documentación o calidad.

## Críticos

### C1. El control humano no se activaba
- **Evidencia:** `PostgresHandoffRepository.open()` insertaba la solicitud, pero nada cambiaba `conversations.automation_state`. Una conversación transferida seguía siendo atendida por la IA.
- **Corrección:** `open()` ahora es atómico: registra la solicitud, pausa la automatización (`automated → paused_for_human`) y cancela las respuestas de IA pendientes, en una sola transacción.
- **Prueba:** `test_opening_a_handoff_pauses_automation_for_every_escalation_reason`, `test_opening_a_handoff_cancels_pending_ai_replies_in_the_same_transaction`, `test_two_processes_opening_the_same_handoff_produce_one_request_and_one_pause`, y `test_handoff_transaction_rolls_back_pause_and_request_if_a_later_step_fails`.
- **Verificación por mutación:** al quitar la pausa, fallan 7 pruebas.

### C2. La línea de negocio no estaba aislada
- **Evidencia:** las salidas no guardaban su línea de origen; el dispatcher usaba siempre la configuración global; el worker procesaba eventos de cualquier línea.
- **Corrección:** MVP de **línea única**, definida explícitamente. El webhook ignora eventos de otras líneas cuando la línea está configurada. El procesador rechaza (permanente) eventos de otras líneas. El dispatcher solo envía desde la línea con la que se generó la salida. El worker no arranca sin `META_PHONE_NUMBER_ID`.
- **Límite que permanece:** `leads.wa_contact_id` sigue siendo único globalmente. Con dos líneas reales, un mismo contacto compartiría lead. Soportar varias líneas exige cambiar el esquema (lead por línea). No se hizo porque el alcance es una línea.
- **Prueba:** `test_outbound_from_another_business_line_is_never_sent`, `test_event_from_another_business_line_is_rejected_permanently_and_stores_nothing`, `test_events_from_another_business_line_are_ignored_when_the_line_is_configured`.
- **Verificación por mutación:** al quitar la comprobación del dispatcher, falla la prueba correspondiente.

### C3. El cuerpo del webhook se leía sin límite
- **Evidencia:** `await request.body()` cargaba el cuerpo completo antes de comparar con `MAX_WEBHOOK_BODY_BYTES`. Un cuerpo fragmentado (sin `Content-Length`) podía ocupar memoria sin límite.
- **Corrección:** `read_limited_body` rechaza por `Content-Length` declarado y corta el streaming en cuanto supera el límite. La firma HMAC sigue calculándose sobre los bytes originales.
- **Prueba:** `test_chunked_oversized_body_is_cut_off_while_streaming` (mide fragmentos consumidos, no solo la respuesta), `test_declared_oversized_body_is_rejected_before_reading_it`, `test_signature_is_computed_over_the_original_bytes_not_a_reserialization`.
- **Nota de método:** `TestClient` consume el generador completo antes de llegar a la aplicación, así que una prueba basada solo en la respuesta HTTP no habría detectado el defecto. La prueba mide directamente el lector.

### C4. El worker deshabilitado terminaba inmediatamente
- **Evidencia:** con `WORKER_ENABLED=false`, `app.worker.main` salía con código 0 al instante. En Render, un Background Worker que termina se reinicia, produciendo un ciclo de reinicios sin sentido.
- **Corrección:** modo de espera. El proceso permanece vivo, no abre conexiones, no lee la cola, registra un latido cada 10 minutos y termina solo con una señal. Además, con `WORKER_ENABLED` falso no se valida el resto de la configuración del worker, para que una variable numérica mal escrita no provoque reinicios.
- **Prueba:** `test_disabled_worker_stays_alive_without_consuming_anything` y `test_disabled_worker_with_invalid_numeric_setting_still_stands_by` ejecutan el proceso real.

## Altos

### A1. La ventana de 24 horas usaba la hora de procesamiento
- **Evidencia:** el procesador grababa cada mensaje con la hora del worker. Una cola atrasada reiniciaba la ventana al momento de procesar, y un mensaje de hace 30 horas podía abrir una ventana.
- **Corrección:** la hora del evento es el campo `timestamp` de Meta (segundos Unix en texto, según la documentación oficial, que no especifica la unidad; la inferencia queda declarada). Solo un timestamp confiable abre la ventana (`window_trusted`). La tolerancia de reloj es de 5 minutos. La ventana se calcula con el máximo de mensajes de confianza, no con el último procesado.
- **Prueba:** `test_delayed_customer_message_does_not_reopen_the_window_at_processing_time`, `test_newer_trusted_message_keeps_window_open_even_if_an_older_one_is_processed_later`, `test_message_without_trusted_timestamp_never_opens_the_window`, y pruebas unitarias de `meta_event_time` con 7 casos inválidos.
- **Verificación por mutación:** al ignorar la marca de confianza, falla la prueba del mensaje no confiable.

### A2. Ninguna confirmación de transferencia al cliente
- **Evidencia:** el escalamiento no enviaba nada al cliente, o enviaba el texto del modelo, que podía prometer algo que la empresa no garantiza.
- **Corrección:** política explícita: **una única confirmación de texto fijo** (`handoff_notice`), que no depende de la IA. Es la única salida que sobrevive al control humano, porque anuncia precisamente el cambio. Sigue sujeta a la ventana de 24 horas.
- **Prueba:** `test_handoff_notice_is_sent_once_and_the_ai_reply_is_not`, `test_handoff_outcome_sends_the_fixed_notice_not_the_model_text`.

### A3. Respuestas de IA pendientes no se cancelaban al transferir
- **Evidencia:** una respuesta generada antes de la transferencia seguía en cola y se habría enviado después.
- **Corrección:** la apertura de la transferencia cancela las respuestas `reply` pendientes en la misma transacción, y el dispatcher las cancela también si la conversación está bajo control humano.
- **Prueba:** `test_opening_a_handoff_cancels_pending_ai_replies_in_the_same_transaction`.

### A4. No existía ningún procedimiento para resolver casos inciertos
- **Evidencia:** `manual_review`, `uncertain`, reservas `reserved`/`uncertain` y handoffs no tenían herramienta operativa.
- **Corrección:** utilidad CLI (`python -m app.admin.cli`), sin endpoints HTTP. Políticas puras de transición, operaciones transaccionales con bloqueo de fila y auditoría en la misma transacción. Nunca devuelve un envío a `pending` ni permite reenviar.
- **Guardas:** rol de base de datos propio (`LACUSINE_ADMIN_DATABASE_URL`, distinta de la de la aplicación), entorno declarado y coincidente, host de nube bloqueado salvo permiso explícito, mutaciones deshabilitadas por defecto, confirmación que debe repetir el identificador del caso, identificador UUID obligatorio para mutar, rol de la conexión comprobado y, en producción, un flag adicional.
- **Prueba:** 33 pruebas unitarias de política y guardas (`tests/admin/`) y 8 de integración de conciliación en PostgreSQL, incluida la **doble resolución concurrente** (exactamente una decisión aplicada).

## Medios

### M1. Un error de guardas leía el entorno del proceso y no el que se pasaba
- **Evidencia:** `_env_true` consultaba `os.environ` aunque `check_guards` recibía otro diccionario. En la ejecución normal no se notaba, pero las guardas no eran comprobables aisladamente y dependían del entorno global.
- **Corrección:** todas las guardas leen el mismo entorno que reciben.

### M2. Clasificación de errores de transporte
- **Evidencia:** un `ConnectTimeout` y un `PoolTimeout` se trataban como resultado incierto.
- **Corrección:** ambos ocurren antes de que la solicitud salga, así que se clasifican como "no enviado" y reintentables. Lectura, escritura, cortes de conexión y protocolo tras enviar siguen siendo inciertos.
- **Prueba:** 7 casos parametrizados de clasificación de transporte.
- **Verificación de lo que no cambió:** un `ReadTimeout` y un `ReadError` siguen siendo inciertos (no se presupone que el envío no llegó).

### M3. Sin prueba con procesos independientes
- **Evidencia:** la concurrencia se probaba solo con hilos.
- **Corrección:** `test_two_independent_processes_claim_disjoint_sets_of_the_same_queue` lanza dos procesos de sistema operativo sobre la misma cola PostgreSQL y comprueba que reclaman conjuntos disjuntos que suman el total.

### M4. Hojas de cálculo y PDF comerciales no estaban excluidos de Git
- **Evidencia:** `.gitignore` no excluía `*.xlsx` ni `*.pdf`; solo `.dockerignore` lo hacía. El brief prohíbe versionar el catálogo original.
- **Corrección:** reglas para `*.xlsx`, `*.xls`, `*.xlsm`, `*.pdf` y `*.csv`. No hay ningún archivo de esos tipos versionado.
- **Prueba:** `test_commercial_spreadsheets_and_pdfs_are_ignored_by_git`.

## Bajos

### B1. Documentación contradictoria
- **Evidencia:** la fase 4 documentaba "no envía", mientras el código de la fase 5 sí enviaba (desactivado). El `render.yaml` y la guía de Render describían un worker que sale inmediatamente.
- **Corrección:** documentos alineados con el comportamiento real. Se distingue implementado, desactivado y verificado.

## Hallazgos revisados y no confirmados

- **"El dispatcher reenvía tras un timeout de Meta":** no ocurre. Un timeout tras enviar se marca `uncertain` y el dispatcher nunca lo vuelve a reclamar. Verificado con `test_lease_expiry_makes_send_uncertain_and_it_is_never_resent` y con la mutación que lo habría provocado.
- **"Dos workers reclaman el mismo evento":** no ocurre. Verificado con procesos y con hilos.
- **"Un handoff duplicado crea dos filas":** no ocurre. La restricción única parcial lo impide.
- **"Un estado de Meta retrocede":** no ocurre. Las transiciones están en una tabla explícita.

## Revisión final de preproducción

Hallazgos detectados en la verificación de roles, dependencias y arranque. Se corrigen en la misma rama y PR.

### C5. Los roles no propietarios no podían usar la base (crítico, corregido)
- **Evidencia:** sonda con un rol de login sin `BYPASSRLS` y con privilegios concedidos. `select count(*) from public.leads` devolvía `0` sin error; `insert` fallaba con `new row violates row-level security policy`; `webhook_events` daba `permission denied`. Las tablas tenían RLS activa y ninguna política. El backend solo funcionaba como propietario, que la documentación no permite usar.
- **Corrección:** migración `20261101100000_app_roles_rls_policies.sql`. Roles de grupo sin login (`lacusine_backend`, `lacusine_admin_ops`), privilegios por tabla según las sentencias reales del código y políticas solo para esos grupos. Los roles de login se crean fuera del repositorio.
- **Prueba:** `tests/integration/test_roles_rls_postgres.py` (15 pruebas), con conexiones reales de cada rol. Sin la migración, fallan el flujo de la aplicación, el de administración y el E2E (el webhook devuelve 503).
- **Verificado:** `anon`, `authenticated` y `public` no tienen políticas ni privilegios. La auditoría es de solo adición, incluso para el rol administrativo.

### A5. Vulnerabilidad moderada en dependencias (alta, corregida)
- **Advisory:** CVE-2025-71176 / GHSA-6w46-j5rx-g56g, `pytest` anterior a 9.0.3, "vulnerable tmpdir handling".
- **Alcance:** dependencia directa de desarrollo (`requirements-dev.txt`), no de ejecución. No afecta a la API ni al worker en producción.
- **Corrección:** `pytest==8.4.2` → `pytest==9.0.3`, la versión corregida mínima. No se cambió nada más. Suite completa ejecutada después del cambio.

### M5. `/ready` revelaba el motivo de la falta de configuración (medio, corregido)
- **Evidencia:** sin base, devolvía `{"status": "not_configured"}`, distinto de `unavailable`. La documentación dice "sin detalles".
- **Corrección:** ambos casos devuelven `{"status": "unavailable"}` con 503.

### M6. Comentario de `render.yaml` (bajo, sin defecto)
- Durante la revisión, un reemplazo mío duplicó una palabra en un comentario. Se restauró y se comprobó que el archivo coincide con el original. El contrato de Render no cambió.

## Limitaciones de la revisión final

1. **Supabase Cloud no se ha probado.** Los roles y privilegios se han probado solo en PostgreSQL local desechable. Supabase puede tener particularidades en sus roles por defecto.
2. **El pooler en modo transacción (6543) no se ha probado** con un servicio real. La compatibilidad se apoya en el diseño (`prepare_threshold=None`) y en la documentación.
3. **El apagado con `SIGTERM` en Windows no es concluyente** (código 143 al terminar desde Git Bash). Debe comprobarse en Linux, el entorno de Render.
4. **La auditoría sigue siendo modificable por un rol propietario.** Ninguna política evita que el propietario del esquema actúe; solo lo impide la separación de roles.

## Limitaciones que permanecen

1. **Una sola línea de negocio.** Diseño intencional del MVP, no aislamiento multi-tenant.
2. **Envío interrumpido tras Meta.** Si la base falla justo después de que Meta acepte el mensaje, la salida queda `uncertain` (no hay identificador que la confirme). Un estado de Meta no puede conciliarla porque no conoce el identificador. Debe resolverse con la CLI, con evidencia del panel de Meta.
3. **Un contacto con revisión pendiente queda bloqueado.** Es intencional: nunca se responde fuera de orden.
4. **Texto de confirmación fijo.** Puede no adaptarse a todos los casos comerciales. Su redacción requiere validación del equipo.
5. **Auditoría protegida solo a nivel de privilegios.** El rol propietario puede modificarla; la aplicación no lo hace.
6. **Plantillas y atribución de anuncios.** Sin cambios en esta fase.
7. **Cálculo de la hora de Meta.** Se asume segundos Unix según la inferencia documentada.
8. **Sin verificación visual** del diagrama en navegador.
9. **Productos retirados del catálogo permanecen.** La importación solo hace `upsert`; no borra. Ver [catalogo-operacion.md](catalogo-operacion.md).
10. **Precio confirmado sin flujo.** `products.price_confirmed_at` existe, pero ningún código lo escribe. Es un paso manual pendiente de decisión.
11. **Vocabulario de búsqueda en memoria.** Tras reimportar el catálogo hay que reiniciar el proceso que lo usa.
