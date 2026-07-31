#Requires -Version 5.1
<#
.SYNOPSIS
  End-to-end Windows smoke test for the offline distribution.

.DESCRIPTION
  Simulates a realistic user path (deep Downloads nesting), runs install.bat
  non-interactively, and verifies opencode.exe is AMD64 and starts.
  Fails the CI job if extract/install would break on Windows MAX_PATH or arch.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PayloadDir,
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"

function Write-Step($msg) { Write-Host ""; Write-Host "=== $msg ===" }

if (-not (Test-Path -LiteralPath (Join-Path $PayloadDir "install.bat"))) {
    throw "install.bat not found in payload: $PayloadDir"
}
if (-not (Test-Path -LiteralPath (Join-Path $PayloadDir "vendor\opencode-home.zip"))) {
    throw "vendor\opencode-home.zip missing — outer package must not expand node_modules"
}

# Fail if someone reintroduced expanded node_modules into the payload
$badNm = Join-Path $PayloadDir "vendor\opencode-home\node_modules"
if (Test-Path -LiteralPath $badNm) {
    throw "FAIL: expanded vendor\opencode-home\node_modules present (path-length bomb)"
}

Write-Step "Audit relative path lengths inside opencode-home.zip"
$tmpAudit = Join-Path $env:TEMP ("vd-audit-" + [guid]::NewGuid().ToString("n"))
New-Item -ItemType Directory -Path $tmpAudit -Force | Out-Null
try {
    tar -xf (Join-Path $PayloadDir "vendor\opencode-home.zip") -C $tmpAudit
    if ($LASTEXITCODE -ne 0) { throw "tar extract of opencode-home.zip failed during audit" }

    $maxRel = 0
    $worst = ""
    Get-ChildItem -LiteralPath $tmpAudit -Recurse -Force -ErrorAction SilentlyContinue | ForEach-Object {
        $rel = $_.FullName.Substring($tmpAudit.Length).TrimStart('\')
        if ($rel.Length -gt $maxRel) {
            $maxRel = $rel.Length
            $worst = $rel
        }
    }
    Write-Host "Max relative path length: $maxRel"
    Write-Host "Worst path: $worst"

    # Leave headroom for C:\vd\opencode\ (14) or LocalAppData prefixes (~40-80)
    $limit = 200
    if ($maxRel -gt $limit) {
        throw "FAIL: relative path $maxRel > $limit chars — Windows extract will break for many users. Prune/flatten node_modules."
    }

    # Simulated absolute path on a long username
    $fakePrefix = "C:\Users\VeryLongUserNameHere\.opencode\"
    $fakeAbs = $fakePrefix.Length + $maxRel
    Write-Host "Simulated abs length under long profile: $fakeAbs"
    if ($fakeAbs -gt 250) {
        Write-Host "WARNING: would exceed classic MAX_PATH under user profile — install uses short root C:\vd\opencode"
    }
} finally {
    Remove-Item -LiteralPath $tmpAudit -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Step "Simulate deep Downloads extract path (outer package)"
$deepRoot = Join-Path $env:TEMP "vd-dl\virtual_developer-windows-x64-dev-test\nested\again"
if (Test-Path -LiteralPath $deepRoot) {
    Remove-Item -LiteralPath $deepRoot -Recurse -Force
}
New-Item -ItemType Directory -Path $deepRoot -Force | Out-Null
# Copy payload (not node_modules tree — should be fast)
Write-Host "Copying payload -> $deepRoot"
robocopy $PayloadDir $deepRoot /E /NFL /NDL /NJH /NJS /nc /ns /np /R:1 /W:1 | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy payload failed: $LASTEXITCODE" }

Write-Step "Defender exclusions (large opencode.exe is often quarantined on runners)"
$excludePaths = @($deepRoot, "C:\vd", "C:\vd\opencode")
foreach ($ep in $excludePaths) {
    try {
        Add-MpPreference -ExclusionPath $ep -ErrorAction Stop
        Write-Host "  exclusion: $ep"
    } catch {
        Write-Host "  (skip exclusion $ep : $($_.Exception.Message))"
    }
}

Write-Step "Run install.bat non-interactively"
$env:VD_NONINTERACTIVE = "1"
$env:VD_OPENCODE_ROOT = "C:\vd\opencode"  # short path used by product
# Clean previous smoke install
if (Test-Path -LiteralPath $env:VD_OPENCODE_ROOT) {
    Remove-Item -LiteralPath $env:VD_OPENCODE_ROOT -Recurse -Force -ErrorAction SilentlyContinue
}
$installBat = Join-Path $deepRoot "install.bat"
$p = Start-Process -FilePath "cmd.exe" -ArgumentList @("/c", "`"$installBat`"") -WorkingDirectory $deepRoot -Wait -PassThru -NoNewWindow
if ($p.ExitCode -ne 0) {
    $oc = Join-Path $env:VD_OPENCODE_ROOT "bin\opencode.exe"
    if (Test-Path -LiteralPath $oc) {
        Write-Host "DEBUG opencode.exe size after failed install: $((Get-Item $oc).Length)"
    }
    throw "install.bat failed with exit code $($p.ExitCode)"
}

Write-Step "Verify installed OpenCode (AMD64 + --version)"
$oc = Join-Path $env:VD_OPENCODE_ROOT "bin\opencode.exe"
if (-not (Test-Path -LiteralPath $oc)) {
    throw "opencode.exe missing after install: $oc"
}
$assert = Join-Path $PayloadDir "packaging\windows\Assert-Amd64Pe.ps1"
& $assert -Path $oc
if ($LASTEXITCODE -ne 0) { throw "AMD64 assert failed" }

$ver = & $oc --version 2>&1
Write-Host "opencode --version => $ver"
if ($LASTEXITCODE -ne 0) {
    throw "opencode --version failed (exit $LASTEXITCODE): $ver"
}

Write-Step "E2E smoke PASSED"
exit 0
