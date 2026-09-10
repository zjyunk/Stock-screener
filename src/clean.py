"""交易所回傳值的清洗工具。

兩所的數字都是帶千分位的字串，缺值有多種寫法（"--"、"---"、""、"　"），
漲跌欄還夾雜 HTML（<p style= color:green>-</p>）。全部集中在這裡處理。
"""

import re

_TAG = re.compile(r"<[^>]+>")
_MISSING = {"", "--", "---", "----", "N/A", "NA", "無", "除權息"}


def text(value) -> str:
    if value is None:
        return ""
    return _TAG.sub("", str(value)).replace("　", " ").strip()


def to_int(value):
    """回傳 int 或 None。"""
    raw = text(value).replace(",", "").replace("+", "")
    if raw in _MISSING:
        return None
    try:
        return int(float(raw))
    except ValueError:
        return None


def to_float(value):
    raw = text(value).replace(",", "").replace("+", "")
    if raw in _MISSING:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def roc_to_iso(value):
    """民國日期轉 ISO。接受 '114年08月01日'、'114/08/01'、'1140801'。"""
    raw = text(value)
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 7:
        year = int(digits[:3]) + 1911
        return f"{year:04d}-{digits[3:5]}-{digits[5:7]}"
    if len(digits) == 6:  # 三位民國年 + 月日各一位的情況極少，保守處理
        return None
    return None


def par_value(value):
    """解析「普通股每股面額」。

    實際出現的值：'新台幣  10.0000元'（1,073 檔）、'0.1'、'無面額'、'美金0.05元'。
    非台幣面額或無面額回 None —— 這種股票算不出流通股數，選股時寧可跳過也不要用錯的分母。
    """
    raw = text(value)
    if not raw or "無面額" in raw:
        return None
    if "美金" in raw or "USD" in raw.upper():
        return None
    match = re.search(r"\d+(?:\.\d+)?", raw)
    if not match:
        return None
    par = float(match.group())
    return par if par > 0 else None


def is_common_stock(code: str) -> bool:
    """4 碼純數字才視為普通股。排除 ETF(00xx)、權證/牛熊證(6碼)、DR(9xxx)。"""
    code = text(code)
    if len(code) != 4 or not code.isdigit():
        return False
    if code.startswith("00"):     # ETF / ETN
        return False
    if code.startswith("9"):      # 存託憑證 DR
        return False
    return True
