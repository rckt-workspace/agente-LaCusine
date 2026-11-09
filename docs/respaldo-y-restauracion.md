# Respaldo, restauración, cifrado y migraciones de Supabase (La Cuisine)

Estado: **restauración verificada; cifrado verificado; migraciones 16, 17 y 18 listas en local; despliegue NO autorizado**.
Ninguna escritura se ha hecho sobre Supabase Cloud (proyecto `vpnnripzdfdwjexkgdnk`).

## 1. Causa raíz del fallo de restauración

El `FATAL: terminating connection due to administrator command` en `roles.sql` **no** procede del archivo. Se reprodujo con `public.ecr.aws/supabase/postgres:17.11.0.011`:

| # | Causa | Evidencia | Corrección |
|---|-------|-----------|------------|
| 1 | La sonda de disponibilidad conectó al **servidor temporal de inicialización**. La imagen arranca PostgreSQL, ejecuta sus scripts, lo **apaga** (`PostgreSQL init process complete`) y arranca el definitivo. | Logs: `server stopped` → `ready for start up` → segundo `ready to accept connections`. | Esperar `init process complete` seguido de `ready to accept connections`, y comprobar que `pg_postmaster_start_time()` no cambia en 3 consultas. |
| 2 | `roles.sql` usa `GRANTED BY "postgres"`. Como `supabase_admin` no puede, falla. | `permission denied to grant privileges as role "postgres"`. | Restaurar como `postgres`, el propietario en Cloud. |
| 3 | `data.sql` incluye `auth.*` y `storage.*`, cuyo esquema local no coincide con Cloud. | `column "ip_address" of relation "audit_log_entries" does not exist`. | Restaurar solo los bloques `COPY` de `public` (ver §3). |

## 2. Script de verificación: `scripts/verify_cloud_backup.ps1`

```powershell
.\scripts\verify_cloud_backup.ps1 -BackupDir "C:\Users\maura\SecureBackups\lacuisine\20261108-184212"
```

- Contenedor temporal propio (nombre aleatorio), `--network none`, sin `-p`. Se elimina al terminar.
- Contraseña aleatoria en archivo temporal que se borra de inmediato. Nunca lee `.env` ni se conecta a Cloud.
- `ON_ERROR_STOP=1` en cada paso. Compara conteos fila a fila y el número de tablas y políticas con `schema.sql`.
- Si existe `backup-manifest.sha256`, verifica los hashes antes de empezar.
- Ante fallo guarda evidencia **sanitizada** (solo `ERROR`/`FATAL`/`PANIC` y eventos del servidor; sin `DETAIL`, `CONTEXT` ni `LINE`).

## 3. Alcance real del respaldo

- **Incluido y verificado:** roles de la aplicación, esquema de `public` (tablas, índices, funciones, políticas RLS) y datos de `public`.
- **No incluido:** `auth.*` (usuarios, sesiones, MFA), `storage.*` (metadatos), **archivos binarios de Storage**, configuración del proyecto y secretos de funciones. Para estos casos el respaldo lógico **no protege** los datos; hace falta el PITR del plan de Supabase o una exportación desde el panel.

## 4. Cifrado: `scripts/protect_backup.ps1`

Sin contraseñas tecleadas ni secretos en el repositorio.

```
backup.enc              "LCBK1" | IV(16) | AES-256-CBC(ZIP con los 3 SQL + manifiesto) | HMAC-SHA256
keyslot-dpapi.bin       clave de 64 bytes protegida con DPAPI (CurrentUser)
keyslot-recovery.bin    la misma clave envuelta con la clave de recuperación (AES-256-CBC + HMAC)
info.txt                formato, fecha, sha256 de backup.enc, alcance (sin secretos)
```

La **clave de recuperación** (64 bytes, base64) se escribe en una ruta indicada con `-RecoveryKeyPath`, que debe estar **fuera del respaldo, de la salida y del repositorio**. Cada ranura descifra por sí sola.

Comandos usados:

