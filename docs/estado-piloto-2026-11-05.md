# Estado del piloto de La Cuisine — 5 de noviembre de 2026

Documento de cierre del ciclo de piloto local. Describe qué está **verificado**, qué son **hipótesis** y qué queda **pendiente**. No contiene credenciales, números de clientes ni contenido de conversaciones.

## 1. Arquitectura real

```
Cliente WhatsApp ──► Meta WhatsApp Cloud API ──► ngrok (HTTPS) ──► FastAPI 127.0.0.1:18011 /webhook
                                                                        │ valida firma, persiste evento (idempotente)
                                                                        ▼
                                                              Supabase PostgreSQL (pooler)
                                                                        ▲
Worker Python (un solo proceso, lease por contacto) ──► Anthropic Messages API (Claude Sonnet 5.5)
        │ genera respuesta validada (Pydantic), reserva presupuesto, guarda en outbox
        ▼
Dispatcher ──► Meta Graph API /messages (envío de texto e indicador "escribiendo...")
        ▲
Estados de entrega (sent/delivered/read/failed) llegan por el mismo webhook y se aplican con transiciones permitidas
```

Componentes en el repositorio:

- `app/api/webhook.py`: verificación de firma, handshake y persistencia de eventos.
- `app/worker/`: proceso independiente (`python -m app.worker.main`), `processor.py`, `dispatcher.py`, `presence.py` (indicador), `profile.py` (perfil de arranque sin secretos).
- `app/agent/`: orquestador comercial, validación de salida, handoff, calificación, prompt en `prompts/commercial_system.md`.
- `app/services/product_search.py`: búsqueda y ranking comercial. `app/repositories/products.py`: consultas SQL parametrizadas.
- `app/integrations/`: cliente de Anthropic, cliente de WhatsApp Cloud API y adaptador de OpenRouter (no usado en el piloto).
- `app/admin/` y `app/admin_api/`, `frontend/`: panel administrativo (React/Vite) con autenticación de Supabase Auth. **No está desplegado.**
- `scripts/pilot_start.ps1`, `scripts/pilot_workers.py`, `scripts/pilot_preflight.py`: arranque controlado y comprobaciones del piloto.

## 2. Stack tecnológico

- Python 3.11 en `.venv` (Windows), FastAPI, psycopg 3 y psycopg-pool, Pydantic v2, httpx, SDK oficial de Anthropic (2.3.0 instalado).
- PostgreSQL (Supabase Cloud para el piloto; contenedor `postgres:16-alpine` solo para pruebas de integración).
- Node/Vite para el frontend administrativo (no se usa en el piloto).
- ngrok para exponer `127.0.0.1:18011`.

## 3. Supabase

- Base de datos del piloto: Supabase Cloud, acceso con el rol de aplicación `lacusine_app`. Las migraciones están en `supabase/migrations/`. **No se ejecutaron migraciones nuevas durante esta fase.**
- Tablas relevantes verificadas en solo lectura: `webhook_events`, `outbound_messages`, `llm_usage_events`, `budget_reservations`, `human_handoffs`, `conversations`, `leads`, `products`.
- Restricción verificada: `leads.wa_contact_id` es **único global** (no hay columna de línea empresarial). Implica que el piloto no admite varias líneas empresariales; ver escenario B en `docs/piloto-whatsapp-ngrok-runbook.md`.

## 4. Catálogo

- **349 productos** importados el **5 de noviembre de 2026** (`created_at` del lote).
- 30 marcas, 19 categorías. Todos tienen enlace de producto (`product_url`).
- Categorías de máquinas: «Cafeteras automáticas» (8 productos, 2 marcas). Categoría de accesorios: «Accesorios cafeteras» (15, Jura). Entre las máquinas: Jura C3, E6, E8, C9 y ENA8.
- El importador y el origen de los datos están documentados en `docs/catalogo-operacion.md`. Los datos de precio son de referencia; el agente no confirma stock, flete ni fechas de entrega.

## 5. Integración Meta / WhatsApp

Verificado en el piloto:

- Recepción de mensajes entrantes por el webhook con HTTP 200, persistencia idempotente y procesamiento por el worker.
- Envío de texto con HTTP 200 de Meta y estados `sent`, `delivered` y `read` aplicados desde los eventos de Meta (`reply_status_from_meta`).
- Indicador "escribiendo..." con el payload oficial (`status: read`, `message_id` del mensaje entrante, `typing_indicator: {type: text}`). Se verificó su forma contra la documentación oficial.
- Número de negocio en modo `LIVE`, estado `CONNECTED`, calidad `GREEN`, límite `TIER_250`, verificado (lectura de solo lectura).

Problemas observados y su causa:

- **Token de acceso caducado** (`OAuthException` código 190, subcódigo 463): los envíos fallaron con 401. Se renovó el token; el preflight lo comprueba antes de arrancar.
- **Errores 400 de envío e indicador tras la renovación:** no se conoce el código de Meta porque el cuerpo de error no se registraba. Desde esta fase se registra `error.code`, `error.type`, `error_subcode`, `fbtrace_id` y un detalle redactado. **Causa pendiente de confirmar** con la primera prueba controlada.

