param(
    [string]$ContainerName = "histo-maker",
    [string]$ImageName = "histo-maker:latest",
    [int]$Port = 8000,
    [int]$MaxUploadMb = 50,
    [int]$InspectRateLimit = 30,
    [int]$AnalyzeRateLimit = 10,
    [int]$EstimateRateLimit = 30,
    [int]$MaxConcurrentInspectionsPerWorker = 1,
    [int]$MaxConcurrentAnalysesPerWorker = 2,
    [int]$UploadCacheTtlSeconds = 600,
    [int]$UploadCacheMaxMb = 256,
    [int]$UploadCacheMaxItems = 100,
    [int]$KdeMaxSampleSize = 20000,
    [int]$RugMaxPoints = 300,
    [string]$MemoryLimit = "1g",
    [string]$CpuLimit = "2.0",
    [string]$SharePublicKeyring = $env:SHARE_PUBLIC_KEYRING,
    [string]$SharePublicKeyringFile = (Join-Path $PSScriptRoot ".share-public-keyring.json"),
    [string]$SigningKeyFile = (Join-Path $PSScriptRoot ".share-signing-key")
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Version = (Get-Content -LiteralPath (Join-Path $ProjectDir "VERSION") -Raw).Trim()
$SigningKeyFile = [IO.Path]::GetFullPath($SigningKeyFile)
$SharePublicKeyringFile = [IO.Path]::GetFullPath($SharePublicKeyringFile)

function Protect-SigningKeyFile {
    param([Parameter(Mandatory = $true)][string]$Path)
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) { return }
    $CurrentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $Acl = New-Object Security.AccessControl.FileSecurity
    $Acl.SetSecurityDescriptorSddlForm(
        "D:P(A;;FA;;;$($CurrentSid.Value))(A;;FA;;;SY)(A;;FA;;;BA)",
        [Security.AccessControl.AccessControlSections]::Access
    )
    [IO.File]::SetAccessControl($Path, $Acl)
    if (-not (Get-Acl -LiteralPath $Path).AreAccessRulesProtected) {
        throw "Die Zugriffsrechte der Signierschluesseldatei konnten nicht gehaertet werden: $Path"
    }
}

function Protect-SecretDirectory {
    param([Parameter(Mandatory = $true)][string]$Path)
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) { return }
    $CurrentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $Acl = New-Object Security.AccessControl.DirectorySecurity
    $Acl.SetSecurityDescriptorSddlForm(
        "D:P(A;OICI;FA;;;$($CurrentSid.Value))(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)",
        [Security.AccessControl.AccessControlSections]::Access
    )
    [IO.Directory]::SetAccessControl($Path, $Acl)
    if (-not (Get-Acl -LiteralPath $Path).AreAccessRulesProtected) {
        throw "Die Zugriffsrechte des temporaeren Secret-Verzeichnisses konnten nicht gehaertet werden: $Path"
    }
}

if (-not $SharePublicKeyring -and (Test-Path -LiteralPath $SharePublicKeyringFile -PathType Leaf)) {
    $SharePublicKeyring = (Get-Content -LiteralPath $SharePublicKeyringFile -Raw).Trim()
}
if ($SharePublicKeyring) {
    try { $null = $SharePublicKeyring | ConvertFrom-Json } catch {
        throw "Der historische Public-Key-Keyring enthaelt kein gueltiges JSON: $SharePublicKeyringFile"
    }
}

