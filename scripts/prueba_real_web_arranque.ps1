#
# Lanzador de la prueba controlada (WhatsApp Web vinculado, NO oficial). Ver docs\prueba-real-whatsapp-web.md.
#
#   -AllIncoming   Modo universal: responde a chats individuales NUEVOS de números que no están en la agenda
#                  ni en el historial. Ventana fija de 15 minutos. No pide números.
#   (sin nada)     Solo muestra el plan y comprobaciones. No arranca nada.
#   -Go            Arranca FastAPI, el worker y el conector (requiere -AllIncoming).
#   -AutoReply     Activa WEB_BRIDGE_AUTO_REPLY=true. Sin él no se envía nada, aunque los procesos estén arriba.
#
# Garantías:
#   * La caducidad se calcula con DateTimeOffset.UtcNow (Unix UTC), una sola vez, y es la misma para los tres procesos.
#   * El secreto HMAC se genera una vez en connector\whatsapp-web\state\bridge-secret.txt (ignorado por Git).
#     Viaja por el entorno heredado; no aparece en la línea de comandos de ningún proceso ni en la salida.
#   * El veredicto del LLM y del sender se lee de la línea "worker_profile" que el PROPIO worker registra al
#     construir sus componentes (no de un preflight paralelo).
#   * Cada ejecución escribe sus logs en connector\whatsapp-web\state\logs\ (ignorados por Git). Sin contenido
#     de mensajes ni números completos.
#   * No mata procesos. Si detecta restos del piloto o puertos ocupados, se detiene y los lista.
#   * El puerto de FastAPI se elige libre; el puerto del puente es 8787 y debe estar libre.
#   * No se modifica .env, Meta Cloud API, .auth ni la base de datos.
#   * No elimina state\STOP. Si existe, el arranque se niega.
[CmdletBinding()]
param(
    [switch]$AllIncoming,
    [switch]$Go,
    [switch]$AutoReply,
    # Límite por interacción en USD. Lo decide usted. Con 0.002 ninguna respuesta de Claude cabe en la reserva.
    [decimal]$InteractionLimitUsd = 0
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$connector = Join-Path $repo "connector\whatsapp-web"
$stateDir = Join-Path $connector "state"
$logDir = Join-Path $stateDir "logs"
$secretFile = Join-Path $stateDir "bridge-secret.txt"
$python = Join-Path $repo ".venv\Scripts\python.exe"
$lineId = "573153863179"
$windowSeconds = 900
$bridgePort = 8787
$runStamp = (Get-Date).ToString("yyyyMMdd-HHmmss")
$workerLog = Join-Path $logDir "worker-$runStamp.log"
$backendLog = Join-Path $logDir "backend-$runStamp.log"
$connectorLog = Join-Path $logDir "connector-$runStamp.log"

function Get-UnixNow { [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() }

function Test-PortFree([int]$port) {
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $port)
    try { $listener.Start(); return $true } catch { return $false } finally { $listener.Stop() }
}

function Find-FreePort([int]$start) {
    for ($p = $start; $p -lt ($start + 50); $p++) { if (Test-PortFree $p) { return $p } }
    throw "No hay puertos libres a partir de $start."
}

# Procesos de este proyecto que siguen vivos. Solo se listan; nunca se terminan desde aquí.
function Get-Leftovers {
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='node.exe' OR Name='ngrok.exe'" |
        Where-Object { ([string]$_.CommandLine) -match 'app\.main:app|app\.worker\.main|dist/index\.js|ngrok' } |
        ForEach-Object {
            $cmd = [string]$_.CommandLine
            [pscustomobject]@{ Pid = $_.ProcessId; Name = $_.Name; Resumen = $cmd.Substring(0, [Math]::Min(90, $cmd.Length)) }
        }
}

# Worker real: el intérprete de la .venv lanza un hijo con la misma línea; se cuenta solo el padre.
function Get-WorkerProcesses {
    $candidates = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { ([string]$_.CommandLine) -match 'app\.worker\.main' })
    $ids = @($candidates | ForEach-Object { [int]$_.ProcessId })
    return @($candidates | Where-Object { $ids -notcontains [int]$_.ParentProcessId })
}

function Get-ProfileField([string]$line, [string]$key) {
    if ($line -match "(?:^|\s)$key=(\S+)") { return $Matches[1] }
    return $null
}

if (-not $AllIncoming) {
    Write-Host "Sin -AllIncoming no hay alcance: no se arranca nada. Use -AllIncoming para el modo universal de 15 minutos."
    exit 0
}
if ($Go -and -not $AutoReply) {
    throw "Para enviar respuestas reales añada -AutoReply junto con -Go. Sin él no se envía nada."
}
if ($Go -and $InteractionLimitUsd -lt 0.02) {
    throw "Defina -InteractionLimitUsd (USD, mínimo 0.02). Con 0.002 ninguna respuesta de Claude cabe en la reserva por interacción."
}
$stopPresent = Test-Path (Join-Path $stateDir "STOP")