```powershell
.\scripts\protect_backup.ps1 -Mode Encrypt -BackupDir "...\20261108-184212" -OutputDir "...\cifrados\20261108-184212" -RecoveryKeyPath "...\RECOVERY-lacuisine-20261108.txt"
.\scripts\protect_backup.ps1 -Mode Verify -OutputDir "...\cifrados\20261108-184212" -Slot dpapi
.\scripts\protect_backup.ps1 -Mode Verify -OutputDir "...\cifrados\20261108-184212" -Slot recovery -RecoveryKeyPath "...\RECOVERY-lacuisine-20261108.txt" -RestoreTo "<carpeta temporal>"
```

El script **nunca borra el original**. En modo `Encrypt` verifica en aislamiento con ambas ranuras y solo declara éxito si los tres hashes coinciden con el manifiesto.

Resultados:

| Prueba | Resultado |
|--------|-----------|
| Cifrado del respaldo real a una carpeta nueva | Verificado con ranura DPAPI y con ranura de recuperación (3 hashes OK) |
| Original tras el cifrado | Sin cambios (`backup-manifest.sha256`, `data.sql` 346 806 B, `roles.sql` 737 B, `schema.sql` 55 265 B) |
| Descifrado aislado con ranura de recuperación + restauración Docker del resultado | **VERIFICADO**: 18 tablas, conteos iguales, 24 políticas. Texto plano eliminado después. |
| Byte alterado en `backup.enc` | Rechazado: `el MAC no coincide` |
| Clave de recuperación de otro cifrado | Rechazada: `el MAC no coincide` |
| Carpeta de salida no vacía / ruta dentro del repositorio | Rechazadas |

**Limitaciones:**
- `keyslot-dpapi.bin` solo descifra con el mismo usuario de Windows en este equipo. Si se pierde el perfil o el equipo, **solo** queda la ranura de recuperación.
- La clave de recuperación está ahora en `C:\Users\maura\SecureBackups\RECOVERY-lacuisine-20261108.txt`, en el mismo disco que el respaldo. **Hay que moverla fuera del equipo** (memoria USB sin conexión, gestor de contraseñas de otro dispositivo) y luego borrar la copia local. No lo he hecho: requiere tu decisión sobre dónde custodiarla.
- El cifrado protege el archivo en reposo. No protege contra alguien que tenga la clave de recuperación y el archivo cifrado a la vez.

**Estrategia de copia fuera del equipo:**
1. Copiar `backup.enc`, `keyslot-recovery.bin` e `info.txt` a un almacenamiento externo (la clave DPAPI no sirve fuera de este perfil; no hace falta copiarla).
2. Guardar la clave de recuperación en un medio **distinto** y sin conexión.
3. Cada trimestre o antes de cada despliegue, repetir `-Mode Verify -Slot recovery` desde una copia de la clave, en una carpeta temporal.

## 5. Migraciones

### 5.1 Un solo mecanismo en Cloud

Supabase CLI es la fuente de verdad para Cloud. El runner Python (`app/db/migrate.py`) **no escribe** en un proyecto que tenga `supabase_migrations.schema_migrations`:

- `--apply` se rechaza con `supabase_cli_history_present` **antes** de crear nada.
- Si el estado **no puede determinarse** (la consulta falla o el resultado no es booleano), el runner se detiene con `cli_history_unknown`, tanto en ensayo como en aplicación, sin escribir.
- El dry-run es de solo lectura y reporta `"cli_managed"`.

Pruebas: `tests/db/test_migrate_guard_unit.py` (7 casos, sin base de datos: fallo seguro, estado no booleano, rechazo antes del bootstrap, dry-run) y `tests/integration/test_migrate_cli_guard_postgres.py` (2 casos).

### 5.2 Migración 17 corregida (no aplicada en Cloud)

`20261105130000_business_report_budget.sql` añade a `analysis_budget_reservations`:

```sql
constraint analysis_budget_released_zero check (status <> 'released' or counted_usd = 0)
```

Revisión previa del código: `release()` ya pone `counted_usd = 0` y `_counted()` excluye `released`, así que la regla no rompe el flujo real. Pruebas en `tests/integration/test_analysis_budget_constraints_postgres.py`: los 4 estados válidos se aceptan; `released` con importe se rechaza en `INSERT` y en `UPDATE`; el camino `reserve → release` de la aplicación satisface la restricción.

### 5.3 Migración 18 (nueva): `20261105140000_commercial_history_fk_restrict.sql`

Cambia tres claves foráneas de `ON DELETE SET NULL` a `ON DELETE RESTRICT`:

