"""Resolve CUSIP -> ticker -> shares outstanding for accumulation candidates.

Everything here is keyless and free:
  - OpenFIGI  maps CUSIP -> ticker / name / security type
  - SEC company_tickers.json maps ticker -> CIK
  - SEC XBRL companyconcept gives shares outstanding by CIK

Results are cached in the `securities` table so steady-state runs only touch
CUSIPs that are new or whose share count has gone stale. Market cap itself is
NOT stored here — it is recomputed at build time from the latest quarter's
implied price, which changes every filing season.
"""

import datetime as dt
import json
import sys
import time
import urllib.error
import urllib.request

import db
import sec
from config import COMPANY_SECURITY_TYPES
from screen import candidate_cusips

OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"
FIGI_BATCH = 10           # max jobs per keyless OpenFIGI request
FIGI_PAUSE = 3.0          # seconds between requests (keyless ~25 req/min)
SHARES_MAX_AGE_DAYS = 30  # refresh shares outstanding after this

SECURITIES_SCHEMA = """
CREATE TABLE IF NOT EXISTS securities (
    cusip             TEXT PRIMARY KEY,
    ticker            TEXT,
    name              TEXT,
    security_type     TEXT,
    is_company        INTEGER NOT NULL DEFAULT 0,
    cik               INTEGER,
    shares_out        INTEGER,
    shares_out_date   TEXT,
    sic_description   TEXT,
    exchange          TEXT,
    profile_fetched_at TEXT,
    mapped_at         TEXT,
    shares_fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS ticker_cik (
    ticker TEXT PRIMARY KEY,
    cik    INTEGER NOT NULL
);
"""


def ensure_schema(conn):
    conn.executescript(SECURITIES_SCHEMA)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(securities)")}
    for col, decl in (
        ("sic_description", "TEXT"),
        ("exchange", "TEXT"),
        ("profile_fetched_at", "TEXT"),
    ):
        if col not in cols:
            conn.execute(f"ALTER TABLE securities ADD COLUMN {col} {decl}")
    conn.commit()


def load_ticker_map(conn, max_age_days=7):
    """SEC ticker->CIK table, refreshed weekly."""
    last = db.get_meta(conn, "ticker_map_fetched")
    fresh = False
    if last:
        try:
            age = (dt.date.today() - dt.date.fromisoformat(last[:10])).days
            fresh = age < max_age_days
        except ValueError:
            fresh = False
    have = conn.execute("SELECT COUNT(*) c FROM ticker_cik").fetchone()["c"]
    if fresh and have:
        return {r["ticker"]: r["cik"] for r in conn.execute("SELECT ticker, cik FROM ticker_cik")}

    data = sec.fetch_json("https://www.sec.gov/files/company_tickers.json")
    rows = [(v["ticker"].upper(), int(v["cik_str"])) for v in data.values()]
    with conn:
        conn.execute("DELETE FROM ticker_cik")
        conn.executemany("INSERT OR REPLACE INTO ticker_cik(ticker, cik) VALUES(?,?)", rows)
        db.set_meta(conn, "ticker_map_fetched", dt.date.today().isoformat())
    return {t: c for t, c in rows}


def openfigi_map(cusips):
    """CUSIP -> {ticker, name, security_type} via OpenFIGI, batched + throttled."""
    out = {}
    for i in range(0, len(cusips), FIGI_BATCH):
        batch = cusips[i : i + FIGI_BATCH]
        payload = json.dumps(
            [{"idType": "ID_CUSIP", "idValue": c} for c in batch]
        ).encode()
        req = urllib.request.Request(
            OPENFIGI_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                results = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:  # rate limited — wait longer and retry once
                time.sleep(FIGI_PAUSE * 4)
                try:
                    with urllib.request.urlopen(req, timeout=60) as resp:
                        results = json.loads(resp.read().decode())
                except Exception as e2:  # noqa: BLE001
                    print(f"  ! OpenFIGI batch failed: {e2}", file=sys.stderr)
                    results = [{} for _ in batch]
            else:
                print(f"  ! OpenFIGI HTTP {e.code}", file=sys.stderr)
                results = [{} for _ in batch]
        except Exception as e:  # noqa: BLE001
            print(f"  ! OpenFIGI error: {e}", file=sys.stderr)
            results = [{} for _ in batch]

        for cusip, res in zip(batch, results):
            data = (res or {}).get("data") or []
            if not data:
                out[cusip] = None
                continue
            d0 = data[0]
            out[cusip] = {
                "ticker": (d0.get("ticker") or "").upper() or None,
                "name": d0.get("name"),
                "security_type": d0.get("securityType2") or d0.get("securityType"),
            }
        time.sleep(FIGI_PAUSE)
        print(f"  figi {min(i + FIGI_BATCH, len(cusips))}/{len(cusips)}", flush=True)
    return out


def shares_outstanding(cik):
    """Latest reported common shares outstanding for a CIK, or None."""
    url = (
        f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}"
        f"/dei/EntityCommonStockSharesOutstanding.json"
    )
    try:
        data = sec.fetch_json(url)
    except Exception:  # noqa: BLE001 - not every filer reports this concept
        return None, None
    best_val, best_end = None, None
    for pts in data.get("units", {}).values():
        for p in pts:
            end = p.get("end")
            if end and (best_end is None or end > best_end):
                best_end, best_val = end, p.get("val")
    return best_val, best_end


