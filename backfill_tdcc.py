"""集保歷史回補。

opendata API 只給最新一週，歷史要靠 TDCC 查詢頁一檔一週地問，所以很貴：
每檔每週一次請求。因此只補「真正用得到的股票」——近期進過選股母體的那些。

    python backfill_tdcc.py --weeks 2                # 最近 2 週（預設）
    python backfill_tdcc.py --weeks 4 --top-n 50     # 只補前 50 名母體
    python backfill_tdcc.py --weeks 2 --interval 8   # 更客氣一點
    python backfill_tdcc.py --list                   # 只列出可查詢的週次

可中斷續跑：資料庫裡已有的 (資料日, 個股) 會自動跳過。

TDCC 只保留約 51 週，最舊的會陸續消失。這個頁面是給人手動查的、沒有 API，
請維持低頻率，不要在上班時段大量跑。
"""

import argparse
import logging
import sys
import time
from datetime import datetime

import config
from src import db, tdcc
from src.http_client import RateLimitedClient

log = logging.getLogger("backfill_tdcc")


def setup_logging():
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(config.LOG_DIR / "backfill_tdcc.log", encoding="utf-8"),
        ],
    )


def target_stocks(con, top_n, lookback_days):
    """近 N 個交易日進過成交值前 top_n 名的個股。"""
    return [
        r[0]
        for r in con.execute(
            """
            WITH recent AS (
                SELECT DISTINCT trade_date FROM daily_price
                ORDER BY trade_date DESC LIMIT ?
            )
            SELECT DISTINCT stock_id FROM v_turnover_rank
            WHERE rank_ma5 <= ? AND trade_date IN (SELECT trade_date FROM recent)
            ORDER BY stock_id
            """,
            [lookback_days, top_n],
        ).fetchall()
    ]


def next_trading_day(con, data_date):
    row = con.execute(
        "SELECT min(trade_date) FROM daily_price WHERE trade_date > ?", [data_date]
    ).fetchone()
    return row[0] if row and row[0] else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weeks", type=int, default=2, help="回補最近幾週（含已有的），預設 2")
    ap.add_argument("--top-n", type=int, default=100, help="母體範圍，預設 100")
    ap.add_argument("--lookback", type=int, default=20, help="用最近幾個交易日決定母體，預設 20")
    ap.add_argument("--interval", type=float, default=5.0, help="請求間隔秒數，預設 5")
    ap.add_argument("--list", action="store_true", help="只列出可查詢的週次就結束")
    args = ap.parse_args()

    setup_logging()

    if not config.DB_PATH.exists():
        print("找不到資料庫，請先跑 run_backfill.py")
        return 1

    client = RateLimitedClient(args.interval, config.USER_AGENT, 60, config.MAX_RETRIES)
    hist = tdcc.TdccHistory(client)
    dates = hist.open()

    if args.list:
        print(f"\nTDCC 目前提供 {len(dates)} 週（新到舊）：")
        for i, d in enumerate(dates, 1):
            print(f"  {i:>2}. {d[:4]}-{d[4:6]}-{d[6:8]}")
        return 0

    wanted = dates[: args.weeks]
    con = db.connect(config.DB_PATH)
    db.create_views(con)

    stocks = target_stocks(con, args.top_n, args.lookback)
    log.info("目標：%d 檔 × %d 週 = 最多 %d 次請求（間隔 %.1f 秒）",
             len(stocks), len(wanted), len(stocks) * len(wanted), args.interval)

    # 已有的 (資料日, 個股) 跳過 —— 這就是續跑的依據
    have = {
        (r[0].isoformat(), r[1])
        for r in con.execute("SELECT DISTINCT data_date, stock_id FROM tdcc_dist").fetchall()
    }

    todo = [
        (d, s)
        for d in wanted
        for s in stocks
        if (f"{d[:4]}-{d[4:6]}-{d[6:8]}", s) not in have
    ]
    if not todo:
        log.info("這 %d 週的目標個股都已在庫，沒事可做", len(wanted))
        con.close()
        return 0

    log.info("實際需要抓 %d 筆，預估 %.0f 分鐘", len(todo), len(todo) * args.interval / 60)

    started = time.monotonic()
    ok = empty = failed = 0
    buffer, avail_cache = [], {}

    for i, (date_str, stock_id) in enumerate(todo, 1):
        try:
            rows = hist.fetch(stock_id, date_str)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            log.error("%s %s 失敗：%s", stock_id, date_str, exc)
            continue

        if not rows:
            empty += 1
        else:
            iso = rows[0]["data_date"]
            if iso not in avail_cache:
                avail_cache[iso] = next_trading_day(
                    con, datetime.strptime(iso, "%Y-%m-%d").date()
                )
            for row in rows:
                row["avail_date"] = avail_cache[iso]
            buffer.extend(rows)
            ok += 1

        # 分批寫入，中途中斷也保得住已抓的部分
        if len(buffer) >= 1500:
            db._upsert(con, "tdcc_dist", buffer, ["data_date", "stock_id", "level"])
            buffer.clear()

        if i % 25 == 0 or i == len(todo):
            elapsed = time.monotonic() - started
            eta = (len(todo) - i) * (elapsed / i)
            log.info("%d/%d  成功=%d 無資料=%d 失敗=%d  剩約 %.0f 分",
                     i, len(todo), ok, empty, failed, eta / 60)

    if buffer:
        db._upsert(con, "tdcc_dist", buffer, ["data_date", "stock_id", "level"])

    weeks = con.execute("SELECT count(DISTINCT data_date) FROM tdcc_dist").fetchone()[0]
    total = con.execute("SELECT count(*) FROM tdcc_dist").fetchone()[0]
    log.info("完成：成功 %d、無資料 %d、失敗 %d，耗時 %.1f 分",
             ok, empty, failed, (time.monotonic() - started) / 60)
    log.info("資料庫現有 %d 週、%d 列", weeks, total)
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
