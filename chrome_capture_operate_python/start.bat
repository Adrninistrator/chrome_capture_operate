@echo off
rem Start service with venv pythonw (no console window, tray icon)
cd /d %~dp0
if not exist .venv\Scripts\pythonw.exe (
    echo [ERROR] .venv not found. Run install.bat first.
    pause
    exit /b 1
)
start "" .venv\Scripts\pythonw.exe -m app.main
