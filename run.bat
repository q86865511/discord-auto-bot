@echo off
REM Run main.py. Exit code 42 = user pressed F to reboot; this script auto-relaunches.
chcp 65001 >nul
cd /d "%~dp0"

if not exist "config.json" (
    echo [ERROR] config.json not found. Run setup.bat first.
    pause
    exit /b 1
)

if not exist "storage_state.json" (
    echo [ERROR] storage_state.json not found. Run login.bat first.
    pause
    exit /b 1
)

if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    set "PY=python"
)

:loop
%PY% main.py
if "%ERRORLEVEL%"=="42" (
    echo.
    echo === Reboot requested, restarting in 3s ===
    timeout /t 3 /nobreak >nul
    goto loop
)

pause
