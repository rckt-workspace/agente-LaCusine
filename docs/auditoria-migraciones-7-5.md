# Auditoría de migraciones — Fase 7.5

Fecha: 2026-11-05. Alcance: 17 migraciones locales en `supabase/migrations/`, comparadas con Supabase Cloud
mediante **solo consultas de lectura** (catálogo y `SELECT`). No se ejecutó ninguna migración en Cloud, no se
ejecutó `supabase db push` y no se modificó `schema_migrations`.

## 1. Migraciones locales

| # | Archivo | Propósito | Estado en Cloud (lectura) |
|---|---------|-----------|---------------------------|
| 1–13 | `20261027100000` … `20261103100000` | Núcleo, RLS, cola, WhatsApp, admin, fase 6 | Objetos presentes (tablas originales con filas). Sin tabla `schema_migrations` |
| 14 | `20261105100000_analytics_retention_rollups.sql` | `analytics_daily_counts`, purga con consolidación | Tabla y función `purge_expired_personal_data(integer)` presentes; cuerpo de la función **idéntico** al repositorio (comparación sin espacios) |
| 15 | `20261105110000_commercial_events_sales.sql` | Eventos, historial de etapas, ventas, líneas y estados (solo-anexar) | Seis tablas, restricciones, índices y cinco triggers presentes. RLS activo. Sin filas |
| 16 | `20261105120000_commercial_analytics_rls_policies.sql` | Políticas RLS para las tablas de 14 y 15 | **No aplicada**: 0 políticas en esas tablas |
| 17 | `20261105130000_business_report_budget.sql` | Libro de presupuesto de informes con IA (`purpose = 'business_report'`) | **No aplicada**: la tabla no existe |

## 2. Defecto encontrado y corregido en el repositorio

Las migraciones 14 y 15 activan RLS en las tablas comerciales pero **no crean políticas**. Los roles de la
aplicación (`lacusine_backend`, `lacusine_admin_ops`) no son propietarios ni tienen `BYPASSRLS`, por lo que con
RLS activo y sin política:

- el backend no ve filas (lecturas en cero, sin error);
- las inserciones de ventas y eventos fallan;
- administración no puede leer.

Los `GRANT` por sí solos no bastan. Esto no lo detectaron las pruebas de integración anteriores porque corrían
como superusuario. La migración 16 corrige el defecto **solo añadiendo políticas**. La prueba
`tests/integration/test_commercial_rls_postgres.py` demuestra el rojo (3 fallos sin la migración 16) y el verde
(6 aprobadas con ella).

Verificación en Cloud: las tablas comerciales pertenecen a `postgres`; el rol de aplicación no es propietario.
Por tanto, **en Cloud el defecto sigue presente hasta aplicar la migración 16**.

## 3. Permisos efectivos (lectura en Cloud, estado actual)

| Rol | Tablas comerciales | Analítica | Presupuesto de informes |
|-----|--------------------|-----------|-------------------------|
| `anon`, `authenticated` | Sin privilegios | Sin privilegios | — (tabla inexistente) |
| `lacusine_backend` | S, I (sin U/D) | S, I, U | — |
| `lacusine_admin_ops` | S | S | — |

Previsto tras aplicar 16 y 17: `lacusine_backend` S/I/U en presupuesto de informes; `lacusine_admin_ops` solo S.
El historial comercial sigue siendo solo-anexar (triggers `*_append_only`).

## 4. Orden seguro de aplicación en Supabase Cloud (NO ejecutado)

Prerrequisito: el historial de migraciones de Cloud no existe. El runner del repositorio (`app/db/migrate.py`)
crea `schema_migrations` y, si la tabla no existe, **intentaría aplicar las 15 primeras** sobre objetos que ya
existen. Eso falla o duplica. Por eso la reconciliación debe hacerse antes de usar el runner.

1. **Copia de seguridad** completa del proyecto (punto de restauración verificado antes de cualquier cambio).
2. **Reconciliación de historial** (manual, con revisión humana): crear `schema_migrations` con la definición del
   runner y registrar solo las migraciones 1–15, con su SHA-256 calculado sobre el archivo tal como está en el
   repositorio. No ejecutar el contenido de 1–15. Verificar antes que cada objeto de 1–15 existe.
