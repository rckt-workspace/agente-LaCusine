#
# Diagnóstico de RECEPCIÓN del conector WhatsApp Web en modo OBSERVACIÓN. No responde, no reenvía, no envía.
#
#   (sin nada)              Arranca la observación (ventana de 15 min) y muestra contadores hasta cerrarse.
#   -Segundos <n>           Duración de la ventana (60 a 900 s).
#   -Cerrar                 Pide el cierre limpio de la observación en curso (POST /kill firmado).
#   -Resumen [-Log <ruta>]  Resume un log de observación: solo conteos, nunca contenido.
#
# Garantías:
#   * Solo arranca el conector. No arranca backend, worker ni despachador. No llama a Anthropic, Supabase ni Meta.
#   * WEB_BRIDGE_AUTO_REPLY=false y WEB_BRIDGE_OBSERVE_ONLY=true: el conector no reenvía ni envía nada.
#   * state\STOP se crea si falta y NUNCA se elimina desde este script.
#   * No toca .auth ni .env. El secreto HMAC viaja por el entorno heredado, no por la línea de comandos.
#   * Un único conector por sesión: se niega si otro conector o un cerrojo con proceso vivo ya la usan.
#   * La ventana termina sola (el propio conector cierra) o con -Cerrar. Ambos cierres liberan el cerrojo.
#   * Logs en connector\whatsapp-web\state\logs\observe-<fecha>.log (UTF-8, ignorados por Git).
[CmdletBinding()]
param(
    [int]$Segundos = 900,
    [switch]$Cerrar,
    [switch]$Resumen,
    [string]$Log
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$connector = Join-Path $repo "connector\whatsapp-web"
$stateDir = Join-Path $connector "state"
$logDir = Join-Path $stateDir "logs"
$stopFile = Join-Path $stateDir "STOP"
$secretFile = Join-Path $stateDir "bridge-secret.txt"
$lockFile = Join-Path $stateDir "observe.lock"
$entry = Join-Path $connector "dist\index.js"
$bridgePort = 8787
$backendPlaceholder = "http://127.0.0.1:8765"
$utf8 = New-Object Text.UTF8Encoding $false

function Get-UnixNow { [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() }

function Test-PortFree([int]$port) {
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $port)
    try { $listener.Start(); return $true } catch { return $false } finally { $listener.Stop() }
}

# Conectores del proyecto vivos (observación o lanzador normal).
function Get-ConnectorProcesses {
    @(Get-CimInstance Win32_Process -Filter "Name='node.exe'" |
        Where-Object { ([string]$_.CommandLine) -match 'dist[/\\]index\.js' })
}

# Petición firmada al puente local (mismo esquema que el conector y el lanzador).
function Invoke-Bridge([string]$path, [string]$method, [string]$direction, [string]$secret) {
    $stamp = [string](Get-UnixNow)
    $hmac = New-Object System.Security.Cryptography.HMACSHA256 -ArgumentList (, [Text.Encoding]::UTF8.GetBytes($secret))
    try { $digest = $hmac.ComputeHash([Text.Encoding]::UTF8.GetBytes("$direction.$stamp.")) } finally { $hmac.Dispose() }
    $sig = [BitConverter]::ToString($digest).Replace("-", "").ToLowerInvariant()
    return Invoke-RestMethod -Uri "http://127.0.0.1:$bridgePort$path" -Method $method `
        -Headers @{ "X-Bridge-Timestamp" = $stamp; "X-Bridge-Signature" = $sig } -TimeoutSec 3
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

# Resumen de un log: solo conteos. No imprime líneas del log.
function Show-Summary([string]$file) {
    if (-not (Test-Path $file)) { throw "No existe el log: $file" }
    $raw = 0; $events = @{}; $byType = @{}; $decisions = @{}; $skips = @{}; $forwards = 0; $handlerErrors = 0
    foreach ($line in (Get-Content -Path $file -Encoding UTF8)) {
        if ($line -match 'stage=00_raw_upsert type=(\w+) count=(\d+)') {
            $raw++
            $events[$Matches[1]] = [int]($events[$Matches[1]]) + 1
            $byType[$Matches[1]] = [int]($byType[$Matches[1]]) + [int]$Matches[2]
        }
        if ($line -match 'stage=01_classified .*?decision=(\w+)(?: reason=(\w+))?') {
            $key = if ($Matches[2]) { "$($Matches[1]):$($Matches[2])" } else { $Matches[1] }
            $decisions[$key] = [int]($decisions[$key]) + 1
        }
        if ($line -match 'stage=04_forward_skipped reason=(\w+)') { $skips[$Matches[1]] = [int]($skips[$Matches[1]]) + 1 }
        if ($line -match 'stage=02_backend_forward_started') { $forwards++ }
        if ($line -match 'stage=01_handler_error') { $handlerErrors++ }
    }
    Write-Host "== Resumen de observación: $file"
    Write-Host "eventos brutos (stage 00): $raw"
    foreach ($k in ($events.Keys | Sort-Object)) { Write-Host ("  upsert type={0}: eventos={1} mensajes={2}" -f $k, $events[$k], $byType[$k]) }
    foreach ($k in ($decisions.Keys | Sort-Object)) { Write-Host ("clasificación {0}: {1}" -f $k, $decisions[$k]) }
    foreach ($k in ($skips.Keys | Sort-Object)) { Write-Host ("reenvío omitido {0}: {1}" -f $k, $skips[$k]) }
    Write-Host "envíos al backend (debe ser 0 en observación): $forwards"
    Write-Host "errores del manejador: $handlerErrors"
}

if ($Resumen) {
    if (-not $Log) {
        $latest = Get-ChildItem -Path $logDir -Filter "observe-*.log" -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($null -eq $latest) { throw "No hay logs de observación en $logDir." }
        $Log = $latest.FullName
    }
    Show-Summary $Log
    exit 0
}

if ($Cerrar) {
    $secret = Read-OrCreateSecret
    try {
        $r = Invoke-Bridge "/kill" "POST" "control" $secret
        Write-Host "Cierre solicitado al conector (killed=$($r.killed)). El proceso se cerrará en breve."
    } catch {
        Write-Host "No hay conector respondiendo en el puerto $bridgePort ($($_.Exception.GetType().Name))."
    }
    exit 0
}

# ---------- Arranque ----------
if ($Segundos -lt 60 -or $Segundos -gt 900) { throw "La ventana debe durar entre 60 y 900 segundos." }
if (-not (Test-Path $entry)) { throw "Falta dist\index.js. Compile antes: cd connector\whatsapp-web; npm run build." }

# 1. Instancia única: ningún conector vivo y ningún cerrojo con proceso vivo.
$alive = @(Get-ConnectorProcesses)
if ($alive.Count -gt 0) { throw "Ya hay $($alive.Count) conector(es) vivo(s) con la sesión. No se arranca la observación." }
if (Test-Path $lockFile) {
    $holder = 0
    [void][int]::TryParse(((Get-Content $lockFile -Raw).Trim()), [ref]$holder)
    if ($holder -gt 0 -and (Get-Process -Id $holder -ErrorAction SilentlyContinue)) {
        throw "Existe un cerrojo de observación con proceso vivo ($holder). No se arranca."
    }
    Write-Host "Cerrojo obsoleto encontrado (proceso inexistente). Lo reemplazará el conector."
}
if (-not (Test-PortFree $bridgePort)) { throw "El puerto $bridgePort está ocupado. No se arranca." }

# 2. STOP: se asegura que exista y se conserva. Nunca se elimina desde este script.
New-Item -ItemType Directory -Force -Path $stateDir | Out-Null
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
if (-not (Test-Path $stopFile)) {
    New-Item -ItemType File -Path $stopFile | Out-Null
    Write-Host "state\STOP creado: el conector no reenviará ni enviará nada."
} else {
    Write-Host "state\STOP presente (se conserva)."
}

$secret = Read-OrCreateSecret
$untilSec = (Get-UnixNow) + $Segundos
$runStamp = (Get-Date).ToString("yyyyMMdd-HHmmss")
$log = Join-Path $logDir "observe-$runStamp.log"

# 3. Entorno: solo para el proceso del conector. Se restaura al terminar.
$vars = @{
    WEB_BRIDGE_SECRET        = $secret
    WEB_BRIDGE_AUTO_REPLY    = "false"
    WEB_BRIDGE_OBSERVE_ONLY  = "true"
    WEB_BRIDGE_OBSERVE_UNTIL = [string]$untilSec
    WEB_BRIDGE_PORT          = "$bridgePort"
    WEB_BRIDGE_BACKEND_URL   = $backendPlaceholder
    WEB_BRIDGE_INBOUND_MODE  = "single_chat"
}
$previous = @{}
foreach ($k in $vars.Keys) {
    $previous[$k] = [Environment]::GetEnvironmentVariable($k, "Process")
    [Environment]::SetEnvironmentVariable($k, [string]$vars[$k], "Process")
}
try {
    # Cada línea se muestra y se añade al log en UTF-8 sin BOM, para poder consultarla después.
    $command = "& node dist/index.js *>&1 | ForEach-Object { `$t = [string]`$_; Write-Output `$t; [IO.File]::AppendAllText('$log', `$t + [Environment]::NewLine, (New-Object Text.UTF8Encoding `$false)) }"
    $script = "`$Host.UI.RawUI.WindowTitle = 'LaCuisine observación WhatsApp Web'; Set-Location '$connector'; $command"
    $null = Start-Process powershell -ArgumentList @("-NoExit", "-Command", $script) -PassThru
} finally {
    foreach ($k in $previous.Keys) { [Environment]::SetEnvironmentVariable($k, $previous[$k], "Process") }
}

