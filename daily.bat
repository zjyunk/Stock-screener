@echo off
REM Run after market close on trading days (suggested 15:30).
REM 1) daily quotes + institutional flows
REM 2) TDCC shareholding distribution - weekly data, but fetched daily so a
REM    missed Saturday can still be caught any day before the next one.
REM 3) screener list
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
if not exist "logs" mkdir "logs"

echo [1/3] fetching daily market data ...
".venv\Scripts\python.exe" run_daily.py

echo.
echo [2/3] checking TDCC shareholding data ...
".venv\Scripts\python.exe" run_daily.py --tdcc

echo.
echo [3/3] screening ...
".venv\Scripts\python.exe" run_screener.py > "logs\screener_latest.txt" 2>&1
type "logs\screener_latest.txt"

echo.
echo Saved to logs\screener_latest.txt
