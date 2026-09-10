"""限速 HTTP client。

交易所端點沒有公布速率上限，但實測請求太密會拿到 429 或被暫時鎖 IP。
這裡強制每個 host 之間最小間隔 + 失敗指數退避，並在退避時加抖動避免同步重試。
"""

import logging
import random
import time

import requests

log = logging.getLogger(__name__)


class RateLimitedClient:
    def __init__(self, min_interval, user_agent, timeout=30, max_retries=4):
        self.min_interval = min_interval
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-TW,zh;q=0.9",
            }
        )
        self._last_request = 0.0

    def _throttle(self):
        elapsed = time.monotonic() - self._last_request
        wait = self.min_interval - elapsed
        if wait > 0:
            time.sleep(wait + random.uniform(0, 0.4))

    def get_text(self, url, params=None):
        """回傳 response text；失敗時退避重試，全部失敗才丟例外。"""
        return self._request("GET", url, params=params)

    def post_text(self, url, data=None):
        return self._request("POST", url, data=data)

    def _request(self, method, url, params=None, data=None):
        last_err = None
        for attempt in range(self.max_retries):
            self._throttle()
            try:
                resp = self.session.request(
                    method, url, params=params, data=data, timeout=self.timeout
                )
                self._last_request = time.monotonic()

                # 429 / 5xx 視為暫時性錯誤，退避後重試
                if resp.status_code == 429 or resp.status_code >= 500:
                    backoff = (2 ** attempt) * self.min_interval
                    log.warning(
                        "HTTP %s from %s, backing off %.0fs (attempt %d/%d)",
                        resp.status_code, url, backoff, attempt + 1, self.max_retries,
                    )
                    time.sleep(backoff)
                    last_err = RuntimeError(f"HTTP {resp.status_code}")
                    continue

                resp.raise_for_status()
                # utf-8-sig：TDCC 的 CSV 開頭有 BOM，用純 utf-8 解會讓第一個欄位名
                # 變成 "﻿資料日期"，欄位取不到值而且不會報錯。
                # 對沒有 BOM 的回應（兩所的 JSON）行為與 utf-8 完全相同。
                resp.encoding = "utf-8-sig"
                return resp.text

            except requests.RequestException as exc:
                self._last_request = time.monotonic()
                backoff = (2 ** attempt) * self.min_interval
                log.warning(
                    "%s on %s, backing off %.0fs (attempt %d/%d)",
                    type(exc).__name__, url, backoff, attempt + 1, self.max_retries,
                )
                time.sleep(backoff)
                last_err = exc

        raise RuntimeError(f"giving up on {url}: {last_err}")
