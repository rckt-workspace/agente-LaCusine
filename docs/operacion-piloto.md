# Operación para el piloto controlado — procedimiento (Fase 6, preparación)

Estado: **procedimiento preparado. No ejecutado.** Ningún servicio Cloud se ha creado, ninguna migración se ha aplicado a Supabase, ningún PR se ha fusionado y ninguna variable de Render se ha definido.

Este documento reúne los pasos para llevar el sistema desde los tres PR actuales hasta una primera prueba real controlada. Cada paso indica si **puede ejecutarse ahora** o si **requiere autorización expresa**.

Documentos de referencia: [go-live-checklist.md](go-live-checklist.md), [render-deployment.md](render-deployment.md), [supabase-deployment.md](supabase-deployment.md), [environment-variables.md](environment-variables.md), [whatsapp-integration.md](whatsapp-integration.md), [catalogo-operacion.md](catalogo-operacion.md), [auditoria-fase-5-1.md](auditoria-fase-5-1.md).

---

## 1. Integración Git

### Estado comprobado

| Rama | Base | Commits sobre su base | PR |
|---|---|---|---|
| `feat/phase-4-reliable-worker` | `main` (`45b243b`) | 4 | #1, borrador, abierto |
| `feat/phase-5-whatsapp-integration` | Fase 4 | 4 | #2, borrador, abierto |
| `fix/phase-5-1-preproduction-hardening` (`1d42038`) | Fase 5 | 7 | #3, borrador, abierto |

- La cadena de ascendencia es correcta: `main` ← Fase 4 ← Fase 5 ← 5.1.
- Simulación en memoria con `git merge-tree`: fusionar en secuencia no produce conflictos en ninguno de los tres pasos.
- Las ramas no tienen protección configurada (`Branch not protected`). Los tres PR están en estado `CLEAN` y `MERGEABLE`.
- El repositorio permite merge commit, squash y rebase. `delete_branch_on_merge` está desactivado: las ramas se conservan.
- **No hay CI de pruebas en GitHub.** Solo existen Dependency Graph y CodeQL. La compuerta de calidad es la regresión local.
- **CodeQL** falló el 27/10/2026 sobre un SHA antiguo (`2548f52`). No he determinado la causa. Antes de la fusión hay que comprobar su estado en el SHA de `main` que se vaya a usar.

### Estrategia recomendada: merge commit, en secuencia

| Motivo | Consecuencia |
|---|---|
| Preserva la ascendencia de los commits apilados | Los PR posteriores no reintroducen commits |
| Squash crearía un commit nuevo sin relación con las ramas apiladas | PR #2 y #3 mostrarían cambios duplicados o conflictos |
| Rebase reescribe los SHA de las ramas | Exige force push: **prohibido** en este proyecto |

### Secuencia exacta (requiere aprobación)

1. **Verificar** que `origin/fix/phase-5-1-preproduction-hardening` está en `1d42038` y que la regresión local pasa en ese SHA.
2. **PR #1 → `main`:** fusionar con merge commit. Anotar el SHA del merge commit.
3. **PR #2:** cambiar la base a `main` antes de fusionar (`gh pr edit 2 --base main`). Confirmar que la lista de archivos del PR contiene solo cambios de la Fase 5. Fusionar con merge commit.
4. **PR #3:** cambiar la base a `main` (`gh pr edit 3 --base main`). Confirmar que solo contiene cambios de la 5.1. Fusionar con merge commit.
5. **Verificar `main`:** hacer checkout del SHA final en un directorio limpio y ejecutar la regresión completa, incluida la integración contra PostgreSQL local.
6. **No borrar las ramas** de Fase 4, 5 y 5.1: son trazabilidad.

Tras cada fusión, comprobar `git log --graph origin/main` y que los archivos del siguiente PR no han cambiado de forma inesperada.

### Comprobaciones pendientes antes de fusionar

- Estado de CodeQL en el SHA de `main` de destino.
- Revisión humana del PR #3, con el resultado de la sección 4 de este documento.
- Autorización escrita de quien fusiona. Ninguna fusión se hace automáticamente.

---

## 2. Supabase de pruebas (proyecto aislado)

**Requiere autorización.** Crear el proyecto y ejecutar cualquier comando contra él es una acción sobre un servicio externo con posible coste.

### 2.1 Crear el proyecto

