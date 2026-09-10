"""三層漏斗選股。

Layer 1  母體    近 N 個交易日「持續」出現在成交值排行榜內（不是只看今天）
Layer 2  技術    KD 與 MACD 漸漸往上，且收盤價站上均線
Layer 3  法人    近一週的買賣超狀況

每層都回傳「是否通過」的欄位而不是直接濾掉，這樣才能看出每層各刷掉多少、
瓶頸在哪一層。融資融券只顯示不篩選 —— 三年實證推翻了「融資減 = 籌碼乾淨」
這個判讀，見 config.SHOW_MARGIN 的註解。
"""

import logging

import pandas as pd

from . import indicators

log = logging.getLogger(__name__)


def resolve_trade_date(con, target=None):
    """回傳 <= target 的最後一個有資料的交易日。target 為 None 則取最新。"""
    if target is None:
        row = con.execute("SELECT max(trade_date) FROM daily_price").fetchone()
    else:
        row = con.execute(
            "SELECT max(trade_date) FROM daily_price WHERE trade_date <= ?", [str(target)]
        ).fetchone()
    return row[0] if row else None


def recent_dates(con, trade_date, n):
    """trade_date 往前數 n 個交易日（含當天），由舊到新。"""
    return [
        r[0]
        for r in con.execute(
            """
            SELECT trade_date FROM (
                SELECT DISTINCT trade_date FROM daily_price
                WHERE trade_date <= ? ORDER BY trade_date DESC LIMIT ?
            ) ORDER BY trade_date
            """,
            [trade_date, n],
        ).fetchall()
    ]


# ------------------------------------------------------------ Layer 1
def layer1_universe(con, trade_date, top_n, lookback, min_days):
    """持續出現在成交值排行榜內。

    只看「今天」在不在榜上，會抓到一日爆量的個股；要求近 lookback 日裡
    至少 min_days 天都在榜內，選到的才是資金持續停留的標的。

    ETF／權證／DR 在入庫時已由 clean.is_common_stock 擋掉。
    尚未處理：處置股（分盤集合競價，實際上進不去也出不來）。
    """
    return con.execute(
        """
        WITH win AS (
            SELECT DISTINCT trade_date FROM daily_price
            WHERE trade_date <= ? ORDER BY trade_date DESC LIMIT ?
        ),
        hits AS (
            SELECT stock_id,
                   count(*)      AS days_in_rank,
                   min(rank_ma5) AS best_rank,
                   max(rank_ma5) AS worst_rank
            FROM v_turnover_rank
            WHERE trade_date IN (SELECT trade_date FROM win) AND rank_ma5 <= ?
            GROUP BY stock_id
            HAVING count(*) >= ?
        )
        SELECT r.stock_id, r.market, r.rank_ma5, r.rank_today,
               r.turnover_ma5, r.turnover,
               h.days_in_rank, h.best_rank, h.worst_rank,
               m.name, m.industry, m.shares_outstanding
        FROM v_turnover_rank r
        JOIN hits h USING (stock_id)
        LEFT JOIN security_master m USING (stock_id)
        WHERE r.trade_date = ?
        ORDER BY r.rank_ma5
        """,
        [trade_date, lookback, top_n, min_days, trade_date],
    ).df()


# ------------------------------------------------------------ Layer 2
def layer2_technical(con, trade_date, stock_ids, params, history_days=140):
    """KD 與 MACD 漸漸往上，且站上均線。全部用還原價計算。

    「漸漸往上」= 今天比昨天高，而且比 TREND_LOOKBACK 天前也高。
    只比昨天會抓到雜訊反彈，只比 N 天前會漏掉今天剛開始回落的情況。
    """
    if not stock_ids:
        return pd.DataFrame()

    prices = con.execute(
        """
        WITH win AS (
            SELECT DISTINCT trade_date FROM daily_price
            WHERE trade_date <= ? ORDER BY trade_date DESC LIMIT ?
        )
        SELECT a.trade_date, a.stock_id, a.open, a.high, a.low, a.close, a.turnover
        FROM v_adjusted_price a
        WHERE a.stock_id IN (SELECT unnest(?))
          AND a.trade_date IN (SELECT trade_date FROM win)
        """,
        [trade_date, history_days, list(stock_ids)],
    ).df()

    if prices.empty:
        return pd.DataFrame()

    enriched = indicators.compute_all(prices)
    latest = enriched[enriched["trade_date"] == pd.Timestamp(trade_date)].copy()
    if latest.empty:
        return pd.DataFrame()

    latest["pass_history"] = latest["bars"] >= indicators.MIN_HISTORY
    latest["pass_above_ma"] = (
        latest["above_ma"].fillna(False) if params["REQUIRE_ABOVE_MA"] else True
    )
    latest["pass_kd_rising"] = (
        latest["k_rising"].fillna(False) if params["REQUIRE_KD_RISING"] else True
    )
    latest["pass_macd_rising"] = (
        latest["osc_rising"].fillna(False) if params["REQUIRE_MACD_RISING"] else True
    )
    apply_tech_modes(latest, params)
    return latest


