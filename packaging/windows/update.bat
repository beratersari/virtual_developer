@echo off
rem Update Yaver in this folder. Run this file yourself.
rem It stops yaver.exe, then replaces the program files.
rem .env and this file stay as they are. Start yaver.exe when this window finishes.
setlocal
cd /d "%~dp0"
set "YAVER_UPDATE_ROOT=%cd%"
set "YAVER_UPDATE_BAT=%~f0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$t = Get-Content -LiteralPath $env:YAVER_UPDATE_BAT -Raw; $m = 'YAVER_UPDATE_BODY'; $i = $t.LastIndexOf($m); if ($i -lt 0) { exit 2 }; iex $t.Substring($i + $m.Length)"
exit /b %ERRORLEVEL%
YAVER_UPDATE_BODY
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$root = $env:YAVER_UPDATE_ROOT
$failed = $false
$work = ''

function Say([string]$text) {
    [Console]::Out.WriteLine($text)
}
function Version-Id([string]$text) {
    $trimmed = $text.Trim()
    $plus = $trimmed.IndexOf('+')
    if ($plus -ge 0) { $trimmed = $trimmed.Substring(0, $plus) }
    return $trimmed.Trim()
}
function Read-EnvValue([string]$path, [string]$name) {
    $found = ''
    foreach ($line in @(Get-Content -LiteralPath $path)) {
        $text = $line.Trim()
        if ($text -eq '' -or $text.StartsWith('#')) { continue }
        if ($text.StartsWith('export ')) { $text = $text.Substring(7).Trim() }
        $eq = $text.IndexOf('=')
        if ($eq -lt 1) { continue }
        $key = $text.Substring(0, $eq).Trim()
        if ($key -ne $name) { continue }
        $found = $text.Substring($eq + 1).Trim().Trim('"').Trim("'")
    }
    return $found
}
function Remove-Tree([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) { return }
    $item = Get-Item -LiteralPath $path -Force
    if ($item.PSIsContainer -and -not $item.Attributes.ToString().Contains('ReparsePoint')) {
        Remove-Item -LiteralPath $path -Recurse -Force
    } else {
        Remove-Item -LiteralPath $path -Force
    }
}
function Restore-Moved($moved, $created) {
    for ($i = $created.Count - 1; $i -ge 0; $i--) {
        Remove-Tree $created[$i]
    }
    for ($i = $moved.Count - 1; $i -ge 0; $i--) {
        $pair = $moved[$i]
        $dest = $pair[0]
        $hold = $pair[1]
        Remove-Tree $dest
        if (Test-Path -LiteralPath $hold) {
            Move-Item -LiteralPath $hold -Destination $dest
        }
    }
}
function Save-Release([string]$url, [string]$dest, [string]$message) {
    $code = & curl.exe --silent --show-error --connect-timeout 15 --max-time 600 --max-redirs 0 --noproxy "*" --output $dest --write-out "%{http_code}" $url
    $code = ([string]$code).Trim()
    if ($LASTEXITCODE -ne 0 -or $code -ne '200') { throw $message }
}
function Assert-ArchiveSafe([string]$zipPath) {
    $names = & tar.exe -tf $zipPath
    if ($LASTEXITCODE -ne 0) { throw 'Could not open the package.' }
    foreach ($raw in @($names)) {
        $name = ([string]$raw).Trim().Replace('\', '/')
        if ($name.StartsWith('./')) { $name = $name.Substring(2) }
        if ($name -eq '') { continue }
        if ($name.StartsWith('/') -or $name -match '^[A-Za-z]:') {
            throw 'The package contains an unsafe path.'
        }
        foreach ($part in $name.Split('/')) {
            if ($part -eq '..') { throw 'The package contains an unsafe path.' }
        }
    }
}
function Get-OurYaver([string]$rootPrefix) {
    @(Get-CimInstance Win32_Process -Filter "Name = 'yaver.exe'" | Where-Object {
        $exePath = [string]$_.ExecutablePath
        if (-not $exePath) { return $false }
        try { $full = [IO.Path]::GetFullPath($exePath) } catch { return $false }
        return $full.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)
    })
}
try {
    if (-not $root) { throw 'Could not find this folder.' }
    $root = [IO.Path]::GetFullPath($root)
    if ($root -match '^[A-Za-z]:\\$') { throw 'Refusing to update a drive root.' }
    if (Test-Path -LiteralPath (Join-Path $root '.git')) {
        throw 'This folder is a git checkout. Update replaces an installed copy.'
    }
    $envFile = Join-Path $root '.env'
    if (-not (Test-Path -LiteralPath $envFile)) {
        throw '.env is missing. Copy .env.example to .env and set RELEASE_HOST and RELEASE_PORT.'
    }
    if (-not (Test-Path -LiteralPath (Join-Path $root 'yaver.exe')) -or -not (Test-Path -LiteralPath (Join-Path $root '_internal'))) {
        throw 'This folder is not a Yaver executable install.'
    }
    $relHost = Read-EnvValue $envFile 'RELEASE_HOST'
    $relPort = Read-EnvValue $envFile 'RELEASE_PORT'
    if ($relHost -notmatch '^[A-Za-z0-9][A-Za-z0-9.-]*$') {
        throw 'Set RELEASE_HOST in .env to an IP or a hostname.'
    }
    if ($relPort -notmatch '^[1-9][0-9]{0,4}$' -or [int]$relPort -gt 65535) {
        throw 'Set RELEASE_PORT in .env to a port from 1 to 65535.'
    }
    Say "Release server ${relHost}:${relPort}"
    $work = Join-Path $env:TEMP ("yaver-update-" + [Guid]::NewGuid().ToString('n'))
    New-Item -ItemType Directory -Path $work | Out-Null
    $meta = Join-Path $work 'latest.json'
    $zip = Join-Path $work 'package.zip'
    $stage = Join-Path $work 'stage'
    $latestUrl = "http://${relHost}:${relPort}/api/latest?platform=windows"
    Save-Release $latestUrl $meta 'Could not reach the release server.'
    $remote = Get-Content -LiteralPath $meta -Raw | ConvertFrom-Json
    $version = [string]$remote.version
    $sha = ([string]$remote.sha256).ToLower()
    if ($version -notmatch '^[0-9A-Za-z][0-9A-Za-z.+-]{0,63}$') {
        throw 'The release server returned an unexpected version.'
    }
    if ($sha -notmatch '^[0-9a-f]{64}$') {
        throw 'The release server returned an unexpected checksum.'
    }
    $current = $false
    $versionFile = Join-Path $root 'VERSION'
    if (Test-Path -LiteralPath $versionFile) {
        $localId = Version-Id (Get-Content -LiteralPath $versionFile -Raw)
        if ($localId -and $localId -eq (Version-Id $version)) {
            Say "This install is already $version."
            $current = $true
        }
    }
    if (-not $current) {
        Say "Downloading $version."
        $downloadUrl = "http://${relHost}:${relPort}/download/windows"
        Save-Release $downloadUrl $zip 'Could not download the package.'
        $actual = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLower()
        if ($actual -ne $sha) { throw 'The package checksum does not match.' }
        Say 'Checking the package.'
        Assert-ArchiveSafe $zip
        New-Item -ItemType Directory -Path $stage | Out-Null
        & tar.exe -xf $zip -C $stage
        if ($LASTEXITCODE -ne 0) { throw 'Could not open the package.' }
        $entries = @(Get-ChildItem -LiteralPath $stage -Force)
        $dirs = @($entries | Where-Object { $_.PSIsContainer })
        $files = @($entries | Where-Object { -not $_.PSIsContainer })
        if ($dirs.Count -eq 1 -and $files.Count -eq 0) {
            $inner = $dirs[0].FullName
            if ((Test-Path -LiteralPath (Join-Path $inner 'yaver.exe')) -and (Test-Path -LiteralPath (Join-Path $inner '_internal'))) {
                $stage = $inner
            }
        }
        if (-not (Test-Path -LiteralPath (Join-Path $stage 'yaver.exe')) -or -not (Test-Path -LiteralPath (Join-Path $stage '_internal'))) {
            throw 'The published package is not a Yaver executable.'
        }
        Say 'Stopping Yaver.'
        $rootPrefix = $root
        if (-not $rootPrefix.EndsWith('\')) { $rootPrefix = $rootPrefix + '\' }
        foreach ($proc in @(Get-OurYaver $rootPrefix)) {
            Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
            Say "Stopped yaver.exe pid=$($proc.ProcessId)"
        }
        $deadline = (Get-Date).AddSeconds(20)
        do {
            $alive = @(Get-OurYaver $rootPrefix)
            if ($alive.Count -eq 0) { break }
            Start-Sleep -Milliseconds 400
        } while ((Get-Date) -lt $deadline)
        if ($alive.Count -gt 0) { throw 'Yaver is still running. Close it and run update.bat again.' }
        Say 'Replacing the files.'
        $moved = New-Object System.Collections.Generic.List[object]
        $created = New-Object System.Collections.Generic.List[string]
        try {
            foreach ($child in @(Get-ChildItem -LiteralPath $stage -Force)) {
                $name = $child.Name
                if ($name -notmatch '^[A-Za-z0-9._-]+$') {
                    Say "skipped $name"
                    continue
                }
                $dest = Join-Path $root $name
                if ($name -ieq '.env') {
                    if (Test-Path -LiteralPath $dest) {
                        Say 'left .env in place'
                    } else {
                        Copy-Item -LiteralPath $child.FullName -Destination $dest -Force
                        Say 'copied .env'
                    }
                    continue
                }
                if ($name -ieq 'update.bat') {
                    Say 'left update.bat in place'
                    continue
                }
                $hold = Join-Path $work ("hold-" + $name)
                Remove-Tree $hold
                if (Test-Path -LiteralPath $dest) {
                    Move-Item -LiteralPath $dest -Destination $hold
                    $moved.Add(@($dest, $hold))
                } else {
                    $created.Add($dest)
                }
                Copy-Item -LiteralPath $child.FullName -Destination $dest -Recurse -Force
                Say "copied $name"
            }
            foreach ($row in $moved) { Remove-Tree $row[1] }
        } catch {
            Restore-Moved $moved $created
            throw
        }
        Say "Updated to $version. Start yaver.exe when you want."
    }
} catch {
    $failed = $true
    $message = $_.Exception.Message
    if (-not $message) { $message = 'The update failed.' }
    Say $message
} finally {
    if ($work -and (Test-Path -LiteralPath $work)) {
        Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
    }
}
if ($failed) { exit 1 }
exit 0
