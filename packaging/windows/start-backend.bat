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
set "VENV_PY=%SCRIPT_DIR%\.venv\Scripts\python.exe"

REM Bind all interfaces (LAN access). Browser: http://127.0.0.1:8080/ or http://THIS-PC:8080/
set "DASHBOARD_HOST=0.0.0.0"
set "DASHBOARD_ALLOW_REMOTE=true"
set "DASHBOARD_PORT=%DASH_PORT%"
set "DASHBOARD_ENABLED=true"
set "VD_WEB_DIST=%SCRIPT_DIR%\web\dist"

echo ========================================
echo   Virtual Developer - Backend
echo ========================================
echo Project : %SCRIPT_DIR%
echo API+SPA : http://0.0.0.0:%DASH_PORT%/  ^(open http://127.0.0.1:%DASH_PORT%/ ^)
echo.

if not exist "%VENV_PY%" (
    echo [ERROR] Missing .venv - run install.bat first.
    call :maybe_pause
    exit /b 1
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

echo Stopping previous backend on port %DASH_PORT% ...
powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%\packaging\windows\Stop-VdProcesses.ps1" -DashboardPort %DASH_PORT% -VitePort 0
timeout /t 1 /nobreak >nul

echo Starting daemon in window "VD-Backend"...
start "VD-Backend" /D "%SCRIPT_DIR%" cmd /c "set DASHBOARD_HOST=0.0.0.0&& set DASHBOARD_ALLOW_REMOTE=true&& set DASHBOARD_PORT=%DASH_PORT%&& set DASHBOARD_ENABLED=true&& set VD_WEB_DIST=%SCRIPT_DIR%\web\dist&& .venv\Scripts\python.exe -m src.daemon & echo. & echo Backend exited. & pause"

echo Waiting for API http://127.0.0.1:%DASH_PORT%/api/meta ...
powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%\packaging\windows\Wait-Http.ps1" -Url "http://127.0.0.1:%DASH_PORT%/api/meta" -TimeoutSec 90
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
echo.
echo Optional separate frontend UI on port %FRONTEND_PORT%: start-frontend.bat
echo.
call :maybe_pause
exit /b 0

:maybe_pause
if /i "%VD_NONINTERACTIVE%"=="1" exit /b 0
pause
exit /b 0
