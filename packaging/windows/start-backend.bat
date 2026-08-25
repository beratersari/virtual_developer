@echo off
REM =============================================================================
REM Virtual Developer - start BACKEND only (daemon + API on :8080)
REM Ops SPA is ALSO served by the daemon at http://0.0.0.0:8080/ when web\dist exists.
REM For a separate UI process on :5173, use start-frontend.bat after this.
REM IMPORTANT: never use unescaped "->" in echo lines (cmd redirect).
REM =============================================================================

setlocal EnableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
cd /d "%SCRIPT_DIR%"

set "DASH_PORT=8080"
set "FRONTEND_PORT=5173"
set "SERVE_PORT=4096"
set "SERVE_HOST=127.0.0.1"

set "PKG_WIN=%SCRIPT_DIR%\packaging\windows"
if not exist "%PKG_WIN%\Wait-Http.ps1" set "PKG_WIN=%SCRIPT_DIR%"
if not exist "%PKG_WIN%\Wait-Http.ps1" (
    if exist "%SCRIPT_DIR%\..\windows\Wait-Http.ps1" set "PKG_WIN=%SCRIPT_DIR%\..\windows"
)
set "VENV_PY=%SCRIPT_DIR%\.venv\Scripts\python.exe"
set "VD_PY="
if exist "%VENV_PY%" (
    set "VD_PY=%VENV_PY%"
) else (
    where python >nul 2>&1
    if not errorlevel 1 set "VD_PY=python"
)

REM Bind all interfaces (LAN access). Browser: http://127.0.0.1:8080/ or http://THIS-PC:8080/
set "DASHBOARD_HOST=0.0.0.0"
set "DASHBOARD_ALLOW_REMOTE=true"
set "DASHBOARD_PORT=%DASH_PORT%"
set "DASHBOARD_ENABLED=true"
set "VD_WEB_DIST=%SCRIPT_DIR%\web\dist"
REM Unattended git: never open Git Credential Manager / username-password GUI
set "GIT_TERMINAL_PROMPT=0"
set "GCM_INTERACTIVE=never"
set "GCM_MODAL_PROMPT=false"
set "GCM_GUI_PROMPT=false"

echo ========================================
echo   Virtual Developer - Backend
echo ========================================
echo Project : %SCRIPT_DIR%
echo API+SPA : http://0.0.0.0:%DASH_PORT%/  ^(open http://127.0.0.1:%DASH_PORT%/ ^)
echo.

if not defined VD_PY (
    echo [ERROR] No project .venv and python is not on PATH.
    echo Run install-dashboard.bat ^(creates .venv^) or
    echo install-dashboard-system-python.bat ^(uses system python^).
    call :maybe_pause
    exit /b 1
)
echo Python  : %VD_PY%

REM Official Codex CLI path (same as chatgpt.com/codex/install.ps1).
if not defined LOCALAPPDATA set "LOCALAPPDATA=%USERPROFILE%\AppData\Local"
set "CODEX_BIN=%LOCALAPPDATA%\Programs\OpenAI\Codex\bin"
if exist "%CODEX_BIN%\codex.exe" (
    set "PATH=%CODEX_BIN%;%PATH%"
    echo Codex   : %CODEX_BIN%\codex.exe
) else if exist "%SCRIPT_DIR%\vendor\bin\codex.exe" (
    set "PATH=%SCRIPT_DIR%\vendor\bin;%PATH%"
    echo Codex   : %SCRIPT_DIR%\vendor\bin\codex.exe
)

if not exist "%SCRIPT_DIR%\.env" (
    if exist "%SCRIPT_DIR%\.env.example" (
        copy /Y "%SCRIPT_DIR%\.env.example" "%SCRIPT_DIR%\.env" >nul
        echo [OK] Created .env from .env.example
    )
)

if not exist "%VD_WEB_DIST%\index.html" (
    echo [WARNING] web\dist\index.html missing - API will run but UI on :8080 will be JSON only.
    echo           Prefer a CI zip that includes web\dist, or use start-frontend after building SPA.
)

