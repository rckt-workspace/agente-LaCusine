# Verificación reproducible de un respaldo lógico de Supabase La Cuisine.
#
# Restaura el respaldo en un PostgreSQL TEMPORAL y AISLADO (contenedor Docker creado por este script)
# y compara conteos y estructura con el propio respaldo. Solo declara éxito si todo coincide.
#
# Garantías:
#   - No se conecta a Supabase Cloud ni lee .env ni ninguna DATABASE_URL.
#   - El contenedor no publica puertos y no tiene red (--network none).
#   - La contraseña del contenedor es aleatoria, vive en un archivo temporal que se borra al instante,
#     y nunca se imprime ni se escribe en el repositorio.
#   - Espera a que el servidor DEFINITIVO esté estable (la imagen Supabase arranca un servidor temporal
#     de inicialización que se apaga; conectarse a él provoca "terminating connection due to administrator command").
#   - Roles, esquema y datos se ejecutan con ON_ERROR_STOP=1 y se comprueba el código de salida de cada uno.
#   - Solo elimina el contenedor que este script creó (nombre generado y registrado).
#   - Ante un fallo conserva evidencia diagnóstica SANITIZADA (sin contraseñas, sin filas de datos).
#   - El respaldo se lee, nunca se modifica. Los archivos temporales de trabajo se borran al terminar.
#
# Alcance: se restauran roles, esquema completo y datos del esquema `public`.
#   Los bloques COPY de `auth` y `storage` NO se restauran: el esquema de GoTrue/Storage de la imagen local
#   es distinto del de Cloud (p. ej. auth.audit_log_entries.ip_address) y su contenido lo gestiona Supabase.
#   Ver docs/respaldo-y-restauracion.md.
#
# Uso:
#   .\scripts\verify_cloud_backup.ps1 -BackupDir "C:\Users\maura\SecureBackups\lacuisine\20261108-184212"
#   Código de salida 0 = verificado. Distinto de 0 = NO verificado (ver evidencia y mensaje).

param(
    [Parameter(Mandatory = $true)]
    [string]$BackupDir,
    [string]$Image = "public.ecr.aws/supabase/postgres:17.11.0.011",
    [string]$EvidenceRoot = "",
    [int]$ReadyTimeoutSec = 180,
    [switch]$KeepContainer
)

$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$randomSuffix = -join ((48..57 + 97..102) | Get-Random -Count 6 | ForEach-Object { [char]$_ })
$containerName = "lacuisine-restore-$stamp-$randomSuffix"
$remoteDir = "/tmp/restore"
$work = Join-Path $env:TEMP ("lacuisine-verify-" + $stamp + "-" + $randomSuffix)
$createdContainer = $false
$envFile = $null
$failures = New-Object System.Collections.Generic.List[string]
$evidenceDir = $null

function Write-Step([string]$text) { Write-Host "== $text" }

function Fail([string]$message) {
    Write-Host "FALLO: $message" -ForegroundColor Red
    $script:failures.Add($message)
}

function Invoke-Docker([string[]]$arguments) {
    # Devuelve @{ Code = <int>; Out = <string[]> }. Sin ErrorRecords: stderr se captura como texto.
    $out = & docker @arguments 2>&1 | ForEach-Object { "$_" }
    return @{ Code = $LASTEXITCODE; Out = @($out) }
}

function Get-SanitizedLines([string[]]$lines, [int]$max = 200) {
    # Conserva solo cabeceras de error y eventos del servidor. Descarta DETAIL/CONTEXT/LINE (pueden contener datos)
    # y cualquier línea que pueda contener secretos.
    $kept = New-Object System.Collections.Generic.List[string]
    foreach ($line in $lines) {
        $relevant = ($line -match "(ERROR|FATAL|PANIC|WARNING):") -or ($line -match "(shut|terminat|restart|ready to accept|init process|ready for start up|starting PostgreSQL)")
        if (-not $relevant) { continue }
        if ($line -match "PASSWORD|postgres(ql)?://|secret|jwt|token|key=") { $kept.Add("[linea omitida: posible secreto]"); continue }
        $kept.Add($line)
        if ($kept.Count -ge $max) { break }
    }
    return $kept
}

