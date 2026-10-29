# Variables de entorno — contrato

Fuente de referencia versionada: [`.env.example`](../.env.example). Este documento explica cada variable. **Ningún valor real debe escribirse aquí ni en el repositorio.**

## Dónde vive cada valor

| Entorno | Dónde se configuran los valores reales |
|---|---|
| Desarrollo local (Windows/PowerShell) | Archivo `.env` local, ignorado por Git. Se carga solo con `LACUSINE_LOAD_DOTENV=true`. |
| Render (web y worker) | Panel de Render, Environment o Environment Group. Los secretos se marcan con `sync: false` en `render.yaml` y se introducen a mano. |
| Pruebas automatizadas | Variables explícitas del proceso de prueba. Nunca el `.env` real. |

`.env` **no** se sube a GitHub, **no** se copia a la imagen de contenedor (`.dockerignore`) y **no** se guarda en Supabase como sustituto de la configuración.

El cargador de `.env` nunca sobrescribe una variable ya definida, nunca imprime valores y no activa ninguna capacidad: solo devuelve los nombres cargados.

## Valores seguros por defecto

Si una variable no está definida, el sistema se comporta así:

| Variable | Valor por defecto | Efecto |
|---|---|---|
| `META_SEND_ENABLED` | `false` | No se envía ningún mensaje a Meta. |
| `WHATSAPP_DRY_RUN` | `true` | Bloquea cualquier solicitud HTTP al adaptador de WhatsApp. |
| `WHATSAPP_ALLOWED_RECIPIENTS` | vacío | Con envío activo, la configuración falla cerrada. |
| `ANTHROPIC_ENABLED` | `false` | No se construye el cliente de Claude. |
| `AGENT_DRY_RUN` | `true` | El agente usa un proveedor simulado sin red ni costo. |
| `ANTHROPIC_MODEL` | vacío | Sin modelo, Claude no puede llamarse aunque esté habilitado. |
| `ANTHROPIC_*_USD_PER_MTOK` | vacío | Sin tarifas, cualquier llamada facturable queda bloqueada. |
| `WORKER_ENABLED` | `false` | El worker sale sin consumir la cola. |
| `AGENT_KILL_SWITCH` | `false` | Si se pone en `true`, el agente se desactiva de inmediato. |

## Categorías

### Meta y webhook

| Variable | Obligatoria cuando | Secreta | Descripción |
|---|---|---|---|
| `META_APP_SECRET` | Siempre que se reciba el webhook | Sí | Valida la firma `X-Hub-Signature-256`. Sin ella el webhook responde 503. |
| `META_VERIFY_TOKEN` | Para el handshake GET | Sí | Token que se registra en Meta para la verificación. |
| `META_GRAPH_API_VERSION` | Con envío activo | No | Versión de Graph API. Valor de referencia verificado el 29/10/2026: `v26.0`. Se valida contra la documentación antes de cada cambio. |

### WhatsApp (salida)

| Variable | Obligatoria cuando | Secreta | Descripción |
|---|---|---|---|
| `META_ACCESS_TOKEN` | Con envío activo | Sí | Token de acceso del backend. Solo viaja en la cabecera `Authorization`. |
| `META_PHONE_NUMBER_ID` | Con envío activo | Sí (en Render) | Identificador del número comercial de origen. Debe coincidir con el de las salidas. |
| `META_SEND_ENABLED` | Para permitir envíos | No | Interruptor principal. Por defecto `false`. |
| `WHATSAPP_DRY_RUN` | Para permitir envíos | No | Segundo interruptor. Ambos deben estar en el estado de envío para que salga una solicitud. |
| `WHATSAPP_ALLOWED_RECIPIENTS` | Con envío activo | Sí (en Render) | Lista de destinatarios autorizados, separados por coma, sin `+`. Vacía = no se envía nada. |
| `WHATSAPP_TIMEOUT_SECONDS` | No | No | Tiempo máximo por solicitud. Por defecto 10. |
| `WHATSAPP_MAX_CONCURRENCY` | No | No | Solicitudes simultáneas a Meta. Por defecto 2. |

### Base de datos

| Variable | Obligatoria | Secreta | Descripción |
|---|---|---|---|
| `DATABASE_URL` | Sí (API y worker) | Sí | Cadena de conexión PostgreSQL. Ver [supabase-deployment.md](supabase-deployment.md) para elegir el modo. No registrar en logs. |
| `MAX_WEBHOOK_BODY_BYTES` | No | No | Límite del cuerpo del webhook. Por defecto 262144. |
| `WEBHOOK_EVENT_RETENTION_DAYS` | No | No | Días de conservación de eventos y contenidos. Por defecto 7. Revisar con el responsable legal. |

