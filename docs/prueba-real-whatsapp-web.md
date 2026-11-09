# Prueba real: respuesta automática a UN chat desde el 3179 (transporte WhatsApp Web, no oficial)

**Estado: preparado. NO activado.** El envío real queda pendiente de tu confirmación final.
No se toca Cloud API, el número de Meta, las migraciones ni el historial de Git.

## 1. Estado observado (sin credenciales)

| Componente | Estado | Qué significa |
|---|---|---|
| Puente Node `127.0.0.1:8787` | Escuchando (responde `401` sin firma) | El proceso existe; su configuración se fijó al arrancar |
| FastAPI `127.0.0.1:8000` | Responde `/health` y `/docs` | Arrancado **antes** de añadir el endpoint `/internal/web-bridge`: hay que reiniciarlo |
| Worker de este repositorio | Ninguno (`pilot_workers list`: 0) | Correcto: no hay doble worker |
| `WEB_BRIDGE_SECRET` en esta sesión | No definido | Hace falta definirlo en cada terminal que lo use |
| Sesión de WhatsApp (`.auth/`) | Presente | Se reutiliza al reiniciar: **no hace falta QR** |
| `state\STOP` | No existe | Correcto |

## 2. Por qué el conector tiene `auto_reply=false`

`WEB_BRIDGE_AUTO_REPLY` se lee **una sola vez, al arrancar el proceso**. Si la terminal del conector se abrió sin esa variable, el valor por defecto es `false`. Lo mismo ocurre en FastAPI, que también la lee al arrancar: cambiarla en otra terminal no afecta a un proceso ya iniciado. Por eso la activación exige **reiniciar** el conector, FastAPI y el worker con el entorno correcto.

El conector también tiene código **anterior** a la autorización de un solo chat, así que el reinicio es necesario por ambos motivos.

## 3. Qué cambia (diseño)

- **Un solo chat autorizado, temporal.** Dos variables del conector: `WEB_BRIDGE_TEST_CONTACT` (solo dígitos) y `WEB_BRIDGE_TEST_CONTACT_UNTIL` (instante Unix de caducidad, máximo 2 horas). Solo anulan la protección de **ese** número. Los demás contactos de su agenda siguen protegidos, y propios, grupos, historial y duplicados siguen descartándose.
- **Backend en `test_allowlist`.** `WHATSAPP_ALLOWED_RECIPIENTS` contiene el mismo número, así que el backend tampoco responde a nadie más.
- **Transporte web nunca público.** `audience.py` lo rechaza al arrancar.
- **Preflight consciente del transporte.** En `web_bridge` comprueba URL, secreto, línea, lista y la sesión conectada del puente, sin consultar Meta.

## 4. Datos que necesito de ti (no los supongo)

1. **Los dígitos del chat de prueba**, en formato `573XXXXXXXXX`. Debe ser una conversación que ya existe en tu WhatsApp. No la elijo yo.
2. **Tu confirmación final** antes de habilitar el envío (paso 7).

## 5. Procedimiento (solo tras tu aprobación de este documento)

Todos los comandos se ejecutan en PowerShell desde la raíz del repositorio. Los valores entre `< >` los define usted.

### 5.1 Parar el conector actual (conserva la sesión)

Cierre su ventana de conector con **Ctrl+C**. No borre `connector\whatsapp-web\.auth`. Confirme que no existe `state\STOP`.

### 5.2 Definir el secreto en cada terminal (sin imprimirlo)

```powershell
$env:WEB_BRIDGE_SECRET = Read-Host "Secreto del puente (32+)" -AsSecureString | ForEach-Object { [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($_)) }
```

El **mismo** valor en el conector y en FastAPI.

### 5.3 Conector, con autorización de un chat y caducidad de 30 minutos

```powershell
$env:WEB_BRIDGE_AUTO_REPLY = "true"
$env:WEB_BRIDGE_TEST_CONTACT = "<573XXXXXXXXX>"
$env:WEB_BRIDGE_TEST_CONTACT_UNTIL = [string]([int][double]::Parse((Get-Date -UFormat %s)) + 1800)
Set-Location connector\whatsapp-web; node dist/index.js
```

Debe aparecer `autorización de prueba de UN chat activa hasta <hora>` y **no** el número. Sin nueva línea de QR: la sesión se reutiliza.

### 5.4 FastAPI, con el transporte web

En **otra** terminal:

```powershell
$env:LACUSINE_LOAD_DOTENV = "true"          # carga .env; las variables de esta terminal tienen prioridad
$env:WHATSAPP_TRANSPORT = "web_bridge"
$env:WEB_BRIDGE_AUTO_REPLY = "true"
$env:WEB_BRIDGE_URL = "http://127.0.0.1:8787"
$env:WEB_BRIDGE_LINE_ID = "573153863179"
$env:WHATSAPP_AUDIENCE_MODE = "test_allowlist"
$env:WHATSAPP_ALLOWED_RECIPIENTS = "<573XXXXXXXXX>"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

`--host 127.0.0.1`: nunca `0.0.0.0`. Compruebe `http://127.0.0.1:8000/internal/web-bridge/inbound`: sin firma debe responder `401`.

### 5.5 Worker: exactamente uno

En **otra** terminal, con las mismas variables de 5.4 más:

```powershell
$env:WORKER_ENABLED = "true"
.\.venv\Scripts\python.exe scripts\pilot_workers.py list --repo (Get-Location)   # debe mostrar 0 antes de arrancar
.\.venv\Scripts\python.exe -m app.worker.main
```

### 5.6 Preflight en solo lectura (antes de confirmar)

```powershell
$env:WHATSAPP_TRANSPORT = "web_bridge"; $env:LACUSINE_LOAD_DOTENV = "true"
.\.venv\Scripts\python.exe scripts\pilot_preflight.py --expect-model claude-sonnet-5-5
```

Debe mostrar: transporte web, puente con `conexion=open`, `respuestas_automaticas_autorizadas=si`, colas sin bloqueos críticos y `LISTO ... test_allowlist (desarrollo). NO es atención pública.` Si algo falla, **no continúe**.

### 5.7 Recorrido que se verificará

1. Desde el **segundo teléfono** (el número de 5.3) se escribe un mensaje de texto **nuevo** al 3179.
2. El conector filtra (`mensaje reenviado`) y firma la solicitud hacia `/internal/web-bridge/inbound`.
3. FastAPI valida la firma y la lista, y registra el evento en `webhook_events` (Supabase).
4. El worker reclama el evento, consulta el catálogo en Supabase y llama a Claude.
5. El despachador crea la respuesta y llama a `WebBridgeSender`, que firma `/send`.
6. El conector envía el texto con Baileys desde el 3179.

Lo que se comprueba en cada paso se resume en el informe de la prueba, que no contiene números completos ni texto de mensajes.

### 5.8 Después de la prueba

Vuelva a poner `WEB_BRIDGE_AUTO_REPLY` a `false` reiniciando el conector, y elimine `WEB_BRIDGE_TEST_CONTACT`. La autorización caduca sola en 30 minutos, pero conviene retirarla.

## 6. Cómo parar sin eliminar la cuenta de WhatsApp

| Qué | Comando | Efecto |
|---|---|---|
| Worker | **Ctrl+C** en su terminal | Termina tras el trabajo en curso. No borra nada |
| FastAPI | **Ctrl+C** en su terminal | Deja de recibir eventos del puente |
| Conector, rápido | `.\scripts\whatsapp_web_connector.ps1 -Action stop` | Crea `state\STOP` (bloquea reinicios) y cierra la sesión |
| Conector, reanudar | Borrar `connector\whatsapp-web\state\STOP` y arrancar de nuevo | Reactivación con la misma sesión |
| Desvincular el dispositivo | `.\scripts\whatsapp_web_connector.ps1 -Action unlink -Confirm` | Cierra solo este dispositivo y borra credenciales locales. **No borra la cuenta** |

Alternativa desde el teléfono: **WhatsApp > Ajustes > Dispositivos vinculados > Cerrar sesión**.

## 7. Riesgos de esta prueba

- **Respuesta automática real** a una conversación personal. Limitada a un solo chat y 30 minutos, pero es un envío real.
- **Escritura en la base de Supabase de producción** (eventos, conversaciones y respuestas). Es el piloto existente, no una base de prueba aparte.
- **Costo de Claude.** Limitado por el presupuesto del agente (`AGENT_DAILY_BUDGET_USD`); revise su valor antes.
- **Suspensión de la cuenta** por uso no oficial, ya documentado en `connector/whatsapp-web/README.md`.
- **Direcciones `@lid`**: si el chat autorizado llega con una dirección oculta, el conector la descarta y no habrá respuesta. Si eso ocurre, el resultado es "no respondió", no un envío erróneo.
- **Sesiones de terminal**: las variables solo existen en esas ventanas. Cerrarlas las elimina.

## 8. Pendiente antes de activar

1. Confirmar el chat de prueba (dígitos).
2. Autorizar explícitamente el paso 5.3 en adelante.
3. Confirmar el presupuesto de Claude.
