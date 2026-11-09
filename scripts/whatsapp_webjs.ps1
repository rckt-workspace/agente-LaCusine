#
# Procedimiento ÚNICO del canal WhatsApp con whatsapp-web.js (dispositivo vinculado). EXPERIMENTAL, NO oficial.
#
#   -Estado                       Muestra procesos, STOP, cerrojo, sesión y contadores. No cambia nada.
#   -Vincular                     Arranca el conector para escanear el QR. No backend, no worker, no envíos.
#   -Observar -Segundos <n>       Solo recibe y registra eventos (60-900 s, contados desde 'ready'). No reenvía ni envía.
#                                 Si 'ready' no llega en 120 s, falla con error y no declara nada listo.
#   -Piloto -AutoReply -InteractionLimitUsd <usd> [-Destinatario <dígitos> | -AllIncoming] -Segundos <n>
#                                 Piloto autorizado: backend + worker + conector con respuestas activadas.
#   -Servicio -PublicInbound -AutoReply -InteractionLimitUsd <usd> -ConfirmarAtencionPublica ACTIVAR_ENTRADA_PUBLICA_WEB_BRIDGE
#                                 Atención pública continua por WhatsApp Web (no oficial). Sin ventana temporal.
#                                 No arranca con STOP. Requiere el token exacto. Se detiene con -Detener.
#   -Detener                      Cierra el conector (/kill), backend y worker de este proyecto, y crea STOP.
#   -Resumen [-Log <ruta>]        Resume un log de observación o piloto (solo conteos).
#
# Garantías:
#   * Nunca se borra .wwebjs_auth ni .env. Nunca se desvincula el dispositivo.
#   * STOP: -Detener lo crea para bloquear cualquier envío posterior. Retirarlo es decisión suya.
#   * El piloto exige -AutoReply, un límite por interacción >= 0.02 USD y un destinatario o -AllIncoming.
#   * Sin -Destinatario, el piloto no envía a nadie fuera de la lista del backend (modo de un solo chat).
#   * El modo universal (-AllIncoming) solo existe con -Piloto y caduca en un máximo de 15 minutos.
#   * Logs por ejecución en connector\whatsapp-webjs\state\logs\ (UTF-8, ignorados por Git).
#   * El QR se muestra en la consola y no se escribe en ningún archivo.
[CmdletBinding()]
param(
    [switch]$Estado,
    [switch]$Vincular,
    [switch]$Observar,
    [switch]$Piloto,
    [switch]$Detener,
    [switch]$Resumen,
    [string]$Log,
    [switch]$AutoReply,
    [switch]$AllIncoming,
    [switch]$Servicio,
    [switch]$PublicInbound,
    [string]$ConfirmarAtencionPublica,
    [string]$Destinatario,
    [decimal]$InteractionLimitUsd = 0,
    [int]$Segundos = 900
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$connector = Join-Path $repo "connector\whatsapp-webjs"
$stateDir = Join-Path $connector "state"
$logDir = Join-Path $stateDir "logs"
$stopFile = Join-Path $stateDir "STOP"
$lockFile = Join-Path $stateDir "webjs.lock"
$secretFile = Join-Path $stateDir "bridge-secret.txt"
$authDir = Join-Path $connector ".wwebjs_auth"
$entry = Join-Path $connector "dist\index.js"
$python = Join-Path $repo ".venv\Scripts\python.exe"
$bridgePort = 8787
$backendBase = 8765
$utf8 = New-Object Text.UTF8Encoding $false
$lineId = "573153863179"

function Get-UnixNow { [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() }

function Test-PortFree([int]$port) {
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $port)
    try { $listener.Start(); return $true } catch { return $false } finally { $listener.Stop() }
}

function Find-FreePort([int]$start) {
    for ($p = $start; $p -lt ($start + 50); $p++) { if (Test-PortFree $p) { return $p } }
    throw "No hay puertos libres a partir de $start."
}

# Procesos de ESTE proyecto (conector, backend o worker). Solo -Detener los termina.
function Get-ProjectProcesses {
    $pattern = 'dist[/\\]index\.js|app\.main:app|app\.worker\.main'
    @(Get-CimInstance Win32_Process -Filter "Name='node.exe' OR Name='python.exe'" |
        Where-Object { ([string]$_.CommandLine) -match $pattern })
}

function Get-WorkerProcesses {
    $candidates = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { ([string]$_.CommandLine) -match 'app\.worker\.main' })
    $ids = @($candidates | ForEach-Object { [int]$_.ProcessId })
    return @($candidates | Where-Object { $ids -notcontains [int]$_.ParentProcessId })
}