if (Test-Path -LiteralPath $SigningKeyFile) {
    Protect-SigningKeyFile -Path $SigningKeyFile
    $SigningKey = (Get-Content -LiteralPath $SigningKeyFile -Raw).Trim()
} else {
    $KeyBytes = New-Object byte[] 32
    $Generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $Generator.GetBytes($KeyBytes) } finally { $Generator.Dispose() }
    $SigningKey = [Convert]::ToBase64String($KeyBytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
    $TemporaryKeyFile = "$SigningKeyFile.tmp.$PID"
    try {
        [IO.File]::WriteAllText($TemporaryKeyFile, $SigningKey, [Text.Encoding]::ASCII)
        Protect-SigningKeyFile -Path $TemporaryKeyFile
        Move-Item -LiteralPath $TemporaryKeyFile -Destination $SigningKeyFile
        Protect-SigningKeyFile -Path $SigningKeyFile
    } finally {
        if (Test-Path -LiteralPath $TemporaryKeyFile) { Remove-Item -LiteralPath $TemporaryKeyFile -Force }
        [Array]::Clear($KeyBytes, 0, $KeyBytes.Length)
    }
    Write-Host "Neuen persistenten Signierschlüssel in '$SigningKeyFile' erstellt."
}

if ($SigningKey -notmatch '^[A-Za-z0-9_-]{43}$') {
    throw "Die Signierschluesseldatei enthaelt keinen gueltigen 32-Byte-Base64url-Seed."
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker wurde nicht gefunden. Bitte Docker Desktop installieren oder starten."
}

function Invoke-Docker {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    & docker @Arguments
    if ($LASTEXITCODE -ne 0) {
        $SafeArguments = $Arguments | ForEach-Object {
            if ($_ -like "SHARE_SIGNING_PRIVATE_KEY=*") { "SHARE_SIGNING_PRIVATE_KEY=<redacted>" } else { $_ }
        }
        throw "Docker-Befehl fehlgeschlagen: docker $($SafeArguments -join ' ')"
    }
}

Write-Host "Baue Image '$ImageName' (Version $Version) ..."
Invoke-Docker build --pull --build-arg "APP_VERSION=$Version" --tag $ImageName $ProjectDir

$CandidateName = "$ContainerName-candidate"
$RollbackName = "$ContainerName-rollback"
$Existing = docker ps -a --filter "name=^/$ContainerName$" --format "{{.ID}}"
$StaleCandidate = docker ps -a --filter "name=^/$CandidateName$" --format "{{.ID}}"
$StaleRollback = docker ps -a --filter "name=^/$RollbackName$" --format "{{.ID}}"
if ($StaleRollback) {
    if (-not $Existing) {
        Write-Warning "Unterbrochenes Deployment erkannt. Stelle Rollback-Container wieder her."
        if ($StaleCandidate) {
            Invoke-Docker rm -f $CandidateName | Out-Null
        }
        Invoke-Docker rename $RollbackName $ContainerName
        Invoke-Docker start $ContainerName | Out-Null
        throw "Vorheriger Container wurde wiederhergestellt. Bitte Deployment erneut starten."
    }
    throw "Rollback-Container '$RollbackName' existiert neben dem aktiven Container. Bitte Zustand manuell prüfen."
}
if ($StaleCandidate) {
    Write-Host "Entferne unvollständigen Kandidaten '$CandidateName' ..."
    Invoke-Docker rm -f $CandidateName | Out-Null
}

$OldWasRunning = $false
$OldRenamed = $false
$CandidateStarted = $false
$CandidatePromoted = $false

