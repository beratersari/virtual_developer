@echo off
REM =============================================================================
REM JIRA Virtual Developer - Windows Installer
REM =============================================================================
REM Offline when run from a CI-built distribution zip (vendor\ present):
REM   - Extracts vendor\opencode-home.zip -> %USERPROFILE%\.opencode
REM     (single archive avoids long-path / slow node_modules extract of the outer zip)
REM   - Creates .venv and installs Python deps from vendor\python-wheels (3.10+)
REM
REM Online fallback only if VD_ALLOW_ONLINE=1 and vendor is missing.
REM =============================================================================

setlocal EnableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
set "VENDOR_DIR=%SCRIPT_DIR%\vendor"
set "OPENCODE_HOME=%USERPROFILE%\.opencode"
set "OPENCODE_BIN=%OPENCODE_HOME%\bin"
set "VENV_DIR=%SCRIPT_DIR%\.venv"
set "VERSIONS_FILE=%SCRIPT_DIR%\packaging\windows\versions.env"
if exist "%VENDOR_DIR%\versions.env" set "VERSIONS_FILE=%VENDOR_DIR%\versions.env"

echo ========================================
echo   JIRA Virtual Developer - Installer
echo   Windows offline / local setup
echo ========================================
echo.
echo Install root : %SCRIPT_DIR%
echo OpenCode home: %OPENCODE_HOME%
echo.

REM ---------------------------------------------------------------------------
REM Prerequisites: Python 3.10+ (64-bit). No Node required for CI zip.
REM ---------------------------------------------------------------------------
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python is not installed or not on PATH.
    echo Install Python 3.10+ ^(64-bit^) from https://www.python.org/downloads/
    echo Enable "Add python.exe to PATH" during setup, then re-run install.bat.
    pause
    exit /b 1
)

for /f "tokens=*" %%a in ('python --version 2^>^&1') do set "PYTHON_VERSION=%%a"
echo [OK] %PYTHON_VERSION%

python -c "import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python 3.10 or newer is required.
    echo Found: %PYTHON_VERSION%
    pause
    exit /b 1
)

REM Reject Python versions that have no offline wheels (e.g. 3.14 without pydantic-core)
if exist "%VENDOR_DIR%\SUPPORTED_PYTHON.txt" (
    python -c "import sys,pathlib; p=pathlib.Path(r'%VENDOR_DIR%')/'SUPPORTED_PYTHON.txt'; lines=[l.strip() for l in p.read_text(encoding='utf-8',errors='ignore').splitlines() if l.strip() and not l.strip().startswith('#')]; ver=f'{sys.version_info.major}.{sys.version_info.minor}'; ok=ver in lines; print('Supported in this package:', ', '.join(lines)); print('Your Python minor:', ver); raise SystemExit(0 if ok else 1)"
    if errorlevel 1 (
        echo.
        echo [ERROR] Your Python is too new ^(or unsupported^) for the bundled offline wheels.
        echo Example: Python 3.14 often has no pydantic-core wheel yet, so offline install fails.
        echo.
        echo Fix: install a supported version from the list above ^(64-bit^), e.g. Python 3.12:
        echo   https://www.python.org/downloads/
        echo Enable "Add python.exe to PATH", open a NEW terminal, re-run install.bat.
        echo.
        echo Tip: delete the .venv folder if it was created with the wrong Python.
        pause
        exit /b 1
    )
    echo [OK] Python minor is in vendor\SUPPORTED_PYTHON.txt
)

REM ---------------------------------------------------------------------------
REM Load pinned versions
REM ---------------------------------------------------------------------------
set "OPENCODE_VERSION=1.18.10"
set "OH_MY_OPENCODE_VERSION=4.19.3"
set "GLAB_VERSION=1.111.0"
if exist "%VERSIONS_FILE%" (
    for /f "usebackq tokens=1,* delims==" %%A in ("%VERSIONS_FILE%") do (
        set "K=%%A"
        set "V=%%B"
        if /i "!K!"=="OPENCODE_VERSION" set "OPENCODE_VERSION=!V!"
        if /i "!K!"=="OH_MY_OPENCODE_VERSION" set "OH_MY_OPENCODE_VERSION=!V!"
        if /i "!K!"=="GLAB_VERSION" set "GLAB_VERSION=!V!"
    )
)

