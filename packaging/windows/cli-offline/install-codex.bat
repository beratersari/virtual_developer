@echo off
REM Offline Codex CLI only. Copies codex.exe and config.toml.
REM Does not copy agents. Edit codex\config.toml (YOUR_HOST) before running.

setlocal
set "SCRIPT_DIR=%~dp0"
set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
set "SRC_EXE=%SCRIPT_DIR%\codex\codex.exe"
set "SRC_CFG=%SCRIPT_DIR%\codex\config.toml"
set "DEST_BIN=%LOCALAPPDATA%\Programs\OpenAI\Codex\bin"
set "DEST_EXE=%DEST_BIN%\codex.exe"
set "DEST_DIR=%USERPROFILE%\.codex"
set "DEST_CFG=%DEST_DIR%\config.toml"

if not exist "%SRC_EXE%" (
    echo [ERROR] Missing codex\codex.exe in this folder.
    call :maybe_pause
    exit /b 1
)
if not exist "%SRC_CFG%" (
    echo [ERROR] Missing codex\config.toml in this folder.
    call :maybe_pause
    exit /b 1
)

if not exist "%DEST_DIR%" mkdir "%DEST_DIR%"
set "INSTALLED="
for /f "usebackq delims=" %%I in (`powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%\Backup-CliBinary.ps1" -Source "%SRC_EXE%" -Name "codex.exe" -Fallback "%DEST_EXE%"`) do set "INSTALLED=%%I"
if not defined INSTALLED (
    echo [ERROR] Could not install codex.exe
    call :maybe_pause
    exit /b 1
)
for %%D in ("%INSTALLED%") do set "DEST_BIN=%%~dpD"
set "DEST_BIN=%DEST_BIN:~0,-1%"
set "DEST_EXE=%INSTALLED%"
copy /Y "%SRC_CFG%" "%DEST_CFG%" >nul
if errorlevel 1 (
    echo [ERROR] Could not copy config.toml
    call :maybe_pause
    exit /b 1
)

powershell -NoProfile -Command "$d='%DEST_BIN%'; $p=[Environment]::GetEnvironmentVariable('Path','User'); if (-not $p) { $p='' }; $parts=@($p -split ';' | Where-Object { $_ -and ($_ -ne $d) }); $parts += $d; [Environment]::SetEnvironmentVariable('Path', ($parts -join ';'), 'User')"
if errorlevel 1 (
    echo [ERROR] Could not update user PATH
    call :maybe_pause
    exit /b 1
)

echo [OK] Codex CLI copied to %DEST_EXE%
echo [OK] Config copied to %DEST_CFG%
echo Set CUSTOM_HOST_TOKEN to the token for YOUR_HOST, then open a new terminal.
call :maybe_pause
exit /b 0

:maybe_pause
if /i "%VD_NONINTERACTIVE%"=="1" exit /b 0
pause
exit /b 0
