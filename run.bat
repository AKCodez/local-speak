@echo off
REM Debug launcher. Runs with a visible console so logs stream live.
cd /d "%~dp0"
".venv\Scripts\python.exe" main.py
echo.
echo STT exited. Press any key to close this window.
pause >nul