Límites documentados por Meta y relevantes para el piloto (verificar en el panel de Meta):
- `131047`: más de 24 horas desde la última respuesta del cliente. Requiere plantilla.
- `131030`: destinatario no permitido para el número de prueba.
- `130429`, `131056`, `80007`: transitorios; se reintentan con backoff.
- `131009`: valor de parámetro inválido.

## 6. Proveedor Anthropic

- Modelo: `claude-sonnet-5-5`. Tarifas oficiales (USD por millón de tokens): entrada 2, salida 10, escritura de caché 2,50, lectura de caché 0,10. **Las tarifas están ligadas al modelo** (`ANTHROPIC_PRICING_MODEL`); si no coinciden, el proveedor no se usa.
- Pensamiento: `ANTHROPIC_THINKING=between_tools` (soportado en Sonnet 5.5 según la guía de migración; sin herramientas, la respuesta es solo texto). `ANTHROPIC_EFFORT=low`.
- Salida máxima: 700 tokens (`ANTHROPIC_MAX_OUTPUT_TOKENS`).
- Caché de prompt: **desactivado**. No verificado para este prompt.
- Llamadas reales verificadas: respuestas válidas con 986 tokens de salida (con límite de 1.100) y una respuesta truncada a 1.100 que motivó la fase de optimización.
- Hipótesis: el pensamiento adaptativo por defecto (cuando no se envía el campo `thinking`) explica gran parte de los tokens de salida observados. Se confirmará con la métrica `blocks=` de la próxima prueba autorizada.

## 7. Búsqueda y recomendaciones comerciales

Causa corregida: la búsqueda limitaba a 5 candidatos antes de ordenar, y el desempate por precio ascendente colocaba accesorios baratos antes que las máquinas. Corrección:

- Recupera hasta 40 candidatos y envía al modelo solo 5 ya ordenados.
- Detecta si el cliente pide máquina o accesorio, y si pide algo económico.
- Clasifica por categoría (`Accesorios…`, `repuesto`).
- Orden: referencia exacta, tipo alineado con la intención, marca, términos coincidentes, disponibilidad. El precio solo desempata en consultas económicas.

Resultados verificados con datos reales en solo lectura:
- «quiero una cafetera Jura» → C3, C9, E6, E8 y ENA8 (antes: cinco accesorios).
- «necesito filtros Jura» → el cartucho de filtro primero.
- Seguimiento real para espresso y cappuccino → recomendación de Jura E6.

**Limitación conocida:** en el seguimiento, la respuesta no comparó de nuevo las dos opciones anteriores. Queda como mejora pendiente.

El modelo solo puede recomendar referencias presentes en los candidatos (las demás se descartan). Los enlaces se validan contra una lista de dominios permitidos. Los precios que aparecen en el texto son los del catálogo, pero **el backend no verifica cada cifra del texto**: es un riesgo pendiente.

## 8. Pruebas y resultados

- Última regresión reportada: **828 pruebas unitarias aprobadas, 1 omitida**; **130 de integración aprobadas** sobre Postgres local desechable (`docker` en `127.0.0.1:55439`, eliminado al terminar).
- `git diff --check`: limpio.
- Ninguna prueba realizó llamadas facturables a Anthropic, envíos a Meta ni escrituras en Supabase.
- Cobertura nueva relevante: observabilidad de errores de Meta (200/400/401 y códigos 100, 131009, 131030, 131047), tarifas ligadas al modelo, reserva conservadora, bloqueos de presupuesto, truncamiento recuperable, recomendaciones de cafeteras Jura, preflight del piloto.

## 9. Problemas encontrados y soluciones

| Problema | Causa demostrada | Solución |
|---|---|---|
| Respuestas de simulador enviadas a clientes | `AGENT_DRY_RUN=true` en el `.env` y worker sin variables de la shell | Arranque que valida el perfil; un worker con simulador no envía |
| Transferencias injustificadas por saludos | El modelo marcaba `requires_human` con motivo `unsupported_request` y el backend lo aceptaba | El backend solo acepta motivos justificados del modelo |
| Transferencia por presupuesto, proveedor o salida inválida | Errores recuperables abrían transferencia permanente | Mensaje neutro sin pausa |
| Búsqueda devolvía accesorios | Límite de 5 antes de ordenar y precio ascendente como desempate | Candidatos ampliados y ranking comercial |
| Tarifas de Haiku aplicadas a Sonnet | Tarifas no ligadas al modelo | `ANTHROPIC_PRICING_MODEL` y validación |
| Reserva subestimada | Estimación de 3 caracteres por token | Estimación conservadora de 2 caracteres por token |
| Envíos fallidos sin causa | El cuerpo de error de Meta no se registraba | Extracción y registro redactado del error |
| Procesos duplicados del worker | Un script no contaba el proceso hijo del venv en Windows | Detección de árboles y espera verificada |

## 10. Costos y límites presupuestarios

