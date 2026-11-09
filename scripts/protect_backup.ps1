# Cifrado de respaldos lógicos de Supabase La Cuisine con dos ranuras de clave independientes.
#
# Formato:
#   backup.enc          "LCBK1" | IV(16) | AES-256-CBC(ZIP) | HMAC-SHA256(todo lo anterior)
#   keyslot-dpapi.bin   clave de 64 bytes protegida con DPAPI (CurrentUser). Solo se descifra en ESTE perfil de Windows.
#   keyslot-recovery.bin  la misma clave envuelta con la clave de recuperación (AES-256-CBC + HMAC).
#   La clave de recuperación (64 bytes, base64) NO se guarda junto al respaldo: se indica con -RecoveryKeyPath
#   y debe custodiarse FUERA del equipo, separada del archivo cifrado.
#
# Garantías:
#   - No pide contraseñas ni las almacena. No escribe secretos en el repositorio ni en la salida.
#   - Nunca modifica ni borra el respaldo original.
#   - Modo Encrypt: cifra, y después verifica en aislamiento con AMBAS ranuras comparando hashes con el manifiesto.
#     Si algo falla, no declara éxito.
#   - Modo Verify: descifra en una carpeta temporal aislada (fuera del repositorio) y valida hashes.
#
# Limitación: keyslot-dpapi.bin solo puede descifrarse con el mismo usuario de Windows en este equipo.
#   La ranura de recuperación es la única vía si el perfil se pierde.
#
# Uso:
#   .\scripts\protect_backup.ps1 -Mode Encrypt -BackupDir "C:\...\20261108-184212" `
#       -OutputDir "C:\...\cifrados\20261108-184212" -RecoveryKeyPath "C:\...\RECOVERY-lacuisine-20261108.txt"
#   .\scripts\protect_backup.ps1 -Mode Verify -OutputDir "C:\...\cifrados\20261108-184212" -Slot dpapi
#   .\scripts\protect_backup.ps1 -Mode Verify -OutputDir "C:\...\cifrados\20261108-184212" -Slot recovery `
#       -RecoveryKeyPath "C:\...\RECOVERY-lacuisine-20261108.txt"

