@echo off
title ServerDeck - Setup
cd /d "%~dp0"
echo.
echo  ServerDeck setup
echo  ----------------
echo.

set "PY="
py -3.12 --version >nul 2>&1 && set "PY=py -3.12"
if not defined PY py -3.13 --version >nul 2>&1 && set "PY=py -3.13"
if not defined PY py -3.11 --version >nul 2>&1 && set "PY=py -3.11"
if not defined PY py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY python --version >nul 2>&1 && set "PY=python"

if not defined PY (
  echo  Python was not found. Installing Python 3.12 with winget...
  winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
  if errorlevel 1 (
    echo.
    echo  Automatic install failed. Install Python 3.12 from https://www.python.org/downloads/
    echo  and tick "Add python.exe to PATH" during install. Then run setup.bat again.
    pause
    exit /b 1
  )
  echo.
  echo  Python installed. Close this window and double-click setup.bat again.
  pause
  exit /b 0
)

echo  Using: %PY%
if not exist ".venv\Scripts\python.exe" (
  echo  Creating private Python environment...
  %PY% -m venv .venv
  if errorlevel 1 ( echo  Failed to create environment. & pause & exit /b 1 )
)

echo  Installing components...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo  Some components failed to install. The app will still open in your web browser.
)

echo  Creating desktop shortcut...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\ServerDeck.lnk');" ^
  "$s.TargetPath='%~dp0.venv\Scripts\pythonw.exe';$s.Arguments='app.py';$s.WorkingDirectory='%~dp0';$s.Save()" >nul 2>&1

echo.
echo  Done! Open ServerDeck from the desktop shortcut or "Start ServerDeck.bat".
echo.
pause
