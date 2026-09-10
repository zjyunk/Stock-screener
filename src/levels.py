"""支撐與壓力：找出「很多人在這個價位交易過」的地方。

兩個來源，各自有行為學上的理由：

  成交量密集區  把近 N 天的成交量依價位分桶，量最大的價位是最多人的成本所在。
               價格跌回來，套牢的人不想認賠、覺得便宜的人想進場，買盤集中 → 支撐；
               價格漲上去，套牢的人終於解套想跑 → 壓力。
  前高前低      局部極值。前高是「上次漲到這裡被打下來」，前低是「上次跌到這裡有人接」。

兩者合併後，把相近的價位聚成一個區間，依「離現價多遠」分成支撐（下方）與壓力（上方）。

全部用還原價計算，跟 K 線圖同一套座標；今天的還原價等於原始價，所以支撐壓力
可以直接跟今天的收盤比。
"""

import math

import numpy as np

LOOKBACK_DAYS = 120       # 看多久的歷史
BIN_PCT = 0.008           # 價位分桶寬度 = 現價的 0.8%
CLUSTER_PCT = 0.015       # 相距 1.5% 內的價位合併成一個區間
SWING_WINDOW = 5          # 前高前低：左右各 5 天內的最高／最低
PEAK_MIN_RATIO = 1.3      # 密集區至少要是平均量的 1.3 倍才算
TOP_N = 3                 # 各取幾個支撐／壓力


def volume_profile(rows, bin_pct=BIN_PCT):
    """回傳 list[(價位中心, 成交量占比)]，只含局部高峰。

    每天的成交量平均攤在當天的 [低, 高] 區間裡 —— 比只算收盤價合理，
    因為當天在各價位都有人成交。
    """
    lo = min(r["low"] for r in rows)
    hi = max(r["high"] for r in rows)
    current = rows[-1]["close"]
    width = current * bin_pct
    if width <= 0 or hi <= lo:
        return []

    n_bins = int(math.ceil((hi - lo) / width)) + 1
    profile = np.zeros(n_bins)
    for r in rows:
        vol = r.get("volume") or 0
        if not vol:
            continue
        b0 = int((r["low"] - lo) / width)
        b1 = int((r["high"] - lo) / width)
        b0, b1 = max(0, min(b0, n_bins - 1)), max(0, min(b1, n_bins - 1))
        profile[b0:b1 + 1] += vol / (b1 - b0 + 1)

    total = profile.sum()
    if total <= 0:
        return []
    mean = total / n_bins

    peaks = []
    for i in range(n_bins):
        v = profile[i]
        if v < mean * PEAK_MIN_RATIO:
            continue
        left = profile[i - 1] if i > 0 else 0
        right = profile[i + 1] if i < n_bins - 1 else 0
        if v >= left and v >= right:
            peaks.append((lo + (i + 0.5) * width, v / total))
    return peaks


def swing_points(rows, window=SWING_WINDOW):
    """回傳 (前高 list, 前低 list)，每個元素是 (價位, 該日索引)。"""
    highs = [r["high"] for r in rows]
    lows = [r["low"] for r in rows]
    n = len(rows)
    sh, sl = [], []
    for i in range(window, n - window):
        seg_h = highs[i - window:i + window + 1]
        seg_l = lows[i - window:i + window + 1]
        if highs[i] == max(seg_h) and seg_h.count(highs[i]) == 1:
            sh.append((highs[i], i))
        if lows[i] == min(seg_l) and seg_l.count(lows[i]) == 1:
            sl.append((lows[i], i))
    return sh, sl


def _cluster(points, tol_pct):
    """points: list[(price, weight, kind)]。相近的合併，回傳 list[dict]。"""
    if not points:
        return []
    points = sorted(points, key=lambda p: p[0])
    clusters = []
    cur = [points[0]]
    for p in points[1:]:
        anchor = sum(x[0] * x[1] for x in cur) / sum(x[1] for x in cur)
        if abs(p[0] - anchor) / anchor <= tol_pct:
            cur.append(p)
        else:
            clusters.append(cur)
            cur = [p]
    clusters.append(cur)

    out = []
    for c in clusters:
        w = sum(x[1] for x in c)
        price = sum(x[0] * x[1] for x in c) / w
        kinds = {x[2] for x in c}
        out.append(
            {
                "price": price,
                "weight": w,
                "vol_share": sum(x[1] for x in c if x[2] == "vp"),
                "touches": sum(1 for x in c if x[2] in ("high", "low")),
                "sources": sorted(kinds),
            }
        )
    return out


