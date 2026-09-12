@echo off
setlocal
cd /d "%~dp0"

rem Optional: run xcoind instead of the GUI. Do NOT run this if
rem X Coin Wallet.exe is already open (same data folder).
rem
rem Set XCOIN_BIN to your xcoind.exe if it is not next to this folder.
rem Example:
rem   set XCOIN_BIN=C:\path\to\xcoind.exe

if defined XCOIN_BIN if exist "%XCOIN_BIN%" goto run

if exist "xcoind.exe" (
  set "XCOIN_BIN=%cd%\xcoind.exe"
  goto run
)

echo Could not find xcoind.exe.
echo.
echo 1. Close X Coin Wallet if it is open.
echo 2. Set XCOIN_BIN to the full path of xcoind.exe from the wallet zip.
echo 3. Run this file again.
echo.
echo Prefer the GUI? Skip this file. Put server=1 in xcoin.conf and
echo start X Coin Wallet.exe, then start.bat  —  see docs\SETUP.md
pause
exit /b 1

:run
echo Using %XCOIN_BIN%
echo RPC 38442   P2P 38443
echo Do not start the GUI wallet at the same time.
echo.
"%XCOIN_BIN%" -server=1