try {
    if ($Existing) {
        $OldWasRunning = (docker inspect --format "{{.State.Running}}" $ContainerName) -eq "true"
        if ($OldWasRunning) {
            Write-Host "Stoppe bisherigen Container erst nach erfolgreichem Image-Build ..."
            Invoke-Docker stop --time 45 $ContainerName | Out-Null
        }
        Invoke-Docker rename $ContainerName $RollbackName
        $OldRenamed = $true
    }

    Write-Host "Starte Release-Kandidaten auf 127.0.0.1:$Port ..."
    $SecretEnvDirectory = Join-Path ([IO.Path]::GetTempPath()) ("histo-maker-docker-secret-" + [guid]::NewGuid().ToString("N"))
    $SecretEnvFile = Join-Path $SecretEnvDirectory "container.env"
    try {
        [void][IO.Directory]::CreateDirectory($SecretEnvDirectory)
        Protect-SecretDirectory -Path $SecretEnvDirectory
        [IO.File]::WriteAllText($SecretEnvFile, "SHARE_SIGNING_PRIVATE_KEY=$SigningKey`n", [Text.Encoding]::ASCII)
        Protect-SigningKeyFile -Path $SecretEnvFile
        Invoke-Docker run --detach `
            --name $CandidateName `
            --restart no `
            --stop-timeout 45 `
            --memory $MemoryLimit `
            --cpus $CpuLimit `
            --pids-limit 256 `
            --read-only `
            --tmpfs "/tmp:rw,noexec,nosuid,size=64m" `
            --cap-drop ALL `
            --security-opt "no-new-privileges:true" `
            --env-file $SecretEnvFile `
            --env "APP_VERSION=$Version" `
            --env "SHARE_PUBLIC_KEYRING=$SharePublicKeyring" `
            --env "TRUST_CF_CONNECTING_IP=1" `
            --env "MAX_UPLOAD_MB=$MaxUploadMb" `
            --env "RATE_LIMIT_INSPECT_PER_WINDOW=$InspectRateLimit" `
            --env "RATE_LIMIT_ANALYZE_PER_WINDOW=$AnalyzeRateLimit" `
            --env "RATE_LIMIT_ESTIMATE_PER_WINDOW=$EstimateRateLimit" `
            --env "MAX_CONCURRENT_INSPECTIONS_PER_WORKER=$MaxConcurrentInspectionsPerWorker" `
            --env "MAX_CONCURRENT_ANALYSES_PER_WORKER=$MaxConcurrentAnalysesPerWorker" `
            --env "UPLOAD_CACHE_TTL_SECONDS=$UploadCacheTtlSeconds" `
            --env "UPLOAD_CACHE_MAX_MB=$UploadCacheMaxMb" `
            --env "UPLOAD_CACHE_MAX_ITEMS=$UploadCacheMaxItems" `
            --env "KDE_MAX_SAMPLE_SIZE=$KdeMaxSampleSize" `
            --env "RUG_MAX_POINTS=$RugMaxPoints" `
            --publish "127.0.0.1:${Port}:8000" `
            $ImageName | Out-Null
        $CandidateStarted = $true
    } finally {
        $SigningKey = $null
        if (Test-Path -LiteralPath $SecretEnvFile) { Remove-Item -LiteralPath $SecretEnvFile -Force }
        if (Test-Path -LiteralPath $SecretEnvDirectory) { [IO.Directory]::Delete($SecretEnvDirectory) }
    }

    $Deadline = [DateTime]::UtcNow.AddSeconds(90)
    do {
        Start-Sleep -Seconds 2
        $Health = docker inspect --format "{{.State.Health.Status}}" $CandidateName 2>$null
        if ($Health -eq "unhealthy") {
            throw "Release-Kandidat wurde als unhealthy markiert."
        }
    } while ($Health -ne "healthy" -and [DateTime]::UtcNow -lt $Deadline)
    if ($Health -ne "healthy") {
        throw "Release-Kandidat wurde nicht innerhalb von 90 Sekunden healthy."
    }

    Invoke-Docker rename $CandidateName $ContainerName
    $CandidateStarted = $false
    $CandidatePromoted = $true
    Invoke-Docker update --restart unless-stopped $ContainerName | Out-Null
    if ($OldRenamed) {
        Invoke-Docker rm $RollbackName | Out-Null
        $OldRenamed = $false
    }
} catch {
    Write-Warning "Deployment fehlgeschlagen. Stelle vorherigen Container wieder her."
    if ($CandidatePromoted) {
        docker rm -f $ContainerName 2>$null | Out-Null
    } elseif ($CandidateStarted) {
        docker logs --tail 80 $CandidateName 2>$null
        docker rm -f $CandidateName 2>$null | Out-Null
    }
    if ($OldRenamed) {
        docker rename $RollbackName $ContainerName | Out-Null
        if ($OldWasRunning) {
            docker start $ContainerName | Out-Null
        }
    }
    throw
}

Write-Host "Histo Maker läuft lokal unter http://127.0.0.1:$Port"
docker ps --filter "name=^/$ContainerName$"
