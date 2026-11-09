# Control del conector EXPERIMENTAL de WhatsApp Web (dispositivo vinculado). NO oficial. Ver connector\whatsapp-web\README.md.
#
#   start   Abre una ventana nueva con el conector. El QR aparece en ESA ventana, nunca en este registro.
#           Requiere $env:WEB_BRIDGE_SECRET (32+ caracteres) definido en esta sesión. No se imprime.
#   status  Estado de conexión y modos (sin datos personales).
#   stop    Apagado inmediato: crea state\STOP (bloquea reinicios) y llama a /kill. La sesión queda cerrada.
#   unlink  Desvincula ESTE dispositivo. Requiere -Confirm. No borra la cuenta de WhatsApp.
#
# Nunca imprime secretos, números, texto de mensajes ni credenciales.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("start", "status", "stop", "unlink")]
    [string]$Action,
    [switch]$Confirm,
    [int]$Port = 8787
)

$ErrorActionPreference = "Stop"
$connector = Join-Path (Split-Path -Parent $PSScriptRoot) "connector\whatsapp-web"
$stateDir = Join-Path $connector "state"
$base = "http://127.0.0.1:$Port"

function Get-Secret {
    $secret = $env:WEB_BRIDGE_SECRET
    if (-not $secret -or $secret.Length -lt 32) { throw "Defina WEB_BRIDGE_SECRET (32+ caracteres) en esta sesión de PowerShell." }
    return $secret
}

function Get-Signature([string]$secret, [string]$direction, [string]$stamp, [string]$body) {
    $key = [Text.Encoding]::UTF8.GetBytes($secret)
    $hmac = New-Object System.Security.Cryptography.HMACSHA256 (, $key)
    try {
        $bytes = $hmac.ComputeHash([Text.Encoding]::UTF8.GetBytes("$direction.$stamp.$body"))
    } finally { $hmac.Dispose() }
    return (($bytes | ForEach-Object { $_.ToString("x2") }) -join "")
}

function Invoke-Bridge([string]$method, [string]$path, [string]$direction, [string]$body = "") {
    $secret = Get-Secret
    $stamp = [string][int][double]::Parse((Get-Date -UFormat %s))
    $headers = @{ "X-Bridge-Timestamp" = $stamp; "X-Bridge-Signature" = (Get-Signature $secret $direction $stamp $body) }
    try {
        if ($method -eq "GET") {
            return Invoke-RestMethod -Method Get -Uri "$base$path" -Headers $headers -TimeoutSec 10
        }
        return Invoke-RestMethod -Method Post -Uri "$base$path" -Headers $headers -Body $body -ContentType "application/json" -TimeoutSec 10
    } catch [System.Net.WebException] {
        return $null
    } catch {
        if ($_.Exception.Response) { throw "El puente respondió con error HTTP $([int]$_.Exception.Response.StatusCode)." }
        return $null
    }
}

switch ($Action) {
    "start" {
        Get-Secret | Out-Null  # solo valida que exista; nunca se muestra
        if (-not (Test-Path (Join-Path $connector "dist\index.js"))) {
            Push-Location $connector
            try { & npm run build | Out-Null } finally { Pop-Location }
        }
        # El proceso hijo hereda WEB_BRIDGE_SECRET del entorno; el secreto no aparece en la línea de comandos.
        $command = "Set-Location '$connector'; node dist/index.js"
        Start-Process powershell -ArgumentList @("-NoExit", "-Command", $command) | Out-Null
        Write-Host "Conector iniciado en una ventana nueva. Escanee el QR desde WhatsApp > Dispositivos vinculados."
        Write-Host "Respuestas automáticas: desactivadas salvo WEB_BRIDGE_AUTO_REPLY=true en esa sesión."
    }
    "status" {
        $response = Invoke-Bridge "GET" "/status" "status"
        if ($null -eq $response) { Write-Host "El conector no está en ejecución."; exit 1 }
        Write-Host ("conexion={0} respuestas_automaticas={1} apagado={2}" -f $response.connection, $response.auto_reply, $response.killed)
    }
    "stop" {
        New-Item -ItemType Directory -Force -Path $stateDir | Out-Null
        New-Item -ItemType File -Force -Path (Join-Path $stateDir "STOP") | Out-Null
        $response = Invoke-Bridge "POST" "/kill" "control" ""
        if ($null -eq $response) { Write-Host "Archivo STOP creado. El conector no estaba en ejecución." }
        else { Write-Host "Apagado: sesión cerrada y envíos bloqueados. Para reactivar, borre state\STOP." }
    }
    "unlink" {
        if (-not $Confirm) { throw "Desvincular requiere -Confirm. No borra la cuenta de WhatsApp: solo este dispositivo vinculado." }
        $response = Invoke-Bridge "POST" "/unlink" "control" ""
        if ($null -eq $response) { throw "El conector no está en ejecución: desvincúlelo desde WhatsApp > Dispositivos vinculados." }
        Write-Host "Dispositivo desvinculado. Credenciales locales eliminadas. La cuenta de WhatsApp sigue activa."
    }
}
