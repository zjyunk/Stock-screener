"""3 年歷史回補。

跑法：
    python run_backfill.py                    # 用 config 的區間，全部來源
    python run_backfill.py --fetch-only       # 只抓不入庫（先把資料弄回來）
    python run_backfill.py --load-only        # 只從 raw 重建資料庫（不連網）
    python run_backfill.py --start 2025-01-01 --end 2025-06-30

可中斷可續跑：raw 檔已存在就跳過，中途 Ctrl-C 再跑一次會從斷點接續。
時間估計：約 735 個交易日 × 4 個端點 ≈ 2,940 次請求，含限速約 3 小時。建議掛著過夜。
"""

import argparse
import json
import logging
import sys
import time
from datetime import date, datetime, timedelta

import config
from src import db, raw_store, tpex, twse
from src.http_client import RateLimitedClient

log = logging.getLogger("backfill")


def setup_logging():
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(config.LOG_DIR / "backfill.log", encoding="utf-8"),
        ],
    )


def weekdays(start: date, end: date):
    d = start
    while d <= end:
        if d.weekday() < 5:          # 週末一定沒有交易，直接跳過省請求
            yield d
        d += timedelta(days=1)


def parse_date(text):
    return datetime.strptime(text, "%Y-%m-%d").date()


# 抓取階段
def fetch_all(start, end):
    twse_client = RateLimitedClient(
        config.TWSE_MIN_INTERVAL, config.USER_AGENT, config.HTTP_TIMEOUT, config.MAX_RETRIES
    )
    tpex_client = RateLimitedClient(
        config.TPEX_MIN_INTERVAL, config.USER_AGENT, config.HTTP_TIMEOUT, config.MAX_RETRIES
    )

    days = list(weekdays(start, end))
    total = len(days)
    log.info("回補區間 %s ~ %s，共 %d 個平日", start, end, total)

    started = time.monotonic()
    fetched = skipped = failed = 0

    for i, d in enumerate(days, 1):
        date_str = d.strftime("%Y%m%d")
        jobs = (
            ("twse_quote", twse.fetch_daily_quote, twse_client),
            ("twse_insti", twse.fetch_insti, twse_client),
            ("tpex_quote", tpex.fetch_daily_quote, tpex_client),
            ("tpex_insti", tpex.fetch_insti, tpex_client),
            ("twse_qfii", twse.fetch_qfii, twse_client),
            ("twse_margin", twse.fetch_margin, twse_client),
            ("tpex_qfii", tpex.fetch_qfii, tpex_client),
            ("tpex_margin", tpex.fetch_margin, tpex_client),
        )
        for name, fn, client in jobs:
            try:
                if fn(client, config.RAW_DIR, date_str):
                    fetched += 1
                else:
                    skipped += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                log.error("%s %s 失敗：%s", name, date_str, exc)

        if i % 20 == 0 or i == total:
            elapsed = time.monotonic() - started
            rate = i / elapsed if elapsed else 0
            eta = (total - i) / rate if rate else 0
            log.info(
                "%d/%d 天 (%.0f%%)  抓取=%d 跳過=%d 失敗=%d  剩餘約 %.0f 分",
                i, total, 100 * i / total, fetched, skipped, failed, eta / 60,
            )

    # 除權息與基本資料一次抓完（區間查詢，不需逐日）
    # fetch_exright(twse.py)不寫檔只回傳文字
    log.info("抓除權息與基本資料…")
    try:
        chunks = []
        cursor = start
        # 不合併為一層，如果合併邏輯有錯，或交易所某一段的欄位格式不一樣，原始資料就已經被改掉，沒辦法從頭重新解析
        while cursor <= end:
            chunk_end = min(cursor + timedelta(days=180), end)
            # fetch_exright 回傳的是 JSON 字串（client.get_text 的 resp.text）
            # chunks 是「字串的 list」
            chunks.append(
                twse.fetch_exright(
                    twse_client, cursor.strftime("%Y%m%d"), chunk_end.strftime("%Y%m%d")
                )
            )
            cursor = chunk_end + timedelta(days=1)
        # raw_store.write 只收一個字串，所以先 json.dumps(chunks)，把整個 list 變成一個字串
        raw_store.write(config.RAW_DIR, "twse_exright", end.strftime("%Y%m%d"),
                        json.dumps(chunks, ensure_ascii=False)) 
    except Exception as exc:  # noqa: BLE001
        log.error("除權息抓取失敗：%s", exc)

    # 公司基本資料
    for source, fn, client in (
        ("twse_company", twse.fetch_company_master, twse_client),
        ("tpex_company", tpex.fetch_company_master, tpex_client),
    ):
        try:
            raw_store.write(config.RAW_DIR, source, end.strftime("%Y%m%d"), fn(client))
        except Exception as exc:  # noqa: BLE001
            log.error("%s 抓取失敗：%s", source, exc)

    log.info("抓取完成：新增 %d、跳過 %d、失敗 %d，耗時 %.1f 分",
             fetched, skipped, failed, (time.monotonic() - started) / 60)


