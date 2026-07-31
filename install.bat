@echo off
REM JIRA Virtual Developer - Installation Script for Windows
REM This script sets up the environment for testing without JIRA

setlocal EnableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

echo ========================================
echo   JIRA Virtual Developer - Installer
echo ========================================
echo.

REM Check if Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python is not installed
    echo Please install Python 3.8 or higher from https://python.org
    pause
    exit /b 1
)

for /f "tokens=*" %%a in ('python --version 2^>^&1') do set PYTHON_VERSION=%%a
echo [OK] Python found: %PYTHON_VERSION%

REM Check if pip is installed
pip --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] pip is not installed
    pause
    exit /b 1
)
echo [OK] pip found

REM Check if npm is installed
npm --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] npm is not installed
    echo Please install Node.js from https://nodejs.org
    pause
    exit /b 1
)
echo [OK] npm found

echo.
echo Step 1: Installing Python dependencies...
cd /d "%SCRIPT_DIR%"
pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo [WARNING] Some Python dependencies may have failed, continuing...
) else (
    echo [OK] Python dependencies installed
)

echo.
echo Step 2: Installing sample project dependencies...
cd /d "%SCRIPT_DIR%\sample_project"
REM Create virtual environment for sample project
if not exist ".venv" (
    python -m venv .venv
    echo [OK] Created virtual environment for sample project
)
REM Install sample project in dev mode
.venv\Scripts\pip install -e ".[test]" --quiet
if errorlevel 1 (
    echo [WARNING] Sample project dependencies may have failed, continuing...
) else (
    echo [OK] Sample project dependencies installed
)

echo.
echo Step 3: Installing OpenCode CLI...
where opencode >nul 2>&1
if errorlevel 1 (
    echo Installing OpenCode CLI via npm...
    npm install -g opencode
    if errorlevel 1 (
        echo [WARNING] OpenCode CLI installation failed. You may need to install manually.
    ) else (
        echo [OK] OpenCode CLI installed
    )
) else (
    echo [OK] OpenCode CLI already installed
)

echo.
echo Step 4: Installing oh-my-opencode...
npm install -g oh-my-opencode --silent
if errorlevel 1 (
    echo [WARNING] oh-my-opencode global install may have failed, continuing...
) else (
    echo [OK] oh-my-opencode installed globally
)

REM Install oh-my-opencode as opencode plugin
set "OPENCODE_DIR=%USERPROFILE%\.opencode"
if not exist "%OPENCODE_DIR%" mkdir "%OPENCODE_DIR%"
cd /d "%OPENCODE_DIR%"

REM Create package.json if it doesn't exist
if not exist "package.json" (
    echo {"dependencies": {}} > package.json
)

REM Install plugin
npm install oh-my-opencode --silent
if errorlevel 1 (
    echo [WARNING] Plugin install may have failed, continuing...
) else (
    echo [OK] oh-my-opencode plugin installed
)

echo.
echo Step 5: Initializing JIRA Virtual Developer...
cd /d "%SCRIPT_DIR%"
python cli.py init
if errorlevel 1 (
    echo [WARNING] Initialization may have had issues, but continuing...
) else (
    echo [OK] JIRA Virtual Developer initialized
)

echo.
echo ========================================
echo   Installation Complete!
echo ========================================
echo.
echo Next Steps:
echo.
echo 1. Test the sample project:
echo    cd "%SCRIPT_DIR%"
echo    python cli.py test-issue ^
echo        --title "Fix calculator bugs" ^
echo        --description "Fix all bugs in calculator/calc.py"
echo.
echo 2. Run tests on sample project:
echo    cd "%SCRIPT_DIR%\sample_project"
echo    .venv\Scripts\pytest -v
echo.
echo 3. To use with JIRA (optional):
echo    Edit .env file with your JIRA credentials:
echo    - JIRA_HOST=https://yourcompany.atlassian.net
echo    - JIRA_USERNAME=your-email@example.com
echo    - JIRA_API_TOKEN=your-api-token
echo.
echo Note: You may need to restart your terminal for all changes to take effect.
echo.
pause
