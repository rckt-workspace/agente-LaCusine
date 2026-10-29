# Supabase PostgreSQL — preparación (no aplicado)

Estado: **preparado y probado solo en PostgreSQL local de pruebas.** Ninguna migración se ha aplicado a Supabase Cloud, y no se ha modificado ningún proyecto real.

## Orden de migraciones

Las migraciones son incrementales y se aplican **en este orden exacto**. Cada archivo se ejecuta en una transacción.

| # | Archivo | Fase | Contenido |
|---|---|---|---|
| 1 | `20261027100000_core_schema.sql` | 2 | Tablas base, restricciones, índices, triggers |
| 2 | `20261027100100_rls_and_grants.sql` | 2 | RLS y retirada de privilegios a `anon` y `authenticated` |
| 3 | `20261027100200_retention.sql` | 2 | Retención inicial (reemplazada en efecto por la 8) |
| 4 | `20261028100000_products_pricing_search.sql` | 3 | Precios, disponibilidad y búsqueda de productos |
| 5 | `20261028100100_llm_usage_events.sql` | 3 | Auditoría de costos de Claude, con RLS |
| 6 | `20261028100200_handoff_reason_return.sql` | 3 | Motivo `return_request` |
| 7 | `20261029100000_worker_queue.sql` | 4 | Cola: leases, `contact_leases`, `outbound_messages`, `budget_reservations` |
| 8 | `20261029100100_retention_worker_safe.sql` | 4 | Retención que solo elimina estados finales |
| 9 | `20261030100000_whatsapp_delivery.sql` | 5 | Estados de entrega, lease de envío, eventos de estado |
| 10 | `20261031100000_phase_5_1_hardening.sql` | 5.1 | Marca de ventana confiable, tipo de salida (`reply`/`handoff_notice`), línea de negocio en salidas, precio confirmado y `admin_audit_log` |

La migración 3 queda reemplazada en efecto por la 8. Ninguna se edita: el historial se conserva.

La migración 10 añade `admin_audit_log` con RLS activa con `update`, `delete` y `truncate` revocados a `public`, `anon` y `authenticated`, de modo que el backend y los roles públicos no pueden reescribir el historial de decisiones administrativas. Un rol con privilegios de propietario aún puede alterarla: ese riesgo queda en el registro de auditoría como limitación.

**Antes de aplicar:** las migraciones de las fases 2 a 5.1 se probaron en PostgreSQL 16 local, todas en una sola ejecución. Eso no garantiza el comportamiento idéntico en Supabase. Verificar en un proyecto de prueba aislado antes de producción.

## Compatibilidad con PostgreSQL de Supabase

- `gen_random_uuid()` es nativo desde PostgreSQL 13. **No se requiere extensión** `pgcrypto`.
- No se usa `pg_trgm` ni `unaccent`. La normalización de texto se hace en Python.
- Las funciones usan `set search_path = ''` y referencias calificadas (`public.tabla`).
- Las migraciones revocan privilegios a `anon` y `authenticated` **si esos roles existen**. Supabase los crea; PostgreSQL local no. Las sentencias son condicionales para funcionar en ambos.
- Supabase concede `EXECUTE` por defecto sobre funciones de `public`. La migración 2 y la 8 lo retiran explícitamente.

## Acceso desde el backend

### Rol de base de datos

El backend debe usar un **rol dedicado de mínimo privilegio**, no el rol `postgres` ni `service_role` en el frontend. Pasos (a ejecutar con autorización, en el proyecto real):

1. Crear un rol de login con contraseña fuerte generada fuera del repositorio.
2. Conceder `usage` sobre el esquema `public` y `select, insert, update, delete` solo sobre las tablas de la aplicación.
3. Conceder `execute` solo sobre `purge_expired_personal_data`.
4. No conceder privilegios sobre `auth`, `storage` ni otros esquemas de Supabase.

Las tablas tienen RLS activa y ningún privilegio para `anon` ni `authenticated`. Por tanto, las claves públicas de Supabase no pueden leer prospectos, mensajes, colas ni reservas.

### Elegir la cadena de conexión

Los modos siguientes provienen de la documentación oficial de Supabase; **verificar la lista vigente en el panel del proyecto**, porque el panel muestra las cadenas exactas.

