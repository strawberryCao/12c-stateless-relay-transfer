[CmdletBinding()]
param(
    [int]$GatewayPort = 8088,
    [int]$ConsolePort = 8070
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$HomeDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ComposeFile = Join-Path $HomeDir 'compose.yaml'
$EnvFile = Join-Path $HomeDir '.env'
$RuntimeDir = Join-Path $HomeDir 'runtime'
$PidFile = Join-Path $RuntimeDir 'cloudflared.pid'
$PublicUrlFile = Join-Path $RuntimeDir 'public-url.txt'

function Write-Step([string]$Message) {
    Write-Host "`n==> $Message" -ForegroundColor Cyan
}

function Invoke-NativeCommand {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    $previousPreference = $ErrorActionPreference
    try {
        # Windows PowerShell 5.1 turns native stderr into NativeCommandError when
        # ErrorActionPreference is Stop. Capture the process result explicitly
        # and decide from its exit code instead.
        $ErrorActionPreference = 'Continue'
        $rawOutput = & $FilePath @Arguments 2>&1
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }

    $output = @()
    foreach ($item in @($rawOutput)) {
        $output += [string]$item
    }

    return [pscustomobject]@{
        ExitCode = $exitCode
        Output = $output
    }
}

function Invoke-Compose {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    $dockerArguments = @(
        'compose',
        '--env-file', $EnvFile,
        '-f', $ComposeFile
    ) + $Arguments

    return Invoke-NativeCommand -FilePath 'docker' -Arguments $dockerArguments
}

function Invoke-ComposeChecked {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    $result = Invoke-Compose -Arguments $Arguments
    foreach ($line in $result.Output) {
        Write-Host $line
    }
    if ($result.ExitCode -ne 0) {
        throw "docker compose failed: $($Arguments -join ' ')"
    }
    return $result
}

function Read-EnvFile([string]$Path) {
    $result = @{}
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match '^\s*#' -or $line -notmatch '=') {
            continue
        }
        $parts = $line.Split('=', 2)
        $result[$parts[0].Trim()] = $parts[1].Trim()
    }
    return $result
}

function Wait-ServiceHealthy {
    param(
        [Parameter(Mandatory = $true)][string]$Service,
        [int]$Attempts = 60
    )

    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        $idResult = Invoke-Compose -Arguments @('ps', '-q', $Service)
        $containerId = ($idResult.Output -join '').Trim()
        if ($idResult.ExitCode -eq 0 -and $containerId) {
            $inspect = Invoke-NativeCommand -FilePath 'docker' -Arguments @(
                'inspect',
                '--format',
                '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}',
                $containerId
            )
            $status = ($inspect.Output -join '').Trim()
            if ($inspect.ExitCode -eq 0 -and $status -eq 'healthy') {
                Write-Host "$Service is healthy." -ForegroundColor Green
                return
            }
        }

        if ($attempt -eq 1 -or $attempt % 5 -eq 0) {
            Write-Host "Waiting for $Service health ($attempt/$Attempts)..."
        }
        Start-Sleep -Seconds 2
    }

    Invoke-ComposeChecked -Arguments @('logs', '--tail', '80', $Service) | Out-Null
    throw "$Service did not become healthy."
}

function Get-RelayHealth {
    $python = 'import json,urllib.request; print(json.dumps(json.load(urllib.request.urlopen("http://127.0.0.1:9090/health", timeout=3))))'
    $result = Invoke-Compose -Arguments @('exec', '-T', 'relay', 'python', '-c', $python)
    if ($result.ExitCode -ne 0) {
        return $null
    }

    $lines = @($result.Output | Where-Object { $_ -and $_.Trim() } | Select-Object -Last 1)
    if ($lines.Count -eq 0) {
        return $null
    }

    try {
        return ($lines[0] | ConvertFrom-Json)
    }
    catch {
        return $null
    }
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw 'Docker CLI not found.'
}
if (-not (Test-Path $EnvFile)) {
    throw 'Missing deploy/home/.env. Run start-home.ps1 once before using finish-home.ps1.'
}

