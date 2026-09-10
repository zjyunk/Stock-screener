"""個股查詢伺服器：開著它，報表就能搜尋任何一檔股票。

    python run_server.py              # http://localhost:8765
    python run_server.py --port 9000

跟 run_report.py 的差別：靜態報表把入選股的資料全部塞進 HTML，離線可開，
但只有那幾檔能看；這個伺服器從資料庫現抓，1,900 檔都能查，但要開著才能用。
兩者共用同一個頁面模板。

只用 Python 內建模組，不用裝東西。每個請求開一條唯讀連線、用完就關，
把跟 daily.bat（寫入）撞鎖的機會降到最低。
"""

import argparse
import json
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import duckdb

import config
from src import db, report, screener

PARAM_KEYS = (
    "SCREEN_MODE", "REVERSAL_REQUIRE_INST", "UNIVERSE_TOP_N", "UNIVERSE_LOOKBACK",
    "UNIVERSE_MIN_DAYS", "TREND_LOOKBACK", "MA_PERIOD", "REQUIRE_ABOVE_MA",
    "REQUIRE_KD_RISING", "REQUIRE_MACD_RISING", "INST_PRIMARY", "INST_WINDOW",
    "INST_MIN_NET", "INST_MIN_BUY_DAYS", "INST_MIN_PCT",
)
TEMPLATE = Path(__file__).parent / "src" / "report_template.html"
CHARTLIB = Path(__file__).parent / "src" / "vendor" / "lightweight-charts.js"

_lock = threading.Lock()
_cache = {"page": None, "screen": None, "trade_date": None}


def connect_ro():
    return duckdb.connect(str(config.DB_PATH), read_only=True)


def ensure_screen():
    """今天的選股結果算一次就好，之後每個請求共用。"""
    with _lock:
        if _cache["screen"] is not None:
            return _cache["screen"], _cache["trade_date"]
        params = {k: getattr(config, k) for k in PARAM_KEYS}
        con = connect_ro()
        try:
            trade_date, result = screener.run(con, params, target_date=None)
            rows = {r["stock_id"]: r for r in result.to_dict("records")} if not result.empty else {}
            payload = report.build_payload(con, result, trade_date, params) if not result.empty else \
                {"trade_date": str(trade_date), "params": params, "stocks": []}
            payload["funnel"] = [
                (label, kept) for label, kept, _ in screener.funnel_stats(result, params)
                if not label.startswith("　")
            ] if not result.empty else []
            payload["mode"] = "server"
            _cache["screen"] = (rows, payload)
            _cache["trade_date"] = trade_date
        finally:
            con.close()
        return _cache["screen"], _cache["trade_date"]


def render_page():
    (rows, payload), _ = ensure_screen()
    return (
        TEMPLATE.read_text(encoding="utf-8")
        .replace("__TRADE_DATE__", str(payload["trade_date"]))
        .replace("__CHARTLIB__", CHARTLIB.read_text(encoding="utf-8"))
        .replace("__PAYLOAD__", report.to_json(payload))
    )


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):      # 安靜一點
        pass

    def _send(self, body, ctype="application/json; charset=utf-8", status=200):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        try:
            if url.path in ("/", "/index.html"):
                self._send(render_page(), "text/html; charset=utf-8")
            elif url.path == "/api/search":
                q = parse_qs(url.query).get("q", [""])[0]
                con = connect_ro()
                try:
                    self._send(json.dumps(report.search_stocks(con, q), ensure_ascii=False))
                finally:
                    con.close()
            elif url.path.startswith("/api/stock/"):
                stock_id = url.path.rsplit("/", 1)[-1].strip()
                (rows, _), trade_date = ensure_screen()
                con = connect_ro()
                try:
                    db.create_views(con) if False else None      # 唯讀連線不建 view；view 已存在
                    payload = report.stock_payload(con, stock_id, trade_date, rows.get(stock_id))
                finally:
                    con.close()
                if payload is None:
                    self._send(json.dumps({"error": f"找不到 {stock_id} 的資料"}, ensure_ascii=False), status=404)
                else:
                    self._send(report.to_json(payload))
            else:
                self._send("not found", "text/plain", 404)
        except Exception as exc:  # noqa: BLE001
            self._send(json.dumps({"error": str(exc)}, ensure_ascii=False), status=500)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true", help="不要自動開瀏覽器")
    args = ap.parse_args()

    if not config.DB_PATH.exists():
        print("找不到資料庫，請先跑 run_backfill.py")
        return 1

    # view 要先確定存在（唯讀連線建不了）
    con = db.connect(config.DB_PATH)
    db.create_views(con)
    con.close()

    print("先算今天的選股結果…", flush=True)
    _, trade_date = ensure_screen()
    url = f"http://localhost:{args.port}"
    print(f"交易日 {trade_date}。伺服器啟動：{url}")
    print("在頁面左上角搜尋框輸入代號或名稱，任何一檔都能查。Ctrl-C 結束。")

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    if not args.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已關閉")
    return 0


if __name__ == "__main__":
    sys.exit(main())
