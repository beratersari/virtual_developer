@echo off
REM =============================================================================
REM Virtual Developer - restart OpenCode headless server
REM IMPORTANT: never use unescaped "->" in echo lines (cmd redirect).
REM =============================================================================

setlocal EnableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
cd /d "%SCRIPT_DIR%"

REM Helpers live under packaging\windows when bat is at product root (after install)
set "PKG_WIN=%SCRIPT_DIR%\packaging\windows"
if not exist "%PKG_WIN%\Wait-Http.ps1" set "PKG_WIN=%SCRIPT_DIR%"
if not exist "%PKG_WIN%\Wait-Http.ps1" (
    if exist "%SCRIPT_DIR%\..\windows\Wait-Http.ps1" set "PKG_WIN=%SCRIPT_DIR%\..\windows"
)

REM Defaults (override via env or .env OPENCODE_SERVE_URL / OPENCODE_SERVE_PORT)
set "SERVE_PORT=4096"
set "SERVE_HOST=127.0.0.1"
if defined OPENCODE_SERVE_PORT set "SERVE_PORT=%OPENCODE_SERVE_PORT%"
if defined OPENCODE_SERVE_HOST set "SERVE_HOST=%OPENCODE_SERVE_HOST%"

REM Parse OPENCODE_SERVE_URL=http://host:port from .env when present
if exist "%SCRIPT_DIR%\.env" (
    for /f "usebackq tokens=1,* delims==" %%A in (`findstr /b /i /c:"OPENCODE_SERVE_URL=" "%SCRIPT_DIR%\.env"`) do (
        set "SERVE_URL_RAW=%%B"
    )
)
if defined OPENCODE_SERVE_URL set "SERVE_URL_RAW=%OPENCODE_SERVE_URL%"
if defined SERVE_URL_RAW (
    REM Strip quotes/spaces
    set "SERVE_URL_RAW=!SERVE_URL_RAW:"=!"
    set "SERVE_URL_RAW=!SERVE_URL_RAW: =!"
    REM Very small parser: http://host:port  or  http://host:port/
    for /f "tokens=1,2 delims=/" %%H in ("!SERVE_URL_RAW:http://=!") do (
        set "HOSTPORT=%%H"
    )
    for /f "tokens=1,2 delims=/" %%H in ("!SERVE_URL_RAW:https://=!") do (
        if not defined HOSTPORT set "HOSTPORT=%%H"
    )
    if defined HOSTPORT (
        for /f "tokens=1,2 delims=:" %%A in ("!HOSTPORT!") do (
            if not "%%A"=="" set "SERVE_HOST=%%A"
            if not "%%B"=="" set "SERVE_PORT=%%B"
        )
    )
)

set "OPENCODE_EXE=%USERPROFILE%\.opencode\bin\opencode.exe"
set "PATH=%USERPROFILE%\.opencode\bin;%PATH%"
set "OPENCODE_DISABLE_MODELS_FETCH=1"

echo ========================================
echo   Virtual Developer - OpenCode Serve
echo ========================================
echo Project : %SCRIPT_DIR%
echo Listen  : http://%SERVE_HOST%:%SERVE_PORT%/
echo Health  : http://127.0.0.1:%SERVE_PORT%/global/health
echo.

if not exist "%OPENCODE_EXE%" (
    where opencode >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] OpenCode not installed.
        echo Run install-backends.bat ^(offline^) or install-opencode-online.bat.
        call :maybe_pause
        exit /b 1
    )
    for /f "delims=" %%P in ('where opencode') do (
        set "OPENCODE_EXE=%%P"
        goto :have_oc
    )
)
:have_oc

if not exist "%PKG_WIN%\Wait-Http.ps1" (
    echo [ERROR] Wait-Http.ps1 not found under packaging\windows.
    call :maybe_pause
    exit /b 1
)

echo Stopping previous OpenCode serve on port %SERVE_PORT% ^(backend/frontend untouched^)...
if exist "%PKG_WIN%\Stop-VdProcesses.ps1" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%PKG_WIN%\Stop-VdProcesses.ps1" -DashboardPort %SERVE_PORT% -VitePort 0
) else (
    REM Fallback: kill listeners on SERVE_PORT via netstat
    for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":%SERVE_PORT% " ^| findstr LISTENING') do (
        echo   Killing PID %%P on port %SERVE_PORT%
        taskkill /F /PID %%P >nul 2>&1
    )
)
timeout /t 1 /nobreak >nul

echo Starting OpenCode serve in window "VD-OpenCode-Serve"...
REM Prefer PATH entry under %%USERPROFILE%%\.opencode\bin (same as start-opencode.bat)
start "VD-OpenCode-Serve" /D "%SCRIPT_DIR%" cmd /c "set OPENCODE_DISABLE_MODELS_FETCH=1&& set PATH=%USERPROFILE%\.opencode\bin;%PATH%&& opencode serve --port %SERVE_PORT% --hostname %SERVE_HOST% --print-logs --log-level INFO & echo. & echo OpenCode serve exited. & pause"
echo Waiting for http://127.0.0.1:%SERVE_PORT%/global/health ...
powershell -NoProfile -ExecutionPolicy Bypass -File "%PKG_WIN%\Wait-Http.ps1" -Url "http://127.0.0.1:%SERVE_PORT%/global/health" -TimeoutSec 60 -OkPattern "healthy"
if errorlevel 1 (
    echo [ERROR] OpenCode serve did not become ready on port %SERVE_PORT%.
    echo Open the "VD-OpenCode-Serve" window and check the log.
    echo Also: %%USERPROFILE%%\.local\share\opencode\log
    call :maybe_pause
    exit /b 1
)

echo.
echo [OK] OpenCode serve is up.
echo   Health : http://127.0.0.1:%SERVE_PORT%/global/health
echo.
call :maybe_pause
exit /b 0

:maybe_pause
if /i "%VD_NONINTERACTIVE%"=="1" exit /b 0
pause
exit /b 0
