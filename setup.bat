@echo off
REM One-shot environment setup: venv + packages + Playwright Chromium
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] python not found in PATH. Install Python 3.10+ first.
    pause
    exit /b 1
)

if not exist ".venv\" (
    echo [1/3] Creating venv ...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] venv creation failed.
        pause
        exit /b 1
    )
)

echo [2/3] Installing Python packages ...
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] pip install failed.
    pause
    exit /b 1
)

echo [3/3] Installing Playwright Chromium ...
python -m playwright install chromium
if errorlevel 1 (
    echo [ERROR] Playwright install failed.
    pause
    exit /b 1
)

if not exist "config.json" (
    echo.
    echo [INFO] config.json not found, copying from config.example.json
    copy /Y config.example.json config.json >nul
    echo        Edit config.json to fill in guild_id / channel_id / notify_user_id.
)

echo.
echo === Done. Next: run login.bat to log in to Discord. ===
pause
