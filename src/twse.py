"""上市（TWSE）資料來源。

端點皆為 2026-09 實測可用。舊的 /exchangeReport/ 與 /fund/ 路徑目前仍會轉到
新的 /rwd/zh/ 路徑，但直接用新路徑比較保險。

抓「單日全市場」而不是「單股全期間」：1,800 檔逐檔抓會被擋，
MI_INDEX 一天一個請求就拿到全市場。
"""

import json
import logging

from . import clean, raw_store

log = logging.getLogger(__name__)

DAILY_QUOTE_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
INSTI_URL = "https://www.twse.com.tw/rwd/zh/fund/T86"
EXRIGHT_URL = "https://www.twse.com.tw/rwd/zh/exRight/TWT49U"
COMPANY_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
QFII_URL = "https://www.twse.com.tw/rwd/zh/fund/MI_QFIIS"
MARGIN_URL = "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN"

# ALLBUT0999 = 全部但不含權證、牛熊證、可展延牛熊證
# 用 ALL 的話單日回應 5MB / 33,000 列（含三萬檔權證），ALLBUT0999 只有 218KB / 1,300 列
QUOTE_TYPE = "ALLBUT0999"

SOURCE_QUOTE = "twse_quote"
SOURCE_INSTI = "twse_insti"
SOURCE_QFII = "twse_qfii"
SOURCE_MARGIN = "twse_margin"


# ------------------------------------------------------------------ 抓取
def fetch_daily_quote(client, raw_dir, date_str, skip_existing=True):
    if skip_existing and raw_store.exists(raw_dir, SOURCE_QUOTE, date_str):
        return False
    text = client.get_text(
        DAILY_QUOTE_URL,
        params={"date": date_str, "type": QUOTE_TYPE, "response": "json"},
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
        params={"date": date_str, "selectType": "ALLBUT0999", "response": "json"},
    )
    if not _has_data(text):
        return False
    raw_store.write(raw_dir, SOURCE_INSTI, date_str, text)
    return True


def fetch_qfii(client, raw_dir, date_str, skip_existing=True):
    """外資及陸資持股統計。這是「存量」——籌碼現在在不在外資手上。

    跟 T86 的買賣超（流量）不同：買賣超說今天買了多少，持股比率說現在握著多少。
    要看「籌碼流回外資」看的是這條線的趨勢，不是集保（集保分不出持有人身分）。
    """
    if skip_existing and raw_store.exists(raw_dir, SOURCE_QFII, date_str):
        return False
    text = client.get_text(
        QFII_URL, params={"date": date_str, "selectType": "ALLBUT0999", "response": "json"}
    )
    if not _has_data(text):
        return False
    raw_store.write(raw_dir, SOURCE_QFII, date_str, text)
    return True


def fetch_margin(client, raw_dir, date_str, skip_existing=True):
    """融資融券餘額。融資是散戶槓桿的主要工具，可當散戶動向的代理指標。"""
    if skip_existing and raw_store.exists(raw_dir, SOURCE_MARGIN, date_str):
        return False
    text = client.get_text(
        MARGIN_URL, params={"date": date_str, "selectType": "ALL", "response": "json"}
    )
    if not _has_data(text):
        return False
    raw_store.write(raw_dir, SOURCE_MARGIN, date_str, text)
    return True


def fetch_exright(client, start_date, end_date):
    """除權息計算結果表。日期為 YYYYMMDD，區間查詢。"""
    return client.get_text(
        EXRIGHT_URL,
        params={"startDate": start_date, "endDate": end_date, "response": "json"},
    )


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
    if stat.lower() != "OK".lower():
        return False
    if payload.get("data"):
        return True
    return any(t.get("data") for t in (payload.get("tables") or []))


# ------------------------------------------------------------------ 解析
def _find_table(payload, keyword):
    """MI_INDEX 一次回傳 10 張表（指數、大盤統計、收盤行情…），依標題取。

    用關鍵字比對而不是寫死索引，因為表的張數會隨日期變動
    （早期沒有「臺灣指數公司」那幾張）。
    """
    for table in payload.get("tables", []):
        if keyword in str(table.get("title", "")):
            return table
    return None


def parse_daily_quote(text, date_iso):
    """回傳 list[dict]，只保留 4 碼普通股。"""
    payload = json.loads(text)
    if payload.get("stat") != "OK":
        return []

    table = _find_table(payload, "每日收盤行情")
    if table is None:
        log.warning("twse %s: 找不到每日收盤行情表", date_iso)
        return []

    rows = []
    for row in table.get("data", []):
        code = clean.text(row[0])
        if not clean.is_common_stock(code):
            continue
        close = clean.to_float(row[8])
        if close is None:
            continue  # 全日無成交
        rows.append(
            {
                "trade_date": date_iso,
                "stock_id": code,
                "name": clean.text(row[1]),
                "market": "TWSE",
                "volume": clean.to_int(row[2]),        # 成交股數
                "trades": clean.to_int(row[3]),        # 成交筆數
                "turnover": clean.to_int(row[4]),      # 成交金額（元）
                "open": clean.to_float(row[5]),
                "high": clean.to_float(row[6]),
                "low": clean.to_float(row[7]),
                "close": close,
            }
        )
    return rows


