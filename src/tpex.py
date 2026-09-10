"""上櫃（TPEx）資料來源。

重要（2026-09 實測）：
  * 新站端點 /www/zh-tw/... 吃的是「西元」日期 YYYY/MM/DD，不是民國年。
    網路上多數教學寫的民國年格式是舊站的，舊站現在會忽略日期參數直接回最新一天。
  * 新站可以查到至少 3 年前的資料（實測 2023/09/05 正常）。
  * openapi/v1/... 只有「最新一天」，不能用來回補，只適合當每日增量或欄位交叉驗證。
"""

import json
import logging

from . import clean, raw_store

log = logging.getLogger(__name__)

QUOTE_URL = "https://www.tpex.org.tw/www/zh-tw/afterTrading/otc"
INSTI_URL = "https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade"
COMPANY_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"
# 只有最新一天，用於欄位順序交叉驗證
OPENAPI_INSTI_URL = "https://www.tpex.org.tw/openapi/v1/tpex_3insti_daily_trading"
QFII_URL = "https://www.tpex.org.tw/www/zh-tw/insti/qfii"
MARGIN_URL = "https://www.tpex.org.tw/www/zh-tw/margin/balance"

SOURCE_QUOTE = "tpex_quote"
SOURCE_INSTI = "tpex_insti"
SOURCE_QFII = "tpex_qfii"
SOURCE_MARGIN = "tpex_margin"


def to_slash_date(date_str):
    """YYYYMMDD -> YYYY/MM/DD"""
    return f"{date_str[:4]}/{date_str[4:6]}/{date_str[6:8]}"


# ------------------------------------------------------------------ 抓取
def fetch_daily_quote(client, raw_dir, date_str, skip_existing=True):
    if skip_existing and raw_store.exists(raw_dir, SOURCE_QUOTE, date_str):
        return False
    text = client.get_text(
        QUOTE_URL,
        params={"date": to_slash_date(date_str), "type": "EW", "id": "", "response": "json"},
    )
    if not _has_data(text):
        return False
    raw_store.write(raw_dir, SOURCE_QUOTE, date_str, text)
    return True


def fetch_insti(client, raw_dir, date_str, skip_existing=True):
    if skip_existing and raw_store.exists(raw_dir, SOURCE_INSTI, date_str):
        return False
    text = client.get_text(
        INSTI_URL,
        params={
            "type": "Daily",
            "sect": "EW",
            "date": to_slash_date(date_str),
            "id": "",
            "response": "json",
        },
    )
    if not _has_data(text):
        return False
    raw_store.write(raw_dir, SOURCE_INSTI, date_str, text)
    return True


def fetch_qfii(client, raw_dir, date_str, skip_existing=True):
    if skip_existing and raw_store.exists(raw_dir, SOURCE_QFII, date_str):
        return False
    text = client.get_text(
        QFII_URL,
        params={"date": to_slash_date(date_str), "type": "Daily", "id": "", "response": "json"},
    )
    if not _has_data(text):
        return False
    raw_store.write(raw_dir, SOURCE_QFII, date_str, text)
    return True


def fetch_margin(client, raw_dir, date_str, skip_existing=True):
    if skip_existing and raw_store.exists(raw_dir, SOURCE_MARGIN, date_str):
        return False
    text = client.get_text(
        MARGIN_URL,
        params={"date": to_slash_date(date_str), "id": "", "response": "json"},
    )
    if not _has_data(text):
        return False
    raw_store.write(raw_dir, SOURCE_MARGIN, date_str, text)
    return True


def fetch_company_master(client):
    return client.get_text(COMPANY_URL)


def _has_data(text):
    """交易所對「當天還沒公布」會回 200 + stat 說明，不是錯誤。

    這種回應不能落地成 raw 檔：run_backfill 的 skip_existing 看到檔案存在就跳過，
    那一天就會永久缺資料。收盤後太早跑很容易踩到 —— 外資持股與融資融券
    公布得比行情晚。
    """
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return False
    if isinstance(payload, list):
        return bool(payload)
    stat = str(payload.get("stat", "")).strip()
    if stat.lower() != "ok".lower():
        return False
    if payload.get("data"):
        return True
    return any(t.get("data") for t in (payload.get("tables") or []))


# ------------------------------------------------------------------ 解析
def _first_table(payload):
    tables = payload.get("tables") or []
    return tables[0] if tables else None