if not exist "%PKG_WIN%\Ensure-OpencodeServe.ps1" (
    echo [ERROR] Ensure-OpencodeServe.ps1 not found.
    call :maybe_pause
    exit /b 1
)

REM Read OPENCODE_SERVE_URL from env or .env (same rules as start-opencode-serve.bat)
if defined OPENCODE_SERVE_PORT set "SERVE_PORT=%OPENCODE_SERVE_PORT%"
if defined OPENCODE_SERVE_HOST set "SERVE_HOST=%OPENCODE_SERVE_HOST%"
if exist "%SCRIPT_DIR%\.env" (
    for /f "usebackq tokens=1,* delims==" %%A in (`findstr /b /i /c:"OPENCODE_SERVE_URL=" "%SCRIPT_DIR%\.env"`) do (
        set "SERVE_URL_RAW=%%B"
    )
)
if defined OPENCODE_SERVE_URL set "SERVE_URL_RAW=%OPENCODE_SERVE_URL%"
if defined SERVE_URL_RAW (
    set "SERVE_URL_RAW=!SERVE_URL_RAW:"=!"
    set "SERVE_URL_RAW=!SERVE_URL_RAW: =!"
    for /f "tokens=1,2 delims=/" %%H in ("!SERVE_URL_RAW:http://=!") do set "HOSTPORT=%%H"
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

echo Ensuring OpenCode serve on http://127.0.0.1:%SERVE_PORT%/ ...
powershell -NoProfile -ExecutionPolicy Bypass -File "%PKG_WIN%\Ensure-OpencodeServe.ps1" -ProjectDir "%SCRIPT_DIR%" -ServeHost "%SERVE_HOST%" -ServePort %SERVE_PORT% -TimeoutSec 90
if errorlevel 1 (
    echo [ERROR] OpenCode serve is required. Jobs cannot run without it.
    echo Fix: install OpenCode, or run start-opencode-serve.bat and read the serve window.
    call :maybe_pause
    exit /b 1
)

echo Stopping previous backend on port %DASH_PORT% ^(does not stop serve or frontend^)...
powershell -NoProfile -ExecutionPolicy Bypass -File "%PKG_WIN%\Stop-VdProcesses.ps1" -DashboardPort %DASH_PORT% -VitePort 0 -KillDaemon
timeout /t 1 /nobreak >nul

echo Starting daemon in window "VD-Backend"...
start "VD-Backend" /D "%SCRIPT_DIR%" cmd /c "set DASHBOARD_HOST=0.0.0.0&& set DASHBOARD_ALLOW_REMOTE=true&& set DASHBOARD_PORT=%DASH_PORT%&& set DASHBOARD_ENABLED=true&& set VD_WEB_DIST=%SCRIPT_DIR%\web\dist&& %VD_PY% -m src.daemon & echo. & echo Backend exited. & pause"

echo Waiting for API http://127.0.0.1:%DASH_PORT%/api/meta ...
powershell -NoProfile -ExecutionPolicy Bypass -File "%PKG_WIN%\Wait-Http.ps1" -Url "http://127.0.0.1:%DASH_PORT%/api/meta" -TimeoutSec 90
if errorlevel 1 (
    echo [ERROR] Backend did not become ready on port %DASH_PORT%.
    echo Open the "VD-Backend" window and read the traceback.
    echo Common issues: bad .env, port in use, firewall.
    call :maybe_pause
    exit /b 1
)

echo.
echo [OK] Backend is up.
echo   API meta : http://127.0.0.1:%DASH_PORT%/api/meta
echo   Dashboard: http://127.0.0.1:%DASH_PORT%/
echo   LAN      : http://^<this-pc-ip^>:%DASH_PORT%/
echo   Serve    : http://127.0.0.1:%SERVE_PORT%/global/health
echo.
echo Optional separate frontend UI on port %FRONTEND_PORT%: start-frontend.bat
echo.
call :maybe_pause
exit /b 0

:maybe_pause
if /i "%VD_NONINTERACTIVE%"=="1" exit /b 0
pause
exit /b 0
