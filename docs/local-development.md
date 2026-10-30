# Desarrollo local — Modo A (Docker, sin servicios externos)

Estado: **verificado en esta máquina** con Docker Desktop. Ningún servicio externo se usa: ni Supabase, ni Meta, ni Anthropic, ni AWS.

## Qué levanta

```text
Docker Compose (red interna)
  ├── db            PostgreSQL 16 local, volumen persistente, expuesto solo en 127.0.0.1:55440
  ├── migrate       aplica las migraciones SQL una vez (explícito, nunca al arrancar la API o el worker)
  ├── dev-roles     crea lacusine_app y lacusine_admin (solo local) y los asocia a sus grupos
  ├── api           FastAPI en 127.0.0.1:18000 (configurable con LACUSINE_API_PORT)
  └── worker        proceso independiente; arranca en espera
```

Proveedores simulados por configuración: `META_SEND_ENABLED=false`, `WHATSAPP_DRY_RUN=true`, `ANTHROPIC_ENABLED=false`, `AGENT_DRY_RUN=true`, `ADVISOR_NOTIFIER=simulated`.

## Requisitos

- Docker Desktop con Compose v2.
- Python 3.11 en `.venv` del repositorio, solo para ejecutar las pruebas desde el host. Nunca instalar en el Python global.

## Primera vez: crear el archivo de secretos local

El archivo `.env` está ignorado por Git y **nunca** se copia a la imagen. Genera los valores aleatorios sin mostrarlos:

```powershell
.venv\Scripts\python.exe -c "import secrets; from pathlib import Path; p=Path('.env'); assert not p.exists(); p.write_text('\n'.join(['LACUSINE_DB_OWNER_PASSWORD='+secrets.token_urlsafe(24),'LACUSINE_DEV_APP_PASSWORD='+secrets.token_urlsafe(24),'LACUSINE_DEV_ADMIN_PASSWORD='+secrets.token_urlsafe(24),'LACUSINE_LOCAL_APP_SECRET='+secrets.token_urlsafe(32),'LACUSINE_LOCAL_VERIFY_TOKEN='+secrets.token_urlsafe(16),'WORKER_ENABLED=false'])+'\n', encoding='utf-8')"
```

Variables obligatorias (Compose se niega a arrancar si falta alguna): `LACUSINE_DB_OWNER_PASSWORD`, `LACUSINE_DEV_APP_PASSWORD`, `LACUSINE_DEV_ADMIN_PASSWORD`, `LACUSINE_LOCAL_APP_SECRET`, `LACUSINE_LOCAL_VERIFY_TOKEN`.

## Levantar el entorno

```powershell
docker compose up -d --build
docker compose ps
```

Orden esperado: `db` healthy → `migrate` termina con código 0 → `dev-roles` termina con código 0 → `api` healthy → `worker` en espera.

## Comprobaciones

```powershell
curl http://127.0.0.1:18000/health      # 200: liveness
curl http://127.0.0.1:18000/ready       # 200 {"status":"ready"}: base alcanzable con lacusine_app
docker compose logs worker              # debe contener worker_standby consuming=false
```

Si el puerto 18000 está ocupado: `LACUSINE_API_PORT=<otro>` en `.env`. El puerto 8000 puede estar ocupado por otros servicios locales.

## Activar el worker (solo en esta base local desechable)

Editar `WORKER_ENABLED=true` en `.env` y reiniciar el worker:

```powershell
docker compose up -d worker
```

Activar el worker **no** activa llamadas externas: el envío, Meta y Claude siguen deshabilitados.

## Flujo de prueba manual

1. Enviar un webhook firmado a `POST /webhook` con el secreto de `.env` (ver `tests/integration/test_phase6_postgres.py` para un ejemplo completo de firma).
2. Comprobar `public.webhook_events` (estado `processed`) y `public.leads` (atribución si el mensaje trae `referral`).
3. Abrir una transferencia desde un proceso del worker: `PostgresHandoffRepository.open(...)`. Crea un aviso en `public.advisor_notifications` y el worker lo marca `sent` mediante el canal simulado.
4. Un mensaje posterior del mismo cliente queda `ignored` (control humano), sin respuesta automática.

Consultas de solo lectura:

```powershell
docker compose exec -T db psql -U postgres -d lacusine_dev -c "select status from public.advisor_notifications;"
```

## Pruebas

Las pruebas de integración son **destructivas** (reinician el esquema). Solo se ejecutan contra PostgreSQL local desechable, protegido por el guard de `tests/integration/db_guard.py`. No usar la base `lacusine_dev` del Compose para pruebas: el guard la rechaza por nombre. Usar el contenedor de pruebas `docker-compose.test.yml`:

```powershell
docker compose -f docker-compose.test.yml up -d
$env:APP_ENV = "test"
$env:LACUSINE_TEST_DB_RESET = "I_UNDERSTAND_THIS_DROPS_PUBLIC_SCHEMA"
$env:TEST_DATABASE_URL = "postgresql://postgres:<contraseña-del-contenedor-de-prueba>@127.0.0.1:55439/lacusine_test"
.venv\Scripts\python.exe -m pytest -q tests
```

Sin `TEST_DATABASE_URL`, las pruebas de integración se omiten con su motivo.

## Parar y limpiar

```powershell
docker compose stop          # detiene sin borrar datos
docker compose down          # detiene y elimina contenedores; conserva el volumen de PostgreSQL
docker compose down -v       # elimina también la base local. Irreversible.
```

## Qué NO hace este entorno

- No se conecta a Supabase, Meta, Anthropic ni AWS.
- No envía mensajes a nadie. No registra ninguna llamada facturable.
- No importa el catálogo comercial. Si se requiere, usar `docs/catalogo-operacion.md`, con un archivo privado local y sin `--apply` por defecto.
- No sustituye la validación sobre Supabase licenciado, que está pendiente (ver `docs/supabase-deployment.md`, Modo B).

## Solución de problemas

| Síntoma | Causa probable | Acción |
|---|---|---|
| `db` no llega a healthy | Volumen con otra contraseña | `docker compose down -v` (borra la base local) y volver a levantar |
| `migrate` sale con error `migration_modified_after_apply` | Un archivo de `supabase/migrations` cambió después de aplicarse | No reescribir el historial. Crear una migración nueva |
| `dev-roles` sale con `dev_roles_*_refused` | El host o el nombre de la base no son locales | Revisar `DATABASE_URL` en `docker-compose.yml` |
| `api` no llega a healthy | Revisar `docker compose logs api` | Los logs no contienen secretos ni contenido de clientes |
