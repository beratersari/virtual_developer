@echo off
REM =============================================================================
REM JIRA Virtual Developer - Windows Installer
REM =============================================================================
REM Offline when run from a CI-built distribution zip (vendor\ present):
REM   - Extracts vendor\opencode-home.zip into %%USERPROFILE%%\.opencode
REM     (single archive avoids long-path / slow node_modules extract of the outer zip)
REM   - Creates .venv and installs Python deps from vendor\python-wheels (3.10+)
REM
REM Online fallback only if VD_ALLOW_ONLINE=1 and vendor is missing.
REM
REM IMPORTANT (cmd.exe): never write unescaped "->" in echo lines — ">" is redirect
REM and will overwrite the path on the right (e.g. opencode.json / opencode.exe).
REM =============================================================================

setlocal EnableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
set "VENDOR_DIR=%SCRIPT_DIR%\vendor"
set "VENV_DIR=%SCRIPT_DIR%\.venv"
set "VERSIONS_FILE=%SCRIPT_DIR%\packaging\windows\versions.env"
if exist "%VENDOR_DIR%\versions.env" set "VERSIONS_FILE=%VENDOR_DIR%\versions.env"

REM ---------------------------------------------------------------------------
REM OpenCode install root: always %%USERPROFILE%%\.opencode (product default).
REM Override only via VD_OPENCODE_ROOT (advanced / CI short-path testing).
REM ---------------------------------------------------------------------------
if defined VD_OPENCODE_ROOT (
    set "OPENCODE_HOME=%VD_OPENCODE_ROOT%"
) else (
    set "OPENCODE_HOME=%USERPROFILE%\.opencode"
)
if not exist "%OPENCODE_HOME%" mkdir "%OPENCODE_HOME%" 2>nul
if not exist "%OPENCODE_HOME%" (
    echo [ERROR] Cannot create OpenCode home: %OPENCODE_HOME%
    call :maybe_pause
    exit /b 1
)
set "OPENCODE_BIN=%OPENCODE_HOME%\bin"
set "USER_OC_HOME=%USERPROFILE%\.opencode"
set "LEGACY_OC_HOME=%SystemDrive%\vd\opencode"
set "LEGACY_OC_HOME2=%LOCALAPPDATA%\vd\opencode"

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
    call :maybe_pause
    exit /b 1
)

for /f "tokens=*" %%a in ('python --version 2^>^&1') do set "PYTHON_VERSION=%%a"
echo [OK] %PYTHON_VERSION%

python -c "import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python 3.10 or newer is required.
    echo Found: %PYTHON_VERSION%
    call :maybe_pause
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
        call :maybe_pause
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
        call :maybe_pause
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
        call :maybe_pause
        exit /b 1
    )
) else if "%HAS_VENDOR%"=="1" (
    echo [ERROR] vendor\ exists but python-wheels is missing. Broken distribution zip.
    call :maybe_pause
    exit /b 1
) else (
    echo [INFO] No offline wheels — installing from PyPI ^(needs internet^)...
    "%VENV_PIP%" install --upgrade pip --quiet
    "%VENV_PIP%" install -r "%SCRIPT_DIR%\requirements.txt"
    if errorlevel 1 (
        echo [ERROR] Python dependency install failed.
        call :maybe_pause
        exit /b 1
    )
)
echo [OK] Python dependencies installed into .venv

REM ---------------------------------------------------------------------------
REM Step 3: Deploy OpenCode home under %%USERPROFILE%%\.opencode (or VD_OPENCODE_ROOT)
REM ---------------------------------------------------------------------------
echo.
echo Step 3: Installing OpenCode under %OPENCODE_HOME% ...

REM Full cleanup so re-running install.bat is enough (no manual rmdir/del needed):
REM   - legacy C:\vd\opencode / LocalAppData\vd\opencode
REM   - %%USERPROFILE%%\.opencode (junction or real tree)
REM   - bad global config at %%USERPROFILE%%\.config\opencode\opencode.json
REM   - stale PATH entries pointing at old bin dirs
call :clean_previous_opencode

if not exist "%OPENCODE_HOME%" mkdir "%OPENCODE_HOME%"
if not exist "%OPENCODE_BIN%" mkdir "%OPENCODE_BIN%"