# 1. Secreto: se crea una vez, sin mostrarlo.
New-Item -ItemType Directory -Force -Path $stateDir | Out-Null
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
if (-not (Test-Path $secretFile)) {
    $bytes = New-Object byte[] 48
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    [IO.File]::WriteAllText($secretFile, [Convert]::ToBase64String($bytes), (New-Object Text.UTF8Encoding $false))
    Write-Host "Secreto HMAC nuevo generado (no se muestra)."
}
$secret = [IO.File]::ReadAllText($secretFile).Trim()
if ($secret.Length -lt 32) { throw "El archivo de secreto no es válido." }

# 2. Ventana: 15 minutos desde ahora, en Unix UTC. Se calcula una vez y se comparte.
$nowSec = Get-UnixNow
$untilSec = $nowSec + $windowSeconds
$autoValue = if ($AutoReply) { "true" } else { "false" }

# 3. Comprobaciones previas (solo lectura).
$leftovers = @(Get-Leftovers)
$bridgeFree = Test-PortFree $bridgePort
$backendPort = Find-FreePort 8765

Write-Host "== Comprobaciones previas"
Write-Host ("Inicio de la ventana (Unix UTC): {0}. Caducidad: {1} (ventana de {2} s)." -f $nowSec, $untilSec, $windowSeconds)
Write-Host "Puerto del backend libre: $backendPort. Puerto del puente $bridgePort libre: $bridgeFree."
if ($stopPresent) { Write-Host "AVISO: existe state\STOP (apagado). -Go se negará hasta que lo retire usted." }
if ($leftovers.Count -gt 0) {
    Write-Host "Restos del piloto vivos (no se terminan desde aquí):"
    $leftovers | Format-Table -AutoSize | Out-String | Write-Host
}

# Proveedor real. El .env trae AGENT_DRY_RUN=true y ANTHROPIC_ENABLED=false: sin estas variables el worker
# arranca con el simulador. Se fijan explícitamente en TODOS los procesos.
$common = @{
    WEB_BRIDGE_SECRET           = $secret
    WHATSAPP_TRANSPORT          = "web_bridge"
    WEB_BRIDGE_URL              = "http://127.0.0.1:$bridgePort"
    WEB_BRIDGE_LINE_ID          = $lineId
    WHATSAPP_AUDIENCE_MODE      = "test_allowlist"
    WHATSAPP_ALLOWED_RECIPIENTS = ""
    WEB_BRIDGE_AUTO_REPLY       = $autoValue
    WEB_BRIDGE_INBOUND_MODE     = "web_inbound_all"
    WEB_BRIDGE_INBOUND_UNTIL    = [string]$untilSec
    LLM_PROVIDER                = "anthropic"
    ANTHROPIC_ENABLED           = "true"
    AGENT_DRY_RUN               = "false"
    ANTHROPIC_MODEL             = "claude-sonnet-5-5"
    ANTHROPIC_PRICING_MODEL     = "claude-sonnet-5-5"
    DEMO_ENABLED                = "false"
    AGENT_KILL_SWITCH           = "false"
    AGENT_INTERACTION_LIMIT_USD = [string]$InteractionLimitUsd
}
$backendVars = $common + @{ LACUSINE_LOAD_DOTENV = "true" }
$workerVars = $common + @{ LACUSINE_LOAD_DOTENV = "true"; WORKER_ENABLED = "true" }
$connectorVars = $common + @{ WEB_BRIDGE_BACKEND_URL = "http://127.0.0.1:$backendPort"; WEB_BRIDGE_PORT = "$bridgePort" }

Write-Host "== Plan (modo universal)"
Write-Host "Remitente: línea 3179. Alcance: chats individuales nuevos, no agenda ni historial."
Write-Host "Respuestas automáticas: $autoValue. Modo público: no permitido por este transporte."
Write-Host "Procesos: conector (puerto $bridgePort), FastAPI (puerto $backendPort), worker único."
Write-Host "Proveedor: anthropic / claude-sonnet-5-5. Límite por interacción: $InteractionLimitUsd USD (mínimo 0.02 con -Go)."
Write-Host "Logs de esta ejecución: $logDir (worker-$runStamp.log, backend-$runStamp.log, connector-$runStamp.log)."

if (-not $Go) {
    Write-Host "Modo plan: no se ha arrancado nada. Para ejecutar: -AllIncoming -Go -AutoReply."
    exit 0
}

