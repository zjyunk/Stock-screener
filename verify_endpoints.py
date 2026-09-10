"""端點健檢：回補前先跑這個。

3 年回補要跑 3 小時，欄位順序猜錯就是白跑。這支程式：
  1. 打一次每個端點，確認還活著
  2. 把上櫃法人的「位置解析」拿去對 openapi 的「具名欄位」交叉驗證
     （openapi 只有最新一天，但欄位有英文名字，正好可以驗位置）

用法：python verify_endpoints.py
"""

import json
import logging
import sys
from datetime import date, timedelta

import config
from src import tpex, twse
from src.http_client import RateLimitedClient

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

OK = "  OK  "
FAIL = " FAIL "


def recent_weekday(days_back=1):
    d = date.today() - timedelta(days=days_back)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def main():
    twse_client = RateLimitedClient(
        config.TWSE_MIN_INTERVAL, config.USER_AGENT, config.HTTP_TIMEOUT, config.MAX_RETRIES
    )
    tpex_client = RateLimitedClient(
        config.TPEX_MIN_INTERVAL, config.USER_AGENT, config.HTTP_TIMEOUT, config.MAX_RETRIES
    )

    d = recent_weekday(1)
    date_str = d.strftime("%Y%m%d")
    date_iso = d.isoformat()
    old = recent_weekday(365 * 2)          # 兩年前，確認歷史查得到
    old_str = old.strftime("%Y%m%d")

    failures = 0

    def check(label, fn):
        nonlocal failures
        try:
            note = fn()
            print(f"[{OK}] {label}  {note}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"[{FAIL}] {label}  {type(exc).__name__}: {exc}")

    print(f"\n=== 端點健檢  近期日={date_iso}  歷史日={old.isoformat()} ===\n")

    check("TWSE 收盤行情（近期）", lambda: _n(twse.parse_daily_quote(
        twse_client.get_text(twse.DAILY_QUOTE_URL,
                             {"date": date_str, "type": twse.QUOTE_TYPE, "response": "json"}), date_iso)))

    check("TWSE 收盤行情（2年前）", lambda: _n(twse.parse_daily_quote(
        twse_client.get_text(twse.DAILY_QUOTE_URL,
                             {"date": old_str, "type": twse.QUOTE_TYPE, "response": "json"}), old.isoformat())))

    check("TWSE 三大法人（近期）", lambda: _n(twse.parse_insti(
        twse_client.get_text(twse.INSTI_URL,
                             {"date": date_str, "selectType": "ALLBUT0999", "response": "json"}), date_iso)))

    check("TWSE 除權息", lambda: _n(twse.parse_exright(
        twse.fetch_exright(twse_client, (d - timedelta(days=60)).strftime("%Y%m%d"), date_str))))

    check("TWSE 上市基本資料", lambda: _n(twse.parse_company_master(
        twse.fetch_company_master(twse_client))))

    check("TPEx 收盤行情（近期）", lambda: _n(tpex.parse_daily_quote(
        tpex_client.get_text(tpex.QUOTE_URL,
                             {"date": tpex.to_slash_date(date_str), "type": "EW", "id": "",
                              "response": "json"}), date_iso)))

    check("TPEx 收盤行情（2年前）", lambda: _n(tpex.parse_daily_quote(
        tpex_client.get_text(tpex.QUOTE_URL,
                             {"date": tpex.to_slash_date(old_str), "type": "EW", "id": "",
                              "response": "json"}), old.isoformat())))

    check("TPEx 三大法人（近期）", lambda: _n(tpex.parse_insti(
        tpex_client.get_text(tpex.INSTI_URL,
                             {"type": "Daily", "sect": "EW", "date": tpex.to_slash_date(date_str),
                              "id": "", "response": "json"}), date_iso)))

    check("TPEx 上櫃基本資料", lambda: _n(tpex.parse_company_master(
        tpex.fetch_company_master(tpex_client))))

    print()
    if not _verify_tpex_columns(tpex_client):
        failures += 1

    print()
    if failures:
        print(f"*** {failures} 項失敗，先修好再跑回補 ***")
        return 1
    print("全部通過，可以跑 run_backfill.py")
    return 0


def _n(rows):
    return f"{len(rows)} 檔"


def _verify_tpex_columns(client):
    """把位置解析的結果，對 openapi 的具名欄位逐檔比對。

    上櫃法人的 web JSON 有 24 個欄位而且欄名重複（7 組買進/賣出/買賣超），
    只能靠位置解析。這裡用 openapi 的英文具名欄位當真值來驗位置對不對。
    """
    print("--- 上櫃法人欄位順序交叉驗證 ---")
    try:
        named = json.loads(client.get_text(tpex.OPENAPI_INSTI_URL))
    except Exception as exc:  # noqa: BLE001
        print(f"[{FAIL}] 無法取得 openapi 具名資料：{exc}")
        return False

    if not named:
        print(f"[{FAIL}] openapi 回傳空資料")
        return False

    day = str(named[0].get("Date", ""))          # 民國 YYYMMDD
    if len(day) != 7:
        print(f"[{FAIL}] 無法解析 openapi 日期：{day!r}")
        return False
    iso = f"{int(day[:3]) + 1911}-{day[3:5]}-{day[5:7]}"
    date_str = iso.replace("-", "")

    def pick(item, *needles):
        for key, value in item.items():
            flat = key.replace(" ", "").lower()
            if all(n.replace(" ", "").lower() in flat for n in needles):
                return value
        return None

    truth = {}
    for item in named:
        code = str(item.get("SecuritiesCompanyCode", "")).strip()
        truth[code] = {
            "foreign_net": pick(item, "ForeignDealersexcluded", "Difference")
            or pick(item, "ForeignDealers excluded", "Difference"),
            "trust_net": pick(item, "SecuritiesInvestmentTrust", "Difference"),
        }

    parsed = tpex.parse_insti(
        client.get_text(
            tpex.INSTI_URL,
            {"type": "Daily", "sect": "EW", "date": tpex.to_slash_date(date_str),
             "id": "", "response": "json"},
        ),
        iso,
    )
    if not parsed:
        print(f"[{FAIL}] 位置解析在 {iso} 拿不到資料（可能當天尚未收盤）")
        return False

    checked = mismatch = 0
    examples = []
    for row in parsed:
        ref = truth.get(row["stock_id"])
        if not ref:
            continue
        for field in ("foreign_net", "trust_net"):
            expect = ref.get(field)
            if expect in (None, ""):
                continue
            checked += 1
            try:
                expect_i = int(str(expect).replace(",", ""))
            except ValueError:
                continue
            if row[field] != expect_i:
                mismatch += 1
                if len(examples) < 5:
                    examples.append(f"{row['stock_id']} {field}: 解析={row[field]} 應為={expect_i}")

    if checked == 0:
        print(f"[{FAIL}] 無可比對資料")
        return False
    if mismatch:
        print(f"[{FAIL}] {mismatch}/{checked} 筆不符 —— 欄位順序已改，請調整 tpex.INSTI_COL")
        for line in examples:
            print("        " + line)
        return False

    print(f"[{OK}] {checked} 筆比對全數相符，tpex.INSTI_COL 位置正確")
    return True


if __name__ == "__main__":
    sys.exit(main())
