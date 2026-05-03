@echo off
REM ─ 一鍵環境安裝：建 venv + 裝套件 + 裝 Playwright Chromium ─
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [錯誤] 找不到 python，請先安裝 Python 3.10+ 並加入 PATH
    pause
    exit /b 1
)

if not exist ".venv\" (
    echo [1/3] 建立 venv ...
    python -m venv .venv
    if errorlevel 1 (
        echo [錯誤] venv 建立失敗
        pause
        exit /b 1
    )
)

echo [2/3] 安裝 Python 套件 ...
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
if errorlevel 1 (
    echo [錯誤] 套件安裝失敗
    pause
    exit /b 1
)

echo [3/3] 安裝 Playwright Chromium ...
python -m playwright install chromium
if errorlevel 1 (
    echo [錯誤] Playwright 安裝失敗
    pause
    exit /b 1
)

if not exist "config.json" (
    echo.
    echo [提示] config.json 不存在，從 config.example.json 複製一份
    copy /Y config.example.json config.json >nul
    echo        請編輯 config.json 填入你的 guild_id / channel_id / notify_user_id
)

echo.
echo === 完成！接著執行 login.bat 進行 Discord 登入 ===
pause
