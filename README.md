# agente-LaCusine

Agente comercial de WhatsApp para La Cuisine Appliances Colombia (WhatsApp Cloud API + Claude API + FastAPI + Supabase).

> **Estado: Fase 1 (local).** Webhook con handshake, validación de firma y deduplicación en memoria. No hay conexión a Meta, Claude ni Supabase, y no se envían mensajes reales.

Diagrama de arquitectura: [`docs/arquitectura.html`](docs/arquitectura.html)

## Validación de la arquitectura (Fase 1)

Revisión de `docs/arquitectura.html` contra la implementación:

| Punto | Estado |
|---|---|
| Webhook: handshake GET y firma `X-Hub-Signature-256` | Implementado y probado |
| Deduplicación ("firma + idempotencia") | Implementada en memoria; la Fase 2 la mueve a Supabase |
| "Responde pronto y delega trabajo pesado" | El POST solo valida y deduplica; la cola (Fase 5) queda marcada en `app/api/webhook.py` |
| Orquestador, Claude API, base de datos, asesor humano | Pendientes (Fases 2 a 5) |
| Conocimiento LCA "349 productos, precios no en tiempo real" | Pendiente; requiere el catálogo aprobado por LCA |
| MCP de Meta solo para configurar y probar | Sin cambios; no forma parte del runtime |

Observación: el diagrama indica que el MCP de Meta es beta. Antes de la Fase 4 hay que confirmar con el proveedor el alcance del MCP y los límites de la ventana de 24 horas y de las plantillas.

## Estructura

```
app/
  main.py            # create_app(): ensambla settings, almacén de eventos y rutas
  api/webhook.py     # GET (verificación) y POST (recepción) del webhook
  core/config.py     # configuración desde variables de entorno
  core/logging_utils.py  # logging seguro (sin cuerpos ni números completos)
  security/signature.py  # HMAC-SHA256 en tiempo constante
  services/idempotency.py # EventStore (interfaz) + implementación en memoria
  models/webhook.py  # extracción mínima de mensajes entrantes
tests/test_webhook.py
docs/arquitectura.html
```

## Configuración

Las credenciales se leen solo de variables de entorno. Copie `.env.example` y complete los valores; nunca versione `.env`.

| Variable | Uso |
|---|---|
| `META_APP_SECRET` | Valida la firma de los POST. Sin él, el webhook responde 503 (falla cerrado). |
| `META_VERIFY_TOKEN` | Token del handshake GET. Sin él, la verificación responde 403. |
| `MAX_WEBHOOK_BODY_BYTES` | Límite de tamaño del cuerpo (por defecto 262144). |

## Ejecución local

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
```

Pruebas:

```powershell
python -m pytest -q
```

Servidor local (sin números conectados; útil para probar con payloads simulados):

```powershell
$env:META_APP_SECRET = "valor-de-prueba"
$env:META_VERIFY_TOKEN = "valor-de-prueba"
python -m uvicorn app.main:app --reload
```

## Pruebas incluidas

- Handshake: token correcto, token incorrecto, modo incorrecto y configuración ausente.
- Firma: válida, ausente, incorrecta, de otro cuerpo, con formato inválido y sin secreto configurado.
- Idempotencia: el mismo `wamid` procesado dos veces se cuenta una sola vez.
- Payload: solo de estados (sin mensajes), JSON inválido y cuerpo sobre el límite.

## Pendiente (no implementado, sin autorización)

- Fase 2: esquema SQL de Supabase con RLS, tablas de conversaciones, leads y eventos.
- Fase 3: adaptador Claude API, presupuesto de tokens y prompts con catálogo aprobado.
- Fase 4: integración real con Meta (OAuth, número autorizado, webhook público). **Requiere autorización explícita antes de conectar cualquier número.**
- Fase 5: cola de trabajos, reintentos, escalamiento humano y observabilidad.

## Notas

- `uvicorn` está fijado en 0.52.3 para coincidir con otro proyecto del mismo entorno Python. Para aislar dependencias use `.venv` como se indica arriba.
