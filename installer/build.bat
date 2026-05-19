@echo off
:: ============================================================
::  AID Helpdesk Agent - Build Script
::  Produces: installer\dist\aid-agent-setup.exe
::
::  Requirements:
::    Python 3.9+ in PATH
::    Run from the repo root or the installer\ directory
::    Must be run on Windows
:: ============================================================

setlocal
cd /d "%~dp0"

echo.
echo  AID Helpdesk Agent - Build
echo  ==========================

:: 1. Install / upgrade build dependencies
echo.
echo  [1/3] Installing build dependencies...
pip install --quiet --upgrade pyinstaller pywin32 pywinrm requests python-dotenv
if errorlevel 1 (
    echo  [ERROR] pip install failed. Make sure Python is in your PATH.
    pause
    exit /b 1
)

:: 2. Build
echo.
echo  [2/3] Running PyInstaller...
pyinstaller aid-agent-setup.spec --noconfirm
if errorlevel 1 (
    echo  [ERROR] PyInstaller build failed.
    pause
    exit /b 1
)

:: 3. Done
echo.
echo  [3/3] Build complete!
echo.
echo  Output: %~dp0dist\aid-agent-setup.exe
echo.
echo  To test locally (opens wizard):
echo    dist\aid-agent-setup.exe
echo.
echo  To test service mode (in an elevated terminal):
echo    dist\aid-agent-setup.exe install
echo    net start AIDHelpdeskAgent
echo    net stop  AIDHelpdeskAgent
echo    dist\aid-agent-setup.exe remove
echo.
pause
