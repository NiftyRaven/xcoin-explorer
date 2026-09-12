@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
  echo Python was not found.
  echo Install Python 3.11+ from https://www.python.org/downloads/
  echo On the first installer screen, tick "Add python.exe to PATH".
  echo Full guide: docs\SETUP.md
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating virtualenv...
  python -m venv .venv
  if errorlevel 1 (
    echo Could not create .venv. See docs\SETUP.md
    pause
    exit /b 1
  )
)

call .venv\Scripts\activate.bat
python -m pip install -q -r requirements.txt
echo.
echo Starting XFER Explorer at http://127.0.0.1:8080
echo Keep X Coin Wallet running. Setup: docs\SETUP.md
echo.
python -m explorer %*
if errorlevel 1 pause
