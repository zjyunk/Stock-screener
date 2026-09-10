"""選股邏輯一致性測試。

backtest.py 為了效率把四層漏斗重寫成向量化版本（一次算完整段期間），
screener.py 則是每天重跑一次。兩者條件必須完全一致 ——
否則回測驗證的根本不是你每天實際在用的選股邏輯，而這種 bug 不會有任何錯誤訊息。

這支程式隨機抽日期，逐日比對兩邊選出來的個股集合。

    python tests/test_consistency.py
    python tests/test_consistency.py --dates 30
"""

import argparse
import random
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
warnings.filterwarnings("ignore")

import config  # noqa: E402
from src import backtest, db, screener  # noqa: E402

PARAM_KEYS = (
    "SCREEN_MODE", "REVERSAL_REQUIRE_INST", "UNIVERSE_TOP_N", "UNIVERSE_LOOKBACK", "UNIVERSE_MIN_DAYS",
    "TREND_LOOKBACK", "MA_PERIOD", "REQUIRE_ABOVE_MA",
    "REQUIRE_KD_RISING", "REQUIRE_MACD_RISING",
    "INST_PRIMARY", "INST_WINDOW", "INST_MIN_NET",
    "INST_MIN_BUY_DAYS", "INST_MIN_PCT",
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dates", type=int, default=20, help="抽驗幾個交易日")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    if not config.DB_PATH.exists():
        print("找不到資料庫，請先跑 run_backfill.py")
        return 1

    params = {key: getattr(config, key) for key in PARAM_KEYS}
    con = db.connect(config.DB_PATH)
    db.create_views(con)

    all_dates = [
        r[0] for r in con.execute(
            "SELECT DISTINCT trade_date FROM daily_price ORDER BY trade_date"
        ).fetchall()
    ]
    # 前段要留給指標暖身，否則 screener 與 backtest 的暖身視窗長度不同會有差異
    candidates = all_dates[160:]
    if len(candidates) < args.dates:
        print(f"可用交易日只有 {len(candidates)} 天，資料量不足以做這項比對")
        return 1

    random.seed(args.seed)
    sample = sorted(random.sample(candidates, args.dates))

    print(f"抽驗 {len(sample)} 個交易日：{sample[0]} ~ {sample[-1]}\n")

    # 向量化版本一次算完整段
    panel, _ = backtest.load_panel(con, params, start=str(sample[0]), end=str(sample[-1]))
    panel = backtest.add_signal_columns(panel, params)
    vec = panel[panel["selected"]]
    vec_by_date = {
        d: set(g["stock_id"]) for d, g in vec.groupby(vec["trade_date"].dt.date)
    }

    mismatches = 0
    total_signals = 0
    for day in sample:
        _, result = screener.run(con, params, target_date=str(day), with_extras=False)
        loop_set = set(result.loc[result["selected"], "stock_id"]) if not result.empty else set()
        vec_set = vec_by_date.get(day, set())
        total_signals += len(loop_set)

        if loop_set == vec_set:
            print(f"[PASS] {day}  {len(loop_set)} 檔一致")
        else:
            mismatches += 1
            only_loop = sorted(loop_set - vec_set)
            only_vec = sorted(vec_set - loop_set)
            print(f"[FAIL] {day}  screener 獨有={only_loop}  backtest 獨有={only_vec}")

    print()
    print(f"共比對 {len(sample)} 天、{total_signals} 個訊號")
    if mismatches:
        print(f"*** {mismatches} 天不一致 —— 兩邊的條件已經分岔，回測結果不可信 ***")
        con.close()
        return 1

    print("兩邊選股結果完全一致")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
