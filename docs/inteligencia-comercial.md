# Inteligencia comercial — estado y reglas

Documento de la implementación local. Describe qué está hecho, cómo se verifica y qué falta. No se ha aplicado nada en Supabase Cloud.

## 1. Modelo de datos (migración `20261105110000_commercial_events_sales.sql`)

- `commercial_events`: hechos observables (interés por producto, intención de compra, cotización solicitada, transferencia a asesor). **Append-only**: un disparador impide modificarlos o borrarlos. `dedupe_key` única evita duplicados por reintento.
- `lead_stage_history`: historial de cambios de etapa, también append-only.
- `sales`: ventas confirmadas, con identificador único `sale_ref`, importe entero en COP, fecha de venta con zona horaria, fuente de confirmación (`admin_manual` o `external_order_system`), responsable y evidencia.
- `sale_products`: productos vendidos, referenciados por `products.id` del catálogo.
- `sale_status_events`: confirmación, cancelación y su historial. Una venta nunca se borra.

Permisos: RLS activado, sin acceso para `anon` ni `authenticated`. El rol de la aplicación escribe y lee; el rol administrativo solo lee.

## 2. Reglas que el código garantiza

- **Una intención de compra no es una venta.** El procesador registra `purchase_intent`; el servicio de ventas es el único que crea registros en `sales`, y solo lo llama la confirmación administrativa.
- **Solo `admin` confirma o cancela** (`app/services/commercial.py`). Operadores y cualquier otro rol reciben `forbidden` antes de tocar la base.
- **Validación antes de escribir:** importe entero entre 1 y 10.000 millones de COP, fecha con zona horaria y no futura, fuente válida, evidencia obligatoria, cantidades positivas.
- **Duplicados:** la misma venta con los mismos datos se reconoce como `duplicate` y no se repite. La misma referencia con datos distintos se rechaza como conflicto; nunca se sobrescribe.
- **Registro de eventos aislado:** si la base falla al registrar un evento, la respuesta al cliente se envía igual. El mensaje de error no se escribe en el log (solo el código).
- **Derivado solo de señales del backend:** la etapa `HIGH_INTENT`, el motivo de transferencia validado y los productos validados contra el catálogo. El texto del modelo no crea eventos.

## 3. Analítica semanal

- Periodo: lunes 00:00 a lunes siguiente 00:00, hora de Bogotá, intervalo semiabierto.
- Ventas e ingresos: solo ventas confirmadas y no canceladas, por fecha de venta.
- Conversión: cohorte de leads creados en la semana que tienen una venta confirmada vigente. Sin cohorte, la conversión es `None`, no cero.
- **Sin datos frente a cero real:** si nunca hubo eventos ni ventas, el estado es `sin_datos` y los valores son `None`. Si hubo historia, un cero es un cero real.
- El estado administrativo `won` de un lead no se cuenta como venta.
- Exportación CSV en filas (sección, indicador, valor), sin datos personales.

## 4. Verificación

- Unitarias: permisos, validación de cada campo, eventos derivados, cálculo de la semana, tasas sin división por cero, CSV. Todas pasan.
- Integración sobre PostgreSQL desechable: confirmación única, duplicado inocuo, conflicto sin sobrescritura, cancelación sin borrado, producto inexistente rechazado, deduplicación de eventos, disparador de solo inserción, informe semanal con venta confirmada y cancelada.
- Ningún test llamó a Meta, a Anthropic ni a Supabase Cloud.

## 5. Pendiente (no implementado)

- **Endpoints HTTP** para confirmar ventas, cancelarlas y consultar el informe. El servicio ya exige el rol; falta conectarlo a la API con la autenticación administrativa existente y probar los permisos por HTTP.
- **Dashboard** en el frontend existente (módulo "Inteligencia Comercial").
- **Documentos PDF** y exportación desde la interfaz. Los datos ya salen en CSV desde el servicio.
- **Resumen narrativo con Claude**: no implementado. Si se añade, solo con datos agregados y presupuesto propio.
- **Atribución de Meta Ads**: no hay datos de atribución en el esquema; `acquisition_source` existe pero no se usa en la analítica todavía.
- **Bloqueos por presupuesto** no se persisten; solo quedan en el log.
- **`ai_assisted`** existe en `sales` pero nada lo calcula todavía.
- La migración **no está aplicada en Supabase Cloud**. Hasta aplicarla, el registro de eventos falla de forma aislada y el piloto sigue respondiendo.

## 6. Requisito antes de producción

Aplicar la migración en Supabase Cloud solo con respaldo y autorización explícita, y luego verificar con una consulta de solo lectura que las tablas existen y que `lacusine_backend` tiene solo los permisos listados.

## 7. API administrativa (`app/admin_api/commercial.py`)

Mismo modelo de autenticación que `/api/admin`: JWT de Supabase verificado en FastAPI, rol desde `app_metadata`. Sin token o con token inválido: 401. Rol insuficiente: 403.

| Método y ruta | Rol | Qué devuelve |
|---|---|---|
| `GET /api/admin/commercial/report?day=YYYY-MM-DD` | operador o admin | Informe semanal que contiene `day` (lunes a lunes, Bogotá) |
| `GET /api/admin/commercial/report.csv?day=` | operador o admin | CSV con los mismos cálculos (sección, indicador, valor) |
| `GET /api/admin/commercial/report.pdf` | operador o admin | 501 `pdf_not_available`: no implementado (ver pendientes) |
| `GET /api/admin/commercial/trend?weeks=1..26` | operador o admin | Semanas recientes: mensajes, conversaciones, ventas, ingresos |
| `GET /api/admin/commercial/sales?limit=1..200` | operador o admin | Ventas recientes con estado actual |
| `POST /api/admin/commercial/sales` | solo admin | Confirma una venta; responsable = `sub` del token, nunca el cuerpo |
| `POST /api/admin/commercial/sales/{sale_ref}/cancel` | solo admin | Cancela con motivo (5 a 300 caracteres) y deja evento auditable |

