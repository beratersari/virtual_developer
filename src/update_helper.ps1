# Replace a Yaver executable folder after the running process has exited.
# ASCII only. Called as: powershell -File update_helper.ps1 -PlanPath PLAN.json
param(
    [Parameter(Mandatory = $true)]
    [string]$PlanPath
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function Write-Log([string]$LogPath, [string]$Message) {
    if (-not $LogPath) { return }
    $line = (Get-Date).ToString("yyyy-MM-ddTHH:mm:ss") + " " + $Message
    Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8
}

function Test-Alive([int]$ProcId) {
    if ($ProcId -le 0) { return $false }
    return $null -ne (Get-Process -Id $ProcId -ErrorAction SilentlyContinue)
}

function Get-Descendants($ChildMap, [int]$RootId) {
    $found = @()
    $pending = @($RootId)
    $seen = @{}
    $seen[[string]$RootId] = $true
    while ($pending.Count -gt 0) {
        $next = @()
        foreach ($cur in $pending) {
            $kids = $ChildMap[[string]$cur]
            if (-not $kids) { continue }
            foreach ($kid in $kids) {
                $key = [string]$kid
                if ($seen.ContainsKey($key)) { continue }
                $seen[$key] = $true
                $found += [int]$kid
                $next += [int]$kid
            }
        }
        $pending = $next
    }
    return $found
}

function Stop-ForeignTree([int]$RootId) {
    # Stop-Process on one pid does not kill this helper. taskkill /T would:
    # this script is a child of yaver.exe, so /T ends the swap halfway.
    $selfId = $PID
    $map = @{}
    $parents = @{}
    try {
        $procs = @(Get-CimInstance -ClassName Win32_Process -ErrorAction Stop)
    } catch {
        $procs = @()
    }
    foreach ($proc in $procs) {
        $id = [int]$proc.ProcessId
        $parentId = [int]$proc.ParentProcessId
        $parents[[string]$id] = $parentId
        $parent = [string]$parentId
        if (-not $map.ContainsKey($parent)) { $map[$parent] = @() }
        $map[$parent] += $id
    }
    $targetTree = @{}
    $targetTree[[string]$RootId] = $true
    foreach ($id in @(Get-Descendants $map $RootId)) {
        $targetTree[[string]$id] = $true
    }
    $protected = @{}
    $protected[[string]$selfId] = $true
    # A descendant that is the new Yaver must still be stopped. Its cwd
    # keeps the install folder locked, so rollback cannot move it.
    foreach ($id in @(Get-Descendants $map $selfId)) {
        if (-not $targetTree.ContainsKey([string]$id)) {
            $protected[[string]$id] = $true
        }
    }
    # A launcher between this script and yaver.exe dies with this script.
    # Leave that launcher running. Still stop yaver.exe itself.
    $cursor = $selfId
    for ($n = 0; $n -lt 64; $n++) {
        $key = [string]$cursor
        if (-not $parents.ContainsKey($key)) { break }
        $parentId = [int]$parents[$key]
        if ($parentId -le 0 -or $parentId -eq $cursor) { break }
        if ($parentId -ne $RootId) { $protected[[string]$parentId] = $true }
        if ($parentId -eq $RootId) { break }
        $cursor = $parentId
    }
    $victims = @()
    foreach ($id in @(Get-Descendants $map $RootId)) {
        if (-not $protected.ContainsKey([string]$id)) { $victims += [int]$id }
    }
    for ($i = $victims.Count - 1; $i -ge 0; $i--) {
        Stop-Process -Id $victims[$i] -Force -ErrorAction SilentlyContinue
    }
    if (-not $protected.ContainsKey([string]$RootId)) {
        Stop-Process -Id $RootId -Force -ErrorAction SilentlyContinue
    }
}

function Get-EnvValue([byte[]]$Bytes, [string]$Name) {
    if ($null -eq $Bytes -or $Bytes.Length -eq 0) { return "" }
    $text = [System.Text.Encoding]::UTF8.GetString($Bytes)
    $found = ""
    foreach ($line in ($text -split "`n")) {
        $trim = $line.Trim().TrimEnd("`r")
        if (-not $trim -or $trim.StartsWith("#")) { continue }
        $eq = $trim.IndexOf("=")
        if ($eq -lt 1) { continue }
        $key = $trim.Substring(0, $eq).Trim()
        if ($key -ne $Name) { continue }
        $value = $trim.Substring($eq + 1).Trim()
        if ($value.Length -ge 2) {
            $quote = $value.Substring(0, 1)
            if (($quote -eq '"') -or ($quote -eq "'")) {
                if ($value.EndsWith($quote)) {
                    $value = $value.Substring(1, $value.Length - 2)
                }
            }
        }
        $found = $value
    }
    return $found
}

function Get-UserdataRel([string]$Install, [byte[]]$EnvBytes) {
    $raw = Get-EnvValue $EnvBytes "YAVER_BASE_DIR"
    if (-not $raw) { return "" }
    $root = [System.IO.Path]::GetFullPath($Install)
    if (-not $root.EndsWith("\")) { $root = $root + "\" }
    $candidate = $raw
    if (-not [System.IO.Path]::IsPathRooted($candidate)) {
        $candidate = Join-Path $Install $candidate
    }
    if (-not (Test-Path -LiteralPath $candidate -PathType Container)) { return "" }
    $item = Get-Item -LiteralPath $candidate -Force
    if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) { return "" }
    $full = [System.IO.Path]::GetFullPath($candidate)
    if (-not $full.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)) { return "" }
    $rel = $full.Substring($root.Length)
    if (-not $rel) { return "" }
    if ($rel -eq "_internal" -or $rel.StartsWith("_internal\")) { return "" }
    return $rel
}

function Move-Directory([string]$From, [string]$To) {
    $last = "Could not move the data folder."
    for ($try = 0; $try -lt 40; $try++) {
        try {
            [System.IO.Directory]::Move($From, $To)
            return
        } catch {
            $last = $_.Exception.Message
            Start-Sleep -Milliseconds 250
        }
    }
    throw $last
}

function Park-Userdata([string]$Install, [byte[]]$EnvBytes) {
    $rel = Get-UserdataRel $Install $EnvBytes
    if (-not $rel) { return "" }
    $parent = Split-Path -Parent $Install
    $leaf = Split-Path -Leaf $Install
    $stash = Join-Path $parent ($leaf + ".userdata")
    if (Test-Path -LiteralPath $stash) {
        throw "A previous update left the data folder beside Yaver. Move that folder back before updating again."
    }
    Move-Directory (Join-Path $Install $rel) $stash
    return $rel
}

function Restore-Userdata([string]$Install, [string]$Rel) {
    if (-not $Rel) { return }
    $parent = Split-Path -Parent $Install
    $leaf = Split-Path -Leaf $Install
    $stash = Join-Path $parent ($leaf + ".userdata")
    if (-not (Test-Path -LiteralPath $stash)) { return }
    $dest = Join-Path $Install $Rel
    $destParent = Split-Path -Parent $dest
    if ($destParent -and -not (Test-Path -LiteralPath $destParent)) {
        New-Item -ItemType Directory -Path $destParent -Force | Out-Null
    }
    if (Test-Path -LiteralPath $dest) {
        Remove-Item -LiteralPath $dest -Recurse -Force
    }
    Move-Directory $stash $dest
}

function Move-WithRetry([string]$From, [string]$NewName) {
    $last = "Could not move the install folder."
    for ($try = 0; $try -lt 40; $try++) {
        try {
            Rename-Item -LiteralPath $From -NewName $NewName -ErrorAction Stop
            return
        } catch {
            $last = $_.Exception.Message
            Start-Sleep -Milliseconds 250
        }
    }
    throw $last
}

function Test-PortOpen([int]$Port, [string]$ProbeHost) {
    if ($Port -le 0) { return $false }
    if (-not $ProbeHost) { $ProbeHost = "127.0.0.1" }
    # Connect() waits for the system TCP timeout, about 20 seconds, when the
    # address does not answer. One check would then use the whole health budget.
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $wait = $client.BeginConnect($ProbeHost, $Port, $null, $null)
        if (-not $wait.AsyncWaitHandle.WaitOne(1000, $false)) {
            return $false
        }
        $client.EndConnect($wait)
        return $true
    } catch {
        return $false
    } finally {
        try { $client.Close() } catch { }
    }
}

function Write-Result($Plan, [bool]$Ok, [string]$ErrorText) {
    $payload = @{
        ok = $Ok
        version = [string]$Plan.version
        error = $ErrorText
    } | ConvertTo-Json -Compress
    Set-Content -LiteralPath ([string]$Plan.result) -Value $payload -Encoding UTF8
}

$plan = Get-Content -LiteralPath $PlanPath -Raw -Encoding UTF8 | ConvertFrom-Json
$logPath = [string]$plan.log
$parkedRel = ""
try {
    if ([string]$plan.layout -ne "frozen") {
        throw "This helper only replaces an executable install."
    }
    $install = [System.IO.Path]::GetFullPath([string]$plan.install_root)
    $staging = [System.IO.Path]::GetFullPath([string]$plan.staging)
    if (-not $install -or (Split-Path -Parent $install) -eq $install) {
        throw "Refusing to replace a drive root."
    }
    if (Test-Path -LiteralPath (Join-Path $install ".git")) {
        throw "Refusing to replace a git checkout."
    }
    if (-not (Test-Path -LiteralPath $staging)) {
        throw "The staged release is missing."
    }
    $procId = 0
    try { $procId = [int]$plan.pid } catch { $procId = 0 }
    if ($procId -gt 0) {
        $deadline = (Get-Date).AddSeconds(90)
        while ((Get-Date) -lt $deadline -and (Test-Alive $procId)) {
            Start-Sleep -Milliseconds 400
        }
        if (Test-Alive $procId) {
            Write-Log $logPath "stopping the running Yaver"
            Stop-ForeignTree $procId
            Start-Sleep -Seconds 2
        }
        if (Test-Alive $procId) {
            throw "Yaver is still running."
        }
    }
    $port = 0
    try { $port = [int]$plan.port } catch { $port = 0 }
    $probe = "127.0.0.1"
    if ($plan.probe_host) { $probe = [string]$plan.probe_host }
    if ($port -gt 0) {
        $deadline = (Get-Date).AddSeconds(30)
        while ((Get-Date) -lt $deadline -and (Test-PortOpen $port $probe)) {
            Start-Sleep -Milliseconds 400
        }
        # An old listener still on this port would look like the new copy opened.
        if (Test-PortOpen $port $probe) {
            throw "The dashboard port is still open."
        }
    }
    $envFile = Join-Path $install ".env"
    $envBytes = $null
    if (Test-Path -LiteralPath $envFile) {
        $envBytes = [System.IO.File]::ReadAllBytes($envFile)
    }
    $parent = Split-Path -Parent $install
    $leaf = Split-Path -Leaf $install
    $parkedRel = Park-Userdata $install $envBytes
    $previous = Join-Path $parent ($leaf + ".previous")
    if (Test-Path -LiteralPath $previous) {
        Remove-Item -LiteralPath $previous -Recurse -Force
    }
    Move-WithRetry $install ($leaf + ".previous")
    try {
        Copy-Item -LiteralPath $staging -Destination $install -Recurse -Force
        if ($null -ne $envBytes) {
            [System.IO.File]::WriteAllBytes((Join-Path $install ".env"), $envBytes)
        }
        if ($parkedRel) {
            Restore-Userdata $install $parkedRel
            $parkedRel = ""
        }
    } catch {
        $broken = Join-Path $parent ($leaf + ".broken")
        if (Test-Path -LiteralPath $broken) {
            Remove-Item -LiteralPath $broken -Recurse -Force -ErrorAction SilentlyContinue
        }
        if (Test-Path -LiteralPath $install) {
            Rename-Item -LiteralPath $install -NewName ($leaf + ".broken") -ErrorAction SilentlyContinue
        }
        if (Test-Path -LiteralPath $previous) {
            Rename-Item -LiteralPath $previous -NewName $leaf -ErrorAction SilentlyContinue
        }
        if ($parkedRel) {
            Restore-Userdata $install $parkedRel
            $parkedRel = ""
        }
        throw
    }
    $argv = @($plan.argv)
    $exe = [string]$argv[0]
    $rest = @()
    if ($argv.Count -gt 1) {
        $rest = @($argv[1..($argv.Count - 1)])
    }
    $cwd = [string]$plan.cwd
    if (-not $cwd) { $cwd = $install }
    $env:PYINSTALLER_RESET_ENVIRONMENT = "1"
    Write-Log $logPath ("starting " + $exe)
    if ($rest.Count -gt 0) {
        $started = Start-Process -FilePath $exe -ArgumentList $rest -WorkingDirectory $cwd -WindowStyle Hidden -PassThru
    } else {
        $started = Start-Process -FilePath $exe -WorkingDirectory $cwd -WindowStyle Hidden -PassThru
    }
    $health = 45
    if ($null -ne $plan.health_seconds) {
        try { $health = [int]$plan.health_seconds } catch { $health = 45 }
    }
    if ($port -gt 0 -and $health -gt 0) {
        $opened = $false
        $deadline = (Get-Date).AddSeconds($health)
        while ((Get-Date) -lt $deadline) {
            if (Test-PortOpen $port $probe) { $opened = $true; break }
            Start-Sleep -Milliseconds 400
        }
        if (-not $opened) {
            if ($started -and -not $started.HasExited) {
                Stop-ForeignTree $started.Id
                $stopBy = (Get-Date).AddSeconds(10)
                while ((Get-Date) -lt $stopBy -and (Test-Alive $started.Id)) {
                    Start-Sleep -Milliseconds 200
                }
            }
            $liveEnv = $null
            $liveFile = Join-Path $install ".env"
            if (Test-Path -LiteralPath $liveFile) {
                $liveEnv = [System.IO.File]::ReadAllBytes($liveFile)
            }
            $parkedRel = Park-Userdata $install $liveEnv
            $broken = Join-Path $parent ($leaf + ".broken")
            if (Test-Path -LiteralPath $broken) {
                Remove-Item -LiteralPath $broken -Recurse -Force
            }
            if (Test-Path -LiteralPath $install) {
                Rename-Item -LiteralPath $install -NewName ($leaf + ".broken")
            }
            if (Test-Path -LiteralPath $previous) {
                Rename-Item -LiteralPath $previous -NewName $leaf
            }
            if ($parkedRel) {
                Restore-Userdata $install $parkedRel
                $parkedRel = ""
            }
            # The previous folder is back. Start it again so the dashboard
            # returns. The Python helper does this too.
            Write-Log $logPath ("starting restored " + $exe)
            if ($rest.Count -gt 0) {
                Start-Process -FilePath $exe -ArgumentList $rest -WorkingDirectory $cwd -WindowStyle Hidden | Out-Null
            } else {
                Start-Process -FilePath $exe -WorkingDirectory $cwd -WindowStyle Hidden | Out-Null
            }
            throw "The new Yaver did not open. The previous copy was restored."
        }
    }
    Write-Result $plan $true ""
    Write-Log $logPath ("started " + [string]$plan.version)
    exit 0
} catch {
    $message = $_.Exception.Message
    if ($parkedRel -and $install) {
        try { Restore-Userdata $install $parkedRel } catch { }
    }
    Write-Log $logPath $message
    try { Write-Result $plan $false $message } catch { }
    exit 1
}