set "HAS_VENDOR=0"
if exist "%VENDOR_DIR%\opencode-home.zip" set "HAS_VENDOR=1"
if exist "%VENDOR_DIR%\opencode-home\bin\opencode.exe" set "HAS_VENDOR=1"

if exist "%VENDOR_DIR%\VERSIONS.txt" (
    echo [OK] Offline vendor bundle detected
    type "%VENDOR_DIR%\VERSIONS.txt"
    echo.
) else if "%HAS_VENDOR%"=="1" (
    echo [OK] Offline vendor bundle detected
    echo.
) else (
    echo [INFO] No vendor\ bundle — online install requires VD_ALLOW_ONLINE=1.
    echo.
)

REM ---------------------------------------------------------------------------
REM Step 1: Python venv + dependencies
REM ---------------------------------------------------------------------------
echo Step 1: Python virtual environment...
if not exist "%VENV_DIR%\Scripts\python.exe" (
    python -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
    echo [OK] Created %VENV_DIR%
) else (
    echo [OK] Using existing %VENV_DIR%
)

set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
set "VENV_PIP=%VENV_DIR%\Scripts\pip.exe"

echo.
echo Step 2: Installing Python packages...
if exist "%VENDOR_DIR%\python-wheels" (
    echo Using offline wheels from vendor\python-wheels ^(no network, Python 3.10+^)...
    "%VENV_PIP%" install --upgrade pip --no-index --find-links="%VENDOR_DIR%\python-wheels" 2>nul
    "%VENV_PIP%" install --no-index --find-links="%VENDOR_DIR%\python-wheels" -r "%SCRIPT_DIR%\requirements.txt"
    if errorlevel 1 (
        echo [ERROR] Offline wheel install failed. Refusing to use the network.
        echo.
        echo Common causes:
        echo   - Wrong Python version ^(use one listed in vendor\SUPPORTED_PYTHON.txt^)
        echo   - 32-bit Python ^(need 64-bit / AMD64^)
        echo   - Old/incomplete package ^(re-download the latest Actions artifact^)
        echo   - Nested extract path too deep — extract so install.bat is only 1 folder deep
        if exist "%VENDOR_DIR%\SUPPORTED_PYTHON.txt" (
            echo.
            echo Supported Python versions in this package:
            type "%VENDOR_DIR%\SUPPORTED_PYTHON.txt"
        )
        pause
        exit /b 1
    )
) else if "%HAS_VENDOR%"=="1" (
    echo [ERROR] vendor\ exists but python-wheels is missing. Broken distribution zip.
    pause
    exit /b 1
) else (
    echo [INFO] No offline wheels — installing from PyPI ^(needs internet^)...
    "%VENV_PIP%" install --upgrade pip --quiet
    "%VENV_PIP%" install -r "%SCRIPT_DIR%\requirements.txt"
    if errorlevel 1 (
        echo [ERROR] Python dependency install failed.
        pause
        exit /b 1
    )
)
echo [OK] Python dependencies installed into .venv

REM ---------------------------------------------------------------------------
REM Step 3: Deploy OpenCode home -> %USERPROFILE%\.opencode
REM ---------------------------------------------------------------------------
echo.
echo Step 3: Installing OpenCode under %OPENCODE_HOME% ...

if not exist "%OPENCODE_HOME%" mkdir "%OPENCODE_HOME%"
if not exist "%OPENCODE_BIN%" mkdir "%OPENCODE_BIN%"

