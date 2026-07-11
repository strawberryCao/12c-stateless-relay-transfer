[CmdletBinding()]
param(
    [string]$DataRoot = "$env:USERPROFILE\12c-server-data",
    [int]$GatewayPort = 8088,
    [int]$ConsolePort = 8070,
    [switch]$SkipBuild
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$HomeDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ComposeFile = Join-Path $HomeDir 'compose.yaml'
$EnvFile = Join-Path $HomeDir '.env'
$RuntimeDir = Join-Path $HomeDir 'runtime'
$PidFile = Join-Path $RuntimeDir 'cloudflared.pid'
$PublicUrlFile = Join-Path $RuntimeDir 'public-url.txt'
$TunnelOutLog = Join-Path $RuntimeDir 'cloudflared.out.log'
$TunnelErrLog = Join-Path $RuntimeDir 'cloudflared.err.log'

function Write-Step([string]$Message) {
    Write-Host "`n==> $Message" -ForegroundColor Cyan
}

function Write-Utf8NoBom {
    param([string]$Path, [string[]]$Lines)
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllLines($Path, $Lines, $encoding)
}

function Invoke-DockerCompose {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    & docker compose --env-file $EnvFile -f $ComposeFile @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose failed: $($Arguments -join ' ')"
    }
}

function New-UrlSafeSecret([int]$ByteCount = 32) {
    $bytes = New-Object byte[] $ByteCount
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($bytes)
    }
    finally {
        $rng.Dispose()
    }
    return [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function Read-EnvFile([string]$Path) {
    $result = @{}
    if (-not (Test-Path $Path)) {
        return $result
    }
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match '^\s*#' -or $line -notmatch '=') {
            continue
        }
        $parts = $line.Split('=', 2)
        $result[$parts[0].Trim()] = $parts[1].Trim()
    }
    return $result
}

function Wait-ContainerHttp {
    param(
        [string]$Service,
        [string]$Url,
        [int]$Attempts = 60
    )
    $python = "import urllib.request; urllib.request.urlopen('$Url', timeout=3).read()"
    for ($i = 1; $i -le $Attempts; $i++) {
        & docker compose --env-file $EnvFile -f $ComposeFile exec -T $Service python -c $python *> $null
        if ($LASTEXITCODE -eq 0) {
            return
        }
        Start-Sleep -Seconds 2
    }
    throw "$Service did not become ready: $Url"
}

function Get-RelayHealth {
    $python = 'import json,urllib.request; print(json.dumps(json.load(urllib.request.urlopen("http://127.0.0.1:9090/health", timeout=3))))'
    $json = & docker compose --env-file $EnvFile -f $ComposeFile exec -T relay python -c $python
    if ($LASTEXITCODE -ne 0 -or -not $json) {
        return $null
    }
    return ($json | ConvertFrom-Json)
}

function Stop-PreviousTunnel {
    if (-not (Test-Path $PidFile)) {
        return
    }
    $oldPid = (Get-Content -LiteralPath $PidFile -Raw).Trim()
    if ($oldPid -match '^\d+$') {
        $process = Get-Process -Id ([int]$oldPid) -ErrorAction SilentlyContinue
        if ($process) {
            Stop-Process -Id $process.Id -Force
            Start-Sleep -Seconds 1
        }
    }
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw 'Docker CLI not found. Install and start Docker Desktop first.'
}
if (-not (Get-Command cloudflared -ErrorAction SilentlyContinue)) {
    throw 'cloudflared not found. Install it first: winget install --id Cloudflare.cloudflared -e'
}

Write-Step 'Checking Docker Desktop'
& docker info *> $null
if ($LASTEXITCODE -ne 0) {
    $candidates = @(
        "$env:ProgramFiles\Docker\Docker\Docker Desktop.exe",
        "$env:LOCALAPPDATA\Programs\Docker\Docker\Docker Desktop.exe"
    )
    $desktop = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $desktop) {
        throw 'Docker Desktop is installed but not running, and its executable was not found.'
    }
    Start-Process -FilePath $desktop | Out-Null
    for ($i = 1; $i -le 90; $i++) {
        Start-Sleep -Seconds 2
        & docker info *> $null
        if ($LASTEXITCODE -eq 0) {
            break
        }
    }
    if ($LASTEXITCODE -ne 0) {
        throw 'Docker Desktop did not become ready within three minutes.'
    }
}

Write-Step 'Preparing data and runtime directories'
New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $DataRoot 'registry') | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $DataRoot 'relay') | Out-Null
Stop-PreviousTunnel
Remove-Item -LiteralPath $TunnelOutLog, $TunnelErrLog -Force -ErrorAction SilentlyContinue