3. **Aplicar 16** en una transacción: `20261105120000_commercial_analytics_rls_policies.sql`.
4. **Aplicar 17** en una transacción: `20261105130000_business_report_budget.sql`.
5. **Registrar 16 y 17** en `schema_migrations` con sus hashes.
6. **Verificar**: catálogo de políticas (la migración 16 crea 19: 3 en cada una de las cinco tablas de historial y comerciales y 4 en `analytics_daily_counts`; la 17 crea 2 en `analysis_budget_reservations`),
   `has_table_privilege` para cada rol y tabla, RLS activo, `anon`/`authenticated` sin privilegios.
7. **Prueba funcional de solo lectura** con el rol de aplicación: `select count(*)` en `sales` y
   `analysis_budget_reservations`.

Criterio de parada: cualquier divergencia de hash, objeto ausente o privilegio inesperado detiene el proceso.
La recuperación es restaurar la copia de seguridad del paso 1; no se deshacen migraciones a mano.

## 5. Verificación local realizada

- 17 migraciones aplicadas en PostgreSQL desechable (`tests/integration/test_phase6_postgres.py` fija el conteo).
- Suite de integración completa: 170 aprobadas.
- Pruebas de presupuesto de informes con concurrencia real (diez hilos, límite para tres): exactamente tres entran.
- Pruebas de RLS con roles de login no propietarios: 6 aprobadas (incluye demostración de defecto sin la migración 16).

## 6. Plan de activación en Supabase Cloud (pendiente de autorización)

Este plan no se ha ejecutado. Ninguna de sus fases escribe en Cloud sin una autorización explícita y registrada. Los
pasos de lectura se pueden hacer en cualquier momento con el rol de solo lectura del proyecto.

**Prohibido:** `supabase db push`. La CLI lleva su propio historial (`supabase_migrations`), distinto del de
`schema_migrations` que usa el runner del repositorio, y aplicaría de nuevo 1–15 sobre objetos existentes. Las
migraciones se aplican solo con el SQL revisado de cada paso, en una transacción.

**Prohibido:** `python -m app.db.migrate --apply` contra Cloud mientras `schema_migrations` no esté reconciliado.
Sin ese registro, el runner intentaría aplicar 1–15.

### 6.1 Copia de seguridad (antes de cualquier cambio)

- Copia lógica completa de la base de Cloud: `pg_dump --format=custom --no-owner` con la URL del rol propietario,
  guardada fuera del repositorio, cifrada, con su SHA-256 anotado.
- Copia física del proyecto desde el panel de Supabase (punto de restauración) y confirmación de su fecha y hora.
- Restauración de prueba de la copia lógica en una base desechable local (`pg_restore`) antes de continuar. Sin una
  restauración verificada, no se avanza.

### 6.2 Integridad de 14 y 15 aplicadas a mano

Las migraciones 14 y 15 están en Cloud, pero el runner no las registró. Comprobaciones de solo lectura:

- Objetos: tablas `analytics_daily_counts`, `commercial_events`, `lead_stage_history`, `sales`, `sale_products` y
  `sale_status_events` con sus restricciones e índices; cinco triggers de solo-anexar; función
  `purge_expired_personal_data(integer)`.
- Cuerpo de la función: comparar con `pg_get_functiondef` frente al archivo del repositorio (la auditoría ya
  confirmó igualdad sin espacios; se repite con la definición actual).
- Ausencia de filas en las tablas comerciales (`count(*)`), salvo las que haya creado una operación autorizada.

### 6.3 Reconciliación de `schema_migrations`

Hashes de referencia, calculados con `app.db.migrate.load_migrations` (que normaliza saltos de línea; no usar
`sha256sum` sobre los bytes del archivo):

