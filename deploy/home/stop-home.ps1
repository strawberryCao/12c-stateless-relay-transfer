[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$HomeDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ComposeFile = Join-Path $HomeDir 'compose.yaml'
$EnvFile = Join-Path $HomeDir '.env'
$PidFile = Join-Path $HomeDir 'runtime\cloudflared.pid'

if (Test-Path $PidFile) {
    $rawPid = (Get-Content -LiteralPath $PidFile -Raw).Trim()
    if ($rawPid -match '^\d+$') {
        $process = Get-Process -Id ([int]$rawPid) -ErrorAction SilentlyContinue
        if ($process) {
            Stop-Process -Id $process.Id -Force
            Write-Host "Stopped cloudflared PID $rawPid"
        }
    }
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
}

if (Test-Path $EnvFile) {
    & docker compose --env-file $EnvFile -f $ComposeFile down
    if ($LASTEXITCODE -ne 0) {
        throw 'docker compose down failed.'
    }
}

Write-Host '12C home server stopped. Data files were preserved.' -ForegroundColor Green
