@echo off
REM Start the MRP Reconciliation app (fully offline, localhost only).
cd /d "%~dp0"
python app.py %*
if errorlevel 1 pause
