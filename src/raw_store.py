"""Raw 層：抓下來的原始回應直接落地，不做任何解析。

理由：TWSE / TPEx 的 JSON 欄位順序與名稱歷年改過數次。原始檔留著，
解析邏輯改了可以重跑，不必再跟交易所要一次資料（3 年回補要跑 3 小時）。

檔案佈局：raw/<source>/<YYYY>/<source>-<YYYYMMDD>.json.gz
"""

import gzip
from pathlib import Path


def raw_path(root: Path, source: str, date_str: str) -> Path:
    """date_str 為 YYYYMMDD。"""
    return root / source / date_str[:4] / f"{source}-{date_str}.json.gz"


def exists(root: Path, source: str, date_str: str) -> bool:
    return raw_path(root, source, date_str).exists()


def write(root: Path, source: str, date_str: str, text: str) -> Path:
    path = raw_path(root, source, date_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 先寫暫存再 rename，避免中途中斷留下半個檔案被當成已完成
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        fh.write(text)
    tmp.replace(path)
    return path # return回去給twse/tpex.py


def read(root: Path, source: str, date_str: str) -> str:
    with gzip.open(raw_path(root, source, date_str), "rt", encoding="utf-8") as fh:
        return fh.read()


def list_dates(root: Path, source: str):
    """已抓過的日期，排序後回傳。"""
    base = root / source
    if not base.exists():
        return []
    dates = []
    # rglob 的 r 是 recursive（遞迴）
    # 因為檔案放在 raw/<source>/<年>/ 底下，多了一層年份資料夾，所以要用遞迴搜尋
    for path in base.rglob(f"{source}-*.json.gz"):
        dates.append(path.stem.replace(f"{source}-", "").replace(".json", ""))
    return sorted(dates)
