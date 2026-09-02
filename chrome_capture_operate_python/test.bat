@echo off
rem Install dev dependencies and run tests
cd /d %~dp0
if not exist .venv\Scripts\python.exe (
    echo [ERROR] .venv not found. Run install.bat first.
    pause
    exit /b 1
)
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest tests -v
echo.
pause
