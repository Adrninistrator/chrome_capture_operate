@echo off
rem Single startup entry: app.main. All startup prompts (success/failure/
rem already running) are implemented in Python (app/main.py).
rem If .venv is missing, a system Python runs "app.main --venv-check" so the
rem same Python code shows the "run install.bat" popup.
cd /d %~dp0

if exist .venv\Scripts\pythonw.exe (
    start "" .venv\Scripts\pythonw.exe -m app.main
    exit /b 0
)
where pythonw >nul 2>nul
if not errorlevel 1 (
    start "" pythonw -m app.main --venv-check
    exit /b 0
)
where py >nul 2>nul
if not errorlevel 1 (
    start "" py -3w -m app.main --venv-check
    exit /b 0
)
rem No Python at all: cannot show the Python popup; fall back to console text.
echo [ERROR] Python not found. Run install.bat first.
timeout /t 8 >nul
exit /b 1