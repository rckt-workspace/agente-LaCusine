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

## 6. Pendiente

- Aplicar 16 y 17 en Cloud **solo con autorización explícita** y copia de seguridad.
- La reconciliación de historial requiere decisión humana sobre el estado real de 1–15 en Cloud.
