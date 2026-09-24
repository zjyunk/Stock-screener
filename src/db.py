"""DuckDB schema 與載入。

設計重點：daily_price 存「原始價 + 還原因子」兩欄，不存還原價。
每次新的除權息都會改寫該股之前所有歷史的還原價，存因子的話重算便宜且可追溯。
指標一律從 view v_adjusted_price 取還原價來算。
"""

import logging

import duckdb

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_price (
    trade_date  DATE,
    stock_id    VARCHAR,
    market      VARCHAR,          -- TWSE / TPEx
    open        DOUBLE,
    high        DOUBLE,
    low         DOUBLE,
    close       DOUBLE,           -- 原始收盤價，未還原
    volume      BIGINT,           -- 成交股數
    turnover    BIGINT,           -- 成交金額（元）
    trades      BIGINT,
    PRIMARY KEY (trade_date, stock_id)
);

CREATE TABLE IF NOT EXISTS inst_flow (
    trade_date   DATE,
    stock_id     VARCHAR,
    market       VARCHAR,
    foreign_net  BIGINT,          -- 外陸資，不含外資自營商
    trust_net    BIGINT,          -- 投信（主訊號）
    dealer_net   BIGINT,          -- 自營商（存但不採用）
    total_net    BIGINT,
    PRIMARY KEY (trade_date, stock_id)
);

CREATE TABLE IF NOT EXISTS ex_right (
    ex_date     DATE,
    stock_id    VARCHAR,
    market      VARCHAR,
    prev_close  DOUBLE,           -- 除權息前收盤價
    ref_price   DOUBLE,           -- 除權息參考價
    factor      DOUBLE,           -- ref_price / prev_close
    PRIMARY KEY (ex_date, stock_id)
);

CREATE TABLE IF NOT EXISTS security_master (
    stock_id            VARCHAR PRIMARY KEY,
    name                VARCHAR,
    market              VARCHAR,
    industry            VARCHAR,
    shares_outstanding  BIGINT,
    delisted_date       DATE      -- 保留下市股，避免生存者偏差
);

-- 外資持股「存量」：籌碼現在在不在外資手上。
-- 跟 inst_flow 的買賣超（流量）互補：流量說今天買多少，存量說現在握多少。
CREATE TABLE IF NOT EXISTS foreign_holding (
    trade_date     DATE,
    stock_id       VARCHAR,
    market         VARCHAR,
    issued_shares  BIGINT,
    foreign_shares BIGINT,
    foreign_pct    DOUBLE,          -- 外資及陸資持股比率（%）
    PRIMARY KEY (trade_date, stock_id)
);

-- 融資融券餘額，單位「張」。融資是散戶槓桿的主要工具，可當散戶動向代理指標。
CREATE TABLE IF NOT EXISTS margin_balance (
    trade_date     DATE,
    stock_id       VARCHAR,
    market         VARCHAR,
    margin_balance BIGINT,          -- 融資今日餘額
    margin_prev    BIGINT,
    short_balance  BIGINT,          -- 融券今日餘額
    short_prev     BIGINT,
    PRIMARY KEY (trade_date, stock_id)
);

CREATE TABLE IF NOT EXISTS tdcc_dist (
    data_date   DATE,             -- 資料日（週五）
    avail_date  DATE,             -- 可使用日（下一交易日），join 一律用這欄
    stock_id    VARCHAR,
    level       SMALLINT,         -- 1..15，排除 16（合計）
    holders     BIGINT,
    shares      BIGINT,
    pct         DOUBLE,
    PRIMARY KEY (data_date, stock_id, level)
);
"""

# 後復權：某日的還原價 = 原始價 × 該日之後所有除權息因子的連乘
ADJUSTED_VIEW = """
CREATE OR REPLACE VIEW v_adjusted_price AS
WITH cum AS (
    SELECT
        p.trade_date,
        p.stock_id,
        COALESCE((
            SELECT exp(sum(ln(e.factor)))
            FROM ex_right e
            WHERE e.stock_id = p.stock_id
              AND e.ex_date > p.trade_date
              AND e.factor > 0
        ), 1.0) AS adj_factor
    FROM daily_price p
)
SELECT
    p.trade_date,
    p.stock_id,
    p.market,
    p.open  * c.adj_factor AS open,
    p.high  * c.adj_factor AS high,
    p.low   * c.adj_factor AS low,
    p.close * c.adj_factor AS close,
    p.close                AS raw_close,
    c.adj_factor,
    p.volume,
    p.turnover,
    p.trades
