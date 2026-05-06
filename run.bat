@echo off
REM -----------------------------------------------------------------
REM  Discord Auto Bot -- all-in-one launcher
REM
REM  Auto-detects state and handles each phase:
REM    no .venv          -> create venv + pip install + chromium
REM    venv missing pkgs -> pip install
REM    no config / login -> main.py interactive wizard
REM    otherwise         -> run main.py with exit-code-42 reboot loop
REM
REM  ASCII-only on purpose: cmd parses .bat files using the system
REM  code page (CP950 on Traditional Chinese Windows), so any non-ASCII
REM  bytes in this file get mis-interpreted as broken commands BEFORE
REM  chcp 65001 takes effect. Keep this script English-only; let main.py
REM  print Chinese (Python always handles UTF-8 cleanly).
REM -----------------------------------------------------------------
chcp 65001 >nul
cd /d "%~dp0"
setlocal enabledelayedexpansion

REM Check Python availability
where python >nul 2>nul
if errorlevel 1 goto NO_PYTHON

REM 1) No venv -> first-time setup
if not exist ".venv\Scripts\python.exe" goto FIRST_SETUP

REM 2) Venv exists but missing critical packages -> reinstall
".venv\Scripts\python.exe" -c "import playwright, rich, qrcode, cryptography" >nul 2>nul
if errorlevel 1 goto INSTALL_DEPS

goto RUN

:FIRST_SETUP
echo.
echo === First-time setup (this runs only once) ===
echo [1/3] Creating Python venv...
python -m venv .venv
if errorlevel 1 goto FAIL_VENV
goto INSTALL_DEPS_FRESH

:INSTALL_DEPS
echo.
echo === Detected missing packages, reinstalling ===
:INSTALL_DEPS_FRESH
echo [2/3] Installing Python packages...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\pip.exe" install -r requirements.txt
if errorlevel 1 goto FAIL_PIP

echo [3/3] Installing Playwright Chromium (~300MB on first run)...
".venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 goto FAIL_PLAYWRIGHT

echo.
echo === Setup complete ===
echo.

:RUN
:loop
".venv\Scripts\python.exe" main.py
if "%ERRORLEVEL%"=="42" (
    echo.
    echo === Reboot requested (exit 42), restarting in 3s... ===
    timeout /t 3 /nobreak >nul
    goto loop
)
endlocal
pause
exit /b 0


:NO_PYTHON
echo.
echo [ERROR] python not found in PATH. Install Python 3.10+ first
echo         (https://www.python.org/downloads/  check "Add to PATH").
endlocal
pause
exit /b 1

:FAIL_VENV
echo.
echo [ERROR] venv creation failed -- verify Python install integrity.
endlocal
pause
exit /b 1

:FAIL_PIP
echo.
echo [ERROR] pip install failed -- check network / scroll up for details.
endlocal
pause
exit /b 1

:FAIL_PLAYWRIGHT
echo.
echo [ERROR] Playwright Chromium download failed -- usually network. Retry later.
endlocal
pause
exit /b 1
