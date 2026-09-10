"""指標正確性測試。

作法是拿「一看就知道對」的逐日迴圈當參考實作，去比對向量化版本。
MACD/KD 算錯不會報錯，只會靜靜給出跟看盤軟體不一樣的數字，
所以這裡驗的是台股慣例的兩個細節：KD 用 1/3 平滑、初始值 50。

    python tests/test_indicators.py
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import indicators  # noqa: E402


def reference_kd(high, low, close, n=9):
    """逐日迴圈版 KD，台股慣例。"""
    k_prev, d_prev = 50.0, 50.0
    ks, ds = [], []
    for i in range(len(close)):
        if i < n - 1:
            ks.append(np.nan)
            ds.append(np.nan)
            continue
        window_high = max(high[i - n + 1: i + 1])
        window_low = min(low[i - n + 1: i + 1])
        span = window_high - window_low
        rsv = 50.0 if span == 0 else (close[i] - window_low) / span * 100
        k_prev = k_prev * 2 / 3 + rsv / 3
        d_prev = d_prev * 2 / 3 + k_prev / 3
        ks.append(k_prev)
        ds.append(d_prev)
    return ks, ds


def reference_ema(values, span):
    alpha = 2 / (span + 1)
    out, prev = [], None
    for v in values:
        prev = v if prev is None else alpha * v + (1 - alpha) * prev
        out.append(prev)
    return out


def reference_macd(close, fast=12, slow=26, signal=9):
    ema_f = reference_ema(close, fast)
    ema_s = reference_ema(close, slow)
    dif = [f - s for f, s in zip(ema_f, ema_s)]
    dea = reference_ema(dif, signal)
    return dif, dea


def make_series(n=200, seed=42):
    rng = np.random.default_rng(seed)
    close = 100 + np.cumsum(rng.normal(0, 1.5, n))
    high = close + rng.uniform(0.1, 2.0, n)
    low = close - rng.uniform(0.1, 2.0, n)
    return high, low, close


def check(label, ours, ref, tol=1e-9):
    ours = np.asarray(ours, dtype="float64")
    ref = np.asarray(ref, dtype="float64")
    mask = ~np.isnan(ref)
    if mask.sum() == 0:
        print(f"[FAIL] {label}: 參考值全為 NaN")
        return False
    diff = np.nanmax(np.abs(ours[mask] - ref[mask]))
    ok = diff < tol
    print(f"[{'PASS' if ok else 'FAIL'}] {label}  最大誤差 {diff:.3e}  (比對 {int(mask.sum())} 點)")
    return ok


def main():
    high, low, close = make_series()
    idx = pd.date_range("2025-01-01", periods=len(close), freq="B")
    s_high = pd.Series(high, index=idx)
    s_low = pd.Series(low, index=idx)
    s_close = pd.Series(close, index=idx)

    passed = True

    k, d = indicators.kd(s_high, s_low, s_close)
    ref_k, ref_d = reference_kd(list(high), list(low), list(close))
    passed &= check("KD 的 K 值（1/3 平滑、種子 50）", k.values, ref_k)
    passed &= check("KD 的 D 值", d.values, ref_d)

    dif, dea, osc = indicators.macd(s_close)
    ref_dif, ref_dea = reference_macd(list(close))
    passed &= check("MACD 的 DIF", dif.values, ref_dif)
    passed &= check("MACD 的訊號線", dea.values, ref_dea)
    passed &= check("MACD 柱狀體 OSC", osc.values,
                    [a - b for a, b in zip(ref_dif, ref_dea)])

    # 種子必須是 50：第一個有效 K = 50*2/3 + RSV*1/3
    first_valid = k.dropna().index[0]
    lowest = s_low.rolling(9).min()[first_valid]
    highest = s_high.rolling(9).max()[first_valid]
    rsv0 = (s_close[first_valid] - lowest) / (highest - lowest) * 100
    expect = 50 * 2 / 3 + rsv0 / 3
    ok = abs(k[first_valid] - expect) < 1e-9
    print(f"[{'PASS' if ok else 'FAIL'}] K 初始值以 50 為種子  "
          f"實得 {k[first_valid]:.6f} 應為 {expect:.6f}")
    passed &= ok

    # 金叉：DIF 上穿訊號線的當天為 True，之後維持在上方的日子不是
    frame = pd.DataFrame({
        "trade_date": idx, "high": high, "low": low, "close": close,
        "turnover": np.full(len(close), 1e8),
    })
    out = indicators.compute(frame)
    cross_rows = out[out["golden_cross"]]
    bad = 0
    for pos in cross_rows.index:
        i = out.index.get_loc(pos)
        if i == 0:
            continue
        if not (out["dif"].iloc[i] > out["macd"].iloc[i]
                and out["dif"].iloc[i - 1] <= out["macd"].iloc[i - 1]):
            bad += 1
    ok = bad == 0 and len(cross_rows) > 0
    print(f"[{'PASS' if ok else 'FAIL'}] 金叉判定  抓到 {len(cross_rows)} 次，錯誤 {bad} 次")
    passed &= ok

    # 連續在上方不該一直判為金叉
    above = (out["dif"] > out["macd"]).sum()
    print(f"        （DIF 在訊號線上方共 {above} 天，金叉只有 {len(cross_rows)} 天 —— 合理）")

    print()
    print("全部通過" if passed else "*** 有測試失敗 ***")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
