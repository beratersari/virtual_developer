#Requires -Version 5.1
# Ensure opencode serve is healthy for the daemon.
# Healthy -> leave it. Port listening -> wait. Else start a sibling window.
# Does not kill the VD daemon. ASCII-only. Do not use $pid as a local name.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ProjectDir,
    [string]$ServeHost = "127.0.0.1",
    [int]$ServePort = 4096,
    [int]$TimeoutSec = 90
)

$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"

function Test-ServeHealthy([int]$Port) {
    try {
        $url = "http://127.0.0.1:$Port/global/health"
        $resp = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 3
        $code = [int]$resp.StatusCode
        $body = [string]$resp.Content
        if ($code -ge 200 -and $code -lt 500 -and ($body -match "healthy")) {
            return $true
        }
    } catch {
    }
    return $false
}

function Test-PortListening([int]$Port) {
    if ($Port -le 0) { return $false }
    try {
        $conns = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
        if ($conns.Count -gt 0) { return $true }
    } catch {
    }
    $lines = @(netstat -ano 2>$null | Select-String ":$Port\s")
    foreach ($line in $lines) {
        if ($line.ToString() -match "LISTEN") { return $true }
    }
    return $false
}

if (Test-ServeHealthy $ServePort) {
    Write-Host "[OK] OpenCode serve already healthy on port $ServePort"
    exit 0
}

$waitPs1 = Join-Path $PSScriptRoot "Wait-Http.ps1"
if (-not (Test-Path -LiteralPath $waitPs1)) {
    Write-Host "[ERROR] Wait-Http.ps1 not found next to Ensure-OpencodeServe.ps1"
    exit 1
}

if (Test-PortListening $ServePort) {
    Write-Host "Port $ServePort is in use; waiting for OpenCode serve health..."
} else {
    $ocBin = Join-Path $env:USERPROFILE ".opencode\bin"
    $ocExe = Join-Path $ocBin "opencode.exe"
    if (-not (Test-Path -LiteralPath $ocExe)) {
        $found = Get-Command opencode -ErrorAction SilentlyContinue
        if (-not $found) {
            Write-Host "[ERROR] OpenCode not installed."
            Write-Host "Run install.bat (full offline) or install-opencode-online.bat."
            exit 1
        }
        $ocBin = Split-Path -Parent $found.Source
    }

    Write-Host "Starting OpenCode serve in window VD-OpenCode-Serve..."
    $inner = "set OPENCODE_DISABLE_MODELS_FETCH=1&& set PATH=$ocBin;%PATH%&& opencode serve --port $ServePort --hostname $ServeHost --print-logs --log-level INFO & echo. & echo OpenCode serve exited. & pause"
    $startArgs = "/c start `"VD-OpenCode-Serve`" /D `"$ProjectDir`" cmd /c `"$inner`""
    Start-Process -FilePath $env:ComSpec -ArgumentList $startArgs -WorkingDirectory $ProjectDir | Out-Null
}

$health = "http://127.0.0.1:$ServePort/global/health"
Write-Host "Waiting for $health ..."
& $waitPs1 -Url $health -TimeoutSec $TimeoutSec -OkPattern "healthy"
exit $LASTEXITCODE
