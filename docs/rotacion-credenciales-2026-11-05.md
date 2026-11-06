# Rotación de credenciales — pendiente del titular

Lista de variables que deben rotarse porque aparecieron en capturas o chats durante el piloto. **Este documento contiene solo nombres de variables, nunca valores.** La rotación la ejecuta el titular en cada panel; el repositorio no puede hacerla.

| Variable | Dónde se rota | Motivo |
|---|---|---|
| `META_ACCESS_TOKEN` | Meta Business Manager: usuario del sistema | Token expuesto en capturas |
| `META_APP_SECRET` | Meta for Developers: configuración de la aplicación | Firma del webhook |
| `META_VERIFY_TOKEN` | Meta: configuración de webhooks y `.env` local | Token de handshake |
| `NGROK_AUTHTOKEN` | Panel de ngrok | Acceso al túnel |
| `ANTHROPIC_API_KEY` | Consola de Anthropic | Clave de facturación |
| `DATABASE_URL` (contraseña del rol) | Supabase: contraseña del proyecto/rol | Acceso a la base |
| `SUPABASE_SECRET_KEY` | Supabase: claves de API | Clave de servicio |
| `DEMO_TOKEN` | Generar uno nuevo en `.env` local | Acceso a la demostración |
| `OPENROUTER_API_KEY` | Panel de OpenRouter | Clave de proveedor no usado en el piloto |

Pasos tras rotar: actualizar el `.env` local sin copiarlo a ningún documento, ejecutar el preflight y reiniciar el worker con `scripts\pilot_start.ps1`.

Pendiente de limpieza: en `HEAD` quedan las credenciales de prueba locales ya sustituidas en el árbol de trabajo. Retirarlas del historial requiere reescribir Git, que no se ha hecho.
