# Copy derman-build / derman-plan and skills into an existing OpenCode home.
# Does not install the CLI. Never writes %USERPROFILE%\.config\opencode.
#
# Home detection (first match):
#   1. -OpenCodeHome
#   2. OPENCODE_HOME
#   3. %USERPROFILE%\.opencode when it looks installed
#   4. Directory of opencode.exe on PATH (...\.opencode\bin\opencode.exe)
#
# ASCII only. Do not name locals after PowerShell automatic variables.
param(
    [string]$SourceRoot = "",
    [string]$OpenCodeHome = ""
)

$ErrorActionPreference = "Stop"

function Test-OpencodeHome([string]$PathValue) {
    if (-not $PathValue) { return $false }
    if (-not (Test-Path -LiteralPath $PathValue -PathType Container)) { return $false }
    $markers = @(
        (Join-Path $PathValue "bin\opencode.exe"),
        (Join-Path $PathValue "bin\opencode"),
        (Join-Path $PathValue "opencode.json"),
        (Join-Path $PathValue "agents")
    )
    foreach ($m in $markers) {
        if (Test-Path -LiteralPath $m) { return $true }
    }
    return $false
}

function Find-SourceTrees([string]$Root) {
    foreach ($rel in @("opencoderman", "opencode_configs", "")) {
        if ($rel) {
            $base = Join-Path $Root $rel
        } else {
            $base = $Root
        }
        $agents = Join-Path $base "agents"
        $skills = Join-Path $base "skills"
        if ((Test-Path -LiteralPath $agents -PathType Container) -and (Test-Path -LiteralPath $skills -PathType Container)) {
            return @{ Agents = $agents; Skills = $skills }
        }
    }
    return $null
}

function Resolve-SourceRoot([string]$Hint) {
    $here = $PSScriptRoot
    $candidates = @()
    if ($Hint) { $candidates += $Hint }
    if ($here) {
        $candidates += $here
        $candidates += (Split-Path -Parent $here)
        $packParent = Split-Path -Parent $here
        if ($packParent) { $candidates += (Split-Path -Parent $packParent) }
    }
    $candidates += (Get-Location).Path
    foreach ($root in $candidates) {
        if (-not $root) { continue }
        $found = Find-SourceTrees $root
        if ($found) { return $found }
    }
    throw "Could not find opencoderman\agents and opencoderman\skills next to this script."
}

function Home-FromBinary([string]$ExePath) {
    if (-not $ExePath) { return $null }
    $binDir = Split-Path -Parent $ExePath
    if (-not $binDir) { return $null }
    $leaf = Split-Path -Leaf $binDir
    if ($leaf -eq "bin") {
        $homeDir = Split-Path -Parent $binDir
        $homeLeaf = Split-Path -Leaf $homeDir
        if ($homeLeaf -eq ".opencode" -or $homeLeaf -eq "opencode") { return $homeDir }
        if (Test-OpencodeHome $homeDir) { return $homeDir }
    }
    if (Test-OpencodeHome $binDir) { return $binDir }
    return $null
}

function Resolve-OpencodeHome([string]$Explicit) {
    if ($Explicit) {
        if (Test-Path -LiteralPath $Explicit) { return $Explicit }
        throw "OpenCode home not found: $Explicit"
    }
    if ($env:OPENCODE_HOME) {
        $fromEnv = $env:OPENCODE_HOME.Trim()
        if ($fromEnv -and (Test-Path -LiteralPath $fromEnv)) { return $fromEnv }
    }
    $defaultHome = Join-Path $env:USERPROFILE ".opencode"
    if (Test-OpencodeHome $defaultHome) { return $defaultHome }

    $cmd = Get-Command opencode.exe -ErrorAction SilentlyContinue
    if (-not $cmd) { $cmd = Get-Command opencode -ErrorAction SilentlyContinue }
    if ($cmd -and $cmd.Source) {
        $derived = Home-FromBinary $cmd.Source
        if ($derived) { return $derived }
    }
    throw "OpenCode is not installed (no OPENCODE_HOME, no $defaultHome, and opencode is not on PATH). Install OpenCode first, then re-run this script."
}

function Copy-Tree([string]$Src, [string]$Dest) {
    if (-not (Test-Path -LiteralPath $Dest -PathType Container)) {
        New-Item -ItemType Directory -Force -Path $Dest | Out-Null
    }
    $robocopy = Get-Command robocopy.exe -ErrorAction SilentlyContinue
    if ($robocopy) {
        & robocopy.exe $Src $Dest /E /NFL /NDL /NJH /NJS /NC /NS /NP | Out-Null
        # robocopy: 0-7 are success (extra files, copies, mismatches).
        if ($LASTEXITCODE -ge 8) {
            throw "robocopy failed with exit $LASTEXITCODE ($Src -> $Dest)"
        }
        return
    }
    Copy-Item -Path (Join-Path $Src "*") -Destination $Dest -Recurse -Force
}

function Copy-PlanBuildAgents([string]$Src, [string]$Dest) {
    if (-not (Test-Path -LiteralPath $Dest -PathType Container)) {
        New-Item -ItemType Directory -Force -Path $Dest | Out-Null
    }
    foreach ($name in @("derman-build.md", "derman-plan.md", "derman-test.md")) {
        $srcFile = Join-Path $Src $name
        if (-not (Test-Path -LiteralPath $srcFile -PathType Leaf)) {
            throw "required agent missing: $srcFile"
        }
        Copy-Item -LiteralPath $srcFile -Destination (Join-Path $Dest $name) -Force
    }
}

$trees = Resolve-SourceRoot $SourceRoot
$homeDir = Resolve-OpencodeHome $OpenCodeHome
$destAgents = Join-Path $homeDir "agents"
$destSkills = Join-Path $homeDir "skills"

Write-Host "OpenCode home : $homeDir"
Write-Host "Source agents : $($trees.Agents) (derman-build, derman-plan only)"
Write-Host "Source skills : $($trees.Skills)"

Copy-PlanBuildAgents $trees.Agents $destAgents
Copy-Tree $trees.Skills $destSkills

foreach ($name in @("derman-build.md", "derman-plan.md", "derman-test.md")) {
    $marker = Join-Path $destAgents $name
    if (-not (Test-Path -LiteralPath $marker -PathType Leaf)) {
        throw "Copy finished but missing $marker"
    }
}

Write-Host "[OK] agents -> $destAgents (derman-build, derman-plan)"
Write-Host "[OK] skills -> $destSkills"
exit 0
