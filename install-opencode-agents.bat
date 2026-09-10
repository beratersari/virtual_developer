@echo off
REM Copy opencoderman\agents and opencoderman\skills into the OpenCode home.
REM Does not install the OpenCode CLI. Does not write %USERPROFILE%\.config\opencode.
REM IMPORTANT (cmd.exe): never write unescaped ">" in echo lines.
setlocal EnableExtensions

set "SCRIPT_DIR=%~dp0"
set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
set "SRC_AGENTS=%SCRIPT_DIR%\opencoderman\agents"
set "SRC_SKILLS=%SCRIPT_DIR%\opencoderman\skills"

if not exist "%SRC_AGENTS%\derman-build.md" goto :missing_src
if not exist "%SRC_AGENTS%\derman-plan.md" goto :missing_src
if not exist "%SRC_AGENTS%\derman-test.md" goto :missing_src
if not exist "%SRC_SKILLS%\" goto :missing_src

set "OC_HOME="
if defined OPENCODE_HOME (
    if exist "%OPENCODE_HOME%\" set "OC_HOME=%OPENCODE_HOME%"
)
if not defined OC_HOME (
    if exist "%USERPROFILE%\.opencode\bin\opencode.exe" set "OC_HOME=%USERPROFILE%\.opencode"
)
if not defined OC_HOME (
    if exist "%USERPROFILE%\.opencode\opencode.json" set "OC_HOME=%USERPROFILE%\.opencode"
)
if not defined OC_HOME (
    if exist "%USERPROFILE%\.opencode\agents\" set "OC_HOME=%USERPROFILE%\.opencode"
)
if not defined OC_HOME (
    for /f "delims=" %%I in ('where opencode.exe 2^>nul') do (
        if not defined OC_HOME call :from_bin "%%~dpI"
    )
)

if not defined OC_HOME (
    echo [ERROR] OpenCode is not installed.
    echo Set OPENCODE_HOME, or install OpenCode so %%USERPROFILE%%\.opencode exists.
    call :maybe_pause
    exit /b 1
)

echo OpenCode home : %OC_HOME%
echo Source agents : %SRC_AGENTS%
echo Source skills : %SRC_SKILLS%

if not exist "%OC_HOME%\agents\" mkdir "%OC_HOME%\agents"
if not exist "%OC_HOME%\skills\" mkdir "%OC_HOME%\skills"

copy /Y "%SRC_AGENTS%\derman-build.md" "%OC_HOME%\agents\derman-build.md" >nul
if errorlevel 1 (
    echo [ERROR] copy derman-build.md failed
    call :maybe_pause
    exit /b 1
)
copy /Y "%SRC_AGENTS%\derman-plan.md" "%OC_HOME%\agents\derman-plan.md" >nul
if errorlevel 1 (
    echo [ERROR] copy derman-plan.md failed
    call :maybe_pause
    exit /b 1
)
copy /Y "%SRC_AGENTS%\derman-test.md" "%OC_HOME%\agents\derman-test.md" >nul
if errorlevel 1 (
    echo [ERROR] copy derman-test.md failed
    call :maybe_pause
    exit /b 1
)
robocopy "%SRC_SKILLS%" "%OC_HOME%\skills" /E /NFL /NDL /NJH /NJS /NC /NS /NP >nul
if errorlevel 8 (
    echo [ERROR] robocopy skills failed with exit %ERRORLEVEL%
    call :maybe_pause
    exit /b 1
)

if not exist "%OC_HOME%\agents\derman-build.md" (
    echo [ERROR] Copy finished but derman-build.md is missing.
    call :maybe_pause
    exit /b 1
)
if not exist "%OC_HOME%\agents\derman-plan.md" (
    echo [ERROR] Copy finished but derman-plan.md is missing.
    call :maybe_pause
    exit /b 1
)
if not exist "%OC_HOME%\agents\derman-test.md" (
    echo [ERROR] Copy finished but derman-test.md is missing.
    call :maybe_pause
    exit /b 1
)

echo [OK] agents -^> %OC_HOME%\agents
echo [OK] skills -^> %OC_HOME%\skills
call :maybe_pause
exit /b 0

:from_bin
if defined OC_HOME goto :eof
set "BINDIR=%~1"
if "%BINDIR:~-1%"=="\" set "BINDIR=%BINDIR:~0,-1%"
for %%P in ("%BINDIR%") do set "LEAF=%%~nxP"
if /I "%LEAF%"=="bin" (
    for %%P in ("%BINDIR%\..") do set "OC_HOME=%%~fP"
)
goto :eof

:missing_src
echo [ERROR] opencoderman\agents and opencoderman\skills not found next to this script.
echo Expected: %SRC_AGENTS%
echo           %SRC_SKILLS%
call :maybe_pause
exit /b 1

:maybe_pause
echo %CMDCMDLINE% | find /I "/c" >nul
if errorlevel 1 pause
exit /b 0