param(
    [Parameter(Mandatory = $true)][ValidateSet("Encrypt", "Verify")][string]$Mode,
    [string]$BackupDir = "",
    [string]$OutputDir = "",
    [string]$RecoveryKeyPath = "",
    [ValidateSet("dpapi", "recovery")][string]$Slot = "dpapi",
    [string]$RestoreTo = ""
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$magic = "LCBK1"
$recoveryHeader = "LACUSINE-RECOVERY-KEY-V1"
$dpapiEntropy = [Text.Encoding]::UTF8.GetBytes("lacuisine-backup-v1")
$utf8NoBom = New-Object System.Text.UTF8Encoding $false
$requiredFiles = @("roles.sql", "schema.sql", "data.sql")

Add-Type -AssemblyName System.Security

function New-RandomBytes([int]$count) {
    $bytes = New-Object byte[] $count
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return , $bytes
}

function Get-Slice([byte[]]$source, [int]$start, [int]$length) {
    $out = New-Object byte[] $length
    [Array]::Copy($source, $start, $out, 0, $length)
    return , $out
}

function Join-Bytes([byte[][]]$parts) {
    $ms = New-Object System.IO.MemoryStream
    try {
        foreach ($p in $parts) { $ms.Write($p, 0, $p.Length) }
        return , $ms.ToArray()
    } finally { $ms.Dispose() }
}

function Test-SameBytes([byte[]]$a, [byte[]]$b) {
    # Comparación en tiempo constante para MAC.
    if ($a.Length -ne $b.Length) { return $false }
    $diff = 0
    for ($i = 0; $i -lt $a.Length; $i++) { $diff = $diff -bor ($a[$i] -bxor $b[$i]) }
    return ($diff -eq 0)
}

function Protect-Aes([byte[]]$plain, [byte[]]$key64) {
    # key64: bytes 0..31 = cifrado, 32..63 = MAC.
    $encKey = Get-Slice $key64 0 32
    $macKey = Get-Slice $key64 32 32
    $iv = New-RandomBytes 16
    $aes = [System.Security.Cryptography.Aes]::Create()
    try {
        $aes.KeySize = 256; $aes.Mode = "CBC"; $aes.Padding = "PKCS7"
        $aes.Key = $encKey; $aes.IV = $iv
        $encryptor = $aes.CreateEncryptor()
        $cipher = $encryptor.TransformFinalBlock($plain, 0, $plain.Length)
    } finally { $aes.Dispose() }
    $head = Join-Bytes @([Text.Encoding]::ASCII.GetBytes($magic), $iv, $cipher)
    $hmac = New-Object System.Security.Cryptography.HMACSHA256 -ArgumentList (, $macKey)
    try { $mac = $hmac.ComputeHash($head) } finally { $hmac.Dispose() }
    return , (Join-Bytes @($head, $mac))
}

function Unprotect-Aes([byte[]]$blob, [byte[]]$key64) {
    $encKey = Get-Slice $key64 0 32
    $macKey = Get-Slice $key64 32 32
    $headerLen = $magic.Length + 16
    if ($blob.Length -lt ($headerLen + 32 + 16)) { throw "archivo cifrado demasiado corto" }
    $authenticated = Get-Slice $blob 0 ($blob.Length - 32)
    $mac = Get-Slice $blob ($blob.Length - 32) 32
    $hmac = New-Object System.Security.Cryptography.HMACSHA256 -ArgumentList (, $macKey)
    try { $expected = $hmac.ComputeHash($authenticated) } finally { $hmac.Dispose() }
    if (-not (Test-SameBytes $expected $mac)) { throw "integridad: el MAC no coincide (clave incorrecta o archivo alterado)" }
    if ([Text.Encoding]::ASCII.GetString($blob, 0, $magic.Length) -ne $magic) { throw "formato desconocido" }
    $iv = Get-Slice $blob $magic.Length 16
    $cipher = Get-Slice $authenticated $headerLen ($authenticated.Length - $headerLen)
    $aes = [System.Security.Cryptography.Aes]::Create()
    try {
        $aes.KeySize = 256; $aes.Mode = "CBC"; $aes.Padding = "PKCS7"
        $aes.Key = $encKey; $aes.IV = $iv
        $decryptor = $aes.CreateDecryptor()
        return , $decryptor.TransformFinalBlock($cipher, 0, $cipher.Length)
    } finally { $aes.Dispose() }
}

function Read-RecoveryKey([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) { throw "no existe la clave de recuperación indicada" }
    $lines = [System.IO.File]::ReadAllLines($path, [Text.Encoding]::UTF8)
    if ($lines.Count -lt 2 -or $lines[0] -ne $recoveryHeader) { throw "archivo de clave de recuperación no válido" }
    $key = [Convert]::FromBase64String($lines[1].Trim())
    if ($key.Length -ne 64) { throw "longitud de clave de recuperación no válida" }
    return , $key
}

function Test-ManifestIn([string]$dir) {
    $manifest = Join-Path $dir "backup-manifest.sha256"
    if (-not (Test-Path -LiteralPath $manifest)) { throw "falta backup-manifest.sha256" }
    $count = 0
    foreach ($line in Get-Content -LiteralPath $manifest) {
        if ($line -notmatch "^([0-9a-fA-F]{64})\s+\*?(\S+)$") { continue }
        $target = Join-Path $dir $matches[2]
        if (-not (Test-Path -LiteralPath $target)) { throw "el manifiesto cita un archivo inexistente" }
        $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $target).Hash.ToLowerInvariant()
        if ($actual -ne $matches[1].ToLowerInvariant()) { throw "hash distinto en $($matches[2])" }
        $count++
    }
    if ($count -lt $requiredFiles.Count) { throw "el manifiesto no cubre todos los archivos" }
    foreach ($f in $requiredFiles) { if (-not (Test-Path -LiteralPath (Join-Path $dir $f))) { throw "falta $f" } }
    return $count
}