if exist "%VENDOR_DIR%\opencode-home.zip" (
    echo Extracting vendor\opencode-home.zip -^> %OPENCODE_HOME%
    echo ^(single archive: avoids long Windows paths / slow outer-zip extract^)
    call :extract_opencode_home_zip
    if errorlevel 1 (
        echo [ERROR] Failed to extract OpenCode home archive.
        pause
        exit /b 1
    )
) else if exist "%VENDOR_DIR%\opencode-home\bin\opencode.exe" (
    echo Copying expanded vendor\opencode-home ^(legacy layout^)...
    robocopy "%VENDOR_DIR%\opencode-home" "%OPENCODE_HOME%" /E /NFL /NDL /NJH /NJS /nc /ns /np >nul
    if errorlevel 8 (
        echo [ERROR] robocopy failed copying opencode-home
        pause
        exit /b 1
    )
) else (
    echo [ERROR] No vendor\opencode-home.zip ^(or expanded opencode-home^) found.
    echo This offline installer requires the CI zip with vendor\ folder.
    echo.
    echo If you intentionally want an online git-clone install, set:
    echo   set VD_ALLOW_ONLINE=1
    echo then re-run install.bat
    if /i not "%VD_ALLOW_ONLINE%"=="1" (
        pause
        exit /b 1
    )
    echo Online fallback enabled via VD_ALLOW_ONLINE=1 ...
    call :install_opencode_online
    if errorlevel 1 (
        echo [ERROR] Online OpenCode install failed.
        pause
        exit /b 1
    )
)

if not exist "%OPENCODE_BIN%\opencode.exe" (
    echo [ERROR] opencode.exe missing at %OPENCODE_BIN%\opencode.exe
    pause
    exit /b 1
)
echo [OK] opencode.exe -> %OPENCODE_BIN%\opencode.exe

if exist "%OPENCODE_HOME%\opencode.json" (
    echo [OK] config      -> %OPENCODE_HOME%\opencode.json
) else (
    echo [WARNING] opencode.json not found under %OPENCODE_HOME%
)

if exist "%OPENCODE_HOME%\node_modules\oh-my-opencode" (
    echo [OK] plugin     -> oh-my-opencode in %OPENCODE_HOME%\node_modules
) else (
    echo [WARNING] oh-my-opencode plugin not found under node_modules
)

REM Mirror config into %USERPROFILE%\.config\opencode for OpenCode global discovery
set "OC_CONFIG_DIR=%USERPROFILE%\.config\opencode"
if not exist "%OC_CONFIG_DIR%" mkdir "%OC_CONFIG_DIR%"
if exist "%OPENCODE_HOME%\opencode.json" (
    copy /Y "%OPENCODE_HOME%\opencode.json" "%OC_CONFIG_DIR%\opencode.json" >nul
)
if exist "%OPENCODE_HOME%\oh-my-opencode.json" (
    copy /Y "%OPENCODE_HOME%\oh-my-opencode.json" "%OC_CONFIG_DIR%\oh-my-opencode.json" >nul
)
if exist "%OPENCODE_HOME%\package.json" (
    copy /Y "%OPENCODE_HOME%\package.json" "%OC_CONFIG_DIR%\package.json" >nul
)
echo [OK] Mirrored config to %OC_CONFIG_DIR%

REM ---------------------------------------------------------------------------
REM Step 4: glab
REM ---------------------------------------------------------------------------
echo.
echo Step 4: GitLab CLI ^(glab^)...
if exist "%OPENCODE_BIN%\glab.exe" (
    echo [OK] glab.exe at %OPENCODE_BIN%\glab.exe
) else (
    echo [WARNING] glab.exe not found under %OPENCODE_BIN%
)

REM ---------------------------------------------------------------------------
REM Step 5: User PATH
REM ---------------------------------------------------------------------------
echo.
echo Step 5: Adding OpenCode bin to user PATH...
call :ensure_user_path "%OPENCODE_BIN%"
set "PATH=%OPENCODE_BIN%;%PATH%"
echo [OK] PATH includes %OPENCODE_BIN% ^(this session + user env^)