if exist "%VENDOR_DIR%\opencode-home.zip" (
    echo Extracting vendor\opencode-home.zip -^> %OPENCODE_HOME%
    echo ^(single archive: avoids long Windows paths / slow outer-zip extract^)
    call :extract_opencode_home_zip
    if errorlevel 1 (
        echo [ERROR] Failed to extract OpenCode home archive.
        call :maybe_pause
        exit /b 1
    )
) else if exist "%VENDOR_DIR%\opencode-home\bin\opencode.exe" (
    echo Copying expanded vendor\opencode-home ^(legacy layout^)...
    robocopy "%VENDOR_DIR%\opencode-home" "%OPENCODE_HOME%" /E /NFL /NDL /NJH /NJS /nc /ns /np >nul
    if errorlevel 8 (
        echo [ERROR] robocopy failed copying opencode-home
        call :maybe_pause
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
        call :maybe_pause
        exit /b 1
    )
    echo Online fallback enabled via VD_ALLOW_ONLINE=1 ...
    call :install_opencode_online
    if errorlevel 1 (
        echo [ERROR] Online OpenCode install failed.
        call :maybe_pause
        exit /b 1
    )
)

if not exist "%OPENCODE_BIN%\opencode.exe" (
    echo [ERROR] opencode.exe missing at %OPENCODE_BIN%\opencode.exe
    call :maybe_pause
    exit /b 1
)
echo [OK] opencode.exe at %OPENCODE_BIN%\opencode.exe

REM Antivirus may replace opencode.exe with a ~21-byte stub after extract.
REM Restore from flat vendor\bin backup (short path, not nested in zip).
call :ensure_opencode_binary
if errorlevel 1 (
    echo.
    echo [ERROR] Could not install a healthy 64-bit opencode.exe.
    echo Windows Defender often quarantines large EXEs right after extract.
    echo That shows up as "not compatible with 64-bit versions of Windows".
    echo.
    echo Fix:
    echo   1. Windows Security -^> Virus ^& threat protection -^> Protection history
    echo      Allow/restore opencode.exe
    echo   2. Add folder exclusion: %OPENCODE_HOME%
    echo   3. Also exclude: %VENDOR_DIR%\bin
    echo   4. rmdir /s /q "%OPENCODE_HOME%"  ^&  re-run install.bat
    call :maybe_pause
    exit /b 1
)

echo Smoke-testing opencode --version ...
"%OPENCODE_BIN%\opencode.exe" --version
if errorlevel 1 (
    echo [ERROR] opencode.exe failed to start after restore attempts.
    call :maybe_pause
    exit /b 1
)
echo [OK] opencode runs ^(64-bit AMD64^)

REM If install root is not %%USERPROFILE%%\.opencode, junction for tools that look there.
call :link_user_opencode_home

REM Seed / repair configs (never use echo with "->" — that overwrites the file via redirect)
call :ensure_opencode_configs
if errorlevel 1 (
    echo [ERROR] OpenCode config under %OPENCODE_HOME% is missing or invalid JSON.
    call :maybe_pause
    exit /b 1
)

if exist "%OPENCODE_HOME%\opencode.json" (
    echo [OK] config at %OPENCODE_HOME%\opencode.json
) else (
    echo [WARNING] opencode.json not found under %OPENCODE_HOME%
)

if exist "%OPENCODE_HOME%\node_modules\oh-my-opencode" (
    echo [OK] plugin oh-my-opencode in %OPENCODE_HOME%\node_modules
) else (
    echo [WARNING] oh-my-opencode plugin not found under node_modules
)

REM Mirror config into %USERPROFILE%\.config\opencode for OpenCode global discovery
call :mirror_opencode_config
if errorlevel 1 (
    echo [ERROR] Failed to install valid config at %%USERPROFILE%%\.config\opencode
    call :maybe_pause
    exit /b 1
)

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
REM Step 5: User PATH (single install: only OPENCODE_BIN; drop legacy short roots)
REM ---------------------------------------------------------------------------
echo.
echo Step 5: Adding OpenCode bin to user PATH...
call :remove_user_path_entry "%LEGACY_OC_HOME%\bin"
call :remove_user_path_entry "%LEGACY_OC_HOME2%\bin"
REM If home is user .opencode, do not leave a second PATH entry for a short root
if /i not "%OPENCODE_HOME%"=="%USER_OC_HOME%" (
    REM short/override root is primary; still ensure user .opencode\bin is not a stale second copy on PATH
    call :remove_user_path_entry "%USER_OC_HOME%\bin"
)
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
echo   global cfg  : %USERPROFILE%\.config\opencode\opencode.json
echo Python venv   : %VENV_DIR%
echo.
echo Next steps:
echo   1. Edit .env with your Jira / GitLab settings
echo   2. Open a NEW terminal ^(so PATH updates apply^)
echo   3. Verify a single OpenCode install:
echo        where opencode
echo        ^(should show only %OPENCODE_BIN%\opencode.exe^)
echo   4. Activate venv and start:
echo        cd /d "%SCRIPT_DIR%"
echo        .venv\Scripts\activate
echo        python cli.py start
echo.
echo Note: Restart terminals so OpenCode bin is on PATH:
echo        %OPENCODE_BIN%
echo.
call :maybe_pause
exit /b 0

