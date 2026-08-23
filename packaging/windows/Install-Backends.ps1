#Requires -Version 5.1
<#
.SYNOPSIS
  Offline install of agent workers only: OpenCode and/or Codex.

.DESCRIPTION
  Does not create a Python venv, install dashboard deps, or write .env.
  Uses vendor\ from the CI zip.

  OpenCode: %USERPROFILE%\.opencode
  Codex:    %LOCALAPPDATA%\Programs\OpenAI\Codex\bin\codex.exe
            (official standalone path, same as chatgpt.com/codex/install.ps1)
  Callers:  install-backends.bat (default both; -OpenCode / -Codex)
            install-codex.bat    (-Codex only)

  ASCII-only. Do not name parameters after PowerShell automatic variables.
#>
[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [switch]$OpenCode,
    [switch]$Codex
)

$ErrorActionPreference = "Stop"

if (-not $RepoRoot) {
    $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
} else {
    $RepoRoot = (Resolve-Path $RepoRoot).Path
}

$doOpenCode = $true
$doCodex = $true
if ($OpenCode -or $Codex) {
    $doOpenCode = [bool]$OpenCode
    $doCodex = [bool]$Codex
}

function Read-Versions([string]$Path) {
    $map = @{}
    if (-not (Test-Path -LiteralPath $Path)) { return $map }
    Get-Content -Path $Path | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#")) { return }
        $parts = $line -split "=", 2
        if ($parts.Count -eq 2) {
            $map[$parts[0].Trim()] = $parts[1].Trim()
        }
    }
    return $map
}