function Save-Evidence([string]$reason) {
    # Se guarda una sola vez por ejecución: la primera causa es la que importa.
    try {
        if (-not $script:evidenceDir -or $script:evidenceSaved) { return }
        $script:evidenceSaved = $true
        New-Item -ItemType Directory -Force -Path $script:evidenceDir | Out-Null
        $summary = @("Motivo: $reason", "Fallos registrados:") + @($script:failures | ForEach-Object { "  - $_" })
        $summary += @("Imagen: $Image", "Fecha: $(Get-Date -Format o)")
        if ($createdContainer) {
            $inspect = Invoke-Docker @("inspect", "-f", "status={{.State.Status}} exitcode={{.State.ExitCode}} restarts={{.RestartCount}} oomkilled={{.State.OOMKilled}}", $containerName)
            $summary += ($inspect.Out)
            $logs = Invoke-Docker @("logs", $containerName)
            $summary += "--- Eventos del servidor (sanitizados)"
            $summary += (Get-SanitizedLines $logs.Out)
        }
        $summary += "--- Errores de psql (sanitizados)"
        foreach ($f in @("roles.log", "schema.log", "data-public.log", "counts.log")) {
            $local = Join-Path $script:work $f
            if (Test-Path $local) {
                $summary += "[$f]"
                $summary += (Get-SanitizedLines (Get-Content -LiteralPath $local -Encoding UTF8))
            }
        }
        $summary | Set-Content -LiteralPath (Join-Path $script:evidenceDir "diagnostico-sanitizado.txt") -Encoding UTF8
        Write-Host "Evidencia sanitizada: $script:evidenceDir"
    } catch {
        Write-Host "No se pudo guardar la evidencia (no contiene secretos al fallar)." -ForegroundColor Yellow
    }
}

