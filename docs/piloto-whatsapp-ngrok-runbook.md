# Runbook del piloto de WhatsApp con ngrok (Windows / PowerShell)

Procedimiento para reanudar el piloto local: FastAPI, ngrok, Meta WhatsApp Cloud API, Supabase y **un único** worker. Todos los comandos se ejecutan desde la raíz del repositorio. Nunca pegues tokens, contraseñas ni números de clientes en chats, capturas o commits.

Convención: `<PLACEHOLDER>` indica un valor que tú pones en tu entorno local.

---

## 1. Preparar Windows PowerShell y `.venv`

```powershell
cd <RUTA_DEL_REPO>\agente-LaCusine
# Entorno virtual del proyecto (no uses el Python global)
.\.venv\Scripts\python.exe --version              # Python 3.11.x
.\.venv\Scripts\python.exe -m pip install -r requirements.txt --quiet
.\.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider --ignore=tests/browser --ignore=tests/integration
```

Esperado: todas las pruebas unitarias pasan (1 omitida). Si falla algo, no sigas.

## 2. Cargar el `.env` local de forma segura

- El archivo `.env` está en la raíz y **no se versiona** (`.gitignore`). Contiene los secretos.
- Se carga solo con `LACUSINE_LOAD_DOTENV=true`. Nunca sobrescribe variables que ya existan en la sesión de PowerShell.
- Plantilla sin valores reales: `.env.example`. Copia `.env.example` a `.env` y rellena tus valores **localmente**.
- No pegues el contenido de `.env` en ningún chat ni commit.

Para FastAPI y el worker, define en cada terminal:

```powershell
$env:LACUSINE_LOAD_DOTENV = "true"
```

## 3. Comprobar las variables efectivas sin imprimir secretos

```powershell
.\.venv\Scripts\python.exe scripts\pilot_preflight.py --expect-model claude-sonnet-5-5
```

El preflight imprime `[OK]` o `[FALLO]` por cada comprobación y **nunca** muestra claves ni tokens. Comprueba:
- Proveedor `anthropic`, modelo, tarifas ligadas al modelo, presupuesto, kill switch.
- Envío a Meta habilitado y lista de destinatarios no vacía (solo cuenta cuántos).
- Token de Meta vigente (lectura de solo lectura, sin enviar mensajes).
- Trabajo pendiente o incierto en la base (solo conteos).

Si una comprobación falla, **no arranques** el piloto. Corrígela primero.

### Cola de trabajo: qué bloquea y qué no

El preflight clasifica el estado de la cola en solo lectura. **Solo lo CRÍTICO impide el arranque**:

- **CRÍTICO:** un evento con lease vigente (otro proceso lo procesa ahora mismo; espera a que venza el lease, unos 2 minutos), reservas de presupuesto sin liquidar, respuestas en envío, inciertas o pendientes de revisar, y llamadas de IA de resultado desconocido.
- **RECUPERABLE:** eventos en `processing` con lease vencido. El worker los devuelve a la cola al arrancar (`worker_recovered_expired`). No se reenvía nada que Meta pudo aceptar.
- **PENDIENTE:** eventos `received`, `queued` o `retry`. Se procesan al arrancar.
- **HISTÓRICO:** `dead_letter` (p. ej. `missing_sender`), `manual_review` de eventos de estado sin contacto, y respuestas fallidas antiguas. No bloquean el servicio. Un `manual_review` **de un contacto** retiene solo la conversación de ese contacto.

Tras un apagado normal o una interrupción, el arranque recupera el trabajo interrumpido sin intervención en Supabase. Después de `worker_started` verifica en el registro la línea `Recuperación al arrancar`.

## 4. Levantar FastAPI en `127.0.0.1:18011`

Terminal 1:

```powershell
cd <RUTA_DEL_REPO>\agente-LaCusine
$env:LACUSINE_LOAD_DOTENV = "true"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 18011
```

Debe escuchar **solo en loopback**. Comprobación local (sin Meta):

