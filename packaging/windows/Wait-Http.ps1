#Requires -Version 5.1
<#
.SYNOPSIS
  Wait until an HTTP URL responds (or timeout).

.DESCRIPTION
  Used by start-backend.bat / start.bat. Avoids fragile inline PowerShell in .bat files.
  Exit 0 = success, 1 = timeout/error, 2 = body matched -FailPattern (optional).
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Url,
    [int]$TimeoutSec = 60,
    [string]$OkPattern = "",
    [string]$FailPattern = ""
)

$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"

$deadline = (Get-Date).AddSeconds([Math]::Max(5, $TimeoutSec))
$lastErr = ""

while ((Get-Date) -lt $deadline) {
    try {
        $resp = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3
        $code = [int]$resp.StatusCode
        $body = [string]$resp.Content
        if ($code -ge 200 -and $code -lt 500) {
            if ($FailPattern -and ($body -match $FailPattern)) {
                Write-Host "FAIL pattern matched at $Url (HTTP $code)"
                exit 2
            }
            if (-not $OkPattern -or ($body -match $OkPattern)) {
                Write-Host "OK $Url (HTTP $code)"
                exit 0
            }
            Write-Host "OK HTTP $code but OkPattern not matched yet..."
        }
    } catch {
        $lastErr = $_.Exception.Message
    }
    Start-Sleep -Milliseconds 400
}

Write-Host "TIMEOUT waiting for $Url"
if ($lastErr) { Write-Host "Last error: $lastErr" }
exit 1