def apply_tech_modes(frame, params):
    """順勢與打底反轉兩種技術型態。共用同一份指標欄位，只是條件不同。

    順勢：KD 與 MACD 漸漸往上，且站上均線。
    反轉：DIF 仍在零軸下方但回升、柱狀體已翻紅（OSC > 0）、K > D。
          刻意不要求站上均線 —— 剛從底部翻上來的股票本來就還沒站上去，
          加了那個條件會把整類標的排除掉。

    實測細節：「剛翻紅」(+0.24%) 不如「已翻紅」(+0.39%)，抓轉折點比跟隨
    已確認的趨勢差；這跟金叉 vs 漸漸往上是同一個模式，所以用 OSC > 0 而不是
    「今天剛翻正」。
    """
    frame["pass_trend"] = (
        frame["pass_history"]
        & frame["pass_above_ma"]
        & frame["pass_kd_rising"]
        & frame["pass_macd_rising"]
    )
    frame["pass_reversal"] = (
        frame["pass_history"]
        & (frame["dif"] < 0)
        & frame["dif_rising"].fillna(False)
        & (frame["osc"] > 0)
        & (frame["k"] > frame["d"])
    )

    mode = params.get("SCREEN_MODE", "trend")
    if mode == "trend":
        frame["pass_tech"] = frame["pass_trend"]
    elif mode == "reversal":
        frame["pass_tech"] = frame["pass_reversal"]
    else:
        frame["pass_tech"] = frame["pass_trend"] | frame["pass_reversal"]

    frame["mode"] = ""
    frame.loc[frame["pass_trend"], "mode"] = "順勢"
    frame.loc[frame["pass_reversal"], "mode"] = "反轉"
    frame.loc[frame["pass_trend"] & frame["pass_reversal"], "mode"] = "兩者"
    return frame


# ------------------------------------------------------------ Layer 3
def layer3_institutional(con, trade_date, stock_ids, window):
    """近 window 個交易日的法人買賣超，含逐日型態。

    型態字串把一週的買賣畫出來（+ 買超、- 賣超、· 沒動作）。
    「連買五天」跟「大買一天其餘小賣」總和可能一樣，但意義完全不同。
    """
    if not stock_ids:
        return pd.DataFrame()

    dates = recent_dates(con, trade_date, window)
    if not dates:
        return pd.DataFrame()

    flows = con.execute(
        """
        SELECT trade_date, stock_id, foreign_net, trust_net
        FROM inst_flow
        WHERE stock_id IN (SELECT unnest(?)) AND trade_date >= ? AND trade_date <= ?
        ORDER BY stock_id, trade_date
        """,
        [list(stock_ids), dates[0], trade_date],
    ).df()

    if flows.empty:
        return pd.DataFrame()

    flows = flows.fillna({"foreign_net": 0, "trust_net": 0})
    index = {d: i for i, d in enumerate(dates)}

    def pattern(group, column):
        marks = ["·"] * len(dates)
        for row in group.itertuples():
            key = row.trade_date
            key = key.date() if hasattr(key, "date") else key
            i = index.get(key)
            if i is None:
                continue
            value = getattr(row, column)
            marks[i] = "+" if value > 0 else ("-" if value < 0 else "·")
        return "".join(marks)

    rows = []
    for stock_id, group in flows.groupby("stock_id"):
        rows.append(
            {
                "stock_id": stock_id,
                "foreign_sum": int(group["foreign_net"].sum()),
                "foreign_buy_days": int((group["foreign_net"] > 0).sum()),
                "foreign_pattern": pattern(group, "foreign_net"),
                "trust_sum": int(group["trust_net"].sum()),
                "trust_buy_days": int((group["trust_net"] > 0).sum()),
                "trust_pattern": pattern(group, "trust_net"),
            }
        )
    return pd.DataFrame(rows)