### Claude (IA)

| Variable | Obligatoria cuando | Secreta | Descripción |
|---|---|---|---|
| `ANTHROPIC_ENABLED` | Para permitir llamadas | No | Interruptor. Por defecto `false`. |
| `AGENT_DRY_RUN` | Para permitir llamadas | No | Por defecto `true`. |
| `ANTHROPIC_API_KEY` | Con llamadas activas | Sí | Clave del backend. Oculta en `repr`. |
| `ANTHROPIC_MODEL` | Con llamadas activas | No | Modelo elegido por el equipo. Candidato verificado el 28/10/2026: `claude-haiku-5-5`. |
| `ANTHROPIC_MAX_OUTPUT_TOKENS`, `ANTHROPIC_TIMEOUT_SECONDS`, `ANTHROPIC_MAX_RETRIES`, `ANTHROPIC_MAX_CONCURRENCY` | No | No | Límites de la llamada. |
| `ANTHROPIC_PRICING_REFERENCE` | No | No | Texto que identifica la fuente de las tarifas en los registros. |
| `ANTHROPIC_PRICE_*_USD_PER_MTOK` | Con llamadas activas | No | Tarifas por millón de tokens. Revisar antes de cada presupuesto. |
| `AGENT_DAILY_BUDGET_USD`, `AGENT_MONTHLY_BUDGET_USD`, `AGENT_INTERACTION_LIMIT_USD` | Recomendadas | No | Presupuestos estimados. |
| `AGENT_KILL_SWITCH` | No | No | Apagado de emergencia. |
| `AGENT_MAX_INPUT_CHARS`, `AGENT_MAX_CONTEXT_CHARS` | No | No | Límites de entrada y de contexto. |
| `ANTHROPIC_PROMPT_CACHING` | No | No | Desactivado. Ver [agente-ia.md](agente-ia.md). |

### Worker

| Variable | Secreta | Descripción |
|---|---|---|
| `WORKER_ENABLED` | No | `true` para consumir la cola. |
| `WORKER_ID` | No | Identificador del proceso en los leases. Vacío = host y PID. |
| `WORKER_POLL_INTERVAL_SECONDS` | No | Espera entre ciclos vacíos. Por defecto 5. |
| `WORKER_BATCH_SIZE` | No | Mensajes por lote. Por defecto 10. |
| `WORKER_MAX_CONCURRENCY` | No | Trabajos simultáneos. Por defecto 4. |
| `WORKER_LEASE_SECONDS` | No | Duración del lease. Debe superar `WORKER_PROCESSING_TIMEOUT_SECONDS`. |
| `WORKER_PROCESSING_TIMEOUT_SECONDS` | No | Límite de procesamiento. Por defecto 60. |
| `WORKER_MAX_ATTEMPTS` | No | Intentos antes de revisión manual. Por defecto 5. |
| `WORKER_BACKOFF_BASE_SECONDS`, `WORKER_BACKOFF_MAX_SECONDS` | No | Backoff exponencial con jitter. |

### Entorno local y pruebas

| Variable | Descripción |
|---|---|
| `LACUSINE_LOAD_DOTENV` | `true` carga `.env` en desarrollo local. Nunca en Render. |
| `APP_ENV` | Debe valer `test` para las pruebas destructivas. |
| `TEST_DATABASE_URL` | Base local de loopback cuyo nombre sea `lacusine_test`. |
| `LACUSINE_TEST_PG_PASSWORD` | Contraseña del contenedor de pruebas. Solo local. |

## Validación al arrancar

- Un valor numérico inválido impide el arranque con un mensaje que nombra solo la variable.
- Con envío activo y sin `META_ACCESS_TOKEN`, `META_PHONE_NUMBER_ID`, `META_GRAPH_API_VERSION` o destinatarios autorizados, el worker falla al construir el adaptador.
- Un `META_PHONE_NUMBER_ID` con formato inválido también falla.
- Ninguna variable de Meta es obligatoria para las pruebas locales simuladas.

## Qué no debe pasar

- Un secreto en el repositorio, en un `render.yaml` con valor literal, en un log o en una respuesta HTTP.
- Un `.env` real en un commit, una imagen o un PR.
- La URL de PostgreSQL con contraseña impresa en un log. El código solo registra nombres de variables y códigos de error.
