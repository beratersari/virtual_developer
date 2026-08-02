#Requires -Version 5.1
<#
.SYNOPSIS
  Stop previous Virtual Developer processes on Windows (ports and/or daemon).

.DESCRIPTION
  Frees dashboard (8080) and/or frontend (5173) ports.
  Optionally kills python processes that look like the product daemon.

  IMPORTANT: start-frontend.bat must NOT pass -KillDaemon, or it will stop
  the backend that is already running.
#>
[CmdletBinding()]
param(
    [int]$DashboardPort = 8080,
    [int]$VitePort = 5173,
    # Only start-backend / full restart should kill the daemon process tree
    [switch]$KillDaemon
)

$ErrorActionPreference = "Continue"

function Stop-ListenersOnPort([int]$Port) {
    # Port 0 means "skip this port"
    if ($Port -le 0) { return }
    $conns = @()
    try {
        $conns = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    } catch {
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
            # Never kill the backend daemon when only freeing the frontend port
            if (-not $KillDaemon -and $Port -eq $VitePort) {
                $cmd = ""
                try {
                    $cmd = [string](Get-CimInstance Win32_Process -Filter "ProcessId = $procId" -ErrorAction SilentlyContinue).CommandLine
                } catch { }
                if ($cmd -match 'src\.daemon|cli\.py.*start|-m\s+src\.daemon') {
                    Write-Host "  Skip PID $procId ($name) — looks like backend daemon"
                    continue
                }
            }
            Stop-Process -Id $procId -Force -ErrorAction Stop
            Write-Host "  Killed PID $procId ($name) listening on $Port"
        } catch {
            Write-Host "  Could not kill PID $procId on $Port : $($_.Exception.Message)"
        }
    }
}

function Stop-DaemonPythons {
    # Single-quoted patterns only (double-quoted \" breaks PowerShell parsing).
    # Do NOT match serve_frontend.py — that is the UI process.
    $patterns = @(
        'src\.daemon',
        'src/daemon',
        'cli\.py(\s+|").*start',
        '-m\s+src\.daemon'
    )
    try {
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
        # Never treat the frontend helper as the daemon
        if ($cmd -match 'serve_frontend\.py') { continue }
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

Write-Host "Stopping listeners on ports Dashboard=$DashboardPort Frontend=$VitePort (KillDaemon=$KillDaemon) ..."
Stop-ListenersOnPort -Port $DashboardPort
Stop-ListenersOnPort -Port $VitePort
if ($KillDaemon) {
    Write-Host "Stopping prior daemon python processes..."
    Stop-DaemonPythons
} else {
    Write-Host "Leaving backend daemon running (KillDaemon not set)."
}
Write-Host "Cleanup done."
exit 0
