"""Render the SQLite contents into web/data.json for the dashboard."""

import datetime as dt
import json
import os

import db
from config import (
    COT_CONTRACTS,
    COT_WEEK_COLUMNS,
    INDEX_FUND_CUSIPS,
    INDEX_NAME_PATTERNS,
    INSTITUTIONS,
    MIDCAP_MAX_USD,
    MIDCAP_MIN_USD,
    QUARTERS,
)
from screen import (
    candidate_cusips,
    candidates_by_institution,
    is_accumulating,
    split_factors,
)

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
TOP_N = 25
TOP_INDEX = 15               # index instruments shown per institution
_INDEX_PATTERNS = [p.upper() for p in INDEX_NAME_PATTERNS]


def is_index_instrument(cusip, issuer):
    """A broad-market index ETF/fund, by verified CUSIP or issuer-name match."""
    if cusip in INDEX_FUND_CUSIPS:
        return True
    name = (issuer or "").upper()
    return any(p in name for p in _INDEX_PATTERNS)


def periods_for(conn, institution):
    rows = conn.execute(
        "SELECT DISTINCT report_date FROM holdings WHERE institution=?"
        " ORDER BY report_date DESC",
        (institution,),
    ).fetchall()
    return [r["report_date"] for r in rows]


def aggregate(conn, institution, period):
    """Positions for one institution/quarter, summed across its filers.

    A single 13F can list the same security several times (Berkshire splits
    rows by investment manager), and an institution can span several filing
    entities, so both collapse into one row per security here.
    """
    rows = conn.execute(
        """
        SELECT cusip,
               put_call,
               MAX(issuer)      AS issuer,
               MAX(title_class) AS title_class,
               MAX(share_type)  AS share_type,
               SUM(value_usd)   AS value_usd,
               SUM(shares)      AS shares
        FROM holdings
        WHERE institution=? AND report_date=?
        GROUP BY cusip, put_call
        """,
        (institution, period),
    ).fetchall()
    return {(r["cusip"], r["put_call"]): dict(r) for r in rows}


def pct_change(new, old):
    if old in (None, 0):
        return None
    return (new - old) / abs(old) * 100.0


def classify(cur, prev):
    if prev is None:
        return "new"
    if cur is None:
        return "exited"
    if prev["shares"] == 0:
        return "held"
    delta = (cur["shares"] - prev["shares"]) / abs(prev["shares"])
    if delta > 0.005:
        return "added"
    if delta < -0.005:
        return "trimmed"
    return "held"


MOVE_N = 10  # rows per new-buys / exits / adds / trims list


def _position_changes(snapshots, latest, prior, split=None):
    """New buys, exits, and the biggest adds/trims, latest vs prior quarter.

    Restricted to outright long equity (put_call empty). Adds/trims are ranked
    by the dollar size of the share change at the current price, so the list
    reflects the scale of the move, not just its percentage. Prior-quarter
    shares are split-adjusted so a stock split is not mistaken for a purchase.
    """
    split = split or {}
    cur = {k: v for k, v in snapshots[latest].items() if not k[1]}
    prev = {k: v for k, v in snapshots[prior].items() if not k[1]} if prior else {}

    def padj(cusip, shares):
        return shares * split.get(cusip, {}).get(prior, 1.0)

    new_buys, exits, adds, trims = [], [], [], []
    for key, v in cur.items():
        p = prev.get(key)
        price = (v["value_usd"] / v["shares"]) if v["shares"] else 0
        p_shares = padj(key[0], p["shares"]) if p else None
        if p is None:
            new_buys.append({"cusip": key[0], "issuer": v["issuer"],
                             "value": v["value_usd"], "shares": v["shares"]})
        elif p_shares and abs(v["shares"] - p_shares) > 0.005 * p_shares:
            d_shares = v["shares"] - p_shares
            entry = {"cusip": key[0], "issuer": v["issuer"],
                     "delta_shares": d_shares, "delta_value": d_shares * price,
                     "pct": pct_change(v["shares"], p_shares),
                     "value": v["value_usd"]}
            (adds if d_shares > 0 else trims).append(entry)
    for key, p in prev.items():
        if key not in cur:
            exits.append({"cusip": key[0], "issuer": p["issuer"],
                          "prev_value": p["value_usd"], "shares": p["shares"]})

    new_buys.sort(key=lambda r: r["value"], reverse=True)
    exits.sort(key=lambda r: r["prev_value"], reverse=True)
    adds.sort(key=lambda r: r["delta_value"], reverse=True)
    trims.sort(key=lambda r: r["delta_value"])  # most negative first
    return {
        "new_buys": new_buys[:MOVE_N],
        "exits": exits[:MOVE_N],
        "adds": adds[:MOVE_N],
        "trims": trims[:MOVE_N],
        "new_buys_total": len(new_buys),
        "exits_total": len(exits),
    }


