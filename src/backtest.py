"""Phase 2 回測。

三個設計上的堅持，錯了回測就會系統性高估：

  1. 進場一律用「訊號日的下一個交易日開盤價」。用訊號日收盤價成交等於偷看未來 ——
     訊號本身就是收盤後才算得出來的。
  2. 報酬用還原價算。持有期間跨過除息日的話，未還原價會憑空吃掉一段報酬。
  3. 基準是「同一天、同一個母體、等權持有同樣天數」，不是加權指數。
     成交值前 100 名本身就偏追高，打贏大盤不代表訊號有選股能力。

為了效率，指標一次算完整段期間（而不是每天重跑選股），
所以這裡不呼叫 screener.run()，而是把同一組條件向量化重寫一次。
兩邊的條件必須一致，tests/test_consistency.py 會逐日比對兩者結果。
"""

import logging

import numpy as np
import pandas as pd

from . import indicators, screener

log = logging.getLogger(__name__)


def round_trip_cost(cfg):
    """來回總成本（比例）。手續費買賣各一次，證交稅只在賣出。"""
    return cfg["FEE_RATE"] * cfg["FEE_DISCOUNT"] * 2 + cfg["TAX_RATE"] + cfg["SLIPPAGE"]


# ------------------------------------------------------------------ 資料準備
def load_panel(con, params, start=None, end=None, warmup_days=140):
    """把回測需要的所有欄位組成一張寬表：價格 + 指標 + 法人 + 排名。"""
    # 母體要往前多涵蓋暖身期：Layer 1 的「近 N 日在榜幾天」是滾動計算，
    # 少了 start 之前那幾天，區間開頭算出來的在榜天數會偏低。
    warm_start = None
    if start is not None:
        row = con.execute(
            """
            SELECT min(trade_date) FROM (
                SELECT DISTINCT trade_date FROM daily_price
                WHERE trade_date < ? ORDER BY trade_date DESC LIMIT ?
            )
            """,
            [start, warmup_days],
        ).fetchone()
        warm_start = row[0] if row and row[0] else start

    universe = con.execute(
        """
        SELECT trade_date, stock_id, market, rank_ma5, turnover_ma5
        FROM v_turnover_rank
        WHERE rank_ma5 <= ?
          AND (? IS NULL OR trade_date >= ?)
          AND (? IS NULL OR trade_date <= ?)
        """,
        [params["UNIVERSE_TOP_N"], warm_start, warm_start, end, end],
    ).df()

    if universe.empty:
        return pd.DataFrame(), pd.DataFrame()

    ids = sorted(universe["stock_id"].unique())
    log.info("母體涵蓋 %d 檔個股、%d 個交易日", len(ids), universe["trade_date"].nunique())

    # 指標需要暖身，所以價格要往前多抓 warmup_days 個交易日
    first_date = pd.Timestamp(start) if start is not None else universe["trade_date"].min()
    prices = con.execute(
        """
        WITH warm AS (
            SELECT DISTINCT trade_date FROM daily_price
            WHERE trade_date < ? ORDER BY trade_date DESC LIMIT ?
        )
        SELECT a.trade_date, a.stock_id, a.market,
               a.open, a.high, a.low, a.close, a.volume, a.turnover
        FROM v_adjusted_price a
        WHERE a.stock_id IN (SELECT unnest(?))
          AND (a.trade_date >= ? OR a.trade_date IN (SELECT trade_date FROM warm))
          AND (? IS NULL OR a.trade_date <= ?)
        """,
        [first_date, warmup_days, ids, first_date, end, end],
    ).df()

    flows = con.execute(
        "SELECT trade_date, stock_id, foreign_net, trust_net FROM inst_flow "
        "WHERE stock_id IN (SELECT unnest(?))",
        [ids],
    ).df()

    master = con.execute(
        "SELECT stock_id, name, industry, shares_outstanding FROM security_master"
    ).df()

    panel = indicators.compute_all(prices)
    panel = panel.merge(flows, on=["trade_date", "stock_id"], how="left")
    # 法人沒有該股當日資料視為 0（沒買賣就是沒買賣），這樣 rolling 的視窗才跟交易日對齊
    panel[["foreign_net", "trust_net"]] = panel[["foreign_net", "trust_net"]].fillna(0.0)
    panel = panel.merge(master, on="stock_id", how="left")
    panel = panel.merge(
        universe[["trade_date", "stock_id", "rank_ma5", "turnover_ma5"]],
        on=["trade_date", "stock_id"], how="left",
    )
    panel["in_universe"] = panel["rank_ma5"].notna()
    panel["in_range"] = (
        True if start is None else panel["trade_date"] >= pd.Timestamp(start)
    )
    return panel.sort_values(["stock_id", "trade_date"]).reset_index(drop=True), universe