def combine_selection(frame, params):
    """把三層併成最終入選。法人層依模式決定要不要套用。

    順勢模式需要法人確認（已經在漲的股票要確認有錢在買，不是散戶追高）；
    反轉模式不需要（剛從底部翻上來時法人通常還沒進場，要求法人買超
    等於把最早期的訊號濾掉）。實證數字見 config.REVERSAL_REQUIRE_INST。
    """
    mode = params.get("SCREEN_MODE", "trend")
    trend_ok = frame["pass_trend"] & frame["pass_inst"]
    if params.get("REVERSAL_REQUIRE_INST", True):
        reversal_ok = frame["pass_reversal"] & frame["pass_inst"]
    else:
        reversal_ok = frame["pass_reversal"]

    if mode == "trend":
        selected = trend_ok
    elif mode == "reversal":
        selected = reversal_ok
    else:
        selected = trend_ok | reversal_ok

    # mode 標籤要反映「實際選上它的是哪一種」，不是「技術型態符合哪一種」
    frame["mode"] = ""
    frame.loc[reversal_ok, "mode"] = "反轉"
    frame.loc[trend_ok, "mode"] = "順勢"
    frame.loc[trend_ok & reversal_ok, "mode"] = "兩者"
    return selected


def apply_layer3(candidates, flows, params):
    merged = candidates.merge(flows, on="stock_id", how="left")
    for col in ("foreign_sum", "foreign_buy_days", "trust_sum", "trust_buy_days"):
        merged[col] = merged[col].fillna(0)
    for col in ("foreign_pattern", "trust_pattern"):
        merged[col] = merged[col].fillna("·" * params["INST_WINDOW"])

    shares = merged["shares_outstanding"].where(merged["shares_outstanding"] > 0)
    merged["foreign_pct"] = merged["foreign_sum"] / shares
    merged["trust_pct"] = merged["trust_sum"] / shares

    primary = "trust" if params.get("INST_PRIMARY") == "trust" else "foreign"
    merged["pass_inst"] = merged[f"{primary}_sum"] > params["INST_MIN_NET"]
    if params["INST_MIN_BUY_DAYS"] > 0:
        merged["pass_inst"] &= merged[f"{primary}_buy_days"] >= params["INST_MIN_BUY_DAYS"]
    if params["INST_MIN_PCT"] > 0:
        # 算不出股數的（無面額、外幣面額）分母為 NaN，比較結果為 False
        merged["pass_inst"] &= merged[f"{primary}_pct"] > params["INST_MIN_PCT"]
    return merged


# ------------------------------------------------------------ 顯示用（不篩選）
def margin_view(con, trade_date, stock_ids, window=5):
    """融資融券餘額與其變化。只顯示，不參與篩選。

    「融資減 = 籌碼乾淨」已被三年實證推翻，方向其實相反：融資變化五分位的
    20 日超額報酬單調遞增（跌最多 -0.17%、漲最多 +0.37%，t=2.54），
    而且「價漲+融資減」樣本內外方向翻轉。所以這裡只列數字，不做好壞判斷。
    """
    if not stock_ids:
        return pd.DataFrame()

    rows = con.execute(
        """
        WITH win AS (
            SELECT DISTINCT trade_date FROM daily_price
            WHERE trade_date <= ? ORDER BY trade_date DESC LIMIT ?
        )
        SELECT trade_date, stock_id, margin_balance, short_balance
        FROM margin_balance
        WHERE stock_id IN (SELECT unnest(?))
          AND trade_date IN (SELECT trade_date FROM win)
        ORDER BY stock_id, trade_date
        """,
        [trade_date, window, list(stock_ids)],
    ).df()

    if rows.empty:
        return pd.DataFrame()

    out = []
    for stock_id, group in rows.groupby("stock_id"):
        group = group.sort_values("trade_date")
        first, last = group.iloc[0], group.iloc[-1]
        chg = None
        if first["margin_balance"] and first["margin_balance"] > 0:
            chg = (last["margin_balance"] / first["margin_balance"] - 1) * 100
        out.append(
            {
                "stock_id": stock_id,
                "margin_balance": last["margin_balance"],
                "margin_chg_pct": chg,
                "short_balance": last["short_balance"],
            }
        )
    return pd.DataFrame(out)


