"""技術指標。台股慣例，一律吃還原價。

兩個跟國外套件預設值不同、算錯了不會報錯只會靜靜給你不同數字的地方：

  * KD 的平滑係數是 1/3（K = 前K×2/3 + RSV×1/3），不是一般的 SMA/EMA。
    而且 K、D 的初始值是 50，不是第一筆 RSV —— pandas 的 ewm(adjust=False)
    預設拿第一筆值當起點，所以要自己塞 50 進去當種子。
  * MACD 的 EMA 用 alpha = 2/(N+1)，對應 pandas 的 ewm(span=N, adjust=False)。
"""

import numpy as np
import pandas as pd

MIN_HISTORY = 60          # 少於這個天數不出訊號（EMA26 + EMA9 需要暖身）
MA_PERIOD = 20            # 均線天數，與 config.MA_PERIOD 對齊
TREND_LOOKBACK = 3        # 「漸漸往上」比較幾日前


# ------------------------------------------------------------------ MACD
def macd(close: pd.Series, fast=12, slow=26, signal=9):
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    return dif, dea, dif - dea


# ------------------------------------------------------------------ KD
def _smooth_from_50(series: pd.Series, alpha=1 / 3) -> pd.Series:
    """以 50 為種子的指數平滑。

    pandas 的 ewm(adjust=False) 會拿序列第一筆當起點，台股軟體則是從 50 起算。
    做法是把 50 接在最前面跑完再丟掉，等價於 K(0) = 50×2/3 + RSV(0)×1/3。
    """
    valid = series.dropna()
    if valid.empty:
        return pd.Series(np.nan, index=series.index, dtype="float64")

    seeded = pd.concat([pd.Series([50.0]), valid.reset_index(drop=True)], ignore_index=True)
    smoothed = seeded.ewm(alpha=alpha, adjust=False).mean().iloc[1:]
    smoothed.index = valid.index
    return smoothed.reindex(series.index)


def kd(high: pd.Series, low: pd.Series, close: pd.Series, n=9):
    lowest = low.rolling(n).min()
    highest = high.rolling(n).max()
    span = highest - lowest

    # 九日內高低相等（連續漲停鎖死等）時 RSV 未定義，台股軟體慣例填 50
    rsv = ((close - lowest) / span.where(span != 0) * 100).where(span.notna())
    rsv = rsv.mask(span == 0, 50.0)

    k = _smooth_from_50(rsv)
    d = _smooth_from_50(k)
    return k, d


# ------------------------------------------------------------------ 組裝
def compute(frame: pd.DataFrame) -> pd.DataFrame:
    """輸入單一個股、依日期排序的還原價序列，回傳補上指標欄位的 frame。"""
    out = frame.sort_values("trade_date").copy()

    dif, dea, osc = macd(out["close"])
    out["dif"] = dif
    out["macd"] = dea
    out["osc"] = osc

    k, d = kd(out["high"], out["low"], out["close"])
    out["k"] = k
    out["d"] = d

    # 金叉：DIF 由下往上穿越訊號線
    prev_dif = out["dif"].shift(1)
    prev_macd = out["macd"].shift(1)
    out["golden_cross"] = (out["dif"] > out["macd"]) & (prev_dif <= prev_macd)

    # 量能：當日成交值 / 20 日均額
    out["turnover_ma20"] = out["turnover"].rolling(20).mean()
    out["vol_ratio"] = out["turnover"] / out["turnover_ma20"].where(out["turnover_ma20"] > 0)

    # 均線：站上均線代表處在多方結構裡
    out["ma"] = out["close"].rolling(MA_PERIOD).mean()
    out["above_ma"] = out["close"] > out["ma"]

    # 「漸漸往上」：今天比昨天高，而且比 TREND_LOOKBACK 天前也高。
    # 只比昨天會抓到雜訊反彈；只比 N 天前會漏掉「今天剛開始回落」的情況，兩個都要。
    def rising(series):
        return (series > series.shift(1)) & (series > series.shift(TREND_LOOKBACK))

    out["k_rising"] = rising(out["k"]) & (out["k"] > out["d"])
    out["osc_rising"] = rising(out["osc"])
    out["dif_rising"] = rising(out["dif"])

    out["bars"] = np.arange(1, len(out) + 1)
    return out


def compute_all(prices: pd.DataFrame) -> pd.DataFrame:
    """多檔一起算。prices 需含 stock_id / trade_date / high / low / close / turnover。"""
    if prices.empty:
        return prices
    # 不用 groupby.apply：pandas 2.2 起 include_groups 的行為在改，
    # 明確迴圈反而穩定，而且母體只有 100 檔，效能不是問題。
    frames = [compute(group) for _, group in prices.groupby("stock_id", sort=False)]
    return pd.concat(frames, ignore_index=True)