- Crear un proyecto nuevo, **sin datos reales**, claramente nombrado como de prueba.
- Anotar la región y el plan. Verificar el coste en el panel de Supabase: este repositorio no declara precios.
- No reutilizar el proyecto de producción ni copiar datos de él.

### 2.2 Aplicar las migraciones en orden

Cada archivo es transaccional por sí mismo. Aplicarlos **uno por uno, en orden lexicográfico**, con el rol propietario del proyecto de prueba:

```bash
# Conexión directa o pooler de sesión (puerto 5432). Nunca el modo transacción (6543) para DDL.
for f in $(ls supabase/migrations/*.sql | sort); do
  echo "== $f"
  psql "$SUPABASE_TEST_OWNER_URL" -v ON_ERROR_STOP=1 -1 -f "$f" || break
done
```

- `-1` ejecuta cada archivo en una transacción. `ON_ERROR_STOP=1` detiene la secuencia al primer error.
- La URL del propietario se pasa por variable de entorno local. **No se escribe en el repositorio ni en la historia de comandos compartida.**
- Orden esperado: 1 `core_schema`, 2 `rls_and_grants`, 3 `retention`, 4 `products_pricing_search`, 5 `llm_usage_events`, 6 `handoff_reason_return`, 7 `worker_queue`, 8 `retention_worker_safe`, 9 `whatsapp_delivery`, 10 `phase_5_1_hardening`, 11 `app_roles_rls_policies`.
- Si un archivo falla, **no reintentar a ciegas**: la transacción ya se revirtió, pero el estado de las anteriores sí queda aplicado. Diagnosticar y decidir.

### 2.3 Crear los roles de login (fuera del repositorio)

Los grupos `lacusine_backend` y `lacusine_admin_ops` existen sin login. Los roles de login se crean con el propietario, con contraseñas generadas fuera del repositorio y guardadas en un gestor de contraseñas:

```sql
create role lacusine_app login password '<generada-fuera-del-repositorio>';
grant lacusine_backend to lacusine_app;

create role lacusine_admin login password '<generada-fuera-del-repositorio>';
grant lacusine_admin_ops to lacusine_admin;
```

- `lacusine_app` es la identidad **cotidiana** de API y worker (`DATABASE_URL`).
- `lacusine_admin` es la identidad de la CLI de conciliación (`LACUSINE_ADMIN_DATABASE_URL`), usada solo de forma puntual.
- **Ninguna** de las dos debe ser `postgres` ni `service_role`. El propietario se usa solo para migraciones y para crear roles.

### 2.4 Comprobación de RLS y privilegios (solo lectura)

Ejecutar con el propietario, sin modificar nada:

```sql
-- RLS activa en todas las tablas de negocio
select relname, relrowsecurity from pg_class
where relnamespace = 'public'::regnamespace and relkind = 'r' order by relname;

-- Ningún rol de login con superusuario ni BYPASSRLS
select rolname, rolsuper, rolbypassrls from pg_roles
where rolname in ('lacusine_app', 'lacusine_admin');

-- anon y authenticated sin acceso a datos privados
select has_table_privilege('anon', 'public.leads', 'select') as anon_leads,
       has_table_privilege('authenticated', 'public.leads', 'select') as auth_leads,
       has_table_privilege('anon', 'public.admin_audit_log', 'select') as anon_audit;

-- Ninguna política dirigida a public, anon o authenticated
select tablename, policyname from pg_policies
where schemaname = 'public' and (roles::text[] && array['anon', 'authenticated', 'public']);
```

Resultado esperado: todas las tablas con RLS, `false` en superusuario y BYPASSRLS, `false` en los tres privilegios, y **ninguna** fila en la última consulta.

**Limitación:** en Supabase, el rol `postgres` puede tener atributos propios. Esta comprobación debe repetirse **con conexiones reales** de `lacusine_app` y `lacusine_admin`, no solo consultando el catálogo como propietario. Los privilegios se han probado en PostgreSQL local, no en Supabase.

### 2.5 Verificación de conexión desde FastAPI y worker

- **Web:** `GET /ready` debe responder `200 {"status": "ready"}` con `DATABASE_URL` del rol `lacusine_app`. Sin base responde `503` sin motivo.
- **Worker:** con `WORKER_ENABLED=false` el proceso debe quedar en espera sin conectarse. Para probar la conexión, usar una réplica local apuntando al proyecto de prueba, **no** el servicio de Render, y con `META_SEND_ENABLED=false` y `ANTHROPIC_ENABLED=false`.
- **No usar** el guard de pruebas destructivas contra Supabase: rechaza hosts de Supabase por diseño. Las pruebas de integración son solo para PostgreSQL local.

