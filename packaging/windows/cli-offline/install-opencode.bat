@echo off
REM Offline OpenCode CLI only. Copies opencode.exe and opencode.json.
REM Does not copy agents or skills. Use install-agents.bat from the Yaver zip for those.
REM Edit opencode\opencode.json (YOUR_HOST) before running. Re-running replaces the config.

setlocal
set "SCRIPT_DIR=%~dp0"
set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
set "SRC_EXE=%SCRIPT_DIR%\opencode\opencode.exe"
set "SRC_CFG=%SCRIPT_DIR%\opencode\opencode.json"
set "DEST_BIN=%USERPROFILE%\.opencode\bin"
set "DEST_EXE=%DEST_BIN%\opencode.exe"
set "DEST_CFG=%USERPROFILE%\.opencode\opencode.json"

if not exist "%SRC_EXE%" (
    echo [ERROR] Missing opencode\opencode.exe in this folder.
    call :maybe_pause
    exit /b 1
)
if not exist "%SRC_CFG%" (
    echo [ERROR] Missing opencode\opencode.json in this folder.
    call :maybe_pause
    exit /b 1
)

set "INSTALLED="
for /f "usebackq delims=" %%I in (`powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%\Backup-CliBinary.ps1" -Source "%SRC_EXE%" -Name "opencode.exe" -Fallback "%DEST_EXE%"`) do set "INSTALLED=%%I"
if not defined INSTALLED (
    echo [ERROR] Could not install opencode.exe
    call :maybe_pause
    exit /b 1
)
for %%D in ("%INSTALLED%") do set "DEST_BIN=%%~dpD"
set "DEST_BIN=%DEST_BIN:~0,-1%"
set "DEST_EXE=%INSTALLED%"
copy /Y "%SRC_CFG%" "%DEST_CFG%" >nul
if errorlevel 1 (
    echo [ERROR] Could not copy opencode.json
    call :maybe_pause
    exit /b 1
)

powershell -NoProfile -Command "[Environment]::SetEnvironmentVariable('OPENCODE_DISABLE_MODELS_FETCH','1','User'); $d='%DEST_BIN%'; $p=[Environment]::GetEnvironmentVariable('Path','User'); if (-not $p) { $p='' }; $parts=@($p -split ';' | Where-Object { $_ -and ($_ -ne $d) }); $parts += $d; [Environment]::SetEnvironmentVariable('Path', ($parts -join ';'), 'User')"
if errorlevel 1 (
    echo [ERROR] Could not update user PATH
    call :maybe_pause
    exit /b 1
)

echo [OK] OpenCode CLI copied to %DEST_EXE%
echo [OK] Config copied to %DEST_CFG%
echo Edit YOUR_HOST in opencode\opencode.json and run this bat again to replace the config.
echo Agents are not copied. Use install-agents.bat from the Yaver zip.
echo Open a new terminal so PATH includes %DEST_BIN%
call :maybe_pause
exit /b 0

:maybe_pause
if /i "%VD_NONINTERACTIVE%"=="1" exit /b 0
pause
exit /b 0
