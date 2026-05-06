@echo off
REM ─────────────────────────────────────────────────────────────────
REM  Discord Auto Bot — all-in-one launcher
REM
REM  雙擊就好,腳本會自動偵測:
REM    - 沒 venv? 建 venv
REM    - 套件沒裝? pip install
REM    - Chromium 沒下? playwright install
REM    - 都好了? 啟動 main.py
REM
REM  首次設定(guild_id / channel_id / Discord 登入)由 main.py 內的
REM  互動式 wizard 引導。退出 code 42 = 使用者按 F 重啟。
REM ─────────────────────────────────────────────────────────────────
chcp 65001 >nul
cd /d "%~dp0"
setlocal enabledelayedexpansion

REM 檢查 Python
where python >nul 2>nul
if errorlevel 1 goto NO_PYTHON

REM 1) 沒 venv 就跑首次設定
if not exist ".venv\Scripts\python.exe" goto FIRST_SETUP

REM 2) 有 venv 但缺關鍵套件 → 重新安裝(例如 requirements.txt 改了)
".venv\Scripts\python.exe" -c "import playwright, rich, qrcode, cryptography" >nul 2>nul
if errorlevel 1 goto INSTALL_DEPS

goto RUN

REM ─── 首次設定:venv + deps + Chromium ───
:FIRST_SETUP
echo.
echo === 首次啟動 — 建立執行環境(只跑一次)===
echo [1/3] 建立 Python venv...
python -m venv .venv
if errorlevel 1 goto FAIL_VENV
goto INSTALL_DEPS_FRESH

REM ─── 安裝相依套件(已有 venv,但模組缺漏)───
:INSTALL_DEPS
echo.
echo === 偵測到套件缺漏,重新安裝 ===
:INSTALL_DEPS_FRESH
echo [2/3] 安裝 Python 套件...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\pip.exe" install -r requirements.txt
if errorlevel 1 goto FAIL_PIP

echo [3/3] 安裝 Playwright Chromium(首次需要下載 ~300MB)...
".venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 goto FAIL_PLAYWRIGHT

echo.
echo === 環境就緒 ===
echo.

REM ─── 啟動 bot;exit code 42 = 使用者按 F 要求重啟 ───
:RUN
:loop
".venv\Scripts\python.exe" main.py
if "%ERRORLEVEL%"=="42" (
    echo.
    echo === 收到重啟請求(exit 42),3 秒後重啟... ===
    timeout /t 3 /nobreak >nul
    goto loop
)
endlocal
pause
exit /b 0


REM ─── 錯誤處理 ───
:NO_PYTHON
echo.
echo [錯誤] 找不到 python — 請先安裝 Python 3.10+ 並確認已加入 PATH
echo        (https://www.python.org/downloads/ → 安裝時勾選 "Add to PATH")
endlocal
pause
exit /b 1

:FAIL_VENV
echo.
echo [錯誤] venv 建立失敗 — 確認 Python 安裝完整
endlocal
pause
exit /b 1

:FAIL_PIP
echo.
echo [錯誤] pip install 失敗 — 檢查網路 / 看上方錯誤訊息
endlocal
pause
exit /b 1

:FAIL_PLAYWRIGHT
echo.
echo [錯誤] Playwright Chromium 下載失敗 — 通常是網路問題,稍後重試
endlocal
pause
exit /b 1
