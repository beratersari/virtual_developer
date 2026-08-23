@echo off
REM =============================================================================
REM JIRA Virtual Developer - Agent backends only (offline)
REM =============================================================================
REM Installs OpenCode and/or Codex from vendor\ in the CI zip.
REM Does NOT create .venv, install Python deps, write .env, or start the dashboard.
REM
REM Usage:
REM   install-backends.bat              both OpenCode + Codex
REM   install-backends.bat opencode     OpenCode only
REM   install-backends.bat codex        Codex only
REM
REM Paths:
REM   OpenCode  %USERPROFILE%\.opencode
REM   Codex     %LOCALAPPDATA%\Programs\OpenAI\Codex\bin\codex.exe
REM
REM IMPORTANT (cmd.exe): never write unescaped "->" in echo lines.
REM =============================================================================

setlocal EnableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
set "PS1=%SCRIPT_DIR%\packaging\windows\Install-Backends.ps1"
set "WHICH=%~1"

if not exist "%PS1%" (
    echo [ERROR] Missing %PS1%
    echo Re-download the full package so packaging\windows\ is present.
    call :maybe_pause
    exit /b 1
)

if /i "%WHICH%"=="codex" (
    if not exist "%SCRIPT_DIR%\vendor\bin\codex.exe" (
        echo [ERROR] vendor\bin\codex.exe missing. This script needs the CI offline zip.
        call :maybe_pause
        exit /b 1
    )
) else (
    if not exist "%SCRIPT_DIR%\vendor\opencode-home.zip" (
        echo [ERROR] vendor\opencode-home.zip missing. This script needs the CI offline zip.
        call :maybe_pause
        exit /b 1
    )
)

echo ========================================
echo   Virtual Developer - Backends only
echo   OpenCode + Codex ^(no Python / dashboard^)
echo ========================================
echo.
echo Project : %SCRIPT_DIR%
if /i "%WHICH%"=="opencode" echo Mode    : OpenCode only
if /i "%WHICH%"=="codex" echo Mode    : Codex only
if /i not "%WHICH%"=="opencode" if /i not "%WHICH%"=="codex" echo Mode    : OpenCode + Codex
echo.

if /i "%WHICH%"=="opencode" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" -RepoRoot "%SCRIPT_DIR%" -OpenCode
) else if /i "%WHICH%"=="codex" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" -RepoRoot "%SCRIPT_DIR%" -Codex
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" -RepoRoot "%SCRIPT_DIR%"
)
set "EC=%ERRORLEVEL%"
if not "%EC%"=="0" (
    echo.
    echo [ERROR] Backends install failed ^(exit %EC%^).
    call :maybe_pause
    exit /b %EC%
)

echo.
echo Next: install-dashboard.bat if the app is not installed yet.
echo Then start-backend.bat. Open a NEW terminal so PATH includes the CLIs.
call :maybe_pause
exit /b 0

:maybe_pause
if /i "%VD_NONINTERACTIVE%"=="1" exit /b 0
pause
exit /b 0
