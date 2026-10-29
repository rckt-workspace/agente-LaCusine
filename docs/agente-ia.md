# Motor comercial de IA — Fase 3

Estado: implementado y probado con proveedor simulado y PostgreSQL local. **No conectado a WhatsApp, no llama a Claude, no envía mensajes.**

## Flujo de una conversación

```
evento de WhatsApp (ya persistido en webhook_events)
  └─ CommercialAgent.handle(conversation_id, texto, call_id)
       1. Apagado de emergencia / proveedor disponible
       2. Control humano: si un asesor tiene la conversación → silencio
       3. Sanitización + detección de datos sensibles e instrucciones maliciosas   (sin llamada)
       4. Idempotencia por call_id (wamid)                                        (sin llamada)
       5. Solicitud explícita de asesor → escalamiento determinista                (sin llamada)
       6. Búsqueda acotada (≤5 productos) + presupuesto del cliente
          + contexto de esta conversación (≤8 mensajes, ≤3000 caracteres)
       7. Precheck de presupuesto (diario, mensual, por interacción)
       8. Llamada al proveedor (FakeLLMProvider o AnthropicAgentClient)
       9. Registro de costo idempotente (solo éxitos)
      10. Validación: esquema Pydantic → productos (solo candidatos reales) → promesas prohibidas
      11. Etapa comercial y escalamiento por señales observables
      → AgentOutcome (no envía nada)
```

## Componentes

| Módulo | Responsabilidad |
|---|---|
| `app/agent/orchestrator.py` | Flujo y controles; resultado tipado (`AgentOutcome`) |
| `app/agent/schemas.py` | `AgentReply` con `extra=forbid`, escalamiento consistente, ≤5 productos |
| `app/agent/safety.py` | Entrada (tarjetas con Luhn, credenciales, cédulas, inyecciones), salida (promesas, enlaces) y redacción para logs |
| `app/agent/qualification.py` | Señales observables: compra, cotización, garantía, devolución, reclamo, presupuesto, ciudad |
| `app/agent/handoff.py` | Escalamiento: el backend decide; el modelo solo puede añadir |
| `app/agent/conversation_context.py` | Historial acotado de una sola conversación; sin memoria en proceso |
| `app/agent/knowledge.py` | Carga del prompt y de las políticas como datos de referencia |
| `app/agent/factory.py` | Única selección de proveedor |
| `app/integrations/llm.py` | Interfaz `LLMProvider`, errores tipados y `FakeLLMProvider` |
| `app/integrations/anthropic_client.py` | Adaptador del SDK oficial (único módulo que lo importa) |
| `app/services/product_search.py` | Búsqueda por nombre, marca, referencia, categoría, presupuesto; corrección de erratas |
| `app/services/cost_tracker.py` | Estimación de costo, presupuestos, límites, precheck |
| `app/services/catalog_xlsx.py` | Importador XLSX con validación y resumen |
| `app/repositories/products.py`, `llm_usage.py`, `crm.py` | Persistencia (consultas parametrizadas, límites) |

## Controles de seguridad

- **Datos sensibles:** tarjetas válidas (Luhn), solicitudes de contraseñas o CVV y cédulas no se envían al modelo. Se responde un mensaje fijo y solo se registra la categoría.
- **Instrucciones maliciosas:** detección de variantes en español e inglés; respuesta fija sin llamada. El prompt declara catálogo, políticas e historial como **datos, no instrucciones**.
- **Salida del modelo:** se rechaza si el JSON no cumple el esquema (campos extra, escalamiento sin motivo, más de 5 productos, IDs repetidos), si está truncada, si promete descuentos, stock garantizado, fechas exactas, pagos confirmados o asesoría jurídica definitiva, o si contiene enlaces fuera de `lacuisineappliances.co`. Cualquier rechazo abre una solicitud de asesor.
- **Productos:** el modelo solo puede recomendar IDs de los candidatos enviados; los demás se descartan. Un ID inventado nunca llega al cliente.
- **Logs:** solo identificadores, modelo, conteos de tokens y códigos de error. Nunca prompts, respuestas ni datos personales. Los mensajes de error del SDK no se propagan.
- **Red:** en las pruebas, una barrera de red bloquea cualquier conexión fuera de loopback.
- **Credenciales:** `ANTHROPIC_API_KEY` solo desde el entorno; oculta en `repr` de la configuración.

