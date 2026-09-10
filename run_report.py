"""產生個股頁報表（Phase 3）。

跑一次選股，把入選個股近 120 個交易日的 K 線、量、MACD、KD、法人買賣超、
外資持股、融資、集保籌碼組成一個離線 HTML，用瀏覽器開就能看：

    python run_report.py                         # 最新交易日 -> report/latest.html
    python run_report.py --date 2026-08-28
    python run_report.py --stocks 2330,2317      # 額外加幾檔沒入選的來看
    python run_report.py --open                  # 產生後直接用瀏覽器開

圖表用 lightweight-charts（已內嵌，離線可開）。六個窗格時間軸連動、十字線連動。
"""

import argparse
import shutil
import sys
import webbrowser
from pathlib import Path

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
OUT_DIR = Path(__file__).parent / "report"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD，預設最新交易日")
    ap.add_argument("--mode", choices=["trend", "reversal", "both"])
    ap.add_argument("--stocks", help="額外加入的個股代號，逗號分隔")
    ap.add_argument("--open", action="store_true", help="產生後用瀏覽器開")
    args = ap.parse_args()

    if not config.DB_PATH.exists():
        print("找不到資料庫，請先跑 run_backfill.py")
        return 1
    if not CHARTLIB.exists():
        print(f"找不到圖表函式庫 {CHARTLIB}")
        return 1

    params = {k: getattr(config, k) for k in PARAM_KEYS}
    if args.mode:
        params["SCREEN_MODE"] = args.mode
    extra = [s.strip() for s in args.stocks.split(",")] if args.stocks else None

    con = db.connect(config.DB_PATH)
    db.create_views(con)

    trade_date, result = screener.run(con, params, target_date=args.date)
    if result.empty:
        print(f"{trade_date} 沒有股票通過 Layer 1")
        con.close()
        return 0

    payload = report.build_payload(con, result, trade_date, params, stock_ids=extra)
    payload["funnel"] = [
        (label, kept) for label, kept, _ in screener.funnel_stats(result, params)
        if not label.startswith("　")
    ]
    con.close()

    html = (
        TEMPLATE.read_text(encoding="utf-8")
        .replace("__TRADE_DATE__", str(trade_date))
        .replace("__CHARTLIB__", CHARTLIB.read_text(encoding="utf-8"))
        .replace("__PAYLOAD__", report.to_json(payload))
    )

    OUT_DIR.mkdir(exist_ok=True)
    dated = OUT_DIR / f"{trade_date}.html"
    latest = OUT_DIR / "latest.html"
    dated.write_text(html, encoding="utf-8")
    shutil.copyfile(dated, latest)

    n = len(payload["stocks"])
    size = dated.stat().st_size / 1024
    print(f"報表已產生：{dated}（{n} 檔，{size:,.0f} KB）")
    print(f"最新一份：  {latest}")

    if args.open:
        webbrowser.open(latest.resolve().as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
