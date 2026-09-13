@echo off
setlocal
cd /d "%~dp0"

echo.
echo  XFER Explorer — one-click Windows setup
echo  No seeds, passwords, or keys are written.
echo.

where powershell >nul 2>&1
if errorlevel 1 (
  echo PowerShell is required.
  echo See docs\SETUP.md
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup-windows.ps1"
set ERR=%ERRORLEVEL%
if %ERR%==2 (
  echo.
  echo Setup prepared the wallet. Start X Coin Wallet, wait until it loads,
  echo then double-click start.bat
  pause
  exit /b 0
)
if %ERR% neq 0 (
  echo.
  echo Setup did not finish. Read the messages above, then docs\SETUP.md
  echo or docs\TROUBLESHOOTING.md
  pause
  exit /b 1
)

call "%~dp0start.bat"
