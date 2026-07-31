#Requires -Version 5.1
<#
.SYNOPSIS
  Build the Windows offline distribution zip for JIRA Virtual Developer.

.DESCRIPTION
  Fetches pinned OpenCode, glab, oh-my-opencode, and Python wheels (3.10+) from
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
    if (-not (Test-Path -LiteralPath $NodeModules)) { return }
    Write-Host "  Pruning node_modules (docs/tests/maps) to shrink archive..."

    $dirNames = @(
        "test", "tests", "__tests__", "docs", "doc", "example", "examples",
        "coverage", ".github", ".circleci", ".vscode", "benchmark", "benchmarks",
        "man", "website", "demo", "demos"
    )
    Get-ChildItem -Path $NodeModules -Recurse -Force -Directory -ErrorAction SilentlyContinue |
        Where-Object { $dirNames -contains $_.Name } |
        ForEach-Object {
            Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue
        }

    $fileFilters = @("*.map", "*.md", "*.markdown", "CHANGELOG*", "HISTORY*", "AUTHORS*", ".npmignore", ".eslintrc*", "tsconfig*.json", "*.ts")
    foreach ($filter in $fileFilters) {
        Get-ChildItem -Path $NodeModules -Recurse -Force -File -Filter $filter -ErrorAction SilentlyContinue |
            Where-Object {
                # Keep package entrypoints that might be .ts in rare packages; drop types/source maps/docs
                $_.Name -notmatch '^\.d\.ts$' -and $_.Extension -ne ".d.ts"
            } |
            ForEach-Object {
                # Do not delete package.json-adjacent runtime needs; *.ts sources are not required at runtime
                Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue
            }
    }

    # Drop TypeScript declaration files (runtime not needed)
    Get-ChildItem -Path $NodeModules -Recurse -Force -File -Filter "*.d.ts" -ErrorAction SilentlyContinue |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue }
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
$PYTHON_MIN_VERSION = if ($ver["PYTHON_MIN_VERSION"]) { $ver["PYTHON_MIN_VERSION"] } else { "3.10" }
$wheelVersionList = if ($ver["PYTHON_WHEEL_VERSIONS"]) {
    @($ver["PYTHON_WHEEL_VERSIONS"] -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ })
} else {
    @("3.10", "3.11", "3.12", "3.13")
}

if (-not $OPENCODE_VERSION) { throw "OPENCODE_VERSION missing in versions.env" }
if (-not $OH_MY_OPENCODE_VERSION) { throw "OH_MY_OPENCODE_VERSION missing in versions.env" }
if (-not $GLAB_VERSION) { throw "GLAB_VERSION missing in versions.env" }

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
    "install.bat",
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

$marker = @"
virtual_developer Windows offline distribution
Built: $(Get-Date -Format "yyyy-MM-ddTHH:mm:ssK")
OpenCode=$OPENCODE_VERSION
oh-my-opencode=$OH_MY_OPENCODE_VERSION
glab=$GLAB_VERSION
PythonMin=$PYTHON_MIN_VERSION
PythonWheels=$($wheelVersionList -join ',')
OpenCodeHome=vendor/opencode-home.zip (single archive — extract via install.bat)
"@
Set-Content -Path (Join-Path $payload "DIST_VERSION.txt") -Value $marker -Encoding UTF8

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
# Record architecture for install-time checks / support
@(
    "OPENCODE_ARCH=win_amd64"
    "OPENCODE_ASSET=$OPENCODE_ASSET"
    "OPENCODE_SHA256=$ocSha"
    "OPENCODE_BYTES=$((Get-Item -LiteralPath $opencodeExe.FullName).Length)"
    "TARGET_OS=Windows 10/11 64-bit (x64 / AMD64)"
) | Set-Content -Path (Join-Path $ocBin "ARCH.txt") -Encoding UTF8

$pkgPath = Join-Path $ocHome "package.json"
$pkgBody = @"
{
  "name": "virtual-developer-opencode-home",
  "private": true,
  "description": "OpenCode user home dependencies for JIRA Virtual Developer",
  "dependencies": {
    "oh-my-opencode": "$OH_MY_OPENCODE_VERSION"
  }
}
"@
Set-Content -Path $pkgPath -Value $pkgBody -Encoding UTF8

