@echo off
REM -----------------------------------------------------------------
REM  Discord Auto Bot -- all-in-one launcher
REM
REM  Auto-detects state and handles each phase:
REM    no .venv          create venv + pip install + chromium
REM    venv missing pkgs reinstall
REM    no config / login interactive wizard
REM    otherwise         run main.py with reboot loop
REM
REM  ASCII-only on purpose: cmd parses .bat files using the system
REM  code page (CP950 on Traditional Chinese Windows). Any non-ASCII
REM  bytes get mis-interpreted as broken commands BEFORE chcp 65001
REM  takes effect. Keep this script English-only; let main.py print
REM  Chinese (Python always handles UTF-8 cleanly).
REM
REM  Line endings MUST be CRLF -- LF-only batch files cause cmd to
REM  silently mis-parse REM lines on some Windows versions, redirecting
REM  fragments into bogus 0-byte files.
REM -----------------------------------------------------------------
chcp 65001 >nul
cd /d "%~dp0"
setlocal enabledelayedexpansion

REM Check Python availability
where python >nul 2>nul
if errorlevel 1 goto NO_PYTHON

REM No venv yet -> first-time setup
if not exist ".venv\Scripts\python.exe" goto FIRST_SETUP

REM Venv exists but missing critical packages -> reinstall
".venv\Scripts\python.exe" -c "import playwright, rich, qrcode, cryptography" >nul 2>nul
if errorlevel 1 goto INSTALL_DEPS

goto RUN

:FIRST_SETUP
echo.
echo === First-time setup [this runs only once] ===
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

echo [3/3] Installing Playwright Chromium [~300MB on first run]...
".venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 goto FAIL_PLAYWRIGHT

echo.
echo === Setup complete ===
echo.

:RUN
REM Clear stale sentinel before each run so we do not false-trigger
if exist "data\.reboot" del /q "data\.reboot" >nul 2>nul

:loop
".venv\Scripts\python.exe" main.py
set "EC=%ERRORLEVEL%"

REM Reboot decision: prefer sentinel file, fallback to exit 42.
REM Two mechanisms because Rich Live alternate-screen mode on Windows
REM can occasionally desync the console state and lose the exit code,
REM but the sentinel file write is unaffected.
REM
REM IMPORTANT: do NOT put parentheses inside echo within an if-block.
REM cmd treats the inner close-paren as the end of the if-block, so
REM the rest of the block including goto gets silently skipped and
REM the batch falls through and exits.
if exist "data\.reboot" (
    del /q "data\.reboot" >nul 2>nul
    echo.
    echo === Reboot requested via sentinel, restarting in 2s... ===
    timeout /t 2 /nobreak >nul
    goto loop
)
if "%EC%"=="42" (
    echo.
    echo === Reboot requested via exit 42, restarting in 2s... ===
    timeout /t 2 /nobreak >nul
    goto loop
)

echo.
echo === Bot exited with code %EC% ===
echo Press any key to close this window.
pause >nul
endlocal
exit /b 0


:NO_PYTHON
echo.
echo [ERROR] python not found in PATH. Install Python 3.10+ first
echo         from https://www.python.org/downloads/ check Add to PATH.
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