function Add-UserPath([string]$Dir) {
    if (-not $Dir) { return }
    $raw = [Environment]::GetEnvironmentVariable("Path", "User")
    $parts = @()
    if ($raw) {
        $parts = @($raw -split ";" | Where-Object { $_ })
    }
    $norm = $Dir.TrimEnd("\")
    foreach ($p in $parts) {
        if ($p.TrimEnd("\").ToLowerInvariant() -eq $norm.ToLowerInvariant()) {
            Write-Host "[OK] User PATH already contains $Dir"
            return
        }
    }
    $new = ($Dir + ";" + ($parts -join ";")).Trim(";")
    [Environment]::SetEnvironmentVariable("Path", $new, "User")
    Write-Host "[OK] Prepended to user PATH: $Dir"
}

function Remove-UserPath([string]$Dir) {
    if (-not $Dir) { return }
    $raw = [Environment]::GetEnvironmentVariable("Path", "User")
    if (-not $raw) { return }
    $norm = $Dir.TrimEnd("\")
    $kept = @($raw -split ";" | Where-Object {
            $_ -and ($_.TrimEnd("\").ToLowerInvariant() -ne $norm.ToLowerInvariant())
        })
    $new = ($kept -join ";").Trim(";")
    if ($new -ne $raw) {
        [Environment]::SetEnvironmentVariable("Path", $new, "User")
        Write-Host "[OK] Removed from user PATH: $Dir"
    }
}

function Remove-Tree([string]$Path) {
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return }
    Write-Host "  Removing $Path"
    try {
        $item = Get-Item -LiteralPath $Path -Force
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            cmd /c "rmdir `"$Path`"" | Out-Null
        } else {
            Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
        }
    } catch {
        Write-Host "  [WARNING] Could not fully remove $Path"
    }
}

function Assert-JsonFile([string]$Path) {
    $raw = Get-Content -LiteralPath $Path -Raw -Encoding UTF8
    $null = $raw | ConvertFrom-Json
}

function Restore-Pe([string]$Target, [string]$Backup, [long]$MinBytes) {
    $assert = Join-Path $PSScriptRoot "Assert-Amd64Pe.ps1"
    $ok = (Test-Path -LiteralPath $Target) -and ((Get-Item -LiteralPath $Target).Length -ge $MinBytes)
    if ($ok) { return }
    if (-not (Test-Path -LiteralPath $Backup)) {
        throw "Missing backup: $Backup"
    }
    $destDir = Split-Path $Target -Parent
    if (-not (Test-Path -LiteralPath $destDir)) {
        New-Item -ItemType Directory -Path $destDir -Force | Out-Null
    }
    Copy-Item -LiteralPath $Backup -Destination $Target -Force
    Unblock-File -LiteralPath $Target -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $assert) {
        & $assert -Path $Target -MinBytes $MinBytes
        if ($LASTEXITCODE -ne 0) { throw "PE check failed: $Target" }
    }
}

$vendor = Join-Path $RepoRoot "vendor"
$pkgWin = Join-Path $RepoRoot "packaging\windows"
$verFile = Join-Path $pkgWin "versions.env"
if (Test-Path -LiteralPath (Join-Path $vendor "versions.env")) {
    $verFile = Join-Path $vendor "versions.env"
}
$ver = Read-Versions $verFile
$omoVer = if ($ver["OH_MY_OPENCODE_VERSION"]) { $ver["OH_MY_OPENCODE_VERSION"] } else { "4.19.3" }
$codexVer = if ($ver["CODEX_VERSION"]) { $ver["CODEX_VERSION"] } else { "0.149.0" }

$userHome = $env:USERPROFILE
if (-not $userHome) { $userHome = $env:HOME }
if (-not $userHome) { $userHome = (Get-Location).Path }
$sysDrive = $env:SystemDrive
if (-not $sysDrive) { $sysDrive = "C:" }
$localApp = $env:LOCALAPPDATA
if (-not $localApp) {
    $localApp = Join-Path $userHome "AppData\Local"
}

if ($env:VD_OPENCODE_ROOT) {
    $ocHome = $env:VD_OPENCODE_ROOT
} else {
    $ocHome = Join-Path $userHome ".opencode"
}
$ocBin = Join-Path $ocHome "bin"
$userOc = Join-Path $userHome ".opencode"
# String concat (not Join-Path): SystemDrive is "C:" and must not require a mounted drive.
$legacy1 = "$sysDrive\vd\opencode"
$legacy2 = "$localApp\vd\opencode"
$codexBin = Join-Path $localApp "Programs\OpenAI\Codex\bin"
$codexExe = Join-Path $codexBin "codex.exe"

Write-Host "========================================"
Write-Host "  Virtual Developer - Backends only"
Write-Host "========================================"
Write-Host "Project     : $RepoRoot"
Write-Host "OpenCode    : $(if ($doOpenCode) { $ocHome } else { 'skip' })"
Write-Host "Codex       : $(if ($doCodex) { $codexExe } else { 'skip' })"
Write-Host "Python/venv : not installed by this script"
Write-Host ""

if ($doOpenCode) {
    $zip = Join-Path $vendor "opencode-home.zip"
    $expanded = Join-Path $vendor "opencode-home\bin\opencode.exe"
    if (-not (Test-Path -LiteralPath $zip) -and -not (Test-Path -LiteralPath $expanded)) {
        throw "vendor\opencode-home.zip missing. Use the CI offline zip."
    }

    Write-Host "Step 1: Cleaning previous OpenCode install at $ocHome ..."
    $installingDefaultHome = (
        $ocHome.ToLowerInvariant() -eq $userOc.ToLowerInvariant()
    )
    Remove-UserPath (Join-Path $legacy1 "bin")
    Remove-UserPath (Join-Path $legacy2 "bin")
    Remove-UserPath $ocBin
    if ($installingDefaultHome) {
        Remove-UserPath (Join-Path $userOc "bin")
        $cfgDir = Join-Path $userHome ".config\opencode"
        foreach ($name in @("opencode.json", "oh-my-opencode.json", "package.json")) {
            $p = Join-Path $cfgDir $name
            if (Test-Path -LiteralPath $p) { Remove-Item -LiteralPath $p -Force -ErrorAction SilentlyContinue }
        }
        Remove-Tree (Join-Path $cfgDir "node_modules")
        Remove-Tree (Join-Path $userHome ".cache\opencode")
        if ($env:APPDATA) {
            Remove-Tree (Join-Path $env:APPDATA "opencode\node_modules")
        }
        Remove-Tree $userOc
    } else {
        # Isolated VD_OPENCODE_ROOT: never touch the user's default OpenCode home/cache.
        Remove-Tree $ocHome
    }
    if ($legacy1.ToLowerInvariant() -ne $ocHome.ToLowerInvariant()) { Remove-Tree $legacy1 }
    if ($legacy2.ToLowerInvariant() -ne $ocHome.ToLowerInvariant()) { Remove-Tree $legacy2 }

    $cfgDir = Join-Path $userHome ".config\opencode"
    $cache = Join-Path $userHome ".cache\opencode"
    if (-not $installingDefaultHome) {
        $cfgDir = Join-Path $ocHome ".config-opencode"
        $cache = Join-Path $ocHome ".cache-opencode"
    }

    New-Item -ItemType Directory -Path $ocBin -Force | Out-Null

    Write-Host "Step 2: Installing OpenCode..."
    $extract = Join-Path $pkgWin "extract-opencode-home.ps1"
    if (Test-Path -LiteralPath $zip) {
        if (-not (Test-Path -LiteralPath $extract)) {
            throw "Missing $extract"
        }
        & $extract -Zip $zip -Dest $ocHome
        if ($LASTEXITCODE -ne 0) { throw "extract-opencode-home failed" }
    } else {
        $src = Join-Path $vendor "opencode-home"
        $rc = Start-Process -FilePath "robocopy.exe" -ArgumentList @(
            $src, $ocHome, "/E", "/NFL", "/NDL", "/NJH", "/NJS", "/nc", "/ns", "/np", "/R:2", "/W:1"
        ) -Wait -PassThru -NoNewWindow
        if ($rc.ExitCode -ge 8) { throw "robocopy of expanded opencode-home failed" }
    }

    $ocExe = Join-Path $ocBin "opencode.exe"
    $backupOc = Join-Path $vendor "bin\opencode.exe"
    Restore-Pe -Target $ocExe -Backup $backupOc -MinBytes 10MB
    $backupGl = Join-Path $vendor "bin\glab.exe"
    if (Test-Path -LiteralPath $backupGl) {
        Copy-Item -LiteralPath $backupGl -Destination (Join-Path $ocBin "glab.exe") -Force
        Unblock-File -LiteralPath (Join-Path $ocBin "glab.exe") -ErrorAction SilentlyContinue
    }

    $verOut = & $ocExe --version 2>&1
    Write-Host ($verOut | Out-String)
    if ($LASTEXITCODE -ne 0) { throw "opencode --version failed" }
    Write-Host "[OK] OpenCode at $ocExe"

    Write-Host "Step 3: OpenCode stock config (no oh-my-openagent)..."
    foreach ($name in @("opencode.json", "package.json")) {
        $dest = Join-Path $ocHome $name
        $src = Join-Path $pkgWin $name
        if ((-not (Test-Path -LiteralPath $dest)) -and (Test-Path -LiteralPath $src)) {
            Copy-Item -LiteralPath $src -Destination $dest -Force
        }
    }
    $homeCfg = Join-Path $ocHome "opencode.json"
    if (-not (Test-Path -LiteralPath $homeCfg)) {
        throw "opencode.json missing under $ocHome"
    }
    try {
        Assert-JsonFile $homeCfg
    } catch {
        if (Test-Path -LiteralPath (Join-Path $pkgWin "opencode.json")) {
            Copy-Item -LiteralPath (Join-Path $pkgWin "opencode.json") -Destination $homeCfg -Force
        }
        Assert-JsonFile $homeCfg
    }
    $pin = Join-Path $pkgWin "Pin-OpencodePlugin.ps1"
    if (Test-Path -LiteralPath $pin) {
        & $pin -ConfigPath $homeCfg
    }

    if (-not (Test-Path -LiteralPath $cfgDir)) {
        New-Item -ItemType Directory -Path $cfgDir -Force | Out-Null
    }
    foreach ($name in @("opencode.json", "package.json")) {
        $src = Join-Path $ocHome $name
        if (Test-Path -LiteralPath $src) {
            Copy-Item -LiteralPath $src -Destination (Join-Path $cfgDir $name) -Force
        }
    }
    Assert-JsonFile (Join-Path $cfgDir "opencode.json")
    Write-Host "[OK] Mirrored stock config to $cfgDir"

    $rgSrc = Join-Path $vendor "bin\rg.exe"
    if (-not (Test-Path -LiteralPath $rgSrc)) {
        $rgSrc = Join-Path $ocBin "rg.exe"
    }
    if (Test-Path -LiteralPath $rgSrc) {
        $rgDir = Join-Path $cache "bin"
        New-Item -ItemType Directory -Path $rgDir -Force | Out-Null
        Copy-Item -LiteralPath $rgSrc -Destination (Join-Path $rgDir "rg.exe") -Force
        Copy-Item -LiteralPath $rgSrc -Destination (Join-Path $ocBin "rg.exe") -Force
        Write-Host "[OK] ripgrep seeded"
    }

    [Environment]::SetEnvironmentVariable("OPENCODE_DISABLE_MODELS_FETCH", "1", "User")
    $env:OPENCODE_DISABLE_MODELS_FETCH = "1"

    # Isolated VD_OPENCODE_ROOT stays isolated. Do not junction over %USERPROFILE%\.opencode.
    Add-UserPath $ocBin
    $env:Path = $ocBin + ";" + $env:Path
}

if ($doCodex) {
    Write-Host ""
    Write-Host "Step 4: Installing Codex CLI $codexVer (official path)..."
    $backupCx = Join-Path $vendor "bin\codex.exe"
    if (-not (Test-Path -LiteralPath $backupCx)) {
        throw "vendor\bin\codex.exe missing. Use the CI offline zip."
    }
    New-Item -ItemType Directory -Path $codexBin -Force | Out-Null
    Copy-Item -LiteralPath $backupCx -Destination $codexExe -Force
    Unblock-File -LiteralPath $codexExe -ErrorAction SilentlyContinue
    Restore-Pe -Target $codexExe -Backup $backupCx -MinBytes 5MB
    $stale = Join-Path $ocBin "codex.exe"
    if (Test-Path -LiteralPath $stale) {
        Remove-Item -LiteralPath $stale -Force -ErrorAction SilentlyContinue
        Write-Host "  Removed leftover $stale"
    }
    $cxOut = & $codexExe --version 2>&1
    Write-Host ($cxOut | Out-String)
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[WARNING] codex.exe failed to start; OpenCode is still installed"
    } else {
        Write-Host "[OK] Codex at $codexExe"
    }
    Add-UserPath $codexBin
    $env:Path = $codexBin + ";" + $env:Path
}

Write-Host ""
Write-Host "Backends install complete."
if ($doOpenCode) { Write-Host "  OpenCode : $ocBin\opencode.exe" }
if ($doCodex) { Write-Host "  Codex    : $codexExe" }
Write-Host "Open a NEW terminal so PATH updates apply."
Write-Host "Dashboard/Python: install-dashboard.bat"
exit 0
