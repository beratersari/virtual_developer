#Requires -Version 5.1
<#
.SYNOPSIS
  Online OpenCode install via the opencoderman submodule (Windows).

.DESCRIPTION
  ONLINE-ONLY. Does not change offline install-backends.bat.

  Downloads the pinned OpenCode CLI (opencoderman/packaging/versions.env)
  with packaging/build_artifact.py --in-place, then runs
  packaging/install_opencode.py (opencoderman/install.py).

  Layout:
    %USERPROFILE%\.opencode\bin\opencode.exe
    %USERPROFILE%\.opencode\agents\
    %USERPROFILE%\.opencode\skills\
    %USERPROFILE%\.opencode\opencode.json  (plugin=[], stock build/plan)

  ASCII only (Windows PowerShell 5.1 / cmd callers).
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [string]$RepoRoot = ""
)

$ErrorActionPreference = "Stop"

if (-not $RepoRoot) {
    $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
} else {
    $RepoRoot = (Resolve-Path $RepoRoot).Path
}

$ocm = Join-Path $RepoRoot "opencoderman"
$installer = Join-Path $RepoRoot "packaging\install_opencode.py"
if (-not (Test-Path -LiteralPath (Join-Path $ocm "install.py"))) {
    throw "opencoderman submodule missing ($ocm). Run: git submodule update --init --recursive"
}
if (-not (Test-Path -LiteralPath $installer)) {
    throw "Missing $installer"
}

function Invoke-VdPython([string]$Root, [string[]]$PyArgs) {
    $venvPy = Join-Path $Root ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPy) {
        & $venvPy @PyArgs
        return $LASTEXITCODE
    }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source) {
        & $cmd.Source @PyArgs
        return $LASTEXITCODE
    }
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py -and $py.Source) {
        & $py.Source -3 @PyArgs
        return $LASTEXITCODE
    }
    throw "Python not found. Run install-dashboard.bat first, or install Python 3.10+."
}

Write-Host "========================================"
Write-Host "  Virtual Developer - Online OpenCode"
Write-Host "  opencoderman vendor + install.py"
Write-Host "========================================"
Write-Host "Project : $RepoRoot"
Write-Host "Pack    : $ocm"
Write-Host ""

$pyArgs = @(
    $installer,
    "--repo-root", $RepoRoot,
    "--opencoderman-root", $ocm,
    "--online",
    "--require-binary"
)
$ec = Invoke-VdPython $RepoRoot $pyArgs
if ($ec -ne 0) { throw "opencoderman online install failed (exit $ec)" }

$ocExe = Join-Path $env:USERPROFILE ".opencode\bin\opencode.exe"
if (-not (Test-Path -LiteralPath $ocExe)) {
    throw "opencode.exe missing after online install: $ocExe"
}
Write-Host "[OK] OpenCode at $ocExe"
exit 0
