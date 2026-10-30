# Integración con WhatsApp Cloud API

Estado: **adaptador implementado y probado con transporte simulado. Sin envíos reales.** No se ha conectado ningún número, no se ha registrado ni migrado ningún número existente, y no se ha tocado ninguna configuración de Meta.

## Flujo de salida

```
respuesta generada (outbound_messages: pending)
  -> reclamo con lease y orden por conversación
  -> control humano  -> cancelled
  -> ventana de 24 h cerrada -> blocked
  -> WhatsAppSender.send_text -> POST https://graph.facebook.com/{versión}/{PHONE_NUMBER_ID}/messages
       -> 200/201 con identificador  -> sent (wamid guardado)
       -> rechazo previo a la aceptación (4xx, 429, conexión no establecida) -> pending (reintento) o failed
       -> resultado incierto (timeout tras enviar, corte o error de lectura, 5xx, respuesta sin identificador) -> uncertain
  -> webhook de estado (statuses) -> sent | delivered | read | failed, sin retroceder
```

La generación de la respuesta y el envío son pasos separados. Crear una fila en el outbox no significa que el cliente recibiera el mensaje.

## Versión de Graph API

- Valor de referencia verificado el **29/10/2026** en la página oficial de versiones de Meta: **`v26.0`** (publicada el 29/07/2026; fecha de fin "por determinar").
- El código **no fija** la versión: `META_GRAPH_API_VERSION` es obligatoria cuando el envío está activo. Antes de cambiarla, verificar de nuevo la página oficial.

## Endpoint y cuerpo

- `POST https://graph.facebook.com/{META_GRAPH_API_VERSION}/{META_PHONE_NUMBER_ID}/messages`
- Autenticación: cabecera `Authorization: Bearer <token>`. El token nunca aparece en logs, errores ni `repr`.
- Cuerpo de texto: `messaging_product: "whatsapp"`, `recipient_type: "individual"`, `to`, `type: "text"`, `text.body` (máximo 4096 caracteres).
- No se envía ninguna cabecera de idempotencia: Meta no la documenta para este endpoint. La idempotencia del envío la garantiza la base de datos (clave única por respuesta y restricción única del identificador de Meta).

## Estados de mensajería

| Estado | Significado | Quién lo escribe |
|---|---|---|
| `pending` | Generada, lista para despachar | Worker (outbox) |
| `sending` | Reclamada por un dispatcher, con lease | Dispatcher |
| `sent` | Meta aceptó la solicitud y devolvió un identificador | Dispatcher |
| `delivered` | Meta confirma entrega (webhook de estado) | Procesador de estados |
| `read` | Meta confirma lectura (webhook de estado) | Procesador de estados |
| `failed` | Rechazo definitivo | Dispatcher o estado `failed` de Meta |
| `uncertain` | No se sabe si Meta aceptó. **Nunca se reenvía sola** | Dispatcher o lease vencido |
| `blocked` | No se envió por política (ventana cerrada, destinatario, número) | Dispatcher |
| `cancelled` | Retenida por control humano | Dispatcher |

**No se marca `delivered` solo porque Meta respondió 200.** Solo lo marca un webhook de estado.

### Transiciones

Solo se permiten estas transiciones de Meta: `uncertain → sent|delivered|read|failed`, `sent → delivered|read|failed`, `delivered → read`. Cualquier otra se ignora. Una confirmación de lectura que llega antes que la de entrega no hace retroceder el estado.

### Estados que llegan antes que el envío

Si un estado de Meta llega antes de que el dispatcher haya guardado el identificador, no se pierde: el procesador lo reintenta con backoff. Tras agotar los intentos pasa a revisión manual.

## Qué se sabe y qué no del contrato de estados

La documentación oficial confirma los valores `sent`, `delivered` y `read` en `statuses`, y que los errores de un mensaje saliente aparecen en `errors`. **No especifica** si los estados pueden llegar duplicados o fuera de orden, ni la lista completa de campos de `errors`. Por eso el diseño:

- trata los duplicados como no-op (cada estado se identifica por mensaje y valor);
- aplica transiciones monótonas (no retrocede);
- ignora campos y valores desconocidos sin fallar;
- registra solo el código de error, nunca su texto.

Verificar contra la documentación vigente antes de la puesta en marcha.

## Ventana de atención de 24 horas

- Un mensaje libre (texto) solo se envía si el cliente escribió en las últimas 24 horas. La ventana se abre con cada mensaje entrante del cliente.
- Fuera de la ventana: el envío queda `blocked` con `outside_service_window`. **No se envía automáticamente** ningún mensaje libre ni plantilla.
- Una conversación iniciada por la empresa (sin mensaje previo del cliente) queda bloqueada.
- **El límite se mide con la hora del mensaje que da Meta** (`timestamp`), no con la hora de procesamiento ni de recepción. Una cola detenida durante horas no reinicia ni extiende la ventana.
- La hora de Meta se acepta solo si es numérica, no anterior a 2020-01-01 (UTC) y no más de 5 minutos en el futuro. Si falta o no es válida, el mensaje se guarda pero **no abre la ventana** (`window_trusted = false`).
- Un mensaje antiguo procesado después de uno más reciente no reduce la ventana: se toma el máximo de los mensajes de confianza.
- Duplicados del mismo webhook no cambian la ventana.
- Unidad de `timestamp`: segundos Unix, según la inferencia de la documentación. **Verificar con un mensaje real de prueba antes de la salida.**