def find_levels(rows, top_n=TOP_N):
    """主入口。rows 由舊到新，每筆需含 high / low / close / volume（還原價）。

    回傳 {"support": [...], "resistance": [...], "current": 現價}，
    各 level 含 price / dist_pct / vol_share / touches / sources / strength。
    """
    rows = [r for r in rows if r.get("close") and r.get("high") and r.get("low")]
    rows = rows[-LOOKBACK_DAYS:]
    if len(rows) < 30:
        return {"support": [], "resistance": [], "current": None}

    current = rows[-1]["close"]
    points = []

    # 成交量密集區：權重 = 成交量占比（通常 0.02 ~ 0.15）
    for price, share in volume_profile(rows):
        points.append((price, share, "vp"))

    # 前高前低：每個給固定小權重，越近期權重略高
    sh, sl = swing_points(rows)
    n = len(rows)
    for price, idx in sh:
        points.append((price, 0.02 * (0.5 + idx / n), "high"))
    for price, idx in sl:
        points.append((price, 0.02 * (0.5 + idx / n), "low"))

    clusters = _cluster(points, CLUSTER_PCT)

    # 現價附近 0.5% 內的不算——那不是支撐壓力，是現在的價位
    near = current * 0.005
    support = [c for c in clusters if c["price"] < current - near]
    resistance = [c for c in clusters if c["price"] > current + near]

    # 支撐：由近到遠、權重高優先。取「離現價最近的幾個且有份量」
    def rank(levels, reverse):
        levels = sorted(levels, key=lambda c: c["weight"], reverse=True)[: top_n * 2]
        levels = sorted(levels, key=lambda c: c["price"], reverse=reverse)[:top_n]
        return levels

    support = rank(support, reverse=True)      # 由高到低（最近的在前）
    resistance = rank(resistance, reverse=False)  # 由低到高

    max_w = max([c["weight"] for c in support + resistance] + [1e-9])
    for c in support + resistance:
        c["dist_pct"] = (c["price"] / current - 1) * 100
        c["strength"] = c["weight"] / max_w          # 0~1，相對強度
        c["price"] = round(c["price"], 2)
        c["dist_pct"] = round(c["dist_pct"], 2)
        c["vol_share"] = round(c["vol_share"], 4)
        c["strength"] = round(c["strength"], 2)
        c.pop("weight", None)

    return {"support": support, "resistance": resistance, "current": current}


def describe(levels, digits=2):
    """一行文字，給 CLI 用。"""
    cur = levels.get("current")
    if cur is None:
        return "資料不足"
    def one(c):
        tag = "".join(
            {"vp": "量", "high": "高", "low": "低"}[s] for s in c["sources"]
        )
        return f"{c['price']:.{digits}f}({c['dist_pct']:+.1f}%{tag})"
    sup = " ".join(one(c) for c in levels["support"]) or "—"
    res = " ".join(one(c) for c in levels["resistance"]) or "—"
    return f"支撐 {sup} ｜ 壓力 {res}"


# ------------------------------------------------------------------ 趨勢線
TREND_MAX_POINTS = 6      # 從最近幾個波段低（高）點裡挑
TREND_TOL_PCT = 0.01      # 距離線 1% 內算「觸碰」
TREND_MIN_SPAN = 10       # 兩個端點至少隔幾天，太近的線沒意義
TREND_MAX_DIST = 0.12     # 延伸到今天離現價超過 12% 的線不採用，離太遠沒有操作意義
TREND_MAX_STALE = 40      # 最後一次觸碰距今超過 40 天的線不採用，市場已經不守它了


