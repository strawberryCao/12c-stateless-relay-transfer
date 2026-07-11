[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$HomeDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ComposeFile = Join-Path $HomeDir 'compose.yaml'
$EnvFile = Join-Path $HomeDir '.env'
$PublicUrlFile = Join-Path $HomeDir 'runtime\public-url.txt'
$PidFile = Join-Path $HomeDir 'runtime\cloudflared.pid'

if (Test-Path $PublicUrlFile) {
    Write-Host "Public URL: $((Get-Content -LiteralPath $PublicUrlFile -Raw).Trim())" -ForegroundColor Green
} else {
    Write-Host 'Public URL: not created yet'
}

if (Test-Path $PidFile) {
    $rawPid = (Get-Content -LiteralPath $PidFile -Raw).Trim()
    $process = $null
    if ($rawPid -match '^\d+$') {
        $process = Get-Process -Id ([int]$rawPid) -ErrorAction SilentlyContinue
    }
    Write-Host "Tunnel: $(if ($process) { "running (PID $rawPid)" } else { 'stopped' })"
} else {
    Write-Host 'Tunnel: stopped'
}

if (Test-Path $EnvFile) {
    & docker compose --env-file $EnvFile -f $ComposeFile ps
    Write-Host "Local Console: http://127.0.0.1:8070"
    Write-Host "Local Gateway: http://127.0.0.1:8088"
} else {
    Write-Host 'Docker stack: not configured yet'
}