Errores estables: `422` con código de validación (`amount_out_of_range`, `evidence_ref_required`, `sold_at_in_future`…), `409` para conflicto o estado inválido, `503` si la base no responde. Nunca se devuelve un traceback.

Fechas fuera de rango (antes de 2020 o más de un día en el futuro): `422 date_out_of_range`.

## 8. Dashboard

Sección **Inteligencia comercial** en el panel existente (`frontend/src/pages/Commercial.tsx`). Muestra el resumen ejecutivo, indicadores, productos consultados, origen, rendimiento del agente, tendencia de seis semanas y la tabla de ventas. Los administradores pueden confirmar y cancelar; los operadores solo consultan.

- La interfaz **no decide permisos**: FastAPI aplica el rol en cada endpoint.
- Un indicador sin datos aparece como "Sin datos", no como cero.
- La intención de compra se rotula como "No es una venta".
- Verificado: `npm run typecheck` y `npm run build` pasan. No hay pruebas automatizadas del navegador para esta sección.

## 9. Definiciones de los indicadores

- **Respuestas aceptadas por Meta**: estados `sent`, `delivered` y `read` (vivos y consolidados).
- **Entregadas o leídas**: `delivered` y `read`. Es el numerador de la tasa de entrega.
- **Tasa de entrega**: entregadas o leídas / aceptadas. `None` si no hay aceptadas.
- **Intención de compra**: leads distintos con evento `purchase_intent` en la semana. No es venta.
- **Venta confirmada**: venta con último estado `confirmed` y fecha de venta dentro de la semana.
- **Ingresos**: suma del importe de ventas confirmadas (no canceladas) por fecha de venta.
- **Conversión**: de los leads creados en la semana, cuántos tienen alguna venta confirmada vigente. Sin leads, `None`.
- **Costo de IA**: suma del costo estimado de llamadas exitosas. Por conversación y por lead, `None` si no hay denominador.
- **Origen**: `referral_source_type` capturado por Meta. Hoy no hay registros con origen: el informe lo declara como "Sin información de origen disponible".

## 10. Informes PDF

- Endpoint: `GET /api/admin/commercial/report.pdf?kind=operativo|marketing|oportunidades|ventas|ejecutivo&day=YYYY-MM-DD&narrative=false`. Roles `operator` y `admin`.
- Respuesta `application/pdf`, `Content-Disposition: attachment` con nombre generado (`la-cuisine-<tipo>-<fecha>.pdf`) y `Cache-Control: no-store`. Nada se guarda en Supabase.
- Motor: ReportLab 5.1.0, fijado en `requirements.txt`. Presenta solo cifras del informe semanal; sin datos aparece "Sin datos suficientes" y un cero real se muestra como 0.
- Cada informe incluye portada, periodo (fechas en español, último día incluido), tablas con encabezado repetido, gráficos, comparación con la semana anterior cuando existe, hallazgos, recomendaciones y limitaciones de los datos.
- Límites: tablas de 60 filas como máximo; documento de 5 MB como máximo (`pdf_too_large`, 413). Fallos internos: `500 pdf_generation_failed` sin detalle.
- El dashboard (`Commercial.tsx`) permite elegir tipo, periodo y descarga CSV o PDF.
- Verificación visual: muestras generadas con datos sintéticos y revisadas como imágenes (portada, tablas largas con encabezado repetido, gráficos, periodo vacío). Las muestras no están en el repositorio.

## 11. Análisis narrativo con IA (opcional)

- Desactivado por defecto. Se activa con `ANALYSIS_AI_ENABLED=true`.
- Configuración propia: `ANALYSIS_AI_MAX_OUTPUT_TOKENS` (400 por defecto), `ANALYSIS_AI_BUDGET_USD_PER_REPORT` (0,02 por defecto), `ANALYSIS_AI_TIMEOUT_SECONDS` (20 por defecto). Requiere además `ANTHROPIC_API_KEY` y tarifas de `ANTHROPIC_PRICING_MODEL` igual al modelo.
- Solo recibe cifras agregadas: sin nombres, teléfonos, conversaciones ni credenciales.
- Cada cifra de la respuesta debe existir en el informe; si el modelo inventa una, la narrativa se descarta. Las cifras pequeñas de redacción (menores de 10) no se exigen.
- Si el presupuesto del informe no alcanza, el modelo no se llama. Si el modelo falla o responde mal, el PDF se genera sin la sección narrativa.
- Verificado con proveedor simulado. **No se ha ejecutado ninguna llamada real a Claude.**

## 12. Pendiente

- **Presupuesto acumulado de informes con IA**: `llm_usage_events` solo admite el propósito `commercial_reply`. Registrar estos costos en base requiere una migración incremental que amplíe ese propósito. Hoy el límite es por informe.
- **Automatización semanal**: no implementada. Cualquier consumidor debe ser un único proceso para no duplicar informes.
- **Atribución de Meta Ads**: sin registros reales de origen.
- **Bloqueos por presupuesto**: no se persisten.
- **Migración `20261105110000`**: no aplicada en Supabase Cloud.
- **Pruebas automatizadas del navegador** para la sección comercial: no existen.