## Catálogo fuente (verificado el 28/10/2026)

Archivo: `Catalogo_La_Cuisine_Agente_WhatsApp.xlsx`, hoja `Catálogo`. **No se copió al repositorio.**

| Dato | Valor |
|---|---|
| Filas | 349 |
| Referencias únicas | 349 (sin duplicados) |
| Marcas / categorías | 30 / 19 |
| Columnas | Marca, Categoría, Producto, Referencia, Precio (COP), Precio antes (COP), Ahorro (COP), Disponibilidad, Nota de entrega, Descripción, Link del producto |
| Disponibilidad | 342 "Disponible", 7 "Agotado" |
| Precio anterior | presente en 195 filas |
| Enlaces | 349 HTTPS, dominio `lacuisineappliances.co` |
| Validación del importador | 349 válidas, 0 inválidas, 0 duplicadas |

Notas de entrega como "Entrega a finales de octubre" son relativas: se muestran tal cual y nunca se convierten en fechas.

Importación (dry-run por defecto; `--apply` escribe con `DATABASE_URL`):

```powershell
.venv\Scripts\python.exe -m app.services.catalog_xlsx --file "RUTA\Catalogo_La_Cuisine_Agente_WhatsApp.xlsx" --extracted-on 2026-10-26
```

El resumen muestra solo conteos y códigos de error. Con `--apply` la escritura solo ocurre si no hay filas inválidas o duplicadas (o con `--allow-partial`).

**Pendiente:** aplicar la importación en el proyecto Supabase real requiere autorización.

## Base de conocimiento (PDF)

Se extrajo el texto de las 8 páginas para clasificar las políticas. Están en `app/agent/policies/lacusine_policies.json` con tres categorías:

- **Aprobado (10):** identidad, precios de referencia, pagos en la web, tiendas, plazo de entrega sin fecha exacta, retiro gratis con 24 h, edad mínima, fletes cotizados, mantenimiento, garantía legal de la Ley 1480.
- **Pendiente de validación legal (10):** notablemente, que los accesorios no tienen garantía, que los productos en promoción no admiten devolución, el 25 % de gasto administrativo, los días sin IVA, la cancelación por precio erróneo y la solicitud de cédula para retiro por tercero. El agente **no** afirma estas reglas como absolutas; escala.
- **Escalamiento obligatorio (10):** garantías, devoluciones, reclamos, estado de pedidos, cotizaciones, stock exacto, descuentos, datos personales, productos fuera de catálogo y asesoría jurídica.

## Modelo y tarifas (verificado 28/10/2026)

Fuente: documentación oficial de Anthropic (`platform.claude.com/docs/en/about-claude/pricing` y `models/overview`).

- **Candidato para tareas de alto volumen y baja latencia:** `claude-haiku-5-5`. Retiro no antes del 7 de octubre de 2027.
- Precios por millón de tokens (prompts de hasta 100.000 tokens): entrada 0.10 USD, salida 0.50 USD, escritura de caché a 5 min 0.125 USD, lectura de caché 0.01 USD.
- Alternativas documentadas: `claude-sonnet-5-5` (entrada 2 USD, salida 10 USD) y `claude-opus-5-5` (4 y 20 USD).
- **`ANTHROPIC_MODEL` queda vacío.** No se fija un modelo hasta que el equipo lo elija con su presupuesto.
- Las tarifas **no están en el código**: se configuran por variables de entorno. Sin entrada y salida configuradas, el agente bloquea cualquier llamada real.

## Costos

