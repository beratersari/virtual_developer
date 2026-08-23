#Requires -Version 5.1
<#
.SYNOPSIS
  Build the Windows offline distribution zip for JIRA Virtual Developer.

.DESCRIPTION
  Fetches pinned OpenCode, Codex, glab, oh-my-opencode, and Python wheels (3.10+) from
  the web, stages the app, packs OpenCode home into a SINGLE archive (avoids
  Windows MAX_PATH / slow node_modules extract for end users), and writes a zip.

  Intended to run on windows-latest in GitHub Actions (or a local Windows box).
#>
[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [string]$OutDir = "",
    [string]$DistName = "virtual_developer-windows-x64"
)

$ErrorActionPreference = "Stop"

function Get-RepoRoot {
    if ($RepoRoot) { return (Resolve-Path $RepoRoot).Path }
    return (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
}

function Read-Versions([string]$Path) {
    $map = @{}
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

function Ensure-Dir([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
    }
}

function Download-File([string]$Url, [string]$OutFile) {
    Write-Host "  Downloading $Url"
    Write-Host "           -> $OutFile"
    Invoke-WebRequest -Uri $Url -OutFile $OutFile -UseBasicParsing
    if (-not (Test-Path -LiteralPath $OutFile)) {
        throw "Download failed: $Url"
    }
    $size = (Get-Item -LiteralPath $OutFile).Length
    Write-Host ("  OK ({0:N1} MB)" -f ($size / 1MB))
}

function Expand-ZipSafe([string]$ZipPath, [string]$Dest) {
    Ensure-Dir $Dest
    # tar handles long paths better than Expand-Archive on modern Windows
    $tar = Get-Command tar -ErrorAction SilentlyContinue
    if ($tar) {
        & tar -xf $ZipPath -C $Dest
        if ($LASTEXITCODE -ne 0) {
            Expand-Archive -Path $ZipPath -DestinationPath $Dest -Force
        }
    } else {
        Expand-Archive -Path $ZipPath -DestinationPath $Dest -Force
    }
}

function Optimize-NodeModules([string]$NodeModules) {
    # IMPORTANT: oh-my-opencode ships agents/skills as ***.md** and nested package trees.
    # Aggressive pruning (delete all *.md / test/docs dirs) strips the plugin and OpenCode
    # silently falls back to default build/plan agents after a multi-minute Bun hang.
    # Only drop heavyweight non-runtime junk that cannot affect plugin load.
    if (-not (Test-Path -LiteralPath $NodeModules)) { return }
    Write-Host "  Light prune of node_modules (source maps only — keep all plugin .md/skills)..."

    Get-ChildItem -Path $NodeModules -Recurse -Force -File -Filter "*.map" -ErrorAction SilentlyContinue |
        ForEach-Object {
            Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue
        }
}

function New-ZipFromDirectory([string]$SourceDir, [string]$ZipPath) {
    if (Test-Path -LiteralPath $ZipPath) {
        Remove-Item -LiteralPath $ZipPath -Force
    }
    # Prefer tar: faster and more reliable with deep trees / long paths on Windows
    $tar = Get-Command tar -ErrorAction SilentlyContinue
    if ($tar) {
        Push-Location $SourceDir
        try {
            # -a: compress format from extension (.zip); paths relative to SourceDir
            & tar -a -cf $ZipPath *
            if ($LASTEXITCODE -ne 0) {
                throw "tar failed creating $ZipPath (exit $LASTEXITCODE)"
            }
        } finally {
            Pop-Location
        }
    } else {
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [System.IO.Compression.ZipFile]::CreateFromDirectory(
            $SourceDir,
            $ZipPath,
            [System.IO.Compression.CompressionLevel]::Optimal,
            $false
        )
    }
    if (-not (Test-Path -LiteralPath $ZipPath)) {
        throw "Failed to create archive: $ZipPath"
    }
}

function Download-WheelsForPythonVersions([string]$Requirements, [string]$WheelsDir, [string[]]$PyVersions) {
    Ensure-Dir $WheelsDir

    # 1) Host interpreter: pure + binary wheels for current Python
    Write-Host "  pip download (host Python, prefer-binary)..."
    python -m pip download -r $Requirements -d $WheelsDir --prefer-binary
    if ($LASTEXITCODE -ne 0) {
        throw "pip download (host) failed"
    }

    # 2) Cross-download win_amd64 wheels for each CPython minor (3.10, 3.11, ...)
    foreach ($pv in $PyVersions) {
        $tag = ($pv -replace "\.", "")
        Write-Host "  pip download win_amd64 cp$tag (Python $pv)..."
        python -m pip download `
            -r $Requirements `
            -d $WheelsDir `
            --python-version $pv `
            --platform win_amd64 `
            --implementation cp `
            --abi "cp$tag" `
            --only-binary=:all: `
            --prefer-binary
        if ($LASTEXITCODE -ne 0) {
            Write-Host "  WARNING: incomplete wheel set for Python $pv (some packages may lack cp$tag wheels)"
        }

        # Critical native dep used by pydantic (often fails first if missing)
        python -m pip download "pydantic-core" -d $WheelsDir `
            --python-version $pv `
            --platform win_amd64 `
            --implementation cp `
            --abi "cp$tag" `
            --only-binary=:all: 2>$null | Out-Null
    }

    # Bootstrap helpers (py3-none-any or version-specific)
    Write-Host "  pip download pip/setuptools/wheel helpers..."
    python -m pip download pip setuptools wheel -d $WheelsDir --prefer-binary
    foreach ($pv in $PyVersions) {
        $tag = ($pv -replace "\.", "")
        python -m pip download pip setuptools wheel -d $WheelsDir `
            --python-version $pv `
            --platform win_amd64 `
            --implementation cp `
            --abi "cp$tag" `
            --only-binary=:all: `
            --prefer-binary 2>$null | Out-Null
    }
}

function Get-SupportedPythonVersions([string]$WheelsDir, [string[]]$Candidates) {
    # A version is supported only if pydantic-core has a matching cpXXX win wheel
    # (pure py3-none-any packages work everywhere; binary deps are the gate).
    $supported = New-Object System.Collections.Generic.List[string]
    foreach ($pv in $Candidates) {
        $tag = ($pv -replace "\.", "")
        $hits = @(Get-ChildItem -Path $WheelsDir -File -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match "pydantic_core-.*-cp$tag-.*" -or $_.Name -match "pydantic_core-.*-cp$tag m-.*" })
        # Also match tags like cp314-cp314-win_amd64
        if (-not $hits -or $hits.Count -eq 0) {
            $hits = @(Get-ChildItem -Path $WheelsDir -File -Filter "*pydantic_core*cp$tag*" -ErrorAction SilentlyContinue)
        }
        if ($hits -and $hits.Count -gt 0) {
            [void]$supported.Add($pv)
            Write-Host "  supported Python $pv (found $($hits[0].Name))"
        } else {
            Write-Host "  UNSUPPORTED Python $pv (no pydantic-core cp$tag wheel in vendor)"
        }
    }
    return ,$supported.ToArray()
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

# Help CI / local Windows with deep npm trees
try {
    git config --global core.longpaths true 2>$null
} catch { }

$root = Get-RepoRoot
$versionsFile = Join-Path $root "packaging\windows\versions.env"
if (-not (Test-Path -LiteralPath $versionsFile)) {
    throw "versions.env not found: $versionsFile"
}
$ver = Read-Versions $versionsFile

$OPENCODE_VERSION = $ver["OPENCODE_VERSION"]
$OH_MY_OPENCODE_VERSION = $ver["OH_MY_OPENCODE_VERSION"]
$GLAB_VERSION = $ver["GLAB_VERSION"]
$CODEX_VERSION = $ver["CODEX_VERSION"]
$CODEX_WINDOWS_ASSET = if ($ver["CODEX_WINDOWS_ASSET"]) {
    $ver["CODEX_WINDOWS_ASSET"]
} else {
    "codex-x86_64-pc-windows-msvc.exe.zip"
}
$NODE_FULL_VERSION = if ($ver["NODE_FULL_VERSION"]) { $ver["NODE_FULL_VERSION"] } else { "20.19.0" }
$PYTHON_MIN_VERSION = if ($ver["PYTHON_MIN_VERSION"]) { $ver["PYTHON_MIN_VERSION"] } else { "3.10" }
$wheelVersionList = if ($ver["PYTHON_WHEEL_VERSIONS"]) {
    @($ver["PYTHON_WHEEL_VERSIONS"] -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ })
} else {
    @("3.10", "3.11", "3.12", "3.13")
}

if (-not $OPENCODE_VERSION) { throw "OPENCODE_VERSION missing in versions.env" }
if (-not $OH_MY_OPENCODE_VERSION) { throw "OH_MY_OPENCODE_VERSION missing in versions.env" }
if (-not $GLAB_VERSION) { throw "GLAB_VERSION missing in versions.env" }
if (-not $CODEX_VERSION) { throw "CODEX_VERSION missing in versions.env" }
if (-not $NODE_FULL_VERSION) { throw "NODE_FULL_VERSION missing in versions.env" }

if (-not $OutDir) {
    $OutDir = Join-Path $root "dist"
}
Ensure-Dir $OutDir

$stage = Join-Path $OutDir "stage"
$payload = Join-Path $stage $DistName
if (Test-Path -LiteralPath $stage) {
    Remove-Item -LiteralPath $stage -Recurse -Force
}
Ensure-Dir $payload

Write-Host "========================================"
Write-Host "  Building Windows distribution"
Write-Host "========================================"
Write-Host "Repo root : $root"
Write-Host "Payload   : $payload"
Write-Host "OpenCode  : $OPENCODE_VERSION"
Write-Host "oh-my-oc  : $OH_MY_OPENCODE_VERSION"
Write-Host "glab      : $GLAB_VERSION"
Write-Host "Codex     : $CODEX_VERSION"
Write-Host "Wheels for: $($wheelVersionList -join ', ') (min runtime $PYTHON_MIN_VERSION)"
Write-Host ""

# ---------------------------------------------------------------------------
# 1) Copy application sources into the payload
# ---------------------------------------------------------------------------
Write-Host "Step 1: Staging application files..."

$copyItems = @(
    "cli.py",
    "requirements.txt",
    ".env.example",
    "install-dashboard.bat",
    "install-dashboard-system-python.bat",
    "install-opencode-online.bat",
    "install-backends.bat",
    "install-codex.bat",
    "VERSION",
    "README.md",
    "AGENTS.md",
    "Agents.md",
    "commitMsgFormat.md",
    "pytest.ini",
    "src",
    "agent",
    "sample_project",
    "packaging"
)

foreach ($item in $copyItems) {
    $src = Join-Path $root $item
    if (-not (Test-Path -LiteralPath $src)) {
        Write-Host "  skip missing: $item"
        continue
    }
    $dest = Join-Path $payload $item
    if ((Get-Item -LiteralPath $src).PSIsContainer) {
        Ensure-Dir (Split-Path $dest -Parent)
        Copy-Item -LiteralPath $src -Destination $dest -Recurse -Force
    } else {
        Ensure-Dir (Split-Path $dest -Parent)
        Copy-Item -LiteralPath $src -Destination $dest -Force
    }
    Write-Host "  + $item"
}

# Root launchers (backend / frontend / both)
foreach ($launcher in @(
        "start.bat",
        "start-backend.bat",
        "start-frontend.bat",
        "start-opencode-serve.bat"
    )) {
    $srcLauncher = Join-Path $root "packaging\windows\$launcher"
    if (-not (Test-Path -LiteralPath $srcLauncher)) {
        throw "packaging\windows\$launcher missing"
    }
    Copy-Item -LiteralPath $srcLauncher -Destination (Join-Path $payload $launcher) -Force
    Write-Host "  + $launcher"
}
foreach ($helper in @("Wait-Http.ps1", "Stop-VdProcesses.ps1", "Ensure-OpencodeServe.ps1", "serve_frontend.py")) {
    $hp = Join-Path $root "packaging\windows\$helper"
    if (-not (Test-Path -LiteralPath $hp)) {
        throw "packaging\windows\$helper missing"
    }
}

# ---------------------------------------------------------------------------
# 1b) Build ops dashboard SPA and stage web/dist only (no node_modules)
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "Step 1b: Building ops dashboard frontend (web/)..."

$webDir = Join-Path $root "web"
if (-not (Test-Path -LiteralPath (Join-Path $webDir "package.json"))) {
    throw "web/package.json missing — cannot build dashboard SPA"
}

$node = Get-Command node -ErrorAction SilentlyContinue
$npm = Get-Command npm -ErrorAction SilentlyContinue
if (-not $node -or -not $npm) {
    throw "Node.js + npm required on PATH to build web/ (CI: setup-node)"
}
Write-Host "  node: $(& node --version 2>$null)"
Write-Host "  npm : $(& npm --version 2>$null)"

Push-Location $webDir
try {
    if (Test-Path -LiteralPath (Join-Path $webDir "package-lock.json")) {
        Write-Host "  npm ci..."
        npm ci --no-fund --no-audit
        if ($LASTEXITCODE -ne 0) {
            Write-Host "  npm ci failed — retrying with npm install..."
            npm install --no-fund --no-audit
            if ($LASTEXITCODE -ne 0) { throw "npm install failed for web/ (exit $LASTEXITCODE)" }
        }
    } else {
        Write-Host "  npm install (no package-lock)..."
        npm install --no-fund --no-audit
        if ($LASTEXITCODE -ne 0) { throw "npm install failed for web/ (exit $LASTEXITCODE)" }
    }

    Write-Host "  npm run build..."
    npm run build
    if ($LASTEXITCODE -ne 0) {
        throw "npm run build failed for web/ (exit $LASTEXITCODE)"
    }
} finally {
    Pop-Location
}

$webDist = Join-Path $webDir "dist"
$webIndex = Join-Path $webDist "index.html"
if (-not (Test-Path -LiteralPath $webIndex)) {
    throw "web/dist/index.html missing after build"
}
$webAssets = Join-Path $webDist "assets"
if (-not (Test-Path -LiteralPath $webAssets)) {
    throw "web/dist/assets missing after build"
}

$payloadWeb = Join-Path $payload "web"
Ensure-Dir $payloadWeb
# Only ship production assets — never web/node_modules (path-length bomb)
$payloadWebDist = Join-Path $payloadWeb "dist"
if (Test-Path -LiteralPath $payloadWebDist) {
    Remove-Item -LiteralPath $payloadWebDist -Recurse -Force
}
Ensure-Dir $payloadWebDist
# Copy *contents* so we never nest web/dist/dist on PowerShell
Copy-Item -Path (Join-Path $webDist "*") -Destination $payloadWebDist -Recurse -Force
Copy-Item -LiteralPath (Join-Path $webDir "package.json") -Destination (Join-Path $payloadWeb "package.json") -Force
$builtJs = @(Get-ChildItem -LiteralPath (Join-Path $webDist "assets") -Filter "index-*.js" -File)
$builtCss = @(Get-ChildItem -LiteralPath (Join-Path $webDist "assets") -Filter "index-*.css" -File)
if ($builtJs.Count -lt 1 -or $builtCss.Count -lt 1) {
    throw "npm run build did not produce hashed index JS/CSS under web/dist/assets"
}
Write-Host "  SPA JS : $($builtJs[0].Name)"
Write-Host "  SPA CSS: $($builtCss[0].Name)"
# Marker so install/start can prove SPA was packaged
$spaMarker = @"
virtual_developer ops dashboard SPA (production build)
Built: $(Get-Date -Format "yyyy-MM-ddTHH:mm:ssK")
Served by the daemon at http://127.0.0.1:8080 (FastAPI StaticFiles)
No Node required at runtime on the user machine.
"@
Set-Content -Path (Join-Path $payloadWeb "DIST_SPA.txt") -Value $spaMarker -Encoding UTF8
$spaFiles = @(Get-ChildItem -LiteralPath $payloadWebDist -Recurse -File -ErrorAction SilentlyContinue)
Write-Host "  + web/dist ($($spaFiles.Count) files) — dashboard SPA for offline install"

# Guard: never ship web/node_modules in the payload
if (Test-Path -LiteralPath (Join-Path $payloadWeb "node_modules")) {
    throw "FAIL: web/node_modules must not be staged in the offline zip"
}

$productVersion = if ($env:VD_PRODUCT_VERSION) { $env:VD_PRODUCT_VERSION } else {
    $vf = Join-Path $root "VERSION"
    if (Test-Path -LiteralPath $vf) { (Get-Content -LiteralPath $vf -Raw).Trim() } else { "0.0.0-dev" }
}
$marker = @"
virtual_developer Windows offline distribution
ProductVersion=$productVersion
DistName=$DistName
Built: $(Get-Date -Format "yyyy-MM-ddTHH:mm:ssK")
OpenCode=$OPENCODE_VERSION
oh-my-opencode=$OH_MY_OPENCODE_VERSION
oh-my-openagent=$OH_MY_OPENCODE_VERSION
glab=$GLAB_VERSION
Codex=$CODEX_VERSION
CodexAsset=$CODEX_WINDOWS_ASSET
PythonMin=$PYTHON_MIN_VERSION
PythonWheels=$($wheelVersionList -join ',')
OpenCodeHome=vendor/opencode-home.zip (single archive — extract via install-backends.bat)
PortableNode=vendor/node (node.exe + npm for install-opencode-online.bat)
NodeFull=$NODE_FULL_VERSION
"@
Set-Content -Path (Join-Path $payload "DIST_VERSION.txt") -Value $marker -Encoding UTF8
Set-Content -Path (Join-Path $payload "VERSION") -Value $productVersion -Encoding UTF8

# ---------------------------------------------------------------------------
# 2) Fetch OpenCode Windows CLI (pinned)
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "Step 2: Fetching OpenCode CLI v$OPENCODE_VERSION..."

$vendor = Join-Path $payload "vendor"
$dl = Join-Path $vendor "_downloads"
Ensure-Dir $dl

# Official 64-bit Windows CLI (AMD64). Do NOT use arm64 or any 32-bit asset.
$OPENCODE_ASSET = "opencode-windows-x64.zip"
$opencodeZip = Join-Path $dl $OPENCODE_ASSET
$opencodeUrl = "https://github.com/anomalyco/opencode/releases/download/v$OPENCODE_VERSION/$OPENCODE_ASSET"
Write-Host "  Asset: $OPENCODE_ASSET (AMD64 / 64-bit Windows)"
Download-File $opencodeUrl $opencodeZip

$opencodeExtract = Join-Path $dl "opencode-extract"
if (Test-Path -LiteralPath $opencodeExtract) {
    Remove-Item -LiteralPath $opencodeExtract -Recurse -Force
}
Expand-ZipSafe $opencodeZip $opencodeExtract

$opencodeExe = Get-ChildItem -Path $opencodeExtract -Filter "opencode.exe" -Recurse | Select-Object -First 1
if (-not $opencodeExe) {
    throw "opencode.exe not found inside $opencodeZip"
}

# Fail the build if GitHub ever ships a non-x64 binary under this name
$assertPe = Join-Path $root "packaging\windows\Assert-Amd64Pe.ps1"
& $assertPe -Path $opencodeExe.FullName
if ($LASTEXITCODE -ne 0) {
    throw "OpenCode binary is not AMD64 — refusing to package a broken/wrong-arch build"
}
$ocSha = (Get-FileHash -Algorithm SHA256 -LiteralPath $opencodeExe.FullName).Hash
Write-Host "  OpenCode SHA256: $ocSha"

# ---------------------------------------------------------------------------
# 3) Fetch glab Windows CLI (pinned)
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "Step 3: Fetching glab v$GLAB_VERSION..."

$glabZip = Join-Path $dl "glab_windows_amd64.zip"
$glabUrl = "https://gitlab.com/api/v4/projects/gitlab-org%2Fcli/packages/generic/glab/$GLAB_VERSION/glab_${GLAB_VERSION}_windows_amd64.zip"
Download-File $glabUrl $glabZip

$glabExtract = Join-Path $dl "glab-extract"
if (Test-Path -LiteralPath $glabExtract) {
    Remove-Item -LiteralPath $glabExtract -Recurse -Force
}
Expand-ZipSafe $glabZip $glabExtract

$glabExe = Get-ChildItem -Path $glabExtract -Filter "glab.exe" -Recurse | Select-Object -First 1
if (-not $glabExe) {
    throw "glab.exe not found inside $glabZip"
}
& $assertPe -Path $glabExe.FullName -MinBytes 1MB
if ($LASTEXITCODE -ne 0) {
    throw "glab.exe is not AMD64"
}

# ---------------------------------------------------------------------------
# 3b) Fetch Codex Windows CLI (pinned rust-vX.Y.Z)
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "Step 3b: Fetching Codex CLI v$CODEX_VERSION..."

$codexZip = Join-Path $dl $CODEX_WINDOWS_ASSET
$codexUrl = "https://github.com/openai/codex/releases/download/rust-v$CODEX_VERSION/$CODEX_WINDOWS_ASSET"
Write-Host "  Asset: $CODEX_WINDOWS_ASSET (AMD64 / 64-bit Windows)"
Download-File $codexUrl $codexZip

$codexExtract = Join-Path $dl "codex-extract"
if (Test-Path -LiteralPath $codexExtract) {
    Remove-Item -LiteralPath $codexExtract -Recurse -Force
}
Expand-ZipSafe $codexZip $codexExtract

$codexExe = Get-ChildItem -Path $codexExtract -Filter "codex*.exe" -Recurse -File |
    Select-Object -First 1
if (-not $codexExe) {
    throw "codex.exe not found inside $codexZip"
}
& $assertPe -Path $codexExe.FullName -MinBytes 5MB
if ($LASTEXITCODE -ne 0) {
    throw "codex.exe is not AMD64"
}
$codexSha = (Get-FileHash -Algorithm SHA256 -LiteralPath $codexExe.FullName).Hash
Write-Host ("  Codex SHA256: {0} ({1:N1} MB)" -f $codexSha, ($codexExe.Length / 1MB))

# ---------------------------------------------------------------------------
# 4) Build OpenCode home in a SHORT temp path, then pack as ONE zip
#    (Users never extract thousands of node_modules files from the outer zip.)
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "Step 4: Building .opencode home template (short path + single archive)..."

# Short path reduces MAX_PATH pain while npm installs deep trees
$ocBuildRoot = Join-Path $env:TEMP "vd-oc-home"
if (Test-Path -LiteralPath $ocBuildRoot) {
    Remove-Item -LiteralPath $ocBuildRoot -Recurse -Force
}
$ocHome = $ocBuildRoot
$ocBin = Join-Path $ocHome "bin"
Ensure-Dir $ocBin

Copy-Item -LiteralPath $opencodeExe.FullName -Destination (Join-Path $ocBin "opencode.exe") -Force
Copy-Item -LiteralPath $glabExe.FullName -Destination (Join-Path $ocBin "glab.exe") -Force
# Flat backup copies OUTSIDE the nested zip — short path, easy re-copy if AV
# quarantines the installed binary (classic 21-byte stub → "not compatible with 64-bit Windows").
Ensure-Dir $vendor
$vendorBin = Join-Path $vendor "bin"
Ensure-Dir $vendorBin
Copy-Item -LiteralPath $opencodeExe.FullName -Destination (Join-Path $vendorBin "opencode.exe") -Force
Copy-Item -LiteralPath $glabExe.FullName -Destination (Join-Path $vendorBin "glab.exe") -Force
Copy-Item -LiteralPath $codexExe.FullName -Destination (Join-Path $vendorBin "codex.exe") -Force
& $assertPe -Path (Join-Path $vendorBin "opencode.exe")
if ($LASTEXITCODE -ne 0) { throw "vendor\bin\opencode.exe failed AMD64 check" }
& $assertPe -Path (Join-Path $vendorBin "codex.exe") -MinBytes 5MB
if ($LASTEXITCODE -ne 0) { throw "vendor\bin\codex.exe failed AMD64 check" }

# Record architecture for install-time checks / support
@(
    "OPENCODE_ARCH=win_amd64"
    "OPENCODE_ASSET=$OPENCODE_ASSET"
    "OPENCODE_SHA256=$ocSha"
    "OPENCODE_BYTES=$((Get-Item -LiteralPath $opencodeExe.FullName).Length)"
    "CODEX_VERSION=$CODEX_VERSION"
    "CODEX_ASSET=$CODEX_WINDOWS_ASSET"
    "CODEX_SHA256=$codexSha"
    "CODEX_BYTES=$((Get-Item -LiteralPath $codexExe.FullName).Length)"
    "TARGET_OS=Windows 10/11 64-bit (x64 / AMD64)"
    "BACKUP=vendor\bin\opencode.exe"
    "CODEX_INSTALL=%LOCALAPPDATA%\Programs\OpenAI\Codex\bin\codex.exe"
    "BACKUP_CODEX=vendor\bin\codex.exe"
) | Set-Content -Path (Join-Path $ocBin "ARCH.txt") -Encoding UTF8
Copy-Item -LiteralPath (Join-Path $ocBin "ARCH.txt") -Destination (Join-Path $vendorBin "ARCH.txt") -Force

$pkgPath = Join-Path $ocHome "package.json"
$pkgBody = @"
{
  "name": "virtual-developer-opencode-home",
  "private": true,
  "description": "OpenCode user home for Yaver (stock build/plan agents, no oh-my plugin)"
}
"@
Set-Content -Path $pkgPath -Value $pkgBody -Encoding UTF8

# Stock OpenCode agents only. Empty plugin list avoids Bun fetching oh-my-openagent.
$ocCfgBody = @"
{
  "`$schema": "https://opencode.ai/config.json",
  "autoupdate": false,
  "plugin": []
}
"@
Set-Content -Path (Join-Path $ocHome "opencode.json") -Value $ocCfgBody -Encoding UTF8
$pkgWindows = Join-Path $payload "packaging\windows"
if (Test-Path -LiteralPath $pkgWindows) {
    Set-Content -Path (Join-Path $pkgWindows "opencode.json") -Value $ocCfgBody -Encoding UTF8
}

Write-Host "  Stock OpenCode config (plugin=[], default build agent). No oh-my-openagent."

# Bundle ripgrep so first TUI run does not hang downloading from GitHub
# OpenCode looks for: %USERPROFILE%\.cache\opencode\bin\rg.exe
Write-Host "  Fetching ripgrep for offline OpenCode tools..."
$rgVer = "15.1.0"
$rgZip = Join-Path $dl "ripgrep-$rgVer-x86_64-pc-windows-msvc.zip"
$rgUrl = "https://github.com/BurntSushi/ripgrep/releases/download/$rgVer/ripgrep-$rgVer-x86_64-pc-windows-msvc.zip"
Download-File $rgUrl $rgZip
$rgExtract = Join-Path $dl "rg-extract"
if (Test-Path -LiteralPath $rgExtract) { Remove-Item -LiteralPath $rgExtract -Recurse -Force }
Expand-ZipSafe $rgZip $rgExtract
$rgExe = Get-ChildItem -Path $rgExtract -Recurse -Filter "rg.exe" | Select-Object -First 1
if (-not $rgExe) { throw "rg.exe not found in ripgrep archive" }
# Into OpenCode home bin (also on PATH after install)
Copy-Item -LiteralPath $rgExe.FullName -Destination (Join-Path $ocBin "rg.exe") -Force
# Into vendor for install-backends.bat seed of ~/.cache/opencode/bin
Copy-Item -LiteralPath $rgExe.FullName -Destination (Join-Path $vendorBin "rg.exe") -Force
Write-Host ("  ripgrep: {0:N1} MB" -f ($rgExe.Length / 1MB))

# Hard fail if relative paths are still too long for classic Windows MAX_PATH budgets
$assertMax = Join-Path $root "packaging\windows\Assert-MaxPath.ps1"
& $assertMax -Root $ocHome -MaxRelativeChars 220
if ($LASTEXITCODE -ne 0) {
    throw "OpenCode home path-length budget exceeded — fix nesting before shipping"
}

# Single archive in vendor — outer dist must NEVER contain expanded node_modules
Ensure-Dir $vendor
$ocHomeZip = Join-Path $vendor "opencode-home.zip"
Write-Host "  Packing OpenCode home -> vendor\opencode-home.zip ..."
New-ZipFromDirectory -SourceDir $ocHome -ZipPath $ocHomeZip
$ocZipSize = (Get-Item -LiteralPath $ocHomeZip).Length
Write-Host ("  OpenCode home archive: {0:N1} MB" -f ($ocZipSize / 1MB))

# Prove the archive still contains a full AMD64 opencode.exe (not a 21-byte stub)
$verifyDir = Join-Path $env:TEMP ("vd-oc-verify-" + [guid]::NewGuid().ToString("n"))
Ensure-Dir $verifyDir
try {
    Expand-ZipSafe $ocHomeZip $verifyDir
    $verifyExe = Get-ChildItem -Path $verifyDir -Recurse -Filter "opencode.exe" | Select-Object -First 1
    if (-not $verifyExe) { throw "opencode-home.zip missing opencode.exe after pack" }
    & $assertPe -Path $verifyExe.FullName
    if ($LASTEXITCODE -ne 0) { throw "Packed opencode.exe failed AMD64/size check" }
    Write-Host ("  Verified packed opencode.exe ({0:N1} MB)" -f ($verifyExe.Length / 1MB))
    $packedCodex = Get-ChildItem -Path $verifyDir -Recurse -Filter "codex.exe" -ErrorAction SilentlyContinue
    if ($packedCodex) {
        throw "opencode-home.zip must not contain codex.exe (Codex installs to %LOCALAPPDATA%\Programs\OpenAI\Codex)"
    }
} finally {
    Remove-Item -LiteralPath $verifyDir -Recurse -Force -ErrorAction SilentlyContinue
}

# Do not ship expanded tree (prevents outer-zip path-length bombs)
Remove-Item -LiteralPath $ocBuildRoot -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "  vendor\opencode-home.zip ready (install-backends.bat extracts to %USERPROFILE%\.opencode)"

# ---------------------------------------------------------------------------
# 4b) Portable Node win-x64 (node.exe + npm) for install-opencode-online.bat
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "Step 4b: Staging portable Node v$NODE_FULL_VERSION (vendor\node)..."

$nodeZipName = "node-v$NODE_FULL_VERSION-win-x64.zip"
$nodeZip = Join-Path $dl $nodeZipName
$nodeUrl = "https://nodejs.org/dist/v$NODE_FULL_VERSION/$nodeZipName"
Download-File $nodeUrl $nodeZip

$nodeExtract = Join-Path $dl "node-extract"
if (Test-Path -LiteralPath $nodeExtract) {
    Remove-Item -LiteralPath $nodeExtract -Recurse -Force
}
Expand-ZipSafe $nodeZip $nodeExtract

# Official zip has a single top folder node-v*-win-x64\
$nodeInner = Get-ChildItem -Path $nodeExtract -Directory | Select-Object -First 1
if (-not $nodeInner) {
    throw "Unexpected Node zip layout (no top-level directory)"
}
$nodeExeSrc = Join-Path $nodeInner.FullName "node.exe"
$npmCmdSrc = Join-Path $nodeInner.FullName "npm.cmd"
if (-not (Test-Path -LiteralPath $nodeExeSrc)) {
    throw "node.exe missing in Node zip: $nodeExeSrc"
}
if (-not (Test-Path -LiteralPath $npmCmdSrc)) {
    throw "npm.cmd missing in Node zip: $npmCmdSrc"
}

$vendorNode = Join-Path $vendor "node"
if (Test-Path -LiteralPath $vendorNode) {
    Remove-Item -LiteralPath $vendorNode -Recurse -Force
}
Ensure-Dir $vendorNode
# Flatten into vendor\node so install-opencode-online finds vendor\node\node.exe
$rcNode = Start-Process -FilePath "robocopy.exe" -ArgumentList @(
    $nodeInner.FullName, $vendorNode, "/E", "/NFL", "/NDL", "/NJH", "/NJS", "/nc", "/ns", "/np", "/R:1", "/W:1"
) -Wait -PassThru -NoNewWindow
if ($rcNode.ExitCode -ge 8) {
    throw "robocopy Node tree failed (exit $($rcNode.ExitCode))"
}
if (-not (Test-Path -LiteralPath (Join-Path $vendorNode "node.exe"))) {
    throw "vendor\node\node.exe missing after stage"
}
if (-not (Test-Path -LiteralPath (Join-Path $vendorNode "npm.cmd"))) {
    throw "vendor\node\npm.cmd missing after stage"
}
# Sanity: node runs
$nodeSmoke = & (Join-Path $vendorNode "node.exe") --version 2>&1
Write-Host "  portable node: $nodeSmoke"
Write-Host ("  vendor\node staged ({0:N1} MB tree)" -f (
    (Get-ChildItem -Path $vendorNode -Recurse -File | Measure-Object -Property Length -Sum).Sum / 1MB
))

# Online-installer config templates (user-editable; offline install-backends.bat never uses these)
foreach ($cfgName in @("npm-online.npmrc", "online-sources.env")) {
    $cfgSrc = Join-Path $root "packaging\windows\$cfgName"
    if (Test-Path -LiteralPath $cfgSrc) {
        Copy-Item -LiteralPath $cfgSrc -Destination (Join-Path $vendor $cfgName) -Force
        Write-Host "  + vendor\$cfgName"
    }
}


# ---------------------------------------------------------------------------
# 5) Download Python wheels for 3.10+ (win_amd64)
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "Step 5: Downloading Python wheels for offline install (3.10+)..."

$wheels = Join-Path $vendor "python-wheels"
$req = Join-Path $root "requirements.txt"
Download-WheelsForPythonVersions -Requirements $req -WheelsDir $wheels -PyVersions $wheelVersionList

$wheelCount = (Get-ChildItem -Path $wheels -File -ErrorAction SilentlyContinue).Count
Write-Host "  Wheels staged: $wheelCount files"

Write-Host "  Detecting which Python versions have complete binary wheels..."
$supportedPy = Get-SupportedPythonVersions -WheelsDir $wheels -Candidates $wheelVersionList
if (-not $supportedPy -or $supportedPy.Count -eq 0) {
    throw "No supported Python versions found (pydantic-core wheels missing). Aborting."
}

$supportedFile = Join-Path $vendor "SUPPORTED_PYTHON.txt"
$supportedBody = @(
    "# CPython minor versions with offline wheels in this build (win_amd64).",
    "# install-dashboard.bat rejects any other version (e.g. too-new 3.x without wheels).",
    ""
) + $supportedPy
Set-Content -Path $supportedFile -Value ($supportedBody -join "`n") -Encoding UTF8
Write-Host "  SUPPORTED_PYTHON.txt: $($supportedPy -join ', ')"

# ---------------------------------------------------------------------------
# 6) Vendor metadata + drop download cache
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "Step 6: Writing vendor metadata..."

$versionsCopy = @"
OPENCODE_VERSION=$OPENCODE_VERSION
OH_MY_OPENCODE_VERSION=$OH_MY_OPENCODE_VERSION
GLAB_VERSION=$GLAB_VERSION
CODEX_VERSION=$CODEX_VERSION
NODE_FULL_VERSION=$NODE_FULL_VERSION
PYTHON_MIN_VERSION=$PYTHON_MIN_VERSION
PYTHON_WHEEL_VERSIONS=$($wheelVersionList -join ',')
SUPPORTED_PYTHON=$($supportedPy -join ',')
BUILT_AT=$(Get-Date -Format "yyyy-MM-ddTHH:mm:ssK")
NOTE=Run install-dashboard.bat (app + .venv), install-dashboard-system-python.bat (app, no venv), install-backends.bat (OpenCode + Codex), install-codex.bat (Codex only), or install-opencode-online.bat (OpenCode via network + vendor\node).
"@
Set-Content -Path (Join-Path $vendor "VERSIONS.txt") -Value $versionsCopy -Encoding UTF8
Copy-Item -LiteralPath $versionsFile -Destination (Join-Path $vendor "versions.env") -Force

# Clear one-screen install hint at payload root
$howTo = @"
JIRA Virtual Developer — Windows offline package
================================================
1. Extract the GitHub Actions download ONCE (you should see install-dashboard.bat here).
2. Do NOT manually unpack vendor\opencode-home.zip.
3. Install a supported Python (vendor\SUPPORTED_PYTHON.txt), e.g. 3.12 x64.
4. Install:
      install-dashboard.bat                 — Python + ops dashboard (.venv)
      install-dashboard-system-python.bat   — same, uses PATH python (no .venv)
      install-backends.bat                  — OpenCode + Codex (no Python)
      install-codex.bat                     — Codex CLI only
      install-opencode-online.bat — ONLINE OpenCode only (requires vendor\node;
                                    edit vendor\npm-online.npmrc registry= for your mirror)
5. Edit .env with Jira / GitLab settings
6. Start:
      start-backend.bat   → API (+ SPA) on http://0.0.0.0:8080/  (open 127.0.0.1:8080)
      start-frontend.bat  → UI on http://0.0.0.0:5173/         (proxies /api to backend)
      start.bat           → both (backend then frontend)
7. OpenCode TUI (after install-backends.bat or install-opencode-online.bat):
      start-opencode.bat
      start-opencode-serve.bat

8. Verify:  where opencode

Supported Python (this build): $($supportedPy -join ', ')
Portable Node (online OpenCode): vendor\node\node.exe (v$NODE_FULL_VERSION)
"@
Set-Content -Path (Join-Path $payload "START_HERE.txt") -Value $howTo -Encoding UTF8

if (Test-Path -LiteralPath $dl) {
    Remove-Item -LiteralPath $dl -Recurse -Force
}

# ---------------------------------------------------------------------------
# 7) Optional single zip for GitHub Releases only (CI artifact uploads the FOLDER)
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "Step 7: Creating optional release zip (folder is primary artifact)..."

$zipPath = Join-Path $OutDir "$DistName.zip"
if (Test-Path -LiteralPath $zipPath) {
    Remove-Item -LiteralPath $zipPath -Force
}

# Zip the payload directory WITHOUT an extra nested folder name:
# contents of zip root = install-dashboard.bat, install-backends.bat, install-codex.bat, vendor\, src\, ...
$tar = Get-Command tar -ErrorAction SilentlyContinue
if ($tar) {
    Push-Location $payload
    try {
        & tar -a -cf $zipPath *
        if ($LASTEXITCODE -ne 0) { throw "tar failed creating dist zip" }
    } finally {
        Pop-Location
    }
} else {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::CreateFromDirectory(
        $payload,
        $zipPath,
        [System.IO.Compression.CompressionLevel]::Optimal,
        $false  # do not nest an extra root directory
    )
}

$zipSize = (Get-Item -LiteralPath $zipPath).Length
$payloadSize = (Get-ChildItem -Path $payload -Recurse -File -ErrorAction SilentlyContinue |
    Measure-Object -Property Length -Sum).Sum

Write-Host ""
Write-Host "========================================"
Write-Host "  Build complete"
Write-Host "========================================"
Write-Host ("Folder : {0}" -f $payload)
Write-Host ("Folder size ~ {0:N1} MB" -f ($payloadSize / 1MB))
Write-Host ("Zip    : {0} ({1:N1} MB) — for Releases only" -f $zipPath, ($zipSize / 1MB))
Write-Host ""
Write-Host "CI uploads the FOLDER (one extract = install-dashboard.bat at top level)."
Write-Host "Supported Python: $($supportedPy -join ', ')"

if ($env:GITHUB_OUTPUT) {
    "zip_path=$zipPath" | Out-File -FilePath $env:GITHUB_OUTPUT -Append -Encoding utf8
    "dist_name=$DistName" | Out-File -FilePath $env:GITHUB_OUTPUT -Append -Encoding utf8
    "payload_path=$payload" | Out-File -FilePath $env:GITHUB_OUTPUT -Append -Encoding utf8
    "supported_python=$($supportedPy -join ',')" | Out-File -FilePath $env:GITHUB_OUTPUT -Append -Encoding utf8
}