REM ---------------------------------------------------------------------------
REM Step 6: .env + project init
REM ---------------------------------------------------------------------------
echo.
echo Step 6: Project configuration...
if not exist "%SCRIPT_DIR%\.env" (
    if exist "%SCRIPT_DIR%\.env.example" (
        copy /Y "%SCRIPT_DIR%\.env.example" "%SCRIPT_DIR%\.env" >nul
        echo [OK] Created .env from .env.example — edit credentials before production use
    ) else (
        echo [WARNING] .env.example missing; create .env manually
    )
) else (
    echo [OK] .env already exists ^(left unchanged^)
)

echo.
echo Step 7: Initializing project ^(cli.py init^)...
cd /d "%SCRIPT_DIR%"
"%VENV_PY%" cli.py init
if errorlevel 1 (
    echo [WARNING] cli.py init reported issues; continuing...
) else (
    echo [OK] Project initialized
)

echo.
echo ========================================
echo   Installation Complete!
echo ========================================
echo.
echo OpenCode home : %OPENCODE_HOME%
echo   bin         : %OPENCODE_BIN%\opencode.exe
if exist "%OPENCODE_BIN%\glab.exe" echo   glab        : %OPENCODE_BIN%\glab.exe
echo   config      : %OPENCODE_HOME%\opencode.json
echo Python venv   : %VENV_DIR%
echo.
echo Next steps:
echo   1. Edit .env with your Jira / GitLab settings
echo   2. Open a NEW terminal ^(so PATH updates apply^)
echo   3. Activate venv and start:
echo        cd /d "%SCRIPT_DIR%"
echo        .venv\Scripts\activate
echo        python cli.py start
echo.
echo Note: Restart terminals so %%USERPROFILE%%\.opencode\bin is on PATH.
echo.
pause
exit /b 0

REM =============================================================================
REM Subroutines
REM =============================================================================

:extract_opencode_home_zip
REM Extract vendor\opencode-home.zip into %OPENCODE_HOME% with long-path support.
set "OC_ZIP=%VENDOR_DIR%\opencode-home.zip"
if not exist "%OC_ZIP%" exit /b 1

if exist "%OPENCODE_HOME%\node_modules" (
    echo   Removing previous node_modules under .opencode ...
    rmdir /s /q "%OPENCODE_HOME%\node_modules" 2>nul
)

set "EXTRACT_PS1=%SCRIPT_DIR%\packaging\windows\extract-opencode-home.ps1"
if exist "%EXTRACT_PS1%" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%EXTRACT_PS1%" -Zip "%OC_ZIP%" -Dest "%OPENCODE_HOME%"
    if errorlevel 1 exit /b 1
    if exist "%OPENCODE_BIN%\opencode.exe" exit /b 0
    exit /b 1
)

REM Fallback if packaging scripts were stripped from the dist
where tar >nul 2>&1
if not errorlevel 1 (
    tar -xf "%OC_ZIP%" -C "%OPENCODE_HOME%"
    if exist "%OPENCODE_BIN%\opencode.exe" exit /b 0
)
exit /b 1

:ensure_user_path
set "ADD_PATH=%~1"
set "USER_PATH="
for /f "tokens=2*" %%A in ('reg query "HKCU\Environment" /v Path 2^>nul') do set "USER_PATH=%%B"
echo ;%USER_PATH%; | find /I ";%ADD_PATH%;" >nul
if not errorlevel 1 (
    echo [OK] User PATH already contains %ADD_PATH%
    goto :eof
)
if defined USER_PATH (
    setx PATH "%USER_PATH%;%ADD_PATH%" >nul
) else (
    setx PATH "%ADD_PATH%" >nul
)
if errorlevel 1 (
    echo [WARNING] Could not update user PATH via setx. Add manually:
    echo   %ADD_PATH%
) else (
    echo [OK] Appended to user PATH: %ADD_PATH%
)
goto :eof

