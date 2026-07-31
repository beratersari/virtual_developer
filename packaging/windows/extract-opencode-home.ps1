#Requires -Version 5.1
<#
.SYNOPSIS
  Extract vendor\opencode-home.zip into %USERPROFILE%\.opencode with long-path care.
  Verifies opencode.exe / glab.exe are AMD64 (64-bit) PE binaries.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Zip,
    [Parameter(Mandatory = $true)][string]$Dest
)

$ErrorActionPreference = "Stop"

function Assert-Pe {
    param([string]$Exe)
    $assert = Join-Path $PSScriptRoot "Assert-Amd64Pe.ps1"
    if (Test-Path -LiteralPath $assert) {
        & $assert -Path $Exe
        if ($LASTEXITCODE -ne 0) { throw "PE check failed: $Exe" }
    }
}

if (-not (Test-Path -LiteralPath $Zip)) {
    throw "Archive not found: $Zip"
}

if (-not (Test-Path -LiteralPath $Dest)) {
    New-Item -ItemType Directory -Path $Dest -Force | Out-Null
}

$extracted = $false

# 1) tar (Win10+): usually best with long paths
$tar = Get-Command tar -ErrorAction SilentlyContinue
if ($tar) {
    & tar -xf $Zip -C $Dest
    $exe = Join-Path $Dest "bin\opencode.exe"
    if ((Test-Path -LiteralPath $exe) -and $LASTEXITCODE -eq 0) {
        Write-Host "Extracted with tar -> $Dest"
        $extracted = $true
    } else {
        Write-Host "tar extract incomplete; trying staged PowerShell extract..."
    }
}

# 2) Extract to short TEMP path, then robocopy into Dest
if (-not $extracted) {
    $tmp = Join-Path $env:TEMP ("vd-oc-" + [guid]::NewGuid().ToString("n"))
    New-Item -ItemType Directory -Path $tmp -Force | Out-Null
    try {
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [System.IO.Compression.ZipFile]::ExtractToDirectory($Zip, $tmp)

        $rc = Start-Process -FilePath "robocopy.exe" -ArgumentList @(
            $tmp, $Dest, "/E", "/NFL", "/NDL", "/NJH", "/NJS", "/nc", "/ns", "/np", "/R:2", "/W:1"
        ) -Wait -PassThru -NoNewWindow
        if ($rc.ExitCode -ge 8) {
            throw "robocopy failed with exit code $($rc.ExitCode)"
        }
    } finally {
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
    Write-Host "Extracted with PowerShell+robocopy -> $Dest"
}

$exe = Join-Path $Dest "bin\opencode.exe"
$glab = Join-Path $Dest "bin\glab.exe"
if (-not (Test-Path -LiteralPath $exe)) {
    throw "opencode.exe missing after extract: $exe"
}

# Remove Mark-of-the-Web so Windows does not block internet-downloaded exes
Get-ChildItem -LiteralPath $Dest -Recurse -File -ErrorAction SilentlyContinue |
    Unblock-File -ErrorAction SilentlyContinue

# Hard-require 64-bit OpenCode (and glab when present).
# Retry: Windows Defender sometimes quarantines the ~170MB binary right after extract
# and leaves a tiny stub (users see "not compatible with 64-bit Windows").
function Wait-HealthyPe([string]$Path, [long]$MinBytes) {
    $assert = Join-Path $PSScriptRoot "Assert-Amd64Pe.ps1"
    $deadline = (Get-Date).AddSeconds(30)
    $lastErr = $null
    while ((Get-Date) -lt $deadline) {
        try {
            if ((Test-Path -LiteralPath $Path) -and ((Get-Item -LiteralPath $Path).Length -ge $MinBytes)) {
                if (Test-Path -LiteralPath $assert) {
                    & $assert -Path $Path -MinBytes $MinBytes
                    if ($LASTEXITCODE -eq 0) { return }
                } else {
                    return
                }
            }
        } catch {
            $lastErr = $_
        }
        Start-Sleep -Milliseconds 500
    }
    # Restore from zip once more into a temp folder and copy over (AV race recovery)
    Write-Host "WARNING: PE not healthy after wait; re-extracting binary from archive..."
    $tmp2 = Join-Path $env:TEMP ("vd-oc-bin-" + [guid]::NewGuid().ToString("n"))
    New-Item -ItemType Directory -Path $tmp2 -Force | Out-Null
    try {
        & tar -xf $Zip -C $tmp2
        $fresh = Get-ChildItem -Path $tmp2 -Recurse -Filter (Split-Path $Path -Leaf) | Select-Object -First 1
        if (-not $fresh) { throw "re-extract could not find $(Split-Path $Path -Leaf)" }
        $binDir = Split-Path $Path -Parent
        if (-not (Test-Path -LiteralPath $binDir)) {
            New-Item -ItemType Directory -Path $binDir -Force | Out-Null
        }
        Copy-Item -LiteralPath $fresh.FullName -Destination $Path -Force
        Unblock-File -LiteralPath $Path -ErrorAction SilentlyContinue
        & $assert -Path $Path -MinBytes $MinBytes
        if ($LASTEXITCODE -ne 0) {
            throw "PE still invalid after re-extract: $Path ($lastErr)"
        }
    } finally {
        Remove-Item -LiteralPath $tmp2 -Recurse -Force -ErrorAction SilentlyContinue
    }
}

Wait-HealthyPe -Path $exe -MinBytes 10MB
if (Test-Path -LiteralPath $glab) {
    Wait-HealthyPe -Path $glab -MinBytes 1MB
}

# Smoke-start in the same process (before Defender can swap the file out mid-flight)
Write-Host "Smoke: opencode --version"
$verOut = & $exe --version 2>&1
Write-Host ($verOut | Out-String)
if ($LASTEXITCODE -ne 0) {
    throw "opencode --version failed after extract (exit $LASTEXITCODE)"
}

Write-Host "OpenCode home ready (AMD64): $Dest"
exit 0