def build_institution(conn, inst):
    periods = periods_for(conn, inst["id"])[:QUARTERS]
    if not periods:
        return None
    # Newest first. The oldest period is kept only as a comparison baseline.
    snapshots = {p: aggregate(conn, inst["id"], p) for p in periods}
    display_periods = periods[: QUARTERS - 1] if len(periods) >= QUARTERS else periods

    quarters = []
    for p in display_periods:
        snap = snapshots[p]
        total = sum(v["value_usd"] for v in snap.values())
        equities = {k: v for k, v in snap.items() if not v["put_call"]}
        puts = {k: v for k, v in snap.items() if v["put_call"] == "Put"}
        calls = {k: v for k, v in snap.items() if v["put_call"] == "Call"}
        quarters.append(
            {
                "period": p,
                "total_value": total,
                "positions": len(snap),
                "equity_value": sum(v["value_usd"] for v in equities.values()),
                "put_value": sum(v["value_usd"] for v in puts.values()),
                "call_value": sum(v["value_usd"] for v in calls.values()),
                "put_count": len(puts),
                "call_count": len(calls),
            }
        )

    latest = display_periods[0]
    latest_snap = snapshots[latest]
    latest_total = sum(v["value_usd"] for v in latest_snap.values()) or 1

    # Rank by the latest quarter, restricted to outright long positions so the
    # "largest holdings" table is not mixed with option overlays.
    ranked = sorted(
        (v for v in latest_snap.values() if not v["put_call"]),
        key=lambda r: r["value_usd"],
        reverse=True,
    )[:TOP_N]

    holdings = []
    for rank, cur in enumerate(ranked, start=1):
        key = (cur["cusip"], cur["put_call"])
        series = []
        for p in periods:  # includes the baseline quarter
            rec = snapshots[p].get(key)
            tot = sum(v["value_usd"] for v in snapshots[p].values()) or 1
            series.append(
                {
                    "period": p,
                    "value": rec["value_usd"] if rec else None,
                    "shares": rec["shares"] if rec else None,
                    "pct": (rec["value_usd"] / tot * 100.0) if rec else None,
                }
            )
        # series[i] is newer than series[i+1]; attach QoQ deltas.
        for i, pt in enumerate(series):
            prev = series[i + 1] if i + 1 < len(series) else None
            cur_rec = snapshots[pt["period"]].get(key)
            prev_rec = snapshots[prev["period"]].get(key) if prev else None
            pt["share_change_pct"] = (
                pct_change(cur_rec["shares"], prev_rec["shares"])
                if cur_rec and prev_rec
                else None
            )
            pt["value_change_pct"] = (
                pct_change(cur_rec["value_usd"], prev_rec["value_usd"])
                if cur_rec and prev_rec
                else None
            )
            pt["status"] = classify(cur_rec, prev_rec) if cur_rec or prev_rec else None

        holdings.append(
            {
                "rank": rank,
                "cusip": cur["cusip"],
                "issuer": cur["issuer"],
                "title_class": cur["title_class"],
                "share_type": cur["share_type"],
                "value": cur["value_usd"],
                "shares": cur["shares"],
                "pct": cur["value_usd"] / latest_total * 100.0,
                "series": series[: len(display_periods)],
            }
        )

    # Market-index instruments held, as their own list (ranked by latest value).
    index_holdings = []
    index_ranked = sorted(
        (
            v for (cu, pc), v in latest_snap.items()
            if not pc and is_index_instrument(cu, v["issuer"])
        ),
        key=lambda r: r["value_usd"],
        reverse=True,
    )[:TOP_INDEX]
    for cur in index_ranked:
        key = (cur["cusip"], "")
        series = []
        for p in display_periods:
            rec = snapshots[p].get(key)
            series.append(
                {
                    "period": p,
                    "value": rec["value_usd"] if rec else None,
                    "shares": rec["shares"] if rec else None,
                }
            )
        newest, prior = series[0], (series[1] if len(series) > 1 else None)
        share_chg = (
            pct_change(newest["shares"], prior["shares"])
            if prior and newest["shares"] is not None and prior["shares"] is not None
            else None
        )
        index_holdings.append(
            {
                "cusip": cur["cusip"],
                "issuer": INDEX_FUND_CUSIPS.get(cur["cusip"], cur["issuer"]),
                "title_class": cur["title_class"],
                "value": cur["value_usd"],
                "shares": cur["shares"],
                "pct": cur["value_usd"] / latest_total * 100.0,
                "share_change_pct": share_chg,
                "series": series,
            }
        )
    index_total = sum(h["value"] for h in index_holdings)

    filings = [
        dict(r)
        for r in conn.execute(
            "SELECT report_date, cik, form, amendment_type, filing_date, accession,"
            " row_count FROM filings WHERE institution=? AND superseded=0"
            " ORDER BY report_date DESC, filing_date DESC",
            (inst["id"],),
        )
    ]

    prior = display_periods[1] if len(display_periods) > 1 else None
    changes = (
        _position_changes(snapshots, latest, prior, split_factors(conn))
        if prior else None
    )

    return {
        "id": inst["id"],
        "name_he": inst["name_he"],
        "name_en": inst["name_en"],
        "kind": inst["kind"],
        "periods": display_periods,
        "quarters": quarters,
        "holdings": holdings,
        "index_holdings": index_holdings,
        "index_total": index_total,
        "index_pct": (index_total / latest_total * 100.0) if latest_total else 0,
        "changes": changes,
        "filings": filings,
    }


def build_cot(conn):
    labels = {c[0]: (c[1], c[2]) for c in COT_CONTRACTS}
    dates = [
        r["report_date"]
        for r in conn.execute(
            "SELECT DISTINCT report_date FROM cot ORDER BY report_date DESC"
        )
    ]
    contracts = []
    for code, (label_he, group) in labels.items():
        rows = conn.execute(
            "SELECT * FROM cot WHERE contract_code=? ORDER BY report_date DESC", (code,)
        ).fetchall()
        if not rows:
            continue
        series = []
        for r in rows:
            lng, sht = r["inst_long"] or 0, r["inst_short"] or 0
            hf_l, hf_s = r["hf_long"], r["hf_short"]
            series.append(
                {
                    "date": r["report_date"],
                    "open_interest": r["open_interest"] or 0,
                    # inst_* = the institutional (Asset Manager) category for
                    # financials, Managed Money for commodities.
                    "long": lng,
                    "short": sht,
                    "net": lng - sht,
                    "long_change": r["inst_long_chg"],
                    "short_change": r["inst_short_chg"],
                    "hf_net": (hf_l - hf_s) if hf_l is not None and hf_s is not None else None,
                    "hf_long": hf_l,
                    "hf_short": hf_s,
                }
            )
        latest = series[0]
        weeks = [
            {
                "date": s["date"],
                "long": s["long"],
                "short": s["short"],
                "long_change": s["long_change"],
                "short_change": s["short_change"],
                "hf_net": s["hf_net"],
            }
            for s in series[:COT_WEEK_COLUMNS]
        ]
        # Weekly change in the hedge-fund net, for the comparison column.
        hf_net_chg = None
        if len(series) > 1 and series[0]["hf_net"] is not None and series[1]["hf_net"] is not None:
            hf_net_chg = series[0]["hf_net"] - series[1]["hf_net"]
        contracts.append(
            {
                "code": code,
                "label_he": label_he,
                "group": group,
                "market_name": rows[0]["market_name"],
                "primary_kind": rows[0]["primary_kind"],
                "latest": latest,
                "hf_net": latest["hf_net"],
                "hf_net_change": hf_net_chg,
                "weeks": weeks,
                "series": series,
            }
        )
    return {"dates": dates, "contracts": contracts}