function Assert-OutsideRepo([string]$path, [string]$label) {
    $full = [IO.Path]::GetFullPath($path)
    $repoFull = [IO.Path]::GetFullPath($repo).TrimEnd("\")
    if ($full.StartsWith($repoFull, [StringComparison]::OrdinalIgnoreCase)) { throw "$label está dentro del repositorio Git" }
}

function Get-Keyslot-Plain([string]$outDir, [string]$slotName, [string]$recoveryFile) {
    if ($slotName -eq "dpapi") {
        $wrapped = [IO.File]::ReadAllBytes((Join-Path $outDir "keyslot-dpapi.bin"))
        return , [System.Security.Cryptography.ProtectedData]::Unprotect($wrapped, $dpapiEntropy, [System.Security.Cryptography.DataProtectionScope]::CurrentUser)
    }
    $kek = Read-RecoveryKey $recoveryFile
    $wrapped = [IO.File]::ReadAllBytes((Join-Path $outDir "keyslot-recovery.bin"))
    return , (Unprotect-Aes $wrapped $kek)
}

function Restore-And-Verify([string]$outDir, [string]$slotName, [string]$recoveryFile, [string]$target) {
    $keyMaterial = Get-Keyslot-Plain $outDir $slotName $recoveryFile
    if ($keyMaterial.Length -ne 64) { throw "clave de datos con longitud no válida" }
    $blob = [IO.File]::ReadAllBytes((Join-Path $outDir "backup.enc"))
    $zipBytes = Unprotect-Aes $blob $keyMaterial
    New-Item -ItemType Directory -Force -Path $target | Out-Null
    $zipPath = Join-Path $target "payload.zip"
    [IO.File]::WriteAllBytes($zipPath, $zipBytes)
    $extract = Join-Path $target "restored"
    Expand-Archive -LiteralPath $zipPath -DestinationPath $extract -Force
    Remove-Item -LiteralPath $zipPath -Force
    return (Test-ManifestIn $extract)
}

try {
    if ($Mode -eq "Encrypt") {
        Write-Host "== Cifrado de respaldo"
        if (-not $BackupDir -or -not $OutputDir -or -not $RecoveryKeyPath) { throw "Encrypt requiere -BackupDir, -OutputDir y -RecoveryKeyPath" }
        $BackupDir = (Resolve-Path -LiteralPath $BackupDir).Path
        Assert-OutsideRepo $BackupDir "el respaldo"
        Assert-OutsideRepo $OutputDir "la salida"
        Assert-OutsideRepo $RecoveryKeyPath "la clave de recuperación"
        $outFull = [IO.Path]::GetFullPath($OutputDir)
        $keyFull = [IO.Path]::GetFullPath($RecoveryKeyPath)
        if ($keyFull.StartsWith($outFull, [StringComparison]::OrdinalIgnoreCase) -or $keyFull.StartsWith($BackupDir, [StringComparison]::OrdinalIgnoreCase)) {
            throw "la clave de recuperación debe guardarse fuera del respaldo y de la salida"
        }
        if (Test-Path -LiteralPath $RecoveryKeyPath) { throw "la clave de recuperación ya existe; no se sobrescribe" }
        if ((Test-Path -LiteralPath $OutputDir) -and @(Get-ChildItem -LiteralPath $OutputDir -Force).Count -gt 0) {
            throw "la carpeta de salida no está vacía; no se sobrescribe"
        }

        $files = Test-ManifestIn $BackupDir
        Write-Host "Manifiesto verificado en origen: $files archivos"

        $work = Join-Path $env:TEMP ("lc-protect-" + [guid]::NewGuid().ToString("N"))
        New-Item -ItemType Directory -Path $work | Out-Null
        $keyMaterial = $null; $kek = $null
        try {
            $staged = Join-Path $work "staged"
            New-Item -ItemType Directory -Path $staged | Out-Null
            foreach ($f in $requiredFiles + @("backup-manifest.sha256")) {
                Copy-Item -LiteralPath (Join-Path $BackupDir $f) -Destination $staged
            }
            $zipPath = Join-Path $work "payload.zip"
            Compress-Archive -Path (Join-Path $staged "*") -DestinationPath $zipPath -CompressionLevel Optimal
            $plainZip = [IO.File]::ReadAllBytes($zipPath)

            $keyMaterial = New-RandomBytes 64
            $blob = Protect-Aes $plainZip $keyMaterial

            New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
            [IO.File]::WriteAllBytes((Join-Path $OutputDir "backup.enc"), $blob)

            $dpapi = [System.Security.Cryptography.ProtectedData]::Protect($keyMaterial, $dpapiEntropy, [System.Security.Cryptography.DataProtectionScope]::CurrentUser)
            [IO.File]::WriteAllBytes((Join-Path $OutputDir "keyslot-dpapi.bin"), $dpapi)

            $kek = New-RandomBytes 64
            $wrapped = Protect-Aes $keyMaterial $kek
            [IO.File]::WriteAllBytes((Join-Path $OutputDir "keyslot-recovery.bin"), $wrapped)
            $recoveryText = $recoveryHeader + "`n" + [Convert]::ToBase64String($kek) + "`n"
            [IO.File]::WriteAllText($RecoveryKeyPath, $recoveryText, $utf8NoBom)

            $blobHash = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $OutputDir "backup.enc")).Hash.ToLowerInvariant()
            $info = @(
                "formato: $magic (AES-256-CBC + HMAC-SHA256)",
                "creado: $(Get-Date -Format o)",
                "archivos: roles.sql, schema.sql, data.sql, backup-manifest.sha256",
                "sha256 backup.enc: $blobHash",
                "ranuras: keyslot-dpapi.bin (este perfil de Windows), keyslot-recovery.bin (clave de recuperación externa)",
                "alcance: public (roles, esquema y datos). NO incluye auth.*, storage.* ni archivos externos."
            )
            [IO.File]::WriteAllLines((Join-Path $OutputDir "info.txt"), $info, $utf8NoBom)
            Write-Host "Cifrado escrito en: $OutputDir"
        }
        finally {
            if ($keyMaterial) { [Array]::Clear($keyMaterial, 0, $keyMaterial.Length) }
            if ($kek) { [Array]::Clear($kek, 0, $kek.Length) }
            if (Test-Path -LiteralPath $work) { Remove-Item -LiteralPath $work -Recurse -Force }
        }

        Write-Host "== Verificación aislada con ranura DPAPI"
        $t1 = Join-Path $env:TEMP ("lc-verify-" + [guid]::NewGuid().ToString("N"))
        try {
            $n1 = Restore-And-Verify $OutputDir "dpapi" "" $t1
            Write-Host "OK: $n1 hashes coinciden"
        } finally { if (Test-Path -LiteralPath $t1) { Remove-Item -LiteralPath $t1 -Recurse -Force } }

        Write-Host "== Verificación aislada con ranura de recuperación"
        $t2 = Join-Path $env:TEMP ("lc-verify-" + [guid]::NewGuid().ToString("N"))
        try {
            $n2 = Restore-And-Verify $OutputDir "recovery" $RecoveryKeyPath $t2
            Write-Host "OK: $n2 hashes coinciden"
        } finally { if (Test-Path -LiteralPath $t2) { Remove-Item -LiteralPath $t2 -Recurse -Force } }

        Write-Host "ENCRIPTADO Y VERIFICADO. El respaldo original NO se ha modificado ni eliminado."
        Write-Host "Mueve la clave de recuperación fuera de este equipo y no la guardes junto a backup.enc."
        exit 0
    }
    else {
        Write-Host "== Verificación de respaldo cifrado (slot: $Slot)"
        if (-not $OutputDir) { throw "Verify requiere -OutputDir" }
        Assert-OutsideRepo $OutputDir "la salida"
        $ownTemp = $false
        if (-not $RestoreTo) { $RestoreTo = Join-Path $env:TEMP ("lc-verify-" + [guid]::NewGuid().ToString("N")); $ownTemp = $true }
        Assert-OutsideRepo $RestoreTo "la carpeta de restauración"
        try {
            $n = Restore-And-Verify $OutputDir $Slot $RecoveryKeyPath $RestoreTo
            Write-Host "OK: $n hashes coinciden con el manifiesto del respaldo"
            exit 0
        } finally {
            # Solo se borra la carpeta temporal creada aquí; nunca una carpeta indicada por el usuario.
            if ($ownTemp -and (Test-Path -LiteralPath $RestoreTo)) { Remove-Item -LiteralPath $RestoreTo -Recurse -Force }
        }
    }
}
catch {
    Write-Host ("FALLO: " + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
