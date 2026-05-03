@echo off
REM ─ 啟動 main.py（自動 bot）─
chcp 65001 >nul
cd /d "%~dp0"

if not exist "config.json" (
    echo [錯誤] 找不到 config.json，請先執行 setup.bat
    pause
    exit /b 1
)

if not exist "storage_state.json" (
    echo [錯誤] 找不到 storage_state.json，請先執行 login.bat 完成 Discord 登入
    pause
    exit /b 1
)

if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    set "PY=python"
)

%PY% main.py
pause
