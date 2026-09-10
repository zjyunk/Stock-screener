"""驗證台股的籌碼判讀：「股價漲 + 融資減 = 乾淨的上漲」。

這句話的完整版是：股價在漲、但融資餘額在減，代表買方是法人而不是散戶，
散戶沒有用槓桿追進來，所以籌碼乾淨、後續比較有續航力。
反過來「股價漲 + 融資增」代表散戶在追高，籌碼變髒。

檢定方式：
  1. 2×2 象限 —— 價漲/價跌 × 融資減/融資增，比較後續超額報酬
  2. 各成分單獨的效果（融資變化、外資持股變化）
  3. 樣本外驗證（前後各半，方向要一致才算數）
  4. 市值控制（每天依市值切五組，只跟同組比）

只用每日資料（融資、外資持股），不含集保週頻資料。

    python research_chips.py
    python research_chips.py --hold 10 --window 5
"""

import argparse
import logging
import sys
import warnings

import numpy as np
import pandas as pd

import config
from src import backtest, db

warnings.filterwarnings("ignore")
log = logging.getLogger("research")

PARAM_KEYS = (
    "SCREEN_MODE", "REVERSAL_REQUIRE_INST", "UNIVERSE_TOP_N", "UNIVERSE_LOOKBACK", "UNIVERSE_MIN_DAYS",
    "TREND_LOOKBACK", "MA_PERIOD", "REQUIRE_ABOVE_MA",
    "REQUIRE_KD_RISING", "REQUIRE_MACD_RISING",
    "INST_PRIMARY", "INST_WINDOW", "INST_MIN_NET",
    "INST_MIN_BUY_DAYS", "INST_MIN_PCT",
)
SPLIT = pd.Timestamp("2025-03-01")


def stats(frame, mask, bench_col="bench_all"):
    """回傳 (n, 平均超額, t值)。樣本太少回 None。"""
    subset = frame[mask]
    if len(subset) < 100:
        return None
    excess = (subset["fwd_ret"] - subset[bench_col]).dropna()
    n = len(excess)
    if n < 100:
        return None
    mean = excess.mean()
    se = excess.std() / np.sqrt(n)
    return n, mean, (mean / se if se else float("nan"))


def fmt(result):
    if result is None:
        return f"{'樣本不足':>26}"
    n, mean, t = result
    star = " *" if abs(t) > 1.96 else "  "
    return f"{n:>7,} {mean * 100:>+8.2f}% {t:>+7.2f}{star}"


