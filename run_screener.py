"""選股 CLI。

三層漏斗：
  1. 母體  近 N 日「持續」在成交值排行榜內
  2. 技術  KD 與 MACD 漸漸往上，且站上均線
  3. 法人  近一週淨買超

    python run_screener.py                     # 最新交易日
    python run_screener.py --date 2026-08-28   # 指定日
    python run_screener.py --top-n 50          # 排行榜收緊到前 50
    python run_screener.py --min-days 3        # 5 日中 3 日在榜即可
    python run_screener.py --all               # 連未入選的一起列，看卡在哪一層
    python run_screener.py --csv out.csv

融資融券只顯示不篩選 —— 三年實證推翻了「融資減 = 籌碼乾淨」的判讀，
方向其實相反，見 config.py 的註解。
"""

import argparse
import sys

import config
from src import db, levels, report, screener

PARAM_KEYS = (
    "SCREEN_MODE", "REVERSAL_REQUIRE_INST", "UNIVERSE_TOP_N", "UNIVERSE_LOOKBACK", "UNIVERSE_MIN_DAYS",
    "TREND_LOOKBACK", "MA_PERIOD", "REQUIRE_ABOVE_MA",
    "REQUIRE_KD_RISING", "REQUIRE_MACD_RISING",
    "INST_PRIMARY", "INST_WINDOW", "INST_MIN_NET",
    "INST_MIN_BUY_DAYS", "INST_MIN_PCT",
)


def build_params(args):
    params = {key: getattr(config, key) for key in PARAM_KEYS}
    if args.top_n:
        params["UNIVERSE_TOP_N"] = args.top_n
    if args.min_days:
        params["UNIVERSE_MIN_DAYS"] = args.min_days
    if args.window:
        params["UNIVERSE_LOOKBACK"] = args.window
        params["INST_WINDOW"] = args.window
    if args.mode:
        params["SCREEN_MODE"] = args.mode
    return params


def num(value, digits=2, dash="-"):
    if value is None:
        return dash
    try:
        if value != value:          # NaN
            return dash
    except TypeError:
        return dash
    return f"{value:,.{digits}f}"


def print_conditions(rows, params):
    """三層條件的實際數值。"""
    who = "trust" if params["INST_PRIMARY"] == "trust" else "foreign"
    label = "投信" if who == "trust" else "外資"
    print(
        f"\n{'排名':>4} {'在榜':>5} {'代號':<6} {'名稱':<10} {'市場':<5} {'收盤':>9} "
        f"{'MA' + str(params['MA_PERIOD']):>9} {'五日均額(億)':>12} "
        f"{label + '(張)':>10} {'買賣型態':<8} {'K':>6} {'D':>6} {'OSC':>8}"
    )
    print("-" * 132)
    for _, r in rows.iterrows():
        mark = "*" if r["selected"] else " "
        print(
            f"{mark}{int(r['rank_ma5']):>3} "
            f"{int(r['days_in_rank']):>2}/{params['UNIVERSE_LOOKBACK']:<2} "
            f"{str(r.get('mode') or '-'):<5} "
            f"{r['stock_id']:<6} {str(r.get('name') or '')[:10]:<10} {r['market']:<5} "
            f"{num(r.get('close')):>9} {num(r.get('ma')):>9} "
            f"{num((r.get('turnover_ma5') or 0) / 1e8, 1):>12} "
            f"{num((r.get(who + '_sum') or 0) / 1000, 0):>10} "
            f"{str(r.get(who + '_pattern') or ''):<8} "
            f"{num(r.get('k'), 1):>6} {num(r.get('d'), 1):>6} {num(r.get('osc'), 2):>8}"
        )
    print("  型態：順勢 = KD/MACD 上升且站上均線；反轉 = DIF<0 回升且柱翻紅且 K>D")
    print("  買賣型態：由舊到新，+ 買超、- 賣超、· 無")


def print_reference(rows):
    """參考資訊，不參與篩選。"""
    print(
        f"\n{'代號':<6} {'名稱':<10} {'外資持股%':>10} {'5日Δpp':>8} "
        f"{'融資(張)':>10} {'融資5日%':>9} {'融券(張)':>9} {'大戶%':>7} {'散戶%':>7}"
    )
    print("-" * 86)
    for _, r in rows.iterrows():
        print(
            f"{r['stock_id']:<6} {str(r.get('name') or '')[:10]:<10} "
            f"{num(r.get('hold_pct')):>10} {num(r.get('hold_chg_pp')):>8} "
            f"{num(r.get('margin_balance'), 0):>10} {num(r.get('margin_chg_pct'), 1):>9} "
            f"{num(r.get('short_balance'), 0):>9} "
            f"{num(r.get('whale_pct')):>7} {num(r.get('retail_pct')):>7}"
        )
    print("  外資持股 = 存量（籌碼在不在外資手上），跟買賣超的流量互補")
    print("  融資只列數字不做判斷：三年實證顯示融資與後續報酬呈正相關，與慣用判讀相反")


