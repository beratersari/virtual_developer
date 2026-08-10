@echo off
REM =============================================================================
REM JIRA Virtual Developer - Dashboard install using SYSTEM Python (no venv)
REM =============================================================================
REM Same as install-dashboard.bat EXCEPT:
REM   - Does NOT create or use .venv
REM   - Uses `python` already on PATH
REM   - pip installs requirements into that interpreter
REM
REM Use when this machine already has a working Python 3.10+ with (or ready
REM for) the project deps. start-backend.bat / start-frontend.bat will use
REM that same `python` when .venv is absent.
REM
REM Does NOT install or touch OpenCode / oh-my-openagent / glab / PATH.
REM
REM IMPORTANT (cmd.exe): never write unescaped ">" in echo lines.
REM =============================================================================

setlocal EnableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
set "VENDOR_DIR=%SCRIPT_DIR%\vendor"
cd /d "%SCRIPT_DIR%"

echo ========================================
echo   Virtual Developer - Dashboard install
echo   System Python ^(no venv^)
echo ========================================
echo.
echo Install root : %SCRIPT_DIR%
echo Python       : system PATH ^(no .venv will be created^)
echo OpenCode     : NOT installed by this script
echo.

REM ---------------------------------------------------------------------------
REM Prerequisites: Python 3.10+ on PATH
REM ---------------------------------------------------------------------------
where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] python is not on PATH.
    echo Install Python 3.10+ ^(64-bit^) and enable "Add python.exe to PATH",
    echo or use install-dashboard.bat to create a project .venv instead.
    call :maybe_pause
    exit /b 1
)

for /f "tokens=*" %%a in ('python --version 2^>^&1') do set "PYTHON_VERSION=%%a"
for /f "tokens=*" %%a in ('where python') do (
    set "SYS_PY=%%a"
    goto :py_where_done
)
:py_where_done
echo [OK] %PYTHON_VERSION%
echo [OK] %SYS_PY%

python -c "import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python 3.10 or newer is required.
    echo Found: %PYTHON_VERSION%
    call :maybe_pause
    exit /b 1
)

if exist "%VENDOR_DIR%\SUPPORTED_PYTHON.txt" (
    python -c "import sys,pathlib; p=pathlib.Path(r'%VENDOR_DIR%')/'SUPPORTED_PYTHON.txt'; lines=[l.strip() for l in p.read_text(encoding='utf-8',errors='ignore').splitlines() if l.strip() and not l.strip().startswith('#')]; ver=f'{sys.version_info.major}.{sys.version_info.minor}'; ok=ver in lines; print('Supported in this package:', ', '.join(lines)); print('Your Python minor:', ver); raise SystemExit(0 if ok else 1)"
    if errorlevel 1 (
        echo.
        echo [ERROR] Your Python is not supported by the bundled offline wheels.
        echo Install a supported 64-bit version, or use install-dashboard.bat
        echo to isolate deps in .venv.
        call :maybe_pause
        exit /b 1
    )
    echo [OK] Python minor is in vendor\SUPPORTED_PYTHON.txt
)

REM ---------------------------------------------------------------------------
REM Step 1: skip venv
REM ---------------------------------------------------------------------------
echo.
echo Step 1: Virtual environment...
echo [OK] Skipped — using system python ^(no .venv^)
if exist "%SCRIPT_DIR%\.venv\Scripts\python.exe" (
    echo [INFO] A project .venv already exists; this script will not use it.
    echo        start-*.bat prefer .venv when present. Rename/remove .venv
    echo        if you want those launchers to use system python too.
)

REM ---------------------------------------------------------------------------
REM Step 2: Python packages into the system interpreter
REM ---------------------------------------------------------------------------
echo.
echo Step 2: Installing Python packages into system python...
if exist "%VENDOR_DIR%\python-wheels" (
    echo Using offline wheels from vendor\python-wheels ^(no network^)...
    python -m pip install --upgrade pip --no-index --find-links="%VENDOR_DIR%\python-wheels" 2>nul
    python -m pip install --no-index --find-links="%VENDOR_DIR%\python-wheels" -r "%SCRIPT_DIR%\requirements.txt"
    if errorlevel 1 (
        echo [ERROR] Offline wheel install into system python failed.
        echo Check Python version ^(vendor\SUPPORTED_PYTHON.txt^) and 64-bit AMD64.
        echo If pip is blocked, try: python -m pip install --user ...
        call :maybe_pause
        exit /b 1
    )
) else (
    echo [INFO] No vendor\python-wheels — installing from PyPI ^(needs internet^)...
    python -m pip install --upgrade pip --quiet
    python -m pip install -r "%SCRIPT_DIR%\requirements.txt"
    if errorlevel 1 (
        echo [ERROR] Python dependency install failed.
        echo If this Python is protected, retry with:
        echo   python -m pip install --user -r requirements.txt
        echo Or use install-dashboard.bat to create a project .venv.
        call :maybe_pause
        exit /b 1
    )
)
echo [OK] Python dependencies installed into system python