| Archivo | SHA-256 |
|---------|---------|
| `20261027100000_core_schema.sql` | `7c3418e6c57dd417acbd4779dc01d3051096dd2eaa60f6eb5e373c798f5c8a7e` |
| `20261027100100_rls_and_grants.sql` | `aa349e65d1323c65a74ad4899f3fadf749215fe900f406d879c8ac3798aae2da` |
| `20261027100200_retention.sql` | `088876cc58c9ebd6fb3f68b0b00cbfb191a4caf4d8e3cfa6b69ad4fcfcd1d8ca` |
| `20261028100000_products_pricing_search.sql` | `cce1adb8173b50d85236c9b664aabe0875ff441a90e03c5e86dd74ecf3a7edd0` |
| `20261028100100_llm_usage_events.sql` | `ca14b1e05c233f22f8f14dfe7cfd4e13fe9bf32589693c279a8cb326c1f2a111` |
| `20261028100200_handoff_reason_return.sql` | `2c353ba06b46846a13dd774f4575053193edb48c907ba925d09ebdde3a66e266` |
| `20261029100000_worker_queue.sql` | `077667f4b52bb0e68e73c8d231d9b6df60065d2a3a8d3c84c5d8f1fceb9fcbfa` |
| `20261029100100_retention_worker_safe.sql` | `a606c76483dcb659f617dc437ecb06790bf4a021e0060d24f3ace8f9ec35a5ec` |
| `20261030100000_whatsapp_delivery.sql` | `e008aeefd6d4b7ff45f0f9998fd9c77746f5b5d48a4ff753e0e6b43acec36dd8` |
| `20261031100000_phase_5_1_hardening.sql` | `b106eacd207c7956662ccd19a51cd993e85a68cf6bde690c1957e8251449556f` |
| `20261101100000_app_roles_rls_policies.sql` | `fa173140fd727534126f166af06036c08c3ad99fded6a855ec2beff8b84d3b27` |
| `20261102100000_phase_6_local_completion.sql` | `819212856660525dafd94f42767d23153846cb06922cff998f3173f06946c567` |
| `20261103100000_admin_notification_visibility.sql` | `368444764cd11fe54fa07214ec7dd4794be86a6271a4b470b9498e05e4d9a8c7` |
| `20261105100000_analytics_retention_rollups.sql` | `182d3b4ee56cda7a9b6462d97576a7434bfb91c1bf7828a6f1157fca8272f59c` |
| `20261105110000_commercial_events_sales.sql` | `1ea71460486cd1c27f4851e8635565d59ce41d249c6a4549466fb2c1bd4f8b74` |
| `20261105120000_commercial_analytics_rls_policies.sql` | `7b667ca9eeceff3b741cd5c413769901139f793390e5ca4737550d12687d2455` |
| `20261105130000_business_report_budget.sql` | `5858bb43cb330f9ee4e62ca6baeb2a8c7d021b7a37f5711f8362e9c644b5832f` |

Procedimiento (requiere revisión humana del SQL generado antes de ejecutarlo):

1. Crear `schema_migrations` con exactamente la definición de `BOOTSTRAP_SQL` de `app/db/migrate.py` (RLS activo,
   `revoke all` a `public`).
2. Insertar solo las filas de 1–15, con los hashes de la tabla, en una sola transacción.
3. Verificar que `load_migrations` coincide con lo registrado: el dry-run (`python -m app.db.migrate --dir
   supabase/migrations`, sin `--apply`) debe listar exactamente 16 y 17 como pendientes.

Criterio de parada: cualquier hash distinto, objeto ausente o fila inesperada detiene el proceso.

### 6.4 Aplicar la migración 16

- Ventana con el worker detenido (`scripts/pilot_start.ps1 -StopOldWorkers`) y `META_SEND_ENABLED=false`.
- Una transacción: `supabase/migrations/20261105120000_commercial_analytics_rls_policies.sql`.
- Registrar la fila en `schema_migrations` con su hash dentro de la misma transacción.
- Verificación: 19 políticas (3 en cada una de las cinco tablas de historial y comerciales, 4 en
  `analytics_daily_counts`) y RLS activo en todas.