```powershell
Invoke-WebRequest http://127.0.0.1:18011/docs -UseBasicParsing | Select-Object StatusCode
```

## 5. Levantar ngrok apuntando a ese puerto

Terminal 2 (ngrok ya autenticado en tu equipo con tu propio token, que no va en el repositorio):

```powershell
ngrok http 18011
```

Deja esta ventana abierta mientras haya piloto.

## 6. Obtener la URL HTTPS vigente

En la salida de ngrok aparece `Forwarding https://<SUBDOMINIO>.ngrok-free.app -> http://localhost:18011`. Esa URL **cambia** cada vez que reinicias ngrok (salvo dominio reservado). Anótala solo en tu sesión.

Comprobación sin datos sensibles:

```powershell
Invoke-WebRequest "https://<SUBDOMINIO>.ngrok-free.app/docs" -UseBasicParsing | Select-Object StatusCode
```

## 7. Configurar la URL `/webhook` en Meta

En Meta for Developers, en tu aplicación de WhatsApp → **Configuración → Webhooks**:
- **URL de devolución de llamada:** `https://<SUBDOMINIO>.ngrok-free.app/webhook`
- **Token de verificación:** el valor de `META_VERIFY_TOKEN` de tu `.env` (no lo copies en ningún documento).
- Esta URL debe coincidir exactamente con el prefijo `/webhook` de la aplicación (`app/api/webhook.py`).

Cada vez que cambie la URL de ngrok, **actualiza este campo**.

## 8. Comprobar el handshake y la suscripción `messages`

- Meta envía `GET /webhook?hub.mode=subscribe&hub.verify_token=…&hub.challenge=…`. La aplicación responde el `hub.challenge` solo si el token coincide. Si pulsas «Verificar y guardar» y Meta confirma, el handshake está bien.
- En la misma pantalla, activa el campo **`messages`** para la aplicación. Los eventos de estado de entrega llegan por el mismo campo.
- Un handshake con token incorrecto responde **403** (`verification_failed`). No pruebes tokens reales en la terminal.

## 9. Verificar permisos del usuario del sistema y la cuenta de pruebas

En Meta Business Manager:
- El **usuario del sistema** debe tener acceso a la cuenta de WhatsApp Business y a la aplicación, con permisos de mensajería (`whatsapp_business_messaging`) y gestión (`whatsapp_business_management`).
- El **token de acceso** debe ser **permanente** (token de usuario del sistema). Los tokens de sesión caducan: un token expirado da HTTP 401 con código 190, subcódigo 463.
- El número de **prueba** (API Setup) y sus destinatarios verificados. Si el número es de prueba, Meta solo entrega a destinatarios de esa lista (error 131030 si no lo están).
- **Rota** cualquier token que haya aparecido en capturas o chats, aunque siga vigente.

## 10. Arrancar exactamente un worker con `scripts/pilot_start.ps1`

Terminal 3:

```powershell
cd <RUTA_DEL_REPO>\agente-LaCusine
.\scripts\pilot_workers.py list --repo .   # no debe haber workers vivos
.\scripts\pilot_start.ps1                  # solo inspecciona: preflight, sin arrancar
.\scripts\pilot_start.ps1 -Start           # arranca UN worker tras el preflight
```

- `-Start` **aborta** si ya hay un worker de este repositorio vivo.
- Con procesos antiguos, `-StopOldWorkers -Start` los detiene de forma verificada antes de arrancar. Solo toca procesos de este repositorio.
- Tras arrancar, el script comprueba que exista **exactamente uno** y muestra la línea `worker_profile` (sin secretos).
- Los registros van a `logs\worker-<fecha>.err.log` y `.out.log` (ignorados por git).

## 11. Verificar la conexión a Supabase

Sin escribir nada:

```powershell
.\.venv\Scripts\python.exe scripts\pilot_preflight.py --expect-model claude-sonnet-5-5
```

La sección «Trabajo pendiente» consulta conteos en **solo lectura**. El host aparece en la línea `worker_profile` como `database_environment=<host>/<base>`, sin usuario ni contraseña.

