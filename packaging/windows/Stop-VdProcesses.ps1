#Requires -Version 5.1
<#
.SYNOPSIS
  Stop previous Virtual Developer / dashboard processes on Windows.

.DESCRIPTION
  Frees the ops dashboard port (default 8080) and optional Vite port (5173),
  and kills python processes that look like this product's daemon so start.bat
  can restart cleanly.
#>
[CmdletBinding()]
param(
    [int]$DashboardPort = 8080,
    [int]$VitePort = 5173
)

$ErrorActionPreference = "Continue"

function Stop-ListenersOnPort([int]$Port) {
    # Port 0 means "skip this port" (start-backend / start-frontend call selectively)
    if ($Port -le 0) { return }
    $conns = @()
    try {
        $conns = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    } catch {
        # Fallback: netstat parse (older shells / restricted modules)
        $lines = @(netstat -ano 2>$null | Select-String ":$Port\s")
        foreach ($line in $lines) {
            $parts = ($line.ToString() -split "\s+") | Where-Object { $_ }
            if ($parts.Count -ge 5 -and $parts[-1] -match "^\d+$") {
                $procId = [int]$parts[-1]
                if ($procId -gt 0) {
                    try {
                        Stop-Process -Id $procId -Force -ErrorAction Stop
                        Write-Host "  Killed PID $procId (netstat port $Port)"
                    } catch {
                        Write-Host "  Could not kill PID $procId : $($_.Exception.Message)"
                    }
                }
            }
        }
        return
    }

    foreach ($c in $conns) {
        $procId = [int]$c.OwningProcess
        if ($procId -le 0) { continue }
        try {
            $p = Get-Process -Id $procId -ErrorAction SilentlyContinue
            $name = if ($p) { $p.ProcessName } else { "?" }
            Stop-Process -Id $procId -Force -ErrorAction Stop
            Write-Host "  Killed PID $procId ($name) listening on $Port"
        } catch {
            Write-Host "  Could not kill PID $procId on $Port : $($_.Exception.Message)"
        }
    }
}

function Stop-DaemonPythons {
    # Use single-quoted patterns only (double-quoted \" breaks PowerShell parsing).
    $patterns = @(
        'src\.daemon',
        'src/daemon',
        'cli\.py(\s+|").*start',
        'uvicorn.*dashboard',
        'virtual_developer.*daemon',
        '-m\s+src\.daemon'
    )
    try {
        # WMI filter: one name at a time (OR with quotes is fragile across PS versions)
        $procs = @()
        foreach ($exe in @('python.exe', 'pythonw.exe')) {
            $procs += @(
                Get-CimInstance Win32_Process -Filter "Name = '$exe'" -ErrorAction SilentlyContinue
            )
        }
    } catch {
        return
    }
    foreach ($proc in $procs) {
        $cmd = [string]$proc.CommandLine
        if (-not $cmd) { continue }
        $hit = $false
        foreach ($re in $patterns) {
            if ($cmd -match $re) { $hit = $true; break }
        }
        if (-not $hit) { continue }
        try {
            Stop-Process -Id $proc.ProcessId -Force -ErrorAction Stop
            Write-Host "  Killed daemon python PID $($proc.ProcessId)"
        } catch {
            Write-Host "  Could not kill python PID $($proc.ProcessId): $($_.Exception.Message)"
        }
    }
}

Write-Host "Stopping listeners on ports $DashboardPort / $VitePort ..."
Stop-ListenersOnPort -Port $DashboardPort
Stop-ListenersOnPort -Port $VitePort
Write-Host "Stopping prior daemon python processes..."
Stop-DaemonPythons
Write-Host "Cleanup done."
exit 0
