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

$authPs1 = Join-Path $PSScriptRoot "ServeAuth.ps1"
if (-not (Test-Path -LiteralPath $authPs1)) {
    Write-Host "[ERROR] ServeAuth.ps1 not found next to Ensure-OpencodeServe.ps1"
    exit 1
}
. $authPs1

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

$waitPs1 = Join-Path $PSScriptRoot "Wait-Http.ps1"
if (-not (Test-Path -LiteralPath $waitPs1)) {
    Write-Host "[ERROR] Wait-Http.ps1 not found next to Ensure-OpencodeServe.ps1"
    exit 1
}

# Existing process env wins. Fill only keys that are unset, then the serve
# window inherits them. Do not put the password on the cmd line.
Import-OpencodeServeAuth -ProjectDir $ProjectDir
$auth = Get-OpencodeServeAuthorization
$health = "http://127.0.0.1:$ServePort/global/health"

function Invoke-ServeWait([int]$Seconds, [switch]$OneShot) {
    # Hashtable splat binds by name. An array splat is positional and would
    # pass the health URL to TimeoutSec.
    $params = @{
        Url = $health
        OkPattern = "healthy"
        FailOnUnauthorized = $true
    }
    if ($auth) {
        $params.Authorization = $auth
    }
    if ($OneShot) {
        $params.Once = $true
    } else {
        $params.TimeoutSec = $Seconds
    }
    & $waitPs1 @params
    return $LASTEXITCODE
}

$first = Invoke-ServeWait -OneShot
if ($first -eq 0) {
    Write-Host "[OK] OpenCode serve already healthy on port $ServePort"
    exit 0
}
if ($first -eq 3) {
    Write-Host "[ERROR] OpenCode serve rejected the password (HTTP 401)."
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
            Write-Host "Run install-backends.bat (offline) or install-opencode-online.bat."
            exit 1
        }
        $ocBin = Split-Path -Parent $found.Source
    }

    Write-Host "Starting OpenCode serve in window VD-OpenCode-Serve..."
    $inner = "set OPENCODE_DISABLE_MODELS_FETCH=1&& set GIT_TERMINAL_PROMPT=0&& set GCM_INTERACTIVE=never&& set GCM_MODAL_PROMPT=false&& set GCM_GUI_PROMPT=false&& set PATH=$ocBin;%PATH%&& opencode serve --port $ServePort --hostname $ServeHost --print-logs --log-level INFO & echo. & echo OpenCode serve exited. & pause"
    $startArgs = "/c start `"VD-OpenCode-Serve`" /D `"$ProjectDir`" cmd /c `"$inner`""
    Start-Process -FilePath $env:ComSpec -ArgumentList $startArgs -WorkingDirectory $ProjectDir | Out-Null
}

Write-Host "Waiting for $health ..."
$waited = Invoke-ServeWait -Seconds $TimeoutSec
if ($waited -eq 3) {
    Write-Host "[ERROR] OpenCode serve rejected the password (HTTP 401)."
    exit 1
}
exit $waited