## 12. Ejecutar una consulta comercial controlada

1. Desde el **número de prueba autorizado**, envía **un solo mensaje**, por ejemplo «quiero una cafetera Jura».
2. Revisa el log del worker. Debes ver, en orden: `worker_profile`, `provider_invoked`, `llm_output_stats` (con `out_tokens`, `blocks`, `truncated=false`), `reply_generated`, `reply_queued`, `job_finished`, `whatsapp_send_accepted http=200`, `dispatch_cycle … sent=1` y, después, `reply_status_from_meta status=sent|delivered|read`.
3. Un seguimiento: «¿cuál me sirve para espresso?». Mismo patrón.

No envíes ráfagas de mensajes. Cada llamada tiene coste.

## 13. Inspeccionar logs, costos y estados de entrega

- **Logs:** `logs\worker-*.err.log`. Solo identificadores, modelos, conteos y códigos. Nunca prompts, respuestas ni pensamiento.
- **Costos:** `llm_usage_events` (solo lectura) muestra proveedor, modelo, estado y coste estimado por llamada.
- **Entrega:** `outbound_messages.delivery_status` pasa de `pending` a `sent`, y después a `delivered` o `read` desde los eventos de Meta. **HTTP 200 de Meta no equivale a entregado.**
- **Transferencias:** `human_handoffs`. Una conversación con transferencia abierta queda pausada.

## 14. Detener worker, FastAPI y ngrok sin afectar otros proyectos

```powershell
# 1. Worker del piloto (solo procesos de este repositorio, con espera verificada)
.\scripts\pilot_workers.py stop-wait --repo .
.\scripts\pilot_workers.py check --repo . --expect 0

# 2. FastAPI del piloto (identifica por ruta del repositorio y puerto; no detengas nada más)
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'uvicorn app.main:app' -and $_.CommandLine -match '18011' }

# 3. ngrok del piloto (solo si apunta a 18011)
Get-CimInstance Win32_Process -Filter "Name='ngrok.exe'" | Where-Object { $_.CommandLine -match '18011' }
```

Para detener un proceso concreto, usa `Stop-Process -Id <PID>` **después de confirmar el PID con la línea de comando**. No termines procesos de otros proyectos, contenedores Docker ni bases de datos. No borres datos, volúmenes, logs privados ni respaldos.

---

## Resolución de problemas

| Síntoma | Causa probable | Acción |
|---|---|---|
| Preflight: token de Meta «FALLO» / 401 código 190 | Token caducado o revocado | Renovar el token permanente del usuario del sistema. Actualizar `.env`. No arrancar hasta que pase. |
| ngrok cambió de URL; Meta no recibe eventos | La URL del webhook en Meta es antigua | Actualizar la URL en Meta → Webhooks y volver a verificar. |
| Webhook sin eventos (HTTP 200 pero nada se procesa) | El campo `messages` no está suscrito o el worker no corre | Revisar la suscripción (sección 8) y que haya un worker (sección 10). |
| `budget_blocked` / `interaction_limit` | El límite por interacción es menor que el peor caso | Subir `AGENT_INTERACTION_LIMIT_USD` **con aprobación** (peor caso con salida de 700: 0,0166). No lo subas por tu cuenta. |
| Respuesta truncada (`truncated=true`) | Salida agotada | Mantener `between_tools`, esfuerzo `low` y 700 tokens. El cliente recibe un mensaje neutro; no hay reintento facturable automático. |
| Error de RLS o permisos en Supabase | El rol de conexión no tiene permisos | Revisar `DATABASE_URL` y el rol `lacusine_app`. No cambies roles en producción sin revisión. |
| Destinatario no autorizado (`recipient_not_allowed` o 131030) | El número no está en la lista permitida ni en la lista de prueba de Meta | Añadirlo en Meta (API Setup) y en `WHATSAPP_ALLOWED_RECIPIENTS` (escenario A). |
| 131047 (ventana de 24 horas) | Han pasado más de 24 horas desde el último mensaje del cliente | Solo se responde con texto dentro de la ventana. Usa una plantilla aprobada. |
| 130429 / 131056 | Límite de rendimiento o de mensajes al mismo número | Transitorio: el sistema reintenta con espera. Reduce la frecuencia de pruebas. |
| 400 de envío sin código conocido | Causa aún no confirmada | Revisa la línea `meta_api_rejected ... code=…` del log; ese código indica la causa exacta. |