- **Estimación, no factura:** el costo se calcula con las tarifas configuradas. Las facturas oficiales de Anthropic pueden diferir.
- **Precheck:** antes de cada llamada se calcula el peor caso (entrada estimada + máximo de salida). Se bloquea si excede el límite por interacción, el diario o el mensual. El día y el mes siguen la hora de Colombia (UTC−5 fijo).
- **Idempotencia:** el éxito se registra una sola vez por `call_id` (índice parcial). Un reintento del mismo evento no vuelve a sumar costo. Los fallos se auditan sin costo.
- **Proveedor simulado:** nunca registra costo, aunque haya tarifas configuradas.
- **Caché de prompts:** desactivada (`ANTHROPIC_PROMPT_CACHING=false`). La escritura cuesta 1.25× la entrada y la lectura 0.1×, así que solo compensa si el mismo prefijo se reutiliza muchas veces dentro del tiempo de vida de la caché. Antes de activarla: medir la tasa real de aciertos y verificar en la documentación el mínimo de tokens cacheables, que no se ha confirmado en esta fase.
- **Interruptor:** `AGENT_KILL_SWITCH=true` desactiva el agente de inmediato.

## Riesgos y límites conocidos

1. **Presupuesto no atómico entre procesos.** El precheck y el registro no son una transacción; dos procesos simultáneos pueden superar el límite por poco. Mitigación: un solo worker en la Fase 5, o un contador atómico en la base.
2. **Fallo de auditoría tras una llamada exitosa.** Si la base falla al registrar el costo, la excepción se propaga y el reintento puede volver a llamar al modelo (y facturar). Se prefiere no ocultar el fallo. Debe resolverse con cola y reintento de la auditoría en la Fase 5.
3. **Reintentos del adaptador.** Solo se reintentan 429 y 5xx/529, porque la API no factura la generación rechazada. Los timeouts no se reintentan porque pueden haberse facturado.
4. **Validación jurídica pendiente.** Las políticas marcadas no deben usarse como afirmaciones absolutas hasta que LCI las valide.
5. **Vocabulario de búsqueda en memoria por proceso.** Un catálogo nuevo requiere reiniciar el proceso para que las erratas se corrijan contra el vocabulario actualizado.
6. **Extracción del PDF.** La última respuesta de FAQ (cafetera Jura) aparece truncada en el texto extraído; conviene revisarla en el documento original.
7. **Búsqueda sin índice trigram.** Con 349 filas el `strpos` sobre `search_text` es suficiente; con catálogos mucho mayores hará falta un índice `pg_trgm`.
8. **No hay envío a WhatsApp.** El resultado es un objeto; el envío, la persistencia de la respuesta y la cola son de la Fase 4-5.

## Pruebas

- Unitarias (sin base de datos, sin red): seguridad, calificación, esquema, contexto, costos, adaptador con SDK simulado, búsqueda, importador XLSX, reglas de selección de proveedor (`test_factory.py`) y 40 conversaciones comerciales del orquestador.
- Integración (PostgreSQL local protegido): importación repetible, búsqueda sobre la base, costos concurrentes, contexto por conversación, escalamiento persistido y dos flujos extremo a extremo.
- Opcional: `LACUSINE_CATALOG_XLSX=<ruta>` valida el archivo real sin imprimir productos.

## Requisitos para las fases siguientes

Estos riesgos son conocidos y no se resuelven en esta fase. Son condiciones para producción:

1. **Worker y cola persistente (Fase 5).** Un único consumidor por conversación, o un contador atómico en PostgreSQL para el presupuesto. El precheck actual no es atómico entre procesos.
2. **Auditoría diferida.** Si la base falla después de una llamada exitosa, el costo debe quedar en cola y registrarse al recuperarse. Hoy la excepción se propaga y un reintento puede volver a facturar.
3. **Catálogo en memoria.** El vocabulario de corrección de erratas se carga una vez por proceso. Tras una reimportación hay que reiniciar el proceso o invalidar la caché.
4. **Políticas pendientes.** Las afirmaciones marcadas como pendientes de validación legal no deben aparecer como absolutas. La protección actual es heurística (patrones de texto) y no sustituye la revisión humana ni la validación jurídica.
5. **Prompt injection.** Las defensas son de dos capas (detección de entrada y validación de salida) y reducen el riesgo, pero no lo eliminan. Ninguna respuesta del modelo se envía sin validar.
6. **Envío real.** El resultado del agente no es un envío. La Fase 4 debe conservar el control humano y el apagado de emergencia antes de conectar WhatsApp.
7. **Caché de prompts.** Desactivada. No activarla sin medir la reutilización real.
