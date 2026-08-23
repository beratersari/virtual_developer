#Requires -Version 5.1
<#
.SYNOPSIS
  Online install of OpenCode + oh-my-openagent for Virtual Developer (Windows).

.DESCRIPTION
  ONLINE-ONLY installer. Does not change offline install-backends.bat.

  Requires portable Node from the dist zip:
    vendor\node\node.exe
    vendor\node\npm.cmd

  npm registry / mirror:
    packaging\windows\npm-online.npmrc  (edit registry= for your server)
    or env NPM_REGISTRY / online-sources.env NPM_REGISTRY

  Optional binary download mirrors (HTTP gateway to your FTP/fileserver):
    packaging\windows\online-sources.env
    OPENCODE_ZIP_URL / GLAB_ZIP_URL / RG_ZIP_URL

  Layout (same product paths as offline install-backends.bat):
    %USERPROFILE%\.opencode\bin\opencode.exe
    %USERPROFILE%\.opencode\node_modules\oh-my-openagent  (+ oh-my-opencode alias)
    %USERPROFILE%\.opencode\opencode.json  (plugin=[], stock build/plan)
    %USERPROFILE%\.config\opencode\        (mirrored configs + node_modules)
    %USERPROFILE%\.cache\opencode\         (full plugin tree for Bun + rg.exe)

  ASCII only (Windows PowerShell 5.1 / cmd callers).
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [string]$RepoRoot = "",

    [Parameter(Mandatory = $false)]
    [string]$VersionsFile = "",

    [Parameter(Mandatory = $false)]
    [string]$NpmRegistry = ""
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "=== $Message ==="
}

function Ensure-Dir([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
    }
}

function Read-Versions([string]$Path) {
    $map = @{}
    if (-not (Test-Path -LiteralPath $Path)) { return $map }
    Get-Content -LiteralPath $Path -Encoding UTF8 | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#")) { return }
        $i = $line.IndexOf("=")
        if ($i -lt 1) { return }
        $k = $line.Substring(0, $i).Trim()
        $v = $line.Substring($i + 1).Trim()
        if ($k) { $map[$k] = $v }
    }
    return $map
}

function Download-File([string]$Url, [string]$OutFile) {
    Ensure-Dir (Split-Path -Parent $OutFile)
    Write-Host "  GET $Url"
    Invoke-WebRequest -Uri $Url -OutFile $OutFile -UseBasicParsing
    if (-not (Test-Path -LiteralPath $OutFile)) {
        throw "Download failed: $Url"
    }
}

function Expand-ZipSafe([string]$Zip, [string]$Dest) {
    Ensure-Dir $Dest
    if (Get-Command tar -ErrorAction SilentlyContinue) {
        & tar -xf $Zip -C $Dest
        if ($LASTEXITCODE -eq 0) { return }
    }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::ExtractToDirectory($Zip, $Dest)
}

