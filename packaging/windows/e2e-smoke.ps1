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

    # Leave headroom for %USERPROFILE%\.opencode\ (~40-80 chars for typical profiles)
    $limit = 200
    if ($maxRel -gt $limit) {
        throw "FAIL: relative path $maxRel > $limit chars — Windows extract will break for many users. Prune/flatten node_modules."
    }

    # Simulated absolute path on a long username under the product default home
    $fakePrefix = "C:\Users\VeryLongUserNameHere\.opencode\"
    $fakeAbs = $fakePrefix.Length + $maxRel
    Write-Host "Simulated abs length under long profile: $fakeAbs"
    if ($fakeAbs -gt 250) {
        Write-Host "WARNING: would exceed classic MAX_PATH under user profile — long-path extract is required"
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

# Product default: %USERPROFILE%\.opencode (override only for advanced short-path tests)
$ocHome = Join-Path $env:USERPROFILE ".opencode"
$ocConfigDir = Join-Path $env:USERPROFILE ".config\opencode"

Write-Step "Defender: disable realtime + exclusions (runner quarantine eats opencode.exe)"
try {
    Set-MpPreference -DisableRealtimeMonitoring $true -ErrorAction Stop
    Write-Host "  realtime monitoring disabled"
} catch {
    Write-Host "  (could not disable realtime: $($_.Exception.Message))"
}
$excludePaths = @(
    $deepRoot,
    $ocHome,
    $ocConfigDir,
    (Join-Path $deepRoot "vendor\bin"),
    (Join-Path $PayloadDir "vendor\bin")
)
foreach ($ep in $excludePaths) {
    try {
        Add-MpPreference -ExclusionPath $ep -ErrorAction Stop
        Write-Host "  exclusion: $ep"
    } catch {
        Write-Host "  (skip exclusion $ep : $($_.Exception.Message))"
    }
}
# Prefer process exclusion for the binary name when possible
try {
    Add-MpPreference -ExclusionProcess "opencode.exe" -ErrorAction Stop
    Write-Host "  exclusion process: opencode.exe"
} catch {
    Write-Host "  (skip process exclusion: $($_.Exception.Message))"
}

Write-Step "Run install.bat non-interactively (home = %USERPROFILE%\.opencode)"
$env:VD_NONINTERACTIVE = "1"
# Do NOT set VD_OPENCODE_ROOT — product default is %USERPROFILE%\.opencode
Remove-Item Env:VD_OPENCODE_ROOT -ErrorAction SilentlyContinue
# Clean previous smoke install (real dir or leftover junction)
if (Test-Path -LiteralPath $ocHome) {
    Remove-Item -LiteralPath $ocHome -Recurse -Force -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $ocHome) {
        cmd /c "rmdir `"$ocHome`"" 2>$null
    }
}
if (Test-Path -LiteralPath (Join-Path $ocConfigDir "opencode.json")) {
    Remove-Item -LiteralPath (Join-Path $ocConfigDir "opencode.json") -Force -ErrorAction SilentlyContinue
}

$installBat = Join-Path $deepRoot "install.bat"
$p = Start-Process -FilePath "cmd.exe" -ArgumentList @("/c", "`"$installBat`"") -WorkingDirectory $deepRoot -Wait -PassThru -NoNewWindow
if ($p.ExitCode -ne 0) {
    $oc = Join-Path $ocHome "bin\opencode.exe"
    if (Test-Path -LiteralPath $oc) {
        Write-Host "DEBUG opencode.exe size after failed install: $((Get-Item $oc).Length)"
    }
    throw "install.bat failed with exit code $($p.ExitCode)"
}

Write-Step "Verify installed OpenCode (AMD64 + --version + valid config JSON)"
$oc = Join-Path $ocHome "bin\opencode.exe"
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

# Regression: unescaped "echo ... -> path" used to overwrite opencode.json with "[OK] config ..."
$homeCfg = Join-Path $ocHome "opencode.json"
$globalCfg = Join-Path $ocConfigDir "opencode.json"
foreach ($cfg in @($homeCfg, $globalCfg)) {
    if (-not (Test-Path -LiteralPath $cfg)) {
        throw "Missing config after install: $cfg"
    }
    $raw = Get-Content -LiteralPath $cfg -Raw -ErrorAction Stop
    if ($raw -match '\[OK\]' -or $raw -match 'config\s+-') {
        throw "FAIL: config looks like install.bat echo output (redirect bug): $cfg => $raw"
    }
    try {
        $null = $raw | ConvertFrom-Json
    } catch {
        throw "FAIL: config is not valid JSON: $cfg => $raw"
    }
    Write-Host "OK valid JSON: $cfg"
}

if (-not (Test-Path -LiteralPath (Join-Path $ocHome "node_modules\oh-my-opencode"))) {
    throw "oh-my-opencode plugin missing under $ocHome\node_modules"
}

# OpenCode loads npm plugins from ~/.cache/opencode — must be seeded offline
$cachePlugin = Join-Path $env:USERPROFILE ".cache\opencode\node_modules\oh-my-opencode"
if (-not (Test-Path -LiteralPath $cachePlugin)) {
    throw "Plugin cache not seeded (black-screen cause): $cachePlugin"
}
Write-Host "OK plugin cache: $cachePlugin"

# Config must pin plugin version (unversioned => Bun fetch hang / black TUI)
$globalCfg = Join-Path $ocConfigDir "opencode.json"
$cfgObj = Get-Content -LiteralPath $globalCfg -Raw | ConvertFrom-Json
$plugins = @($cfgObj.plugin)
$pinned = $plugins | Where-Object { $_ -match '^oh-my-opencode@' }
if (-not $pinned) {
    throw "FAIL: opencode.json plugin not version-pinned: $($plugins -join ', ')"
}
if ($cfgObj.autoupdate -ne $false) {
    Write-Host "WARNING: autoupdate is not false (may hang offline)"
}
Write-Host "OK pinned plugin: $($pinned -join ', ')"

# Non-interactive command must return quickly (no TUI black-screen hang)
Write-Step "Non-interactive opencode smoke (debug config / run --help)"
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $oc
$psi.Arguments = "debug config"
$psi.UseShellExecute = $false
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
$psi.CreateNoWindow = $true
$proc = [System.Diagnostics.Process]::Start($psi)
if (-not $proc.WaitForExit(120000)) {
    try { $proc.Kill() } catch {}
    throw "opencode debug config hung >120s (black-screen class failure)"
}
$stdout = $proc.StandardOutput.ReadToEnd()
$stderr = $proc.StandardError.ReadToEnd()
Write-Host "debug config exit=$($proc.ExitCode)"
if ($proc.ExitCode -ne 0) {
    Write-Host "stdout: $stdout"
    Write-Host "stderr: $stderr"
    # debug subcommand may differ by version — try run --help
    $help = & $oc run --help 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "opencode non-interactive commands failed (exit $($proc.ExitCode) / run --help $LASTEXITCODE)"
    }
    Write-Host "OK opencode run --help"
} else {
    Write-Host "OK opencode debug config"
}

Write-Step "E2E smoke PASSED"
exit 0
