#Requires -Version 5.1
<#
.SYNOPSIS
  Fail if any file under Root has a relative path longer than MaxRelativeChars.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Root,
    [int]$MaxRelativeChars = 200
)

$ErrorActionPreference = "Stop"
if (-not (Test-Path -LiteralPath $Root)) {
    throw "Root not found: $Root"
}

$rootFull = (Resolve-Path -LiteralPath $Root).Path
$maxRel = 0
$worst = ""
Get-ChildItem -LiteralPath $rootFull -Recurse -Force -ErrorAction SilentlyContinue | ForEach-Object {
    $rel = $_.FullName.Substring($rootFull.Length).TrimStart('\')
    if ($rel.Length -gt $maxRel) {
        $maxRel = $rel.Length
        $worst = $rel
    }
}

Write-Host "Max relative path under ${Root}: $maxRel chars"
if ($maxRel -gt 0) {
    Write-Host "Worst: $worst"
}

if ($maxRel -gt $MaxRelativeChars) {
    throw @"
Path-length budget exceeded: $maxRel > $MaxRelativeChars
Worst relative path:
  $worst

Flatten/prune node_modules before packaging. Windows MAX_PATH is 260 unless
long paths are enabled; many corporate PCs still use the classic limit.
"@
}

exit 0
