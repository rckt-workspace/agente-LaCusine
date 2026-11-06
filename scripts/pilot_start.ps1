# Arranque controlado del piloto de La Cuisine: UN worker con Anthropic real.
#
# Por defecto SOLO INSPECCIONA: lista workers y ejecuta el preflight (solo lectura). No detiene ni arranca nada.
#   -StopOldWorkers  termina SOLO los workers de este repo (arbol completo) y ESPERA a que no quede ninguno.
#                    Si alguno sigue vivo tras el plazo, aborta.
#   -Start           aborta si queda cualquier worker de este repo vivo; si no, arranca UN worker y comprueba
#                    que exista exactamente uno. Hacerlo habilita envios reales a WhatsApp.
#
# Nunca imprime claves, tokens ni contrasenas.

param(
    [switch]$StopOldWorkers,
    [switch]$Start,
    [string]$WorkerId = "lacusine-pilot-1",
    [string]$ExpectedModel = "claude-sonnet-5-5"
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo ".venv\Scripts\python.exe"
$workersTool = Join-Path $repo "scripts\pilot_workers.py"
$preflightTool = Join-Path $repo "scripts\pilot_preflight.py"
$logDir = Join-Path $repo "logs"

if (-not (Test-Path $python)) { throw "No existe el entorno virtual: $python" }

Push-Location $repo
try {
    Write-Host "== 1. Workers de La Cuisine (solo de este repo)"
    & $python $workersTool list --repo $repo
    if ($LASTEXITCODE -ne 0) { Write-Host "No se pudo listar los workers."; exit 1 }

    if ($StopOldWorkers) {
        Write-Host "== 2. Parada verificada de workers antiguos"
        & $python $workersTool stop-wait --repo $repo --timeout 45
        if ($LASTEXITCODE -ne 0) { Write-Host "ABORTADO: un worker no termino. No se arranca."; exit 1 }
    }

    if ($Start) {
        Write-Host "== 3. Comprobacion: no debe quedar ningun worker vivo"
        & $python $workersTool check --repo $repo --expect 0
        if ($LASTEXITCODE -ne 0) {
            Write-Host "ABORTADO: ya hay un worker de La Cuisine vivo. Usa -StopOldWorkers -Start."
            exit 1
        }
    }

    Write-Host "== 4. Variables del piloto (solo en este proceso)"
    $env:LACUSINE_LOAD_DOTENV = "true"
    $env:WORKER_ENABLED = "true"
    $env:WORKER_ID = $WorkerId
    $env:LLM_PROVIDER = "anthropic"
    $env:ANTHROPIC_ENABLED = "true"
    $env:AGENT_DRY_RUN = "false"
    $env:ANTHROPIC_MODEL = $ExpectedModel
    $env:META_SEND_ENABLED = "true"
    $env:WHATSAPP_DRY_RUN = "false"
    $env:AGENT_KILL_SWITCH = "false"

    Write-Host "== 5. Preflight (solo lectura, sin Anthropic ni envios)"
    $pfOut = & $python $preflightTool --expect-model $ExpectedModel
    $pfExit = $LASTEXITCODE
    $pfOut | Write-Host
    $pfRecipients = $null
    foreach ($line in $pfOut) { if ($line -match 'destinatarios_autorizados=(\d+)') { $pfRecipients = [int]$Matches[1] } }
    if ($pfExit -ne 0) { Write-Host "Arranque cancelado por el preflight."; exit 1 }

    if (-not $Start) {
        Write-Host "Preflight correcto. Para arrancar: repite con -Start (y -StopOldWorkers si hay workers)."
        exit 0
    }

    Write-Host "== 6. Arranque de UN worker"
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $out = Join-Path $logDir "worker-$stamp.out.log"
    $err = Join-Path $logDir "worker-$stamp.err.log"
    $proc = Start-Process -FilePath $python -ArgumentList "-m", "app.worker.main" -WorkingDirectory $repo `
        -RedirectStandardOutput $out -RedirectStandardError $err -WindowStyle Hidden -PassThru
    Write-Host "   lanzador PID $($proc.Id) - registro: $err"
    Write-Host "== 7. Comprobacion posterior: el worker arranca, sigue vivo y es el unico"
    $started = $false
    for ($i = 0; $i -lt 45; $i++) {
        Start-Sleep -Seconds 1
        if (-not (Get-Process -Id $proc.Id -ErrorAction SilentlyContinue)) {
            Write-Host "ABORTADO: el lanzador termino antes de iniciar el worker. Revisa $err"
            exit 1
        }
        if (Select-String -Path $err -Pattern "worker_started" -Quiet -ErrorAction SilentlyContinue) { $started = $true; break }
    }
    if (-not $started) { Write-Host "AVISO: no aparecio worker_started en 45 s. Revisa $err antes de enviar nada."; exit 1 }
    & $python $workersTool check --repo $repo --expect 1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "AVISO: el numero de workers no es 1. Revisa con 'pilot_workers.py list' antes de enviar nada."
        exit 1
    }
    $profileLine = Select-String -Path $err -Pattern "worker_profile" -ErrorAction SilentlyContinue | Select-Object -Last 1
    if ($profileLine) { Write-Host "   $($profileLine.Line)" } else { Write-Host "   AVISO: aun no aparece worker_profile en el registro." }
    if ($profileLine -and $pfRecipients -ne $null) {
        $workerRecipients = $null
        if ($profileLine.Line -match 'recipients_allowlisted=(\d+)') { $workerRecipients = [int]$Matches[1] }
        if ($workerRecipients -ne $pfRecipients) {
            Write-Host "ABORTADO: el worker tiene $workerRecipients destinatarios y el .env tiene $pfRecipients. Deteniendo el worker."
            & $python $workersTool stop-wait --repo $repo --timeout 45 | Out-Null
            exit 1
        }
        Write-Host "   Destinatarios verificados: $workerRecipients (coinciden con el .env)"
    }
    $recovered = Select-String -Path $err -Pattern "worker_recovered_expired" -ErrorAction SilentlyContinue | Select-Object -Last 1
    if ($recovered) { Write-Host "   Recuperacion al arrancar: $($recovered.Line)" } else { Write-Host "   Sin trabajos vencidos que recuperar." }
} finally {
    Pop-Location
}
