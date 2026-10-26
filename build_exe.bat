@echo off
title ServerDeck - Build .exe (optional)
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" ( echo Run setup.bat first. & pause & exit /b 1 )
".venv\Scripts\python.exe" -m pip install pyinstaller
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --noconsole --onefile --name ServerDeck --add-data "ui;ui" app.py
echo.
echo Built: dist\ServerDeck.exe
echo Move it to any folder you like. Its servers are stored in a "data" folder next to it.
pause
