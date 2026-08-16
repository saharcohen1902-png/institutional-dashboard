"""SQLite storage for 13F holdings and CFTC COT positioning."""

import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "holdings.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS filings (
    accession     TEXT PRIMARY KEY,
    institution   TEXT NOT NULL,
    cik           INTEGER NOT NULL,
    form          TEXT NOT NULL,
    amendment_type TEXT,
    filing_date   TEXT NOT NULL,
    report_date   TEXT NOT NULL,
    row_count     INTEGER,
    total_value   INTEGER,
    superseded    INTEGER NOT NULL DEFAULT 0,
    fetched_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_filings_inst ON filings(institution, report_date);

CREATE TABLE IF NOT EXISTS holdings (
    accession   TEXT NOT NULL REFERENCES filings(accession) ON DELETE CASCADE,
    institution TEXT NOT NULL,
    report_date TEXT NOT NULL,
    cusip       TEXT NOT NULL,
    issuer      TEXT NOT NULL,
    title_class TEXT,
    put_call    TEXT NOT NULL DEFAULT '',   -- '', 'Put', 'Call'
    value_usd   INTEGER NOT NULL,
    shares      INTEGER NOT NULL,
    share_type  TEXT                        -- 'SH' or 'PRN'
);
CREATE INDEX IF NOT EXISTS idx_holdings_lookup
    ON holdings(institution, report_date, cusip, put_call);
CREATE INDEX IF NOT EXISTS idx_holdings_acc ON holdings(accession);

-- COT positioning by trader category.
--   primary_kind = 'asset_manager'  (TFF, financial futures — institutional)
--                = 'managed_money'   (Disaggregated, commodities — no
--                                     institutional breakdown is published)
-- inst_* is that primary (institutional-ish) category; hf_* is Leveraged
-- Funds (hedge funds) and is only populated for the TFF financial contracts.
CREATE TABLE IF NOT EXISTS cot (
    report_date    TEXT NOT NULL,
    contract_code  TEXT NOT NULL,
    market_name    TEXT NOT NULL,
    primary_kind   TEXT NOT NULL,
    open_interest  INTEGER,
    inst_long      INTEGER,
    inst_short     INTEGER,
    inst_long_chg  INTEGER,
    inst_short_chg INTEGER,
    hf_long        INTEGER,
    hf_short       INTEGER,
    hf_long_chg    INTEGER,
    hf_short_chg   INTEGER,
    fetched_at     TEXT NOT NULL,
    PRIMARY KEY (report_date, contract_code)
);

-- Free-form key/value log so the UI can show when each source last ran.
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(filings)")}
    if "superseded" not in cols:
        conn.execute("ALTER TABLE filings ADD COLUMN superseded INTEGER NOT NULL DEFAULT 0")
    # The COT table was reshaped from the legacy Non-Commercial report to the
    # TFF/Disaggregated trader-category schema. Drop the old one so the new
    # CREATE takes effect; fetch_cot repopulates it from scratch.
    cot_cols = {r["name"] for r in conn.execute("PRAGMA table_info(cot)")}
    if cot_cols and "inst_long" not in cot_cols:
        conn.executescript("DROP TABLE cot;" + SCHEMA)
    conn.commit()


def set_meta(conn, key, value):
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def get_meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default
