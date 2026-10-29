# ADR 0001 — PostgreSQL como cola de trabajos del MVP

Estado: aceptado (Fase 4). Fecha: 2026-10-29.

## Contexto

Los eventos de WhatsApp llegan al webhook y deben procesarse después por un worker: contexto, catálogo, IA, calificación y respuesta. Necesitamos reintentos, recuperación tras reinicios, orden por contacto, un solo consumidor por conversación y presupuesto compartido entre workers.

## Decisión

Usar la tabla `webhook_events` de PostgreSQL como cola, con las columnas de lease, reintento y estado necesarias. La adquisición se hace con `SELECT ... FOR UPDATE SKIP LOCKED` en una transacción corta. El orden y la exclusión por contacto se garantizan con una restricción de unicidad (`contact_leases`).

## Alternativas descartadas

- **Redis / RQ / Celery:** añaden un servicio, un modelo de fallos distinto y no ofrecen transacciones con los datos del negocio. Sin una carga que lo justifique, no compensan.
- **RabbitMQ:** mismo problema, y la publicación de mensajes no es atómica con la escritura en PostgreSQL.
- **Variables en memoria:** pierden trabajo ante un reinicio. Descartado por requisito.

## Consecuencias

- La cola escala verticalmente y es suficiente para el volumen de campañas previsto. Hay que vigilar la contención de la tabla si el volumen crece.
- Cada garantía (exclusión, orden, presupuesto) está en una restricción o transacción verificable con pruebas de concurrencia.
- Si el volumen supera lo razonable, la migración a un broker implica reemplazar `JobRepository`, sin tocar el procesamiento.