---

## Escenario A: varios destinatarios de prueba (mismo número de prueba)

Soportado para pruebas controladas. Cada destinatario tiene su propio contacto, lead y conversación, aislados por `wa_contact_id`.

1. **En Meta (API Setup):** añade hasta **cinco** destinatarios verificados al número de prueba. Confirma el límite vigente en el panel de Meta.
2. **Consentimiento:** cada tester debe aceptar que sus mensajes se procesen con IA y se consuma presupuesto. Registra el consentimiento fuera del repositorio.
3. **Lista blanca:** actualiza `WHATSAPP_ALLOWED_RECIPIENTS` **en tu `.env` local**, como lista separada por comas. No la pegues en chats ni commits.
4. **Verificación de la lista efectiva:** el preflight muestra `recipients_allowlisted=<n>` (solo el número). Comprueba que coincide con los testers.
5. **Aislamiento:** envía un mensaje desde dos testers distintos. Debe haber dos conversaciones y dos leads, y cada respuesta debe ir al remitente correcto.
6. **Lanzador:** `pilot_start.ps1` **no escribe** `WHATSAPP_ALLOWED_RECIPIENTS`; toma la lista de tu `.env` o de la sesión. Si la sesión la define, prevalece sobre el `.env`. Verifica que no quede un valor antiguo en la sesión (`$env:WHATSAPP_ALLOWED_RECIPIENTS`).
7. **Presupuesto:** el presupuesto es compartido por todos los testers. Cada consulta cuesta; limita las pruebas.

## Escenario B: varias líneas empresariales (NO soportado)

**Estado: no implementado de forma segura. No afirmes compatibilidad multi-línea.**

Restricciones verificadas en el esquema y el código:
- `leads.wa_contact_id` es **único global**. Un mismo contacto no puede tener un lead distinto por línea.
- `conversations` tiene una sola conversación abierta por lead, sin separación por línea.
- `webhook_events` es único por `(provider, external_event_id)`, pero no existe un identificador de línea en el lead.
- El worker valida `expected_business_phone_id` y rechaza eventos de otra línea (`foreign_business_line`). Eso aísla el procesamiento, **no** la identidad de los clientes.
- `contact_leases` usa la clave `línea + wa_id`, pero los leads no.

Para soportarlo harían falta cambios de esquema (identidad de lead por línea), migraciones revisadas y pruebas de aislamiento. Eso queda fuera de este piloto y **no se debe hacer sin autorización y sin ensayo en base desechable**.

---

## Checklist mínimo antes de cada sesión

- [ ] `.env` local presente y sin versionar.
- [ ] Token de Meta vigente y rotado si hubo exposición.
- [ ] Preflight en `LISTO`.
- [ ] FastAPI en `127.0.0.1:18011` y ngrok apuntando a 18011.
- [ ] URL de ngrok actualizada en Meta.
- [ ] `pilot_workers.py list` sin workers antes de arrancar.
- [ ] Un solo worker tras `pilot_start.ps1 -Start`.

---

## 15. Entornos: desarrollo, piloto y producción

