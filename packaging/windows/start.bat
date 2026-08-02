@echo off
REM =============================================================================
REM JIRA Virtual Developer - start backend + ops dashboard (Windows)
REM =============================================================================
REM Stops any previous instance (ports 8080 / 5173 and old python -m src.daemon),
REM then starts the daemon. The ops UI (React SPA in web\dist) is served by the
REM same process on http://127.0.0.1:8080 — no separate Node frontend required.
REM
REM IMPORTANT (cmd.exe): never write unescaped "->" in echo lines.
REM =============================================================================

setlocal EnableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
cd /d "%SCRIPT_DIR%"

set "DASH_HOST=127.0.0.1"
set "DASH_PORT=8080"
set "VITE_PORT=5173"
set "VENV_PY=%SCRIPT_DIR%\.venv\Scripts\python.exe"
set "LOG_DIR=%SCRIPT_DIR%\logs"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%" 2>nul

echo ========================================
echo   Virtual Developer - Start
echo ========================================
echo Project : %SCRIPT_DIR%
echo Dashboard: http://%DASH_HOST%:%DASH_PORT%
echo.

if not exist "%VENV_PY%" (
    echo [ERROR] Python venv not found: %VENV_PY%
    echo Run install.bat first, then re-run start.bat.
    call :maybe_pause
    exit /b 1
)

if not exist "%SCRIPT_DIR%\web\dist\index.html" (
    echo [ERROR] Ops dashboard build missing: web\dist\index.html
    echo This offline package must include a prebuilt web\dist from CI.
    echo Rebuild the dist, or on a machine with Node: cd web ^&^& npm ci ^&^& npm run build
    call :maybe_pause
    exit /b 1
)

if not exist "%SCRIPT_DIR%\.env" (
    if exist "%SCRIPT_DIR%\.env.example" (
        copy /Y "%SCRIPT_DIR%\.env.example" "%SCRIPT_DIR%\.env" >nul
        echo [OK] Created .env from .env.example — edit Jira/GitLab credentials.
        echo.
    ) else (
        echo [WARNING] No .env file. Daemon may refuse to start until configured.
        echo.
    )
)

echo [1/2] Stopping previous Virtual Developer processes...
powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%\packaging\windows\Stop-VdProcesses.ps1" -DashboardPort %DASH_PORT% -VitePort %VITE_PORT%
if errorlevel 1 (
    echo [WARNING] Cleanup reported an issue; continuing with start...
)
timeout /t 1 /nobreak >nul

echo [2/2] Starting product ^(backend API + ops dashboard SPA in ONE process^)...
REM Same process serves REST/WS and web\dist — do not look for port 5173.
REM VD_WEB_DIST makes SPA discovery reliable regardless of import path quirks.
set "VD_WEB_DIST=%SCRIPT_DIR%\web\dist"
REM New console; cwd = project root so relative .venv and web\dist resolve
start "VD-Backend" /D "%SCRIPT_DIR%" cmd /c "set VD_WEB_DIST=%SCRIPT_DIR%\web\dist&& .venv\Scripts\python.exe -m src.daemon & echo. & echo Daemon exited. & pause"

echo Waiting for http://%DASH_HOST%:%DASH_PORT%/ ...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$hostPort='%DASH_HOST%'; $port=%DASH_PORT%; $ok=$false; for ($i=0; $i -lt 90; $i++) { try { $c=New-Object Net.Sockets.TcpClient($hostPort, $port); $c.Close(); $ok=$true; break } catch { Start-Sleep -Milliseconds 500 } }; if (-not $ok) { exit 1 }; try { $r=Invoke-WebRequest -Uri (\"http://${hostPort}:${port}/\") -UseBasicParsing -TimeoutSec 5; if ($r.Content -match 'id=\"root\"') { Write-Host 'OK SPA HTML (ops dashboard UI)'; exit 0 } elseif ($r.Content -match 'Dashboard API is running') { Write-Host 'WARN API only — web\\dist SPA not loaded'; exit 2 } else { Write-Host 'OK HTTP 200 (check browser)'; exit 0 } } catch { Write-Host $_.Exception.Message; exit 1 }"
set "CHK=%ERRORLEVEL%"
if "%CHK%"=="1" (
    echo [WARNING] Port %DASH_PORT% not open yet.
    echo Check the "VD-Backend" window for errors ^(Jira .env, stack traces^).
) else if "%CHK%"=="2" (
    echo [ERROR] Backend is up but the frontend SPA is missing.
    echo Expected: %SCRIPT_DIR%\web\dist\index.html
    echo Open http://%DASH_HOST%:%DASH_PORT%/ — if you see JSON, re-download a CI zip that includes web\dist.
    echo Do NOT use http://%DASH_HOST%:5173 — Vite is not used in the offline package.
) else (
    echo [OK] Open the ops dashboard at http://%DASH_HOST%:%DASH_PORT%/
    start "" "http://%DASH_HOST%:%DASH_PORT%/"
)

echo.
echo ========================================
echo   Running
echo ========================================
echo ONE process: backend + frontend UI
echo   URL: http://%DASH_HOST%:%DASH_PORT%/
echo   Not: http://%DASH_HOST%:5173  ^(that is Vite dev only^)
echo Console: VD-Backend ^(close it to stop^)
echo Re-run start.bat to kill the old instance and restart.
echo.
echo OpenCode TUI ^(optional, separate tool^): start-opencode.bat
echo.
call :maybe_pause
exit /b 0

:maybe_pause
if /i "%VD_NONINTERACTIVE%"=="1" exit /b 0
pause
exit /b 0
