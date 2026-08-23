#Requires -Version 5.1
<#
.SYNOPSIS
  Verify a Windows PE executable is AMD64 (x64 / 64-bit), not x86 or ARM64.

.DESCRIPTION
  The official OpenCode CLI we ship is win_amd64. A wrong or truncated binary
  often surfaces as Windows "not compatible with the version of Windows"
  (especially 64-bit OS). Fail fast with a clear message.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Path,
    [long]$MinBytes = 10MB,
    [switch]$Quiet
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $Path)) {
    throw "File not found: $Path"
}

$item = Get-Item -LiteralPath $Path
if ($item.Length -lt $MinBytes) {
    throw @"
File looks truncated or wrong: $Path
Size: $($item.Length) bytes (expected at least $MinBytes)
Re-run install-backends.bat from a complete offline package (do not skip files when extracting).
"@
}

# Read PE headers
$fs = [System.IO.File]::OpenRead($item.FullName)
try {
    $br = New-Object System.IO.BinaryReader($fs)
    $mz = $br.ReadUInt16()
    if ($mz -ne 0x5A4D) {  # 'MZ'
        throw "Not a Windows PE executable (missing MZ): $Path"
    }
    $fs.Seek(0x3C, [System.IO.SeekOrigin]::Begin) | Out-Null
    $e_lfanew = $br.ReadInt32()
    if ($e_lfanew -le 0 -or $e_lfanew -gt 1024) {
        throw "Invalid PE header offset in: $Path"
    }
    $fs.Seek($e_lfanew, [System.IO.SeekOrigin]::Begin) | Out-Null
    $peSig = $br.ReadUInt32()
    if ($peSig -ne 0x00004550) {  # 'PE\0\0'
        throw "Not a PE file: $Path"
    }
    $machine = $br.ReadUInt16()
} finally {
    $fs.Dispose()
}

# IMAGE_FILE_MACHINE_AMD64 = 0x8664
# IMAGE_FILE_MACHINE_I386  = 0x14c
# IMAGE_FILE_MACHINE_ARM64 = 0xAA64
$archName = switch ($machine) {
    0x8664 { "AMD64 (x64 / 64-bit)" }
    0x014c { "i386 (32-bit)" }
    0xAA64 { "ARM64" }
    default { "unknown (0x{0:X4})" -f $machine }
}

if ($machine -ne 0x8664) {
    throw @"
Wrong CPU architecture for this package: $Path
Detected: $archName
Required: AMD64 (x64 / 64-bit) — the standard for 64-bit Windows PCs.

This offline dist only ships the official opencode-windows-x64 build.
Delete %USERPROFILE%\.opencode and re-run install-backends.bat from a fresh package.
"@
}

if (-not $Quiet) {
    Write-Host ("OK AMD64 PE: {0} ({1:N1} MB)" -f $Path, ($item.Length / 1MB))
}
exit 0