def _midcap_row(cusip, series, meta, srow, periods, display_periods):
    """One mid-cap table row, or None if it is not a priced $5-50B company."""
    if not srow or not srow["is_company"] or not srow["shares_out"]:
        return None
    last = periods[-1]
    rec = series[cusip][last]
    raw = rec.get("raw_shares") or rec["shares"]
    price = (rec["value"] / raw) if raw else 0
    market_cap = price * srow["shares_out"]
    if not (MIDCAP_MIN_USD <= market_cap <= MIDCAP_MAX_USD):
        return None

    pts = []
    for p in display_periods:
        r = series[cusip].get(p)
        pts.append({"period": p, "shares": r["shares"] if r else None,
                    "value": r["value"] if r else None})
    share_vals = [series[cusip][p]["shares"] or 0 for p in periods]
    last_qoq = pct_change(share_vals[-1], share_vals[-2])
    # Scale of the accumulation in dollars: net shares added across the whole
    # window valued at the current price — magnitude, not percentage growth.
    accum_shares = share_vals[-1] - share_vals[0]
    accum_value = accum_shares * price
    if last_qoq is None:
        ongoing = "held"
    elif last_qoq > 0.5:
        ongoing = "accumulating"
    elif last_qoq < -0.5:
        ongoing = "distributing"
    else:
        ongoing = "held"

    return {
        "cusip": cusip,
        "ticker": srow["ticker"],
        "name": srow["name"] or meta.get(cusip, {}).get("issuer"),
        "market_cap": market_cap,
        "price": price,
        "shares_out": srow["shares_out"],
        "year_growth": (share_vals[-1] / share_vals[0]) if share_vals[0] else None,
        "held_value": rec["value"],
        "accum_shares": accum_shares,
        "accum_value": accum_value,
        "ongoing": ongoing,
        "last_qoq": last_qoq,
        "series": pts,
    }


def build_midcap(conn):
    """Per-institution mid-cap ($5-50B) accumulation tables.

    Each institution is screened on its own holdings, so the names it has been
    accumulating are attributed to it rather than to a seven-way aggregate.
    Market cap = latest quarter's implied share price x shares outstanding
    (from the enrichment cache). Funds and names without a share count are
    skipped; each institution keeps its MIDCAP_LIMIT most massive accumulators.
    """
    import enrich
    from config import MIDCAP_LIMIT
    enrich.ensure_schema(conn)  # so the first build works before enrichment runs

    by_inst, union = candidates_by_institution(conn, INSTITUTIONS)
    if not union:
        return {"periods": [], "institutions": [], "priced": 0}

    sec_rows = {
        r["cusip"]: r
        for r in conn.execute(
            "SELECT * FROM securities WHERE cusip IN (%s)"
            % ",".join("?" * len(union)),
            sorted(union),
        )
    }

    out_insts = []
    total_priced = 0
    for inst in INSTITUTIONS:
        data = by_inst[inst["id"]]
        periods, series, meta = data["periods"], data["series"], data["meta"]
        if len(periods) < 2:
            continue
        display_periods = periods[-QUARTERS + 1 :] if len(periods) >= QUARTERS else periods
        rows = []
        for cusip in data["cusips"]:
            row = _midcap_row(cusip, series, meta, sec_rows.get(cusip),
                              periods, display_periods)
            if row:
                rows.append(row)
        rows.sort(key=lambda r: r["accum_value"] or 0, reverse=True)
        matched = len(rows)
        rows = rows[:MIDCAP_LIMIT]
        total_priced += len(rows)
        out_insts.append(
            {
                "id": inst["id"],
                "name_he": inst["name_he"],
                "name_en": inst["name_en"],
                "kind": inst["kind"],
                "periods": display_periods,
                "rows": rows,
                "matched": matched,
                "candidates": len(data["cusips"]),
            }
        )
    return {"institutions": out_insts, "priced": total_priced}


def build_consensus(conn, institutions):
    """Cross-institution conviction: where the most institutions move together.

    For each equity, counts how many of the tracked institutions increased vs
    decreased their share count last quarter (a new position counts as an
    increase, a full exit as a decrease). Ranks by the number of institutions
    moving the same way, then by the aggregate dollars traded at the current
    price. This surfaces crowd/consensus, which a single institution's top
    holdings cannot.
    """
    periods = [
        r["report_date"]
        for r in conn.execute(
            "SELECT DISTINCT report_date FROM holdings ORDER BY report_date DESC"
        )
    ]
    if len(periods) < 2:
        return {"buys": [], "sells": [], "latest": None, "prior": None,
                "n_institutions": len(institutions)}
    latest, prior = periods[0], periods[1]

    def snap(period):
        d = {}
        for r in conn.execute(
            "SELECT institution, cusip, MAX(issuer) issuer, SUM(shares) sh,"
            " SUM(value_usd) val FROM holdings WHERE report_date=? AND put_call=''"
            " AND share_type='SH' GROUP BY institution, cusip",
            (period,),
        ):
            d[(r["institution"], r["cusip"])] = r
        return d

    cur, prev = snap(latest), snap(prior)
    cusips = {k[1] for k in cur} | {k[1] for k in prev}
    price = _latest_prices(conn, list(cusips)) if cusips else {}
    split = split_factors(conn)

    agg = {}
    for cusip in cusips:
        adders = reducers = 0
        buy_val = sell_val = 0.0
        buy_sh = sell_sh = 0
        issuer = ""
        px = price.get(cusip, 0)
        pfac = split.get(cusip, {}).get(prior, 1.0)  # split-adjust prior shares
        for inst in institutions:
            iid = inst["id"]
            c = cur.get((iid, cusip))
            p = prev.get((iid, cusip))
            cs = c["sh"] if c else 0
            ps = (p["sh"] * pfac) if p else 0
            if c:
                issuer = c["issuer"]
            elif p and not issuer:
                issuer = p["issuer"]
            if cs > ps:
                adders += 1
                buy_val += (cs - ps) * px
                buy_sh += (cs - ps)
            elif cs < ps:
                reducers += 1
                sell_val += (ps - cs) * px
                sell_sh += (ps - cs)
        agg[cusip] = {
            "cusip": cusip, "issuer": issuer,
            "adders": adders, "reducers": reducers,
            "buy_value": buy_val, "sell_value": sell_val,
            "buy_shares": buy_sh, "sell_shares": sell_sh,
        }

    buys = [a for a in agg.values() if a["adders"] >= 2]
    buys.sort(key=lambda a: (a["adders"], a["buy_value"]), reverse=True)
    sells = [a for a in agg.values() if a["reducers"] >= 2]
    sells.sort(key=lambda a: (a["reducers"], a["sell_value"]), reverse=True)
    return {
        "latest": latest, "prior": prior,
        "n_institutions": len(institutions),
        "buys": buys[:20],
        "sells": sells[:20],
    }


