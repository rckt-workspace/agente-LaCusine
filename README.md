# agente-LaCusine

Agente comercial de WhatsApp para La Cuisine Appliances Colombia (WhatsApp Cloud API + Claude API + FastAPI + Supabase).

> **Estado: Fases 1, 2 y 3 implementadas en el repositorio.** El webhook persiste eventos de forma idempotente, el motor comercial de IA está completo y probado con un proveedor simulado, y el catálogo se valida localmente. **No hay conexión a WhatsApp, no se llama a Claude, no se aplican migraciones en Supabase real y no hay envíos.**

Documentación: [`docs/arquitectura.html`](docs/arquitectura.html) · [`docs/modelo-datos.md`](docs/modelo-datos.md) · [`docs/agente-ia.md`](docs/agente-ia.md)

## Estado por fase

| Fase | Contenido | Estado |
|---|---|---|
| 1 | Webhook con handshake, firma `X-Hub-Signature-256`, límites de tamaño y falla cerrada | Completada |
| 2 | Migraciones Supabase, RLS, repositorios, deduplicación persistente, importador CSV | Implementada y probada en PostgreSQL local. **Sin aplicar a Supabase real.** |
| 3 | Motor comercial de IA, búsqueda de productos, adaptador Anthropic, control de costos, importador XLSX | Implementada y probada con proveedor simulado. **Sin llamadas reales.** |
| 4 | Integración real con Meta (OAuth, número autorizado, envíos) | Pendiente. **Requiere autorización explícita.** |
| 5 | Worker, cola persistente, reintentos, notificación a asesores, auditoría diferida | Pendiente |

## Motor de IA (Fase 3)

Flujo, controles y decisiones: [`docs/agente-ia.md`](docs/agente-ia.md). En resumen:

- **Orquestador** (`app/agent/orchestrator.py`): el backend controla el flujo. Claude propone texto y estructura; el backend valida antes de aceptar. Ningún paso que pueda evitar una llamada facturable se salta.
- **Seguridad**: datos sensibles (tarjetas con Luhn, contraseñas, cédulas) e instrucciones maliciosas no llegan al modelo. Las respuestas que prometen descuentos, stock garantizado, fechas exactas, pagos confirmados o asesoría jurídica definitiva se rechazan y escalan.
- **Productos**: búsqueda parametrizada con máximo 5 candidatos. El modelo solo puede recomendar IDs de esos candidatos.
- **Costos**: estimación con tarifas configuradas, presupuestos diario, mensual y por interacción, y registro idempotente. Sin tarifas no hay llamadas reales.
- **Modelo**: `claude-haiku-5-5` es el candidato verificado el 28/10/2026 en la documentación oficial. `ANTHROPIC_MODEL` queda vacío hasta que el equipo lo elija.
- **Modo simulado por defecto**: `AGENT_DRY_RUN=true` y `ANTHROPIC_ENABLED=false`. En dry-run siempre se usa el proveedor simulado, aunque Anthropic esté habilitado.

### Catálogo

El archivo `Catalogo_La_Cuisine_Agente_WhatsApp.xlsx` **no está en el repositorio**. Se valida localmente con el importador (dry-run por defecto):

```powershell
.venv\Scripts\python.exe -m app.services.catalog_xlsx --file "RUTA\Catalogo_La_Cuisine_Agente_WhatsApp.xlsx" --extracted-on 2026-10-26
```

Resultado de la validación del 28/10/2026: 349 filas válidas, 0 inválidas, 0 duplicadas. El importador escribe en la base solo con `--apply` y `DATABASE_URL`, y solo si no hay errores (o con `--allow-partial`).

## Comportamiento del webhook

- **GET** `/webhook`: handshake con `hub.verify_token`. Sin token configurado responde 403.
- **POST** `/webhook`, en este orden:
  1. Sin `META_APP_SECRET` o sin base de datos → 503 (falla cerrada).
  2. Cuerpo mayor que `MAX_WEBHOOK_BODY_BYTES` → 413.
  3. Firma `X-Hub-Signature-256` inválida o ausente → 403.
  4. JSON inválido → 400; estructura inválida, `from` mal formado o más de `max_messages_per_request` mensajes → 400.
  5. Por cada mensaje, `INSERT … ON CONFLICT DO NOTHING` en `webhook_events`.
  6. Respuesta 200 `{accepted, duplicates, conflicts}` **solo si todo quedó guardado**. Un fallo de base → 503 y Meta reintenta.
- El POST no llama a Claude ni ejecuta trabajo costoso. Una prueba verifica en un subproceso limpio que el webhook no importa el SDK de IA.

## Estructura

```
app/
  main.py                      # create_app(): inyecta repositorios; pool con ciclo de vida
  api/webhook.py               # GET (verificación) y POST (recepción)
  agent/
    orchestrator.py            # flujo comercial y controles (AgentOutcome)
    factory.py                 # única selección de proveedor
    schemas.py                 # AgentReply (Pydantic estricto) y LeadStage
    safety.py                  # entrada, salida y redacción para logs
    qualification.py           # señales observables y etapa comercial
    handoff.py                 # decisión de escalamiento
    conversation_context.py    # historial acotado por conversación
    knowledge.py               # prompt y políticas como datos
    prompts/commercial_system.md
    policies/lacusine_policies.json   # aprobado / pendiente de validación / escalamiento
  integrations/
    llm.py                     # LLMProvider, errores tipados, FakeLLMProvider
    anthropic_client.py        # SDK oficial; reintentos y concurrencia acotados
  services/
    product_search.py          # búsqueda con corrección de erratas
    cost_tracker.py            # costos, presupuestos y precheck
    catalog_xlsx.py            # importador XLSX (dry-run por defecto)
    catalog_import.py          # importador CSV (Fase 2)
    text.py                    # normalización para búsqueda
  repositories/                # PostgreSQL: webhook_events, catalog, products, crm, llm_usage
  core/config.py               # configuración desde variables de entorno
  core/logging_utils.py        # logging seguro
  db/pool.py                   # pool psycopg
  models/webhook.py            # validación del payload
  security/signature.py        # HMAC-SHA256 en tiempo constante
supabase/migrations/           # esquema, RLS, retención, catálogo, costos, handoff (versionadas)
docs/                          # arquitectura.html, modelo-datos.md, agente-ia.md
tests/                         # unitarias (dobles en memoria, red bloqueada)
tests/integration/             # PostgreSQL real, con guard de seguridad
```

