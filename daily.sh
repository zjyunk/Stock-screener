#!/usr/bin/env bash
# 每個交易日收盤後跑（建議 15:30）。Git Bash 用這個，PowerShell 用 daily.bat。
#   ./daily.sh
set -u
cd "$(dirname "$0")"
export PYTHONIOENCODING=utf-8
mkdir -p logs
PY=.venv/Scripts/python.exe

echo "[1/4] 抓取當日行情、法人、外資持股、融資融券 ..."
"$PY" run_daily.py

echo
echo "[2/4] 檢查集保 ..."
"$PY" run_daily.py --tdcc

echo
echo "[3/4] 選股 ..."
"$PY" run_screener.py | tee logs/screener_latest.txt

echo
echo "清單已存到 logs/screener_latest.txt"

echo
echo "[4/4] 產生報表 ..."
"$PY" run_report.py
echo "用瀏覽器開 report/latest.html"
