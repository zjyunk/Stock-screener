"""個股頁報表的資料組裝。

每檔股票抓近 N 個交易日的還原價、指標、法人流量、外資持股、融資、集保，
組成一個可以直接塞進 HTML 的 dict。圖表在瀏覽器端用 lightweight-charts 畫，
這裡只負責把數字整理好。
"""

import json
from datetime import date

import pandas as pd

from . import indicators

DISPLAY_DAYS = 120       # 畫面上顯示幾個交易日
WARMUP_DAYS = 80         # 指標暖身，多抓這麼多天再切掉


def _clean(value):
    """缺值 -> None，其餘轉成 JSON 可接受的型別。

    pandas 3.0 的缺值可能是 NaN、None 或 pd.NA（nullable dtype），
    只有 pd.isna 三種都認得；直接拿 math.isnan 會漏掉 pd.NA。
    """
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass                                # list 之類的不是純量，跳過
    if isinstance(value, (pd.Timestamp, date)):
        return value.strftime("%Y-%m-%d")
    if hasattr(value, "item"):              # numpy scalar
        return value.item()
    return value


def stock_series(con, stock_id, trade_date, display_days=DISPLAY_DAYS):
    """回傳單一個股的時間序列，list[dict]，由舊到新。"""
    frame = con.execute(
        """
        WITH win AS (
            SELECT DISTINCT trade_date FROM daily_price
            WHERE trade_date <= ? ORDER BY trade_date DESC LIMIT ?
        )
        SELECT a.trade_date, a.stock_id,
               a.open, a.high, a.low, a.close, a.raw_close, a.volume, a.turnover,
               i.foreign_net, i.trust_net,
               f.foreign_pct AS hold_pct,
               m.margin_balance, m.short_balance
        FROM v_adjusted_price a
        LEFT JOIN inst_flow i USING (trade_date, stock_id)
        LEFT JOIN foreign_holding f USING (trade_date, stock_id)
        LEFT JOIN margin_balance m USING (trade_date, stock_id)
        WHERE a.stock_id = ?
          AND a.trade_date IN (SELECT trade_date FROM win)
        ORDER BY a.trade_date
        """,
        [trade_date, display_days + WARMUP_DAYS, stock_id],
    ).df()

    if frame.empty:
        return []

    frame = indicators.compute(frame)
    frame = frame.tail(display_days)

    columns = [
        "trade_date", "open", "high", "low", "close", "raw_close", "volume", "turnover",
        "ma", "dif", "macd", "osc", "k", "d",
        "foreign_net", "trust_net", "hold_pct", "margin_balance", "short_balance",
    ]
    rows = []
    for record in frame[columns].itertuples(index=False):
        rows.append({col: _clean(val) for col, val in zip(columns, record)})
    return rows


def chip_series(con, stock_id, trade_date):
    """集保週頻資料：大戶／散戶持股比與人數，按可使用日排序。"""
    frame = con.execute(
        """
        SELECT avail_date,
               sum(CASE WHEN level = 15 THEN pct ELSE 0 END)                AS whale_pct,
               sum(CASE WHEN level BETWEEN 1 AND 8 THEN pct ELSE 0 END)     AS retail_pct,
               sum(CASE WHEN level BETWEEN 1 AND 8 THEN holders ELSE 0 END) AS retail_holders
        FROM tdcc_dist
        WHERE stock_id = ? AND avail_date <= ?
        GROUP BY avail_date ORDER BY avail_date
        """,
        [stock_id, trade_date],
    ).df()
    return [
        {k: _clean(v) for k, v in zip(frame.columns, rec)}
        for rec in frame.itertuples(index=False)
    ]


def build_payload(con, result, trade_date, params, stock_ids=None):
    """把選股結果 + 各檔時間序列組成一個 payload。

    result 是 screener.run() 的輸出；預設只帶入選的，stock_ids 可額外指定。
    """
    picked = result[result["selected"]].copy()
    if stock_ids:
        extra = result[result["stock_id"].isin(stock_ids) & ~result["selected"]]
        picked = pd.concat([picked, extra])

    stocks = []
    for row in picked.itertuples(index=False):
        series = stock_series(con, row.stock_id, trade_date)
        if not series:
            continue
        stocks.append(
            {
                "stock_id": row.stock_id,
                "name": _clean(row.name),
                "market": row.market,
                "industry": _clean(getattr(row, "industry", None)),
                "mode": _clean(getattr(row, "mode", "")) or "",
                "selected": bool(row.selected),
                "rank": int(row.rank_ma5),
                "days_in_rank": int(row.days_in_rank),
                "close": _clean(row.close),
                "foreign_sum": _clean(getattr(row, "foreign_sum", None)),
                "foreign_pattern": _clean(getattr(row, "foreign_pattern", "")),
                "hold_pct": _clean(getattr(row, "hold_pct", None)),
                "hold_chg_pp": _clean(getattr(row, "hold_chg_pp", None)),
                "margin_chg_pct": _clean(getattr(row, "margin_chg_pct", None)),
                "whale_pct": _clean(getattr(row, "whale_pct", None)),
                "retail_pct": _clean(getattr(row, "retail_pct", None)),
                "series": series,
                "chips": chip_series(con, row.stock_id, trade_date),
            }
        )

    return {
        "trade_date": _clean(trade_date),
        "params": {k: _clean(v) for k, v in params.items()},
        "stocks": stocks,
    }


def to_json(payload):
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
