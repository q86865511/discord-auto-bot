@echo off
REM ─ 啟動 main.py（自動 bot），exit code 42 = 使用者按 F 觸發程式重啟 ─
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

:loop
%PY% main.py
if "%ERRORLEVEL%"=="42" (
    echo.
    echo === 偵測到重啟請求，3 秒後重新啟動 ===
    timeout /t 3 /nobreak >nul
    goto loop
)

pause
