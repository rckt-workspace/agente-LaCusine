# Auditoría del modelo comercial — 5 de noviembre de 2026

Fase 1 de la analítica comercial. Describe qué datos existen, cuáles permiten indicadores históricos y qué falta. Todo lo afirmado se comprobó contra el código y las migraciones; no se leyeron datos personales.

## 1. Tablas y datos disponibles

| Tabla | Qué aporta a la analítica | Retención actual |
|---|---|---|
| `messages` | Mensajes **entrantes** (`direction='inbound'`), con `wa_message_id` único. Las respuestas salientes **no** se registran aquí. | El texto se vacía a su vencimiento; **la fila se conserva**. |
| `conversations` | Conversaciones nuevas (`started_at`), estado de automatización. | Se conserva. |
| `leads` | Leads nuevos (`created_at`), estado (`new`, `qualifying`, `qualified`, `quoted`, `handed_off`, `won`, `lost`, `opted_out`), `acquisition_source`. | Se conserva. |
| `human_handoffs` | Derivaciones a asesor por motivo y fecha. | Se conserva. |
| `llm_usage_events` | Tokens, costo estimado y estado de cada llamada a la IA. Sin contenido. | Se conserva. |
| `budget_reservations` | Reservas presupuestarias por estado. | Se conserva. |
| `webhook_events` | Eventos recibidos de Meta. **No** es fuente de ventas ni de mensajes entregados. | **Se borran** las filas terminales a los 7 días. |
| `outbound_messages` | Respuestas enviadas, con estado de entrega (`sent`, `delivered`, `read`, `failed`). | **Se borran** las filas cerradas a los 7 días. |

## 2. Hallazgo crítico: la retención destruiría la historia

La función `purge_expired_personal_data` (migración `20261029100100`) borra filas de `webhook_events` y de `outbound_messages`. Sin corrección, los indicadores de entrega y de eventos de semanas anteriores desaparecerían.

**Corrección aplicada** (migración `20261105100000_analytics_retention_rollups.sql`): antes de borrar, la misma sentencia suma los conteos por día de Bogotá y por estado en `analytics_daily_counts`. Cada fila se cuenta exactamente una vez. Verificado en PostgreSQL local: el informe semanal da **los mismos totales antes y después** de la limpieza, y una segunda limpieza no duplica nada.

**Segundo hallazgo, de privacidad:** la limpieza **nunca borraba** las respuestas en estado `read`, aunque es un estado final. Su texto permanecía indefinidamente. Corregido en la misma migración.

## 3. Fallos de consulta detectados durante la verificación

- Ambigüedad de columnas (`status`, `created_at`) al unir `llm_usage_events` con `conversations`. Corregido calificando todas las columnas.
- Permisos perdidos al recrear la función de limpieza. Restaurados en la misma migración con las concesiones de `20261101100000`.

## 4. Qué permite calcular cada indicador

**Disponible hoy, con datos reales:**
- Mensajes entrantes, conversaciones nuevas, leads creados por estado.
- Respuestas aceptadas por Meta, entregadas y leídas, fallidas (vivas y consolidadas).
- Derivaciones por motivo.
- Llamadas a la IA exitosas y fallidas, tokens, costo estimado, costo por conversación y por lead.
- Reservas presupuestarias por estado.

**Sin datos todavía (requieren eventos adicionales):**
- **Ventas confirmadas, ingresos, ticket promedio y conversión a venta:** no existe el registro de ventas confirmadas (Fase 3). Ninguna señal de la IA ni estado `won` cuenta como venta.
- **Intención de compra y cotizaciones solicitadas por lead:** no se persisten como señales; solo existen como lógica de escalamiento.
- **Bloqueos por presupuesto:** no se guardan; solo quedan en el log.
- **Pérdidas (`lost`):** solo existen si un administrador las marca.
- **Productos vendidos:** requiere un registro real de pedido.

## 5. Riesgos y decisiones pendientes

- `leads.status = 'won'` puede asignarse manualmente sin importe ni pedido. El informe lo muestra como estado administrativo, no como venta.
- Las respuestas de contingencia no tienen estado propio; se cuentan dentro de las consultas fallidas sin separarlas.
- El informe depende de que las tablas de conteo sigan existiendo. Si se cambia la retención, hay que revisar esta auditoría.

## 6. Validación

- Pruebas de integración de retención (4), de analítica contra la base (3) y de migraciones (5) pasan sobre PostgreSQL local desechable.
- La migración nueva no se ha aplicado en Supabase Cloud. Requiere respaldo y autorización explícita.