:install_opencode_online
echo   Fetching OpenCode v!OPENCODE_VERSION! ...
set "TMP_OC=%TEMP%\vd-opencode-%RANDOM%"
mkdir "!TMP_OC!" 2>nul
set "OC_ZIP=!TMP_OC!\opencode-windows-x64.zip"
set "OC_URL=https://github.com/anomalyco/opencode/releases/download/v!OPENCODE_VERSION!/opencode-windows-x64.zip"
powershell -NoProfile -Command "Invoke-WebRequest -Uri '!OC_URL!' -OutFile '!OC_ZIP!' -UseBasicParsing"
if errorlevel 1 (
    echo [ERROR] Download OpenCode failed
    exit /b 1
)
powershell -NoProfile -Command "Expand-Archive -Path '!OC_ZIP!' -DestinationPath '!TMP_OC!\extract' -Force"
if not exist "!TMP_OC!\extract\opencode.exe" (
    for /r "!TMP_OC!\extract" %%F in (opencode.exe) do (
        copy /Y "%%F" "!OPENCODE_BIN!\opencode.exe" >nul
        goto :oc_copied
    )
    echo [ERROR] opencode.exe not in archive
    exit /b 1
)
copy /Y "!TMP_OC!\extract\opencode.exe" "!OPENCODE_BIN!\opencode.exe" >nul
:oc_copied

if exist "!SCRIPT_DIR!\packaging\windows\opencode.json" (
    copy /Y "!SCRIPT_DIR!\packaging\windows\opencode.json" "!OPENCODE_HOME!\opencode.json" >nul
) else (
    > "!OPENCODE_HOME!\opencode.json" echo {"$schema":"https://opencode.ai/config.json","plugin":["oh-my-opencode"]}
)
if exist "!SCRIPT_DIR!\packaging\windows\oh-my-opencode.json" (
    copy /Y "!SCRIPT_DIR!\packaging\windows\oh-my-opencode.json" "!OPENCODE_HOME!\oh-my-opencode.json" >nul
)
if exist "!SCRIPT_DIR!\packaging\windows\package.json" (
    copy /Y "!SCRIPT_DIR!\packaging\windows\package.json" "!OPENCODE_HOME!\package.json" >nul
) else (
    > "!OPENCODE_HOME!\package.json" echo {"dependencies":{"oh-my-opencode":"!OH_MY_OPENCODE_VERSION!"}}
)

echo   Fetching glab v!GLAB_VERSION! ...
set "GLAB_ZIP=!TMP_OC!\glab.zip"
set "GLAB_URL=https://gitlab.com/api/v4/projects/gitlab-org%%2Fcli/packages/generic/glab/!GLAB_VERSION!/glab_!GLAB_VERSION!_windows_amd64.zip"
powershell -NoProfile -Command "Invoke-WebRequest -Uri '!GLAB_URL!' -OutFile '!GLAB_ZIP!' -UseBasicParsing"
if not errorlevel 1 (
    powershell -NoProfile -Command "Expand-Archive -Path '!GLAB_ZIP!' -DestinationPath '!TMP_OC!\glab' -Force"
    for /r "!TMP_OC!\glab" %%F in (glab.exe) do (
        copy /Y "%%F" "!OPENCODE_BIN!\glab.exe" >nul
        goto :glab_done
    )
)
:glab_done

where npm >nul 2>&1
if errorlevel 1 (
    echo [WARNING] npm not found — plugin not installed. Use the CI zip instead.
) else (
    pushd "!OPENCODE_HOME!"
    call npm install --omit=dev --no-fund --no-audit "oh-my-opencode@!OH_MY_OPENCODE_VERSION!"
    popd
)

rmdir /s /q "!TMP_OC!" 2>nul
exit /b 0
