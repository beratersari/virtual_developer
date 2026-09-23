#Requires -Version 5.1
<#
.SYNOPSIS
  Fast layout check for a staged Windows offline payload.

  Missing opencode.exe is a warning when vendor/opencode-home.zip is present
  (GitHub-hosted Defender has been quarantining that binary after the PE check).
  Other missing paths still fail the job.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$PayloadDir
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $PayloadDir)) {
    throw "Payload dir missing: $PayloadDir"
}

$p = (Resolve-Path -LiteralPath $PayloadDir).Path

$ocmExe = Join-Path $p "opencoderman\vendor\bin\windows\opencode.exe"
$vendorExe = Join-Path $p "vendor\bin\opencode.exe"
$ocZip = Join-Path $p "vendor\opencode-home.zip"
if (-not (Test-Path -LiteralPath $ocmExe) -and (Test-Path -LiteralPath $vendorExe)) {
    New-Item -ItemType Directory -Force -Path (Split-Path $ocmExe) | Out-Null
    Copy-Item -LiteralPath $vendorExe -Destination $ocmExe -Force
    Write-Host "Restored opencoderman opencode.exe from vendor/bin (AV quarantine)"
}

$required = @(
    ".env.example",
    "install-dashboard.bat",
    "install-dashboard-system-python.bat",
    "install-opencode-online.bat",
    "install-backends.bat",
    "install-agents.bat",
    "packaging\windows\Install-OpencodeAgents.ps1",
    "install-codex.bat",
    "packaging\install_opencode.py",
    "packaging\windows\Install-Backends.ps1",
    "opencoderman\install.py",
    "opencoderman.pin",
    "opencoderman\agents\derman-reviewer.md",
    "opencoderman\agents\derman-test.md",
    "start.bat",
    "start-backend.bat",
    "start-frontend.bat",
    "start-opencode-serve.bat",
    "cli.py",
    "src\daemon.py",
    "web\dist\index.html",
    "vendor\opencode-home.zip",
    "vendor\codex-package-x86_64-pc-windows-msvc.tar.gz",
    "packaging\windows\codex-config.toml",
    "vendor\python-wheels",
    "vendor\node\node.exe",
    "vendor\node\npm.cmd",
    "vendor\npm-online.npmrc",
    "vendor\online-sources.env",
    "packaging\windows\Install-OpencodeOnline.ps1",
    "packaging\windows\npm-online.npmrc",
    "packaging\windows\Stop-VdProcesses.ps1",
    "packaging\windows\Wait-Http.ps1",
    "packaging\windows\Ensure-OpencodeServe.ps1",
    "packaging\windows\serve_frontend.py"
)
$optionalExe = @(
    "vendor\bin\opencode.exe",
    "opencoderman\vendor\bin\windows\opencode.exe"
)

$missing = @()
foreach ($rel in $required) {
    $path = Join-Path $p $rel
    if (-not (Test-Path -LiteralPath $path)) {
        $missing += $rel
        Write-Host "MISSING $rel"
    } else {
        Write-Host "OK $rel"
    }
}

$missingExe = @()
foreach ($rel in $optionalExe) {
    $path = Join-Path $p $rel
    if (-not (Test-Path -LiteralPath $path)) {
        $missingExe += $rel
        Write-Host "MISSING EXE $rel"
    } else {
        Write-Host "OK $rel"
    }
}

if ($missingExe.Count -gt 0) {
    if (Test-Path -LiteralPath $ocZip) {
        Write-Host ("WARN opencode.exe missing after AV; zip is present so installers can extract it: " + ($missingExe -join ", "))
    } else {
        $missing += $missingExe
    }
}

if (Test-Path -LiteralPath (Join-Path $p "install.bat")) {
    throw "install.bat must not be in the payload - use install-dashboard.bat + install-backends.bat"
}
if (Test-Path -LiteralPath (Join-Path $p "web\node_modules")) {
    throw "web/node_modules must not be in the offline zip"
}
if (Test-Path -LiteralPath (Join-Path $p "vendor\opencode-home\node_modules")) {
    throw "expanded vendor/opencode-home/node_modules must not be in the zip"
}

$spa = Join-Path $p "web\dist"
$js = @(Get-ChildItem -LiteralPath (Join-Path $spa "assets") -Filter "index-*.js" -File -ErrorAction SilentlyContinue)
$css = @(Get-ChildItem -LiteralPath (Join-Path $spa "assets") -Filter "index-*.css" -File -ErrorAction SilentlyContinue)
if ($js.Count -lt 1) { $missing += "web/dist/assets/index-*.js" }
if ($css.Count -lt 1) { $missing += "web/dist/assets/index-*.css" }

$stale = @()
if ($js.Count -ge 1) {
    $jsText = (
        $js | ForEach-Object {
            Get-Content -LiteralPath $_.FullName -Raw -Encoding UTF8 -ErrorAction Stop
        }
    ) -join "`n"
    foreach ($needle in @(
            "agent_task_max_retries",
            "agent_task_max_incomplete_retries",
            "Maximum extra attempts after a timeout or error.",
            "Default model for plan, build, test, and /yaver.",
            "Type any id Codex accepts",
            "agent_backend",
            "Delete unused clones after (days)"
        )) {
        if ($jsText -notmatch [regex]::Escape($needle)) {
            $stale += $needle
            Write-Host "MISSING SPA needle: $needle"
        } else {
            Write-Host "OK SPA contains $needle"
        }
    }
    foreach ($gone in @(
            "opencode_serve_max_compact_continues",
            "Codex API key"
        )) {
        if ($jsText -match [regex]::Escape($gone)) {
            throw "Stale dashboard SPA: removed setting '$gone' still in bundle"
        }
    }
    Write-Host "OK SPA bundle $($js[0].Name) + $($css[0].Name)"
}

if ($missing.Count -gt 0 -or $stale.Count -gt 0) {
    $msg = @()
    if ($missing.Count -gt 0) { $msg += "missing: $($missing -join ', ')" }
    if ($stale.Count -gt 0) { $msg += "SPA missing needles: $($stale -join ', ')" }
    throw ($msg -join " | ")
}

Write-Host "Payload layout OK (no full install smoke)"