| Constraint | Tabla | Antes | Después |
|---|---|---|---|
| `commercial_events_conversation_id_fkey` | `commercial_events` | `n` | `r` |
| `commercial_events_product_id_fkey` | `commercial_events` | `n` | `r` |
| `sales_conversation_id_fkey` | `sales` | `n` | `r` |

- **No modifica** la migración 15 ni ninguna fila. `ALTER TABLE ... ADD CONSTRAINT` valida con `SELECT` y no dispara los triggers de `UPDATE`.
- Termina con una comprobación que hace fallar la migración si alguna clave no quedó en `RESTRICT`.
- **Prueba sobre datos existentes:** con las claves en su estado anterior y filas de historial, se aplicó la migración 18. El hash de `(id, conversation_id, product_id)` de `commercial_events` y `sales` fue **idéntico** antes y después (1 fila de prueba).
- **Prueba de error:** borrar una conversación con historial devuelve `foreign_key_violation (23503)`, no un intento de `UPDATE` sobre tablas inmutables.

**Análisis de privacidad (lo pediste explícitamente):**

- No encontré en `app/` ni en `scripts/` ningún borrado de `leads`, `conversations` ni `products`. La importación de catálogo no borra productos.
- `RESTRICT` no añade bloqueos nuevos al borrado de prospectos: todo evento o venta que apunta a una conversación también apunta a su lead, y `commercial_events.lead_id` y `sales.lead_id` ya son `RESTRICT`.
- **Contradicción preexistente, no resuelta por la migración 18:** `docs/modelo-datos.md` documenta la supresión de un prospecto como `DELETE FROM leads`, con cascada a conversaciones y mensajes. Desde la migración 15, ese borrado **falla si el prospecto tiene historial comercial**. Es decir, una solicitud de eliminación de datos personales no puede ejecutarse por el camino documentado cuando hay historial.

**Alternativa que preserva el historial y cumple la privacidad (no implementada; requiere decisión legal):**
En lugar de borrar el prospecto, una función de anonimización (`SECURITY DEFINER`, solo para el propietario) que:
1. Sustituye `leads.wa_contact_id` por un identificador sintético sin relación con el número real (manteniendo la restricción de formato).
2. Borra el contenido de `messages` y `webhook_events` asociados (el texto personal).
3. Conserva `commercial_events`, `sales` y `lead_stage_history` intactos, porque su `lead_id` sigue apuntando al prospecto anonimizado.

Así el historial comercial permanece, el número real y el texto desaparecen, y no hay que tocar los triggers de inmutabilidad. Requiere una migración futura y una decisión sobre el plazo de conservación con el responsable legal de La Cuisine.

### 5.4 Auditoría de migraciones 14–17 (resumen)

- **14:** correcta. Riesgo resuelto por la 16: `analytics_daily_counts` tiene RLS sin política hasta la 16, y la purga escribe en esa tabla con el rol de aplicación.
- **15:** defecto de `ON DELETE SET NULL` sobre tablas inmutables, corregido por la 18. Contradicción de privacidad preexistente, ver 5.3.
- **16:** correcta. 19 políticas, ningún permiso para `anon`, `authenticated` ni `public`.
- **17:** defecto de invariante, corregido en 5.2.

## 6. Pruebas (resultado exacto)

| Suite | Resultado |
|---|---|
| Suite completa sin base de datos (`pytest`, sin `TEST_DATABASE_URL`) | **1001 passed, 22 skipped** |
| Integración (`-m integration`) contra `postgres:16-alpine` en loopback, base `lacusine_test` | **164 passed, 22 deselected** |
| Runner sobre Supabase PG17 (imagen de producción), base desechable: 18 migraciones en orden | `applied` 18, código 0; segunda ejecución sin reaplicar; dry-run sin pendientes |
| Políticas RLS de las tablas de las migraciones 16 y 17 | 21 (19 + 2), como se esperaba |
| Borrado de conversación con historial sobre PG17 | `foreign_key_violation (23503)` |
| Restauración de respaldo (`verify_cloud_backup.ps1`) | Verificada (ver §2) |
| Cifrado y descifrado aislado (`protect_backup.ps1`) | Verificados (ver §4) |
| `git diff --check` | Sin errores |
| Escaneo de secretos en los archivos nuevos y modificados | Sin hallazgos |