### 2.6 Escritura no destructiva de prueba

Con el rol `lacusine_app`, insertar un evento sintético identificable y borrarlo. La aplicación tiene `delete` solo sobre `webhook_events`, `outbound_messages` y `contact_leases`:

```sql
insert into public.webhook_events (external_event_id, contact_wa_id, payload, payload_sha256, expires_at, business_phone_id)
values ('probe-fase6-' || gen_random_uuid(), '5700000000000', '{}'::jsonb, repeat('0', 64), now() + interval '1 hour', '000000000000')
returning id;
delete from public.webhook_events where external_event_id like 'probe-fase6-%';
```

### 2.7 Validación del catálogo desde archivo privado

Ver [catalogo-operacion.md](catalogo-operacion.md). Resumen:

1. **Ensayo en seco, sin conexión a base (puede ejecutarse ahora, localmente):**
   `python -m app.services.catalog_xlsx --file <ruta-privada> --extracted-on <fecha>`. No escribe.
2. **Importación al proyecto de prueba (requiere autorización):** misma orden con `--apply` y `DATABASE_URL` del rol `lacusine_app`. Sin `--allow-partial`.
3. Comprobar contadores con consulta de solo lectura. Una segunda importación debe mostrar `unchanged`.
4. **Nunca** importar a producción sin una decisión separada.

---

## 3. Render

**Requiere autorización.** Crear servicios puede generar coste.

### 3.1 Servicios necesarios

| Servicio | Tipo | Comando de arranque | Health check | Qué hace |
|---|---|---|---|---|
| `lacusine-api` | Web Service | `uvicorn app.main:app --host 0.0.0.0 --port $PORT` | `/health` | Recibe el webhook de Meta, valida firma, persiste el evento |
| `lacusine-worker` | Background Worker | `python -m app.worker.main` | No aplica | Procesa la cola, el agente, el outbox y el dispatcher |

- **Python:** 3.11.9 (`PYTHON_VERSION` en `render.yaml`). Coincide con la versión usada localmente.
- **Dependencias:** `pip install -r requirements.txt`. Son solo las de ejecución; `requirements-dev.txt` no se instala en Render.
- **Supabase:** no es un servicio de Render. Se referencia por `DATABASE_URL`.
- **Plan:** `starter` en ambos, como valor de partida. **Validar el plan y el precio en el panel antes de crear.** El plan gratuito suspende el servicio por inactividad y no es válido para webhooks.
- **Auto-deploy:** `autoDeployTrigger: off` en ambos. Los despliegues son manuales sobre un commit concreto.

### 3.2 Procedimiento

1. Confirmar el commit a desplegar. Debe ser el SHA de `main` tras la fusión, o el de la rama aprobada.
2. En el panel de Render, crear el Blueprint desde `render.yaml` apuntando a esa rama o commit.
3. Introducir **directamente en el panel** los valores marcados `sync: false` (sección 4). No en GitHub, no en un archivo.
4. Verificar que el worker arranca en **espera**: log `worker_standby consuming=false` y ningún `job_finished`.
5. Verificar que el web responde `200` en `/health`.
6. Verificar `/ready`: `200` solo cuando `DATABASE_URL` apunta al proyecto de prueba y el rol es `lacusine_app`.
7. **No activar** `WORKER_ENABLED` hasta la sección 7.

### 3.3 Comprobaciones de arranque ya realizadas localmente

| Comprobación | Resultado |
|---|---|
| `uvicorn app.main:app` sin variables | Arranca; `/health` 200 |
| `/ready` sin base | 503, sin motivo |
| Worker sin `WORKER_ENABLED` | Queda en espera; probado con proceso real |
| Worker con `WORKER_ENABLED=true` sin base o sin `META_PHONE_NUMBER_ID` | Sale con código 2 |
| Cierre con SIGTERM | **No concluyente en Windows** (código 143). Comprobar en Linux antes de producción |

---

## 4. Variables de entorno por servicio (solo nombres)

Los valores reales **no** aparecen en este documento. Columna "Origen": `panel` significa definir en el panel de Render, marcada como secreta (`sync: false`). `valor` significa literal en `render.yaml`.

### 4.1 Web (`lacusine-api`)