$dockerInfo = Invoke-NativeCommand -FilePath 'docker' -Arguments @('info')
if ($dockerInfo.ExitCode -ne 0) {
    throw 'Docker Desktop Linux engine is not running. Open Docker Desktop and wait for Engine running.'
}

$settings = Read-EnvFile -Path $EnvFile
if (-not $settings.ContainsKey('PUBLIC_URL') -or -not $settings['PUBLIC_URL']) {
    throw 'PUBLIC_URL is missing from deploy/home/.env.'
}
$publicUrl = $settings['PUBLIC_URL'].TrimEnd('/')

if (-not (Test-Path $PidFile)) {
    throw 'The Cloudflare tunnel PID file is missing. Run start-home.ps1 -SkipBuild again.'
}
$tunnelPidText = (Get-Content -LiteralPath $PidFile -Raw).Trim()
$tunnelProcess = $null
if ($tunnelPidText -match '^\d+$') {
    $tunnelProcess = Get-Process -Id ([int]$tunnelPidText) -ErrorAction SilentlyContinue
}
if (-not $tunnelProcess) {
    throw 'The temporary Cloudflare tunnel is no longer running. Run start-home.ps1 -SkipBuild again.'
}

Write-Step 'Checking Registry and Relay'
Wait-ServiceHealthy -Service 'registry'
Wait-ServiceHealthy -Service 'relay'

$health = Get-RelayHealth
if (-not $health -or -not $health.installId) {
    Invoke-ComposeChecked -Arguments @('logs', '--tail', '100', 'registry', 'relay') | Out-Null
    throw 'Relay did not expose an installId.'
}