AI_ACCUM_GROWTH = 1.15  # aggregate shares +15% over the year to flag as accumulated


def build_ai(conn):
    """AI value-chain holdings: the institutions' aggregate stake per layer.

    For each of the eight layers, the tracked names actually held are ranked by
    latest value and the top five kept. Share counts are split-adjusted so the
    year-over-year trajectory reflects real accumulation, not corporate actions.
    """
    import collections
    from config import AI_LAYERS, INSTITUTIONS

    # Short column labels for the per-institution breakdown.
    SHORT = {
        "blackrock": "בלאקרוק", "vanguard": "ונגארד", "statestreet": "סטייט סטריט",
        "jpmorgan": "ג'יי.פי מורגן", "bofa": "בנק אוף אמריקה",
        "berkshire": "ברקשייר", "metlife": "מטלייף",
    }
    inst_cols = [{"id": i["id"], "short": SHORT.get(i["id"], i["name_he"])}
                 for i in INSTITUTIONS]

    all_cusips = [co[0] for layer in AI_LAYERS for co in layer["companies"]]
    periods = [
        r["report_date"]
        for r in conn.execute(
            "SELECT DISTINCT report_date FROM holdings ORDER BY report_date"
        )
    ]
    # Newest quarter first, so display_periods[0] is the latest — matching the
    # rest of the dashboard and making the per-institution column current.
    window = periods[-QUARTERS + 1:] if len(periods) >= QUARTERS else periods
    display_periods = list(reversed(window))
    factors = split_factors(conn)

    # cusip -> {period: {shares(adj), value, ni}}
    agg = collections.defaultdict(dict)
    for r in conn.execute(
        "SELECT report_date, cusip, SUM(shares) sh, SUM(value_usd) val,"
        " COUNT(DISTINCT institution) ni FROM holdings"
        " WHERE put_call='' AND share_type='SH' AND cusip IN (%s)"
        " GROUP BY report_date, cusip" % ",".join("?" * len(all_cusips)),
        all_cusips,
    ):
        fac = factors.get(r["cusip"], {}).get(r["report_date"], 1.0)
        agg[r["cusip"]][r["report_date"]] = {
            "shares": (r["sh"] or 0) * fac, "value": r["val"] or 0, "ni": r["ni"]}

    # Per-institution, per-quarter shares (split-adjusted) and value, so each
    # institution's actual position and its quarter-to-quarter change show.
    ph = ",".join("?" * len(all_cusips))
    qmarks = ",".join("?" * len(display_periods))
    detail = collections.defaultdict(lambda: collections.defaultdict(dict))
    for r in conn.execute(
        "SELECT report_date, cusip, institution, SUM(shares) sh, SUM(value_usd) val"
        f" FROM holdings WHERE put_call='' AND share_type='SH' AND cusip IN ({ph})"
        f" AND report_date IN ({qmarks}) GROUP BY report_date, cusip, institution",
        [*all_cusips, *display_periods],
    ):
        fac = factors.get(r["cusip"], {}).get(r["report_date"], 1.0)
        detail[r["cusip"]][r["institution"]][r["report_date"]] = {
            "shares": (r["sh"] or 0) * fac, "value": r["val"] or 0}

    def inst_detail(cusip):
        """Per-institution series (newest-first) with shares, value, QoQ change."""
        out = []
        for col in inst_cols:
            per = detail.get(cusip, {}).get(col["id"])
            if not per:
                continue
            series = []
            for i, p in enumerate(display_periods):
                rec = per.get(p)
                older = per.get(display_periods[i + 1]) if i + 1 < len(display_periods) else None
                series.append({
                    "period": p,
                    "shares": rec["shares"] if rec else None,
                    "value": rec["value"] if rec else None,
                    "chg": (pct_change(rec["shares"], older["shares"])
                            if rec and older else None),
                })
            latest_val = series[0]["value"] if series[0]["value"] else 0
            out.append({"id": col["id"], "short": col["short"],
                        "latest_value": latest_val, "series": series})
        out.sort(key=lambda x: x["latest_value"], reverse=True)
        return out

    latest_period = display_periods[0] if display_periods else None
    by_inst = collections.defaultdict(dict)
    if latest_period:
        for cusip in all_cusips:
            for col in inst_cols:
                per = detail.get(cusip, {}).get(col["id"])
                if per and latest_period in per:
                    by_inst[cusip][col["id"]] = per[latest_period]["value"]

    out_layers = []
    for layer in AI_LAYERS:
        rows = []
        for cusip, name, ticker in layer["companies"]:
            per = agg.get(cusip)
            if not per or display_periods[0] not in per:
                continue  # not held in the latest quarter
            series = []
            for i, p in enumerate(display_periods):
                rec = per.get(p)
                prevp = display_periods[i + 1] if i + 1 < len(display_periods) else None
                prev = per.get(prevp) if prevp else None
                series.append({
                    "period": p,
                    "value": rec["value"] if rec else None,
                    "shares": rec["shares"] if rec else None,
                    "share_change_pct": (
                        pct_change(rec["shares"], prev["shares"])
                        if rec and prev else None),
                })
            latest = series[0]
            oldest = next((s for s in reversed(series) if s["shares"]), None)
            growth = (latest["shares"] / oldest["shares"]
                      if oldest and oldest["shares"] else None)
            qoq = latest["share_change_pct"]
            status = ("accumulating" if qoq and qoq > 0.5
                      else "distributing" if qoq and qoq < -0.5 else "held")
            rows.append({
                "cusip": cusip, "name": name, "ticker": ticker,
                "value": latest["value"], "ni": per[display_periods[0]]["ni"],
                "year_growth": growth, "last_qoq": qoq, "status": status,
                "series": series,
                "by_inst": by_inst.get(cusip, {}),
                "detail": inst_detail(cusip),
            })
        # Each institution's OWN top-five in the layer (ranked by that
        # institution's held value) — these genuinely differ, so the displayed
        # set is the union of them, not one aggregate top-five. Plus any name
        # being accumulated (aggregate shares up >=15% YoY, split-adjusted).
        held = [r["cusip"] for r in rows]
        inst_top = {}
        for col in inst_cols:
            ranked = sorted(
                ((cu, by_inst.get(cu, {}).get(col["id"], 0)) for cu in held),
                key=lambda x: x[1], reverse=True)
            inst_top[col["id"]] = {cu for cu, v in ranked[:5] if v}
        union = set().union(*inst_top.values()) if inst_top else set()

        rows.sort(key=lambda r: r["value"] or 0, reverse=True)
        chosen = [r for r in rows if r["cusip"] in union]
        for r in chosen:
            r["reason"] = "top"
        shown_ids = {r["cusip"] for r in chosen}
        for r in rows:
            if r["cusip"] in shown_ids:
                continue
            if (r.get("year_growth") or 0) >= AI_ACCUM_GROWTH:
                r["reason"] = "accum"
                chosen.append(r)
                shown_ids.add(r["cusip"])
        # Annotate for whom each name is a top-five holding.
        for r in chosen:
            r["top_for"] = [col["short"] for col in inst_cols
                            if r["cusip"] in inst_top.get(col["id"], set())]
            for it in r["detail"]:
                it["is_top"] = r["cusip"] in inst_top.get(it["id"], set())
        out_layers.append({
            "n": layer["n"], "he": layer["he"], "buying": layer["buying"],
            "rows": chosen,
        })

    # Institution-first view: each institution, its own top-five per layer, with
    # that institution's actual shares/value/quarterly change.
    name_by_id = {i["id"]: i["name_he"] for i in INSTITUTIONS}

    def inst_series(cusip, inst_id):
        per = detail.get(cusip, {}).get(inst_id)
        if not per:
            return None
        s = []
        for i, p in enumerate(display_periods):
            rec = per.get(p)
            older = per.get(display_periods[i + 1]) if i + 1 < len(display_periods) else None
            s.append({
                "period": p,
                "shares": rec["shares"] if rec else None,
                "value": rec["value"] if rec else None,
                "chg": (pct_change(rec["shares"], older["shares"])
                        if rec and older else None),
            })
        return s

    def inst_growth(per):
        """This institution's split-adjusted share growth over the window."""
        latest_sh = per.get(display_periods[0], {}).get("shares")
        oldest_sh = None
        for p in reversed(display_periods):
            sh = per.get(p, {}).get("shares")
            if sh:
                oldest_sh = sh
                break
        return (latest_sh / oldest_sh) if latest_sh and oldest_sh else None

    by_institution = []
    for col in inst_cols:
        layers_out = []
        for layer in AI_LAYERS:
            comps = []
            for cusip, name, ticker in layer["companies"]:
                per = detail.get(cusip, {}).get(col["id"])
                if not per or display_periods[0] not in per:
                    continue
                comps.append((per[display_periods[0]]["value"], cusip, name, ticker, per))
            comps.sort(key=lambda x: x[0], reverse=True)
            # This institution's five largest in the layer, plus any name it is
            # itself accumulating (its own shares up >=15% YoY) beyond the five.
            chosen = list(comps[:5])
            chosen_ids = {c[1] for c in chosen}
            reasons = {c[1]: "top" for c in chosen}
            for entry in comps[5:]:
                g = inst_growth(entry[4])
                if g and g >= AI_ACCUM_GROWTH and entry[1] not in chosen_ids:
                    chosen.append(entry)
                    reasons[entry[1]] = "accum"
            rows = [{
                "cusip": cu, "name": nm, "ticker": tk, "value": lv,
                "reason": reasons[cu], "series": inst_series(cu, col["id"]),
            } for lv, cu, nm, tk, _per in chosen]
            if rows:
                layers_out.append({
                    "n": layer["n"], "he": layer["he"],
                    "buying": layer["buying"], "rows": rows})
        by_institution.append({
            "id": col["id"], "name_he": name_by_id.get(col["id"], col["short"]),
            "layers": layers_out})

    return {"periods": display_periods, "institutions": inst_cols,
            "by_institution": by_institution, "layers": out_layers}