| Aspecto | Desarrollo (local) | Piloto (este runbook) | Producción (no existe) |
|---------|--------------------|-----------------------|------------------------|
| Base de datos | PostgreSQL local (`docker-compose.yml`) o desechable para pruebas (`docker-compose.test.yml`) | Supabase Cloud, con migraciones reconciliadas | Supabase con copia de seguridad, plan de cambio y ventana |
| `LACUSINE_ENVIRONMENT` / `--environment` | `local` o `test` | `pilot` | `production` (reservado; sin uso hasta autorizar) |
| Envío a WhatsApp | Desactivado (`WHATSAPP_DRY_RUN=true`, `META_SEND_ENABLED=false`) | Activo solo al número de prueba, con destinatarios verificados | Número empresarial, plantillas aprobadas por Meta |
| LLM | Sin llamadas reales | Anthropic real con presupuesto diario | Anthropic real con presupuesto mensual y tarifas verificadas |
| Exposición HTTP | `127.0.0.1` | ngrok (HTTPS temporal) | Dominio propio con TLS y balanceador |
| Procesos | API y worker a mano | Un solo worker (`pilot_start.ps1`) | Servicio gestionado con reinicio automático y un único worker |
| Panel | `npm run dev` o `frontend/dist` | `frontend/dist` servido en `/admin` | Igual, con secretos en el panel de la plataforma |
| Datos | Sintéticos | Datos reales de la cuenta de prueba | Datos reales; respaldos y retención verificados |

Regla general: nada de esta tabla pasa de desarrollo a piloto ni de piloto a producción sin el cambio autorizado correspondiente.

## 16. Arrancar y detener FastAPI, ngrok y el worker

- **FastAPI:** sección 4 (`uvicorn app.main:app --host 127.0.0.1 --port 18011`). Comprobar `GET /health` y `GET /ready`.
- **ngrok:** sección 5, apuntando a `18011`. Actualizar la URL en Meta si cambia (sección 7).
- **Worker:** sección 10. Solo `scripts\pilot_start.ps1 -Start` lo arranca. No lanzar `python -m app.worker.main` a mano en el piloto: el script comprueba que exista exactamente uno.
- **Parar:** sección 14. Primero el worker, luego FastAPI y ngrok. El worker termina de forma verificada.
- **Estado del worker:** `.\.venv\Scripts\python.exe scripts\pilot_workers.py list --repo .`

## 17. Levantar el dashboard

El panel React vive en `frontend/` y lo sirve FastAPI en `/admin` cuando hay base de datos y configuración de Supabase Auth (`SUPABASE_URL` y `SUPABASE_JWKS_URL`). Si falta algo, la ruta no existe.

Compilar y servir (piloto):

```powershell
cd frontend
npm ci
npm test            # 4 pruebas de formato
npm run build       # genera frontend\dist (ignorado por git)
cd ..
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 18011
# Abrir http://127.0.0.1:18011/admin
```

Desarrollo con recarga: `npm run dev` en `frontend/`. Vite reenvía `/api` a FastAPI. Si FastAPI no usa el puerto 18000, defina `LACUSINE_API_PORT=18011` antes de arrancar. `VITE_SUPABASE_URL` y `VITE_SUPABASE_PUBLISHABLE_KEY` van en `frontend/.env` (ignorado por git).

El inicio de sesión usa Supabase Auth. El rol sale de `app_metadata.role` (`operator` o `admin`), que solo un administrador de Supabase puede escribir.

## 18. Consultar mensajes, conversaciones y errores

Desde el panel, las páginas son **Conversaciones**, **Leads**, **Analítica** y **Configuración**. Son de solo lectura salvo las acciones de pausa, reanudación y asignación de asesor.

Desde la línea de comandos, con solo lectura:

```powershell
.\.venv\Scripts\python.exe -m app.admin.cli --environment pilot --operator "<nombre de quien revisa>" status
.\.venv\Scripts\python.exe -m app.admin.cli --environment pilot --operator "<nombre>" inspect --type webhook_event --id <uuid>
.\.venv\Scripts\python.exe -m app.admin.cli --environment pilot --operator "<nombre>" audit --type outbound_message --id <uuid>
```

- `status` da conteos por estado. `inspect` muestra metadatos. `audit` muestra las intervenciones previas.
- Errores de Meta: consulte la sección 9 (códigos 190/463 y 131030). Los logs del worker van a `logs\worker-<fecha>.err.log`.

