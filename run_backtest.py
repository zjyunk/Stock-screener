"""Phase 2 回測 —— 決策關卡。

    python run_backtest.py
    python run_backtest.py --start 2024-01-01 --end 2026-08-31
    python run_backtest.py --min-pct 0.05 --top-n 150     # 調參數重跑
    python run_backtest.py --csv trades.csv

判讀方式：關鍵不是「賺不賺錢」，是 excess（超額報酬）。
成交值前 100 名本身就是當日爆量追價區，打贏加權指數不算數；
要打贏「同一天無腦買下整個母體等權持有」，才代表這組訊號真的有選股能力。

若 excess 長期為負，回頭調 config.py 的參數，不要往下做 UI。
"""

import argparse
import logging
import sys

import config
from src import backtest, db

PARAM_KEYS = (
    "SCREEN_MODE", "REVERSAL_REQUIRE_INST", "UNIVERSE_TOP_N", "UNIVERSE_LOOKBACK", "UNIVERSE_MIN_DAYS",
    "TREND_LOOKBACK", "MA_PERIOD", "REQUIRE_ABOVE_MA",
    "REQUIRE_KD_RISING", "REQUIRE_MACD_RISING",
    "INST_PRIMARY", "INST_WINDOW", "INST_MIN_NET",
    "INST_MIN_BUY_DAYS", "INST_MIN_PCT",
)
COST_KEYS = ("HOLD_DAYS", "FEE_RATE", "FEE_DISCOUNT", "TAX_RATE", "SLIPPAGE")