Copy-Item -LiteralPath (Join-Path $root "packaging\windows\opencode.json") -Destination (Join-Path $ocHome "opencode.json") -Force
Copy-Item -LiteralPath (Join-Path $root "packaging\windows\oh-my-opencode.json") -Destination (Join-Path $ocHome "oh-my-opencode.json") -Force

Write-Host "  Installing oh-my-opencode@$OH_MY_OPENCODE_VERSION (npm)..."
Push-Location $ocHome
try {
    npm install --omit=dev --no-fund --no-audit "oh-my-opencode@$OH_MY_OPENCODE_VERSION"
    if ($LASTEXITCODE -ne 0) {
        throw "npm install oh-my-opencode@$OH_MY_OPENCODE_VERSION failed (exit $LASTEXITCODE)"
    }
} finally {
    Pop-Location
}

if (-not (Test-Path -LiteralPath (Join-Path $ocHome "node_modules\oh-my-opencode"))) {
    throw "oh-my-opencode missing after npm install"
}

Optimize-NodeModules (Join-Path $ocHome "node_modules")

# Single archive in vendor — outer dist zip only has this one file for OpenCode home
Ensure-Dir $vendor
$ocHomeZip = Join-Path $vendor "opencode-home.zip"
Write-Host "  Packing OpenCode home -> vendor\opencode-home.zip ..."
New-ZipFromDirectory -SourceDir $ocHome -ZipPath $ocHomeZip
$ocZipSize = (Get-Item -LiteralPath $ocHomeZip).Length
Write-Host ("  OpenCode home archive: {0:N1} MB" -f ($ocZipSize / 1MB))

# Do not ship expanded tree (prevents long-path extract errors for users)
Remove-Item -LiteralPath $ocBuildRoot -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "  vendor\opencode-home.zip ready (install.bat extracts to %USERPROFILE%\.opencode)"

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
    "# install.bat rejects any other version (e.g. too-new 3.x without wheels).",
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
PYTHON_MIN_VERSION=$PYTHON_MIN_VERSION
PYTHON_WHEEL_VERSIONS=$($wheelVersionList -join ',')
SUPPORTED_PYTHON=$($supportedPy -join ',')
BUILT_AT=$(Get-Date -Format "yyyy-MM-ddTHH:mm:ssK")
NOTE=Run install.bat from this folder. Do not manually unpack vendor files.
"@
Set-Content -Path (Join-Path $vendor "VERSIONS.txt") -Value $versionsCopy -Encoding UTF8
Copy-Item -LiteralPath $versionsFile -Destination (Join-Path $vendor "versions.env") -Force

# Clear one-screen install hint at payload root
$howTo = @"
JIRA Virtual Developer — Windows offline package
================================================
1. You only need ONE extract (the GitHub Actions download).
2. Open THIS folder (should contain install.bat next to vendor\ and src\).
3. Install Python from the supported list (see vendor\SUPPORTED_PYTHON.txt).
4. Double-click install.bat
5. Edit .env, then:  .venv\Scripts\activate  &&  python cli.py start

Supported Python (this build): $($supportedPy -join ', ')
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
# contents of zip root = install.bat, vendor\, src\, ...
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
Write-Host "CI uploads the FOLDER (one extract = install.bat at top level)."
Write-Host "Supported Python: $($supportedPy -join ', ')"

if ($env:GITHUB_OUTPUT) {
    "zip_path=$zipPath" | Out-File -FilePath $env:GITHUB_OUTPUT -Append -Encoding utf8
    "dist_name=$DistName" | Out-File -FilePath $env:GITHUB_OUTPUT -Append -Encoding utf8
    "payload_path=$payload" | Out-File -FilePath $env:GITHUB_OUTPUT -Append -Encoding utf8
    "supported_python=$($supportedPy -join ',')" | Out-File -FilePath $env:GITHUB_OUTPUT -Append -Encoding utf8
}