# --------------------------------------------------------------- 入庫階段
def load_all(start, end):
    con = db.connect(config.DB_PATH)
    log.info("從 raw 載入 DuckDB：%s", config.DB_PATH)

    # 基本資料先進，才有 shares_outstanding (流通在外股數，公司總共發行、目前在市場上的普通股股數) 的容身處
    for source, parser in (("twse_company", twse.parse_company_master),
                           ("tpex_company", tpex.parse_company_master)):
        # 在不同日子跑過好幾次 backfill，迴圈會由舊到新全部載入
        for date_str in raw_store.list_dates(config.RAW_DIR, source):
            rows = parser(raw_store.read(config.RAW_DIR, source, date_str))
            # load_security_master 的做法是先刪除同代號的列，再插入
            log.info("%s：%d 檔", source, db.load_security_master(con, rows))

    prices = insti = qfii = margin = 0
    shares = {}
    for d in weekdays(start, end):
        date_str = d.strftime("%Y%m%d")
        date_iso = d.isoformat()

        for source, parser in (("twse_quote", twse.parse_daily_quote),
                               ("tpex_quote", tpex.parse_daily_quote)):
            if not raw_store.exists(config.RAW_DIR, source, date_str):
                continue
            rows = parser(raw_store.read(config.RAW_DIR, source, date_str), date_iso)
            for row in rows:
                if row.get("shares_outstanding"):
                    shares[row["stock_id"]] = row["shares_outstanding"]
            prices += db.load_prices(con, rows)

        for source, parser in (("twse_insti", twse.parse_insti),
                               ("tpex_insti", tpex.parse_insti)):
            if not raw_store.exists(config.RAW_DIR, source, date_str):
                continue
            rows = parser(raw_store.read(config.RAW_DIR, source, date_str), date_iso)
            insti += db.load_inst_flow(con, rows)

        for source, parser, loader in (
            ("twse_qfii", twse.parse_qfii, db.load_foreign_holding),
            ("tpex_qfii", tpex.parse_qfii, db.load_foreign_holding),
            ("twse_margin", twse.parse_margin, db.load_margin),
            ("tpex_margin", tpex.parse_margin, db.load_margin),
        ):
            if not raw_store.exists(config.RAW_DIR, source, date_str):
                continue
            rows = parser(raw_store.read(config.RAW_DIR, source, date_str), date_iso)
            if source.endswith("qfii"):
                qfii += loader(con, rows)
            else:
                margin += loader(con, rows)

    log.info("價格 %d 列、法人 %d 列、外資持股 %d 列、融資融券 %d 列",
             prices, insti, qfii, margin)

    # 除權息
    for date_str in raw_store.list_dates(config.RAW_DIR, "twse_exright"):
        #  先用 json.loads 還原成字串 list，再把每個字串交給 parse_exright 做第二次 json.loads
        chunks = json.loads(raw_store.read(config.RAW_DIR, "twse_exright", date_str))
        rows = []
        for chunk in chunks:
            rows.extend(twse.parse_exright(chunk))
        log.info("除權息 %d 筆", db.load_ex_right(con, rows))

    if shares:
        db.update_shares_outstanding(con, list(shares.items()))
        log.info("更新上櫃發行股數 %d 檔", len(shares))

    db.create_views(con)

    info = db.summary(con)
    log.info("---- 入庫完成 ----")
    for key, value in info.items():
        log.info("%-18s %s", key, value)
    con.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=config.BACKFILL_START)
    ap.add_argument("--end", default=config.BACKFILL_END)
    ap.add_argument("--fetch-only", action="store_true")
    ap.add_argument("--load-only", action="store_true")
    args = ap.parse_args()

    setup_logging()
    start = parse_date(args.start)
    end = parse_date(args.end) if args.end else date.today()

    if not args.load_only:
        fetch_all(start, end)
    if not args.fetch_only:
        load_all(start, end)


if __name__ == "__main__":
    main()