Notas de entorno:
- La imagen Supabase no sirve para la integración: en ella `public` pertenece a `pg_database_owner` y las pruebas hacen `DROP SCHEMA public`. Por eso se usa `postgres:16-alpine`, que es lo que define `docker-compose.test.yml`.
- El puerto 55439 de `docker-compose.test.yml` cae en un rango excluido por Windows en este equipo. Se usó el 15439 con el mismo servicio.
- Hubo un fallo intermitente en pruebas del worker durante el desarrollo. Venía de una fixture mía que dejaba `public` sin los privilegios de Supabase. Corregido reutilizando `_reset_and_migrate`. Dos ejecuciones completas posteriores pasan.
- No hay linter configurado en el proyecto.

## 7. Estado de Supabase Cloud (no verificado en esta sesión)

`npx supabase migration list --remote` y `db push --dry-run --remote` devuelven **403** (`DbConfigLoginRoleStatusError`) desde esta sesión, en dos intentos. No he usado credenciales alternativas.

Lo que consta es lo que reportaste: 1–15 registradas, 16 y 17 pendientes. Con la migración 18 el dry-run **debe** mostrar tres pendientes: 16, 17 y 18.

### Comandos de solo lectura para ejecutar tú

```powershell
cd "C:\Users\maura\OneDrive\Documents\GitHub\agente-LaCusine"
npx supabase migration list --remote
npx supabase db push --dry-run --remote
```

Resultado esperado: en `migration list`, 1–15 con los dos lados rellenos (local y remoto) y 16, 17, 18 solo en local. En el dry-run, exactamente `20261105120000_commercial_analytics_rls_policies.sql`, `20261105130000_business_report_budget.sql` y `20261105140000_commercial_history_fk_restrict.sql`.

Si devuelve algo distinto, **no sigas**: pégame el resultado.

## 8. Riesgos restantes

1. **Contradicción de privacidad preexistente** (5.3): el borrado de un prospecto con historial está bloqueado. Requiere decisión sobre la alternativa de anonimización.
2. **Clave de recuperación en el mismo disco** que el respaldo (§4). Debe moverse fuera del equipo.
3. **Cloud no verificado** en esta sesión (403).
4. **El respaldo lógico no cubre** `auth`, `storage` (objetos) ni la configuración del proyecto (§3).
5. **Ninguna prueba cubre el borrado de un prospecto con historial** como flujo completo; solo que queda bloqueado.
6. **Ventana de bloqueo breve** en Cloud al añadir las claves foráneas (`ALTER TABLE` toma un bloqueo de tabla). Tablas pequeñas; hacerlo con el worker detenido.

## 9. Comandos del despliegue final (NO ejecutados)

Solo tras GO, en este orden:

1. Repetir los dos comandos de solo lectura de §7 y confirmar 1–15 aplicadas y 16, 17, 18 pendientes.
2. Detener el worker y los envíos de WhatsApp. Confirmar que no hay procesos activos.
3. Respaldo lógico nuevo, verificado con `verify_cloud_backup.ps1` y cifrado con `protect_backup.ps1`.
4. `npx supabase db push --remote` (aplica 16, 17 y 18).
5. Comprobaciones posteriores, solo lectura:
   - 21 políticas RLS sobre las tablas de las migraciones 16 y 17.
   - `analysis_budget_released_zero` presente.
   - Tres claves foráneas con `confdeltype = 'r'`.
   - Conteos: 349 productos, 118 eventos, 3 conversaciones.
6. Reactivar el worker.

No usar `db reset`, `db push --include-all`, `migration repair` ni restauraciones sobre producción.

## 10. Veredicto

**GO técnico en local; NO-GO para desplegar hoy** por dos motivos:
- Cloud no se pudo re-verificar desde esta sesión (403), y el despliegue exige ver el dry-run real con 16, 17 y 18.
- La contradicción de privacidad de §5.3 necesita una decisión tuya antes de tratar el borrado de prospectos como cubierto.

Condiciones para **GO**: dry-run con acceso válido que muestre 16, 17 y 18; decisión sobre la anonimización de prospectos; clave de recuperación fuera del equipo; respaldo cifrado nuevo justo antes del despliegue.
