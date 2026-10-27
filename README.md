# agente-LaCusine

Agente comercial de WhatsApp para La Cuisine Appliances Colombia (WhatsApp Cloud API + Claude API + FastAPI + Supabase).

> **Estado: Fase 1 completada, Fase 2 implementada en el repositorio.** El webhook valida firmas y persiste eventos de forma idempotente en PostgreSQL. No hay conexión a Meta, Claude ni a un proyecto Supabase real, y no se envían mensajes.

Diagrama de arquitectura: [`docs/arquitectura.html`](docs/arquitectura.html) · Modelo de datos: [`docs/modelo-datos.md`](docs/modelo-datos.md)

## Estado por fase

| Fase | Contenido | Estado |
|---|---|---|
| 1 | Webhook con handshake, firma `X-Hub-Signature-256`, límites de tamaño y falla cerrada | Completada |
| 2 | Migraciones Supabase, RLS, repositorios, deduplicación persistente, importador de catálogo | Implementada y probada en PostgreSQL local. **Sin aplicar al proyecto Supabase real.** |
| 3 | Adaptador Claude API, prompts con catálogo aprobado, presupuesto de tokens | Pendiente |
| 4 | Integración real con Meta (OAuth, número autorizado, webhook público) | Pendiente. **Requiere autorización explícita.** |
| 5 | Worker y cola persistente, reintentos, escalamiento humano, observabilidad | Pendiente |

## Comportamiento del webhook

- **GET** `/webhook`: handshake con `hub.verify_token`. Sin token configurado responde 403.
- **POST** `/webhook`, en este orden:
  1. Sin `META_APP_SECRET` o sin base de datos configurada → 503 (falla cerrada).
  2. Cuerpo mayor que `MAX_WEBHOOK_BODY_BYTES` → 413.
  3. Firma `X-Hub-Signature-256` inválida o ausente → 403.
  4. JSON inválido → 400; estructura inválida, `from` mal formado o más de `max_messages_per_request` mensajes → 400.
  5. Por cada mensaje, `INSERT … ON CONFLICT DO NOTHING` en `webhook_events`.
  6. Respuesta 200 `{accepted, duplicates, conflicts}` **solo si todo quedó guardado**. Un fallo de base → 503 y Meta reintenta.
- Los estados de entrega (`statuses`) se reconocen pero no se guardan.
- El POST no llama a Claude ni ejecuta trabajo costoso.

## Estructura

```
app/
  main.py                    # create_app(): inyecta repositorios; pool con ciclo de vida
  api/webhook.py             # GET (verificación) y POST (recepción)
  core/config.py             # configuración desde variables de entorno
  core/logging_utils.py      # logging seguro (sin cuerpos ni números completos)
  db/pool.py                 # pool psycopg (sin sentencias preparadas, compatible con pooler)
  models/webhook.py          # validación y extracción de mensajes
  repositories/
    errors.py                # StorageError: sin causa original ni datos
    webhook_events.py        # deduplicación persistente (interfaz + PostgreSQL)
    catalog.py               # UPSERT por referencia, solo cambios reales
    crm.py                   # leads, conversaciones, mensajes, solicitudes de asesor
  security/signature.py      # HMAC-SHA256 en tiempo constante
  services/catalog_import.py # importador CSV: todo o nada
supabase/migrations/         # esquema, RLS y retención (versionadas)
docs/arquitectura.html       # diagrama de arquitectura
docs/modelo-datos.md         # ER, decisiones, retención, costos
tests/                       # unitarias (dobles en memoria)
tests/integration/           # PostgreSQL real, solo con TEST_DATABASE_URL
```

## Configuración

Las credenciales se leen solo de variables de entorno. Copie `.env.example` a `.env` y complete los valores. Nunca versione `.env`.

| Variable | Uso |
|---|---|
| `META_APP_SECRET` | Valida la firma de los POST. Sin él, el webhook responde 503. |
| `META_VERIFY_TOKEN` | Token del handshake GET. |
| `DATABASE_URL` | Conexión del backend a PostgreSQL. Sin ella, el webhook responde 503. |
| `MAX_WEBHOOK_BODY_BYTES` | Límite de tamaño del cuerpo (por defecto 262144). |
| `WEBHOOK_EVENT_RETENTION_DAYS` | Días que se conservan los eventos (por defecto 7). |
| `TEST_DATABASE_URL` | Solo pruebas de integración. Base **local** cuyo nombre contenga `test`. |

## Entorno Python

Todo el trabajo se hace en `.venv` del repositorio. No instale paquetes en el Python global.

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pip check
```

Versiones fijadas: `fastapi 0.141.1`, `uvicorn 0.52.3`, `psycopg 3.3.6`, `psycopg-pool 3.3.3`, `pytest 8.4.2`, `httpx 0.28.1`.

## Pruebas

**Unitarias** (sin base de datos, dobles en memoria): 62 pruebas.

```powershell
.venv\Scripts\python.exe -m pytest -q tests --ignore=tests/integration
```

**Integración** (PostgreSQL real, base desechable local): 24 pruebas.

Levantar un PostgreSQL de pruebas en Docker (la contraseña no debe quedar en el repositorio):

```powershell
docker run -d --name lacusine-test-pg -e POSTGRES_PASSWORD=<contraseña-local> -e POSTGRES_DB=lacusine_test -p 127.0.0.1:55439:5432 postgres:16-alpine
$env:TEST_DATABASE_URL = "postgresql://postgres:<contraseña-local>@127.0.0.1:55439/lacusine_test"
.venv\Scripts\python.exe -m pytest -q tests/integration
```

Las pruebas de integración **reinician el esquema público** de la base indicada. El guard rechaza cualquier base cuyo nombre no contenga `test`. Nunca apunte `TEST_DATABASE_URL` a producción.

Sin `TEST_DATABASE_URL`, las pruebas de integración se omiten con el motivo indicado.

**Todas:** `python -m pytest -q` → 86 pasan con la base de pruebas; 62 pasan y 1 se omite sin ella.

Se verificó que las migraciones se aplican sin error en PostgreSQL 16, cada una en una transacción, y que RLS queda activa en las seis tablas.

## Pendiente (no implementado, sin autorización)

- **Aplicar migraciones a Supabase real**: requiere autorización explícita y un plan de cambio.
- **Rol de base de datos del backend** con privilegios mínimos (fuera del repositorio).
- **Worker y cola** (Fase 5), que consumirá `webhook_events` pendientes.
- **Catálogo real**: el importador valida el contrato, pero no hay datos de LCA en el repositorio.
- **Precios de Meta, Anthropic y Supabase**: deben verificarse en sus páginas oficiales antes de presupuestar. No se han inventado tarifas.
- **Confirmación de alcance**: el MCP de Meta es una herramienta de configuración, no el motor de producción; antes de la Fase 4, confirmar sus límites.

## Notas

- La deduplicación en memoria de la Fase 1 fue **provisional** y se retiró del camino de producción. Hoy la garantiza PostgreSQL.
- `httpx` es la dependencia que pide el cliente de pruebas de Starlette; ese módulo emite un aviso de deprecación de la librería, no del proyecto.
