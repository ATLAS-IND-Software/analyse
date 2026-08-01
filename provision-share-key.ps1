[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "High")]
param(
    [string]$KeyFile = (Join-Path $PSScriptRoot ".share-signing-key"),
    [string]$PublicKeyringFile = (Join-Path $PSScriptRoot ".share-public-keyring.json"),
    [switch]$Force,
    [switch]$AllowInvalidateExistingLinks,
    [switch]$Redeploy,
    [string]$ContainerName = "histo-maker",
    [string]$ImageName = "histo-maker:latest",
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
$KeyFile = [IO.Path]::GetFullPath($KeyFile)
$PublicKeyringFile = [IO.Path]::GetFullPath($PublicKeyringFile)
$KeyDirectory = Split-Path -Parent $KeyFile

function Protect-PrivateKeyFile {
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
        throw "Die Zugriffsrechte der privaten Schluesseldatei konnten nicht gehaertet werden: $Path"
    }
}

function Save-CurrentPublicKey {
    param([Parameter(Mandatory = $true)][string]$Destination)
    $Endpoint = "http://127.0.0.1:$Port/api/share-key"
    try {
        $Current = Invoke-RestMethod -Uri $Endpoint -Method Get -TimeoutSec 10
    } catch {
        throw "Der aktuelle Public Key konnte nicht von $Endpoint gesichert werden. Container starten oder -AllowInvalidateExistingLinks bewusst angeben."
    }
    $KeyId = [string]$Current.key_id
    $PublicKey = [string]$Current.public_key
    if ($KeyId -notmatch '^[0-9a-f]{16}$' -or $PublicKey -notmatch '^[A-Za-z0-9_-]{43}$') {
        throw "Die Share-Key-Antwort enthaelt keinen gueltigen aktuellen Ed25519-Public-Key."
    }

    $Keyring = [ordered]@{}
    if (Test-Path -LiteralPath $Destination -PathType Leaf) {
        try { $Existing = Get-Content -LiteralPath $Destination -Raw | ConvertFrom-Json } catch {
            throw "Der vorhandene Public-Key-Keyring enthaelt kein gueltiges JSON: $Destination"
        }
        if ($null -ne $Existing) {
            foreach ($Property in $Existing.PSObject.Properties) {
                if ($Property.Name -notmatch '^[0-9a-f]{16}$' -or [string]$Property.Value -notmatch '^[A-Za-z0-9_-]{43}$') {
                    throw "Der vorhandene Public-Key-Keyring enthaelt einen ungueltigen Eintrag."
                }
                $Keyring[$Property.Name] = [string]$Property.Value
            }
        }
    }
    $Keyring[$KeyId] = $PublicKey
    $TemporaryKeyring = "$Destination.tmp.$PID"
    try {
        $Json = ConvertTo-Json -InputObject $Keyring -Compress
        [IO.File]::WriteAllText($TemporaryKeyring, $Json, [Text.UTF8Encoding]::new($false))
        Move-Item -LiteralPath $TemporaryKeyring -Destination $Destination -Force
    } finally {
        if (Test-Path -LiteralPath $TemporaryKeyring) { Remove-Item -LiteralPath $TemporaryKeyring -Force }
    }
    Write-Host "Aktuellen Public Key $KeyId im historischen Keyring gesichert: $Destination"
}

if (-not (Test-Path -LiteralPath $KeyDirectory -PathType Container)) {
    throw "Das Zielverzeichnis existiert nicht: $KeyDirectory"
}
if ((Test-Path -LiteralPath $KeyFile) -and -not $Force) {
    throw "Es existiert bereits ein Signierschlüssel. Verwende -Force, um ihn bewusst zu rotieren; der bisherige Public Key wird dabei standardmäßig erhalten."
}

$Action = if (Test-Path -LiteralPath $KeyFile) { "Ed25519-Signierschlüssel rotieren" } else { "Ed25519-Signierschlüssel erzeugen" }
if (-not $PSCmdlet.ShouldProcess($KeyFile, $Action)) {
    return
}

if (Test-Path -LiteralPath $KeyFile) {
    Protect-PrivateKeyFile -Path $KeyFile
    if ($AllowInvalidateExistingLinks) {
        Write-Warning "Der bisherige Public Key wird nicht automatisch erhalten; bestehende Links koennen ungueltig werden."
    } else {
        Save-CurrentPublicKey -Destination $PublicKeyringFile
    }
}

$BackupFile = $null
if (Test-Path -LiteralPath $KeyFile) {
    $Timestamp = [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ")
    $BackupFile = "$KeyFile.backup.$Timestamp"
    Copy-Item -LiteralPath $KeyFile -Destination $BackupFile
    Protect-PrivateKeyFile -Path $BackupFile
}

$KeyBytes = New-Object byte[] 32
$Generator = [Security.Cryptography.RandomNumberGenerator]::Create()
try {
    $Generator.GetBytes($KeyBytes)
} finally {
    $Generator.Dispose()
}
$SigningKey = [Convert]::ToBase64String($KeyBytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
$TemporaryFile = "$KeyFile.tmp.$PID"

try {
    [IO.File]::WriteAllText($TemporaryFile, $SigningKey, [Text.Encoding]::ASCII)
    Protect-PrivateKeyFile -Path $TemporaryFile
    Move-Item -LiteralPath $TemporaryFile -Destination $KeyFile -Force
    Protect-PrivateKeyFile -Path $KeyFile
} finally {
    if (Test-Path -LiteralPath $TemporaryFile) {
        Remove-Item -LiteralPath $TemporaryFile -Force
    }
    [Array]::Clear($KeyBytes, 0, $KeyBytes.Length)
    $SigningKey = $null
}

Write-Host "Neuer Signierschlüssel wurde provisioniert: $KeyFile"
if ($BackupFile) {
    Write-Host "Vorheriger Schlüssel wurde gesichert: $BackupFile"
}

if ($Redeploy) {
    $DeployScript = Join-Path $PSScriptRoot "deploy.ps1"
    Write-Host "Deploye Container mit dem neuen Schlüssel ..."
    & $DeployScript -ContainerName $ContainerName -ImageName $ImageName -Port $Port -SigningKeyFile $KeyFile -SharePublicKeyringFile $PublicKeyringFile
} else {
    Write-Host "Der laufende Container verwendet den neuen Schlüssel erst nach einem Redeployment."
}