| Variable | Origen | Estado inicial |
|---|---|---|
| `PYTHON_VERSION` | valor | `3.11.9` |
| `META_APP_SECRET` | panel | secreto |
| `META_VERIFY_TOKEN` | panel | secreto |
| `DATABASE_URL` | panel | secreto. Rol `lacusine_app` |
| `META_PHONE_NUMBER_ID` | panel | identificador de la línea. **Obligatoria** para aislar la línea en el webhook |
| `META_GRAPH_API_VERSION` | valor | `v26.0` |
| `META_SEND_ENABLED` | valor | `false` |
| `WHATSAPP_DRY_RUN` | valor | `true` |
| `ANTHROPIC_ENABLED` | valor | `false` |
| `AGENT_DRY_RUN` | valor | `true` |

### 4.2 Worker (`lacusine-worker`)

| Variable | Origen | Estado inicial |
|---|---|---|
| `PYTHON_VERSION` | valor | `3.11.9` |
| `WORKER_ENABLED` | valor | `false` |
| `DATABASE_URL` | panel | secreto. Rol `lacusine_app` |
| `META_ACCESS_TOKEN` | panel | secreto. Solo para envío |
| `META_PHONE_NUMBER_ID` | panel | obligatoria cuando `WORKER_ENABLED=true` |
| `WHATSAPP_ALLOWED_RECIPIENTS` | panel | secreto. Lista de destinatarios autorizados |
| `META_GRAPH_API_VERSION` | valor | `v26.0` |
| `META_SEND_ENABLED` | valor | `false` |
| `WHATSAPP_DRY_RUN` | valor | `true` |
| `ANTHROPIC_API_KEY` | panel | secreto. Vacía mientras no se autorice Claude |
| `ANTHROPIC_ENABLED` | valor | `false` |
| `AGENT_DRY_RUN` | valor | `true` |

### 4.3 Solo para la fase de piloto (no definir ahora)

| Variable | Dónde | Motivo |
|---|---|---|
| `ANTHROPIC_MODEL` | worker | Modelo aprobado. Candidato verificado el 28/10/2026: `claude-haiku-5-5` |
| `ANTHROPIC_PRICE_INPUT_USD_PER_MTOK`, `_OUTPUT_`, `_CACHE_WRITE_`, `_CACHE_READ_` | worker | Tarifas. Tomarlas de la página oficial en la fecha del presupuesto |
| `ANTHROPIC_PRICING_REFERENCE` | worker | Texto que identifica la fuente de las tarifas |
| `ANTHROPIC_MAX_OUTPUT_TOKENS`, `ANTHROPIC_TIMEOUT_SECONDS`, `ANTHROPIC_MAX_RETRIES`, `ANTHROPIC_MAX_CONCURRENCY` | worker | Límites de cada llamada |
| `AGENT_DAILY_BUDGET_USD`, `AGENT_MONTHLY_BUDGET_USD`, `AGENT_INTERACTION_LIMIT_USD` | worker | Presupuestos aprobados por un responsable |
| `AGENT_KILL_SWITCH` | worker | Interruptor de emergencia. Probado en pruebas unitarias |
| `WORKER_LEASE_SECONDS`, `WORKER_MAX_ATTEMPTS`, etc. | worker | Ajustes operativos. Los valores por defecto son válidos |

### 4.4 Nunca en Render

- `LACUSINE_ADMIN_DATABASE_URL`, `LACUSINE_ADMIN_ROLE`, `LACUSINE_ADMIN_ENABLED`, `LACUSINE_ENVIRONMENT`, `LACUSINE_ADMIN_ALLOW_*`: la CLI de conciliación se ejecuta de forma puntual y local con el rol `lacusine_admin`. No es un servicio.
- `TEST_DATABASE_URL`, `LACUSINE_TEST_PG_PASSWORD`, `APP_ENV=test`: solo pruebas locales.
- `LACUSINE_LOAD_DOTENV`: nunca en Render. El `.env` local no se copia a la imagen (`.dockerignore`).

### 4.5 Reglas

- `.env` local se excluye por `.gitignore`. `.env.example` solo contiene referencias sin valores reales.
- Ningún secreto aparece en `render.yaml`, en el repositorio ni en la documentación.
- Un secreto comprometido se rota en el proveedor (Meta, Anthropic, Supabase) y luego se actualiza en el panel de Render.

---

## 5. Recursos facturables y aprobaciones

