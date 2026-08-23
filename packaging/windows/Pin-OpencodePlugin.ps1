#Requires -Version 5.1
<#
.SYNOPSIS
  Ensure opencode.json uses stock OpenCode (plugin=[], autoupdate=false).
  Strips oh-my-openagent / oh-my-opencode so Bun will not fetch Sisyphus/Prometheus.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ConfigPath,
    [string]$Version = ""
)

$ErrorActionPreference = "Stop"
if (-not (Test-Path -LiteralPath $ConfigPath)) {
    throw "Config not found: $ConfigPath"
}

$raw = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8
$d = $raw | ConvertFrom-Json
$plugs = @()
if ($null -ne $d.plugin) { $plugs = @($d.plugin) }

$keep = New-Object System.Collections.Generic.List[object]
$changed = $false
foreach ($x in $plugs) {
    $s = [string]$x
    if ($s -eq "oh-my-opencode" -or $s -eq "oh-my-openagent" -or $s -match "^oh-my-opencod(e|agent)(@|$)") {
        $changed = $true
        continue
    }
    [void]$keep.Add($x)
}

if ($d.PSObject.Properties.Name -notcontains "autoupdate" -or $d.autoupdate -ne $false) {
    $d | Add-Member -NotePropertyName autoupdate -NotePropertyValue $false -Force
    $changed = $true
}

if ($changed -or $plugs.Count -ne $keep.Count) {
    $d.plugin = $keep.ToArray()
    $json = $d | ConvertTo-Json -Depth 10
    if ($json -notmatch '"\$schema"') {
        $json = $json -replace '"schema"\s*:', '"$schema":'
    }
    Set-Content -LiteralPath $ConfigPath -Value $json -Encoding UTF8
    Write-Host "  stock OpenCode config (oh-my plugin removed)"
} else {
    Write-Host "  plugin list already has no oh-my-openagent"
}
exit 0
