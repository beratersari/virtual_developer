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
            Extracted from vendor\codex-package-x86_64-pc-windows-msvc.tar.gz
            with tar.exe (offline CI zip only). Dummy config is copied
            to %USERPROFILE%\.codex\config.toml when that file is missing.
  Callers:  install-backends.bat (default both; -OpenCode / -Codex)
            install-codex.bat    (-Codex only)

  ASCII-only. Do not name parameters after PowerShell automatic variables.
#>
[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [switch]$OpenCode,
    [switch]$Codex,
    [string]$CodexExtract = ""
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

function Expand-TarGz([string]$Archive, [string]$Dest) {
    if (-not (Test-Path -LiteralPath $Archive)) {
        throw "Archive missing: $Archive"
    }
    if (Test-Path -LiteralPath $Dest) {
        Remove-Item -LiteralPath $Dest -Recurse -Force
    }
    New-Item -ItemType Directory -Path $Dest -Force | Out-Null
    $tar = Join-Path $env:SystemRoot "System32\tar.exe"
    if (-not (Test-Path -LiteralPath $tar)) {
        $cmd = Get-Command tar.exe -ErrorAction SilentlyContinue
        if ($cmd) { $tar = $cmd.Source } else { $tar = "tar" }
    }
    $absArchive = [IO.Path]::GetFullPath($Archive)
    $absDest = [IO.Path]::GetFullPath($Dest)
    Write-Host "  tar extract: $absArchive"
    Push-Location $absDest
    try {
        & $tar --force-local -xf $absArchive
        if ($LASTEXITCODE -ne 0) {
            & $tar -xf $absArchive
        }
        if ($LASTEXITCODE -ne 0) {
            throw "tar extract failed (exit $LASTEXITCODE): $absArchive"
        }
    } finally {
        Pop-Location
    }
}

function Find-CodexExe([string]$Root) {
    if (-not $Root -or -not (Test-Path -LiteralPath $Root)) { return $null }
    $hit = Get-ChildItem -Path $Root -Filter "codex.exe" -Recurse -File -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($hit) { return $hit.FullName }
    $hit = Get-ChildItem -Path $Root -Filter "codex-*.exe" -Recurse -File -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($hit) { return $hit.FullName }
    return $null
}

function Install-DummyCodexConfig([string]$DummySrc, [string]$UserHome) {
    $cfgDir = Join-Path $UserHome ".codex"
    $cfg = Join-Path $cfgDir "config.toml"
    if (-not (Test-Path -LiteralPath $cfgDir)) {
        New-Item -ItemType Directory -Path $cfgDir -Force | Out-Null
    }
    if (Test-Path -LiteralPath $cfg) {
        Write-Host "[OK] Keeping existing $cfg"
        return
    }
    if (-not $DummySrc -or -not (Test-Path -LiteralPath $DummySrc)) {
        Write-Host "[WARNING] Dummy Codex config not found: $DummySrc"
        return
    }
    Copy-Item -LiteralPath $DummySrc -Destination $cfg -Force
    Write-Host "[OK] Seeded dummy config.toml at $cfg"
}

function Invoke-VdPython([string]$RepoRoot, [string[]]$PyArgs) {
    $venvPy = Join-Path $RepoRoot ".venv\Scripts\python.exe"
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
$codexAsset = if ($ver["CODEX_WINDOWS_ASSET"]) {
    $ver["CODEX_WINDOWS_ASSET"]
} else {
    "codex-package-x86_64-pc-windows-msvc.tar.gz"
}

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
Write-Host "OpenCode    : $(if ($doOpenCode) { $userOc } else { 'skip' }) (opencoderman)"
Write-Host "Codex       : $(if ($doCodex) { $codexExe } else { 'skip' })"
Write-Host "Python/venv : not installed by this script"
Write-Host ""

if ($doOpenCode) {
    $ocm = Join-Path $RepoRoot "opencoderman"
    $installer = Join-Path $RepoRoot "packaging\install_opencode.py"
    if (-not (Test-Path -LiteralPath (Join-Path $ocm "install.py"))) {
        throw "opencoderman submodule missing ($ocm). Run: git submodule update --init --recursive"
    }
    if (-not (Test-Path -LiteralPath $installer)) {
        throw "Missing $installer"
    }
    $zip = Join-Path $vendor "opencode-home.zip"
    $expanded = Join-Path $vendor "opencode-home\bin\opencode.exe"
    $vendorExe = Join-Path $vendor "bin\opencode.exe"
    $ocmExe = Join-Path $ocm "vendor\bin\windows\opencode.exe"
    if (
        -not (Test-Path -LiteralPath $zip) -and
        -not (Test-Path -LiteralPath $expanded) -and
        -not (Test-Path -LiteralPath $vendorExe) -and
        -not (Test-Path -LiteralPath $ocmExe)
    ) {
        throw "No OpenCode CLI. Need opencoderman\vendor\bin\windows\opencode.exe, vendor\bin\opencode.exe, or vendor\opencode-home.zip (CI zip). Online: install-opencode-online.bat"
    }

    Write-Host "Step 1-3: OpenCode via opencoderman (backup ~/.opencode, CLI + agents + skills)..."
    $pyArgs = @(
        $installer,
        "--repo-root", $RepoRoot,
        "--opencoderman-root", $ocm,
        "--vendor-root", $vendor,
        "--require-binary"
    )
    $ec = Invoke-VdPython $RepoRoot $pyArgs
    if ($ec -ne 0) { throw "opencoderman install failed (exit $ec)" }

    $ocExe = Join-Path $userOc "bin\opencode.exe"
    $backupOc = Join-Path $vendor "bin\opencode.exe"
    if (Test-Path -LiteralPath $backupOc) {
        Restore-Pe -Target $ocExe -Backup $backupOc -MinBytes 10MB
    }
    if (-not (Test-Path -LiteralPath $ocExe)) {
        throw "opencode.exe missing after opencoderman install: $ocExe"
    }
    $homeCfg = Join-Path $userOc "opencode.json"
    if (-not (Test-Path -LiteralPath $homeCfg)) {
        throw "opencode.json missing under $userOc"
    }
    Assert-JsonFile $homeCfg
    $review = Join-Path $userOc "agents\gitlab-reviewer.md"
    if (-not (Test-Path -LiteralPath $review)) {
        throw "opencoderman agent missing: $review"
    }

    $verOut = & $ocExe --version 2>&1
    Write-Host ($verOut | Out-String)
    if ($LASTEXITCODE -ne 0) { throw "opencode --version failed" }
    Write-Host "[OK] OpenCode at $ocExe"

    $ocBin = Join-Path $userOc "bin"
    $env:OPENCODE_DISABLE_MODELS_FETCH = "1"
    $env:Path = $ocBin + ";" + $env:Path
}

if ($doCodex) {
    Write-Host ""
    Write-Host "Step 4: Installing Codex CLI $codexVer from $codexAsset ..."
    $vendorPkg = Join-Path $vendor $codexAsset
    $dummyCfg = Join-Path $pkgWin "codex-config.toml"
    if (-not (Test-Path -LiteralPath $dummyCfg)) {
        $dummyCfg = Join-Path $vendor "codex-config.toml"
    }
    $srcExe = $null
    $extractDir = ""

    if ($CodexExtract -and (Test-Path -LiteralPath $CodexExtract)) {
        $srcExe = Find-CodexExe $CodexExtract
        if ($srcExe) {
            Write-Host "  Using pre-extracted package: $CodexExtract"
        }
    }

    if (-not $srcExe -and (Test-Path -LiteralPath $vendorPkg)) {
        $extractDir = Join-Path $env:TEMP "vd-codex-pkg-ps"
        Expand-TarGz $vendorPkg $extractDir
        $srcExe = Find-CodexExe $extractDir
    }

    if (-not $srcExe) {
        throw "Codex package missing. Need vendor\$codexAsset in the CI offline zip. No download; no vendor\bin\codex.exe."
    }

    New-Item -ItemType Directory -Path $codexBin -Force | Out-Null
    Copy-Item -LiteralPath $srcExe -Destination $codexExe -Force
    Unblock-File -LiteralPath $codexExe -ErrorAction SilentlyContinue
    $stale = Join-Path $ocBin "codex.exe"
    if (Test-Path -LiteralPath $stale) {
        Remove-Item -LiteralPath $stale -Force -ErrorAction SilentlyContinue
        Write-Host "  Removed leftover $stale"
    }
    Install-DummyCodexConfig $dummyCfg $userHome
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