def foreign_holding_view(con, trade_date, stock_ids, window=5):
    """外資持股比率與其變化（百分點）。目前只顯示。

    這是「存量」——籌碼現在在不在外資手上，跟 inst_flow 的買賣超（流量）互補。
    三年實證：持股比率上升組 20 日超額 +0.43%(t=4.89)、下降組 -0.36%(t=-4.46)。
    """
    if not stock_ids:
        return pd.DataFrame()

    rows = con.execute(
        """
        WITH win AS (
            SELECT DISTINCT trade_date FROM daily_price
            WHERE trade_date <= ? ORDER BY trade_date DESC LIMIT ?
        )
        SELECT trade_date, stock_id, foreign_pct
        FROM foreign_holding
        WHERE stock_id IN (SELECT unnest(?))
          AND trade_date IN (SELECT trade_date FROM win)
        ORDER BY stock_id, trade_date
        """,
        [trade_date, window, list(stock_ids)],
    ).df()

    if rows.empty:
        return pd.DataFrame()

    out = []
    for stock_id, group in rows.groupby("stock_id"):
        group = group.sort_values("trade_date").dropna(subset=["foreign_pct"])
        if group.empty:
            continue
        last = group["foreign_pct"].iloc[-1]
        first = group["foreign_pct"].iloc[0]
        out.append(
            {
                "stock_id": stock_id,
                "hold_pct": last,
                "hold_chg_pp": (last - first) if len(group) >= 2 else None,
            }
        )
    return pd.DataFrame(out)


CHIP_COLUMNS = ("whale_pct", "retail_pct",
                "whale_delta_1w", "retail_delta_1w",
                "whale_delta_4w", "retail_delta_4w")


def layer4_chips(con, trade_date, stock_ids):
    """大戶與散戶持股比，以及各自的 1 週／4 週變化。只顯示，不篩選。

    大戶 = 級距 15（1,000 張以上）；散戶 = 級距 1~8 加總（50 張以下）。
    級距單位是「股」不是「張」。

    join 一律用 avail_date：集保是週五資料、週六才公布，用 data_date 等於偷看未來。
    水位一週就算得出來；Δ1W 需要 2 週；Δ4W 需要 5 週。不夠的回 None。
    """
    if not stock_ids:
        return pd.DataFrame()

    rows = con.execute(
        """
        SELECT stock_id, avail_date,
               sum(CASE WHEN level = 15 THEN pct ELSE 0 END)            AS whale_pct,
               sum(CASE WHEN level BETWEEN 1 AND 8 THEN pct ELSE 0 END) AS retail_pct
        FROM tdcc_dist
        WHERE stock_id IN (SELECT unnest(?)) AND avail_date <= ?
        GROUP BY stock_id, avail_date
        ORDER BY stock_id, avail_date
        """,
        [list(stock_ids), trade_date],
    ).df()

    if rows.empty:
        return pd.DataFrame()

    out = []
    for stock_id, group in rows.groupby("stock_id"):
        group = group.sort_values("avail_date")
        latest = group.iloc[-1]

        def delta(back, column):
            if len(group) <= back:
                return None
            return latest[column] - group.iloc[-(back + 1)][column]

        out.append(
            {
                "stock_id": stock_id,
                "whale_pct": latest["whale_pct"],
                "retail_pct": latest["retail_pct"],
                "whale_delta_1w": delta(1, "whale_pct"),
                "retail_delta_1w": delta(1, "retail_pct"),
                "whale_delta_4w": delta(4, "whale_pct"),
                "retail_delta_4w": delta(4, "retail_pct"),
                "whale_weeks": len(group),
            }
        )
    return pd.DataFrame(out)