REM =============================================================================
REM Subroutines
REM =============================================================================

:maybe_pause
if /i "%VD_NONINTERACTIVE%"=="1" exit /b 0
pause
exit /b 0

:ensure_opencode_binary
REM Ensure %OPENCODE_BIN%\opencode.exe is a full ~170MB AMD64 binary.
REM Prefer re-copy from vendor\bin if AV ate the extracted copy.
if not exist "%OPENCODE_BIN%" mkdir "%OPENCODE_BIN%"
set "BACKUP_OC=%VENDOR_DIR%\bin\opencode.exe"
set "BACKUP_GL=%VENDOR_DIR%\bin\glab.exe"
set "TARGET_OC=%OPENCODE_BIN%\opencode.exe"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop';" ^
  "$target='%TARGET_OC%'; $backup='%BACKUP_OC%'; $glabTarget='%OPENCODE_BIN%\glab.exe'; $glabBackup='%BACKUP_GL%';" ^
  "function Ok($p){ return (Test-Path -LiteralPath $p) -and ((Get-Item -LiteralPath $p).Length -ge 10MB) };" ^
  "for ($i=1; $i -le 5; $i++) {" ^
  "  if (Ok $target) { Write-Host ('OK opencode.exe attempt {0}: {1:N1} MB' -f $i, ((Get-Item $target).Length/1MB)); break };" ^
  "  Write-Host ('Restore attempt {0}: target size={1}' -f $i, $(if(Test-Path $target){(Get-Item $target).Length}else{'missing'}));" ^
  "  if (-not (Test-Path -LiteralPath $backup)) { throw 'Missing backup vendor\\bin\\opencode.exe' };" ^
  "  if (-not (Ok $backup)) { throw ('Backup also unhealthy: {0} bytes' -f (Get-Item $backup).Length) };" ^
  "  New-Item -ItemType Directory -Path (Split-Path $target) -Force | Out-Null;" ^
  "  Copy-Item -LiteralPath $backup -Destination $target -Force;" ^
  "  Unblock-File -LiteralPath $target -ErrorAction SilentlyContinue;" ^
  "  if (Test-Path -LiteralPath $glabBackup) { Copy-Item -LiteralPath $glabBackup -Destination $glabTarget -Force; Unblock-File -LiteralPath $glabTarget -ErrorAction SilentlyContinue };" ^
  "  Start-Sleep -Seconds 1;" ^
  "};" ^
  "if (-not (Ok $target)) { exit 1 };" ^
  "exit 0"
if errorlevel 1 exit /b 1
exit /b 0

:clean_previous_opencode
REM Idempotent wipe of previous OpenCode installs + bad global config.
REM Safe to re-run; does not touch this dist's .venv or project files.
echo   Cleaning previous OpenCode installs and stale config...

REM Drop legacy / dual PATH entries first (OPENCODE_BIN is re-added in Step 5)
call :remove_user_path_entry "%LEGACY_OC_HOME%\bin"
call :remove_user_path_entry "%LEGACY_OC_HOME2%\bin"
call :remove_user_path_entry "%USER_OC_HOME%\bin"
if defined OPENCODE_BIN (
    call :remove_user_path_entry "%OPENCODE_BIN%"
)