try {
    Push-Location $repo
    New-Item -ItemType Directory -Force -Path $work | Out-Null

    Write-Step "1. Requisitos"
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw "Docker no está instalado o no está en PATH." }
    $info = Invoke-Docker @("info", "--format", "{{.ServerVersion}}")
    if ($info.Code -ne 0) { throw "El demonio de Docker no responde. Arranca Docker Desktop." }
    Write-Host "Docker $($info.Out[0])"

    $imageCheck = Invoke-Docker @("image", "inspect", $Image)
    if ($imageCheck.Code -ne 0) {
        Write-Host "Imagen no local; descargando $Image"
        $pull = Invoke-Docker @("pull", $Image)
        if ($pull.Code -ne 0) { throw "No se pudo descargar la imagen $Image." }
    }

    $BackupDir = (Resolve-Path -LiteralPath $BackupDir).Path
    if ($BackupDir.StartsWith($repo, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "El respaldo está dentro del repositorio. Debe guardarse fuera de Git."
    }
    foreach ($f in @("roles.sql", "schema.sql", "data.sql")) {
        $p = Join-Path $BackupDir $f
        if (-not (Test-Path -LiteralPath $p) -or (Get-Item -LiteralPath $p).Length -eq 0) { throw "Falta o está vacío: $f" }
    }

    $manifest = Join-Path $BackupDir "backup-manifest.sha256"
    if (Test-Path -LiteralPath $manifest) {
        foreach ($line in Get-Content -LiteralPath $manifest) {
            if ($line -notmatch "^([0-9a-fA-F]{64})\s+\*?(\S+)$") { continue }
            $expectedHash = $matches[1].ToLowerInvariant()
            $fileName = $matches[2]
            $target = Join-Path $BackupDir $fileName
            if (-not (Test-Path -LiteralPath $target)) { throw "El manifiesto cita un archivo inexistente: $fileName" }
            $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $target).Hash.ToLowerInvariant()
            if ($actual -ne $expectedHash) { throw "El hash de $fileName no coincide con el manifiesto. El respaldo puede estar alterado." }
        }
        Write-Host "Manifiesto SHA-256 verificado."
    } else {
        Write-Host "AVISO: no existe backup-manifest.sha256; no se verifica integridad por hash." -ForegroundColor Yellow
    }

    if (-not $EvidenceRoot) { $EvidenceRoot = Join-Path (Split-Path -Parent $BackupDir) "verificaciones" }
    $script:evidenceDir = Join-Path $EvidenceRoot "$stamp-$randomSuffix"

    Write-Step "2. Preparar datos de restauración (archivo temporal, sin cambiar el respaldo)"
    $dataLines = [System.IO.File]::ReadAllLines((Join-Path $BackupDir "data.sql"), [System.Text.Encoding]::UTF8)
    $utf8 = New-Object System.Text.UTF8Encoding $false
    $filtered = New-Object System.Collections.Generic.List[string]
    $expected = [ordered]@{}
    $skipping = $false
    $inCopy = $null
    $rowCount = 0
    foreach ($line in $dataLines) {
        if ($inCopy) {
            if ($line -eq '\.') { $expected[$inCopy] = $rowCount; $inCopy = $null } else { $rowCount++ }
            if (-not $skipping) { $filtered.Add($line) }
            continue
        }
        if ($skipping) { if ($line -eq '\.') { $skipping = $false }; continue }
        if ($line -match '^COPY "(auth|storage)"\.') { $skipping = $true; continue }
        if ($line -match '^COPY "public"\."([a-z_]+)"') { $inCopy = $matches[1]; $rowCount = 0 }
        $filtered.Add($line)
    }
    $dataPublic = Join-Path $work "data-public.sql"
    [System.IO.File]::WriteAllLines($dataPublic, $filtered, $utf8)
    Copy-Item -LiteralPath (Join-Path $BackupDir "roles.sql") -Destination (Join-Path $work "roles.sql")
    Copy-Item -LiteralPath (Join-Path $BackupDir "schema.sql") -Destination (Join-Path $work "schema.sql")
    $schemaText = Get-Content -LiteralPath (Join-Path $BackupDir "schema.sql") -Raw -Encoding UTF8
    $expectedTables = ([regex]::Matches($schemaText, '(?m)^CREATE TABLE')).Count
    $expectedPolicies = ([regex]::Matches($schemaText, '(?m)^CREATE POLICY')).Count
    Write-Host "Tablas públicas en el respaldo: $($expected.Count) bloques COPY; tablas en el esquema: $expectedTables; políticas: $expectedPolicies"

    Write-Step "3. Crear contenedor temporal aislado"
    $pwBytes = New-Object byte[] 24
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($pwBytes)
    $pw = ([System.BitConverter]::ToString($pwBytes) -replace "-", "").ToLowerInvariant()
    $envFile = Join-Path $work "container.env"
    [System.IO.File]::WriteAllText($envFile, "POSTGRES_PASSWORD=$pw`n", $utf8)
    $pw = $null
    $run = Invoke-Docker @("run", "-d", "--name", $containerName, "--network", "none", "--env-file", $envFile, $Image)
    Remove-Item -LiteralPath $envFile -Force
    if ($run.Code -ne 0) { throw "No se pudo crear el contenedor temporal." }
    $createdContainer = $true
    Write-Host "Contenedor temporal creado: $containerName (sin red, sin puertos)"

    Write-Step "4. Esperar al servidor DEFINITIVO y estable"
    $deadline = (Get-Date).AddSeconds($ReadyTimeoutSec)
    $ready = $false
    while ((Get-Date) -lt $deadline) {
        $logs = (Invoke-Docker @("logs", $containerName)).Out
        $marker = -1
        for ($i = 0; $i -lt $logs.Count; $i++) { if ($logs[$i] -match "PostgreSQL init process complete") { $marker = $i } }
        if ($marker -ge 0) {
            $after = @($logs[$marker..($logs.Count - 1)] | Where-Object { $_ -match "database system is ready to accept connections" })
            if ($after.Count -gt 0) { $ready = $true; break }
        }
        Start-Sleep -Seconds 2
    }
    if (-not $ready) { throw "El servidor definitivo no arrancó en $ReadyTimeoutSec s." }
    $stableValues = @()
    for ($k = 0; $k -lt 3; $k++) {
        $q = Invoke-Docker @("exec", $containerName, "psql", "-U", "postgres", "-d", "postgres", "-X", "-tAc", "select pg_postmaster_start_time()")
        if ($q.Code -ne 0) { throw "El servidor no responde de forma estable." }
        $stableValues += $q.Out[0]
        Start-Sleep -Seconds 2
    }
    if (($stableValues | Select-Object -Unique).Count -ne 1) { throw "El proceso postmaster se reinició durante la comprobación de estabilidad." }
    Write-Host "Servidor definitivo estable."

    Write-Step "5. Restaurar con ON_ERROR_STOP=1 (rol postgres, igual que en Cloud)"
    Invoke-Docker @("exec", $containerName, "mkdir", "-p", $remoteDir) | Out-Null
    $steps = @(
        @{ Name = "roles"; Local = (Join-Path $work "roles.sql") },
        @{ Name = "schema"; Local = (Join-Path $work "schema.sql") },
        @{ Name = "data-public"; Local = $dataPublic }
    )
    foreach ($step in $steps) {
        $remoteSql = "$remoteDir/$($step.Name).sql"
        $remoteLog = "$remoteDir/$($step.Name).log"
        $cp = Invoke-Docker @("cp", $step.Local, "${containerName}:$remoteSql")
        if ($cp.Code -ne 0) { throw "No se pudo copiar $($step.Name) al contenedor." }
        $inner = "psql -U postgres -d postgres -X -q -v ON_ERROR_STOP=1 -f $remoteSql > $remoteLog 2>&1; echo `$?"
        $res = Invoke-Docker @("exec", $containerName, "sh", "-c", $inner)
        $code = $res.Out[-1]
        Invoke-Docker @("cp", "${containerName}:$remoteLog", (Join-Path $work "$($step.Name).log")) | Out-Null
        if ($code -ne "0") {
            Fail "restauración de $($step.Name) terminó con código $code"
            throw "La restauración se detuvo en '$($step.Name)'. No se declara éxito."
        }
        Write-Host "OK: $($step.Name)"
    }

    Write-Step "6. Comparar conteos y estructura con el respaldo"
    $countSql = @()
    foreach ($t in $expected.Keys) {
        $countSql += "select '$t', count(*) from public.$t"
    }
    $countSqlText = ($countSql -join " union all ") + ";"
    $countLocal = Join-Path $work "counts.sql"
    [System.IO.File]::WriteAllText($countLocal, $countSqlText + "`n", $utf8)
    Invoke-Docker @("cp", $countLocal, "${containerName}:$remoteDir/counts.sql") | Out-Null
    $countRes = Invoke-Docker @("exec", $containerName, "sh", "-c", "psql -U postgres -d postgres -X -At -F : -v ON_ERROR_STOP=1 -f $remoteDir/counts.sql > $remoteDir/counts.out 2>&1; echo `$?")
    Invoke-Docker @("cp", "${containerName}:$remoteDir/counts.out", (Join-Path $work "counts.log")) | Out-Null
    if ($countRes.Out[-1] -ne "0") { Fail "no se pudieron contar las filas restauradas"; Save-Evidence "conteo"; throw "Conteo fallido." }
    $actual = @{}
    foreach ($row in (Get-Content -LiteralPath (Join-Path $work "counts.log") -Encoding UTF8)) {
        if ($row -match "^([a-z_]+):(\d+)$") { $actual[$matches[1]] = [int]$matches[2] }
    }

    $report = New-Object System.Collections.Generic.List[object]
    foreach ($t in $expected.Keys) {
        $ok = ($actual.ContainsKey($t) -and $actual[$t] -eq $expected[$t])
        if (-not $ok) { Fail "conteo de ${t}: respaldo=$($expected[$t]) restaurado=$($actual[$t])" }
        $report.Add([pscustomobject]@{ Tabla = $t; Respaldo = $expected[$t]; Restaurado = $actual[$t]; Coincide = $ok })
    }
    $report | Format-Table -AutoSize | Out-String | Write-Host

    $structSql = "select 'tables', count(*) from pg_tables where schemaname='public' union all select 'policies', count(*) from pg_policies where schemaname='public';"
    [System.IO.File]::WriteAllText((Join-Path $work "struct.sql"), $structSql + "`n", $utf8)
    Invoke-Docker @("cp", (Join-Path $work "struct.sql"), "${containerName}:$remoteDir/struct.sql") | Out-Null
    $structRes = Invoke-Docker @("exec", $containerName, "sh", "-c", "psql -U postgres -d postgres -X -At -F : -f $remoteDir/struct.sql > $remoteDir/struct.out 2>&1; echo `$?")
    if ($structRes.Out[-1] -ne "0") { Fail "no se pudo leer la estructura restaurada" }
    else {
        $structOut = Invoke-Docker @("exec", $containerName, "cat", "$remoteDir/struct.out")
        foreach ($row in $structOut.Out) {
            if ($row -match "^tables:(\d+)$" -and [int]$matches[1] -ne $expectedTables) { Fail "tablas públicas: esquema=$expectedTables restauradas=$($matches[1])" }
            if ($row -match "^policies:(\d+)$" -and [int]$matches[1] -ne $expectedPolicies) { Fail "políticas públicas: esquema=$expectedPolicies restauradas=$($matches[1])" }
            if ($row -match "^(tables|policies):") { Write-Host "Estructura $row" }
        }
    }

    if ($failures.Count -gt 0) {
        Save-Evidence "verificación de conteos o estructura"
        throw "La restauración no coincide con el respaldo."
    }

    New-Item -ItemType Directory -Force -Path $script:evidenceDir | Out-Null
    $report | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath (Join-Path $script:evidenceDir "conteos.json") -Encoding UTF8
    Write-Host ""
    Write-Host "VERIFICADO: roles, esquema y datos de public restaurados y coincidentes con el respaldo." -ForegroundColor Green
    Write-Host "Alcance: NO incluye auth.* ni storage.* (ver cabecera del script)."
    $exitCode = 0
}
catch {
    Fail $_.Exception.Message
    Save-Evidence "ejecución interrumpida"
    $exitCode = 1
}
finally {
    if ($envFile -and (Test-Path -LiteralPath $envFile)) { Remove-Item -LiteralPath $envFile -Force }
    if ($createdContainer -and -not $KeepContainer) {
        # Solo se elimina el contenedor que este script creó (nombre generado aquí).
        Invoke-Docker @("rm", "-f", $containerName) | Out-Null
        Write-Host "Contenedor temporal eliminado: $containerName"
    }
    if (Test-Path -LiteralPath $work) { Remove-Item -LiteralPath $work -Recurse -Force }
    Pop-Location
}

exit $exitCode
