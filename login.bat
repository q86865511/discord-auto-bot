@echo off
REM ─ 啟動 login.py：開瀏覽器讓你手動登入 Discord 後存 storage_state.json ─
chcp 65001 >nul
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    set "PY=python"
)

%PY% login.py
pause
