@echo off
REM Build a standalone DiscordBot.exe with PyInstaller.
REM Output: dist\DiscordBot.exe (~30-50 MB)
REM
REM First launch on a fresh PC will download Chromium (~300 MB) via the
REM run-once "playwright install chromium" inside the exe (see main.py boot
REM path). After that, no network needed unless user updates the bot.
chcp 65001 >nul
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
    set "PIP=.venv\Scripts\pip.exe"
) else (
    set "PY=python"
    set "PIP=pip"
)

echo === Ensuring PyInstaller is installed ===
%PIP% show pyinstaller >nul 2>&1
if errorlevel 1 (
    %PIP% install pyinstaller
    if errorlevel 1 (
        echo [ERROR] pip install pyinstaller failed
        pause
        exit /b 1
    )
)

echo === Cleaning previous build ===
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo === Building DiscordBot.exe ===
%PY% -m PyInstaller build.spec --clean --noconfirm
if errorlevel 1 (
    echo [ERROR] PyInstaller build failed
    pause
    exit /b 1
)

echo.
echo === Build complete ===
echo Output: dist\DiscordBot.exe
echo.
echo To distribute:
echo   1. Copy dist\DiscordBot.exe to target machine
echo   2. Place config.json + storage_state.json in same folder as the .exe
echo   3. First run will download Chromium (~300MB, needs internet)
echo   4. Windows Defender may flag the exe — user must approve once
echo.
pause
