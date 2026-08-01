#Requires -Version 5.1
<#
.SYNOPSIS
  Resolve a professional SemVer-compatible product version for Windows dist builds.

.DESCRIPTION
  Source of truth for the base version is the repo root VERSION file (MAJOR.MINOR.PATCH).

  Rules (SemVer 2.0 style):
    - git tag vX.Y.Z (or X.Y.Z)  →  X.Y.Z                 (release)
    - branch main (no tag)       →  X.Y.Z+gSHA7           (build metadata)
    - branch develop             →  X.Y.Z-dev.YYYYMMDD.N+gSHA7  (prerelease)
    - other / PR / manual        →  X.Y.Z-dev.SHA7        (prerelease)
    - workflow_dispatch suffix   →  appended as -suffix when not a pure tag release

  Outputs (GITHUB_OUTPUT when present):
    version, version_safe, dist_name, channel, base_version
#>
[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [string]$DistSuffix = "",
    [string]$ProductName = "virtual_developer-windows-x64"
)

$ErrorActionPreference = "Stop"

function Get-RepoRoot {
    if ($RepoRoot) { return (Resolve-Path $RepoRoot).Path }
    return (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
}

function Read-BaseVersion([string]$Root) {
    $vf = Join-Path $Root "VERSION"
    if (-not (Test-Path -LiteralPath $vf)) {
        throw "VERSION file not found at repo root: $vf"
    }
    $raw = (Get-Content -LiteralPath $vf -Raw).Trim()
    if ($raw -notmatch '^\d+\.\d+\.\d+$') {
        throw "VERSION must be MAJOR.MINOR.PATCH (got: '$raw')"
    }
    return $raw
}

function Get-ShortSha {
    if ($env:GITHUB_SHA) { return $env:GITHUB_SHA.Substring(0, [Math]::Min(7, $env:GITHUB_SHA.Length)) }
    try {
        $s = (git rev-parse --short=7 HEAD 2>$null)
        if ($s) { return $s.Trim() }
    } catch {}
    return "unknown"
}

function Get-Channel {
    if ($env:GITHUB_REF_TYPE -eq "tag") { return "release" }
    $ref = if ($env:GITHUB_REF_NAME) { $env:GITHUB_REF_NAME } else { "" }
    $head = if ($env:GITHUB_HEAD_REF) { $env:GITHUB_HEAD_REF } else { "" }
    # PR builds use head ref; push uses ref name
    $branch = if ($env:GITHUB_EVENT_NAME -eq "pull_request" -and $head) { $head } else { $ref }
    if ($branch -eq "main" -or $branch -eq "master") { return "main" }
    if ($branch -eq "develop") { return "develop" }
    return "dev"
}

$root = Get-RepoRoot
$base = Read-BaseVersion $root
$sha = Get-ShortSha
$channel = Get-Channel
$run = if ($env:GITHUB_RUN_NUMBER) { $env:GITHUB_RUN_NUMBER } else { "0" }
$date = (Get-Date).ToUniversalTime().ToString("yyyyMMdd")

$version = $base
if ($env:GITHUB_REF_TYPE -eq "tag") {
    # Tag is authoritative for releases: v0.2.0 / 0.2.0 / v0.2.0-rc.1
    $tag = $env:GITHUB_REF_NAME
    $version = if ($tag.StartsWith("v")) { $tag.Substring(1) } else { $tag }
    $channel = "release"
} elseif ($channel -eq "main") {
    # Main builds without a tag: version + build metadata (not a prerelease)
    $version = "$base+g$sha"
} elseif ($channel -eq "develop") {
    $version = "$base-dev.$date.$run+g$sha"
} else {
    $version = "$base-dev.$sha"
}

if ($DistSuffix -and $channel -ne "release") {
    # sanitize suffix for SemVer pre-release / filename
    $safeSuf = ($DistSuffix -replace '[^0-9A-Za-z.-]', '-')
    if ($version -match '\+') {
        $version = $version -replace '\+', "-$safeSuf+"
    } else {
        $version = "$version-$safeSuf"
    }
}

# Zip/artifact names cannot contain '+' on some systems — use a safe form
$versionSafe = $version -replace '\+', '.'

$distName = "$ProductName-$versionSafe"

Write-Host "base_version=$base"
Write-Host "version=$version"
Write-Host "version_safe=$versionSafe"
Write-Host "channel=$channel"
Write-Host "dist_name=$distName"

if ($env:GITHUB_OUTPUT) {
    "base_version=$base" | Out-File -FilePath $env:GITHUB_OUTPUT -Append -Encoding utf8
    "version=$version" | Out-File -FilePath $env:GITHUB_OUTPUT -Append -Encoding utf8
    "version_safe=$versionSafe" | Out-File -FilePath $env:GITHUB_OUTPUT -Append -Encoding utf8
    "channel=$channel" | Out-File -FilePath $env:GITHUB_OUTPUT -Append -Encoding utf8
    "dist_name=$distName" | Out-File -FilePath $env:GITHUB_OUTPUT -Append -Encoding utf8
}

# Also emit for local shells
return [pscustomobject]@{
    BaseVersion = $base
    Version     = $version
    VersionSafe = $versionSafe
    Channel     = $channel
    DistName    = $distName
}