def classify_sector(sic):
    from config import SECTOR_PATTERNS
    if not sic:
        return None
    low = sic.lower()
    for sub, sector in SECTOR_PATTERNS:
        if sub in low:
            return sector
    return "other"


def build_sectors(conn, institutions):
    """Sector weights and quarter-over-quarter rotation, by SIC.

    Rotation is measured as the change in each sector's *weight* (share of the
    classified book), which isolates the reallocation from overall market
    drift. Coverage is reported because SIC is only known for the enriched
    securities (the large positions), not the entire long tail.
    """
    import collections
    from config import SECTOR_LABELS

    sic_map = {
        r["cusip"]: r["sic_description"]
        for r in conn.execute(
            "SELECT cusip, sic_description FROM securities WHERE sic_description IS NOT NULL"
        )
    }
    periods = [
        r["report_date"]
        for r in conn.execute(
            "SELECT DISTINCT report_date FROM holdings ORDER BY report_date DESC"
        )
    ]
    if len(periods) < 2:
        return None
    latest, prior = periods[0], periods[1]

    def sector_totals(where_inst=None):
        """Return {period: {sector: value}}, plus classified/total per period."""
        by = {latest: collections.defaultdict(float), prior: collections.defaultdict(float)}
        classified = {latest: 0.0, prior: 0.0}
        total = {latest: 0.0, prior: 0.0}
        q = ("SELECT report_date, cusip, SUM(value_usd) v FROM holdings"
             " WHERE put_call='' AND report_date IN (?,?)")
        params = [latest, prior]
        if where_inst:
            q += " AND institution=?"
            params.append(where_inst)
        q += " GROUP BY report_date, cusip"
        for r in conn.execute(q, params):
            p = r["report_date"]
            total[p] += r["v"]
            sec = classify_sector(sic_map.get(r["cusip"]))
            if sec:
                by[p][sec] += r["v"]
                classified[p] += r["v"]
        return by, classified, total

    def rows_from(by, classified):
        rows = []
        for sector, label in SECTOR_LABELS.items():
            v = by[latest].get(sector, 0.0)
            vp = by[prior].get(sector, 0.0)
            if v == 0 and vp == 0:
                continue
            w = (v / classified[latest] * 100.0) if classified[latest] else 0
            wp = (vp / classified[prior] * 100.0) if classified[prior] else 0
            rows.append({
                "sector": sector, "label": label,
                "value": v, "value_prior": vp,
                "weight": w, "weight_prior": wp,
                "weight_change": w - wp,
                "value_change_pct": pct_change(v, vp),
            })
        rows.sort(key=lambda r: r["value"], reverse=True)
        return rows

    by, classified, total = sector_totals()
    aggregate = rows_from(by, classified)
    cov = {
        "latest": (classified[latest] / total[latest] * 100.0) if total[latest] else 0,
        "prior": (classified[prior] / total[prior] * 100.0) if total[prior] else 0,
    }

    inst_rows = []
    for inst in institutions:
        iby, iclass, itot = sector_totals(inst["id"])
        inst_rows.append({
            "id": inst["id"], "name_he": inst["name_he"],
            "coverage": (iclass[latest] / itot[latest] * 100.0) if itot[latest] else 0,
            "rows": rows_from(iby, iclass),
        })

    return {
        "latest": latest, "prior": prior,
        "coverage": cov,
        "aggregate": aggregate,
        "institutions": inst_rows,
    }


