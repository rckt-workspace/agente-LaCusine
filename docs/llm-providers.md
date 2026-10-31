# Proveedores de IA: Anthropic y OpenRouter

Estado: **implementado y probado con transporte simulado. Sin llamadas reales.** Ninguna clave de proveedor se ha usado durante el desarrollo. Ninguna llamada a Anthropic, OpenRouter ni Meta se ha ejecutado.

## Qué cambia y qué no

- El agente comercial no cambia. El orquestador depende solo de la interfaz `LLMProvider`.
- Se añade `OpenRouterAgentClient` (`app/integrations/openrouter_client.py`), que cumple esa interfaz.
- La selección se centraliza en `app/agent/factory.py`. Un único proveedor se elige por `LLM_PROVIDER`.
- Las tarifas se resuelven por proveedor (`pricing_for`), no con una tarifa global.

## Reglas de selección

| Condición | Resultado |
|---|---|
| `AGENT_DRY_RUN=true` (defecto) | Proveedor simulado, aunque haya otro habilitado. Nunca cobra |
| `LLM_PROVIDER` vacío | Comportamiento histórico: Anthropic solo si `ANTHROPIC_ENABLED=true` |
| `LLM_PROVIDER=fake` | Simulado. Nunca cobra |
| `LLM_PROVIDER=anthropic` | Anthropic solo si `ANTHROPIC_ENABLED=true`, clave y modelo |
| `LLM_PROVIDER=openrouter` | OpenRouter solo si hay clave, modelo, tarifas del mismo modelo, al menos un límite de presupuesto, y `AGENT_DRY_RUN=false` |
| Valor desconocido | Ningún proveedor. Se registra `llm_provider_unknown` |

Anthropic y OpenRouter nunca se construyen a la vez: `LLM_PROVIDER` elige uno. Si falta cualquier requisito, no se crea cliente y el agente responde como deshabilitado.

## Variables de OpenRouter (solo nombres)

| Variable | Obligatoria | Secreta | Descripción |
|---|---|---|---|
| `LLM_PROVIDER` | Sí, para usar OpenRouter | No | Valor `openrouter` |
| `OPENROUTER_API_KEY` | Sí | Sí | Clave. Nunca en el repositorio ni en logs |
| `OPENROUTER_MODEL` | Sí | No | Modelo. Sin valor no hay llamada |
| `OPENROUTER_PRICE_INPUT_USD_PER_MTOK` | Sí | No | Tarifa de entrada del modelo indicado en `OPENROUTER_PRICING_MODEL` |
| `OPENROUTER_PRICE_OUTPUT_USD_PER_MTOK` | Sí | No | Tarifa de salida |
| `OPENROUTER_PRICING_MODEL` | Sí | No | Debe ser igual a `OPENROUTER_MODEL`, o no hay llamadas |
| `OPENROUTER_PRICING_REFERENCE` | No | No | Texto que identifica la fuente de las tarifas |
| `OPENROUTER_TIMEOUT_SECONDS` | No | No | Por defecto 20 |
| `OPENROUTER_MAX_RETRIES` | No | No | Por defecto 1. Solo se repite un 429 |
| `OPENROUTER_MAX_CONCURRENCY` | No | No | Por defecto 2 |
| `AGENT_DAILY_BUDGET_USD` o `AGENT_MONTHLY_BUDGET_USD` | Al menos una | No | Sin límite de presupuesto no hay llamada |

Las tarifas **no se inventan**: sin ellas la llamada queda bloqueada. Hay que tomarlas de la página oficial del proveedor en la fecha del presupuesto.

## Política de facturación

- **Reserva antes de llamar.** Se reserva el peor caso. Si no cabe en el presupuesto, no se llama.
- **Facturación no garantizada como nula** (timeout, conexión interrumpida, 5xx, respuesta inválida): la reserva queda como incierta, la llamada **no se repite sola** y el caso va a conciliación.
- **No facturada** (401, 403, 400, 402, 404, 422, 429): la reserva se libera. El 429 se reintenta hasta `OPENROUTER_MAX_RETRIES`.
- **Uso sin tokens informados:** se cobra el peor caso, nunca cero.
- **Idempotencia por mensaje:** una segunda llamada con el mismo identificador no vuelve al proveedor. Si la anterior quedó incierta, responde `requires_reconciliation`.
- **Sin reintento entre proveedores** ante resultados inciertos.

## Pruebas

Ejecutadas, sin red: el adaptador con transporte simulado (36 pruebas), la selección y la facturación del agente (24 pruebas), y el resto de la suite del agente, las integraciones y el worker (293 pruebas previas, sin cambios de resultado).

Dos mutaciones deliberadas fueron detectadas por las pruebas: reintentar un timeout y eliminar el límite de presupuesto obligatorio. El archivo se restauró y quedó idéntico.

## Procedimiento de prueba autorizada (no ejecutado)

Solo con autorización expresa del responsable, que debe aprobar el consumo de cuota y el presupuesto:

1. Confirmar que el presupuesto y las tarifas oficiales del modelo están definidos y revisados.
2. Definir en un entorno local, nunca en el repositorio: `LLM_PROVIDER=openrouter`, `AGENT_DRY_RUN=false`, la clave, el modelo, las tarifas y `OPENROUTER_PRICING_MODEL` igual al modelo.
3. Usar un único mensaje sintético. No usar datos de clientes.
4. Comprobar la respuesta, la fila de uso con proveedor y modelo, y el costo estimado frente al saldo del proveedor.
5. Desactivar de inmediato: `AGENT_DRY_RUN=true` o `AGENT_KILL_SWITCH=true`.

Para Anthropic, el procedimiento equivalente está en `docs/go-live-checklist.md` y exige la misma autorización.

## Límites conocidos

- Una sola tarifa por proveedor. Cambiar de modelo exige actualizar también las tarifas y `OPENROUTER_PRICING_MODEL`. No hay tabla de tarifas por modelo.
- OpenRouter no informa caché en este adaptador: las tarifas de caché se igualan a la de entrada (sobreestima, nunca subestima).
- El costo es una estimación con las tarifas configuradas. No es una factura del proveedor.
- No se consulta el costo real que informe OpenRouter.