# 4. Bloqueos: no se arranca con restos, con puertos ocupados ni con la ventana casi vencida.
if ($stopPresent) { throw "Existe state\STOP (apagado). Bórrelo solo si quiere reactivar el conector." }
if ($leftovers.Count -gt 0) { throw "Hay restos del piloto vivos. Ciérrelos antes de arrancar (lista arriba)." }
if (-not $bridgeFree) { throw "El puerto $bridgePort del puente está ocupado. No se arranca." }
if ($windowSeconds -gt 900) { throw "La ventana no puede superar 900 s." }

# 5. Arranque. Las variables se heredan por el entorno del proceso y se restauran al final.
# Cada proceso escribe su salida en un log propio (tee). -u evita que Python retenga líneas en el búfer.
$previous = @{}
function Set-LaunchEnv([hashtable]$vars) {
    foreach ($k in $vars.Keys) {
        $previous[$k] = [Environment]::GetEnvironmentVariable($k, "Process")
        [Environment]::SetEnvironmentVariable($k, [string]$vars[$k], "Process")
    }
}
function Restore-LaunchEnv {
    foreach ($k in $previous.Keys) { [Environment]::SetEnvironmentVariable($k, $previous[$k], "Process") }
}
function Start-Window([string]$title, [string]$workdir, [string]$command, [string]$logFile) {
    $script = "`$Host.UI.RawUI.WindowTitle = '$title'; Set-Location '$workdir'; $command *>&1 | Tee-Object -FilePath '$logFile'"
    return Start-Process powershell -ArgumentList @("-NoExit", "-Command", $script) -PassThru
}

try {
    Set-LaunchEnv $backendVars
    $null = Start-Window "LaCuisine FastAPI" $repo "& '$python' -u -m uvicorn app.main:app --host 127.0.0.1 --port $backendPort" $backendLog
    Restore-LaunchEnv; $previous = @{}

    $healthy = $false
    for ($i = 0; $i -lt 60 -and -not $healthy; $i++) {
        Start-Sleep -Seconds 1
        try { $healthy = (Invoke-WebRequest -Uri "http://127.0.0.1:$backendPort/health" -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200 } catch { $healthy = $false }
    }
    if (-not $healthy) { throw "FastAPI no responde en el puerto $backendPort. Revise $backendLog." }

    Set-LaunchEnv $workerVars
    $null = Start-Window "LaCuisine worker" $repo "& '$python' -u -m app.worker.main" $workerLog
    Restore-LaunchEnv; $previous = @{}

    Set-LaunchEnv $connectorVars
    $null = Start-Window "LaCuisine conector WhatsApp Web" $connector "node dist/index.js" $connectorLog
    Restore-LaunchEnv; $previous = @{}
}
finally {
    Restore-LaunchEnv
}

# 6. Verificación. Cada veredicto se deriva de lo que el proceso registra, no de suposiciones.
Write-Host "== Verificación"

# 6a. Perfil que el worker REGISTRA al construir sus componentes (espera hasta 60 s).
$profileLine = $null
for ($i = 0; $i -lt 60 -and -not $profileLine; $i++) {
    Start-Sleep -Seconds 1
    if (Test-Path $workerLog) {
        $hit = Select-String -Path $workerLog -Pattern "worker_profile " -SimpleMatch | Select-Object -Last 1
        if ($hit) { $profileLine = [string]$hit.Line }
    }
}
$provider = Get-ProfileField $profileLine "provider"
$model = Get-ProfileField $profileLine "model"
$dryRun = Get-ProfileField $profileLine "dry_run"
$transport = Get-ProfileField $profileLine "whatsapp_transport"
$senderClass = Get-ProfileField $profileLine "sender_class"
$sendEnabled = Get-ProfileField $profileLine "send_enabled"
$profileOk = ($provider -ceq "anthropic") -and ($model -ceq "claude-sonnet-5-5") -and ($dryRun -ceq "False") `
    -and ($transport -ceq "web_bridge") -and ($senderClass -ceq "WebBridgeSender") -and ($sendEnabled -ceq "True")
Write-Host ("perfil registrado por el worker: provider={0} model={1} dry_run={2} whatsapp_transport={3} sender_class={4} send_enabled={5}" -f `
    $provider, $model, $dryRun, $transport, $senderClass, $sendEnabled)
if ($null -eq $profileLine) { Write-Host "AVISO: el worker no registró perfil en 60 s. Revise $workerLog." }