def _latest_prices(conn, cusips):
    """Aggregate implied price per CUSIP in the latest quarter (value/shares)."""
    if not cusips:
        return {}
    last = conn.execute("SELECT MAX(report_date) m FROM holdings").fetchone()["m"]
    out = {}
    for r in conn.execute(
        "SELECT cusip, SUM(value_usd) v, SUM(shares) s FROM holdings"
        " WHERE report_date=? AND put_call='' AND share_type='SH'"
        " AND cusip IN (%s) GROUP BY cusip" % ",".join("?" * len(cusips)),
        [last, *cusips],
    ):
        if r["s"]:
            out[r["cusip"]] = r["v"] / r["s"]
    return out


def build_descriptions(conn, cusips):
    """cusip -> short factual blurb for the click-through panel.

    Composed only from data we hold (SEC industry, exchange, security type,
    derived market cap, tracked index) so nothing is invented.
    """
    if not cusips:
        return {}
    prices = _latest_prices(conn, cusips)
    sec_rows = {
        r["cusip"]: r
        for r in conn.execute(
            "SELECT * FROM securities WHERE cusip IN (%s)"
            % ",".join("?" * len(cusips)),
            sorted(cusips),
        )
    }
    out = {}
    for cusip in cusips:
        s = sec_rows.get(cusip)
        idx_label = INDEX_FUND_CUSIPS.get(cusip)
        name = (s["name"] if s and s["name"] else None)
        ticker = s["ticker"] if s else None
        stype = (s["security_type"] if s else None) or ""
        is_index = idx_label is not None or (
            name and any(p in name.upper() for p in _INDEX_PATTERNS)
        )
        parts = []
        title = idx_label or name or (ticker or cusip)
        if is_index:
            parts.append(f"{title} — קרן סל / קרן מדד.")
            parts.append("עוקבת אחר מדד שוק רחב ומעניקה חשיפה מפוזרת במקום מניה בודדת.")
        else:
            head = title
            if ticker:
                head += f" ({ticker})"
            sic = s["sic_description"] if s and s["sic_description"] else None
            if sic:
                parts.append(f"{head} — חברה בתחום {sic}.")
            else:
                parts.append(f"{head} — {stype or 'נייר ערך'}.")
            price = prices.get(cusip)
            if s and s["shares_out"] and price:
                mcap = price * s["shares_out"]
                parts.append(f"שווי שוק מוערך: {_money_he(mcap)}.")
            if s and s["exchange"]:
                parts.append(f"נסחרת בבורסת {s['exchange']}.")
        out[cusip] = {
            "title": title,
            "ticker": ticker,
            "is_index": bool(is_index),
            "blurb": " ".join(parts),
        }
    return out


def _money_he(v):
    a = abs(v)
    if a >= 1e12:
        return f"${v/1e12:.2f} טריליון"
    if a >= 1e9:
        return f"${v/1e9:.1f} מיליארד"
    if a >= 1e6:
        return f"${v/1e6:.0f} מיליון"
    return f"${v:,.0f}"


# Representative COT contracts the market read leans on, by group.
_READ_CONTRACTS = {
    "sp500": "13874A", "nasdaq": "20974+", "russell": "239742", "dow": "12460+",
    "vix": "1170E1", "ust10": "043602", "gold": "088691", "usd": "098662",
    "btc": "133741",
}