- Tarifas oficiales de Sonnet 5.5 verificadas (sección 6).
- Peor caso de una llamada con salida de 550 y entrada estimada de 4.801 tokens: 0,0151 USD; con salida de 700: 0,0166 USD.
- Costo real observado en cálculo: 0,0102 a 0,0157 USD por llamada con 4.358 tokens de entrada y salidas de 150 a 700.
- Límites en uso durante la última prueba: por interacción **0,020**, diario **0,20**, mensual **3,00**. El `.env` de referencia conserva 0,002 por interacción, que bloquea Sonnet; subirlo fue una decisión explícita.
- Con 0,20 diarios caben 12 o 13 llamadas en peor caso; con 3,00 mensuales, 180 a 198.

## 11. Restricciones de seguridad

- `.env` no se versiona (`.gitignore`). Las credenciales viven solo en el entorno local o en el panel de cada servicio.
- Dedicación de los procesos: el lanzador y la parada verifican que el proceso pertenezca a este repositorio antes de detenerlo.
- El webhook valida la firma de Meta. El agente rechaza datos sensibles en la entrada (tarjetas, contraseñas, cédula) y no los envía al modelo.
- Un worker con simulador nunca envía a WhatsApp.
- Las reservas presupuestarias y el bloqueo por límite se aplican antes de cada llamada facturable. No hay reintentos automáticos de llamadas de resultado incierto.
- Registros: los logs del worker contienen identificadores, modelos, conteos de tokens y códigos de error. No contienen prompts, respuestas, pensamiento ni datos del cliente.
- **Pendiente crítico:** el token de acceso de Meta apareció en capturas de pantalla durante la sesión y debe **rotarse o revocarse** en el panel de Meta, aunque siga vigente.

## 12. Funcionalidades completadas y pendientes

Completadas y verificadas en el piloto local:
- Recepción, persistencia idempotente y procesamiento por el worker.
- Respuestas comerciales con Claude Sonnet 5.5 y datos del catálogo.
- Envío por WhatsApp Cloud API y seguimiento de estados.
- Indicador "escribiendo...".
- Presupuesto con reservas, liquidación y bloqueos.
- Arranque controlado, preflight y parada verificada.

Pendientes:
- Confirmar el código de Meta de los 400 de envío e indicador con una prueba controlada.
- Reemplazar las credenciales de prueba locales que aparecen en el historial y en contenido versionado (sección 15).
- Caché de prompt (no verificado) y medición de `blocks=` en una llamada real autorizada.
- Comparación de todas las opciones en el seguimiento conversacional.
- Verificación de cifras de precio en el texto por parte del backend.
- Panel administrativo y despliegue: no desplegados.
- Arquitectura AWS: prevista (`docs/aws-architecture.md`), no desplegada.
- Multi-línea empresarial: no implementado de forma segura (escenario B del runbook).

## 13. Limitaciones de memoria conversacional

- El contexto que recibe el modelo son los últimos mensajes de la conversación, hasta 8 y 3.000 caracteres, tomados de la base de datos. No hay memoria a largo plazo ni resumen.
- Los textos fijos de transferencia y error se excluyen del historial para que no induzcan nuevas transferencias.
- Una conversación cuya transferencia sigue abierta queda pausada hasta que un operador la resuelva y la reactive.

## 14. Estado de Git y próximos pasos

- Repositorio: `rckt-workspace/agente-LaCusine` (origen HTTPS). Rama de trabajo: `feat/llm-provider-openrouter`. Último commit de la rama antes de esta fase: `a64cadc`.
- Cambios sin confirmar: ver el reporte de cierre de la sesión. No se fusionó con `main` ni se hizo force push.
- Próximos pasos: revisión de seguridad del historial, rotación de credenciales, confirmación de la causa de los 400 de Meta, y una única prueba controlada cuando se autorice.

## 15. Hallazgos de seguridad antes de publicar

Auditoría de 72 archivos del árbol de trabajo y del historial (solo patrones; sin valores):
- Ningún token de Meta, clave de Anthropic, clave de Supabase ni URL con contraseña real en archivos del árbol de trabajo. Los dobles de prueba están etiquetados como falsos.
- En `HEAD` hay **contraseñas literales de bases de prueba locales** (`127.0.0.1:55439`) en README, documentación y tres archivos de pruebas. No coinciden con los valores del `.env`. Aun así, son valores con forma de credencial en un repositorio público. **Pendiente:** sustituirlas por marcadores (`<contraseña-local>` o variables de entorno) y decidir si se reescribe el historial. La reescritura queda fuera del alcance de esta fase.
- Las claves de OpenRouter en pruebas son ficticias (marcadas como tales).

## 16. Guía para reanudar

1. Lee `docs/piloto-whatsapp-ngrok-runbook.md`.
2. Ejecuta las pruebas unitarias e integración desde `.venv`.
3. Rota las credenciales de Meta y Anthropic antes de cualquier prueba.
4. Arranca con `scripts/pilot_start.ps1`; nunca dejes dos workers.
