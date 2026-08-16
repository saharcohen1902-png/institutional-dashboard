"""Ingest CFTC positioning by trader category.

The user wants the *institutional* line, so financial futures are taken from
the Traders-in-Financial-Futures (TFF) report, which breaks large traders into
"Asset Manager/Institutional" and "Leveraged Funds" (hedge funds). Physical
commodities are not in TFF; there the closest published category is the
Disaggregated report's "Managed Money" (money managers / hedge funds), and no
institutional breakdown exists — so that is stored and labelled as such.

Rows are keyed by (report_date, contract_code) so re-running is idempotent.
"""

import argparse
import datetime as dt
import json
import time
import urllib.error
import urllib.parse
import urllib.request

import db
from config import COT_CONTRACTS, COT_WEEKS

TFF_DATASET = "gpe5-46if"           # Traders in Financial Futures, futures-only
DISAGG_DATASET = "72hh-3qpy"        # Disaggregated, futures-only
COMMODITY_GROUP = "סחורות"

TFF_FIELDS = [
    "report_date_as_yyyy_mm_dd", "cftc_contract_market_code",
    "market_and_exchange_names", "open_interest_all",
    "asset_mgr_positions_long", "asset_mgr_positions_short",
    "change_in_asset_mgr_long", "change_in_asset_mgr_short",
    "lev_money_positions_long", "lev_money_positions_short",
    "change_in_lev_money_long", "change_in_lev_money_short",
]
DISAGG_FIELDS = [
    "report_date_as_yyyy_mm_dd", "cftc_contract_market_code",
    "market_and_exchange_names", "open_interest_all",
    "m_money_positions_long_all", "m_money_positions_short_all",
    "change_in_m_money_long_all", "change_in_m_money_short_all",
]


def _get(dataset, params):
    url = f"https://publicreporting.cftc.gov/resource/{dataset}.json?{urllib.parse.urlencode(params)}"
    # The CFTC endpoint occasionally stalls; retry with backoff rather than
    # failing the whole run on a single slow response.
    last = None
    for attempt in range(4):
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"CFTC request failed after retries: {last}")


def _int(v):
    if v in (None, "", "."):
        return None
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def _fetch(dataset, fields, codes, since):
    quoted = ",".join("'" + c.replace("'", "''") + "'" for c in codes)
    where = (
        f"cftc_contract_market_code in({quoted})"
        f" AND report_date_as_yyyy_mm_dd >= '{since}T00:00:00.000'"
    )
    rows, offset = [], 0
    while True:
        page = _get(dataset, {
            "$select": ",".join(fields),
            "$where": where,
            "$order": "report_date_as_yyyy_mm_dd DESC",
            "$limit": 5000,
            "$offset": offset,
        })
        rows.extend(page)
        if len(page) < 5000:
            break
        offset += 5000
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weeks", type=int, default=COT_WEEKS)
    args = ap.parse_args()

    since = (dt.date.today() - dt.timedelta(weeks=args.weeks + 1)).isoformat()
    fin_codes = [c[0] for c in COT_CONTRACTS if c[2] != COMMODITY_GROUP]
    com_codes = [c[0] for c in COT_CONTRACTS if c[2] == COMMODITY_GROUP]

    records = []  # (row_dict, primary_kind)
    if fin_codes:
        for r in _fetch(TFF_DATASET, TFF_FIELDS, fin_codes, since):
            records.append((
                {
                    "date": r["report_date_as_yyyy_mm_dd"][:10],
                    "code": r["cftc_contract_market_code"],
                    "market": r["market_and_exchange_names"],
                    "kind": "asset_manager",
                    "oi": _int(r.get("open_interest_all")),
                    "inst_long": _int(r.get("asset_mgr_positions_long")),
                    "inst_short": _int(r.get("asset_mgr_positions_short")),
                    "inst_long_chg": _int(r.get("change_in_asset_mgr_long")),
                    "inst_short_chg": _int(r.get("change_in_asset_mgr_short")),
                    "hf_long": _int(r.get("lev_money_positions_long")),
                    "hf_short": _int(r.get("lev_money_positions_short")),
                    "hf_long_chg": _int(r.get("change_in_lev_money_long")),
                    "hf_short_chg": _int(r.get("change_in_lev_money_short")),
                },
            ))
    if com_codes:
        for r in _fetch(DISAGG_DATASET, DISAGG_FIELDS, com_codes, since):
            records.append((
                {
                    "date": r["report_date_as_yyyy_mm_dd"][:10],
                    "code": r["cftc_contract_market_code"],
                    "market": r["market_and_exchange_names"],
                    "kind": "managed_money",
                    "oi": _int(r.get("open_interest_all")),
                    "inst_long": _int(r.get("m_money_positions_long_all")),
                    "inst_short": _int(r.get("m_money_positions_short_all")),
                    "inst_long_chg": _int(r.get("change_in_m_money_long_all")),
                    "inst_short_chg": _int(r.get("change_in_m_money_short_all")),
                    "hf_long": None, "hf_short": None,
                    "hf_long_chg": None, "hf_short_chg": None,
                },
            ))

    print(f"fetched {len(records)} COT rows since {since} "
          f"({len(fin_codes)} TFF financial, {len(com_codes)} disaggregated commodity)")

    now = dt.datetime.now().isoformat(timespec="seconds")
    conn = db.connect()
    with conn:
        conn.executemany(
            "INSERT INTO cot(report_date, contract_code, market_name, primary_kind,"
            " open_interest, inst_long, inst_short, inst_long_chg, inst_short_chg,"
            " hf_long, hf_short, hf_long_chg, hf_short_chg, fetched_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(report_date, contract_code) DO UPDATE SET"
            "   market_name=excluded.market_name, primary_kind=excluded.primary_kind,"
            "   open_interest=excluded.open_interest,"
            "   inst_long=excluded.inst_long, inst_short=excluded.inst_short,"
            "   inst_long_chg=excluded.inst_long_chg, inst_short_chg=excluded.inst_short_chg,"
            "   hf_long=excluded.hf_long, hf_short=excluded.hf_short,"
            "   hf_long_chg=excluded.hf_long_chg, hf_short_chg=excluded.hf_short_chg,"
            "   fetched_at=excluded.fetched_at",
            [
                (m["date"], m["code"], m["market"], m["kind"], m["oi"],
                 m["inst_long"], m["inst_short"], m["inst_long_chg"], m["inst_short_chg"],
                 m["hf_long"], m["hf_short"], m["hf_long_chg"], m["hf_short_chg"], now)
                for (m,) in records
            ],
        )
        cutoff = (dt.date.today() - dt.timedelta(weeks=args.weeks + 2)).isoformat()
        conn.execute("DELETE FROM cot WHERE report_date < ?", (cutoff,))
        db.set_meta(conn, "cot_last_run", now)
        latest = conn.execute("SELECT MAX(report_date) m FROM cot").fetchone()["m"]
        if latest:
            db.set_meta(conn, "cot_latest_report", latest)

    n_dates = conn.execute("SELECT COUNT(DISTINCT report_date) c FROM cot").fetchone()["c"]
    print(f"stored: {n_dates} weekly reports, latest {latest}")


if __name__ == "__main__":
    main()