def _cot_net_at(conn, code, weeks_back):
    """Institutional (Asset Manager) net position `weeks_back` reports ago."""
    rows = conn.execute(
        "SELECT inst_long, inst_short FROM cot WHERE contract_code=?"
        " ORDER BY report_date DESC LIMIT ?",
        (code, weeks_back + 1),
    ).fetchall()
    if len(rows) <= weeks_back:
        return None
    r = rows[weeks_back]
    return (r["inst_long"] or 0) - (r["inst_short"] or 0)


# Bump when the market-read logic changes, to force a regenerate past the
# fortnight cache.
READ_VERSION = 4


def _institutional_signals(institutions, midcap):
    """Quarter-over-quarter 13F flow signals across all institutions.

    Everything here comes from the same tables the dashboard already shows,
    so the market read reflects the whole interface — not just the futures
    positioning.
    """
    put_now = put_prev = call_now = call_prev = 0
    pos_now = pos_prev = 0
    idx_now = idx_prev = 0
    for inst in institutions:
        qs = inst.get("quarters") or []
        if qs:
            put_now += qs[0]["put_value"]; call_now += qs[0]["call_value"]
            pos_now += qs[0]["positions"]
        if len(qs) > 1:
            put_prev += qs[1]["put_value"]; call_prev += qs[1]["call_value"]
            pos_prev += qs[1]["positions"]
        for h in inst.get("index_holdings", []):
            ser = h.get("series") or []
            if ser and ser[0]["value"] is not None:
                idx_now += ser[0]["value"]
            if len(ser) > 1 and ser[1]["value"] is not None:
                idx_prev += ser[1]["value"]

    # Mid-cap accumulation breadth across every institution's screen.
    acc = dist = 0
    for mi in (midcap or {}).get("institutions", []):
        for r in mi["rows"]:
            if r["ongoing"] == "accumulating":
                acc += 1
            elif r["ongoing"] == "distributing":
                dist += 1

    pc_now = (put_now / call_now) if call_now else None
    pc_prev = (put_prev / call_prev) if call_prev else None
    return {
        "put_call_now": pc_now,
        "put_call_prev": pc_prev,
        "put_call_dir": (None if pc_now is None or pc_prev is None
                         else pc_now - pc_prev),
        "positions_now": pos_now,
        "positions_change": pos_now - pos_prev if pos_prev else None,
        "index_now": idx_now,
        "index_change": idx_now - idx_prev if idx_prev else None,
        "accumulating": acc,
        "distributing": dist,
    }


