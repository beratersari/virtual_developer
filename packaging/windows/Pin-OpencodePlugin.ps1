#Requires -Version 5.1
<#
.SYNOPSIS
  Ensure opencode.json has a version-pinned oh-my-opencode / oh-my-openagent plugin
  and autoupdate=false (avoids black-screen Bun fetch at TUI start).
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ConfigPath,
    [Parameter(Mandatory = $true)][string]$Version
)

$ErrorActionPreference = "Stop"
if (-not (Test-Path -LiteralPath $ConfigPath)) {
    throw "Config not found: $ConfigPath"
}
if (-not $Version) {
    throw "Version is required"
}

$wantOmo = "oh-my-opencode@$Version"
$wantOma = "oh-my-openagent@$Version"
$raw = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8
$d = $raw | ConvertFrom-Json
$plugs = @()
if ($null -ne $d.plugin) { $plugs = @($d.plugin) }

$fixed = New-Object System.Collections.Generic.List[object]
$seen = $false
$changed = $false

foreach ($x in $plugs) {
    $s = [string]$x
    if ($s -eq "oh-my-opencode" -or $s -eq "oh-my-openagent" -or $s -match "^oh-my-opencod(e|agent)(@|$)") {
        $pinnedOk = ($s -eq $wantOmo -or $s -eq $wantOma)
        if (-not $pinnedOk) { $changed = $true }
        if (-not $seen) {
            if ($s -like "oh-my-openagent*") {
                [void]$fixed.Add($wantOma)
            } else {
                [void]$fixed.Add($wantOmo)
            }
            $seen = $true
        }
    } else {
        [void]$fixed.Add($x)
    }
}

if (-not $seen) {
    [void]$fixed.Add($wantOmo)
    $changed = $true
}

if ($d.PSObject.Properties.Name -notcontains "autoupdate" -or $d.autoupdate -ne $false) {
    $d | Add-Member -NotePropertyName autoupdate -NotePropertyValue $false -Force
    $changed = $true
}

if ($changed) {
    $d.plugin = $fixed.ToArray()
    $json = $d | ConvertTo-Json -Depth 10
    # PowerShell ConvertTo-Json may emit "schema" without $; force $schema if missing
    if ($json -notmatch '"\$schema"') {
        $json = $json -replace '"schema"\s*:', '"$schema":'
    }
    Set-Content -LiteralPath $ConfigPath -Value $json -Encoding UTF8
    Write-Host "  pinned plugin: $($fixed -join ', ')"
} else {
    Write-Host "  plugin already pinned: $($plugs -join ', ')"
}
exit 0