def print_levels(con, trade_date, rows):
    """入選股的支撐壓力（近 120 日成交量密集區 + 前高前低）。"""
    if rows.empty:
        return
    print("\n支撐壓力（還原價；量 = 成交量密集區、高/低 = 前高前低）：")
    print("  實證：貼近壓力後續較差(-0.52%)、跌破支撐更差(-0.74%)；「貼近支撐會反彈」不成立")
    for _, r in rows.iterrows():
        series = report.stock_series(con, r["stock_id"], trade_date)
        lv = levels.find_levels(series)
        digits = 2 if (lv.get("current") or 0) < 500 else 0
        print(f"  {r['stock_id']:<6} {str(r.get('name') or '')[:8]:<8} "
              f"現價 {num(lv.get('current'), digits):>9}  {levels.describe(lv, digits)}")
        ts = levels.trend_line(series, "support")
        tr = levels.trend_line(series, "resistance")
        if ts or tr:
            print(f"  {'':<6} {'':<8} {'趨勢線':>14}  "
                  f"{levels.describe_trend(ts)} ｜ {levels.describe_trend(tr)}")
        prev = series[-2]["close"] if len(series) >= 2 else None
        for w in levels.warnings_for(lv, prev):
            print(f"  {'':<6} {'':<8} {'⚠':>14}  {w}")


def print_reasons(rows):
    """未入選卡在哪一層 —— 調參數時最有用的一張表。"""
    checks = [
        ("pass_history", "資料不足"),
        ("pass_trend", "順勢型態不成立"),
        ("pass_above_ma", "　└ 未站上均線"),
        ("pass_kd_rising", "　└ KD 未往上"),
        ("pass_macd_rising", "　└ MACD 未往上"),
        ("pass_reversal", "反轉型態不成立"),
        ("pass_inst", "法人未淨買（只影響順勢）"),
    ]
    print("\n未入選原因（Layer 1 通過者中）：")
    for col, label in checks:
        if col not in rows.columns:
            continue
        failed = int((~rows[col].fillna(False)).sum())
        print(f"  {label:<14} 未通過 {failed:>4} 檔")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD，預設最新交易日")
    ap.add_argument("--top-n", type=int, help="覆寫成交值排行榜門檻（50 或 100）")
    ap.add_argument("--min-days", type=int, help="覆寫「窗口內至少幾天在榜」")
    ap.add_argument("--window", type=int, help="覆寫觀察窗口（同時套用到母體與法人）")
    ap.add_argument("--mode", choices=["trend", "reversal", "both"],
                    help="選股模式：順勢 / 打底反轉 / 兩者")
    ap.add_argument("--all", action="store_true", help="連未入選的一起列")
    ap.add_argument("--csv", help="輸出 CSV 路徑")
    args = ap.parse_args()

    if not config.DB_PATH.exists():
        print("找不到資料庫，請先跑：python run_backfill.py")
        return 1

    con = db.connect(config.DB_PATH)
    db.create_views(con)

    params = build_params(args)
    trade_date, result = screener.run(con, params, target_date=args.date)

    print(f"\n=== 選股結果  交易日 {trade_date} ===")
    print(
        f"母體：近 {params['UNIVERSE_LOOKBACK']} 日中至少 {params['UNIVERSE_MIN_DAYS']} 日"
        f"進入成交值前 {params['UNIVERSE_TOP_N']} 名"
    )
    mode = params.get("SCREEN_MODE", "trend")
    if mode in ("trend", "both"):
        bits = []
        if params["REQUIRE_KD_RISING"]:
            bits.append("KD 上升且 K>D")
        if params["REQUIRE_MACD_RISING"]:
            bits.append("MACD 柱狀體放大")
        if params["REQUIRE_ABOVE_MA"]:
            bits.append(f"站上 {params['MA_PERIOD']} 日均線")
        print(f"技術·順勢：{' · '.join(bits)}（與 {params['TREND_LOOKBACK']} 日前比較）")
    if mode in ("reversal", "both"):
        print("技術·反轉：DIF < 0 但回升 · 柱狀體已翻紅 · K > D（不要求站上均線）")
    print(
        f"法人：{'投信' if params['INST_PRIMARY'] == 'trust' else '外資'} "
        f"近 {params['INST_WINDOW']} 日淨買超 > {params['INST_MIN_NET']:,} 股"
    )

    if result.empty:
        print("\n沒有股票通過 Layer 1。試著放寬 --min-days 或 --top-n。")
        con.close()
        return 0

    print()
    for label, kept, total in screener.funnel_stats(result, params):
        print(f"  {label:<44} {kept:>4} / {total}")

    selected = result[result["selected"]]
    shown = result if args.all else selected

    if shown.empty:
        print("\n今日無個股同時通過技術與法人兩層。")
        print_reasons(result)
    else:
        print_conditions(shown, params)
        if args.all:
            print("\n（* 為入選）")
        print_reference(selected if not selected.empty else shown)
        print_levels(con, trade_date, selected if not selected.empty else shown)
        print_reasons(result)

    weeks = result["whale_weeks"].max() if "whale_weeks" in result.columns else 0
    weeks = 0 if weeks != weeks else int(weeks or 0)
    if weeks == 0:
        print("\n集保：尚未抓過。跑 daily.bat 或 python backfill_tdcc.py --weeks 2")
    else:
        print(f"\n集保：已累積 {weeks} 週" + ("（Δ4W 需要 5 週）" if weeks < 5 else ""))

    if args.csv:
        result.to_csv(args.csv, index=False, encoding="utf-8-sig")
        print(f"已輸出 {len(result)} 列到 {args.csv}")

    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