## Configuración

Las credenciales se leen solo de variables de entorno. Copie `.env.example` a `.env` y complete los valores. Nunca versione `.env`.

Principales variables:

| Variable | Uso | Por defecto |
|---|---|---|
| `META_APP_SECRET`, `META_VERIFY_TOKEN` | Firma y handshake de Meta | vacías (falla cerrada) |
| `DATABASE_URL` | Conexión del backend | vacía (falla cerrada) |
| `AGENT_DRY_RUN` | Proveedor simulado | `true` |
| `ANTHROPIC_ENABLED` | Habilita el cliente real (solo si `AGENT_DRY_RUN=false`) | `false` |
| `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` | Credencial y modelo | vacías |
| `ANTHROPIC_PRICE_*_USD_PER_MTOK` | Tarifas para estimar costos | vacías (sin tarifas no hay llamadas) |
| `AGENT_KILL_SWITCH` | Apagado de emergencia | `false` |
| `AGENT_*_BUDGET_USD`, `AGENT_INTERACTION_LIMIT_USD` | Presupuestos | vacíos |
| `TEST_DATABASE_URL` | Solo pruebas de integración | vacía |

## Entorno Python

Todo el trabajo se hace en `.venv` del repositorio. No instale paquetes en el Python global.

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pip check
```

Dependencias de ejecución: `fastapi 0.141.1`, `uvicorn 0.52.3`, `psycopg 3.3.6`, `psycopg-pool 3.3.3`, `anthropic 2.3.0`, `openpyxl 3.1.5`. Desarrollo: `pytest 8.4.2`, `httpx 0.28.1`.

## Pruebas

**Unitarias** (sin base de datos y sin red; las pruebas del agente bloquean cualquier conexión fuera de loopback):

```powershell
.venv\Scripts\python.exe -m pytest -q tests --ignore=tests/integration
```

→ 271 pasan.

**Integración** (PostgreSQL real, base desechable local):

```powershell
docker run -d --name lacusine-test-pg -e POSTGRES_PASSWORD=<contraseña-local> -e POSTGRES_DB=lacusine_test -p 127.0.0.1:55439:5432 postgres:16-alpine
$env:APP_ENV = "test"
$env:LACUSINE_TEST_DB_RESET = "I_UNDERSTAND_THIS_DROPS_PUBLIC_SCHEMA"
$env:TEST_DATABASE_URL = "postgresql://postgres:<contraseña-local>@127.0.0.1:55439/lacusine_test"
.venv\Scripts\python.exe -m pytest -q tests/integration
```

→ 41 pasan.

### Guard de las pruebas destructivas

Las pruebas de integración reinician el esquema `public`. Por eso exigen **todas** estas condiciones, o fallan antes de conectarse:

- `APP_ENV=test`.
- `LACUSINE_TEST_DB_RESET=I_UNDERSTAND_THIS_DROPS_PUBLIC_SCHEMA`.
- Host de loopback exacto: `127.0.0.1`, `localhost` o `::1`.
- Nombre de base exactamente `lacusine_test`.
- Sin rastros de Supabase en host o usuario; esquema `postgresql://` o `postgres://`.
- Si `DATABASE_URL` está definida, debe apuntar al mismo destino de prueba. Otro destino indica producción en el entorno.

Si `TEST_DATABASE_URL` está definida pero es insegura, el módulo **falla**; no se omite. Las 29 pruebas del guard son unitarias y nunca se conectan a ninguna base.

**Todas:** `python -m pytest -q` → 312 pasan con la base de pruebas (271 unitarias + 41 de integración) y 1 se omite (prueba opcional del catálogo real). Sin base de datos: 271 pasan y 3 se omiten, cada una con su motivo.

**Opcional:** `LACUSINE_CATALOG_XLSX=<ruta>` valida el catálogo real sin imprimir productos.

## Pendiente (no implementado, sin autorización)

- **Aplicar migraciones a Supabase real**: requiere autorización y un plan de cambio.
- **Rol de base de datos del backend** con privilegios mínimos (fuera del repositorio).
- **Envío por WhatsApp** y conexión del agente al flujo real (Fase 4).
- **Worker y cola**, notificación a asesores y auditoría diferida (Fase 5).
- **Importar el catálogo a Supabase**: requiere la aprobación de LCA.
- **Validación legal** de las políticas marcadas como pendientes (garantías de accesorios, devoluciones en promoción, 25 % de gasto administrativo).
- **Precios de Meta, Anthropic y Supabase**: verificar antes de presupuestar. Las tarifas de Anthropic usadas como referencia se verificaron el 28/10/2026 y no están en el código.

## Notas

- `httpx` es la dependencia que pide el cliente de pruebas de Starlette; ese módulo emite un aviso de deprecación de la librería, no del proyecto.
- El Python global de esta máquina tiene conflictos previos con otros proyectos (`gotrue`, `supafunc`, `opencv`, entre otros). Este proyecto no los usa: todo se ejecuta desde `.venv`.
