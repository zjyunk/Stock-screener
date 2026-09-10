"""驗證支撐壓力有沒有預測力。

支撐壓力的行為學解釋（很多人在這個價位買過 → 跌回來有買盤、漲上去有賣壓）
聽起來合理，但從來沒被本專案的資料檢定過。這支程式測三件事：

  1. 現價貼近支撐（支撐上方 2% 內）→ 後續報酬是否較好？
  2. 現價貼近壓力（壓力下方 2% 內）→ 後續報酬是否較差？
  3. 剛突破壓力（昨天在壓力下、今天收在壓力上）→ 後續報酬如何？

關鍵：每個日期的支撐壓力只用「那天以前」的資料算，不偷看未來。
這比一般回測貴很多（每個股票日都要重算一次成交量分佈），所以隔幾天抽樣一次。

    python research_levels.py
    python research_levels.py --step 2 --hold 10
"""

import argparse
import sys
import warnings

import numpy as np
import pandas as pd

import config
from src import backtest, db, levels

warnings.filterwarnings("ignore")

PARAM_KEYS = (
    "SCREEN_MODE", "REVERSAL_REQUIRE_INST", "UNIVERSE_TOP_N", "UNIVERSE_LOOKBACK",
    "UNIVERSE_MIN_DAYS", "TREND_LOOKBACK", "MA_PERIOD", "REQUIRE_ABOVE_MA",
    "REQUIRE_KD_RISING", "REQUIRE_MACD_RISING", "INST_PRIMARY", "INST_WINDOW",
    "INST_MIN_NET", "INST_MIN_BUY_DAYS", "INST_MIN_PCT",
)
NEAR_PCT = 0.02
SPLIT = pd.Timestamp("2025-03-01")


def stats(frame, mask):
    d = frame[mask]
    if len(d) < 100:
        return None
    ex = (d["ret"] - d["bench"]).dropna()
    n = len(ex)
    m = ex.mean()
    se = ex.std() / np.sqrt(n)
    return n, m, (m / se if se else float("nan"))


def fmt(r):
    if r is None:
        return f"{'樣本不足':>24}"
    n, m, t = r
    return f"{n:>6,} {m * 100:>+7.2f}% {t:>+6.2f}{' *' if abs(t) > 1.96 else '  '}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hold", type=int, default=20)
    ap.add_argument("--step", type=int, default=3, help="每幾個交易日抽樣一次")
    args = ap.parse_args()

    params = {k: getattr(config, k) for k in PARAM_KEYS}
    con = db.connect(config.DB_PATH)
    db.create_views(con)

    panel, _ = backtest.load_panel(con, params)
    panel = backtest.add_trade_prices(panel, (args.hold,))
    panel = panel.sort_values(["stock_id", "trade_date"]).reset_index(drop=True)
    con.close()

    uni_mask = panel["in_universe"] & panel["entry_price"].notna() & panel[f"exit_price_{args.hold}"].notna()
    panel["ret"] = panel[f"exit_price_{args.hold}"] / panel["entry_price"] - 1
    panel["bench"] = panel.loc[uni_mask].groupby("trade_date")["ret"].transform("mean")

    print(f"逐日重算支撐壓力（每 {args.step} 日抽樣）…", flush=True)
    records = []
    lookback = levels.LOOKBACK_DAYS
    for stock_id, g in panel.groupby("stock_id", sort=False):
        g = g.reset_index(drop=True)
        rows = g[["high", "low", "close", "volume"]].to_dict("records")
        for i in range(lookback, len(g), args.step):
            if not g.at[i, "in_universe"] or pd.isna(g.at[i, "ret"]) or pd.isna(g.at[i, "bench"]):
                continue
            window = rows[i - lookback + 1: i + 1]
            lv = levels.find_levels(window)
            close = g.at[i, "close"]
            prev_close = g.at[i - 1, "close"]

            sup = [c for c in lv["support"]]
            res = [c for c in lv["resistance"]]
            near_sup = any(0 < (close - c["price"]) / c["price"] <= NEAR_PCT for c in sup)
            near_res = any(0 < (c["price"] - close) / close <= NEAR_PCT for c in res)
            strong_sup = any(0 < (close - c["price"]) / c["price"] <= NEAR_PCT and c["strength"] >= 0.7 for c in sup)
            strong_res = any(0 < (c["price"] - close) / close <= NEAR_PCT and c["strength"] >= 0.7 for c in res)

            # 突破：昨天收盤在某個壓力之下、今天收在之上（壓力用昨天算的比較嚴謹，這裡近似用今天的清單）
            prev_lv = levels.find_levels(rows[i - lookback: i]) if i > lookback else lv
            broke = any(prev_close <= c["price"] < close for c in prev_lv["resistance"])
            lost = any(prev_close >= c["price"] > close for c in prev_lv["support"])

            records.append(
                {
                    "trade_date": g.at[i, "trade_date"], "stock_id": stock_id,
                    "ret": g.at[i, "ret"], "bench": g.at[i, "bench"],
                    "near_sup": near_sup, "near_res": near_res,
                    "strong_sup": strong_sup, "strong_res": strong_res,
                    "broke_res": broke, "lost_sup": lost,
                    "n_sup": len(sup), "n_res": len(res),
                }
            )

    df = pd.DataFrame(records)
    print(f"樣本 {len(df):,} 筆（{df['trade_date'].min().date()} ~ {df['trade_date'].max().date()}）")
    print(f"持有 {args.hold} 日 · 超額 = 對同日母體等權 · * 為 |t|>1.96\n")

    none = ~df["near_sup"] & ~df["near_res"]
    print(f"{'情境':<30}{'n / 超額 / t值':>24}")
    print("-" * 56)
    for label, m in [
        ("全部（基準）", df["ret"].notna()),
        ("不貼近任何價位", none),
        ("貼近支撐（上方 2% 內）", df["near_sup"]),
        ("　└ 強支撐（強度≥0.7）", df["strong_sup"]),
        ("貼近壓力（下方 2% 內）", df["near_res"]),
        ("　└ 強壓力（強度≥0.7）", df["strong_res"]),
        ("剛突破壓力", df["broke_res"]),
        ("剛跌破支撐", df["lost_sup"]),
        ("上方沒有壓力（創高區）", df["n_res"] == 0),
    ]:
        print(f"{label:<30}{fmt(stats(df, m))}")

    print("\n--- 樣本外驗證 ---")
    ins, oos = df["trade_date"] < SPLIT, df["trade_date"] >= SPLIT
    print(f"{'情境':<30}{'樣本內':>24}{'樣本外':>24}  一致")
    print("-" * 82)
    for label, m in [
        ("貼近支撐", df["near_sup"]),
        ("貼近壓力", df["near_res"]),
        ("剛突破壓力", df["broke_res"]),
        ("上方沒有壓力", df["n_res"] == 0),
    ]:
        a, b = stats(df, m & ins), stats(df, m & oos)
        same = "—"
        if a and b:
            same = "是" if (a[1] > 0) == (b[1] > 0) else "否 ← 翻轉"
        print(f"{label:<30}{fmt(a)}{fmt(b)}  {same}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