function Read-OrCreateSecret {
    New-Item -ItemType Directory -Force -Path $stateDir | Out-Null
    if (-not (Test-Path $secretFile)) {
        $bytes = New-Object byte[] 48
        $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
        try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
        [IO.File]::WriteAllText($secretFile, [Convert]::ToBase64String($bytes), $utf8)
    }
    $value = [IO.File]::ReadAllText($secretFile).Trim()
    if ($value.Length -lt 32) { throw "El archivo de secreto no es válido." }
    return $value
}

# Petición firmada al conector. Lanza si no responde.
function Invoke-Connector([string]$path, [string]$method, [string]$direction, [string]$secret) {
    $stamp = [string](Get-UnixNow)
    $hmac = New-Object System.Security.Cryptography.HMACSHA256 -ArgumentList (, [Text.Encoding]::UTF8.GetBytes($secret))
    try { $digest = $hmac.ComputeHash([Text.Encoding]::UTF8.GetBytes("$direction.$stamp.")) } finally { $hmac.Dispose() }
    $sig = [BitConverter]::ToString($digest).Replace("-", "").ToLowerInvariant()
    return Invoke-RestMethod -Uri "http://127.0.0.1:$bridgePort$path" -Method $method `
        -Headers @{ "X-Bridge-Timestamp" = $stamp; "X-Bridge-Signature" = $sig } -TimeoutSec 3
}

function Find-Chrome {
    if ($env:WEBJS_CHROME_PATH -and (Test-Path $env:WEBJS_CHROME_PATH)) { return $env:WEBJS_CHROME_PATH }
    foreach ($candidate in @(
        "C:\Program Files\Google\Chrome\Application\chrome.exe",
        "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")) {
        if (Test-Path $candidate) { return $candidate }
    }
    return $null
}

# Consola en UTF-8 (página de códigos 65001) para que el QR y los acentos se vean bien en Windows.
$consoleSetup = "chcp 65001 > `$null; [Console]::OutputEncoding = [Text.Encoding]::UTF8; `$OutputEncoding = [Text.Encoding]::UTF8; `$env:PYTHONIOENCODING = 'utf-8'; "

# Ventana para el CONECTOR. No hay tubería ni registro propio: el conector es el ÚNICO escritor de su archivo
# de log (WEBJS_LOG_FILE). Así no hay escrituras concurrentes y el QR llega íntegro a la consola.
function Start-Window([string]$title, [string]$workdir, [string]$command) {
    $script = $consoleSetup + "`$Host.UI.RawUI.WindowTitle = '$title'; Set-Location '$workdir'; $command"
    return Start-Process powershell -ArgumentList @("-NoExit", "-Command", $script) -PassThru
}

# Ventana para procesos Python (backend y worker). La ventana abre el archivo UNA vez y es el único escritor:
# cada línea se muestra y se escribe en UTF-8 sin BOM con el mismo manejador.
function Start-Logged([string]$title, [string]$workdir, [string]$command, [string]$logFile) {
    $open = "`$sw = New-Object IO.StreamWriter('$logFile', `$true, (New-Object Text.UTF8Encoding `$false)); `$sw.AutoFlush = `$true"
    $pipe = "*>&1 | ForEach-Object { `$t = [string]`$_; Write-Output `$t; `$sw.WriteLine(`$t) }; `$sw.Close()"
    $script = $consoleSetup + "`$Host.UI.RawUI.WindowTitle = '$title'; Set-Location '$workdir'; $open; $command $pipe"
    return Start-Process powershell -ArgumentList @("-NoExit", "-Command", $script) -PassThru
}

$previous = @{}
function Set-LaunchEnv([hashtable]$vars) {
    foreach ($k in $vars.Keys) {
        $previous[$k] = [Environment]::GetEnvironmentVariable($k, "Process")
        [Environment]::SetEnvironmentVariable($k, [string]$vars[$k], "Process")
    }
}
function Restore-LaunchEnv {
    foreach ($k in $previous.Keys) { [Environment]::SetEnvironmentVariable($k, $previous[$k], "Process") }
    $previous.Clear()
}

function Show-Summary([string]$file) {
    if (-not (Test-Path $file)) { throw "No existe el log: $file" }
    $raw = 0; $decisions = @{}; $skips = @{}; $forwards = 0; $sends = 0; $errors = 0; $states = @{}
    foreach ($line in (Get-Content -Path $file -Encoding UTF8)) {
        if ($line -match 'stage=00_message_received') { $raw++ }
        if ($line -match 'stage=01_classified .*?decision=(\w+)(?: reason=(\w+))?') {
            $key = if ($Matches[2]) { "$($Matches[1]):$($Matches[2])" } else { $Matches[1] }
            $decisions[$key] = [int]($decisions[$key]) + 1
        }
        if ($line -match 'stage=04_forward_skipped reason=(\w+)') { $skips[$Matches[1]] = [int]($skips[$Matches[1]]) + 1 }
        if ($line -match 'stage=02_backend_forward') { $forwards++ }
        if ($line -match 'stage=connection state=(\w+)') { $states[$Matches[1]] = [int]($states[$Matches[1]]) + 1 }
        if ($line -match 'stage=01_handler_error|stage=process error') { $errors++ }
    }
    Write-Host "== Resumen: $file"
    Write-Host "mensajes recibidos (stage 00): $raw"
    foreach ($k in ($decisions.Keys | Sort-Object)) { Write-Host ("clasificación {0}: {1}" -f $k, $decisions[$k]) }
    foreach ($k in ($skips.Keys | Sort-Object)) { Write-Host ("reenvío omitido {0}: {1}" -f $k, $skips[$k]) }
    Write-Host "reenvíos intentados al backend: $forwards"
    foreach ($k in ($states.Keys | Sort-Object)) { Write-Host ("transiciones de conexión {0}: {1}" -f $k, $states[$k]) }
    Write-Host "errores: $errors"
}

# ---------- Resumen ----------
if ($Resumen) {
    if (-not $Log) {
        $latest = Get-ChildItem -Path $logDir -Filter "webjs-*.log" -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($null -eq $latest) { throw "No hay logs del canal webjs en $logDir." }
        $Log = $latest.FullName
    }
    Show-Summary $Log
    exit 0
}

# ---------- Estado ----------
if ($Estado -or (-not $Vincular -and -not $Observar -and -not $Piloto -and -not $Servicio -and -not $Detener)) {
    $procs = @(Get-ProjectProcesses)
    Write-Host "== Estado del canal whatsapp-web.js"
    Write-Host ("STOP: {0}" -f $(if (Test-Path $stopFile) { "ACTIVO" } else { "ausente" }))
    Write-Host ("Cerrojo: {0}" -f $(if (Test-Path $lockFile) { "presente" } else { "libre" }))
    Write-Host ("Sesión guardada (.wwebjs_auth): {0}" -f $(if (Test-Path $authDir) { "existe" } else { "NO existe: ejecute -Vincular" }))
    Write-Host ("Navegador: {0}" -f $(if (Find-Chrome) { "encontrado" } else { "NO encontrado (fije WEBJS_CHROME_PATH)" }))
    Write-Host ("Procesos del proyecto vivos: {0}" -f $procs.Count)
    $procs | ForEach-Object { Write-Host ("  pid={0} {1}" -f $_.ProcessId, $_.Name) }
    if (Test-Path $secretFile) {
        try {
            $s = Invoke-Connector "/status" "GET" "status" (Read-OrCreateSecret)
            Write-Host ("Conector: connection={0} ready={1} modo={2} mensajes={3} reenviados={4} stop_bloqueados={5} errores={6}" -f `
                $s.connection, $s.ready, $s.mode, $s.message_events_total, $s.messages_forwarded_total, $s.stop_blocked_total, $s.handler_errors_total)
        } catch { Write-Host "Conector: no responde en el puerto $bridgePort." }
    }
    exit 0
}

# ---------- Detener ----------
if ($Detener) {
    New-Item -ItemType Directory -Force -Path $stateDir | Out-Null
    if (-not (Test-Path $stopFile)) { New-Item -ItemType File -Path $stopFile | Out-Null }
    Write-Host "STOP activado: no habrá reenvíos ni envíos."
    try {
        $r = Invoke-Connector "/kill" "POST" "control" (Read-OrCreateSecret)
        Write-Host "Conector: cierre solicitado (killed=$($r.killed))."
    } catch { Write-Host "Conector: no responde (quizá ya estaba cerrado)." }
    Start-Sleep -Seconds 3
    foreach ($p in @(Get-ProjectProcesses)) {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Host ("Proceso del proyecto cerrado: pid={0} {1}" -f $p.ProcessId, $p.Name)
    }
    Write-Host "Sesión conservada. La vinculación no se ha tocado."
    exit 0
}

# ---------- Comprobaciones comunes de arranque ----------
if (-not (Test-Path $entry)) { throw "Falta dist\index.js. Compile antes: cd connector\whatsapp-webjs; npm run build." }
if (Test-Path $lockFile) {
    $holder = 0
    [void][int]::TryParse(((Get-Content $lockFile -Raw).Trim()), [ref]$holder)
    if ($holder -gt 0 -and (Get-Process -Id $holder -ErrorAction SilentlyContinue)) {
        throw "Ya hay un conector con la sesión (pid $holder). Ciérrelo con -Detener antes de arrancar."
    }
}
if (@(Get-ProjectProcesses).Count -gt 0) { throw "Hay procesos del proyecto vivos. Revise -Estado o use -Detener." }
if (-not (Test-PortFree $bridgePort)) { throw "El puerto $bridgePort del conector está ocupado. No se arranca." }
$chrome = Find-Chrome
if (-not $chrome) { throw "No se encuentra Chrome ni Edge. Fije WEBJS_CHROME_PATH." }
$secret = Read-OrCreateSecret
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$runStamp = (Get-Date).ToString("yyyyMMdd-HHmmss")

# Variables comunes del conector. El secreto viaja por el entorno heredado, nunca en la línea de comandos.
$connectorVars = @{
    WEB_BRIDGE_SECRET   = $secret
    WEB_BRIDGE_PORT     = "$bridgePort"
    WEBJS_CHROME_PATH   = $chrome
    WEBJS_AUTH_DIR      = ".wwebjs_auth"
    WEBJS_STATE_DIR     = "state"
    WEBJS_LOG_DIR       = "state/logs"
}

# ---------- Vincular ----------
if ($Vincular) {
    if (Test-Path $authDir) { Write-Host "Ya existe una sesión guardada: el conector la reutilizará sin QR." }
    $logFile = Join-Path $logDir "webjs-vincular-$runStamp.log"
    $vars = $connectorVars + @{ WEBJS_MODE = "live"; WEB_BRIDGE_AUTO_REPLY = "false"; WEB_BRIDGE_BACKEND_URL = "http://127.0.0.1:$backendBase"; WEBJS_LOG_FILE = $logFile }
    Set-LaunchEnv $vars
    try { $null = Start-Window "LaCuisine webjs vinculación" $connector "& node dist/index.js" } finally { Restore-LaunchEnv }
    Write-Host "Vincular no reenvía ni envía nada (sin backend y sin respuestas), por eso STOP no lo bloquea."
    Write-Host "== Vinculación"
    Write-Host "Escanee el QR de la ventana 'LaCuisine webjs vinculación' desde WhatsApp > Dispositivos vinculados."
    Write-Host "Cuando el log indique state=ready, ciérrelo con -Detener. Log: $logFile"
    exit 0
}

# ---------- Observar ----------
if ($Observar) {
    if (-not (Test-Path $authDir)) { throw "No hay sesión guardada. Ejecute -Vincular primero." }
    if ($Segundos -lt 60 -or $Segundos -gt 900) { throw "La observación debe durar entre 60 y 900 segundos." }
    $logFile = Join-Path $logDir "webjs-observar-$runStamp.log"
    # La ventana útil cuenta desde que el conector confirma 'ready', no desde el arranque.
    $readyLimit = 120
    $vars = $connectorVars + @{ WEBJS_MODE = "observe"; WEB_BRIDGE_AUTO_REPLY = "false"; WEBJS_OBSERVE_SECONDS = [string]$Segundos; WEBJS_READY_TIMEOUT_SEC = [string]$readyLimit; WEB_BRIDGE_BACKEND_URL = "http://127.0.0.1:$backendBase"; WEBJS_LOG_FILE = $logFile }
    Set-LaunchEnv $vars
    try { $null = Start-Window "LaCuisine webjs observación" $connector "& node dist/index.js" } finally { Restore-LaunchEnv }
    Write-Host "== Observación: esperando la confirmación 'ready' (plazo $readyLimit s). Nada se declara listo antes."
    $confirmed = $null
    $waitUntil = (Get-Date).AddSeconds($readyLimit + 20)
    while ($null -eq $confirmed -and (Get-Date) -lt $waitUntil) {
        Start-Sleep -Seconds 2
        try { $s = Invoke-Connector "/status" "GET" "status" $secret; if ($s.ready -eq $true) { $confirmed = $s } } catch { }
        if ($null -eq $confirmed -and (Test-Path $logFile) -and (Select-String -Path $logFile -Pattern 'stage=ready state=timeout|stage=init state=failed' -Quiet)) { break }
    }
    if ($null -eq $confirmed) {
        try { $null = Invoke-Connector "/kill" "POST" "control" $secret } catch { }
        throw "No se confirmó 'ready': la sesión no es operativa. No hay ventana de observación. Log: $logFile"
    }
    $closes = (Get-Date).AddSeconds($Segundos)
    Write-Host ("== Observación lista: ready confirmado (connection={0}). Ventana útil de {1} s, hasta {2}. No responde ni reenvía." -f $confirmed.connection, $Segundos, $closes.ToString('HH:mm:ss'))
    Write-Host "Envíe UN mensaje desde otro teléfono. Log: $logFile"
    Write-Host "Resumen: .\scripts\whatsapp_webjs.ps1 -Resumen -Log `"$logFile`""
    exit 0
}

# ---------- Servicio de atención pública (sin ventana temporal) ----------
# Solo con el token exacto. Nunca arranca con STOP. Sin ventana: se detiene con -Detener (que crea STOP).
if ($Servicio) {
    if (-not $PublicInbound) { throw "El servicio requiere -PublicInbound." }
    if ($ConfirmarAtencionPublica -cne "ACTIVAR_ENTRADA_PUBLICA_WEB_BRIDGE") { throw "Falta la confirmación exacta de atención pública (-ConfirmarAtencionPublica)." }
    if (-not $AutoReply) { throw "El servicio requiere -AutoReply." }
    if ($InteractionLimitUsd -lt 0.02) { throw "Defina -InteractionLimitUsd (mínimo 0.02 USD)." }
    if (Test-Path $stopFile) { throw "Existe state\STOP (apagado). El servicio no arranca mientras exista." }
    if (-not (Test-Path $authDir)) { throw "No hay sesión guardada. Ejecute -Vincular primero." }

    $backendPort = Find-FreePort $backendBase
    $common = @{
        WEB_BRIDGE_SECRET           = $secret
        WHATSAPP_TRANSPORT          = "web_bridge"
        WEB_BRIDGE_URL              = "http://127.0.0.1:$bridgePort"
        WEB_BRIDGE_LINE_ID          = $lineId
        WHATSAPP_AUDIENCE_MODE      = "public_inbound"
        WHATSAPP_ALLOWED_RECIPIENTS = ""
        WEB_BRIDGE_AUTO_REPLY       = "true"
        WEB_BRIDGE_INBOUND_MODE     = "public_inbound"
        WEB_BRIDGE_PUBLIC_CONFIRMATION = $ConfirmarAtencionPublica
        LLM_PROVIDER                = "anthropic"
        ANTHROPIC_ENABLED           = "true"
        AGENT_DRY_RUN               = "false"
        ANTHROPIC_MODEL             = "claude-sonnet-5-5"
        ANTHROPIC_PRICING_MODEL     = "claude-sonnet-5-5"
        DEMO_ENABLED                = "false"
        AGENT_KILL_SWITCH           = "false"
        AGENT_INTERACTION_LIMIT_USD = [string]$InteractionLimitUsd
    }
    $backendLog = Join-Path $logDir "webjs-servicio-backend-$runStamp.log"
    $workerLog = Join-Path $logDir "webjs-servicio-worker-$runStamp.log"
    $connLog = Join-Path $logDir "webjs-servicio-conector-$runStamp.log"

    Set-LaunchEnv ($common + @{ LACUSINE_LOAD_DOTENV = "true" })
    try { $null = Start-Logged "LaCuisine servicio backend" $repo "& '$python' -u -m uvicorn app.main:app --host 127.0.0.1 --port $backendPort" $backendLog } finally { Restore-LaunchEnv }
    $healthy = $false
    for ($i = 0; $i -lt 60 -and -not $healthy; $i++) {
        Start-Sleep -Seconds 1
        try { $healthy = (Invoke-WebRequest -Uri "http://127.0.0.1:$backendPort/health" -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200 } catch { $healthy = $false }
    }
    if (-not $healthy) { throw "El backend no responde en $backendPort. Revise $backendLog." }

    Set-LaunchEnv ($common + @{ LACUSINE_LOAD_DOTENV = "true"; WORKER_ENABLED = "true" })
    try { $null = Start-Logged "LaCuisine servicio worker" $repo "& '$python' -u -m app.worker.main" $workerLog } finally { Restore-LaunchEnv }

    $connVars = $connectorVars + @{
        WEBJS_MODE = "live"; WEB_BRIDGE_AUTO_REPLY = "true"; WEB_BRIDGE_INBOUND_MODE = "public_inbound"
        WEB_BRIDGE_PUBLIC_CONFIRMATION = $ConfirmarAtencionPublica
        WEB_BRIDGE_BACKEND_URL = "http://127.0.0.1:$backendPort"; WEBJS_LOG_FILE = $connLog
    }
    Set-LaunchEnv $connVars
    try { $null = Start-Window "LaCuisine servicio conector" $connector "& node dist/index.js" } finally { Restore-LaunchEnv }

    $connOk = $false
    for ($i = 0; $i -lt 120 -and -not $connOk; $i++) {
        Start-Sleep -Seconds 1
        try { $s = Invoke-Connector "/status" "GET" "status" $secret; $connOk = ($s.connection -eq "open") } catch { $connOk = $false }
    }
    # Nunca se declara éxito sin evidencia: conector READY y worker vivo con su arranque registrado.
    # Si algo falla, se cierran los procesos de este proyecto (sin crear STOP, para poder reintentar).
    try {
        if (-not $connOk) { throw "El conector no está READY tras 120 s. El servicio NO se declara en marcha. Revise $connLog" }
        Start-Sleep -Seconds 5
        $workers = @(Get-WorkerProcesses)
        $workerStarted = (Test-Path $workerLog) -and (Select-String -Path $workerLog -Pattern "worker_started" -SimpleMatch -Quiet)
        if ($workers.Count -ne 1 -or -not $workerStarted) {
            throw "El worker no está activo (procesos=$($workers.Count), arranque registrado=$workerStarted). El servicio NO se declara en marcha. Revise $workerLog"
        }
    } catch {
        foreach ($p in @(Get-ProjectProcesses)) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
        throw
    }
    Write-Host "== Servicio de atención pública en marcha (sin ventana temporal)."
    Write-Host ("Estado verificado: backend=OK conector=READY worker=UNO modo=public_inbound")
    Write-Host "Logs: $backendLog | $workerLog | $connLog"
    Write-Host "Para detener (crea STOP y cierra todo): .\scripts\whatsapp_webjs.ps1 -Detener"
    exit 0
}

# ---------- Piloto autorizado ----------
if ($Piloto) {
    if (-not $AutoReply) { throw "El piloto requiere -AutoReply. Sin él no se envía nada." }
    if ($InteractionLimitUsd -lt 0.02) { throw "Defina -InteractionLimitUsd (mínimo 0.02 USD)." }
    if (Test-Path $stopFile) { throw "Existe state\STOP (apagado). El piloto no arranca mientras exista; retírelo solo con autorización." }
    if (-not (Test-Path $authDir)) { throw "No hay sesión guardada. Ejecute -Vincular primero." }
    if (-not $AllIncoming -and -not $Destinatario) { throw "Indique -Destinatario <dígitos> o -AllIncoming (modo universal, máximo 15 min)." }
    if ($AllIncoming -and $Segundos -gt 900) { throw "El modo universal no puede durar más de 900 segundos." }
    if ($Segundos -lt 60 -or $Segundos -gt 900) { throw "La ventana debe durar entre 60 y 900 segundos." }
    if ($Destinatario -and $Destinatario -notmatch '^[0-9]{6,20}$') { throw "-Destinatario debe ser solo dígitos." }

    $backendPort = Find-FreePort $backendBase
    $until = (Get-UnixNow) + $Segundos
    $inbound = if ($AllIncoming) { "web_inbound_all" } else { "single_chat" }
    $allowed = if ($Destinatario) { $Destinatario } else { "" }
    $common = @{
        WEB_BRIDGE_SECRET           = $secret
        WHATSAPP_TRANSPORT          = "web_bridge"
        WEB_BRIDGE_URL              = "http://127.0.0.1:$bridgePort"
        WEB_BRIDGE_LINE_ID          = $lineId
        WHATSAPP_AUDIENCE_MODE      = "test_allowlist"
        WHATSAPP_ALLOWED_RECIPIENTS = $allowed
        WEB_BRIDGE_AUTO_REPLY       = "true"
        WEB_BRIDGE_INBOUND_MODE     = $inbound
        LLM_PROVIDER                = "anthropic"
        ANTHROPIC_ENABLED           = "true"
        AGENT_DRY_RUN               = "false"
        ANTHROPIC_MODEL             = "claude-sonnet-5-5"
        ANTHROPIC_PRICING_MODEL     = "claude-sonnet-5-5"
        DEMO_ENABLED                = "false"
        AGENT_KILL_SWITCH           = "false"
        AGENT_INTERACTION_LIMIT_USD = [string]$InteractionLimitUsd
    }
    if ($AllIncoming) { $common["WEB_BRIDGE_INBOUND_UNTIL"] = [string]$until }
    $backendLog = Join-Path $logDir "webjs-backend-$runStamp.log"
    $workerLog = Join-Path $logDir "webjs-worker-$runStamp.log"
    $connLog = Join-Path $logDir "webjs-piloto-$runStamp.log"

    Set-LaunchEnv ($common + @{ LACUSINE_LOAD_DOTENV = "true" })
    try { $null = Start-Logged "LaCuisine webjs backend" $repo "& '$python' -u -m uvicorn app.main:app --host 127.0.0.1 --port $backendPort" $backendLog } finally { Restore-LaunchEnv }
    $healthy = $false
    for ($i = 0; $i -lt 60 -and -not $healthy; $i++) {
        Start-Sleep -Seconds 1
        try { $healthy = (Invoke-WebRequest -Uri "http://127.0.0.1:$backendPort/health" -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200 } catch { $healthy = $false }
    }
    if (-not $healthy) { throw "El backend no responde en $backendPort. Revise $backendLog." }

    Set-LaunchEnv ($common + @{ LACUSINE_LOAD_DOTENV = "true"; WORKER_ENABLED = "true" })
    try { $null = Start-Logged "LaCuisine webjs worker" $repo "& '$python' -u -m app.worker.main" $workerLog } finally { Restore-LaunchEnv }

    $connVars = $connectorVars + @{
        WEBJS_MODE = "live"; WEB_BRIDGE_AUTO_REPLY = "true"; WEB_BRIDGE_INBOUND_MODE = $inbound
        WEB_BRIDGE_BACKEND_URL = "http://127.0.0.1:$backendPort"; WEBJS_LOG_FILE = $connLog
    }
    # Cierre programado del conector también en un solo chat: -Segundos siempre limita la prueba.
    $connVars["WEB_BRIDGE_INBOUND_UNTIL"] = [string]$until
    Set-LaunchEnv $connVars
    try { $null = Start-Window "LaCuisine webjs conector" $connector "& node dist/index.js" } finally { Restore-LaunchEnv }

    # Verificación: lo que el worker REGISTRA al construir sus componentes, y el estado real del conector.
    Write-Host "== Verificación"
    $profile = $null
    for ($i = 0; $i -lt 60 -and -not $profile; $i++) {
        Start-Sleep -Seconds 1
        if (Test-Path $workerLog) {
            $hit = Select-String -Path $workerLog -Pattern "worker_profile " -SimpleMatch | Select-Object -Last 1
            if ($hit) { $profile = [string]$hit.Line }
        }
    }
    $field = { param($k) if ($profile -match "(?:^|\s)$k=(\S+)") { $Matches[1] } else { $null } }
    $senderClass = & $field "sender_class"
    $provider = & $field "provider"
    $dryRun = & $field "dry_run"
    $transport = & $field "whatsapp_transport"
    Write-Host ("worker: provider={0} dry_run={1} whatsapp_transport={2} sender_class={3}" -f $provider, $dryRun, $transport, $senderClass)
    $connOk = $false
    for ($i = 0; $i -lt 120 -and -not $connOk; $i++) {
        Start-Sleep -Seconds 1
        try { $s = Invoke-Connector "/status" "GET" "status" $secret; $connOk = ($s.connection -eq "open") } catch { $connOk = $false }
    }
    Start-Sleep -Seconds 5
    $workers = @(Get-WorkerProcesses)
    $profileOk = ($provider -ceq "anthropic") -and ($dryRun -ceq "False") -and ($transport -ceq "web_bridge") -and ($senderClass -ceq "WebBridgeSender")
    Write-Host ("Estado: backend={0} conector={1} worker={2} LLM={3} sender={4}" -f `
        $(if ($healthy) { "OK" } else { "NO" }), $(if ($connOk) { "READY" } else { "NO_LISTO" }),
        $(if ($workers.Count -eq 1) { "UNO" } else { "REVISAR($($workers.Count))" }),
        $(if ($profileOk) { "anthropic_real" } else { "REVISAR" }), $(if ($senderClass -ceq "WebBridgeSender") { "WebBridgeSender" } else { "REVISAR" }))
    Write-Host ("Ventana hasta {0} (hora local). Logs: {1} | {2} | {3}" -f (Get-Date).AddSeconds($Segundos).ToString("HH:mm:ss"), $backendLog, $workerLog, $connLog)
    Write-Host "Para cerrar: .\scripts\whatsapp_webjs.ps1 -Detener"
    exit 0
}

throw "Acción no reconocida."