def company_profile(cik):
    """Industry (SIC) label and primary exchange for a CIK, from submissions."""
    try:
        data = sec.fetch_json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
    except Exception:  # noqa: BLE001
        return None, None
    sic = data.get("sicDescription") or None
    exchanges = [e for e in (data.get("exchanges") or []) if e]
    return sic, (exchanges[0] if exchanges else None)


def displayed_holdings_cusips(conn, top_n=40):
    """CUSIPs shown in the holdings/index tables, which also need descriptions.

    The union of each institution's top-N long positions plus anything whose
    issuer name looks like a market-index fund.
    """
    from config import INDEX_FUND_CUSIPS, INDEX_NAME_PATTERNS, INSTITUTIONS

    out = set(INDEX_FUND_CUSIPS)
    last = conn.execute("SELECT MAX(report_date) m FROM holdings").fetchone()["m"]
    for inst in INSTITUTIONS:
        rows = conn.execute(
            "SELECT cusip, SUM(value_usd) v FROM holdings"
            " WHERE institution=? AND report_date=? AND put_call=''"
            " GROUP BY cusip ORDER BY v DESC LIMIT ?",
            (inst["id"], last, top_n),
        )
        out.update(r["cusip"] for r in rows)
    like = " OR ".join("UPPER(issuer) LIKE ?" for _ in INDEX_NAME_PATTERNS)
    params = [f"%{p.upper()}%" for p in INDEX_NAME_PATTERNS]
    for r in conn.execute(
        f"SELECT DISTINCT cusip FROM holdings WHERE report_date=? AND put_call=''"
        f" AND ({like})",
        [last, *params],
    ):
        out.add(r["cusip"])
    return out


def main():
    conn = db.connect()
    ensure_schema(conn)

    from config import INSTITUTIONS
    from screen import candidates_by_institution

    _by_inst, accum_union = candidates_by_institution(conn, INSTITUTIONS)
    displayed = displayed_holdings_cusips(conn)
    # Everything that needs at least a mapping: accumulation candidates (per
    # institution) plus the securities shown in the ordinary holdings tables.
    cusips = sorted(accum_union | displayed)
    _agg, _periods, _series, meta = candidate_cusips(conn)
    print(
        f"to enrich: {len(cusips)} CUSIPs "
        f"({len(accum_union)} accumulation, {len(displayed)} displayed)"
    )

    existing = {
        r["cusip"]: r
        for r in conn.execute("SELECT * FROM securities")
    }
    today = dt.date.today()

    def stale(row):
        if row is None or row["mapped_at"] is None:
            return True
        if not row["is_company"]:
            return False  # funds never need a share count
        if row["shares_fetched_at"] is None:
            return True
        try:
            age = (today - dt.date.fromisoformat(row["shares_fetched_at"][:10])).days
        except ValueError:
            return True
        return age >= SHARES_MAX_AGE_DAYS

    need_map = [c for c in cusips if c not in existing]
    print(f"new CUSIPs needing OpenFIGI: {len(need_map)}")
    mapped = openfigi_map(need_map) if need_map else {}

    now = dt.datetime.now().isoformat(timespec="seconds")
    ticker_map = None

    for cusip in cusips:
        row = existing.get(cusip)
        info = mapped.get(cusip)
        if info is not None or row is None:
            info = info or {"ticker": None, "name": meta.get(cusip, {}).get("issuer"),
                            "security_type": None}
            is_company = int((info.get("security_type") or "") in COMPANY_SECURITY_TYPES)
            with conn:
                conn.execute(
                    "INSERT INTO securities(cusip, ticker, name, security_type,"
                    " is_company, mapped_at) VALUES(?,?,?,?,?,?)"
                    " ON CONFLICT(cusip) DO UPDATE SET ticker=excluded.ticker,"
                    " name=excluded.name, security_type=excluded.security_type,"
                    " is_company=excluded.is_company, mapped_at=excluded.mapped_at",
                    (cusip, info.get("ticker"), info.get("name"),
                     info.get("security_type"), is_company, now),
                )
            row = conn.execute("SELECT * FROM securities WHERE cusip=?", (cusip,)).fetchone()

        if not row["is_company"]:
            continue
        need_shares = stale(row)
        need_profile = row["profile_fetched_at"] is None or row["sic_description"] is None
        if not need_shares and not need_profile:
            continue
        if ticker_map is None:
            ticker_map = load_ticker_map(conn)
        cik = row["cik"] or (ticker_map.get(row["ticker"]) if row["ticker"] else None)
        if not cik:
            continue
        if need_shares:
            val, end = shares_outstanding(cik)
            with conn:
                conn.execute(
                    "UPDATE securities SET cik=?, shares_out=?, shares_out_date=?,"
                    " shares_fetched_at=? WHERE cusip=?",
                    (cik, val, end, now, cusip),
                )
        if need_profile:
            sic, exch = company_profile(cik)
            with conn:
                conn.execute(
                    "UPDATE securities SET cik=?, sic_description=?, exchange=?,"
                    " profile_fetched_at=? WHERE cusip=?",
                    (cik, sic, exch, now, cusip),
                )

    with conn:
        db.set_meta(conn, "enrich_last_run", now)
    priced = conn.execute(
        "SELECT COUNT(*) c FROM securities WHERE is_company=1 AND shares_out IS NOT NULL"
    ).fetchone()["c"]
    print(f"done: {priced} companies with a share count on file")


if __name__ == "__main__":
    main()
