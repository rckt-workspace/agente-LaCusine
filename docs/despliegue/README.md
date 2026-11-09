# Despliegue excepcional de las migraciones 16, 17 y 18 desde Supabase SQL Editor

**Estado: preparado y validado en PostgreSQL 17 desechable. NO ejecutado en producción. Decisión: NO-GO hasta cumplir los criterios de la sección 5.**

Proyecto destino: `vpnnripzdfdwjexkgdnk` (producción). Usar la sesión web del propietario del proyecto en Supabase Dashboard.

## 1. Archivos

| Archivo | Función | Ejecutar en producción |
|---|---|---|
| `00-diagnostico-solo-lectura.sql` | Identidad de la base, estructura real del historial de la CLI, registros existentes, estado de 16/17/18, conteos. Solo `SELECT`. | **Sí, primero** |
| `despliegue-16-18-sql-editor.sql` | Transacción única: comprobaciones previas, migraciones 16, 17 y 18 íntegras, filas del historial de la CLI y comprobaciones posteriores. Generado por `scripts/build_sql_editor_deploy.py`. | **Sí, solo con GO** |
| `historial-16-18.sql` | Tres `INSERT` en `supabase_migrations.schema_migrations`, generados por la propia CLI (`migration repair`) en una base desechable. | Incluido en el anterior |

El archivo de despliegue **no se edita a mano**. Se regenera con `python scripts/build_sql_editor_deploy.py` y `tests/test_sql_editor_deploy.py` comprueba que coincide con las migraciones actuales.

## 2. Estructura real del historial de la CLI (verificada)

Creada por la propia CLI en una base desechable al aplicar 1–15:

| Columna | Tipo | Nulo |
|---|---|---|
| `version` | `text` | No (clave primaria) |
| `statements` | `text[]` | Sí |
| `name` | `text` | Sí |

- `version`: marca de tiempo de 14 dígitos, sin extensión.
- `name`: nombre del archivo sin `.sql` y sin la marca de tiempo.
- `statements`: arreglo de sentencias calculado por la CLI a partir del archivo.

No hay valores por defecto ni columnas adicionales. El script comprueba esta estructura exacta y aborta si difiere.

## 3. Procedimiento (solo tras GO)

1. **Confirmar la base.** Abrir el SQL Editor desde `supabase.com/dashboard/project/vpnnripzdfdwjexkgdnk`. La URL debe contener ese identificador. Ejecutar `00-diagnostico-solo-lectura.sql` completo.
2. **Revisar el diagnóstico** con la lista de la sección 5. Si algo no coincide, **detenerse**.
3. **Detener el worker** y los envíos de WhatsApp. Confirmar que no hay procesos activos.
4. **Respaldo nuevo** verificado con `scripts/verify_cloud_backup.ps1` y cifrado con `scripts/protect_backup.ps1`. Guardar el resultado antes de continuar.
5. **Pegar y ejecutar** `despliegue-16-18-sql-editor.sql` completo en el SQL Editor. Es una sola transacción (`begin` … `commit`).
6. **Leer la salida.**
   - Si termina sin error, la transacción quedó confirmada.
   - Si aparece cualquier error, ejecutar `rollback;` y **no** seguir. El error indica qué comprobación falló (`PRE:` o `POST:`).
7. **Verificación posterior** con la CLI, solo si el acceso administrativo está disponible:
   ```powershell
   npx supabase migration list --remote    # 1–18 aplicadas en local y remoto
   npx supabase db push --dry-run --remote # upToDate: true
   ```
   Sin acceso, verificar con el diagnóstico de solo lectura (paso 1) y dejar constancia de que la comprobación con la CLI queda pendiente.
8. **Reactivar el worker** solo después de confirmar los conteos.

## 4. Qué hace y qué no hace el script

