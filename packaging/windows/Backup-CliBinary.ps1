# Rename an existing CLI binary with today's date, then copy the new file
# into that same directory. When nothing is installed yet, use -Fallback.
param(
    [Parameter(Mandatory = $true)][string]$Source,
    [Parameter(Mandatory = $true)][string]$Name,
    [Parameter(Mandatory = $true)][string]$Fallback
)

$ErrorActionPreference = "Stop"
if (-not (Test-Path -LiteralPath $Source)) {
    throw "Missing new binary: $Source"
}

$found = $null
$cmd = Get-Command -Name $Name -ErrorAction SilentlyContinue
if ($cmd -and $cmd.Source -and (Test-Path -LiteralPath $cmd.Source)) {
    $candidate = [IO.Path]::GetFullPath($cmd.Source)
    $newFile = [IO.Path]::GetFullPath($Source)
    if ($candidate -ne $newFile) {
        $found = $candidate
    }
}
if (-not $found -and (Test-Path -LiteralPath $Fallback)) {
    $found = [IO.Path]::GetFullPath($Fallback)
}

$dest = if ($found) { $found } else { $Fallback }
$dir = Split-Path -Parent $dest
if (-not (Test-Path -LiteralPath $dir)) {
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
}
if (Test-Path -LiteralPath $dest) {
    $stamp = Get-Date -Format "yyyyMMdd"
    $bak = "$dest.$stamp"
    if (Test-Path -LiteralPath $bak) {
        $bak = "$dest.$stamp-$(Get-Date -Format 'HHmmss')"
    }
    Move-Item -LiteralPath $dest -Destination $bak
    Write-Host "[OK] Renamed existing binary to $bak"
}
Copy-Item -LiteralPath $Source -Destination $dest -Force
Write-Output $dest
