@echo off
REM =============================================================================
REM Virtual Developer - start BOTH backend (:8080) and frontend (:5173)
REM IMPORTANT: never use unescaped "->" in echo lines (cmd redirect).
REM =============================================================================

setlocal EnableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
cd /d "%SCRIPT_DIR%"

echo ========================================
echo   Virtual Developer - Start all
echo ========================================
echo.
echo   Backend  : http://127.0.0.1:8080/   ^(API + SPA^)
echo   Frontend : http://127.0.0.1:5173/   ^(SPA + proxy — use this if you want :5173^)
echo   Serve    : http://127.0.0.1:4096/   ^(OpenCode — started with backend if needed^)
echo.

set "BE=%SCRIPT_DIR%\start-backend.bat"
set "FE=%SCRIPT_DIR%\start-frontend.bat"
if not exist "%BE%" set "BE=%SCRIPT_DIR%\packaging\windows\start-backend.bat"
if not exist "%FE%" set "FE=%SCRIPT_DIR%\packaging\windows\start-frontend.bat"

if not exist "%BE%" (
    echo [ERROR] start-backend.bat not found. Run install-dashboard.bat or re-download the package.
    call :maybe_pause
    exit /b 1
)
if not exist "%FE%" (
    echo [ERROR] start-frontend.bat not found. Run install-dashboard.bat or re-download the package.
    call :maybe_pause
    exit /b 1
)

echo === [1/2] Backend ===
set "VD_NONINTERACTIVE=1"
call "%BE%"
if errorlevel 1 (
    echo [ERROR] Backend failed to start. See "VD-Backend" window.
    set "VD_NONINTERACTIVE="
    call :maybe_pause
    exit /b 1
)

echo.
echo === [2/2] Frontend ===
call "%FE%"
set "RC=%ERRORLEVEL%"
set "VD_NONINTERACTIVE="

if not "%RC%"=="0" (
    echo [ERROR] Frontend failed. Backend may still be on :8080.
    echo Open http://127.0.0.1:8080/ for the daemon-hosted UI.
    call :maybe_pause
    exit /b 1
)

echo.
echo ========================================
echo   Both running
echo ========================================
echo Prefer UI:  http://127.0.0.1:5173/
echo Also UI:    http://127.0.0.1:8080/
echo API:        http://127.0.0.1:8080/api/meta
echo.
echo Console windows: VD-OpenCode-Serve, VD-Backend, VD-Frontend
echo.
call :maybe_pause
exit /b 0

:maybe_pause
if /i "%VD_NONINTERACTIVE%"=="1" exit /b 0
pause
exit /b 0
