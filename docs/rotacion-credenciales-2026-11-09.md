# Rotación de credenciales expuestas (2026-11-09)

**Estado: PENDIENTE.** Las credenciales listadas se tratan como comprometidas. Hasta rotarlas, **no se hacen llamadas reales** con ellas (ni al modelo, ni a Supabase, ni a Meta, ni a ngrok) salvo lecturas de diagnóstico.

Este documento no contiene valores. No pegue ningún secreto en chats, capturas, logs ni commits.

## 1. Inventario (nombres de variables en `.env`, sin valores)

| Variable | Proveedor / uso | Acción de rotación | Consumidores a actualizar |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | Anthropic (Claude) | Crear clave nueva en la consola de Anthropic; revocar la anterior | `.env` (worker). Otros entornos que la usen |
| `OPENROUTER_API_KEY` | OpenRouter | Crear clave nueva; revocar la anterior | `.env`. Hoy inactivo en el piloto |
| `SUPABASE_SECRET_KEY` | Supabase (clave secreta del proyecto) | Regenerar en Project Settings → API Keys | `.env` (FastAPI/worker si la usan) |
| `SUPABASE_PUBLISHABLE_KEY` | Supabase (clave pública) | Rotar si la política lo exige; no es secreta por diseño | `.env` |
| `DATABASE_URL` | PostgreSQL, usuario `lacusine_app` | **Contiene la contraseña.** Cambiar la contraseña del rol (ver §3) y reconstruir la URL | `.env`; cualquier proceso que la use |
| `LACUSINE_DB_OWNER_PASSWORD` | Contraseña del rol propietario (desarrollo) | Cambiar en la base; si no se usa, eliminar la variable | `.env` |
| `LACUSINE_DEV_APP_PASSWORD`, `LACUSINE_DEV_ADMIN_PASSWORD` | Roles de desarrollo local | Regenerar; si la base de desarrollo ya no existe, eliminar | `.env` |
| `META_ACCESS_TOKEN` | Meta Graph API (Cloud API, no usado en el piloto) | Regenerar el token del usuario de sistema, o revocarlo | `.env` |
| `META_APP_SECRET` | Meta (firma de webhooks) | Restablecer en la app de Meta | `.env` |
| `META_VERIFY_TOKEN` | Meta (verificación de webhook) | Cambiar en la configuración del webhook y en `.env` | `.env` |
| `NGROK_AUTHTOKEN` | ngrok | Regenerar en el panel de ngrok; cerrar túneles que usen el anterior | `.env`; procesos `ngrok` |
| `LACUSINE_LOCAL_APP_SECRET`, `LACUSINE_LOCAL_VERIFY_TOKEN` | Desarrollo local | Regenerar con un generador de secretos | `.env` |
| `WEB_BRIDGE_SECRET` (fuera de `.env`, en `connector\whatsapp-web\state\bridge-secret.txt`) | HMAC entre conector, FastAPI y worker | Borrar el archivo y arrancar de nuevo con el lanzador (genera uno nuevo) | Los tres procesos deben reiniciarse |

## 2. Orden recomendado

1. Parar todo lo que use las credenciales (worker, FastAPI, conector, ngrok). Conservar `.auth`.
2. Rotar en el panel de cada proveedor, empezando por Anthropic y la base de datos.
3. Actualizar `.env` **sin imprimir valores** (editor local). Mantener la copia de seguridad fuera del repositorio.
4. Verificar con `scripts\diagnostico_runtime.py` que los procesos leen la configuración nueva (solo presencia y longitud).
5. Reactivar. Nunca reutilizar una clave revocada.

## 3. Contraseña de PostgreSQL (`DATABASE_URL`)

Cambiar la contraseña del rol `lacusine_app` requiere ejecutar `ALTER ROLE ... PASSWORD` como propietario, en el SQL Editor del proyecto. La sentencia contiene la nueva contraseña: **no la escriba en el chat**. Después reconstruya `DATABASE_URL` en `.env` con el mismo formato y compruebe la conexión con `scripts\diagnostico_runtime.py`. Comprobar antes que ningún otro proceso (p. ej. `pilot_workers`) depende de la contraseña antigua.

## 4. Verificación posterior

- Anthropic: una llamada mínima desde el worker con el perfil `provider=anthropic`.
- Base de datos: `conexion_lectura=OK` en el diagnóstico.
- Meta/ngrok: se mantienen deshabilitados en el piloto web; rotar aunque no se usen.
- Ningún proceso antiguo conserva la credencial anterior (`Get-Process`, y reinicio de todos los servicios).

## 5. Riesgos aceptados mientras esté pendiente

- Cualquier persona con acceso al `.env` o a su historial puede usar las credenciales. Por eso el piloto **no** se activa antes de rotar.
- Las copias de seguridad de `.env` en `%LOCALAPPDATA%\LaCuisine\backups\` contienen las mismas credenciales: protegerlas o eliminarlas tras rotar.