Write-Host "== Observación iniciada"
Write-Host ("Ventana: hasta {0} (hora local). Máximo 15 min. Log: {1}" -f (Get-Date).AddSeconds($Segundos).ToString("HH:mm:ss"), $log)
Write-Host "Envíe UN mensaje de texto desde el segundo teléfono a la línea de prueba. Este modo NO responde."

# 4. Espera de conexión y seguimiento. Solo la ventana (o -Cerrar) termina la observación.
$status = $null
for ($i = 0; $i -lt 90 -and $null -eq $status; $i++) {
    Start-Sleep -Seconds 1
    try { $status = Invoke-Bridge "/status" "GET" "status" $secret } catch { $status = $null }
}
if ($null -eq $status) { Write-Host "El conector no responde. Revise el log: $log" } else { Write-Host "conexion=$($status.connection) observe_only=$($status.observe_only)" }

while ((Get-UnixNow) -lt $untilSec) {
    Start-Sleep -Seconds 30
    if (@(Get-ConnectorProcesses).Count -eq 0) { Write-Host "El conector ya no está vivo. Fin de la observación."; break }
    try {
        $s = Invoke-Bridge "/status" "GET" "status" $secret
        Write-Host ("[{0}] conexion={1} raw_upserts_total={2} notify_received_total={3} append_ignored_total={4} stop_blocked_total={5} handler_errors_total={6} restante_s={7}" -f `
            (Get-Date).ToString("HH:mm:ss"), $s.connection, $s.raw_upserts_total, $s.notify_received_total,
            $s.append_ignored_total, $s.stop_blocked_total, $s.handler_errors_total, ([int64]$untilSec - (Get-UnixNow)))
    } catch { Write-Host "Sin respuesta del conector en este instante." }
}

Write-Host ""
if (Test-Path $stopFile) { Write-Host "state\STOP sigue presente." } else { Write-Host "AVISO: state\STOP ya no existe. Revise antes de cualquier otra prueba." }
Show-Summary $log
