@echo off
rem Create venv if missing, then install dependencies
cd /d %~dp0
if not exist .venv\Scripts\python.exe python -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
echo.
echo Install done.
pause
