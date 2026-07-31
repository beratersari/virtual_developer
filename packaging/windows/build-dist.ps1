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

        # abi3 / stable ABI wheels often tagged cp3X but also py3
        python -m pip download `
            -r $Requirements `
            -d $WheelsDir `
            --python-version $pv `
            --platform win_amd64 `
            --implementation cp `
            --abi none `
            --only-binary=:all: `
            --prefer-binary 2>$null | Out-Null
    }

    # Bootstrap helpers (py3-none-any or version-specific)
    Write-Host "  pip download pip/setuptools/wheel helpers..."
    python -m pip download pip setuptools wheel -d $WheelsDir --prefer-binary
    foreach ($pv in $PyVersions) {
        python -m pip download pip setuptools wheel -d $WheelsDir `
            --python-version $pv `
            --platform win_amd64 `
            --implementation cp `
            --abi ("cp" + ($pv -replace "\.", "")) `
            --only-binary=:all: `
            --prefer-binary 2>$null | Out-Null
    }
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

$opencodeZip = Join-Path $dl "opencode-windows-x64.zip"
$opencodeUrl = "https://github.com/anomalyco/opencode/releases/download/v$OPENCODE_VERSION/opencode-windows-x64.zip"
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
BUILT_AT=$(Get-Date -Format "yyyy-MM-ddTHH:mm:ssK")
OPENCODE_HOME_ARCHIVE=opencode-home.zip
"@
Set-Content -Path (Join-Path $vendor "VERSIONS.txt") -Value $versionsCopy -Encoding UTF8
Copy-Item -LiteralPath $versionsFile -Destination (Join-Path $vendor "versions.env") -Force

if (Test-Path -LiteralPath $dl) {
    Remove-Item -LiteralPath $dl -Recurse -Force
}

# ---------------------------------------------------------------------------
# 7) Zip outer distribution (no deep node_modules — fast extract)
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "Step 7: Creating distribution zip..."

$zipPath = Join-Path $OutDir "$DistName.zip"
if (Test-Path -LiteralPath $zipPath) {
    Remove-Item -LiteralPath $zipPath -Force
}

$tar = Get-Command tar -ErrorAction SilentlyContinue
if ($tar) {
    Push-Location $stage
    try {
        & tar -a -cf $zipPath $DistName
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
        $true
    )
}

$zipSize = (Get-Item -LiteralPath $zipPath).Length
Write-Host ""
Write-Host "========================================"
Write-Host "  Build complete"
Write-Host "========================================"
Write-Host ("Zip : {0}" -f $zipPath)
Write-Host ("Size: {0:N1} MB" -f ($zipSize / 1MB))
Write-Host ""
Write-Host "User flow: extract zip (fast) -> run install.bat"
Write-Host "  install.bat extracts vendor\opencode-home.zip -> %USERPROFILE%\.opencode"
Write-Host "  Python wheels cover: $($wheelVersionList -join ', ')"

if ($env:GITHUB_OUTPUT) {
    "zip_path=$zipPath" | Out-File -FilePath $env:GITHUB_OUTPUT -Append -Encoding utf8
    "dist_name=$DistName" | Out-File -FilePath $env:GITHUB_OUTPUT -Append -Encoding utf8
}
