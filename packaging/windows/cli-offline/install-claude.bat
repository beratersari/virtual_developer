@echo off
REM Offline Claude Code CLI only. Copies claude.exe and settings.json.
REM Does not copy agents or skills. Use install-agents.bat from the Yaver zip for those.
REM Edit claude\settings.json (YOUR_HOST) before running. Re-running replaces the config.

setlocal
set "SCRIPT_DIR=%~dp0"
set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
set "SRC_EXE=%SCRIPT_DIR%\claude\claude.exe"
set "SRC_CFG=%SCRIPT_DIR%\claude\settings.json"
set "DEST_BIN=%USERPROFILE%\.local\bin"
set "DEST_EXE=%DEST_BIN%\claude.exe"
set "DEST_DIR=%USERPROFILE%\.claude"
set "DEST_CFG=%DEST_DIR%\settings.json"

if not exist "%SRC_EXE%" (
    echo [ERROR] Missing claude\claude.exe in this folder.
    call :maybe_pause
    exit /b 1
)
if not exist "%SRC_CFG%" (
    echo [ERROR] Missing claude\settings.json in this folder.
    call :maybe_pause
    exit /b 1
)

if not exist "%DEST_BIN%" mkdir "%DEST_BIN%"
if not exist "%DEST_DIR%" mkdir "%DEST_DIR%"
copy /Y "%SRC_EXE%" "%DEST_EXE%" >nul
if errorlevel 1 (
    echo [ERROR] Could not copy claude.exe
    call :maybe_pause
    exit /b 1
)
copy /Y "%SRC_CFG%" "%DEST_CFG%" >nul
if errorlevel 1 (
    echo [ERROR] Could not copy settings.json
    call :maybe_pause
    exit /b 1
)

powershell -NoProfile -Command "$d=$env:USERPROFILE + '\.local\bin'; $p=[Environment]::GetEnvironmentVariable('Path','User'); if (-not $p) { $p='' }; $parts=@($p -split ';' | Where-Object { $_ -and ($_ -ne $d) }); $parts += $d; [Environment]::SetEnvironmentVariable('Path', ($parts -join ';'), 'User')"
if errorlevel 1 (
    echo [ERROR] Could not update user PATH
    call :maybe_pause
    exit /b 1
)

echo [OK] Claude Code CLI copied to %DEST_EXE%
echo [OK] Config copied to %DEST_CFG%
echo Edit YOUR_HOST in claude\settings.json and run this bat again to replace the config.
echo Agents are not copied. Use install-agents.bat from the Yaver zip.
echo Open a new terminal so PATH includes %DEST_BIN%
call :maybe_pause
exit /b 0

:maybe_pause
if /i "%VD_NONINTERACTIVE%"=="1" exit /b 0
pause
exit /b 0
