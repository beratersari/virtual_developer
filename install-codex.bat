@echo off
REM =============================================================================
REM JIRA Virtual Developer - Codex CLI only
REM =============================================================================
REM Downloads/extracts the official Windows package:
REM   codex-package-x86_64-pc-windows-msvc.tar.gz
REM Prefer vendor\ copy (offline zip). Else download from GitHub.
REM Extract with tar.exe (Windows 10+). Does NOT install OpenCode / Python.
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
set "VER_FILE=%SCRIPT_DIR%\packaging\windows\versions.env"
if exist "%SCRIPT_DIR%\vendor\versions.env" set "VER_FILE=%SCRIPT_DIR%\vendor\versions.env"
set "DUMMY_CFG=%SCRIPT_DIR%\packaging\windows\codex-config.toml"
set "CODEX_VERSION=0.149.0"
set "ASSET=codex-package-x86_64-pc-windows-msvc.tar.gz"

if not exist "%PS1%" (
    echo [ERROR] Missing %PS1%
    echo Re-download the full package so packaging\windows\ is present.
    call :maybe_pause
    exit /b 1
)

if exist "%VER_FILE%" (
    for /f "usebackq eol=# tokens=1,* delims==" %%A in ("%VER_FILE%") do (
        if /i "%%A"=="CODEX_VERSION" set "CODEX_VERSION=%%B"
        if /i "%%A"=="CODEX_WINDOWS_ASSET" set "ASSET=%%B"
    )
)
set "CODEX_VERSION=%CODEX_VERSION: =%"
set "ASSET=%ASSET: =%"
if not defined ASSET set "ASSET=codex-package-x86_64-pc-windows-msvc.tar.gz"

set "PKG=%SCRIPT_DIR%\vendor\%ASSET%"
set "EXTRACT=%TEMP%\vd-codex-pkg"
set "DL=%TEMP%\%ASSET%"
set "CODEX_URL=https://github.com/openai/codex/releases/download/rust-v%CODEX_VERSION%/%ASSET%"

if not exist "%PKG%" (
    echo Package not in vendor. Downloading from GitHub...
    echo URL     : %CODEX_URL%
    curl.exe -L --fail --retry 3 -o "%DL%" "%CODEX_URL%"
    if errorlevel 1 (
        echo [ERROR] Could not download %ASSET%
        echo Place it at vendor\%ASSET% for offline install, or check the network.
        echo This installer only uses the tar.gz package ^(not vendor\bin\codex.exe^).
        call :maybe_pause
        exit /b 1
    )
    set "PKG=%DL%"
)

if not exist "%PKG%" goto :install_ps

echo Extracting %ASSET% with tar...
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
echo [OK] Extracted package with tar

:install_ps
echo ========================================
echo   Virtual Developer - Codex only
echo   Codex CLI ^(no OpenCode / Python^)
echo ========================================
echo.
echo Project : %SCRIPT_DIR%
echo Package : %ASSET%
echo Target  : %LOCALAPPDATA%\Programs\OpenAI\Codex\bin\codex.exe
echo Config  : %USERPROFILE%\.codex
echo.

if exist "%DUMMY_CFG%" (
    if not exist "%USERPROFILE%\.codex" mkdir "%USERPROFILE%\.codex"
    if not exist "%USERPROFILE%\.codex\config.toml" (
        copy /Y "%DUMMY_CFG%" "%USERPROFILE%\.codex\config.toml" >nul
        echo [OK] Seeded dummy config.toml under %USERPROFILE%\.codex
    ) else (
        echo [OK] Keeping existing %USERPROFILE%\.codex\config.toml
    )
)

if defined EXTRACT if exist "%EXTRACT%" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" -RepoRoot "%SCRIPT_DIR%" -Codex -CodexExtract "%EXTRACT%"
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" -RepoRoot "%SCRIPT_DIR%" -Codex
)
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