FROM daily_price p
JOIN cum c USING (trade_date, stock_id);
"""

# 上市 + 上櫃合併的成交值排行（近 5 日均額）
TURNOVER_RANK_VIEW = """
CREATE OR REPLACE VIEW v_turnover_rank AS
WITH avg5 AS (
    SELECT
        trade_date,
        stock_id,
        market,
        turnover,
        avg(turnover) OVER (
            PARTITION BY stock_id ORDER BY trade_date
            ROWS BETWEEN 4 PRECEDING AND CURRENT ROW
        ) AS turnover_ma5,
        count(*) OVER (
            PARTITION BY stock_id ORDER BY trade_date
            ROWS BETWEEN 4 PRECEDING AND CURRENT ROW
        ) AS n
    FROM daily_price
)
SELECT
    trade_date,
    stock_id,
    market,
    turnover,
    turnover_ma5,
    rank() OVER (PARTITION BY trade_date ORDER BY turnover_ma5 DESC) AS rank_ma5,
    rank() OVER (PARTITION BY trade_date ORDER BY turnover DESC)     AS rank_today
FROM avg5
WHERE n = 5;
"""


def connect(db_path):
    """DuckDB 同一時間只允許一個寫入程序。

    實務上很容易撞到：排程的 daily.bat 還在跑，你又手動跑 run_screener.py。
    原始錯誤訊息是英文的 IOException，這裡換成看得懂的說明。
    """
    try:
        con = duckdb.connect(str(db_path))
    except duckdb.IOException as exc:
        if "another process" in str(exc).lower() or "已" in str(exc) or "正由" in str(exc):
            raise RuntimeError(
                f"資料庫 {db_path} 正被另一個程序使用。\n"
                "DuckDB 一次只允許一個寫入程序 —— 通常是排程的 daily.bat、\n"
                "run_backfill.py 或 backfill_tdcc.py 還在跑。等它結束再試。"
            ) from exc
        raise
    con.execute(SCHEMA)
    return con


def create_views(con):
    con.execute(ADJUSTED_VIEW)
    con.execute(TURNOVER_RANK_VIEW)


def _upsert(con, table, rows, key_cols):
    """DuckDB 沒有 UPSERT，用 delete + insert。重跑同一天是冪等的。"""
    if not rows:
        return 0
    import pandas as pd

    frame = pd.DataFrame(rows)
    con.register("_staging", frame)

    cols = list(frame.columns)
    join = " AND ".join(f"t.{c} = s.{c}" for c in key_cols)
    con.execute(
        f"DELETE FROM {table} t WHERE EXISTS "
        f"(SELECT 1 FROM _staging s WHERE {join})"
    )
    con.execute(f"INSERT INTO {table} ({', '.join(cols)}) SELECT {', '.join(cols)} FROM _staging")
    con.unregister("_staging")
    return len(frame)


def load_prices(con, rows):
    for row in rows:
        row.pop("name", None)
        row.pop("shares_outstanding", None)
    return _upsert(con, "daily_price", rows, ["trade_date", "stock_id"])


def load_inst_flow(con, rows):
    return _upsert(con, "inst_flow", rows, ["trade_date", "stock_id"])


def load_ex_right(con, rows):
    return _upsert(con, "ex_right", rows, ["ex_date", "stock_id"])


def load_foreign_holding(con, rows):
    return _upsert(con, "foreign_holding", rows, ["trade_date", "stock_id"])


def load_margin(con, rows):
    return _upsert(con, "margin_balance", rows, ["trade_date", "stock_id"])

# 後載入的（最新的）覆蓋舊的
def load_security_master(con, rows):
    if not rows:
        return 0
    import pandas as pd

    frame = pd.DataFrame(rows)
    # 把 frame 註冊成名為 _sm 的虛擬表，之後 SQL 裡出現 _sm，就是指 frame 這個 DataFrame
    con.register("_sm", frame)
    # 先刪除同代號的列
    con.execute("DELETE FROM security_master t WHERE EXISTS (SELECT 1 FROM _sm s WHERE t.stock_id = s.stock_id)")
    cols = list(frame.columns)
    # 再插入
    con.execute(f"INSERT INTO security_master ({', '.join(cols)}) SELECT {', '.join(cols)} FROM _sm")
    con.unregister("_sm")
    return len(frame)


def update_shares_outstanding(con, pairs):
    """pairs: list[(stock_id, shares)]，上櫃的發行股數來自行情表。"""
    if not pairs:
        return 0
    import pandas as pd

    frame = pd.DataFrame(pairs, columns=["stock_id", "shares_outstanding"])
    con.register("_so", frame)
    con.execute(
        "UPDATE security_master SET shares_outstanding = s.shares_outstanding "
        "FROM _so s WHERE security_master.stock_id = s.stock_id"
    )
    con.unregister("_so")
    return len(frame)


def summary(con):
    out = {}
    for table in ("daily_price", "inst_flow", "foreign_holding", "margin_balance",
                  "ex_right", "security_master", "tdcc_dist"):
        out[table] = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    span = con.execute("SELECT min(trade_date), max(trade_date) FROM daily_price").fetchone()
    out["date_range"] = span
    out["markets"] = con.execute(
        "SELECT market, count(DISTINCT stock_id) FROM daily_price GROUP BY market"
    ).fetchall()
    return out
