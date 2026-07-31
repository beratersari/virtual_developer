#Requires -Version 5.1
<#
.SYNOPSIS
  Extract vendor\opencode-home.zip into %USERPROFILE%\.opencode with long-path care.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Zip,
    [Parameter(Mandatory = $true)][string]$Dest
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $Zip)) {
    throw "Archive not found: $Zip"
}

if (-not (Test-Path -LiteralPath $Dest)) {
    New-Item -ItemType Directory -Path $Dest -Force | Out-Null
}

# 1) tar (Win10+): usually best with long paths
$tar = Get-Command tar -ErrorAction SilentlyContinue
if ($tar) {
    & tar -xf $Zip -C $Dest
    $exe = Join-Path $Dest "bin\opencode.exe"
    if ((Test-Path -LiteralPath $exe) -and $LASTEXITCODE -eq 0) {
        Write-Host "Extracted with tar -> $Dest"
        exit 0
    }
    Write-Host "tar extract incomplete; trying staged PowerShell extract..."
}

# 2) Extract to short TEMP path, then robocopy into Dest (robocopy handles long paths well)
$tmp = Join-Path $env:TEMP ("vd-oc-" + [guid]::NewGuid().ToString("n"))
New-Item -ItemType Directory -Path $tmp -Force | Out-Null
try {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::ExtractToDirectory($Zip, $tmp)

    $rc = Start-Process -FilePath "robocopy.exe" -ArgumentList @(
        $tmp, $Dest, "/E", "/NFL", "/NDL", "/NJH", "/NJS", "/nc", "/ns", "/np"
    ) -Wait -PassThru -NoNewWindow
    # robocopy: 0-7 success, >=8 failure
    if ($rc.ExitCode -ge 8) {
        throw "robocopy failed with exit code $($rc.ExitCode)"
    }
} finally {
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
}

$exe = Join-Path $Dest "bin\opencode.exe"
if (-not (Test-Path -LiteralPath $exe)) {
    throw "opencode.exe missing after extract: $exe"
}
Write-Host "Extracted with PowerShell+robocopy -> $Dest"
exit 0
