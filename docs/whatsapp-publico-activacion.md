# Modo de audiencia de WhatsApp: `test_allowlist` y `public`

Este documento describe el control de quién recibe respuestas automáticas. **El modo público no está activo** y no debe activarse sin aprobación del titular del negocio.

## 1. Modos

| Modo | Quién recibe respuestas | Por defecto |
|---|---|---|
| `test_allowlist` | Solo los números de `WHATSAPP_ALLOWED_RECIPIENTS`. Es el modo de pruebas con el número virtual de Meta. | **Sí** |
| `public` | Cualquier contacto que escriba primero a la línea empresarial registrada, dentro de la ventana de 24 horas. | No |

El modo público **no es** una lista vacía. Es un modo separado que exige configuración validada.

## 2. Qué no cambia en ningún modo

- Firma del webhook y validación de la línea empresarial (`META_PHONE_NUMBER_ID`).
- Ventana de atención de 24 horas: sin mensaje entrante no hay ventana, y el dispatcher bloquea el envío.
- Idempotencia del outbox: una respuesta por mensaje recibido.
- Control humano: una conversación derivada no recibe respuestas automáticas.
- Presupuesto, límite por interacción y bloqueos de la IA.
- Ningún envío masivo ni promocional: el sistema solo responde a mensajes entrantes.

## 3. Activación del modo público (requisitos)

El worker **no arranca** si falta cualquiera de estas condiciones. La comprobación es pura y previa a cualquier conexión.

1. `WHATSAPP_AUDIENCE_MODE=public`.
2. `WHATSAPP_PUBLIC_CONFIRMATION=ABRIR_ATENCION_PUBLICA_CONFIRMADA`. Cualquier otro valor, incluido `true` o vacío, no activa el modo.
3. `DEMO_ENABLED=false` (la demostración nunca es pública).
4. `META_APP_SECRET` y `DATABASE_URL` definidos.
5. `META_SEND_ENABLED=true` y `WHATSAPP_DRY_RUN=false`.
6. Agente real: `LLM_PROVIDER=anthropic` (u otro real) y `AGENT_DRY_RUN=false`. Nunca respuestas simuladas a clientes reales.
7. Proceso de aprobación del titular del negocio registrado fuera del repositorio.

El perfil de arranque muestra `audience_mode=public` en la línea `worker_profile`. Verifícalo antes de aceptar tráfico.

## 4. Procedimiento de activación

1. Confirma que no hay un worker activo: `scripts\pilot_workers.py list --repo .`
2. Define las variables de activación **en el `.env` local**, no en el chat.
3. Ejecuta `scripts\pilot_start.ps1` sin `-Start` y revisa el preflight.
4. Arranca con `scripts\pilot_start.ps1 -Start` y confirma `audience_mode=public` en el perfil.
5. Prueba con **un** número externo autorizado por su titular, escribiendo primero.

## 5. Desactivación inmediata (reversión)

Cualquiera de estas acciones detiene las respuestas:

- **Más rápido:** `META_SEND_ENABLED=false` y reiniciar el worker. Las respuestas quedan `pending` sin enviarse.
- **Parada total:** `scripts\pilot_workers.py stop-wait --repo .` (solo procesos de este repositorio).
- **Solo la IA:** `AGENT_KILL_SWITCH=true`. Atención: en ese caso el sistema envía un mensaje neutro a quien escriba, así que prefiere la primera opción.
- **Volver al modo de pruebas:** `WHATSAPP_AUDIENCE_MODE=test_allowlist` y reiniciar. Las listas de prueba vuelven a aplicar.

La reversión **no borra** conversaciones, leads ni outbox. Las respuestas pendientes pueden enviarse al reactivar: revísalas antes.

## 6. Qué queda fuera de este modo

- No hay campañas, plantillas promocionales ni envíos masivos.
- No hay control de frecuencia por contacto: pendiente antes de activar el modo público con tráfico real.
- No hay soporte seguro para varias líneas empresariales (ver el runbook, escenario B).
- Las conversaciones de clientes distintos permanecen aisladas por `wa_contact_id`.

## 7. Requisitos externos pendientes (Meta)

- Número empresarial registrado en WhatsApp Cloud API, con su propio `META_PHONE_NUMBER_ID`.
- Nombre comercial y requisitos de verificación aplicables según Meta; confirmar en el panel.
- Usuario del sistema con token permanente, rotado si hubo exposición.
- Campo `messages` suscrito y URL HTTPS estable del webhook.
- Verificación del canal con un destinatario externo.
- Enlace `https://wa.me/57...` y código QR: solo después de confirmar que el número responde.

## 8. Verificación de identidad de la línea (preflight)

El preflight consulta, en solo lectura, `account_mode`, `code_verification_status` y `status` del número. El modo `public` **solo** pasa si la línea es `LIVE`, está `VERIFIED` y `CONNECTED`. Un número de pruebas de Meta no cumple ese criterio y el arranque se rechaza. El preflight nunca envía mensajes.

## 9. Límites de frecuencia por conversación

- `CONTACT_MAX_INBOUND_PER_HOUR` (por defecto 20) y `CONTACT_MAX_INBOUND_PER_DAY` (por defecto 60).
- Al superar un límite, el mensaje se marca como `ignored` con motivo `contact_rate_limited`. **No se llama al modelo y no se responde**, para no consumir créditos de IA.
- Los límites se aplican por conversación. Un cliente limitado no afecta a los demás.
- Los límites no se incrementan automáticamente. Cualquier cambio requiere decisión del titular.