REM Remove managed global config files (do not wipe entire .config — auth may live there)
set "OC_CONFIG_DIR=%USERPROFILE%\.config\opencode"
if exist "%OC_CONFIG_DIR%\opencode.json" (
    echo   Removing %OC_CONFIG_DIR%\opencode.json
    del /f /q "%OC_CONFIG_DIR%\opencode.json" >nul 2>&1
)
if exist "%OC_CONFIG_DIR%\oh-my-opencode.json" (
    del /f /q "%OC_CONFIG_DIR%\oh-my-opencode.json" >nul 2>&1
)
if exist "%OC_CONFIG_DIR%\package.json" (
    del /f /q "%OC_CONFIG_DIR%\package.json" >nul 2>&1
)

REM Remove install roots (junction or real tree). Order: user home, target, legacy.
call :force_remove_dir "%USER_OC_HOME%"
if /i not "%OPENCODE_HOME%"=="%USER_OC_HOME%" call :force_remove_dir "%OPENCODE_HOME%"
if /i not "%LEGACY_OC_HOME%"=="%OPENCODE_HOME%" if /i not "%LEGACY_OC_HOME%"=="%USER_OC_HOME%" (
    call :force_remove_dir "%LEGACY_OC_HOME%"
)
if /i not "%LEGACY_OC_HOME2%"=="%OPENCODE_HOME%" if /i not "%LEGACY_OC_HOME2%"=="%USER_OC_HOME%" (
    call :force_remove_dir "%LEGACY_OC_HOME2%"
)
REM Drop empty C:\vd shell left by legacy short-path installs
if exist "%SystemDrive%\vd" (
    dir /a /b "%SystemDrive%\vd" 2>nul | findstr /r "." >nul
    if errorlevel 1 (
        call :force_remove_dir "%SystemDrive%\vd"
    )
)

echo   [OK] Previous OpenCode locations cleaned
exit /b 0

:force_remove_dir
REM Remove a directory tree or junction. Arg1 = full path.
set "RM_TARGET=%~1"
if not defined RM_TARGET exit /b 0
if not exist "%RM_TARGET%" exit /b 0
echo   Removing %RM_TARGET%
REM Junction/symlink: rmdir without /s unlinks; real tree needs /s /q
fsutil reparsepoint query "%RM_TARGET%" >nul 2>&1
if not errorlevel 1 (
    rmdir "%RM_TARGET%" 2>nul
) else (
    rmdir /s /q "%RM_TARGET%" 2>nul
)
if exist "%RM_TARGET%" (
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
      "Remove-Item -LiteralPath $env:RM_TARGET -Recurse -Force -ErrorAction SilentlyContinue"
)
if exist "%RM_TARGET%" (
    echo   [WARNING] Could not fully remove %RM_TARGET% — close programs using it and re-run
)
exit /b 0

:link_user_opencode_home
REM Only when OPENCODE_HOME is NOT already %%USERPROFILE%%\.opencode (VD_OPENCODE_ROOT).
set "USER_OC=%USERPROFILE%\.opencode"
if /i "%OPENCODE_HOME%"=="%USER_OC%" exit /b 0
if exist "%USER_OC%" (
    REM Remove previous install/junction so we can re-link
    rmdir "%USER_OC%" 2>nul
    if exist "%USER_OC%" rmdir /s /q "%USER_OC%" 2>nul
)
mklink /J "%USER_OC%" "%OPENCODE_HOME%" >nul 2>&1
if errorlevel 1 (
    echo [WARNING] Could not junction %%USERPROFILE%%\.opencode to %OPENCODE_HOME%
    echo           OpenCode still works via PATH: %OPENCODE_BIN%
) else (
    echo [OK] Junction: %%USERPROFILE%%\.opencode =^> %OPENCODE_HOME%
)
exit /b 0

