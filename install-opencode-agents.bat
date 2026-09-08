@echo off
REM Copy OpenCoderman agents/ and skills/ into the detected OpenCode home.
REM Does not install the OpenCode CLI. Does not write %USERPROFILE%\.config\opencode.
REM
REM IMPORTANT (cmd.exe): never write unescaped ">" in echo lines.
setlocal

set "SCRIPT_DIR=%~dp0"
set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
set "PS1=%SCRIPT_DIR%\Install-OpencodeAgents.ps1"
if not exist "%PS1%" set "PS1=%SCRIPT_DIR%\packaging\windows\Install-OpencodeAgents.ps1"

if not exist "%PS1%" (
    echo [ERROR] Install-OpencodeAgents.ps1 not found.
    echo Expected next to this bat, or in packaging\windows\.
    call :maybe_pause
    exit /b 1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PS1%" -SourceRoot "%SCRIPT_DIR%"
set "EC=%ERRORLEVEL%"
if not "%EC%"=="0" (
    echo.
    echo Copy failed with exit code %EC%.
    call :maybe_pause
    exit /b %EC%
)
echo.
echo Done. OpenCode agents and skills are in the detected OpenCode home.
call :maybe_pause
exit /b 0

:maybe_pause
echo %CMDCMDLINE% | find /I "/c" >nul
if errorlevel 1 pause
exit /b 0