| Modo | Host | Puerto | Soporte IPv4 | Sentencias preparadas |
|---|---|---|---|---|
| Conexión directa | `db.<ref>.supabase.co` | 5432 | IPv6 (IPv4 con complemento) | Sí |
| Pooler compartido, modo sesión | `aws-<n>-<región>.pooler.supabase.com` | 5432 | Sí, en todos los planes | Sí |
| Pooler compartido, modo transacción | `aws-<n>-<región>.pooler.supabase.com` | 6543 | Sí, en todos los planes | **No** |
| Pooler dedicado, modo transacción | `db.<ref>.supabase.co` | 6543 | IPv6 en Pro o superior | Sí (PgBouncer 1.21+) |

**Recomendación para Render:** el pooler compartido en **modo sesión (5432)**. Render no tiene IPv6 garantizado, el pooler funciona por IPv4 sin complementos, y el worker mantiene un pool de conexiones persistente.

**Compatibilidad del código:** el pool desactiva las sentencias preparadas (`prepare_threshold=None`), así que también funciona en el modo transacción (6543). En ese modo, la cola y las reservas siguen siendo correctas porque usan transacciones cortas y bloqueos transaccionales (`SKIP LOCKED`, `pg_advisory_xact_lock`). No se usan `LISTEN/NOTIFY` ni estado de sesión.

**Migraciones:** aplicarlas por la conexión directa o por el modo sesión. El modo transacción no es adecuado para DDL de varias sentencias.

### SSL

- Usar `sslmode=require` como mínimo en `DATABASE_URL`. Psycopg sin especificar el modo puede caer a texto plano en algunos entornos.
- `sslmode=verify-full` con el certificado raíz del proyecto es la opción más segura. Requiere subir el certificado; no se incluye en el repositorio.

## Conectividad y readiness

- `GET /ready` ejecuta `select 1` sobre el pool. Un 503 indica que la base no responde, sin revelar el motivo.
- Antes de habilitar el worker: confirmar `/ready` = 200 desde Render y que `select count(*) from public.webhook_events` responde con el rol del backend.

## Retención

- `purge_expired_personal_data(p_batch_size)` elimina por lotes eventos en estados finales vencidos, contenidos de mensajes vencidos y respuestas cerradas vencidas.
- No elimina pendientes, en proceso, en revisión manual ni reservas.
- Se ejecuta con el rol del backend (privilegio concedido en el paso 3 del rol). Programarla debe hacerse con autorización: no hay un job programado en el repositorio.

## Respaldo y recuperación

- Verificar en el panel de Supabase qué respaldos incluye el plan contratado y la retención de cada uno. **Este repositorio no asume ninguna política de respaldo.**
- Antes de cada migración en producción: respaldo lógico o punto de restauración confirmado.
- Las migraciones son hacia adelante. Un error se corrige con una nueva migración o con restauración. Probar la restauración en un proyecto aislado antes de necesitarla.

## Procedimiento de aplicación (cuando haya autorización)

1. Crear un proyecto Supabase de **prueba** aislado y aplicar las 9 migraciones en orden.
2. Las pruebas destructivas **nunca** se ejecutan contra Supabase: el guard rechaza hosts de Supabase y exige una base de loopback. La verificación en la nube se limita a `/ready` y consultas de solo lectura.
3. Crear el rol del backend y verificar que no puede leer `auth` ni escribir fuera de las tablas.
4. Crear, si se usará la CLI de conciliación, un rol administrativo **distinto**: `select` e `insert` sobre `admin_audit_log`, y `update` solo sobre las tablas que la CLI modifica. Nunca `delete` ni `truncate` sobre la auditoría. Su conexión va en `LACUSINE_ADMIN_DATABASE_URL`.
5. Aplicar en producción en una ventana acordada, con respaldo confirmado, registrando el resultado de cada archivo.
6. Verificar RLS (`relrowsecurity = true`) y privilegios de `anon` y `authenticated` (`has_table_privilege` debe ser falso).

Las migraciones deben aplicarse en el orden de la tabla (1 a 10). Cada archivo es transaccional; un fallo a mitad de la secuencia deja las anteriores aplicadas y la siguiente sin ejecutar.

## Qué no se ha hecho

- Ninguna migración aplicada a Supabase Cloud.
- Ningún rol creado.
- Ninguna contraseña generada ni guardada.
- Ningún job de retención programado.
