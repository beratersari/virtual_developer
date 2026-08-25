@echo off
REM =============================================================================
REM JIRA Virtual Developer - Codex CLI only (offline)
REM =============================================================================
REM Installs Codex from the CI zip:
REM   vendor\codex-package-x86_64-pc-windows-msvc.tar.gz
REM Extract with tar.exe (Windows 10+). No network. No vendor\bin\codex.exe.
REM Does NOT install OpenCode, Python, .venv, or the dashboard.
REM
REM Paths:
REM   %LOCALAPPDATA%\Programs\OpenAI\Codex\bin\codex.exe
REM   %USERPROFILE%\.codex\config.toml   (dummy if missing; never overwrite)
REM
REM IMPORTANT (cmd.exe): never write unescaped "->" in echo lines.
REM =============================================================================

setlocal EnableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
set "PS1=%SCRIPT_DIR%\packaging\windows\Install-Backends.ps1"
set "ASSET=codex-package-x86_64-pc-windows-msvc.tar.gz"
set "PKG=%SCRIPT_DIR%\vendor\%ASSET%"
set "DUMMY_CFG=%SCRIPT_DIR%\packaging\windows\codex-config.toml"
if not exist "%DUMMY_CFG%" set "DUMMY_CFG=%SCRIPT_DIR%\vendor\codex-config.toml"
set "EXTRACT=%TEMP%\vd-codex-pkg"

if not exist "%PS1%" (
    echo [ERROR] Missing %PS1%
    echo Re-download the full package so packaging\windows\ is present.
    call :maybe_pause
    exit /b 1
)

if not exist "%PKG%" (
    echo [ERROR] %PKG% missing.
    echo This script needs the CI offline zip, which ships vendor\%ASSET%.
    echo It does not download Codex and does not use vendor\bin\codex.exe.
    call :maybe_pause
    exit /b 1
)

echo ========================================
echo   Virtual Developer - Codex only
echo   Codex CLI ^(offline tar, no OpenCode / Python^)
echo ========================================
echo.
echo Project : %SCRIPT_DIR%
echo Package : vendor\%ASSET%
echo Target  : %LOCALAPPDATA%\Programs\OpenAI\Codex\bin\codex.exe
echo Config  : %USERPROFILE%\.codex
echo.

echo Extracting vendor\%ASSET% with tar...
if exist "%EXTRACT%" rmdir /s /q "%EXTRACT%"
mkdir "%EXTRACT%"
pushd "%EXTRACT%"
tar.exe --force-local -xf "%PKG%"
if errorlevel 1 tar.exe -xf "%PKG%"
set "TAR_EC=!ERRORLEVEL!"
popd
if not "!TAR_EC!"=="0" (
    echo [ERROR] tar extract failed ^(exit !TAR_EC!^). Need Windows 10+ tar.exe.
    call :maybe_pause
    exit /b 1
)
echo [OK] Extracted vendor\%ASSET% with tar

if exist "%DUMMY_CFG%" (
    if not exist "%USERPROFILE%\.codex" mkdir "%USERPROFILE%\.codex"
    if not exist "%USERPROFILE%\.codex\config.toml" (
        copy /Y "%DUMMY_CFG%" "%USERPROFILE%\.codex\config.toml" >nul
        echo [OK] Seeded dummy config.toml under %USERPROFILE%\.codex
    ) else (
        echo [OK] Keeping existing %USERPROFILE%\.codex\config.toml
    )
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" -RepoRoot "%SCRIPT_DIR%" -Codex -CodexExtract "%EXTRACT%"
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
