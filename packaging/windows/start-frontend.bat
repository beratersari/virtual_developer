@echo off
REM =============================================================================
REM Virtual Developer - start FRONTEND only (SPA on :5173, proxies to backend)
REM Requires: install.bat done, web\dist present, backend already running
REM            (start-backend.bat). No Node/Vite required.
REM IMPORTANT: never use unescaped "->" in echo lines (cmd redirect).
REM =============================================================================

setlocal EnableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
cd /d "%SCRIPT_DIR%"

set "FRONTEND_HOST=0.0.0.0"
set "FRONTEND_PORT=5173"
set "BACKEND_URL=http://127.0.0.1:8080"
set "VENV_PY=%SCRIPT_DIR%\.venv\Scripts\python.exe"
set "VD_WEB_DIST=%SCRIPT_DIR%\web\dist"
set "SERVE_PY=%SCRIPT_DIR%\packaging\windows\serve_frontend.py"

echo ========================================
echo   Virtual Developer - Frontend
echo ========================================
echo Project  : %SCRIPT_DIR%
echo UI       : http://0.0.0.0:%FRONTEND_PORT%/  ^(open http://127.0.0.1:%FRONTEND_PORT%/ ^)
echo Proxies  : /api and /ws  -^>  %BACKEND_URL%
echo.

if not exist "%VENV_PY%" (
    echo [ERROR] Missing .venv - run install.bat first.
    call :maybe_pause
    exit /b 1
)

if not exist "%VD_WEB_DIST%\index.html" (
    echo [ERROR] Missing %VD_WEB_DIST%\index.html
    echo This offline package must include a prebuilt SPA from CI ^(web\dist^).
    call :maybe_pause
    exit /b 1
)

if not exist "%SERVE_PY%" (
    echo [ERROR] Missing %SERVE_PY%
    call :maybe_pause
    exit /b 1
)

echo Checking backend at %BACKEND_URL%/api/meta ...
powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%\packaging\windows\Wait-Http.ps1" -Url "%BACKEND_URL%/api/meta" -TimeoutSec 15
if errorlevel 1 (
    echo [ERROR] Backend is not reachable at %BACKEND_URL%
    echo Start it first:  start-backend.bat
    call :maybe_pause
    exit /b 1
)
echo [OK] Backend is reachable.

echo Stopping previous frontend on port %FRONTEND_PORT% only ^(backend stays up^)...
REM Do NOT pass -KillDaemon — that was killing the running backend.
powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%\packaging\windows\Stop-VdProcesses.ps1" -DashboardPort 0 -VitePort %FRONTEND_PORT%
timeout /t 1 /nobreak >nul

echo Re-checking backend still alive after cleanup...
powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%\packaging\windows\Wait-Http.ps1" -Url "%BACKEND_URL%/api/meta" -TimeoutSec 10
if errorlevel 1 (
    echo [ERROR] Backend died or is unreachable after frontend cleanup.
    echo This should not happen. Re-run start-backend.bat, then start-frontend.bat.
    call :maybe_pause
    exit /b 1
)
echo [OK] Backend still up.

echo Starting frontend in window "VD-Frontend"...
start "VD-Frontend" /D "%SCRIPT_DIR%" cmd /c "set VD_WEB_DIST=%SCRIPT_DIR%\web\dist&& set VD_BACKEND_URL=%BACKEND_URL%&& set VD_FRONTEND_HOST=%FRONTEND_HOST%&& set VD_FRONTEND_PORT=%FRONTEND_PORT%&& .venv\Scripts\python.exe packaging\windows\serve_frontend.py --dist web\dist --backend %BACKEND_URL% --host %FRONTEND_HOST% --port %FRONTEND_PORT% & echo. & echo Frontend exited. & pause"

echo Waiting for UI http://127.0.0.1:%FRONTEND_PORT%/ ...
powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%\packaging\windows\Wait-Http.ps1" -Url "http://127.0.0.1:%FRONTEND_PORT%/" -TimeoutSec 60 -OkPattern "id=.root"
if errorlevel 1 (
    echo [ERROR] Frontend did not become ready on port %FRONTEND_PORT%.
    echo Open the "VD-Frontend" window for errors.
    call :maybe_pause
    exit /b 1
)

echo.
echo [OK] Frontend is up.
echo   Open: http://127.0.0.1:%FRONTEND_PORT%/
echo   LAN : http://^<this-pc-ip^>:%FRONTEND_PORT%/
echo   Backend API remains at %BACKEND_URL%
start "" "http://127.0.0.1:%FRONTEND_PORT%/"
echo.
call :maybe_pause
exit /b 0

:maybe_pause
if /i "%VD_NONINTERACTIVE%"=="1" exit /b 0
pause
exit /b 0