def parse_insti(text, date_iso):
    """三大法人買賣超。

    欄位順序（T86，2026-09 實測）：
      0 證券代號 / 1 證券名稱
      2-4   外陸資買進/賣出/買賣超（不含外資自營商）  <- 用這個，比總外資乾淨
      5-7   外資自營商
      8-10  投信買進/賣出/買賣超
      11    自營商買賣超（合計）
      12-14 自營商（自行買賣）
      15-17 自營商（避險）
      18    三大法人買賣超股數
    """
    payload = json.loads(text)
    if payload.get("stat") != "OK":
        return []

    rows = []
    for row in payload.get("data", []):
        code = clean.text(row[0])
        if not clean.is_common_stock(code):
            continue
        rows.append(
            {
                "trade_date": date_iso,
                "stock_id": code,
                "market": "TWSE",
                "foreign_net": clean.to_int(row[4]),
                "trust_net": clean.to_int(row[10]),
                "dealer_net": clean.to_int(row[11]),
                "total_net": clean.to_int(row[18]),
            }
        )
    return rows


def parse_exright(text):
    """除權息。回傳 list[dict]，用來建還原因子。"""
    payload = json.loads(text)
    if payload.get("stat") != "OK":
        return []

    rows = []
    for row in payload.get("data", []):
        date_iso = clean.roc_to_iso(row[0])
        code = clean.text(row[1])
        if date_iso is None or not clean.is_common_stock(code):
            continue
        prev_close = clean.to_float(row[3])   # 除權息前收盤價
        ref_price = clean.to_float(row[4])    # 除權息參考價
        if not prev_close or not ref_price:
            continue
        rows.append(
            {
                "ex_date": date_iso,
                "stock_id": code,
                "market": "TWSE",
                "prev_close": prev_close,
                "ref_price": ref_price,
                # 後復權因子：除權息當日之前的價格要乘上 ref/prev 才能跟之後接得起來
                "factor": ref_price / prev_close,
            }
        )
    return rows


def parse_company_master(text):
    """上市沒有直接給發行股數，用 實收資本額 ÷ 每股面額 推算。

    台積電實測：259,323,700,670 / 10 = 25,932,370,067 股，
    對實際流通 25,930,380,458 股誤差 0.008%，當法人買超佔比的分母綽綽有餘。
    面額非台幣或無面額者回 None，選股時該股的佔比條件會直接不通過。
    """
    rows = []
    for item in json.loads(text):
        code = clean.text(item.get("公司代號"))
        if not clean.is_common_stock(code):
            continue

        capital = clean.to_float(item.get("實收資本額"))
        par = clean.par_value(item.get("普通股每股面額"))
        shares = int(capital / par) if capital and par else None

        rows.append(
            {
                "stock_id": code,
                "name": clean.text(item.get("公司簡稱")),
                "market": "TWSE",
                "industry": clean.text(item.get("產業別")),
                "shares_outstanding": shares,
            }
        )
    return rows


def parse_qfii(text, date_iso):
    """MI_QFIIS 欄位（2026-09 實測）：
      0 證券代號 / 1 證券名稱 / 2 國際證券編碼 / 3 發行股數
      4 尚可投資股數 / 5 全體外資及陸資持有股數
      6 尚可投資比率 / 7 全體外資及陸資持股比率  ← 主要欄位
    """
    payload = json.loads(text)
    if payload.get("stat") != "OK":
        return []

    rows = []
    for row in payload.get("data", []):
        code = clean.text(row[0])
        if not clean.is_common_stock(code):
            continue
        pct = clean.to_float(row[7])
        if pct is None:
            continue
        rows.append(
            {
                "trade_date": date_iso,
                "stock_id": code,
                "market": "TWSE",
                "issued_shares": clean.to_int(row[3]),
                "foreign_shares": clean.to_int(row[5]),
                "foreign_pct": pct,
            }
        )
    return rows


def parse_margin(text, date_iso):
    """MI_MARGN 的第二張表「融資融券彙總」，欄位：
      0 代號 / 1 名稱
      融資 2 買進 3 賣出 4 現金償還 5 前日餘額 6 今日餘額 7 限額
      融券 8 買進 9 賣出 10 現券償還 11 前日餘額 12 今日餘額 13 限額
      14 資券互抵 / 15 註記
    餘額單位是「張」。
    """
    payload = json.loads(text)
    if payload.get("stat") != "OK":
        return []

    table = _find_table(payload, "融資融券彙總")
    if table is None:
        return []

    rows = []
    for row in table.get("data", []):
        code = clean.text(row[0])
        if not clean.is_common_stock(code) or len(row) < 13:
            continue
        rows.append(
            {
                "trade_date": date_iso,
                "stock_id": code,
                "market": "TWSE",
                "margin_balance": clean.to_int(row[6]),
                "margin_prev": clean.to_int(row[5]),
                "short_balance": clean.to_int(row[12]),
                "short_prev": clean.to_int(row[11]),
            }
        )
    return rows