def add_signal_columns(panel, params):
    """把三層漏斗的條件向量化。欄位命名與 screener.py 保持一致，方便比對。

    Layer 1 母體  近 UNIVERSE_LOOKBACK 日中至少 UNIVERSE_MIN_DAYS 日進榜
    Layer 2 技術  KD 與 MACD 漸漸往上，且站上均線
    Layer 3 法人  近 INST_WINDOW 日淨買超
    """
    grouped = panel.groupby("stock_id", sort=False)

    # --- Layer 1：持續在榜。in_universe 已由 load_panel 逐日標好
    lookback = params["UNIVERSE_LOOKBACK"]
    panel["days_in_rank"] = grouped["in_universe"].transform(
        lambda s: s.astype("float64").rolling(lookback, min_periods=1).sum()
    )
    panel["pass_universe"] = panel["in_universe"] & (
        panel["days_in_rank"] >= params["UNIVERSE_MIN_DAYS"]
    )

    # --- Layer 2：技術。順勢與打底反轉兩種型態，條件與 screener.py 共用
    panel["pass_history"] = panel["bars"] >= indicators.MIN_HISTORY
    panel["pass_above_ma"] = (
        panel["above_ma"].fillna(False) if params["REQUIRE_ABOVE_MA"] else True
    )
    panel["pass_kd_rising"] = (
        panel["k_rising"].fillna(False) if params["REQUIRE_KD_RISING"] else True
    )
    panel["pass_macd_rising"] = (
        panel["osc_rising"].fillna(False) if params["REQUIRE_MACD_RISING"] else True
    )
    screener.apply_tech_modes(panel, params)

    # --- Layer 3：法人買賣超
    window = params["INST_WINDOW"]
    for who in ("trust", "foreign"):
        panel[f"{who}_sum"] = grouped[f"{who}_net"].transform(
            lambda s: s.rolling(window, min_periods=window).sum()
        )
        panel[f"{who}_buy_days"] = grouped[f"{who}_net"].transform(
            lambda s: (s > 0).astype("float64").rolling(window, min_periods=window).sum()
        )

    shares = panel["shares_outstanding"].where(panel["shares_outstanding"] > 0)
    panel["trust_pct"] = panel["trust_sum"] / shares
    panel["foreign_pct"] = panel["foreign_sum"] / shares

    primary = "trust" if params.get("INST_PRIMARY") == "trust" else "foreign"
    panel["pass_inst"] = panel[f"{primary}_sum"] > params["INST_MIN_NET"]
    if params["INST_MIN_BUY_DAYS"] > 0:
        panel["pass_inst"] &= panel[f"{primary}_buy_days"] >= params["INST_MIN_BUY_DAYS"]
    if params["INST_MIN_PCT"] > 0:
        panel["pass_inst"] &= panel[f"{primary}_pct"] > params["INST_MIN_PCT"]

    panel["selected"] = (
        panel.get("in_range", True)
        & panel["pass_universe"]
        & screener.combine_selection(panel, params)
    )
    return panel


# ------------------------------------------------------------------ 交易撮合
def add_trade_prices(panel, holds):
    """進場價 = 隔日開盤；出場價 = 再過 N 個交易日的開盤。

    全部用 shift 在「個股自己的交易日序列」上取，所以停牌造成的跳日會自動被吸收。
    """
    grouped = panel.groupby("stock_id", sort=False)
    panel["entry_price"] = grouped["open"].shift(-1)
    panel["entry_date"] = grouped["trade_date"].shift(-1)
    for hold in holds:
        panel[f"exit_price_{hold}"] = grouped["open"].shift(-(1 + hold))
        panel[f"exit_date_{hold}"] = grouped["trade_date"].shift(-(1 + hold))
    return panel


def build_trades(panel, holds, cost):
    """每一筆訊號展開成一列交易，含毛報酬與淨報酬。"""
    signals = panel[panel["selected"]].copy()
    rows = []
    for hold in holds:
        frame = signals[
            signals["entry_price"].notna() & signals[f"exit_price_{hold}"].notna()
        ].copy()
        if frame.empty:
            continue
        frame["hold"] = hold
        frame["gross"] = frame[f"exit_price_{hold}"] / frame["entry_price"] - 1
        frame["net"] = frame["gross"] - cost
        frame["exit_date"] = frame[f"exit_date_{hold}"]
        frame["exit_price"] = frame[f"exit_price_{hold}"]
        rows.append(
            frame[
                ["trade_date", "entry_date", "exit_date", "stock_id", "name", "market",
                 "industry", "hold", "rank_ma5", "days_in_rank", "trust_pct", "foreign_pct",
                 "entry_price", "exit_price", "gross", "net", "dif", "k", "ma"]
            ]
        )
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


def build_benchmark(panel, holds, cost):
    """同一天、同一個母體、等權持有同樣天數。

    這是關鍵的比較基準：成交值前 100 名本身就是當日爆量追價區，
    打贏加權指數不算數，要打贏「無腦買下整個母體」才代表訊號有選股能力。
    """
    universe = panel[panel["in_universe"]].copy()
    rows = []
    for hold in holds:
        frame = universe[
            universe["entry_price"].notna() & universe[f"exit_price_{hold}"].notna()
        ].copy()
        if frame.empty:
            continue
        frame["hold"] = hold
        frame["gross"] = frame[f"exit_price_{hold}"] / frame["entry_price"] - 1
        rows.append(frame[["trade_date", "stock_id", "hold", "gross"]])
    if not rows:
        return pd.DataFrame()

    bench = pd.concat(rows, ignore_index=True)
    daily = (
        bench.groupby(["trade_date", "hold"])["gross"]
        .agg(["mean", "median", "count"])
        .reset_index()
        .rename(columns={"mean": "bench_gross", "median": "bench_median", "count": "bench_n"})
    )
    daily["bench_net"] = daily["bench_gross"] - cost
    return daily


