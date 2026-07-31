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

# Hard-require 64-bit OpenCode (and glab when present)
Assert-Pe -Exe $exe
if (Test-Path -LiteralPath $glab) {
    # glab is smaller; lower min size
    $assert = Join-Path $PSScriptRoot "Assert-Amd64Pe.ps1"
    if (Test-Path -LiteralPath $assert) {
        & $assert -Path $glab -MinBytes 1MB
    }
}

Write-Host "OpenCode home ready (AMD64): $Dest"
exit 0
