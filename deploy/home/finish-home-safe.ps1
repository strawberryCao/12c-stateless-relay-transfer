[CmdletBinding()]
param(
    [int]$GatewayPort = 8088,
    [int]$ConsolePort = 8070
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$homeDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$sourcePath = Join-Path $homeDir 'finish-home.ps1'
$tempPath = Join-Path $homeDir '.finish-home-patched.ps1'

if (-not (Test-Path -LiteralPath $sourcePath)) {
    throw 'Missing deploy/home/finish-home.ps1.'
}

$newFunction = @'
function Get-RelayHealth {
    $python = 'import base64,json,urllib.request; data=json.load(urllib.request.urlopen("http://127.0.0.1:9090/health", timeout=3)); print(base64.b64encode(json.dumps(data,separators=(",",":")).encode("utf-8")).decode("ascii"))'
    $result = Invoke-Compose -Arguments @('exec', '-T', 'relay', 'python', '-c', $python)
    if ($result.ExitCode -ne 0) {
        return $null
    }

    foreach ($line in @($result.Output | Select-Object -Reverse)) {
        $candidate = ([string]$line).Trim()
        if ($candidate -notmatch '^[A-Za-z0-9+/]+={0,2}$' -or $candidate.Length -lt 16) {
            continue
        }
        try {
            $json = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($candidate))
            $health = $json | ConvertFrom-Json
            if ($health.installId) {
                return $health
            }
        }
        catch {
            continue
        }
    }

    return $null
}
'@

$source = [System.IO.File]::ReadAllText($sourcePath)
$pattern = '(?s)function Get-RelayHealth \{.*?\r?\n\}\r?\n\r?\nif \(-not \(Get-Command docker'
$replacement = $newFunction + "`r`n`r`nif (-not (Get-Command docker"
$patched = [regex]::Replace($source, $pattern, $replacement, 1)

if ($patched -eq $source) {
    throw 'Could not patch Relay health parser. Pull the latest main branch and retry.'
}

$encoding = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($tempPath, $patched, $encoding)

try {
    & $tempPath -GatewayPort $GatewayPort -ConsolePort $ConsolePort
}
finally {
    Remove-Item -LiteralPath $tempPath -Force -ErrorAction SilentlyContinue
}