function Force-RemoveDir([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    # Junction: remove link only
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if ($item -and ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        cmd /c "rmdir `"$Path`"" | Out-Null
        return
    }
    Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction SilentlyContinue
}

function Robocopy-Tree([string]$Src, [string]$Dest) {
    Ensure-Dir $Dest
    $p = Start-Process -FilePath "robocopy.exe" -ArgumentList @(
        $Src, $Dest, "/E", "/NFL", "/NDL", "/NJH", "/NJS", "/nc", "/ns", "/np", "/R:2", "/W:1"
    ) -Wait -PassThru -NoNewWindow
    # robocopy: 0-7 success
    if ($p.ExitCode -ge 8) {
        throw "robocopy failed ($($p.ExitCode)): $Src -> $Dest"
    }
}

function Prepend-UserPath([string]$Dir) {
    if (-not $Dir) { return }
    $raw = [Environment]::GetEnvironmentVariable("Path", "User")
    $parts = @()
    if ($raw) {
        $parts = @($raw -split ";" | Where-Object { $_ -and $_.Trim() })
    }
    $norm = $Dir.TrimEnd("\")
    $parts = @($parts | Where-Object {
            $_.TrimEnd("\").ToLowerInvariant() -ne $norm.ToLowerInvariant()
        })
    $new = ($Dir + ";" + ($parts -join ";")).Trim(";")
    [Environment]::SetEnvironmentVariable("Path", $new, "User")
    $env:Path = $Dir + ";" + $env:Path
    Write-Host "  PATH (user) prepended: $Dir"
}

function Remove-UserPathEntry([string]$Dir) {
    if (-not $Dir) { return }
    $raw = [Environment]::GetEnvironmentVariable("Path", "User")
    if (-not $raw) { return }
    $norm = $Dir.TrimEnd("\")
    $parts = @($raw -split ";" | Where-Object {
            $_ -and ($_.TrimEnd("\").ToLowerInvariant() -ne $norm.ToLowerInvariant())
        })
    $new = ($parts -join ";").Trim(";")
    [Environment]::SetEnvironmentVariable("Path", $new, "User")
}

function Resolve-BundledNode([string]$Root) {
    # Online installer REQUIRES our shipped portable Node (no system Node).
    $candidates = @(
        (Join-Path $Root "vendor\node\node.exe"),
        (Join-Path $Root "vendor\node\bin\node.exe")
    )
    foreach ($c in $candidates) {
        if (Test-Path -LiteralPath $c) {
            $dir = Split-Path -Parent $c
            $npm = Join-Path $dir "npm.cmd"
            if (-not (Test-Path -LiteralPath $npm)) {
                throw "Found node.exe but npm.cmd missing next to it: $dir"
            }
            return @{ NodeExe = $c; NodeDir = $dir; NpmCmd = $npm }
        }
    }
    $vendorNode = Join-Path $Root "vendor\node"
    if (Test-Path -LiteralPath $vendorNode) {
        $nested = Get-ChildItem -Path $vendorNode -Filter "node.exe" -Recurse -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($nested) {
            $dir = $nested.DirectoryName
            $npm = Join-Path $dir "npm.cmd"
            if (-not (Test-Path -LiteralPath $npm)) {
                throw "Found nested node.exe but npm.cmd missing: $dir"
            }
            return @{ NodeExe = $nested.FullName; NodeDir = $dir; NpmCmd = $npm }
        }
    }
    return $null
}

function Resolve-NpmrcPath([string]$Root) {
    # Prefer user-edited copy under vendor\ (survives re-copy of packaging templates)
    $cands = @(
        (Join-Path $Root "vendor\npm-online.npmrc"),
        (Join-Path $Root "packaging\windows\npm-online.npmrc")
    )
    foreach ($c in $cands) {
        if (Test-Path -LiteralPath $c) { return $c }
    }
    return $null
}

function Get-RegistryFromNpmrc([string]$NpmrcPath) {
    if (-not $NpmrcPath -or -not (Test-Path -LiteralPath $NpmrcPath)) { return $null }
    foreach ($line in (Get-Content -LiteralPath $NpmrcPath -Encoding UTF8)) {
        $t = $line.Trim()
        if (-not $t -or $t.StartsWith("#") -or $t.StartsWith(";")) { continue }
        if ($t -match '^\s*registry\s*=\s*(.+)\s*$') {
            return $Matches[1].Trim().Trim('"').Trim("'")
        }
    }
    return $null
}

# ---------------------------------------------------------------------------
# Resolve roots + versions
# ---------------------------------------------------------------------------
if (-not $RepoRoot) {
    $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
}
$RepoRoot = $RepoRoot.TrimEnd("\")

if (-not $VersionsFile) {
    $vfVendor = Join-Path $RepoRoot "vendor\versions.env"
    $vfPkg = Join-Path $RepoRoot "packaging\windows\versions.env"
    if (Test-Path -LiteralPath $vfVendor) { $VersionsFile = $vfVendor }
    else { $VersionsFile = $vfPkg }
}

$ver = Read-Versions $VersionsFile
$OPENCODE_VERSION = if ($ver["OPENCODE_VERSION"]) { $ver["OPENCODE_VERSION"] } else { "1.18.10" }
$OH_MY = if ($ver["OH_MY_OPENCODE_VERSION"]) { $ver["OH_MY_OPENCODE_VERSION"] } else { "4.19.3" }
$GLAB_VERSION = if ($ver["GLAB_VERSION"]) { $ver["GLAB_VERSION"] } else { "1.111.0" }
$NODE_FULL = if ($ver["NODE_FULL_VERSION"]) { $ver["NODE_FULL_VERSION"] } else { "20.19.0" }

# Optional online-only source overrides (FTP/fileserver HTTP gateways)
$onlineSources = Join-Path $RepoRoot "vendor\online-sources.env"
if (-not (Test-Path -LiteralPath $onlineSources)) {
    $onlineSources = Join-Path $RepoRoot "packaging\windows\online-sources.env"
}
$srcMap = Read-Versions $onlineSources
$OPENCODE_ZIP_URL = if ($env:OPENCODE_ZIP_URL) { $env:OPENCODE_ZIP_URL } elseif ($srcMap["OPENCODE_ZIP_URL"]) { $srcMap["OPENCODE_ZIP_URL"] } else { "" }
$GLAB_ZIP_URL = if ($env:GLAB_ZIP_URL) { $env:GLAB_ZIP_URL } elseif ($srcMap["GLAB_ZIP_URL"]) { $srcMap["GLAB_ZIP_URL"] } else { "" }
$RG_ZIP_URL = if ($env:RG_ZIP_URL) { $env:RG_ZIP_URL } elseif ($srcMap["RG_ZIP_URL"]) { $srcMap["RG_ZIP_URL"] } else { "" }

if ($env:VD_OPENCODE_ROOT) {
    $OPENCODE_HOME = $env:VD_OPENCODE_ROOT.TrimEnd("\")
} else {
    $OPENCODE_HOME = Join-Path $env:USERPROFILE ".opencode"
}
$OPENCODE_BIN = Join-Path $OPENCODE_HOME "bin"
$OC_CONFIG = Join-Path $env:USERPROFILE ".config\opencode"
$OC_CACHE = Join-Path $env:USERPROFILE ".cache\opencode"
$USER_OC = Join-Path $env:USERPROFILE ".opencode"
$LEGACY1 = Join-Path $env:SystemDrive "vd\opencode"
$LEGACY2 = Join-Path $env:LOCALAPPDATA "vd\opencode"

Write-Host "Repo root      : $RepoRoot"
Write-Host "OpenCode home  : $OPENCODE_HOME"
Write-Host "OpenCode ver   : $OPENCODE_VERSION"
Write-Host "oh-my-openagent: $OH_MY"
Write-Host "Node (portable): $NODE_FULL (vendor\node required)"
Write-Host "Mode           : ONLINE only (offline install-backends.bat is separate)"

# ---------------------------------------------------------------------------
# 1) Portable Node (vendor\node) REQUIRED - no system Node fallback
# ---------------------------------------------------------------------------
Write-Step "Locate portable Node (vendor\node) - required"

$nodeInfo = Resolve-BundledNode $RepoRoot
if (-not $nodeInfo) {
    throw @"
Portable Node is required for install-opencode-online.bat.

Expected:
  $RepoRoot\vendor\node\node.exe
  $RepoRoot\vendor\node\npm.cmd

This zip must be a CI-built package (build-dist stages Node under vendor\node).
Offline install-backends.bat does not need Node - use that for fully offline installs.
"@
}

Write-Host "  Using bundled Node: $($nodeInfo.NodeExe)"
# Put OUR node first so npm.cmd never picks a different node.exe
$env:Path = $nodeInfo.NodeDir + ";" + $env:Path
$nodeVer = & $nodeInfo.NodeExe --version 2>&1
Write-Host "  node $nodeVer"
$npmCmd = $nodeInfo.NpmCmd
$npmVer = & $npmCmd --version 2>&1
Write-Host "  npm  $npmVer (from vendor\node)"

# ---------------------------------------------------------------------------
# 2) Clean previous OpenCode install (idempotent re-run)
# ---------------------------------------------------------------------------
Write-Step "Clean previous OpenCode roots"

foreach ($legacyBin in @(
        (Join-Path $LEGACY1 "bin"),
        (Join-Path $LEGACY2 "bin"),
        (Join-Path $USER_OC "bin"),
        $OPENCODE_BIN
    )) {
    Remove-UserPathEntry $legacyBin
}

Force-RemoveDir $USER_OC
if ($OPENCODE_HOME -ne $USER_OC) { Force-RemoveDir $OPENCODE_HOME }
if ($LEGACY1 -ne $USER_OC -and $LEGACY1 -ne $OPENCODE_HOME) { Force-RemoveDir $LEGACY1 }
if ($LEGACY2 -ne $USER_OC -and $LEGACY2 -ne $OPENCODE_HOME) { Force-RemoveDir $LEGACY2 }

# Stale configs / cache (plugin trees re-seeded below)
if (Test-Path -LiteralPath (Join-Path $OC_CONFIG "opencode.json")) {
    Remove-Item -LiteralPath (Join-Path $OC_CONFIG "opencode.json") -Force -ErrorAction SilentlyContinue
}
if (Test-Path -LiteralPath (Join-Path $OC_CONFIG "node_modules")) {
    Force-RemoveDir (Join-Path $OC_CONFIG "node_modules")
}
Force-RemoveDir (Join-Path $OC_CACHE "node_modules")
Force-RemoveDir (Join-Path $OC_CACHE "packages")

Ensure-Dir $OPENCODE_HOME
Ensure-Dir $OPENCODE_BIN
Ensure-Dir $OC_CONFIG
Ensure-Dir $OC_CACHE

# ---------------------------------------------------------------------------
# 3) Download OpenCode CLI (public release or your mirror URL)
# ---------------------------------------------------------------------------
Write-Step "Download OpenCode v$OPENCODE_VERSION"

$tmp = Join-Path $env:TEMP ("vd-oc-online-" + [guid]::NewGuid().ToString("n"))
Ensure-Dir $tmp
$ocZip = Join-Path $tmp "opencode-windows-x64.zip"
$ocUrl = if ($OPENCODE_ZIP_URL) {
    $OPENCODE_ZIP_URL
} else {
    "https://github.com/anomalyco/opencode/releases/download/v$OPENCODE_VERSION/opencode-windows-x64.zip"
}
if ($OPENCODE_ZIP_URL) { Write-Host "  Using OPENCODE_ZIP_URL override" }
Download-File $ocUrl $ocZip
$ocExtract = Join-Path $tmp "opencode-extract"
Expand-ZipSafe $ocZip $ocExtract
$ocExe = Get-ChildItem -Path $ocExtract -Filter "opencode.exe" -Recurse | Select-Object -First 1
if (-not $ocExe) { throw "opencode.exe not found in release zip" }
Copy-Item -LiteralPath $ocExe.FullName -Destination (Join-Path $OPENCODE_BIN "opencode.exe") -Force
Unblock-File -LiteralPath (Join-Path $OPENCODE_BIN "opencode.exe") -ErrorAction SilentlyContinue
Write-Host ("  opencode.exe {0:N1} MB" -f ($ocExe.Length / 1MB))

$installedOc = Join-Path $OPENCODE_BIN "opencode.exe"
if (-not (Test-Path -LiteralPath $installedOc)) {
    throw "opencode.exe missing after download: $installedOc"
}
$ocVerOut = & $installedOc --version 2>&1
Write-Host "  opencode --version => $ocVerOut"

# ---------------------------------------------------------------------------
# 4) glab (optional but used for MRs)
# ---------------------------------------------------------------------------
Write-Step "Download glab v$GLAB_VERSION (optional)"
try {
    $glabZip = Join-Path $tmp "glab.zip"
    $glabUrl = if ($GLAB_ZIP_URL) {
        $GLAB_ZIP_URL
    } else {
        "https://gitlab.com/api/v4/projects/gitlab-org%2Fcli/packages/generic/glab/$GLAB_VERSION/glab_${GLAB_VERSION}_windows_amd64.zip"
    }
    if ($GLAB_ZIP_URL) { Write-Host "  Using GLAB_ZIP_URL override" }
    Download-File $glabUrl $glabZip
    $glabExtract = Join-Path $tmp "glab-extract"
    Expand-ZipSafe $glabZip $glabExtract
    $glabExe = Get-ChildItem -Path $glabExtract -Filter "glab.exe" -Recurse | Select-Object -First 1
    if ($glabExe) {
        Copy-Item -LiteralPath $glabExe.FullName -Destination (Join-Path $OPENCODE_BIN "glab.exe") -Force
        Write-Host "  glab.exe installed"
    }
} catch {
    Write-Host "  [WARNING] glab download skipped: $($_.Exception.Message)"
}

# ---------------------------------------------------------------------------
# 5) Config + package.json (pinned openagent id)
# ---------------------------------------------------------------------------
Write-Step "Write OpenCode configs (stock build/plan, no oh-my plugin)"

$pkgBody = @"
{
  "name": "virtual-developer-opencode-home",
  "private": true,
  "description": "OpenCode user home for Yaver (stock build/plan agents, no oh-my plugin)"
}
"@
Set-Content -Path (Join-Path $OPENCODE_HOME "package.json") -Value $pkgBody -Encoding UTF8

$ocCfgBody = @"
{
  "`$schema": "https://opencode.ai/config.json",
  "autoupdate": false,
  "plugin": []
}
"@
Set-Content -Path (Join-Path $OPENCODE_HOME "opencode.json") -Value $ocCfgBody -Encoding UTF8

$pinPs1 = Join-Path $RepoRoot "packaging\windows\Pin-OpencodePlugin.ps1"
if (Test-Path -LiteralPath $pinPs1) {
    & $pinPs1 -ConfigPath (Join-Path $OPENCODE_HOME "opencode.json")
}

# ---------------------------------------------------------------------------
# 6) Mirror stock config (no oh-my-openagent npm install)
# ---------------------------------------------------------------------------
Write-Step "Mirror stock OpenCode config"

foreach ($name in @("opencode.json", "package.json")) {
    $src = Join-Path $OPENCODE_HOME $name
    if (Test-Path -LiteralPath $src) {
        Copy-Item -LiteralPath $src -Destination (Join-Path $OC_CONFIG $name) -Force
    }
}
Write-Host "  stock OpenCode config mirrored (plugin=[])"

# ---------------------------------------------------------------------------
# 8) ripgrep seed (avoid first-run download hang)
# ---------------------------------------------------------------------------
Write-Step "Seed ripgrep (rg.exe)"

$rgSrc = $null
$vendorRg = Join-Path $RepoRoot "vendor\bin\rg.exe"
$binRg = Join-Path $OPENCODE_BIN "rg.exe"
if (Test-Path -LiteralPath $vendorRg) { $rgSrc = $vendorRg }
elseif (Test-Path -LiteralPath $binRg) { $rgSrc = $binRg }

if (-not $rgSrc) {
    try {
        $rgVer = "15.1.0"
        $rgZip = Join-Path $tmp "rg.zip"
        $rgUrl = if ($RG_ZIP_URL) {
            $RG_ZIP_URL
        } else {
            "https://github.com/BurntSushi/ripgrep/releases/download/$rgVer/ripgrep-$rgVer-x86_64-pc-windows-msvc.zip"
        }
        if ($RG_ZIP_URL) { Write-Host "  Using RG_ZIP_URL override" }
        Download-File $rgUrl $rgZip
        $rgExtract = Join-Path $tmp "rg-extract"
        Expand-ZipSafe $rgZip $rgExtract
        $rgExe = Get-ChildItem -Path $rgExtract -Filter "rg.exe" -Recurse | Select-Object -First 1
        if ($rgExe) { $rgSrc = $rgExe.FullName }
    } catch {
        Write-Host "  [WARNING] could not download rg.exe: $($_.Exception.Message)"
    }
}

if ($rgSrc) {
    $rgCacheBin = Join-Path $OC_CACHE "bin"
    Ensure-Dir $rgCacheBin
    Copy-Item -LiteralPath $rgSrc -Destination (Join-Path $rgCacheBin "rg.exe") -Force
    Copy-Item -LiteralPath $rgSrc -Destination (Join-Path $OPENCODE_BIN "rg.exe") -Force
    Write-Host "  rg.exe -> $rgCacheBin"
} else {
    Write-Host "  [WARNING] rg.exe not seeded; first TUI run may download it"
}

# ---------------------------------------------------------------------------
# 9) PATH + env
# ---------------------------------------------------------------------------
Write-Step "User PATH and env"

Prepend-UserPath $OPENCODE_BIN
try {
    [Environment]::SetEnvironmentVariable("OPENCODE_DISABLE_MODELS_FETCH", "1", "User")
    $env:OPENCODE_DISABLE_MODELS_FETCH = "1"
    Write-Host "  OPENCODE_DISABLE_MODELS_FETCH=1"
} catch {
    Write-Host "  [WARNING] could not set OPENCODE_DISABLE_MODELS_FETCH"
}

# Junction only when VD_OPENCODE_ROOT points elsewhere
if ($OPENCODE_HOME -ne $USER_OC) {
    if (-not (Test-Path -LiteralPath $USER_OC)) {
        cmd /c "mklink /J `"$USER_OC`" `"$OPENCODE_HOME`"" | Out-Null
        Write-Host "  Junction: $USER_OC => $OPENCODE_HOME"
    }
}

# Cleanup temp
if (Test-Path -LiteralPath $tmp) {
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "========================================"
Write-Host "  Online OpenCode install complete"
Write-Host "========================================"
Write-Host "  bin     : $OPENCODE_BIN\opencode.exe"
Write-Host "  plugin  : none (stock build/plan)"
Write-Host "  config  : $OC_CONFIG\opencode.json"
Write-Host "  cache   : $OC_CACHE"
Write-Host ""
Write-Host "  Next: open a NEW terminal, then run start-opencode.bat"
Write-Host "        from the project folder (not your user home)."
Write-Host ""
exit 0