# 6b. Puente: conexión, contadores y caducidad. Solo 'open' cuenta como conexión.
$status = $null
for ($i = 0; $i -lt 90; $i++) {
    Start-Sleep -Seconds 1
    try {
        $stamp = [string](Get-UnixNow)
        $hmac = New-Object System.Security.Cryptography.HMACSHA256 -ArgumentList (, [Text.Encoding]::UTF8.GetBytes($secret))
        try { $digest = $hmac.ComputeHash([Text.Encoding]::UTF8.GetBytes("status.$stamp.")) } finally { $hmac.Dispose() }
        $sig = [BitConverter]::ToString($digest).Replace("-", "").ToLowerInvariant()
        $status = Invoke-RestMethod -Uri "http://127.0.0.1:$bridgePort/status" -Headers @{ "X-Bridge-Timestamp" = $stamp; "X-Bridge-Signature" = $sig } -TimeoutSec 3
        if ($status.connection -eq "open") { break }
    } catch { $status = $null }
}
if ($null -eq $status) { throw "El puente no responde. Revise $connectorLog." }
$sameUntil = [int64]$status.inbound_until -eq [int64]$untilSec
Write-Host ("puente: conexion={0} ready={1} respuestas_automaticas={2} caducidad_coincide={3} restante_s={4}" -f `
    $status.connection, $status.ready, $status.auto_reply, $sameUntil, ([int64]$status.inbound_until - (Get-UnixNow)))
Write-Host ("contadores: raw_upserts={0} notify={1} append_ignorados={2} stop_bloqueados={3} reenviados={4}" -f `
    $status.raw_upserts_total, $status.notify_received_total, $status.append_ignored_total, $status.stop_blocked_total, $status.messages_forwarded_total)

# 6c. Vida de los procesos: se comprueba DESPUÉS del arranque, no solo en el instante de iniciarlos.
Start-Sleep -Seconds 10
$workers = @(Get-WorkerProcesses)
$connectorAlive = @(Get-CimInstance Win32_Process -Filter "Name='node.exe'" | Where-Object { ([string]$_.CommandLine) -match 'dist/index\.js' }).Count -gt 0
$backendAlive = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { ([string]$_.CommandLine) -match 'app\.main:app' }).Count -gt 0
$errorLines = @()
foreach ($log in @($workerLog, $connectorLog, $backendLog)) {
    if (Test-Path $log) {
        $errorLines += @(Select-String -Path $log -Pattern "Traceback|ValueError|RuntimeError|stage=01_handler_error|stage=connection state=closed" -SimpleMatch:$false | Select-Object -Last 3)
    }
}

# 7. Resumen. "Message Forwarding" NO se declara VERIFIED aquí: solo lo demuestra un mensaje real trazado.
Write-Host ""
Write-Host "== Estado"
Write-Host ("FastAPI: {0}" -f ($(if ($healthy -and $backendAlive) { "HEALTHY_VIVO" } else { "NO_DISPONIBLE" })))
Write-Host ("Conector: {0}" -f ($(if ($connectorAlive -and $status.connection -eq "open") { "CONECTADO_VIVO" } else { "NO_DISPONIBLE (estado=$($status.connection))" })))
Write-Host ("Worker: {0}" -f ($(if ($workers.Count -eq 1) { "EXACTLY_ONE_VIVO" } elseif ($workers.Count -eq 0) { "CAIDO_O_NO_ARRANCO" } else { "REVISAR_($($workers.Count))" })))
Write-Host ("LLM: {0}" -f ($(if ($profileOk) { "ANTHROPIC_REAL" } else { "NO_ES_ANTHROPIC_REAL (ver perfil arriba)" })))
Write-Host ("Dry Run: {0}" -f ($(if ($dryRun -ceq "False") { "FALSE" } else { "REVISAR" })))
Write-Host ("Sender: {0}" -f ($(if ($senderClass -ceq "WebBridgeSender") { "WebBridgeSender" } else { "REVISAR ($senderClass)" })))
Write-Host "Transport: WEB_BRIDGE"
Write-Host "Inbound: ALL_NEW_INDIVIDUAL_MESSAGES (ventana de 15 min)"
Write-Host "Message Forwarding: PENDIENTE_MENSAJE_REAL"
Write-Host "Outbound: ENABLED_WITHIN_WINDOW"
if ($errorLines.Count -gt 0) {
    Write-Host "AVISO: errores registrados en los logs (últimas líneas):"
    $errorLines | ForEach-Object { Write-Host ("  " + $_.Line) }
}
if ($status.connection -ne "open") { Write-Host "AVISO: la sesión no está 'open'. No envíe mensajes de prueba todavía." }
if (-not $sameUntil) { Write-Host "AVISO: la caducidad del conector no coincide. Detenga la prueba." }
if (-not $profileOk) { Write-Host "AVISO: el perfil efectivo NO coincide con el esperado. Detenga el worker antes de enviar." }
Write-Host "Logs: $workerLog | $connectorLog | $backendLog"
