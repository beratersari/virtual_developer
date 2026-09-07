@echo off
REM =============================================================================
REM JIRA Virtual Developer - Online OpenCode installer
REM =============================================================================
REM ONLINE ONLY. Does not replace offline install-backends.bat.
REM
REM Uses the opencoderman submodule:
REM   python opencoderman/packaging/build_artifact.py --in-place
REM   python packaging/install_opencode.py --require-binary
REM
REM Requires:
REM   - internet (official OpenCode GitHub release, version from
REM     opencoderman/packaging/versions.env)
REM   - Python 3.10+ (project .venv or python / py on PATH)
REM
REM Result layout (same as offline install-backends.bat):
REM   %USERPROFILE%\.opencode\          (CLI + agents + skills)
REM
REM IMPORTANT (cmd.exe): never write unescaped "->" in echo lines.
REM =============================================================================

setlocal EnableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
set "PS1=%SCRIPT_DIR%\packaging\windows\Install-OpencodeOnline.ps1"
set "OCM=%SCRIPT_DIR%\opencoderman"

if not exist "%PS1%" (
    echo [ERROR] Missing %PS1%
    echo Re-download the full package so packaging\windows\ is present.
    call :maybe_pause
    exit /b 1
)

if not exist "%OCM%\install.py" (
    echo [ERROR] opencoderman\install.py missing.
    echo         From a git checkout: git submodule update --init --recursive
    call :maybe_pause
    exit /b 1
)

echo ========================================
echo   Virtual Developer - Online OpenCode
echo   opencoderman vendor + install.py
echo ========================================
echo.
echo Project root : %SCRIPT_DIR%
echo OpenCoderman : %OCM%
echo Offline path : install-backends.bat ^(CI zip / vendor CLI^)
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" -RepoRoot "%SCRIPT_DIR%"
set "EC=%ERRORLEVEL%"
if not "%EC%"=="0" (
    echo.
    echo [ERROR] Online OpenCode install failed ^(exit %EC%^).
    echo Need network to GitHub releases ^(anomalyco/opencode^).
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
