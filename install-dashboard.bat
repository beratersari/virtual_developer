@echo off
REM =============================================================================
REM JIRA Virtual Developer - Dashboard-only installer (backend + frontend)
REM =============================================================================
REM Installs ONLY:
REM   - Python .venv + requirements (offline wheels when vendor\ present)
REM   - start.bat / start-backend.bat / start-frontend.bat
REM   - .env from .env.example (if missing)
REM   - cli.py init
REM
REM Does NOT install or touch OpenCode / oh-my-openagent / glab / PATH.
REM Use when you already have OpenCode on this machine (install-backends.bat
REM rewrites %%USERPROFILE%%\.opencode — skip that with this script).
REM
REM Agents still need `opencode` on PATH at runtime (your existing install).
REM
REM IMPORTANT (cmd.exe): never write unescaped "->" in echo lines.
REM =============================================================================

setlocal EnableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
set "VENDOR_DIR=%SCRIPT_DIR%\vendor"
set "VENV_DIR=%SCRIPT_DIR%\.venv"
cd /d "%SCRIPT_DIR%"

echo ========================================
echo   Virtual Developer - Dashboard install
echo   Backend + frontend only ^(no OpenCode^)
echo ========================================
echo.
echo Install root : %SCRIPT_DIR%
echo OpenCode     : NOT installed by this script ^(use existing install^)
echo.

REM ---------------------------------------------------------------------------
REM Prerequisites: Python 3.10+ (64-bit)
REM ---------------------------------------------------------------------------
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python is not installed or not on PATH.
    echo Install Python 3.10+ ^(64-bit^) from https://www.python.org/downloads/
    echo Enable "Add python.exe to PATH" during setup, then re-run this script.
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

if exist "%VENDOR_DIR%\SUPPORTED_PYTHON.txt" (
    python -c "import sys,pathlib; p=pathlib.Path(r'%VENDOR_DIR%')/'SUPPORTED_PYTHON.txt'; lines=[l.strip() for l in p.read_text(encoding='utf-8',errors='ignore').splitlines() if l.strip() and not l.strip().startswith('#')]; ver=f'{sys.version_info.major}.{sys.version_info.minor}'; ok=ver in lines; print('Supported in this package:', ', '.join(lines)); print('Your Python minor:', ver); raise SystemExit(0 if ok else 1)"
    if errorlevel 1 (
        echo.
        echo [ERROR] Your Python is not supported by the bundled offline wheels.
        echo Install a supported 64-bit version from the list above, then re-run.
        call :maybe_pause
        exit /b 1
    )
    echo [OK] Python minor is in vendor\SUPPORTED_PYTHON.txt
)

REM ---------------------------------------------------------------------------
REM Step 1: Python venv
REM ---------------------------------------------------------------------------
echo.
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

REM ---------------------------------------------------------------------------
REM Step 2: Python packages
REM ---------------------------------------------------------------------------
echo.
echo Step 2: Installing Python packages...
if exist "%VENDOR_DIR%\python-wheels" (
    echo Using offline wheels from vendor\python-wheels ^(no network^)...
    "%VENV_PIP%" install --upgrade pip --no-index --find-links="%VENDOR_DIR%\python-wheels" 2>nul
    "%VENV_PIP%" install --no-index --find-links="%VENDOR_DIR%\python-wheels" -r "%SCRIPT_DIR%\requirements.txt"
    if errorlevel 1 (
        echo [ERROR] Offline wheel install failed.
        echo Check Python version ^(vendor\SUPPORTED_PYTHON.txt^) and 64-bit AMD64.
        call :maybe_pause
        exit /b 1
    )
) else (
    echo [INFO] No vendor\python-wheels — installing from PyPI ^(needs internet^)...
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
"%VENV_PY%" cli.py init
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
    echo           Install OpenCode with install-backends.bat, Codex with install-codex.bat.
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
echo   Dashboard install complete
echo ========================================
echo.
echo Python venv : %VENV_DIR%
echo OpenCode    : skipped ^(use your existing install^)
echo.
echo Next steps:
echo   1. Edit .env with Jira / GitLab settings
echo   2. Start the product:
echo        start-backend.bat    API + SPA on http://127.0.0.1:8080/
echo        start-frontend.bat   UI on http://127.0.0.1:5173/
echo        start.bat            both ^(backend then frontend^)
echo.
echo Agent workers:
echo        install-backends.bat   OpenCode + Codex
echo        install-codex.bat      Codex only
echo.
call :maybe_pause
exit /b 0

:maybe_pause
if /i "%VD_NONINTERACTIVE%"=="1" exit /b 0
pause
exit /b 0
