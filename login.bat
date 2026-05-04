@echo off
REM Run login.py: opens a real browser; finish Discord login manually then it auto-closes.
chcp 65001 >nul
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    set "PY=python"
)

%PY% login.py
pause