| Recurso | Cuándo se factura | Aprobación necesaria |
|---|---|---|
| Render Web Service (plan `starter`) | Desde que se crea, aunque no reciba tráfico | Sí: responsable de la cuenta |
| Render Background Worker (plan `starter`) | Desde que se crea, **también en espera** | Sí. Mantenerlo detenido si no se usa |
| Supabase (proyecto de prueba y producción) | Según plan y uso | Sí. Verificar precio del plan |
| Meta, mensajes de WhatsApp | Por conversación, según tipo y mercado | Sí. Verificar tarifas oficiales. No declaradas en el repositorio |
| Anthropic (tokens) | Por token en cada llamada | Sí. Solo con `ANTHROPIC_ENABLED=true` y `AGENT_DRY_RUN=false` |

**Límite de coste en el piloto:** presupuestos diario, mensual y por interacción definidos antes de activar Claude. Sin tarifas en el entorno, cualquier llamada facturable queda bloqueada.

---

## 6. Primer mensaje de WhatsApp de prueba

**Requiere autorización escrita del responsable de la cuenta de Meta y del equipo.** Ningún paso de esta sección se ejecuta sin ella.

### 6.1 Prerrequisitos

1. Número de prueba de Meta autorizado por escrito. No se usa el número comercial de La Cuisine para la primera prueba.
2. Aplicación de Meta con permisos verificados y webhook suscrito al campo `messages`.
3. Webhook HTTPS público verificado con el handshake de Meta: `GET /webhook` con `hub.mode=subscribe`, `hub.verify_token` correcto y `hub.challenge`. Un token incorrecto devuelve `403`.
4. `META_PHONE_NUMBER_ID` del número de prueba en web y worker. Coincide con el de la cabecera del evento.
5. Un solo destinatario de prueba en `WHATSAPP_ALLOWED_RECIPIENTS`, el número del equipo que envía el mensaje.
6. Presupuesto de Claude en cero: `ANTHROPIC_ENABLED=false` y `AGENT_DRY_RUN=true`. **La primera prueba no consume tokens.** La respuesta la genera el proveedor simulado.

### 6.2 Cambios de interruptores (solo tras autorización)

En el orden siguiente, cada cambio en el panel de Render:

1. `WORKER_ENABLED=true`. Antes, comprobar en logs que no hay errores de configuración.
2. `META_SEND_ENABLED=true` y `WHATSAPP_DRY_RUN=false`. Ambos son necesarios. Sin uno de los dos, no sale ninguna solicitud.

Estos cambios se hacen juntos en una ventana acordada, nunca con tráfico real de clientes.

### 6.3 Secuencia de prueba

1. Enviar desde el número de prueba un mensaje de texto al número de la línea.
2. **Web:** log `inbound_message_accepted`, y en la base `webhook_events` con el evento. Verificar que `messages.window_trusted = true`. Si aparece `webhook_foreign_business_line_ignored`, el número no es el de la línea configurada: parar.
3. **Worker:** `job_finished` con `outcome=processed`. Después, `whatsapp_send_accepted` en logs. Una fila en `outbound_messages` con `kind = reply` y `delivery_status = sent`, con `external_message_id` que empieza por `wamid.`.
4. **Meta:** el mensaje llega al teléfono de prueba.
5. **Estado de entrega:** Meta envía un webhook `delivered`. El estado de la salida pasa a `delivered` con `delivered_at` relleno.
6. **Sin llamadas a Claude:** `llm_usage_events` sin filas nuevas.
7. **Sin texto en logs:** revisar los logs y confirmar que no aparece el texto del cliente ni del modelo.
8. **Ventana:** la hora de Meta del mensaje debe coincidir con la hora de recepción, con tolerancia de unos segundos. Esto confirma la unidad del `timestamp`. Si no coincide, **parar** y revisar la conversión (ver `whatsapp-integration.md`).

### 6.4 Criterios de parada inmediata

- Cualquier salida en `uncertain` sin resolver.
- Un error 401 o 403 de Meta.
- Un mensaje enviado a un número fuera de la lista.
- Una respuesta enviada con la línea distinta de la de origen.
- Cualquier llamada a Claude no esperada.

---

## 7. Plan de rollback

Los cambios de configuración se revierten en minutos. Los cambios de base de datos son hacia adelante y se corrigen con una migración nueva o restauración.