REM ---------------------------------------------------------------------------
REM Step 3: Product launchers ^(backend / frontend / both^)
REM ---------------------------------------------------------------------------
echo.
echo Step 3: Product start scripts...
for %%F in (start.bat start-backend.bat start-frontend.bat start-opencode.bat start-opencode-serve.bat) do (
    if exist "%SCRIPT_DIR%\packaging\windows\%%F" (
        copy /Y "%SCRIPT_DIR%\packaging\windows\%%F" "%SCRIPT_DIR%\%%F" >nul
        echo [OK] %%F
    ) else if exist "%SCRIPT_DIR%\%%F" (
        echo [OK] %%F ^(already at package root^)
    ) else (
        echo [WARNING] %%F not found
    )
)

if exist "%SCRIPT_DIR%\web\dist\index.html" (
    echo [OK] ops dashboard SPA present: web\dist
) else (
    echo [WARNING] web\dist\index.html missing — UI will not load until SPA is built
    echo           CI offline zips include web\dist. From source: cd web ^&^& npm ci ^&^& npm run build
)

REM ---------------------------------------------------------------------------
REM Step 4: .env + project init
REM ---------------------------------------------------------------------------
echo.
echo Step 4: Project configuration...
if not exist "%SCRIPT_DIR%\.env" (
    if exist "%SCRIPT_DIR%\.env.example" (
        copy /Y "%SCRIPT_DIR%\.env.example" "%SCRIPT_DIR%\.env" >nul
        echo [OK] Created .env from .env.example — edit credentials before start
    ) else (
        echo [WARNING] .env.example missing; create .env manually
    )
) else (
    echo [OK] .env already exists ^(left unchanged^)
)

echo.
echo Step 5: Initializing project ^(cli.py init^)...
cd /d "%SCRIPT_DIR%"
python cli.py init
if errorlevel 1 (
    echo [WARNING] cli.py init reported issues; continuing...
) else (
    echo [OK] Project initialized
)

REM ---------------------------------------------------------------------------
REM Step 6: Existing OpenCode check ^(informational only^)
REM ---------------------------------------------------------------------------
echo.
echo Step 6: Checking for existing OpenCode on PATH...
where opencode >nul 2>&1
if errorlevel 1 (
    echo [WARNING] `opencode` not found on PATH.
    echo           Dashboard and poller can still run; agent jobs will fail until OpenCode is available.
    echo           Install OpenCode yourself, or run full install.bat once for a bundled OpenCode.
) else (
    for /f "tokens=*" %%a in ('where opencode 2^>^&1') do (
        echo [OK] Found opencode: %%a
        goto :opencode_found_done
    )
)
:opencode_found_done

where glab >nul 2>&1
if errorlevel 1 (
    echo [INFO] `glab` not on PATH — MR creation may fall back to GitLab API if configured.
) else (
    for /f "tokens=*" %%a in ('where glab 2^>^&1') do (
        echo [OK] Found glab: %%a
        goto :glab_found_done
    )
)
:glab_found_done

echo.
echo ========================================
echo   Dashboard install complete ^(system python^)
echo ========================================
echo.
echo Python      : %SYS_PY%
echo Virtual env : not created
echo OpenCode    : skipped ^(use your existing install^)
echo.
echo Next steps:
echo   1. Edit .env with Jira / GitLab settings
echo   2. Start the product:
echo        start-backend.bat    API + SPA on http://127.0.0.1:8080/
echo        start-frontend.bat   UI on http://127.0.0.1:5173/
echo        start.bat            both ^(backend then frontend^)
echo.
echo Isolated install ^(project .venv^):
echo        install-dashboard.bat
echo Full offline install ^(OpenCode + plugins + glab^):
echo        install.bat
echo.
call :maybe_pause
exit /b 0

:maybe_pause
if /i "%VD_NONINTERACTIVE%"=="1" exit /b 0
pause
exit /b 0
