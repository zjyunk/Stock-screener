"""每日增量更新。掛 Windows 工作排程器，收盤後跑。

建議排程：
    週一至週五 15:30  python run_daily.py
    週六       09:00  python run_daily.py --tdcc

TWSE 收盤行情約 14:30 後出、三大法人約 15:00 後出，所以 15:30 是安全的時點。
"""

import argparse
import csv
import io
import logging
import sys
from datetime import date, datetime, timedelta

import config
from src import db, raw_store, tpex, twse
from src.http_client import RateLimitedClient

log = logging.getLogger("daily")

TDCC_URL = "https://opendata.tdcc.com.tw/getOD.ashx?id=1-5"


def setup_logging():
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(config.LOG_DIR / "daily.log", encoding="utf-8"),
        ],
    )


def next_trading_day(con, data_date):
    """集保是週五資料、週六才公布，所以最早能用的是下一個交易日。

    找不到（例如資料比 DB 還新）就退回 data_date + 3 天，寧可保守。
    """
    row = con.execute(
        "SELECT min(trade_date) FROM daily_price WHERE trade_date > ?", [data_date]
    ).fetchone()
    if row and row[0]:
        return row[0]
    return data_date + timedelta(days=3)


def update_market(target: date):
    date_str = target.strftime("%Y%m%d")
    date_iso = target.isoformat()

    twse_client = RateLimitedClient(
        config.TWSE_MIN_INTERVAL, config.USER_AGENT, config.HTTP_TIMEOUT, config.MAX_RETRIES
    )
    tpex_client = RateLimitedClient(
        config.TPEX_MIN_INTERVAL, config.USER_AGENT, config.HTTP_TIMEOUT, config.MAX_RETRIES
    )

    for name, fn, client in (
        ("twse_quote", twse.fetch_daily_quote, twse_client),
        ("twse_insti", twse.fetch_insti, twse_client),
        ("tpex_quote", tpex.fetch_daily_quote, tpex_client),
        ("tpex_insti", tpex.fetch_insti, tpex_client),
        ("twse_qfii", twse.fetch_qfii, twse_client),
        ("twse_margin", twse.fetch_margin, twse_client),
        ("tpex_qfii", tpex.fetch_qfii, tpex_client),
        ("tpex_margin", tpex.fetch_margin, tpex_client),
    ):
        try:
            fn(client, config.RAW_DIR, date_str, skip_existing=False)
            log.info("抓取 %s %s", name, date_str)
        except Exception as exc:  # noqa: BLE001
            log.error("%s 失敗：%s", name, exc)

    con = db.connect(config.DB_PATH)
    prices = insti = 0
    for source, parser in (("twse_quote", twse.parse_daily_quote),
                           ("tpex_quote", tpex.parse_daily_quote)):
        if raw_store.exists(config.RAW_DIR, source, date_str):
            prices += db.load_prices(
                con, parser(raw_store.read(config.RAW_DIR, source, date_str), date_iso)
            )
    for source, parser in (("twse_insti", twse.parse_insti),
                           ("tpex_insti", tpex.parse_insti)):
        if raw_store.exists(config.RAW_DIR, source, date_str):
            insti += db.load_inst_flow(
                con, parser(raw_store.read(config.RAW_DIR, source, date_str), date_iso)
            )

    extra = 0
    for source, parser, loader in (
        ("twse_qfii", twse.parse_qfii, db.load_foreign_holding),
        ("tpex_qfii", tpex.parse_qfii, db.load_foreign_holding),
        ("twse_margin", twse.parse_margin, db.load_margin),
        ("tpex_margin", tpex.parse_margin, db.load_margin),
    ):
        if raw_store.exists(config.RAW_DIR, source, date_str):
            extra += loader(
                con, parser(raw_store.read(config.RAW_DIR, source, date_str), date_iso)
            )

    if prices == 0:
        log.info("%s 沒有行情資料（休市或尚未公布）", date_iso)
    else:
        log.info("%s 入庫：價格 %d 列、法人 %d 列、籌碼存量 %d 列",
                 date_iso, prices, insti, extra)

    db.create_views(con)
    con.close()

    catch_up(twse_client, tpex_client)


LATE_SOURCES = (
    ("twse_qfii", twse.fetch_qfii, twse.parse_qfii, "foreign_holding"),
    ("tpex_qfii", tpex.fetch_qfii, tpex.parse_qfii, "foreign_holding"),
    ("twse_margin", twse.fetch_margin, twse.parse_margin, "margin_balance"),
    ("tpex_margin", tpex.fetch_margin, tpex.parse_margin, "margin_balance"),
)