Resolver casos inciertos (`resolve-event`, `resolve-outbound`, `resolve-reservation`, `resolve-handoff`, `resume-conversation`) exige `--id`, `--confirm` con el mismo identificador y `--reason`. Según el caso añade `--to`, `--evidence` o `--actual-usd`. Cada resolución queda en auditoría.

## 19. Generar los cinco informes

Los informes son PDF de una **semana comercial**, de lunes 00:00 a lunes siguiente 00:00 (hora de Bogotá). La fecha indicada cae en la semana que la contiene. Tipos: `operativo`, `marketing`, `oportunidades`, `ventas` y `ejecutivo`.

- **Desde el panel:** página **Inteligencia comercial**. Elegir el tipo y la fecha y pulsar **Generar PDF**. El tipo ejecutivo admite narrativa con IA solo si `ANALYSIS_AI_ENABLED` está activo y la migración 17 está aplicada.
- **Desde la API** (rol `operator`):

```
GET /api/admin/commercial/report.pdf?kind=operativo&day=2026-11-02
GET /api/admin/commercial/report.csv?day=2026-11-02      (informe semanal en CSV)
```

Respuesta 413 si el PDF es demasiado grande, 503 si el almacenamiento no responde y 500 `pdf_generation_failed` sin detalle interno.

## 20. Confirmar y cancelar una venta

Solo el rol `admin` puede confirmar o cancelar. Desde **Inteligencia comercial → Confirmar venta** en el panel, o por API:

```
POST /api/admin/commercial/sales
{
  "sale_ref": "VTA-0001",            (3 a 100 caracteres, único)
  "lead_id": "<uuid del lead>",
  "amount_cop": 85000,
  "sold_at": "2026-11-06T15:00:00-05:00",
  "source": "admin_manual",           (o "external_order_system")
  "evidence_ref": "<referencia verificable: factura, pedido o wamid>",
  "products": { "<uuid de producto>": 2 }
}

POST /api/admin/commercial/sales/{sale_ref}/cancel
{ "reason": "motivo de al menos 5 caracteres" }
```

Una venta confirmada cuenta en los informes de **ventas** y **ejecutivo**. Cancelar no borra la venta: el cambio queda como evento en el historial de estados (solo-anexar). Códigos: 409 si la venta ya existe o no admite ese cambio, 422 si falta un dato válido, 403 si el rol no es `admin`.

## 21. Verificar permisos

- Sección 9: permisos de Meta, token permanente y números de prueba.
- Rol de base de datos: la aplicación debe conectarse con `lacusine_backend` y nunca con el propietario del esquema. Las consultas de comprobación de privilegios están en `docs/auditoria-migraciones-7-5.md`, sección 6.6.
- Rol del panel: `operator` ve informes y conversaciones; `admin` además confirma y cancela ventas y resuelve casos.
- Ninguna comprobación de permisos envía mensajes ni consume presupuesto de IA.

## 22. Línea pública empresarial (cuando exista)

No está activa. El procedimiento, incluidos los requisitos de Meta, la verificación del negocio, las plantillas y el cambio de esquema para varias líneas, está en [whatsapp-publico-activacion.md](whatsapp-publico-activacion.md). El piloto acepta una sola línea (escenario B de este runbook).

## 23. Desplegar el backend de forma permanente

Estado: **no desplegado**. Ninguna opción se ejecuta sin autorización, copia de seguridad verificada y el plan de cambio de [auditoria-migraciones-7-5.md](auditoria-migraciones-7-5.md), sección 6.

- **Render:** `render.yaml` y [render-free-demo.md](render-free-demo.md) son solo para la demostración con datos sintéticos (`DEMO_ENABLED`). No son el backend de producción.
- **AWS:** la arquitectura prevista está en [aws-architecture.md](aws-architecture.md). No hay recursos creados.
- Antes de cualquier despliegue: un solo worker, `META_SEND_ENABLED` decidido por el responsable, migraciones 1–17 registradas, secretos en el gestor de la plataforma (nunca en el repositorio) y la lista de [go-live-checklist.md](go-live-checklist.md) completa.
