@echo off
REM Run main.py. Exit code 42 = user pressed F to reboot; this script auto-relaunches.
REM First-run setup (config wizard + Discord login) is handled by main.py itself,
REM so we don't pre-check config.json / storage_state.json here.
chcp 65001 >nul
cd /d "%~dp0"

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
