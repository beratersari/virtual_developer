@echo off
REM Launch OpenCode from the Virtual Developer project directory.
REM CRITICAL: never run "opencode" from C:\Users\<you> — OpenCode treats that
REM entire profile as the project and hangs on a black screen while indexing.
setlocal
set "SCRIPT_DIR=%~dp0"
set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

set "OPENCODE_DISABLE_MODELS_FETCH=1"
set "PATH=%USERPROFILE%\.opencode\bin;%PATH%"

cd /d "%SCRIPT_DIR%"
if not exist "%USERPROFILE%\.opencode\bin\opencode.exe" (
    echo [ERROR] OpenCode not installed. Run install.bat first.
    pause
    exit /b 1
)

echo Starting OpenCode in project folder:
echo   %CD%
echo.
echo Tip: leave this window open. Do not run opencode from your user home.
echo.
"%USERPROFILE%\.opencode\bin\opencode.exe" %*
set "EC=%ERRORLEVEL%"
if not "%EC%"=="0" (
    echo.
    echo OpenCode exited with code %EC%.
    echo Logs: %%USERPROFILE%%\.local\share\opencode\log
    pause
)
exit /b %EC%
