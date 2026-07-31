#Requires -Version 5.1
<#
.SYNOPSIS
  Build the Windows offline distribution zip for JIRA Virtual Developer.

.DESCRIPTION
  Fetches pinned OpenCode, glab, oh-my-opencode, and Python wheels from the web,
  stages the app + a ready-to-copy %USERPROFILE%\.opencode tree, and writes a zip.

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
    # Invoke-WebRequest follows redirects; -UseBasicParsing avoids IE engine
    Invoke-WebRequest -Uri $Url -OutFile $OutFile -UseBasicParsing
    if (-not (Test-Path -LiteralPath $OutFile)) {
        throw "Download failed: $Url"
    }
    $size = (Get-Item -LiteralPath $OutFile).Length
    Write-Host ("  OK ({0:N1} MB)" -f ($size / 1MB))
}

function Expand-ZipSafe([string]$ZipPath, [string]$Dest) {
    Ensure-Dir $Dest
    Expand-Archive -Path $ZipPath -DestinationPath $Dest -Force
}

$root = Get-RepoRoot
$versionsFile = Join-Path $root "packaging\windows\versions.env"
if (-not (Test-Path -LiteralPath $versionsFile)) {
    throw "versions.env not found: $versionsFile"
}
$ver = Read-Versions $versionsFile

$OPENCODE_VERSION = $ver["OPENCODE_VERSION"]
$OH_MY_OPENCODE_VERSION = $ver["OH_MY_OPENCODE_VERSION"]
$GLAB_VERSION = $ver["GLAB_VERSION"]

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

# Mark this tree as a bundled offline dist so install.bat can detect vendor/
$marker = @"
virtual_developer Windows offline distribution
Built: $(Get-Date -Format "yyyy-MM-ddTHH:mm:ssK")
OpenCode=$OPENCODE_VERSION
oh-my-opencode=$OH_MY_OPENCODE_VERSION
glab=$GLAB_VERSION
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
# GitLab generic package URL (percent-encoded dots in path segment)
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
# 4) Stage %USERPROFILE%\.opencode template (binary + config + plugin)
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "Step 4: Building .opencode home template..."

$ocHome = Join-Path $vendor "opencode-home"
$ocBin = Join-Path $ocHome "bin"
Ensure-Dir $ocBin

Copy-Item -LiteralPath $opencodeExe.FullName -Destination (Join-Path $ocBin "opencode.exe") -Force
Copy-Item -LiteralPath $glabExe.FullName -Destination (Join-Path $ocBin "glab.exe") -Force

# Config + package.json from packaging templates (version pinned in package.json)
$pkgTemplate = Join-Path $root "packaging\windows\package.json"
$ocConfigTemplate = Join-Path $root "packaging\windows\opencode.json"
$omoConfigTemplate = Join-Path $root "packaging\windows\oh-my-opencode.json"

Copy-Item -LiteralPath $pkgTemplate -Destination (Join-Path $ocHome "package.json") -Force
Copy-Item -LiteralPath $ocConfigTemplate -Destination (Join-Path $ocHome "opencode.json") -Force
Copy-Item -LiteralPath $omoConfigTemplate -Destination (Join-Path $ocHome "oh-my-opencode.json") -Force

# Pin dependency version explicitly (always rewrite — avoids ConvertTo-Json quirks)
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

Write-Host "  Installing oh-my-opencode@$OH_MY_OPENCODE_VERSION into template (npm)..."
Push-Location $ocHome
try {
    # Prefer exact version from registry for reproducible offline install
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

Write-Host "  .opencode template ready: $ocHome"

# ---------------------------------------------------------------------------
# 5) Download Python wheels (offline pip install)
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "Step 5: Downloading Python wheels for offline install..."

$wheels = Join-Path $vendor "python-wheels"
Ensure-Dir $wheels
$req = Join-Path $root "requirements.txt"

python -m pip download -r $req -d $wheels
if ($LASTEXITCODE -ne 0) {
    throw "pip download failed"
}

# Also fetch pip/setuptools/wheel so venv bootstrap works offline-ish
python -m pip download pip setuptools wheel -d $wheels
if ($LASTEXITCODE -ne 0) {
    Write-Host "  WARNING: could not download pip/setuptools/wheel helpers"
}

$wheelCount = (Get-ChildItem -Path $wheels -File).Count
Write-Host "  Wheels staged: $wheelCount files"

# ---------------------------------------------------------------------------
# 6) Vendor metadata + drop download cache from payload
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "Step 6: Writing vendor metadata..."

$versionsCopy = @"
OPENCODE_VERSION=$OPENCODE_VERSION
OH_MY_OPENCODE_VERSION=$OH_MY_OPENCODE_VERSION
GLAB_VERSION=$GLAB_VERSION
BUILT_AT=$(Get-Date -Format "yyyy-MM-ddTHH:mm:ssK")
"@
Set-Content -Path (Join-Path $vendor "VERSIONS.txt") -Value $versionsCopy -Encoding UTF8
Copy-Item -LiteralPath $versionsFile -Destination (Join-Path $vendor "versions.env") -Force

# Remove temporary downloads to keep zip smaller
if (Test-Path -LiteralPath $dl) {
    Remove-Item -LiteralPath $dl -Recurse -Force
}

# ---------------------------------------------------------------------------
# 7) Zip
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "Step 7: Creating zip archive..."

$zipPath = Join-Path $OutDir "$DistName.zip"
if (Test-Path -LiteralPath $zipPath) {
    Remove-Item -LiteralPath $zipPath -Force
}

# Compress-Archive can struggle with very large trees / long paths; use .NET
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::CreateFromDirectory(
    $payload,
    $zipPath,
    [System.IO.Compression.CompressionLevel]::Optimal,
    $true  # include base directory name in zip
)

$zipSize = (Get-Item -LiteralPath $zipPath).Length
Write-Host ""
Write-Host "========================================"
Write-Host "  Build complete"
Write-Host "========================================"
Write-Host ("Zip : {0}" -f $zipPath)
Write-Host ("Size: {0:N1} MB" -f ($zipSize / 1MB))
Write-Host ""
Write-Host "User flow: extract zip -> run install.bat"
Write-Host "OpenCode lands in %USERPROFILE%\.opencode (bin + config + plugin)"

# Export path for GitHub Actions
if ($env:GITHUB_OUTPUT) {
    "zip_path=$zipPath" | Out-File -FilePath $env:GITHUB_OUTPUT -Append -Encoding utf8
    "dist_name=$DistName" | Out-File -FilePath $env:GITHUB_OUTPUT -Append -Encoding utf8
}