if ($health.assignmentStatus -ne 'assigned') {
    Write-Step "Approving this Relay installId: $($health.installId)"

    $approvalPython = @'
import os, sys, urllib.parse, urllib.request
install_id = sys.argv[1]
key = os.environ["REGISTRY_ADMIN_API_KEY"]
url = "http://127.0.0.1:8080/api/admin/registration-requests/" + urllib.parse.quote(install_id, safe="") + "/approve"
request = urllib.request.Request(url, data=b"{}", method="POST", headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
response = urllib.request.urlopen(request, timeout=5)
print(response.read().decode("utf-8"))
'@

    $approved = $false
    for ($attempt = 1; $attempt -le 45; $attempt++) {
        $result = Invoke-Compose -Arguments @(
            'exec', '-T', 'registry', 'python', '-c', $approvalPython, [string]$health.installId
        )
        if ($result.ExitCode -eq 0) {
            $approved = $true
            Write-Host 'Relay registration approved.' -ForegroundColor Green
            break
        }
        if ($attempt -eq 1 -or $attempt % 5 -eq 0) {
            Write-Host "Waiting for registration request ($attempt/45)..."
        }
        Start-Sleep -Seconds 2
    }

    if (-not $approved) {
        Invoke-ComposeChecked -Arguments @('logs', '--tail', '100', 'registry', 'relay') | Out-Null
        throw 'Relay registration request could not be approved.'
    }
}

Write-Step 'Waiting for Relay assignment'
for ($attempt = 1; $attempt -le 60; $attempt++) {
    $health = Get-RelayHealth
    if ($health -and $health.assignmentStatus -eq 'assigned' -and $health.relayId) {
        break
    }
    if ($attempt -eq 1 -or $attempt % 5 -eq 0) {
        Write-Host "Waiting for assignment ($attempt/60)..."
    }
    Start-Sleep -Seconds 2
}
if (-not $health -or $health.assignmentStatus -ne 'assigned' -or -not $health.relayId) {
    throw 'Relay assignment did not complete.'
}
Write-Host "Relay assigned: $($health.relayId)" -ForegroundColor Green

Write-Step 'Synchronizing the Relay public URL in the Registry allowlist'
$patchPython = @'
import json, os, sys, urllib.parse, urllib.request
relay_id = sys.argv[1]
relay_url = sys.argv[2]
key = os.environ["REGISTRY_ADMIN_API_KEY"]
url = "http://127.0.0.1:8080/api/admin/allowlist/" + urllib.parse.quote(relay_id, safe="")
body = json.dumps({"relayBaseUrl": relay_url, "enabled": True}).encode("utf-8")
request = urllib.request.Request(url, data=body, method="PATCH", headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
response = urllib.request.urlopen(request, timeout=5)
print(response.read().decode("utf-8"))
'@

$patchResult = Invoke-Compose -Arguments @(
    'exec', '-T', 'registry', 'python', '-c', $patchPython, [string]$health.relayId, "$publicUrl/block"
)
if ($patchResult.ExitCode -ne 0) {
    foreach ($line in $patchResult.Output) {
        Write-Host $line
    }
    throw 'Failed to synchronize the Relay public URL.'
}

Write-Step 'Waiting for Relay key bootstrap'
for ($attempt = 1; $attempt -le 60; $attempt++) {
    $health = Get-RelayHealth
    if (
        $health -and
        $health.assignmentStatus -eq 'assigned' -and
        $health.registryApiKeyReady -and
        $health.blockAuthKeyReady
    ) {
        break
    }
    if ($attempt -eq 1 -or $attempt % 5 -eq 0) {
        Write-Host "Waiting for key bootstrap ($attempt/60)..."
    }
    Start-Sleep -Seconds 2
}
if (-not $health -or -not $health.registryApiKeyReady -or -not $health.blockAuthKeyReady) {
    Invoke-ComposeChecked -Arguments @('logs', '--tail', '100', 'registry', 'relay') | Out-Null
    throw 'Relay key bootstrap did not complete.'
}
Write-Host 'Relay keys are ready.' -ForegroundColor Green

Write-Step 'Starting Gateway and Console'
Invoke-ComposeChecked -Arguments @('up', '--detach', 'console', 'gateway') | Out-Null

$gatewayReady = $false
for ($attempt = 1; $attempt -le 60; $attempt++) {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$GatewayPort/" -TimeoutSec 3
        if ($response.StatusCode -eq 200) {
            $gatewayReady = $true
            break
        }
    }
    catch {
        # Retry while Caddy is starting.
    }
    if ($attempt -eq 1 -or $attempt % 5 -eq 0) {
        Write-Host "Waiting for Gateway ($attempt/60)..."
    }
    Start-Sleep -Seconds 2
}
if (-not $gatewayReady) {
    Invoke-ComposeChecked -Arguments @('logs', '--tail', '100', 'gateway') | Out-Null
    throw 'Local gateway did not become ready.'
}

Write-Step 'Verifying that control-plane routes are not published'
$blockedResult = Invoke-NativeCommand -FilePath 'curl.exe' -Arguments @(
    '-s', '-o', 'NUL', '-w', '%{http_code}', '-X', 'POST',
    '-H', 'Content-Type: application/json', '-d', '{}',
    "http://127.0.0.1:$GatewayPort/api/relay/registration-request"
)
$blockedStatus = ($blockedResult.Output -join '').Trim()
if ($blockedResult.ExitCode -ne 0 -or $blockedStatus -ne '404') {
    throw "Security verification failed: registration-request returned HTTP $blockedStatus"
}

$adminResult = Invoke-NativeCommand -FilePath 'curl.exe' -Arguments @(
    '-s', '-o', 'NUL', '-w', '%{http_code}',
    "http://127.0.0.1:$GatewayPort/api/admin/allowlist"
)
$adminStatus = ($adminResult.Output -join '').Trim()
if ($adminResult.ExitCode -ne 0 -or $adminStatus -ne '404') {
    throw "Security verification failed: admin route returned HTTP $adminStatus"
}

Invoke-ComposeChecked -Arguments @('ps') | Out-Null
Set-Content -LiteralPath $PublicUrlFile -Value $publicUrl -Encoding UTF8

Write-Host "`nDeployment complete." -ForegroundColor Green
Write-Host "Public Web/PWA: $publicUrl"
Write-Host "Local Console:   http://127.0.0.1:$ConsolePort"
Write-Host "Saved URL:       $PublicUrlFile"
Write-Host "`nKeep Docker Desktop running and keep the laptop awake."