# ------------------------------------------------------------------ 統計
def trade_metrics(trades, bench_daily):
    """逐筆交易統計，並對齊同日基準以隔離「選股能力」與「進場時機」。"""
    out = []
    for hold, group in trades.groupby("hold"):
        merged = group.merge(
            bench_daily[bench_daily["hold"] == hold][["trade_date", "bench_gross"]],
            on="trade_date", how="left",
        )
        excess = (merged["gross"] - merged["bench_gross"]).dropna()
        # 只報平均超額很容易把雜訊當成效應，所以一併算標準誤與 95% 信賴區間。
        # 樣本數小的時候，±1% 的平均超額可能完全不代表任何東西。
        n = len(excess)
        se = float(excess.std() / np.sqrt(n)) if n > 1 else float("nan")
        mean_excess = float(excess.mean()) if n else float("nan")
        out.append(
            {
                "hold": hold,
                "trades": len(group),
                "excess_se": se,
                "excess_t": mean_excess / se if se and se == se and se > 0 else float("nan"),
                "excess_lo": mean_excess - 1.96 * se,
                "excess_hi": mean_excess + 1.96 * se,
                "win_rate": float((group["net"] > 0).mean()),
                "gross_mean": float(group["gross"].mean()),
                "gross_median": float(group["gross"].median()),
                "net_mean": float(group["net"].mean()),
                "net_median": float(group["net"].median()),
                "bench_mean": float(merged["bench_gross"].mean()),
                "excess_mean": float(excess.mean()),
                "excess_win_rate": float((excess > 0).mean()),
                "best": float(group["net"].max()),
                "worst": float(group["net"].min()),
                "std": float(group["net"].std()),
            }
        )
    return pd.DataFrame(out).sort_values("hold").reset_index(drop=True)


def equity_curve(panel, trades, hold, cost):
    """日頻權益曲線：每天對「當下所有在倉部位」等權，取當日報酬平均。

    這是重疊訊號的標準處理方式 —— 同一天可能有多筆訊號，也可能一筆都沒有。
    成本在進場日與出場日各扣一半。
    """
    subset = trades[trades["hold"] == hold]
    if subset.empty:
        return pd.Series(dtype="float64"), 0.0

    closes = panel.pivot_table(index="trade_date", columns="stock_id", values="close")
    opens = panel.pivot_table(index="trade_date", columns="stock_id", values="open")
    dates = closes.index

    daily = {d: [] for d in dates}
    half = cost / 2

    for row in subset.itertuples():
        try:
            i0 = dates.get_loc(row.entry_date)
            i1 = dates.get_loc(row.exit_date)
        except KeyError:
            continue
        sid = row.stock_id
        # 進場日：以開盤買進，當日報酬是開盤到收盤
        first = closes.iloc[i0][sid] / opens.iloc[i0][sid] - 1 - half
        daily[dates[i0]].append(first)
        # 中間各日：收盤到收盤
        for i in range(i0 + 1, i1):
            prev, cur = closes.iloc[i - 1][sid], closes.iloc[i][sid]
            if pd.notna(prev) and pd.notna(cur) and prev > 0:
                daily[dates[i]].append(cur / prev - 1)
        # 出場日：以開盤賣出
        prev = closes.iloc[i1 - 1][sid]
        if pd.notna(prev) and prev > 0:
            daily[dates[i1]].append(opens.iloc[i1][sid] / prev - 1 - half)

    series = pd.Series(
        {d: (np.nanmean(v) if v else 0.0) for d, v in daily.items()}
    ).sort_index()
    equity = (1 + series).cumprod()
    drawdown = equity / equity.cummax() - 1
    return equity, float(drawdown.min())


def run(con, params, cfg, start=None, end=None):
    holds = tuple(cfg["HOLD_DAYS"])
    cost = round_trip_cost(cfg)

    panel, _ = load_panel(con, params, start, end)
    if panel.empty:
        raise RuntimeError("沒有母體資料，請確認資料庫區間")

    panel = add_signal_columns(panel, params)
    panel = add_trade_prices(panel, holds)

    trades = build_trades(panel, holds, cost)
    bench = build_benchmark(panel, holds, cost)
    if trades.empty:
        return {"panel": panel, "trades": trades, "bench": bench,
                "metrics": pd.DataFrame(), "cost": cost, "equity": {}}

    metrics = trade_metrics(trades, bench)
    equity = {}
    for hold in holds:
        curve, mdd = equity_curve(panel, trades, hold, cost)
        equity[hold] = (curve, mdd)

    return {"panel": panel, "trades": trades, "bench": bench,
            "metrics": metrics, "cost": cost, "equity": equity}