def catch_up(twse_client, tpex_client, days=5):
    """補抓近幾個交易日缺漏的「晚公布」來源。

    外資持股統計與融資融券餘額的公布時間比行情晚不少 —— 15:30 跑排程時
    通常還沒上架，交易所會回「查詢日期大於可查詢最大日期」。
    與其把排程時間往後拖（拖了還是可能遇到延遲公布），不如每次執行時
    順便回頭看幾天、把缺的補上。通常不需要任何請求。
    """
    con = db.connect(config.DB_PATH)
    trade_days = [
        r[0] for r in con.execute(
            "SELECT DISTINCT trade_date FROM daily_price ORDER BY trade_date DESC LIMIT ?",
            [days],
        ).fetchall()
    ]

    filled = 0
    pending = {}
    for source, fetch, parse, table in LATE_SOURCES:
        client = twse_client if source.startswith("twse") else tpex_client
        market = "TWSE" if source.startswith("twse") else "TPEx"
        have = {
            r[0] for r in con.execute(
                f"SELECT DISTINCT trade_date FROM {table} WHERE market = ?", [market]
            ).fetchall()
        }
        loader = db.load_foreign_holding if table == "foreign_holding" else db.load_margin

        for day in trade_days:
            if day in have:
                continue
            date_str = day.strftime("%Y%m%d")
            try:
                if not fetch(client, config.RAW_DIR, date_str, skip_existing=True):
                    if not raw_store.exists(config.RAW_DIR, source, date_str):
                        pending.setdefault(source, []).append(date_str)
                        continue        # 交易所還沒公布，下次再說
                rows = parse(raw_store.read(config.RAW_DIR, source, date_str), day.isoformat())
                n = loader(con, rows)
                if n:
                    filled += n
                    log.info("補抓 %s %s：%d 列", source, date_str, n)
            except Exception as exc:  # noqa: BLE001
                log.warning("補抓 %s %s 失敗：%s", source, date_str, exc)

    if filled:
        log.info("補抓完成，共 %d 列", filled)
    if pending:
        for source, days_ in sorted(pending.items()):
            log.info("尚未公布：%s %s（交易所端還沒上架，下次執行會自動補）",
                     source, ", ".join(days_))
    elif not filled:
        log.info("近 %d 個交易日的籌碼存量資料都齊了", len(trade_days))
    con.close()


def update_tdcc():
    """集保股權分散表。每週抓一次，自行累積歷史。

    這個 API 只給「最新一期」（實測 2.3MB、4,051 檔、單一資料日），
    交易所端沒有多年歷史可回補，所以從現在開始每週存一次是唯一的累積方式。
    """
    client = RateLimitedClient(
        config.TPEX_MIN_INTERVAL, config.USER_AGENT, 120, config.MAX_RETRIES
    )
    text = client.get_text(TDCC_URL)

    reader = csv.DictReader(io.StringIO(text))
    rows = []
    data_dates = set()
    for item in reader:
        data_date = item.get("資料日期", "").strip()
        level = item.get("持股分級", "").strip()
        if not data_date or level in ("", "16", "17"):   # 16 = 合計列
            continue
        try:
            level_i = int(level)
        except ValueError:
            continue
        if not 1 <= level_i <= 15:
            continue
        data_dates.add(data_date)
        rows.append(
            {
                "data_date": f"{data_date[:4]}-{data_date[4:6]}-{data_date[6:8]}",
                "stock_id": item.get("證券代號", "").strip(),
                "level": level_i,
                "holders": int(item.get("人數", "0") or 0),
                "shares": int(item.get("股數", "0") or 0),
                "pct": float(item.get("占集保庫存數比例%", "0") or 0),
            }
        )

    if not rows:
        log.error("集保資料解析後為空")
        return

    con = db.connect(config.DB_PATH)

    # 這份資料是週頻的：同一週的快照會一直掛在 API 上直到下週六被換掉。
    # 每天跑不會多拿到東西，但等於把「週六單次機會」變成「七天補救窗口」，
    # 所以照跑無妨 —— 已經有的那一週就直接跳過，不重寫。
    existing = {
        r[0].isoformat()
        for r in con.execute("SELECT DISTINCT data_date FROM tdcc_dist").fetchall()
    }
    fetched = {f"{d[:4]}-{d[4:6]}-{d[6:8]}" for d in data_dates}
    new_dates = fetched - existing
    if not new_dates:
        log.info("集保：%s 這週的資料已在庫，無新增（下一份會在下週六上架）",
                 ", ".join(sorted(fetched)))
        con.close()
        return

    # avail_date 每個資料日只需算一次，不要每列都去查一次資料庫
    avail = {
        d: next_trading_day(con, datetime.strptime(d, "%Y-%m-%d").date())
        for d in sorted(new_dates)
    }
    rows = [r for r in rows if r["data_date"] in new_dates]
    for row in rows:
        row["avail_date"] = avail[row["data_date"]]

    from src.db import _upsert
    n = _upsert(con, "tdcc_dist", rows, ["data_date", "stock_id", "level"])
    weeks = con.execute("SELECT count(DISTINCT data_date) FROM tdcc_dist").fetchone()[0]
    log.info("集保新增 %d 列，資料日 %s，可使用日 %s；累計 %d 週",
             n, ", ".join(sorted(new_dates)), ", ".join(str(v) for v in avail.values()), weeks)
    if weeks < 5:
        log.info("      （Δ4W 需要 5 週快照，還差 %d 週）", 5 - weeks)
    con.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD，預設今天")
    ap.add_argument("--tdcc", action="store_true", help="改抓集保股權分散表（週六跑）")
    args = ap.parse_args()

    setup_logging()
    if args.tdcc:
        update_tdcc()
        return
    target = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else date.today()
    update_market(target)


if __name__ == "__main__":
    main()
