#Requires -Version 5.1
<#
.SYNOPSIS
  Wait until an HTTP URL responds (or timeout).

.DESCRIPTION
  Used by start-backend.bat / start.bat. Avoids fragile inline PowerShell in .bat files.
  Exit 0 = success, 1 = timeout/error, 2 = body matched -FailPattern (optional).
  Exit 3 = HTTP 401 when -FailOnUnauthorized is set (server answered; auth missing or wrong).
  -Authorization is optional. Dashboard waits do not pass it.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Url,
    [int]$TimeoutSec = 60,
    [string]$OkPattern = "",
    [string]$FailPattern = "",
    [string]$Authorization = "",
    [switch]$FailOnUnauthorized,
    [switch]$Once
)

$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"

$deadline = (Get-Date).AddSeconds([Math]::Max(5, $TimeoutSec))
$lastErr = ""

while ((Get-Date) -lt $deadline) {
    try {
        $params = @{
            Uri = $Url
            UseBasicParsing = $true
            TimeoutSec = 3
        }
        if ($Authorization) {
            $params.Headers = @{ Authorization = $Authorization }
        }
        $resp = Invoke-WebRequest @params
        $code = [int]$resp.StatusCode
        $body = [string]$resp.Content
        if ($FailOnUnauthorized -and $code -eq 401) {
            Write-Host "HTTP 401 from $Url (server answered; authorization missing or wrong)"
            exit 3
        }
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
        $status = 0
        $respObj = $_.Exception.Response
        if ($respObj) {
            try { $status = [int]$respObj.StatusCode } catch { $status = 0 }
        }
        if ($status -eq 0 -and ([string]$_.Exception.Message) -match "\(401\)") {
            $status = 401
        }
        if ($FailOnUnauthorized -and $status -eq 401) {
            Write-Host "HTTP 401 from $Url (server answered; authorization missing or wrong)"
            exit 3
        }
        $lastErr = $_.Exception.Message
    }
    if ($Once) { break }
    Start-Sleep -Milliseconds 400
}

if ($Once) {
    Write-Host "NOT READY $Url"
} else {
    Write-Host "TIMEOUT waiting for $Url"
    if ($lastErr) { Write-Host "Last error: $lastErr" }
}
exit 1
