@echo off
REM =============================================================================
REM JIRA Virtual Developer - Codex CLI only (offline)
REM =============================================================================
REM Installs Codex from vendor\bin\codex.exe in the CI zip.
REM Does NOT install OpenCode, Python, .venv, or the dashboard.
REM
REM Path:
REM   %LOCALAPPDATA%\Programs\OpenAI\Codex\bin\codex.exe
REM   (official standalone path, same as chatgpt.com/codex/install.ps1)
REM
REM IMPORTANT (cmd.exe): never write unescaped "->" in echo lines.
REM =============================================================================

setlocal EnableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
set "PS1=%SCRIPT_DIR%\packaging\windows\Install-Backends.ps1"

if not exist "%PS1%" (
    echo [ERROR] Missing %PS1%
    echo Re-download the full package so packaging\windows\ is present.
    call :maybe_pause
    exit /b 1
)

if not exist "%SCRIPT_DIR%\vendor\bin\codex.exe" (
    echo [ERROR] vendor\bin\codex.exe missing. This script needs the CI offline zip.
    call :maybe_pause
    exit /b 1
)

echo ========================================
echo   Virtual Developer - Codex only
echo   Codex CLI ^(no OpenCode / Python^)
echo ========================================
echo.
echo Project : %SCRIPT_DIR%
echo Target  : %LOCALAPPDATA%\Programs\OpenAI\Codex\bin\codex.exe
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" -RepoRoot "%SCRIPT_DIR%" -Codex
set "EC=%ERRORLEVEL%"
if not "%EC%"=="0" (
    echo.
    echo [ERROR] Codex install failed ^(exit %EC%^).
    call :maybe_pause
    exit /b %EC%
)

echo.
echo Next: install-dashboard.bat if the app is not installed yet.
echo      install-backends.bat for OpenCode.
echo Open a NEW terminal so PATH includes Codex.
call :maybe_pause
exit /b 0

:maybe_pause
if /i "%VD_NONINTERACTIVE%"=="1" exit /b 0
pause
exit /b 0
