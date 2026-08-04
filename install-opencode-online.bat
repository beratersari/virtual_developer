@echo off
REM =============================================================================
REM JIRA Virtual Developer - Online OpenCode installer
REM =============================================================================
REM ONLINE ONLY. Does not replace offline install.bat.
REM
REM Requires:
REM   - internet (or HTTP mirrors of OpenCode zip + npm packages)
REM   - vendor\node\node.exe + npm.cmd (shipped in CI zip - no system Node)
REM
REM npm registry (edit BEFORE running):
REM   packaging\windows\npm-online.npmrc   (or vendor\npm-online.npmrc)
REM   set registry=http://your-server/...
REM Optional binary mirrors:
REM   packaging\windows\online-sources.env  (OPENCODE_ZIP_URL, NPM_REGISTRY, ...)
REM
REM Result layout (same as offline install.bat):
REM   %USERPROFILE%\.opencode\
REM   %USERPROFILE%\.config\opencode\
REM   %USERPROFILE%\.cache\opencode\
REM
REM IMPORTANT (cmd.exe): never write unescaped "->" in echo lines.
REM =============================================================================

setlocal EnableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
set "PS1=%SCRIPT_DIR%\packaging\windows\Install-OpencodeOnline.ps1"
set "NODE_EXE=%SCRIPT_DIR%\vendor\node\node.exe"
set "NPM_CMD=%SCRIPT_DIR%\vendor\node\npm.cmd"
set "NPMRC=%SCRIPT_DIR%\packaging\windows\npm-online.npmrc"
if exist "%SCRIPT_DIR%\vendor\npm-online.npmrc" set "NPMRC=%SCRIPT_DIR%\vendor\npm-online.npmrc"

if not exist "%PS1%" (
    echo [ERROR] Missing %PS1%
    echo Re-download the full package so packaging\windows\ is present.
    call :maybe_pause
    exit /b 1
)

if not exist "%NODE_EXE%" (
    echo [ERROR] Portable Node missing: %NODE_EXE%
    echo This online installer requires vendor\node from the CI zip.
    echo For offline OpenCode, use install.bat instead ^(vendor\opencode-home.zip^).
    call :maybe_pause
    exit /b 1
)
if not exist "%NPM_CMD%" (
    echo [ERROR] Portable npm missing: %NPM_CMD%
    call :maybe_pause
    exit /b 1
)

echo ========================================
echo   Virtual Developer - Online OpenCode
echo   vendor\node + npm registry install
echo ========================================
echo.
echo Project root : %SCRIPT_DIR%
echo Node         : %NODE_EXE%
echo npm registry : edit %NPMRC%
echo Offline path : install.bat is unchanged ^(vendor\opencode-home.zip^)
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" -RepoRoot "%SCRIPT_DIR%"
set "EC=%ERRORLEVEL%"
if not "%EC%"=="0" (
    echo.
    echo [ERROR] Online OpenCode install failed ^(exit %EC%^).
    echo Check npm-online.npmrc registry= and network to your package server.
    call :maybe_pause
    exit /b %EC%
)

echo.
echo Done. Launch TUI with start-opencode.bat ^(from this project folder^).
echo Do NOT run opencode from your user home folder.
call :maybe_pause
exit /b 0

:maybe_pause
if /i "%CI%"=="true" exit /b 0
if /i "%VD_NO_PAUSE%"=="1" exit /b 0
if defined GITHUB_ACTIONS exit /b 0
pause
exit /b 0