### Plantillas

- **Pendiente:** no existe todavía la interfaz de plantillas ni su registro. Una conversación fuera de ventana necesita una plantilla aprobada por Meta, y eso requiere una decisión comercial y su aprobación.
- Mientras tanto, esos casos quedan `blocked` y deben tratarse por un asesor.
- No se crean ni envían campañas.

## Control humano

- Abrir una transferencia es una sola transacción: pausa la automatización (`paused_for_human`), cancela las respuestas de IA pendientes y registra la solicitud. Si cualquier paso falla, no queda nada aplicado a medias (prueba de rollback).
- Abrir la misma transferencia varias veces, o desde dos procesos a la vez, produce una sola solicitud activa.
- Antes de cada envío se lee el estado. Si un asesor controla la conversación, la respuesta de IA pendiente se **cancela**.
- **Confirmación única al cliente:** se envía un texto fijo (`handoff_notice`), que no proviene del modelo. Es la única salida que se envía con la automatización pausada.
- Un mensaje nuevo del cliente **no** reactiva la automatización, mientras haya una transferencia activa o la conversación esté pausada.
- Reactivación: `python -m app.admin.cli ... resume-conversation`, solo si no hay transferencias activas. Queda en `admin_audit_log` con operador, motivo y fecha.

## Línea de negocio

- **MVP de una sola línea.** La línea se configura con `META_PHONE_NUMBER_ID`, y se correlaciona en cada evento, contacto, conversación y salida.
- Un evento de otra línea: el webhook lo ignora cuando la línea está configurada; el procesador lo rechaza como error permanente. Nada se guarda en la conversación ajena.
- Una salida de otra línea: el dispatcher la marca `failed` con `business_line_mismatch` y no la envía.
- **Límite conocido:** `leads.wa_contact_id` es único en toda la base. Con dos líneas reales, el mismo número de cliente compartiría lead entre ambas. Soportar varias líneas exige cambiar el esquema (lead por línea). No se hizo porque el alcance es una línea.

## Destinatarios autorizados

- El envío real requiere `WHATSAPP_ALLOWED_RECIPIENTS` con al menos un número. Vacío = configuración inválida con el envío activo.
- Un destinatario fuera de la lista se rechaza **antes** de cualquier solicitud.
- El número de origen debe coincidir con `META_PHONE_NUMBER_ID`. Si no coincide, se rechaza sin solicitud.
- Ningún número real aparece en el repositorio. Las pruebas usan números sintéticos.

## Atribución de anuncios de Meta Ads

- **No implementado.** Hoy no se persiste la referencia de anuncio que Meta pueda entregar en el mensaje entrante.
- Cuando se implemente, debe guardarse solo lo que Meta entregue, sin suponer que todas las conversaciones tienen las mismas condiciones de mensajería o facturación.

## Diagnóstico de coexistencia y del número actual

**Este diagnóstico no modifica nada.** Debe completarlo el responsable de la cuenta empresarial de La Cuisine antes de cualquier conexión real. No se han consultado ni alterado los activos de Meta de LCA.

Preguntas que deben responderse con evidencia (panel de Meta Business Manager y de la aplicación):

1. ¿Qué cuenta empresarial (Business Portfolio) es la titular del número? ¿Quién tiene el rol de administrador?
2. ¿Cuál es el identificador de la cuenta de WhatsApp Business (WABA) y el `PHONE_NUMBER_ID` del número?
3. ¿El número se usa hoy en **WhatsApp Business App**, en **Cloud API** o en una modalidad de **coexistencia**?
4. Si hay conversaciones activas en la app móvil, ¿qué pasa con ellas al conectar la API? ¿Hay un plan de migración y de comunicación a los clientes?
5. ¿La aplicación de Meta tiene los permisos necesarios y está suscrita a los campos `messages` del webhook de esa WABA?
6. ¿Cuál es el estado del número (verificado, calidad, límites de mensajería)?
7. ¿El número tiene plantillas aprobadas y en uso? ¿Quién es responsable de su aprobación?
8. ¿Hay restricciones de migración que impidan usar el número con la API sin perder la app móvil?
9. ¿Qué pasa con las conversaciones existentes si el número se migra? ¿Quién puede consultarlas después?
10. ¿Quién autoriza una conexión real, y por escrito?

**Riesgos a evaluar antes de conectar:** pérdida de acceso a conversaciones en la app móvil, interrupción de la atención a clientes existentes, límites de calidad o de mensajería que bloqueen el número, y costos de conversación según tipo y mercado (verificar en la documentación de precios de Meta; este repositorio no los declara).

**El MCP de WhatsApp Business Tools** puede ayudar en el diagnóstico de solo lectura, pero no reemplaza el webhook ni el adaptador, y no debe usarse para mutaciones reales.

## Qué queda pendiente

- Plantillas y su interfaz.
- Atribución de anuncios.
- Recepción de estados con mayor cobertura de campos (`conversation`, `pricing`).
- Prueba con un número de prueba de Meta, autorizada por el responsable de la cuenta.
- Adaptador de envío en el despliegue de Render, con `META_SEND_ENABLED` activado solo en la fase de salida.