def pct(value, digits=2):
    return f"{value * 100:+.{digits}f}%"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--top-n", type=int)
    ap.add_argument("--min-days", type=int, help="覆寫「窗口內至少幾天在榜」")
    ap.add_argument("--mode", choices=["trend", "reversal", "both"],
                    help="選股模式")
    ap.add_argument("--csv", help="逐筆交易輸出路徑")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(levelname)-7s %(message)s",
    )

    if not config.DB_PATH.exists():
        print("找不到資料庫，請先跑：python run_backfill.py")
        return 1

    params = {key: getattr(config, key) for key in PARAM_KEYS}
    cfg = {key: getattr(config, key) for key in COST_KEYS}
    if args.top_n:
        params["UNIVERSE_TOP_N"] = args.top_n
    if args.min_days:
        params["UNIVERSE_MIN_DAYS"] = args.min_days
    if args.mode:
        params["SCREEN_MODE"] = args.mode

    con = db.connect(config.DB_PATH)
    db.create_views(con)

    result = backtest.run(con, params, cfg, start=args.start, end=args.end)
    trades, metrics, cost = result["trades"], result["metrics"], result["cost"]

    span = con.execute(
        "SELECT min(trade_date), max(trade_date), count(DISTINCT trade_date) FROM daily_price"
        + ("" if not args.start else " WHERE trade_date >= '%s'" % args.start)
    ).fetchone()

    print(f"\n=== 回測結果 ===")
    print(f"資料區間 {span[0]} ~ {span[1]}，{span[2]} 個交易日")
    bits = []
    if params["REQUIRE_KD_RISING"]:
        bits.append("KD上升")
    if params["REQUIRE_MACD_RISING"]:
        bits.append("MACD柱放大")
    if params["REQUIRE_ABOVE_MA"]:
        bits.append(f"站上{params['MA_PERIOD']}MA")
    print(
        f"母體：近 {params['UNIVERSE_LOOKBACK']} 日中至少 {params['UNIVERSE_MIN_DAYS']} 日"
        f"進前 {params['UNIVERSE_TOP_N']} 名"
    )
    mode_label = {"trend": "順勢", "reversal": "打底反轉"}.get(
        params.get("SCREEN_MODE", "trend"), "順勢 + 打底反轉")
    print(f"技術：{mode_label}（順勢 = {' · '.join(bits)}）")
    print(
        f"法人：{'投信' if params['INST_PRIMARY'] == 'trust' else '外資'} "
        f"近 {params['INST_WINDOW']} 日淨買超 > {params['INST_MIN_NET']:,} 股"
    )
    print(
        f"成本：手續費 {config.FEE_RATE*100:.4f}%×{config.FEE_DISCOUNT}×2 + "
        f"證交稅 {config.TAX_RATE*100:.2f}% + 滑價 {config.SLIPPAGE*100:.2f}% "
        f"= 來回 {cost*100:.2f}%"
    )

    if trades.empty:
        print("\n區間內沒有任何訊號。先用 run_screener.py --all 看漏斗卡在哪一層。")
        con.close()
        return 0

    years = max((span[2] or 1) / 245.0, 1e-9)
    n_signals = len(trades[trades["hold"] == cfg["HOLD_DAYS"][0]])
    print(f"\n訊號數 {n_signals} 次，年化約 {n_signals / years:.0f} 次")

    header = (
        f"\n{'持有':>4} {'筆數':>6} {'勝率':>7} {'毛報酬':>8} {'淨報酬':>8} "
        f"{'基準':>8} {'超額':>8} {'95%信賴區間':>20} {'t值':>7} {'最大回撤':>9}"
    )
    print(header)
    print("-" * 102)
    for row in metrics.itertuples():
        _, mdd = result["equity"].get(row.hold, (None, float("nan")))
        ci = f"[{pct(row.excess_lo)}, {pct(row.excess_hi)}]"
        print(
            f"{row.hold:>4} {row.trades:>6} {row.win_rate*100:>6.1f}% "
            f"{pct(row.gross_mean):>8} {pct(row.net_mean):>8} "
            f"{pct(row.bench_mean):>8} {pct(row.excess_mean):>8} {ci:>20} "
            f"{row.excess_t:>+7.2f} {pct(mdd):>9}"
        )

    print("\n判讀（超額報酬才是重點，淨報酬為正只代表市場在漲）：")
    for row in metrics.itertuples():
        if abs(row.excess_t) < 1.96:
            verdict = "與基準無顯著差異 —— 看不出選股能力"
        elif row.excess_mean > 0:
            verdict = "顯著優於基準"
        else:
            verdict = "顯著劣於基準"
        print(
            f"  持有 {row.hold:>2} 日 — 超額 {pct(row.excess_mean)}（t={row.excess_t:+.2f}），{verdict}"
        )

    if (metrics["excess_t"].abs() < 1.96).all() or (metrics["excess_mean"] <= 0).all():
        print(
            "\n  ⚠ 沒有任何持有期顯示出正的選股能力。依規格，這一關沒過就不該往下做 UI，\n"
            "    應回頭調整 config.py 的條件，或用 run_screener.py --all 看漏斗形狀。"
        )

    best = metrics.loc[metrics["excess_mean"].idxmax()]
    print(
        f"\n超額報酬最佳為持有 {int(best['hold'])} 日：{pct(best['excess_mean'])}，"
        f"淨報酬 {pct(best['net_mean'])}，勝率 {best['win_rate']*100:.1f}%"
    )

    # 分年看穩定性 —— 只在某一年有效的訊號通常是過度配適
    print("\n分年超額報酬（持有 %d 日）：" % int(best["hold"]))
    sub = trades[trades["hold"] == int(best["hold"])].merge(
        result["bench"][result["bench"]["hold"] == int(best["hold"])][["trade_date", "bench_gross"]],
        on="trade_date", how="left",
    )
    sub["year"] = sub["trade_date"].dt.year
    sub["excess"] = sub["gross"] - sub["bench_gross"]
    for year, group in sub.groupby("year"):
        print(
            f"  {year}  {len(group):>4} 筆  超額 {pct(group['excess'].mean())}  "
            f"淨報酬 {pct(group['net'].mean())}  勝率 {(group['net'] > 0).mean()*100:.1f}%"
        )

    if args.csv:
        trades.to_csv(args.csv, index=False, encoding="utf-8-sig")
        print(f"\n已輸出 {len(trades)} 筆交易到 {args.csv}")

    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