def build(con, params, hold, window):
    panel, _ = backtest.load_panel(con, params)
    panel = backtest.add_signal_columns(panel, params)
    panel = backtest.add_trade_prices(panel, (hold,))

    ids = sorted(panel["stock_id"].unique())

    margin = con.execute(
        "SELECT trade_date, stock_id, margin_balance FROM margin_balance "
        "WHERE stock_id IN (SELECT unnest(?))", [ids]
    ).df()
    foreign = con.execute(
        "SELECT trade_date, stock_id, foreign_pct AS foreign_hold_pct FROM foreign_holding "
        "WHERE stock_id IN (SELECT unnest(?))", [ids]
    ).df()
    raw = con.execute(
        "SELECT trade_date, stock_id, close AS raw_close FROM daily_price "
        "WHERE stock_id IN (SELECT unnest(?))", [ids]
    ).df()

    panel = panel.merge(margin, on=["trade_date", "stock_id"], how="left")
    panel = panel.merge(foreign, on=["trade_date", "stock_id"], how="left")
    panel = panel.merge(raw, on=["trade_date", "stock_id"], how="left")
    panel = panel.sort_values(["stock_id", "trade_date"])

    g = panel.groupby("stock_id", sort=False)
    # 融資用變化「率」，外資持股用變化「百分點」（它本身已經是比率）
    panel["margin_chg"] = g["margin_balance"].transform(
        lambda s: s / s.shift(window) - 1
    ) * 100
    panel["foreign_chg"] = g["foreign_hold_pct"].transform(lambda s: s - s.shift(window))
    panel["price_chg"] = g["close"].transform(lambda s: s / s.shift(window) - 1) * 100
    panel["mktcap"] = panel["raw_close"] * panel["shares_outstanding"]

    uni = panel[
        panel["in_universe"]
        & panel["entry_price"].notna()
        & panel[f"exit_price_{hold}"].notna()
    ].copy()
    uni["fwd_ret"] = uni[f"exit_price_{hold}"] / uni["entry_price"] - 1

    uni["bench_all"] = uni.groupby("trade_date")["fwd_ret"].transform("mean")
    uni["size_q"] = uni.groupby("trade_date")["mktcap"].transform(
        lambda s: pd.qcut(s.rank(method="first"), 5, labels=False)
        if s.notna().sum() >= 5 else np.nan
    )
    uni["bench_size"] = uni.groupby(["trade_date", "size_q"])["fwd_ret"].transform("mean")
    return uni


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hold", type=int, default=20, help="持有天數，預設 20")
    ap.add_argument("--window", type=int, default=5, help="變化觀察窗口，預設 5 日")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if not config.DB_PATH.exists():
        print("找不到資料庫")
        return 1

    params = {k: getattr(config, k) for k in PARAM_KEYS}
    con = db.connect(config.DB_PATH)
    db.create_views(con)

    have = con.execute("SELECT count(*) FROM margin_balance").fetchone()[0]
    if have < 100000:
        print(f"融資資料只有 {have:,} 列，回補可能還沒跑完。先執行：")
        print("  python run_backfill.py --load-only")
        con.close()
        return 1

    uni = build(con, params, args.hold, args.window)
    have_both = uni["margin_chg"].notna() & uni["foreign_chg"].notna()
    uni = uni[have_both]

    print(f"\n=== 籌碼判讀驗證 · 持有 {args.hold} 日 · 變化窗口 {args.window} 日 ===")
    print(f"母體前 {params['UNIVERSE_TOP_N']} 名，可用樣本 {len(uni):,} 筆"
          f"（{uni['trade_date'].min().date()} ~ {uni['trade_date'].max().date()}）")
    print("超額報酬 = 該組平均報酬 − 同日母體等權報酬；* 代表 |t| > 1.96")

    up = uni["price_chg"] > 0
    mdown = uni["margin_chg"] < 0

    print(f"\n--- 2×2 象限（{args.window} 日）---")
    print(f"{'':<26}{'n / 超額 / t值':>26}")
    print("-" * 54)
    for label, mask in [
        ("價漲 + 融資減（乾淨上漲）", up & mdown),
        ("價漲 + 融資增（散戶追高）", up & ~mdown),
        ("價跌 + 融資減", ~up & mdown),
        ("價跌 + 融資增（套牢）", ~up & ~mdown),
    ]:
        print(f"{label:<26}{fmt(stats(uni, mask))}")

    print("\n--- 各成分單獨 ---")
    for label, mask in [
        ("融資減少", mdown),
        ("融資增加", ~mdown),
        ("外資持股增加", uni["foreign_chg"] > 0),
        ("外資持股減少", uni["foreign_chg"] < 0),
        ("融資減 + 外資增", mdown & (uni["foreign_chg"] > 0)),
        ("價漲 + 融資減 + 外資增", up & mdown & (uni["foreign_chg"] > 0)),
    ]:
        print(f"{label:<26}{fmt(stats(uni, mask))}")

    print("\n--- 融資變化五分位（第1組跌最多）---")
    uni["m_q"] = pd.qcut(uni["margin_chg"].rank(method="first"), 5, labels=False)
    for q in range(5):
        mask = uni["m_q"] == q
        median = uni.loc[mask, "margin_chg"].median()
        print(f"{'第' + str(q + 1) + '組 (中位 ' + f'{median:+.1f}%' + ')':<26}"
              f"{fmt(stats(uni, mask))}")

    print("\n--- 樣本外驗證（前後各半，方向一致才算數）---")
    ins, oos = uni["trade_date"] < SPLIT, uni["trade_date"] >= SPLIT
    print(f"{'':<26}{'樣本內':>26}{'樣本外':>26}  一致")
    print("-" * 82)
    for label, mask in [
        ("價漲 + 融資減", up & mdown),
        ("價漲 + 融資增", up & ~mdown),
        ("融資減少", mdown),
        ("融資減 + 外資增", mdown & (uni["foreign_chg"] > 0)),
    ]:
        a, b = stats(uni, mask & ins), stats(uni, mask & oos)
        same = "—"
        if a and b:
            same = "是" if (a[1] > 0) == (b[1] > 0) else "否 ← 翻轉"
        print(f"{label:<26}{fmt(a)}{fmt(b)}  {same}")

    print("\n--- 市值控制（每天依市值切五組，只跟同組比）---")
    print(f"{'':<26}{'對全母體':>26}{'對同市值組':>26}")
    print("-" * 78)
    for label, mask in [
        ("價漲 + 融資減", up & mdown),
        ("融資減少", mdown),
        ("融資減 + 外資增", mdown & (uni["foreign_chg"] > 0)),
    ]:
        print(f"{label:<26}{fmt(stats(uni, mask))}"
              f"{fmt(stats(uni, mask, 'bench_size'))}")

    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