:ensure_opencode_configs
REM Ensure opencode.json / oh-my-opencode.json / package.json exist and are valid JSON.
REM Prefer files already in OPENCODE_HOME (from opencode-home.zip); fall back to packaging templates.
set "PKG_OC=%SCRIPT_DIR%\packaging\windows"
if not exist "%OPENCODE_HOME%\opencode.json" (
    if exist "%PKG_OC%\opencode.json" (
        copy /Y "%PKG_OC%\opencode.json" "%OPENCODE_HOME%\opencode.json" >nul
    )
)
if not exist "%OPENCODE_HOME%\oh-my-opencode.json" (
    if exist "%PKG_OC%\oh-my-opencode.json" (
        copy /Y "%PKG_OC%\oh-my-opencode.json" "%OPENCODE_HOME%\oh-my-opencode.json" >nul
    )
)
if not exist "%OPENCODE_HOME%\package.json" (
    if exist "%PKG_OC%\package.json" (
        copy /Y "%PKG_OC%\package.json" "%OPENCODE_HOME%\package.json" >nul
    )
)
if not exist "%OPENCODE_HOME%\opencode.json" (
    echo [ERROR] opencode.json missing under %OPENCODE_HOME%
    exit /b 1
)
REM If config was corrupted (e.g. old installer wrote echo output via "->"), re-seed from packaging.
python -c "import json,sys; p=sys.argv[1]; json.load(open(p,encoding='utf-8-sig'))" "%OPENCODE_HOME%\opencode.json" >nul 2>&1
if errorlevel 1 (
    echo [WARNING] opencode.json is not valid JSON — restoring from package template
    if exist "%PKG_OC%\opencode.json" (
        copy /Y "%PKG_OC%\opencode.json" "%OPENCODE_HOME%\opencode.json" >nul
    ) else (
        > "%OPENCODE_HOME%\opencode.json" echo {"$schema":"https://opencode.ai/config.json","plugin":["oh-my-opencode"]}
    )
    python -c "import json,sys; p=sys.argv[1]; json.load(open(p,encoding='utf-8-sig'))" "%OPENCODE_HOME%\opencode.json" >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] Could not write valid opencode.json
        exit /b 1
    )
)
exit /b 0

:mirror_opencode_config
REM Mirror home configs into %%USERPROFILE%%\.config\opencode (OpenCode global discovery).
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
if not exist "%OC_CONFIG_DIR%\opencode.json" (
    echo [ERROR] Failed to mirror opencode.json to %OC_CONFIG_DIR%
    exit /b 1
)
python -c "import json,sys; p=sys.argv[1]; json.load(open(p,encoding='utf-8-sig'))" "%OC_CONFIG_DIR%\opencode.json" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Mirrored opencode.json is not valid JSON: %OC_CONFIG_DIR%\opencode.json
    exit /b 1
)
echo [OK] Mirrored valid config to %OC_CONFIG_DIR%
exit /b 0

:extract_opencode_home_zip
REM Extract vendor\opencode-home.zip into %OPENCODE_HOME% with long-path support.
REM Caller already ran :clean_previous_opencode (fresh dest dir).
set "OC_ZIP=%VENDOR_DIR%\opencode-home.zip"
if not exist "%OC_ZIP%" exit /b 1
if not exist "%OPENCODE_HOME%" mkdir "%OPENCODE_HOME%"

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
    exit /b 0
)
REM Prepend so our 64-bit opencode wins over any older install
if defined USER_PATH (
    setx PATH "%ADD_PATH%;%USER_PATH%" >nul
) else (
    setx PATH "%ADD_PATH%" >nul
)
if errorlevel 1 (
    echo [WARNING] Could not update user PATH via setx. Add manually at the FRONT of PATH:
    echo   %ADD_PATH%
) else (
    echo [OK] Prepended to user PATH: %ADD_PATH%
)
exit /b 0

:remove_user_path_entry
REM Drop a PATH segment (e.g. legacy C:\vd\opencode\bin) so only one OpenCode remains.
set "DROP_PATH=%~1"
if not defined DROP_PATH exit /b 0
set "USER_PATH="
for /f "tokens=2*" %%A in ('reg query "HKCU\Environment" /v Path 2^>nul') do set "USER_PATH=%%B"
if not defined USER_PATH exit /b 0
echo ;%USER_PATH%; | find /I ";%DROP_PATH%;" >nul
if errorlevel 1 exit /b 0
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$drop='%DROP_PATH%';" ^
  "$raw=[Environment]::GetEnvironmentVariable('Path','User');" ^
  "if ([string]::IsNullOrEmpty($raw)) { exit 0 };" ^
  "$parts=$raw -split ';' | Where-Object { $_ -and ($_.TrimEnd('\') -ne $drop.TrimEnd('\')) -and ($_.ToLowerInvariant() -ne $drop.ToLowerInvariant()) };" ^
  "$new=($parts -join ';').Trim(';');" ^
  "[Environment]::SetEnvironmentVariable('Path',$new,'User');" ^
  "Write-Host ('[OK] Removed from user PATH: {0}' -f $drop)"
exit /b 0

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