def _blank(frame, columns, fill=None):
    for col in columns:
        frame[col] = fill
    return frame


# ------------------------------------------------------------ 主流程
def run(con, params, target_date=None, with_extras=True):
    trade_date = resolve_trade_date(con, target_date)
    if trade_date is None:
        raise RuntimeError("資料庫沒有任何行情資料，請先跑 run_backfill.py")

    result = layer1_universe(
        con, trade_date,
        params["UNIVERSE_TOP_N"], params["UNIVERSE_LOOKBACK"], params["UNIVERSE_MIN_DAYS"],
    )
    if result.empty:
        log.warning("%s 沒有股票通過 Layer 1（持續在榜）", trade_date)
        return trade_date, result

    ids = result["stock_id"].tolist()

    tech = layer2_technical(con, trade_date, ids, params)
    tech_cols = ["stock_id", "close", "ma", "dif", "macd", "osc", "k", "d",
                 "vol_ratio", "bars", "pass_history", "pass_above_ma",
                 "pass_kd_rising", "pass_macd_rising",
                 "pass_trend", "pass_reversal", "mode", "pass_tech"]
    if tech.empty:
        result = _blank(result, tech_cols[1:])
        result["pass_tech"] = False
        result["pass_trend"] = False
        result["pass_reversal"] = False
        result["mode"] = ""
    else:
        result = result.merge(tech[tech_cols], on="stock_id", how="left")
        result["pass_tech"] = result["pass_tech"].fillna(False)

    flows = layer3_institutional(con, trade_date, ids, params["INST_WINDOW"])
    if flows.empty:
        result = _blank(result, ["foreign_sum", "foreign_buy_days", "trust_sum",
                                 "trust_buy_days", "foreign_pct", "trust_pct"], 0)
        result = _blank(result, ["foreign_pattern", "trust_pattern"],
                        "·" * params["INST_WINDOW"])
        result["pass_inst"] = False
    else:
        result = apply_layer3(result, flows, params)

    if with_extras:
        for view, columns in (
            (margin_view(con, trade_date, ids, params["INST_WINDOW"]),
             ["margin_balance", "margin_chg_pct", "short_balance"]),
            (foreign_holding_view(con, trade_date, ids, params["INST_WINDOW"]),
             ["hold_pct", "hold_chg_pp"]),
            (layer4_chips(con, trade_date, ids), list(CHIP_COLUMNS) + ["whale_weeks"]),
        ):
            if view.empty:
                result = _blank(result, columns)
            else:
                result = result.merge(view, on="stock_id", how="left")

    result["selected"] = combine_selection(result, params)
    primary = "trust" if params.get("INST_PRIMARY") == "trust" else "foreign"
    return trade_date, result.sort_values(
        ["selected", f"{primary}_sum"], ascending=[False, False]
    ).reset_index(drop=True)


def _mode_label(params):
    return {"trend": "順勢", "reversal": "打底反轉"}.get(
        params.get("SCREEN_MODE", "trend"), "順勢 + 打底反轉")


def funnel_stats(result, params):
    """每層各留下多少，看瓶頸在哪一層。"""
    if result.empty:
        return []
    total = len(result)
    return [
        (f"Layer 1 持續在榜（{params['UNIVERSE_LOOKBACK']}日中≥"
         f"{params['UNIVERSE_MIN_DAYS']}日進前{params['UNIVERSE_TOP_N']}）", total, total),
        (f"Layer 2 技術（{_mode_label(params)}）", int(result["pass_tech"].sum()), total),
        ("　　└ 順勢（KD/MACD 上升 + 站上均線）",
         int(result["pass_trend"].sum()) if "pass_trend" in result else 0, total),
        ("　　└ 反轉（DIF<0 回升 + 柱翻紅 + K>D）",
         int(result["pass_reversal"].sum()) if "pass_reversal" in result else 0, total),
        (f"Layer 3 法人（{params['INST_WINDOW']}日淨買超）",
         int(result["pass_inst"].sum()), total),
        ("最終入選（2 且 3）", int(result["selected"].sum()), total),
    ]