def parse_daily_quote(text, date_iso):
    """欄位順序（新站，2026-09 實測）：
      0 代號 / 1 名稱 / 2 收盤 / 3 漲跌 / 4 開盤 / 5 最高 / 6 最低
      7 成交股數 / 8 成交金額(元) / 9 成交筆數
      10-13 最後買賣價量 / 14 發行股數 / 15 次日漲停 / 16 次日跌停

    注意上櫃的「發行股數」直接給在行情表裡（上市要另外從基本資料表拿），
    這正好是法人買超佔股本比例的分母。
    """
    payload = json.loads(text)
    if str(payload.get("stat", "")).lower() != "ok":
        return []

    table = _first_table(payload)
    if table is None:
        return []

    rows = []
    for row in table.get("data", []):
        code = clean.text(row[0])
        if not clean.is_common_stock(code):
            continue
        close = clean.to_float(row[2])
        if close is None:
            continue
        rows.append(
            {
                "trade_date": date_iso,
                "stock_id": code,
                "name": clean.text(row[1]),
                "market": "TPEx",
                "close": close,
                "open": clean.to_float(row[4]),
                "high": clean.to_float(row[5]),
                "low": clean.to_float(row[6]),
                "volume": clean.to_int(row[7]),
                "turnover": clean.to_int(row[8]),
                "trades": clean.to_int(row[9]),
                "shares_outstanding": clean.to_int(row[14]) if len(row) > 14 else None,
            }
        )
    return rows


# 欄位順序由 verify_endpoints.py 對 openapi 具名欄位交叉驗證，改版時會驗出來
INSTI_COL = {
    "foreign_net": 4,    # 外資及陸資買賣超（不含外資自營商）
    "trust_net": 13,     # 投信買賣超
    "dealer_net": 22,    # 自營商買賣超（合計）
    "total_net": 23,     # 三大法人買賣超合計
}


def parse_insti(text, date_iso):
    payload = json.loads(text)
    if str(payload.get("stat", "")).lower() != "ok":
        return []

    table = _first_table(payload)
    if table is None:
        return []

    rows = []
    for row in table.get("data", []):
        code = clean.text(row[0])
        if not clean.is_common_stock(code):
            continue
        if len(row) <= INSTI_COL["total_net"]:
            continue
        rows.append(
            {
                "trade_date": date_iso,
                "stock_id": code,
                "market": "TPEx",
                "foreign_net": clean.to_int(row[INSTI_COL["foreign_net"]]),
                "trust_net": clean.to_int(row[INSTI_COL["trust_net"]]),
                "dealer_net": clean.to_int(row[INSTI_COL["dealer_net"]]),
                "total_net": clean.to_int(row[INSTI_COL["total_net"]]),
            }
        )
    return rows


def parse_company_master(text):
    """注意：TPEx 給的是產業「代碼」（SecuritiesIndustryCode），
    上市給的是產業「名稱」。Phase 5 類股金流要跨市場比較前需要先做碼表對應。
    """
    rows = []
    for item in json.loads(text):
        code = clean.text(item.get("SecuritiesCompanyCode"))
        if not clean.is_common_stock(code):
            continue
        rows.append(
            {
                "stock_id": code,
                "name": clean.text(item.get("CompanyAbbreviation")) or clean.text(item.get("CompanyName")),
                "market": "TPEx",
                "industry": clean.text(item.get("SecuritiesIndustryCode")),
            }
        )
    return rows


def parse_qfii(text, date_iso):
    """上櫃外資持股。欄位跟上市不同，第 0 欄是「排行」不是代號：
      0 排行 / 1 代號 / 2 名稱 / 3 發行股數(A)
      4 尚可投資股數(B) / 5 僑外資及陸資持有股數(C)
      6 尚可投資比率(D) / 7 僑外資及陸資持股比率(E)  ← 主要欄位
    """
    payload = json.loads(text)
    if str(payload.get("stat", "")).lower() != "ok":
        return []

    table = _first_table(payload)
    if table is None:
        return []

    rows = []
    for row in table.get("data", []):
        if len(row) < 8:
            continue
        code = clean.text(row[1])
        if not clean.is_common_stock(code):
            continue
        pct = clean.to_float(str(row[7]).replace("%", ""))
        if pct is None:
            continue
        rows.append(
            {
                "trade_date": date_iso,
                "stock_id": code,
                "market": "TPEx",
                "issued_shares": clean.to_int(row[3]),
                "foreign_shares": clean.to_int(row[5]),
                "foreign_pct": pct,
            }
        )
    return rows


def parse_margin(text, date_iso):
    """上櫃融資融券。欄位：
      0 代號 / 1 名稱
      融資 2 前餘額 3 資買 4 資賣 5 現償 6 資餘額 7 屬證金 8 使用率 9 限額
      融券 10 前餘額 11 券賣 12 券買 13 券償 14 券餘額 ...
    餘額單位是「張」，與上市一致。
    """
    payload = json.loads(text)
    if str(payload.get("stat", "")).lower() != "ok":
        return []

    table = _first_table(payload)
    if table is None:
        return []

    rows = []
    for row in table.get("data", []):
        if len(row) < 15:
            continue
        code = clean.text(row[0])
        if not clean.is_common_stock(code):
            continue
        rows.append(
            {
                "trade_date": date_iso,
                "stock_id": code,
                "market": "TPEx",
                "margin_balance": clean.to_int(row[6]),
                "margin_prev": clean.to_int(row[2]),
                "short_balance": clean.to_int(row[14]),
                "short_prev": clean.to_int(row[10]),
            }
        )
    return rows