def trend_line(rows, kind="support", window=SWING_WINDOW):
    """把「至少兩個波段低點（高點）」連成一條線，延伸到今天。

    支撐趨勢線：連波段低點，之後的最低價都不能明顯跌破這條線（跌破就不算支撐）。
    壓力趨勢線：連波段高點，之後的最高價都不能明顯突破。

    候選線很多（任兩點都能連），挑法：先淘汰被違反的，再取「觸碰次數最多、
    其次是最近期」的那條。回傳 None 代表找不到有效的線。

    ⚠ 三年逐日檢定（research_levels.py）：貼近／穿越趨勢線的八個情境全部不顯著，
    樣本外方向全部翻轉。趨勢線沒有預測力，只保留做圖形對照。水平價位
    （find_levels）則通過檢定。合理的解釋：水平價位對應真實的成交密集區
    （很多人的成本在那裡），趨勢線是幾何作圖，斜線上一個月前的價位不是任何人的成本。
    """
    rows = [r for r in rows if r.get("close") and r.get("high") and r.get("low")]
    if len(rows) < 30:
        return None
    sh, sl = swing_points(rows, window)
    pts = (sl if kind == "support" else sh)[-TREND_MAX_POINTS:]
    if len(pts) < 2:
        return None

    n = len(rows)
    series = [r["low"] for r in rows] if kind == "support" else [r["high"] for r in rows]
    best = None
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            (p1, x1), (p2, x2) = pts[i], pts[j]
            if x2 - x1 < TREND_MIN_SPAN:
                continue
            slope = (p2 - p1) / (x2 - x1)
            line = lambda x: p1 + slope * (x - x1)

            violated = False
            touches = 0
            last_touch = x1
            for x in range(x1, n):
                lv = line(x)
                if lv <= 0:
                    violated = True
                    break
                gap = (series[x] - lv) / lv
                if kind == "support" and gap < -TREND_TOL_PCT:
                    violated = True
                    break
                if kind == "resistance" and gap > TREND_TOL_PCT:
                    violated = True
                    break
                if abs(gap) <= TREND_TOL_PCT:
                    touches += 1
                    last_touch = x
            if violated:
                continue

            today = line(n - 1)
            if abs(today / rows[-1]["close"] - 1) > TREND_MAX_DIST:
                continue                        # 離現價太遠，沒有操作意義
            if n - 1 - last_touch > TREND_MAX_STALE:
                continue                        # 太久沒被觸碰，市場已經不守它了

            cand = {
                "kind": kind,
                "slope_pct": round(slope / rows[-1]["close"] * 100, 3),   # 每日 %，正 = 上升
                "value_today": round(today, 2),
                "dist_pct": round((today / rows[-1]["close"] - 1) * 100, 2),
                "touches": touches,
                "start": rows[x1]["trade_date"], "start_price": round(p1, 2),
                "end": rows[x2]["trade_date"], "end_price": round(p2, 2),
                "last": rows[-1]["trade_date"],
                "last_touch": rows[last_touch]["trade_date"],
            }
            key = (touches, last_touch)
            if best is None or key > best[0]:
                best = (key, cand)
    return best[1] if best else None


def describe_trend(tl):
    if not tl:
        return "—"
    direction = "上升" if tl["slope_pct"] > 0.05 else "下降" if tl["slope_pct"] < -0.05 else "水平"
    return (f"{direction}{'支撐' if tl['kind']=='support' else '壓力'}線 "
            f"今 {tl['value_today']:.2f}({tl['dist_pct']:+.1f}%) 觸 {tl['touches']} 次 "
            f"{tl['start'][5:]}→{tl['end'][5:]}")


# ------------------------------------------------------------------ 警示
NEAR_PCT = 0.02


def warnings_for(lv, prev_close=None):
    """依三年實證給出提醒。只提醒，不當篩選條件。

    實測（19,717 筆、持有 20 日、對同日母體）：
      貼近壓力（下方 2% 內）  -0.52%  t=-2.60   強壓力 -0.82%  t=-2.56
      剛跌破支撐             -0.74%  t=-2.42
      剛突破壓力             +0.55%  t=+1.69（樣本內外一致，但未達顯著）
      貼近支撐               -0.06%  t=-0.27   ← 沒有「支撐反彈」這回事
    """
    out = []
    cur = lv.get("current")
    if not cur:
        return out
    for c in lv["resistance"]:
        gap = (c["price"] - cur) / cur
        if 0 < gap <= NEAR_PCT:
            out.append(f"上方 {gap*100:.1f}% 有{'強' if c['strength'] >= 0.7 else ''}壓力 {c['price']:.2f}")
            break
    if prev_close is not None:
        for c in lv["support"]:
            if prev_close >= c["price"] > cur:
                out.append(f"今日跌破支撐 {c['price']:.2f}")
                break
        # 突破壓力要用「昨天的壓力清單」才嚴謹，這裡用今天清單裡剛好被跨過的近似
    return out
