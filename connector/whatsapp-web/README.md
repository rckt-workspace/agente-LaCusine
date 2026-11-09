# Conector experimental de WhatsApp Web (dispositivo vinculado)

> **AVISO.** Esta integración usa WhatsApp Web como **dispositivo vinculado** mediante Baileys. **No es la API oficial de Meta.**
> Su uso puede provocar **restricciones o la suspensión de la cuenta** de WhatsApp. No hay garantía de seguridad absoluta
> ni de cumplimiento de las condiciones de Meta. Úselo solo bajo su propia responsabilidad y en pruebas controladas.

**Estado: preparado y verificado en local con pruebas automáticas. NO vinculado. NO envía mensajes. Respuestas automáticas desactivadas.**

## 1. Qué hace

- Un proceso Node.js/TypeScript aislado que se vincula a su WhatsApp como **dispositivo adicional** (QR). Su celular sigue funcionando.
- Recibe mensajes **nuevos de texto** de **contactos no conocidos** en chats 1:1 y los reenvía al backend FastAPI, que los registra
  en `webhook_events` y los procesa con la misma cola, el mismo motor de Claude, presupuestos, idempotencia y transferencia humana que Cloud API.
- Envía respuestas **solo** si `WEB_BRIDGE_AUTO_REPLY=true`, solo como respuesta dentro de la ventana de 24 horas, y solo a destinatarios permitidos en modo de prueba.

## 2. Qué NO hace (garantías verificadas por código y pruebas)

| Garantía | Dónde |
|---|---|
| No procesa historial, sincronización ni mensajes antiguos | `policy.ts` (`history_sync`, `too_old`); `index.ts` (ventana de 120 s) |
| No responde a sus conversaciones personales existentes | `policy.ts` (`known_contact`); contactos y chats de `messaging-history.set`, `chats.set`, `contacts.upsert` |
| No procesa mensajes propios, grupos, estados, listas ni canales | `policy.ts` (`own_message`, `group`, `status_or_broadcast`, `newsletter`) |
| No procesa imágenes, audios ni documentos | `policy.ts` (`not_text`) |
| No envía nada sin autorización explícita | `WEB_BRIDGE_AUTO_REPLY` en el conector y en el backend; sin ella el puente responde `409` |
| No envía mensajes iniciados por la empresa | No existe ningún camino de envío sin mensaje entrante dentro de 24 h |
| No responde fuera de la lista en modo de prueba | `WebBridgeSender` y el endpoint de entrada (`not_in_test_allowlist`) |
| No duplica respuestas | Idempotencia por `idempotency_key` en el puente y en la cola del backend |
| No acepta peticiones sin firma | HMAC-SHA256 con ventana de 5 minutos, en ambos sentidos |
| No expone datos en el puerto | Escucha solo en `127.0.0.1` |
| No registra texto, números ni identificadores de chat | Logs solo con contadores y motivos |
| No se puede activar en público | `audience.py`: `public_mode_refused_on_web_bridge` |

## 3. Requisitos

- Node.js 20 o superior (verificado con Node 22). Dependencias fijadas en `package.json`: `@whiskeysockets/baileys` **6.7.24** (la línea estable; la 7 es candidata).
- Python: el entorno `.venv` del proyecto. No se modifica el Python global.
- Un secreto compartido de **32 caracteres o más**, definido solo en el entorno de la sesión (`WEB_BRIDGE_SECRET`). Nunca en el repositorio.

## 4. Configuración

Variables del **backend** (`.env`, nunca versionado):

```
WHATSAPP_TRANSPORT=web_bridge            # por defecto: cloud_api
WEB_BRIDGE_URL=http://127.0.0.1:8787     # dirección del conector (loopback)
WEB_BRIDGE_LINE_ID=573153863179         # identificador interno de la línea web (solo dígitos)
WEB_BRIDGE_AUTO_REPLY=false              # true SOLO con su autorización expresa
WHATSAPP_AUDIENCE_MODE=test_allowlist    # public NO está permitido con web_bridge
WHATSAPP_ALLOWED_RECIPIENTS=<su número de prueba, sin +>
```

Variables del **conector** (solo en la sesión de PowerShell, no en archivos):

```
WEB_BRIDGE_SECRET        = <32+ caracteres, el mismo valor que el backend>
WEB_BRIDGE_BACKEND_URL   = http://127.0.0.1:8000   # loopback o HTTPS
WEB_BRIDGE_PORT          = 8787
WEB_BRIDGE_AUTO_REPLY    = false
```

`WEB_BRIDGE_SECRET` debe ser el **mismo** en ambos lados. Para los dos, generarlo con un gestor de contraseñas y no escribirlo en ningún archivo del repositorio.

## 5. Comandos (PowerShell)

Desde la raíz del repositorio:

```powershell
# Instalar dependencias del conector (una vez; no vincula nada)
cd connector\whatsapp-web; npm install; cd ..\..

# Definir el secreto en ESTA sesión (no se imprime)
$env:WEB_BRIDGE_SECRET = Read-Host "Secreto del puente" -AsSecureString | ForEach-Object { [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($_)) }

# Iniciar: abre una ventana nueva; el QR aparece en ESA ventana
.\scripts\whatsapp_web_connector.ps1 -Action start

# Consultar estado (sin datos personales)
.\scripts\whatsapp_web_connector.ps1 -Action status

# Apagado inmediato (bloquea reinicios con state\STOP y cierra la sesión)
.\scripts\whatsapp_web_connector.ps1 -Action stop

# Reactivar tras un apagado: borrar el archivo STOP con confirmación explícita
Remove-Item connector\whatsapp-web\state\STOP
```

## 6. Procedimiento para desvincular sin eliminar su cuenta

1. Opción recomendada desde el teléfono: **WhatsApp > Ajustes > Dispositivos vinculados** > seleccione el dispositivo > **Cerrar sesión**. No afecta a su cuenta ni a sus chats.
2. Desde el conector: `.\scripts\whatsapp_web_connector.ps1 -Action unlink -Confirm`. Cierra la sesión de ese dispositivo y **elimina las credenciales locales** (`connector\whatsapp-web\.auth`).
3. Verifique que el dispositivo ya no aparece en **Dispositivos vinculados**, y que WhatsApp sigue activo en su celular.

Nunca use "Eliminar cuenta" ni desinstale WhatsApp para desvincular: eso borra su cuenta y su historial.

## 7. Pruebas

```powershell
# Conector: compilación estricta + pruebas (política, firma, puente HTTP)
cd connector\whatsapp-web; npm test; cd ..\..

# Backend: transporte, firma, filtros, remitente y endpoint interno
.\.venv\Scripts\python.exe -m pytest tests\integrations\test_web_bridge.py -q
```

Resultado en esta versión: **24 pruebas de Node y 31 de Python** del transporte web, sin WhatsApp real. El vector HMAC es el mismo en ambos lenguajes.

Pendiente para la fase de prueba real (**no ejecutada**, requiere su aprobación expresa): vincular el número, enviar desde un segundo teléfono al 3179, y comprobar la respuesta.

## 8. Riesgos (informe de seguridad)

| Riesgo | Gravedad | Mitigación | Residual |
|---|---|---|---|
| **Suspensión o restricción de la cuenta** por uso no oficial | Alta | Sin envíos iniciados por la empresa; sin difusión; ritmo bajo; pruebas controladas | **Alto**: no hay forma de garantizarlo. Acepte el riesgo conscientemente |
| Respuesta a una conversación personal existente | Alta | Protección por agenda y chats al vincular; ventana de 120 s; direcciones no identificables se ignoran | Medio: un contacto nuevo que no esté en su agenda sí puede recibir respuesta. Si un contacto le escribe sin estar en su agenda, el sistema le responderá |
| Direcciones `@lid` (identificadores ocultos de WhatsApp) | Media | Se ignoran (`unsupported_address`) | Puede bloquear a clientes legítimos que usen esa dirección |
| Fuga de la sesión (`.auth/`) | Crítica | Carpeta ignorada por Git; desvinculación borra credenciales | Quien tenga esa carpeta puede usar la sesión: protéjala como una contraseña |
| Secreto compartido débil o filtrado | Alta | Mínimo 32 caracteres; solo en entorno; firma con ventana de 5 min | Rotar si sospecha filtración |
| Reenvío de datos personales a Anthropic | Alta | Solo mensajes de texto nuevos de contactos no conocidos; los contenidos no se registran | Los mensajes que sí se reenvían sí se envían al proveedor de IA |
| Procesamiento de historial personal | Alta | Nunca se reenvía `history_sync` ni mensajes antiguos | Ninguno conocido |
| Caída del puente con envío en curso | Media | Error `500` = incierto; el backend no reintenta automáticamente | Un mensaje puede enviarse dos veces si se reintenta a mano |
| Número de Meta (2419) | — | No se modifica ni se usa | — |
| Presupuesto de IA | Media | Presupuesto diario/mensual del agente existente; kill switch | Revisar límites antes de activar |

## 9. Separación de conversaciones personales (documentación exigida antes del modo público)

El modo público **no está permitido** con este transporte. Para responder a conocidos o a desconocidos de forma controlada, el criterio actual es:

1. **Protegido**: cualquier contacto de la agenda o chat existente al vincular. Nunca recibe respuesta.
2. **Elegible**: contacto no presente en la agenda, que escribe en un chat 1:1 con texto, en vivo, dentro de los 120 s.
3. **Descartado**: todo lo demás (grupos, estados, historial, propios, medios).

Antes de cualquier activación fuera de prueba, debe demostrarse en una prueba real que **un contacto de su agenda no recibe respuesta**. Esa prueba no se ha ejecutado.

## 10. Lo que no hay en esta versión

- No hay plantillas aprobadas: fuera de 24 horas no se responde (bloqueado por diseño).
- No hay recepción de mensajes de grupo ni multimedia.
- No hay sincronización de historial ni importación de conversaciones.
- No hay activación del modo público.
