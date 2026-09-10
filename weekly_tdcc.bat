@echo off
REM Run every Saturday (suggested 09:00).
REM TDCC only serves the LATEST week - a missed run is a permanently missing week.
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
if not exist "logs" mkdir "logs"
".venv\Scripts\python.exe" run_daily.py --tdcc