| Nivel | Acción | Efecto | Tiempo |
|---|---|---|---|
| 1 | `META_SEND_ENABLED=false` y `WHATSAPP_DRY_RUN=true` | Ninguna solicitud sale a Meta | Un cambio en el panel |
| 2 | `AGENT_KILL_SWITCH=true` | El agente se detiene sin llamadas | Un cambio en el panel |
| 3 | `WORKER_ENABLED=false` | El worker queda en espera y no consume la cola | Un cambio en el panel |
| 4 | Redesplegar el commit anterior conocido (manual) | Vuelve el código previo | Minutos |
| 5 | Pausar una conversación con una transferencia humana | Esa conversación deja de recibir respuestas automáticas | Por conversación |
| 6 | Restaurar la base desde respaldo | Solo si hay corrupción de datos | Horas. Requiere decisión |

**Envíos inciertos:** no se reenvían. Se resuelven con la CLI de conciliación (`python -m app.admin.cli`), solo con evidencia externa del panel de Meta.

**Secretos:** si se sospecha una filtración, rotar en el proveedor y actualizar el panel de Render. No se reutilizan contraseñas.

**Migraciones:** no se revierten automáticamente. Un cambio malo se corrige con una migración nueva.

---

## 8. Criterios GO / NO-GO del piloto

### GO: solo si se cumplen todos

- [ ] Los PR #1, #2 y #3 fusionados en secuencia, con merge commit, y `main` verificado con la regresión.
- [ ] CodeQL revisado en el SHA de destino.
- [ ] Migraciones 1 a 11 aplicadas en Supabase de prueba sin error.
- [ ] Comprobación de RLS y privilegios de la sección 2.4 con conexiones reales de `lacusine_app` y `lacusine_admin`.
- [ ] `/ready` 200 con el rol `lacusine_app`.
- [ ] Render: auto-deploy off; worker en espera en logs; web en `/health` 200.
- [ ] Todas las variables de la sección 4.1 y 4.2 definidas en el panel, sin valores en Git.
- [ ] Número de prueba autorizado por escrito; handshake de Meta verificado.
- [ ] Presupuestos y tarifas de Claude definidos, aunque Claude quede desactivado en la primera prueba.
- [ ] Texto de transferencia y reglas de precio validados por el equipo.
- [ ] Responsable nombrado para observar los primeros envíos y para la conciliación.
- [ ] Plan de rollback ensayado al menos en nivel 1 y nivel 3.

### NO-GO: basta con uno

- Cualquier prueba fallida en el commit que se va a desplegar.
- RLS o privilegios distintos de los de la sección 2.4.
- Un rol de la aplicación con `SUPERUSER` o `BYPASSRLS`, o la aplicación conectada como `postgres`.
- Auto-deploy activo.
- Un secreto en Git o en el repositorio.
- `META_PHONE_NUMBER_ID` ausente en web o en worker.
- La unidad del `timestamp` de Meta no verificada.
- Un envío `uncertain` sin resolver.
- Coste del worker o de la base no aprobado.

---

## 9. Qué puede ejecutarse ahora y qué no

### Puede ejecutarse ahora (local, sin coste, sin servicio externo)

- La regresión completa sobre PostgreSQL local protegido por el guard.
- El ensayo en seco del catálogo con un archivo privado local (sin `--apply`).
- Las comprobaciones de arranque local (`uvicorn` y worker en espera).
- Revisión de `render.yaml` y de las variables documentadas.
- Preparación de la fusión: comprobar la secuencia con `git merge-tree`, sin modificar ramas.

### Requiere autorización expresa

- Fusionar cualquier PR, o cambiar la base de un PR.
- Crear el proyecto Supabase de prueba, o cualquier comando contra él.
- Aplicar migraciones en Supabase de prueba o de producción.
- Crear roles de login y sus contraseñas.
- Crear servicios en Render, o definir variables en su panel.
- Activar `WORKER_ENABLED`, `META_SEND_ENABLED`, `WHATSAPP_DRY_RUN=false` o `ANTHROPIC_ENABLED`.
- Conectar el número de prueba de Meta, cambiar cualquier activo de Meta, o enviar cualquier mensaje.
- Llamar a Anthropic, con cualquier clave.
- Importar el catálogo a una base que no sea local.

---

## 10. Estado de este documento

- Verificado en el repositorio: nombres de variables, comandos de arranque, orden de migraciones y estado de los PR.
- No verificado: Supabase Cloud, Render, Meta, Anthropic, tarifas de ningún proveedor, el pooler en modo transacción y el cierre con SIGTERM en Linux.
