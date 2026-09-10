"""集保股權分散表的「歷史」抓取。

跟 run_daily.py 的每日抓取不同：
  * opendata API（getOD.ashx）一次給全市場，但**只有最新一週**
  * 查詢頁（qryStock）有 51 週歷史，但**一次只給一檔一週**

所以歷史回補是 N 檔 × M 週次請求，很貴。只補真正用得到的股票。

TDCC 只保留約 51 週（實測 2025-09-12 ~ 2026-09-04），最舊的那幾週會陸續消失，
要補就要趁早。這個頁面是給人手動查的，沒有提供 API，請維持低頻率。
"""

import logging
import re

log = logging.getLogger(__name__)

QUERY_URL = "https://www.tdcc.com.tw/portal/zh/smWeb/qryStock"

_TOKEN = re.compile(r'name="SYNCHRONIZER_TOKEN" value="([^"]*)"')
_URI = re.compile(r'name="SYNCHRONIZER_URI" value="([^"]*)"')
_OPTION = re.compile(r'<option[^>]*value="(\d{8})"')
_TABLE = re.compile(r"<table.*?</table>", re.S)
_ROW = re.compile(r"<tr.*?</tr>", re.S)
_CELL = re.compile(r"<t[dh].*?</t[dh]>", re.S)
_TAG = re.compile(r"<[^>]+>")


def _cells(row_html):
    return [_TAG.sub("", c).replace("\xa0", " ").strip() for c in _CELL.findall(row_html)]


class TdccHistory:
    """一個 session 打多次查詢。SYNCHRONIZER_TOKEN 每次回應都會換，要跟著更新。"""

    def __init__(self, client):
        self.client = client
        self.token = None
        self.uri = None
        self.dates = []

    def open(self):
        html = self.client.get_text(QUERY_URL)
        self._absorb(html)
        self.dates = sorted(set(_OPTION.findall(html)), reverse=True)
        if not self.dates:
            raise RuntimeError("TDCC 查詢頁沒有解析到任何資料日期，頁面可能改版了")
        log.info("TDCC 可查詢週次 %d 週：%s ~ %s",
                 len(self.dates), self.dates[-1], self.dates[0])
        return self.dates

    def _absorb(self, html):
        token = _TOKEN.search(html)
        uri = _URI.search(html)
        if token:
            self.token = token.group(1)
        if uri:
            self.uri = uri.group(1)

    def fetch(self, stock_no, date_str):
        """回傳該檔該週的 15 個級距，list[dict]；查無資料回空 list。"""
        if self.token is None:
            self.open()

        html = self.client.post_text(
            QUERY_URL,
            data={
                "SYNCHRONIZER_TOKEN": self.token,
                "SYNCHRONIZER_URI": self.uri,
                "method": "submit",
                "firDate": date_str,
                "scaDate": date_str,
                "sqlMethod": "StockNo",
                "stockNo": stock_no,
                "stockName": "",
            },
        )
        self._absorb(html)
        return self._parse(html, stock_no, date_str)

    @staticmethod
    def _parse(html, stock_no, date_str):
        """資料表長這樣（17 列）：
        表頭 / 序1..15 各級距 / 合計
        欄位：序, 持股分級, 人數, 股數, 占集保庫存數比例(%)
        """
        rows = []
        for table in _TABLE.findall(html):
            body = _ROW.findall(table)
            if len(body) < 10:          # 第一張表是查詢表單，列數很少
                continue
            for row in body:
                cells = _cells(row)
                if len(cells) < 5 or not cells[0].isdigit():
                    continue            # 表頭與「合計」列
                level = int(cells[0])
                if not 1 <= level <= 15:
                    continue
                rows.append(
                    {
                        "data_date": f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}",
                        "stock_id": stock_no,
                        "level": level,
                        "holders": int(cells[2].replace(",", "") or 0),
                        "shares": int(cells[3].replace(",", "") or 0),
                        "pct": float(cells[4].replace(",", "") or 0),
                    }
                )
            if rows:
                break
        if len(rows) not in (0, 15):
            log.warning("%s %s 解析到 %d 個級距（預期 15），可能是新上市或停牌",
                        stock_no, date_str, len(rows))
        return rows