def build_market_read(conn, institutions=None, midcap=None):
    """A data-derived, bi-weekly read of the whole dashboard.

    Blends the futures positioning (COT) with the institutional 13F flows the
    rest of the interface shows — options hedging, breadth, passive/index
    rotation, and mid-cap accumulation breadth — into one narrative.
    Regenerated once the latest COT report is 14+ days newer than the stored
    read (or the logic version changed). Informational only, not advice.
    """
    latest = conn.execute("SELECT MAX(report_date) m FROM cot").fetchone()["m"]
    if not latest:
        return None
    stored = db.get_meta(conn, "market_read_json")
    if stored:
        try:
            prev = json.loads(stored)
            d0 = dt.date.fromisoformat(prev["based_on"])
            if prev.get("v") == READ_VERSION and (
                dt.date.fromisoformat(latest) - d0
            ).days < 14:
                return prev  # still inside the current fortnight
        except Exception:  # noqa: BLE001
            pass

    def net(code):
        return _cot_net_at(conn, code, 0)

    def chg(code):
        now, then = _cot_net_at(conn, code, 0), _cot_net_at(conn, code, 2)
        return None if now is None or then is None else now - then

    eq_now = sum(v for v in (net(_READ_CONTRACTS[k]) for k in
                 ("sp500", "nasdaq", "russell", "dow")) if v is not None)
    eq_chg = sum(v for v in (chg(_READ_CONTRACTS[k]) for k in
                 ("sp500", "nasdaq", "russell", "dow")) if v is not None)

    def word(v, up, down, flat="נותר יציב"):
        if v is None:
            return flat
        if v > 0:
            return up
        if v < 0:
            return down
        return flat

    details = []
    # Equities — TFF Asset Manager / Institutional category.
    eq_dir = word(eq_chg, "הגדילו חשיפת לונג", "צמצמו לונג / הגדילו שורט")
    stance = ("נטו-לונג" if eq_now > 0 else "נטו-שורט")
    details.append(
        f"מניות (מוסדיים/Asset Managers): {eq_dir} במדדי המניות המובילים בשבועיים "
        f"האחרונים (שינוי מצרפי של {eq_chg:+,} חוזים), ונמצאים כעת בעמדה {stance} כוללת."
    )
    # Volatility
    vix_c = chg(_READ_CONTRACTS["vix"])
    details.append(
        "תנודתיות (VIX, מוסדיים): " + word(
            vix_c,
            "עלייה בפוזיציות הלונג — סימן להתגברות גידור/חשש",
            "ירידה בלונג על ה-VIX — פחות ביקוש להגנה",
        ) + f" ({vix_c:+,} חוזים)." if vix_c is not None else "תנודתיות: אין נתון."
    )
    # Rates
    r_c = chg(_READ_CONTRACTS["ust10"])
    details.append(
        "אג\"ח 10 שנים: " + word(
            r_c,
            "הגדלת לונג — הימור על ירידת תשואות/ריכוך מוניטרי",
            "הגדלת שורט — ציפייה לתשואות גבוהות יותר",
        ) + f" ({r_c:+,})." if r_c is not None else "אג\"ח: אין נתון."
    )
    # Gold (managed money — commodities have no institutional split) + Dollar.
    g_c, u_c = chg(_READ_CONTRACTS["gold"]), chg(_READ_CONTRACTS["usd"])
    details.append(
        "מקלטים בטוחים: זהב (מנהלי כספים) " + word(g_c, "בביקוש (לונג עולה)", "בהיחלשות לונג")
        + ", דולר (מוסדיים) " + word(u_c, "מתחזק בפוזישן", "נחלש בפוזישן") + "."
    )
    # Crypto
    b_c = chg(_READ_CONTRACTS["btc"])
    if b_c is not None:
        details.append(
            "ביטקוין: " + word(b_c, "הגדלת לונג — תיאבון סיכון ער",
                               "צמצום לונג — ריסון בסיכון") + f" ({b_c:+,})."
        )

    # --- 13F institutional flows (the rest of the dashboard) ---------------
    sig = _institutional_signals(institutions or [], midcap or {})
    inst_score = 0

    pc_dir = sig["put_call_dir"]
    if sig["put_call_now"] is not None:
        hedge = word(pc_dir, "עולה — המוסדות מגדילים הגנות (PUT) מול CALL",
                     "יורד — פחות הגנות, הטיה שורית יותר", "יציב")
        details.append(
            f"מוסדות — גידור (13F): יחס PUT/CALL {sig['put_call_now']:.2f}, {hedge}."
        )
        if pc_dir is not None:
            inst_score += -1 if pc_dir > 0 else 1

    if sig["positions_change"] is not None:
        breadth = word(sig["positions_change"],
                       "המוסדות הרחיבו מספר פוזיציות — רוחב שוק משתפר",
                       "המוסדות צמצמו פוזיציות — רוחב שוק מצטמצם")
        details.append(f"מוסדות — רוחב (13F): {breadth} ({sig['positions_change']:+,}).")
        inst_score += 1 if sig["positions_change"] > 0 else -1

    if sig["index_change"] is not None:
        rot = word(sig["index_change"],
                   "הגדלת חשיפה למדדי שוק רחבים (רוטציה לפאסיבי/סיכון)",
                   "הקטנת חשיפה למדדים רחבים")
        details.append(f"מוסדות — מדדים (13F): {rot} ({_money_he(abs(sig['index_change']))}).")
        inst_score += 1 if sig["index_change"] > 0 else -1

    if sig["accumulating"] or sig["distributing"]:
        details.append(
            f"איסוף שווי-בינוני (13F): {sig['accumulating']} חברות באיסוף פעיל "
            f"מול {sig['distributing']} בדילול — "
            + ("רוחב איסוף חיובי." if sig["accumulating"] >= sig["distributing"]
               else "הטיה לדילול.")
        )
        inst_score += 1 if sig["accumulating"] >= sig["distributing"] else -1

    # --- Composite of futures + institutional signals ---------------------
    cot_score = 0
    if eq_chg:
        cot_score += 1 if eq_chg > 0 else -1
    if vix_c is not None:
        cot_score += -1 if vix_c > 0 else 1
    total = cot_score + inst_score

    if total >= 2:
        headline = "הטיה לכיוון סיכון (Risk-On)"
        outlook = ("גם הפוזישן בחוזים וגם זרימות ה-13F נוטים לתיאבון סיכון — "
                   "התרחבות לונג/רוחב מול גידור ממותן; מומנטום כלפי מעלה כל עוד "
                   "התמונה נמשכת.")
    elif total <= -2:
        headline = "מעבר להגנתיות (Risk-Off)"
        outlook = ("שילוב של הצטמצמות לונג בחוזים והגברת גידור/צמצום רוחב אצל "
                   "המוסדות מרמז על זהירות; פוטנציאל לתיקון או דשדוש כלפי מטה.")
    else:
        headline = "תמונה מעורבת"
        outlook = ("האותות מפוצלים — חלק מהאפיקים בחוזים ובזרימות המוסדיות "
                   "מתחזקים בסיכון ואחרים בהגנה; סביבה תלוית-נתונים.")

    result = {
        "v": READ_VERSION,
        "based_on": latest,
        "headline": headline,
        "outlook": outlook,
        "details": details,
        "cot_score": cot_score,
        "inst_score": inst_score,
        "total_score": total,
    }
    with conn:
        db.set_meta(conn, "market_read_json", json.dumps(result, ensure_ascii=False))
    return result


def main():
    conn = db.connect()
    institutions = [x for x in (build_institution(conn, i) for i in INSTITUTIONS) if x]
    cot = build_cot(conn)
    midcap = build_midcap(conn)
    consensus = build_consensus(conn, INSTITUTIONS)
    sectors = build_sectors(conn, INSTITUTIONS)
    ai = build_ai(conn)

    # Descriptions only for securities actually shown anywhere.
    shown = set()
    for inst in institutions:
        shown.update(h["cusip"] for h in inst["holdings"])
        shown.update(h["cusip"] for h in inst.get("index_holdings", []))
        ch = inst.get("changes") or {}
        for lst in ("new_buys", "exits", "adds", "trims"):
            shown.update(r["cusip"] for r in ch.get(lst, []))
    for mi in midcap.get("institutions", []):
        shown.update(r["cusip"] for r in mi["rows"])
    for side in ("buys", "sells"):
        shown.update(r["cusip"] for r in consensus.get(side, []))
    for layer in ai["layers"]:
        shown.update(r["cusip"] for r in layer["rows"])
    descriptions = build_descriptions(conn, shown)

    payload = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "institutions": institutions,
        "cot": cot,
        "midcap": midcap,
        "consensus": consensus,
        "sectors": sectors,
        "ai": ai,
        "market_read": build_market_read(conn, institutions, midcap),
        "descriptions": descriptions,
        "meta": {
            "13f_last_run": db.get_meta(conn, "13f_last_run"),
            "cot_last_run": db.get_meta(conn, "cot_last_run"),
            "cot_latest_report": db.get_meta(conn, "cot_latest_report"),
            "enrich_last_run": db.get_meta(conn, "enrich_last_run"),
        },
    }
    os.makedirs(WEB_DIR, exist_ok=True)
    out = os.path.join(WEB_DIR, "data.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
    size = os.path.getsize(out)
    print(
        f"wrote {out} ({size/1024:.0f} KB): "
        f"{len(payload['institutions'])} institutions, "
        f"{len(payload['cot']['contracts'])} contracts, "
        f"{payload['midcap']['priced']} mid-caps, "
        f"{len(payload['descriptions'])} descriptions"
    )


if __name__ == "__main__":
    main()