### 6.5 Aplicar la migración 17

- Igual que 6.4 con `supabase/migrations/20261105130000_business_report_budget.sql`.
- Verificación: 2 políticas en `analysis_budget_reservations`, tabla con RLS activo y restricciones del libro.
- El análisis con IA solo se construye si `ANALYSIS_AI_ENABLED` está activo (`app/admin_api/wiring.py`, función
  `_narrative`). Activarlo exige 6.5 aplicada.

### 6.6 Privilegios y RLS con los roles reales

Con `SET ROLE` dentro de una transacción de solo lectura, sin escribir:

- `lacusine_backend`: `select` e `insert` en `commercial_events`, `lead_stage_history`, `sales`, `sale_products` y
  `sale_status_events`; `select`, `insert` y `update` en `analytics_daily_counts` y `analysis_budget_reservations`.
  Sin `update` ni `delete` en el historial.
- `lacusine_admin_ops`: solo `select` en las cinco tablas comerciales, en `analytics_daily_counts` y en
  `analysis_budget_reservations`.
- `anon` y `authenticated`: sin privilegios sobre estas tablas (`has_table_privilege`).
- Con RLS, el rol de aplicación debe ver filas: `select count(*)` debe coincidir con el conteo del propietario.

Antes de 6.4, la misma consulta debe **fallar o devolver cero** con `lacusine_backend`. Esa es la evidencia de que el
defecto existía y de que la migración 16 lo corrige.

### 6.7 Verificación funcional de solo lectura

Con el rol de aplicación, sin escrituras:

- `select count(*)` en `sales`, `sale_products`, `sale_status_events`, `commercial_events`, `lead_stage_history`,
  `analytics_daily_counts` y `analysis_budget_reservations`.
- Dashboard de lectura (`GET /api/admin/commercial/report`, `/report.csv`, `/report.pdf?kind=operativo`) contra la
  URL de Cloud, con un token de operador real. Solo GET.
- Registro de oportunidades: el conteo de `commercial_events` y del historial de etapas debe ser coherente con los
  mensajes de `conversations`.

### 6.8 Pruebas del dashboard contra Cloud

- Smoke de lectura (6.7) y `GET /ready` con la base de Cloud.
- Prueba de **confirmación de venta**: `POST /api/admin/commercial/sales` y su cancelación. Escribe en Cloud, por
  tanto **requiere autorización explícita**, un `sale_ref` de prueba marcado como tal y el monto de prueba
  acordado. Se ejecuta en una ventana separada y se cancela el mismo día.
- Ninguna prueba de envío a Meta ni llamada a Anthropic. Las pruebas del dashboard no consumen presupuesto de IA.

### 6.9 Plan de recuperación ante fallo

| Fallo | Acción | Vuelta atrás |
|-------|--------|--------------|
| Hash distinto o fila inesperada en la reconciliación | Detener. No insertar nada más | Ninguna escritura hecha: la transacción se revierte |
| Error dentro de 16 o 17 | La transacción revierte sola | Verificar que no quedó fila en `schema_migrations` |
| Políticas o privilegios distintos de lo esperado tras 16/17 | Detener el worker y el panel | Restaurar la copia de 6.1 si no se puede corregir con un SQL revisado y autorizado |
| Lectura de 0 filas tras 16 | Verificar RLS y roles antes de cualquier cambio | Si el defecto persiste, retirar la migración solo con autorización |
| Pérdida de datos o corrupción | Restaurar la copia verificada de 6.1 | Restauración completa; no se deshacen migraciones a mano |

Antes de cualquier restauración, anotar la hora del incidente, los identificadores afectados y el estado del worker.
No se reenvían mensajes a clientes durante la recuperación.

## 7. Pendiente

- Aplicar 16 y 17 en Cloud **solo con autorización explícita** y copia de seguridad (sección 6).
- La reconciliación de historial requiere decisión humana sobre el estado real de 1–15 en Cloud.
- Decidir la estrategia de copias: quién ejecuta `pg_dump` y dónde se guarda la copia cifrada.