- **Comprobaciones previas (`PRE`):** base esperada, PostgreSQL 17, estructura del historial, historial exactamente 1–15, ausencia de 16/17/18, ausencia de `public.schema_migrations`, 349 productos y claves de la 18 en `SET NULL`. Guarda una huella de conteos.
- **Migraciones:** 16, 17 y 18 íntegras. Ninguna sentencia de datos de negocio.
- **Historial:** tres filas nuevas en la misma transacción. Si el despliegue falla, las filas también se revierten.
- **Comprobaciones posteriores (`POST`):** 21 políticas, ninguna para `anon`, `authenticated` ni `public`; restricción de la 17; claves de la 18 en `RESTRICT`; historial exactamente 1–18 con sentencias registradas; conteos de datos idénticos a la huella.
- **No hace:** `db reset`, `db push`, `migration repair`, restauraciones, `DELETE` ni `UPDATE` sobre datos, ni crea `public.schema_migrations`.

## 5. Criterios GO / NO-GO

**GO** solo si se cumplen **todos**:

1. El diagnóstico muestra: base `postgres`, historial con exactamente 1–15 y la estructura de la sección 2, tabla de la 17 ausente, 0 políticas nuevas, claves de la 18 en `n`, 349 productos.
2. Existe un respaldo nuevo cifrado y verificado, fuera del equipo de trabajo.
3. El worker y los envíos están detenidos.
4. Tú, como propietario, autorizas por escrito el registro del historial (incluido en el script).
5. Las pruebas de la sección 6 siguen en verde en el commit que se despliega.

**NO-GO** si:
- Cualquier comprobación del diagnóstico no coincide.
- Falta el respaldo verificado o la clave de recuperación no está fuera del equipo.
- El worker o los envíos no están detenidos.
- Se detecta una divergencia de historial que no se explica.

## 6. Evidencia de validación (base desechable)

Todas las pruebas usaron PostgreSQL 17 de la imagen Supabase y el historial creado por la CLI. Ninguna tocó Cloud.

| Caso | Resultado |
|---|---|
| Control: despliegue completo sobre base con 1–15 y 349 productos | Código 0. Historial 18 filas. 21 políticas. Claves en `r`. 349 productos. |
| CLI `db push --dry-run` tras el despliegue | `upToDate: true` |
| CLI `migration list` tras el despliegue | 1–18 presentes |
| A: error dentro de la migración 18 (nombre de clave erróneo) | Código de error 3, en la línea de la 18. Estado final idéntico al inicial: historial 15, 0 políticas, claves `n`, sin tabla de la 17. |
| B: fallo de la comprobación posterior tras todas las sentencias | `POST: se esperaban 21 políticas RLS`. Revertido completo: historial 15, 0 políticas, claves `n`, sin tabla de la 17. |
| C: historial sin la versión 15 | `PRE: el historial no contiene exactamente las versiones 1–15`. Sin cambios. |
| D: segunda ejecución sobre base ya desplegada | Abortado en `PRE`. Sin cambios: historial 18, 21 políticas, claves `rrr`. |
| Prueba estática `tests/test_sql_editor_deploy.py` | Ver resultado en la sección 7 |

## 7. Limitaciones

- **Identidad del proyecto:** el SQL no puede comprobar el identificador del proyecto. La protección es el nombre de la base, la versión de PostgreSQL, la estructura del historial y los conteos. El operador debe confirmar la URL del dashboard.
- **Historial registrado a mano:** las filas las escribe el SQL Editor, no la CLI. Son idénticas a las que genera la CLI en la prueba, pero la CLI no ha sido quien las insertó en producción. Verificar después con `migration list`.
- **Bloqueo breve:** las claves foráneas y las políticas toman bloqueos de tabla durante la transacción.
- **Privacidad:** el borrado de prospectos con historial comercial sigue bloqueado por `lead_id RESTRICT` (migración 15). Este despliegue no lo cambia. La solución de supresión o anonimización queda para una fase independiente. Este documento no atribuye conformidad legal a ningún procedimiento.
- **Datos de producción:** la validación usó 349 productos sintéticos, no el catálogo real. Las comprobaciones de catálogo de producción dependen del diagnóstico.