Write-Step 'Starting a temporary Cloudflare Quick Tunnel'
$configBackups = @()
$cloudflareDir = Join-Path $HOME '.cloudflared'
foreach ($name in @('config.yml', 'config.yaml')) {
    $configPath = Join-Path $cloudflareDir $name
    if (Test-Path $configPath) {
        $backupPath = "$configPath.12c-temporary-$(Get-Date -Format yyyyMMddHHmmss)"
        Move-Item -LiteralPath $configPath -Destination $backupPath
        $configBackups += [pscustomobject]@{ Original = $configPath; Backup = $backupPath }
    }
}

try {
    $cloudflaredPath = (Get-Command cloudflared).Source
    $startParameters = @{
        FilePath = $cloudflaredPath
        ArgumentList = @('tunnel', '--url', "http://127.0.0.1:$GatewayPort")
        RedirectStandardOutput = $TunnelOutLog
        RedirectStandardError = $TunnelErrLog
        WindowStyle = 'Hidden'
        PassThru = $true
    }
    $tunnelProcess = Start-Process @startParameters
    Write-Utf8NoBom -Path $PidFile -Lines @([string]$tunnelProcess.Id)

    $publicUrl = $null
    for ($i = 1; $i -le 90; $i++) {
        Start-Sleep -Seconds 1
        $process = Get-Process -Id $tunnelProcess.Id -ErrorAction SilentlyContinue
        if (-not $process) {
            $details = ((Get-Content $TunnelOutLog -Raw -ErrorAction SilentlyContinue) + "`n" + (Get-Content $TunnelErrLog -Raw -ErrorAction SilentlyContinue)).Trim()
            throw "cloudflared exited before creating a URL.`n$details"
        }
        $combined = (Get-Content $TunnelOutLog -Raw -ErrorAction SilentlyContinue) + "`n" + (Get-Content $TunnelErrLog -Raw -ErrorAction SilentlyContinue)
        $match = [regex]::Match($combined, 'https://[a-z0-9-]+\.trycloudflare\.com', [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)
        if ($match.Success) {
            $publicUrl = $match.Value.TrimEnd('/')
            break
        }
    }
    if (-not $publicUrl) {
        throw 'Timed out waiting for a trycloudflare.com URL.'
    }
}
finally {
    foreach ($item in $configBackups) {
        if ((Test-Path $item.Backup) -and -not (Test-Path $item.Original)) {
            Move-Item -LiteralPath $item.Backup -Destination $item.Original
        }
    }
}

Write-Utf8NoBom -Path $PublicUrlFile -Lines @($publicUrl)
Write-Host "Temporary public URL: $publicUrl" -ForegroundColor Green

Write-Step 'Writing local deployment settings'
$settings = Read-EnvFile $EnvFile
if (-not $settings.ContainsKey('REGISTRY_ADMIN_API_KEY') -or $settings['REGISTRY_ADMIN_API_KEY'] -like 'REPLACE_WITH*') {
    $settings['REGISTRY_ADMIN_API_KEY'] = New-UrlSafeSecret
}
if (-not $settings.ContainsKey('RELAY_ADMIN_API_KEY') -or $settings['RELAY_ADMIN_API_KEY'] -like 'REPLACE_WITH*') {
    $settings['RELAY_ADMIN_API_KEY'] = New-UrlSafeSecret
}
if (-not $settings.ContainsKey('REGISTRY_BLOCK_AUTH_MASTER_KEY') -or $settings['REGISTRY_BLOCK_AUTH_MASTER_KEY'] -like 'REPLACE_WITH*') {
    $settings['REGISTRY_BLOCK_AUTH_MASTER_KEY'] = New-UrlSafeSecret
}
$settings['DATA_ROOT'] = ($DataRoot -replace '\\', '/')
$settings['PUBLIC_URL'] = $publicUrl
$settings['PUBLIC_RELAY_BASE_URL'] = "$publicUrl/block"
$settings['HOME_GATEWAY_PORT'] = [string]$GatewayPort
$settings['HOME_CONSOLE_PORT'] = [string]$ConsolePort

$orderedKeys = @(
    'DATA_ROOT',
    'PUBLIC_URL',
    'PUBLIC_RELAY_BASE_URL',
    'HOME_GATEWAY_PORT',
    'HOME_CONSOLE_PORT',
    'REGISTRY_ADMIN_API_KEY',
    'RELAY_ADMIN_API_KEY',
    'REGISTRY_BLOCK_AUTH_MASTER_KEY'
)
$envLines = foreach ($key in $orderedKeys) {
    "$key=$($settings[$key])"
}
Write-Utf8NoBom -Path $EnvFile -Lines $envLines

Write-Step 'Validating and building containers'
Invoke-DockerCompose config | Out-Null
if (-not $SkipBuild) {
    Invoke-DockerCompose build
}

Write-Step 'Starting Registry and Relay on the private Docker network'
Invoke-DockerCompose up -d registry relay
Wait-ContainerHttp -Service registry -Url 'http://127.0.0.1:8080/health'
Wait-ContainerHttp -Service relay -Url 'http://127.0.0.1:9090/health'

$health = Get-RelayHealth
if (-not $health -or -not $health.installId) {
    Invoke-DockerCompose logs registry relay
    throw 'Relay did not expose an installId.'
}

if ($health.assignmentStatus -ne 'assigned') {
    Write-Step "Approving this Relay installId: $($health.installId)"
    $approvalScript = @'
import json
import os
import sys
import urllib.parse
import urllib.request

install_id = sys.argv[1]
key = os.environ["REGISTRY_ADMIN_API_KEY"]
url = "http://127.0.0.1:8080/api/admin/registration-requests/" + urllib.parse.quote(install_id, safe="") + "/approve"
request = urllib.request.Request(
    url,
    data=b"{}",
    method="POST",
    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
)
with urllib.request.urlopen(request, timeout=5) as response:
    print(json.dumps(json.load(response)))
'@

    $approved = $false
    for ($i = 1; $i -le 45; $i++) {
        $approvalScript | & docker compose --env-file $EnvFile -f $ComposeFile exec -T registry python - $health.installId
        if ($LASTEXITCODE -eq 0) {
            $approved = $true
            break
        }
        Start-Sleep -Seconds 2
    }
    if (-not $approved) {
        Invoke-DockerCompose logs registry relay
        throw 'Relay registration request could not be approved.'
    }
}

for ($i = 1; $i -le 60; $i++) {
    Start-Sleep -Seconds 2
    $health = Get-RelayHealth
    if ($health -and $health.assignmentStatus -eq 'assigned' -and $health.relayId) {
        break
    }
}
if (-not $health -or $health.assignmentStatus -ne 'assigned' -or -not $health.relayId) {
    throw 'Relay assignment did not complete.'
}

Write-Step 'Synchronizing the Relay public URL in the Registry allowlist'
$patchScript = @'
import json
import os
import sys
import urllib.parse
import urllib.request

relay_id = sys.argv[1]
relay_url = sys.argv[2]
key = os.environ["REGISTRY_ADMIN_API_KEY"]
url = "http://127.0.0.1:8080/api/admin/allowlist/" + urllib.parse.quote(relay_id, safe="")
body = json.dumps({"relayBaseUrl": relay_url, "enabled": True}).encode("utf-8")
request = urllib.request.Request(
    url,
    data=body,
    method="PATCH",
    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
)
with urllib.request.urlopen(request, timeout=5) as response:
    print(json.dumps(json.load(response)))
'@
$patchScript | & docker compose --env-file $EnvFile -f $ComposeFile exec -T registry python - $health.relayId "$publicUrl/block"
if ($LASTEXITCODE -ne 0) {
    throw 'Failed to synchronize the Relay public URL.'
}

for ($i = 1; $i -le 60; $i++) {
    Start-Sleep -Seconds 2
    $health = Get-RelayHealth
    if ($health -and $health.assignmentStatus -eq 'assigned' -and $health.registryApiKeyReady -and $health.blockAuthKeyReady) {
        break
    }
}
if (-not $health.registryApiKeyReady -or -not $health.blockAuthKeyReady) {
    Invoke-DockerCompose logs registry relay
    throw 'Relay key bootstrap did not complete.'
}

Write-Step 'Starting the localhost-only gateway and management console'
Invoke-DockerCompose up -d console gateway

$gatewayReady = $false
for ($i = 1; $i -le 60; $i++) {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$GatewayPort/" -TimeoutSec 3
        if ($response.StatusCode -eq 200) {
            $gatewayReady = $true
            break
        }
    }
    catch {
        Start-Sleep -Seconds 2
    }
}
if (-not $gatewayReady) {
    Invoke-DockerCompose logs gateway
    throw 'Local gateway did not become ready.'
}

Write-Step 'Verifying that control-plane routes are not published'
$blockedStatus = & curl.exe -s -o NUL -w '%{http_code}' -X POST -H 'Content-Type: application/json' -d '{}' "http://127.0.0.1:$GatewayPort/api/relay/registration-request"
if ($blockedStatus -ne '404') {
    throw "Security verification failed: registration-request returned HTTP $blockedStatus"
}
$adminStatus = & curl.exe -s -o NUL -w '%{http_code}' "http://127.0.0.1:$GatewayPort/api/admin/allowlist"
if ($adminStatus -ne '404') {
    throw "Security verification failed: admin route returned HTTP $adminStatus"
}

Invoke-DockerCompose ps
Write-Host "`nDeployment complete." -ForegroundColor Green
Write-Host "Public Web/PWA: $publicUrl"
Write-Host "Local Console:   http://127.0.0.1:$ConsolePort"
Write-Host "Saved URL:       $PublicUrlFile"
Write-Host "Data directory:  $DataRoot"
Write-Host "`nKeep the laptop awake and keep Docker Desktop running. Closing this PowerShell window does not stop the tunnel process."
