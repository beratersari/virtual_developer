#Requires -Version 5.1
# Stop VD processes on Windows: free ports and optionally kill the backend daemon.
# start-frontend must call without -KillDaemon so the backend keeps running.
# Keep this file ASCII-only. Avoid double-quotes inside single-quoted regexes.

[CmdletBinding()]
param(
    [int]$DashboardPort = 8080,
    [int]$VitePort = 5173,
    [switch]$KillDaemon
)

$ErrorActionPreference = "Continue"

function Test-LooksLikeDaemon([string]$CmdLine) {
    if (-not $CmdLine) { return $false }
    if ($CmdLine -match 'serve_frontend\.py') { return $false }
    if ($CmdLine -match 'src\.daemon') { return $true }
    if ($CmdLine -match 'src/daemon') { return $true }
    if ($CmdLine -match '-m\s+src\.daemon') { return $true }
    # cli.py start (no nested quotes in the pattern)
    if (($CmdLine -match 'cli\.py') -and ($CmdLine -match '\s+start(\s|$)')) { return $true }
    return $false
}

function Stop-ListenersOnPort([int]$Port) {
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
                        Write-Host "  Could not kill PID $procId"
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
            $name = "?"
            if ($p) { $name = $p.ProcessName }

            # When only clearing the frontend port, never kill the backend daemon
            if ((-not $KillDaemon) -and ($Port -eq $VitePort)) {
                $cmd = ""
                try {
                    $wmi = Get-CimInstance Win32_Process -Filter "ProcessId = $procId" -ErrorAction SilentlyContinue
                    if ($wmi) { $cmd = [string]$wmi.CommandLine }
                } catch {
                    $cmd = ""
                }
                if (Test-LooksLikeDaemon $cmd) {
                    Write-Host "  Skip PID $procId ($name) - backend daemon"
                    continue
                }
            }

            Stop-Process -Id $procId -Force -ErrorAction Stop
            Write-Host "  Killed PID $procId ($name) on port $Port"
        } catch {
            Write-Host "  Could not kill PID $procId on port $Port"
        }
    }
}

function Stop-DaemonPythons {
    try {
        $procs = @()
        foreach ($exe in @("python.exe", "pythonw.exe")) {
            $filter = "Name = '$exe'"
            $procs += @(Get-CimInstance Win32_Process -Filter $filter -ErrorAction SilentlyContinue)
        }
    } catch {
        return
    }

    foreach ($proc in $procs) {
        $cmd = [string]$proc.CommandLine
        if (-not (Test-LooksLikeDaemon $cmd)) { continue }
        try {
            Stop-Process -Id $proc.ProcessId -Force -ErrorAction Stop
            Write-Host "  Killed daemon python PID $($proc.ProcessId)"
        } catch {
            Write-Host "  Could not kill python PID $($proc.ProcessId)"
        }
    }
}

Write-Host "Stopping ports Dashboard=$DashboardPort Frontend=$VitePort KillDaemon=$KillDaemon"
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
