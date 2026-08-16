"""Shared logic for the mid-cap accumulation screen.

Kept separate so the enrichment step (which decides *which* CUSIPs to price)
and the site build (which renders the final table) apply exactly the same
definition of "accumulation".
"""

import collections

from config import ACCUM_MIN_GROWTH, ACCUM_PRICE_FLOOR, ACCUM_TOLERANCE


def all_periods(conn):
    return [
        r["report_date"]
        for r in conn.execute(
            "SELECT DISTINCT report_date FROM holdings ORDER BY report_date"
        )
    ]


# Clean split ratios to snap to. A stock split multiplies every holder's share
# count by the same factor while the per-share price divides by it, so raw
# quarter-over-quarter share deltas would otherwise show a huge phantom "buy".
_CLEAN_SPLITS = [2, 3, 4, 5, 6, 7, 8, 10, 12, 15, 20]
_SPLIT_TOL = 0.06
_split_cache = {}


def _snap_split(older_price, newer_price, older_shares, newer_shares):
    """Factor to bring older-quarter shares onto the newer quarter's basis.

    A split moves price and aggregate share count in opposite directions by the
    same factor; a mere price rally moves price alone. Requiring the share
    count to confirm the direction stops a tripling stock (price ratio ~1/3)
    from being mistaken for a 3:1 reverse split.
    """
    if not older_price or not newer_price or not older_shares or not newer_shares:
        return 1.0
    r = older_price / newer_price          # > 1 across a forward split
    share_ratio = newer_shares / older_shares
    for n in _CLEAN_SPLITS:
        # Forward split n:1 — price ~divides by n, shares ~multiply by n.
        if abs(r - n) <= _SPLIT_TOL * n and share_ratio >= 1.5:
            return float(n)
        # Reverse split 1:n — price ~multiplies by n, shares ~divide by n.
        if abs(r - 1.0 / n) <= _SPLIT_TOL / n and share_ratio <= 0.67:
            return 1.0 / n
    return 1.0


def split_factors(conn):
    """{cusip: {period: factor}} scaling each period's shares to the latest basis.

    Memoised per connection so the whole build pays for it once. Detected from
    the aggregate implied price (value/shares) plus the aggregate share count,
    which must both move consistently for a split to be recognised.
    """
    key = id(conn)
    if key in _split_cache:
        return _split_cache[key]
    periods = all_periods(conn)
    prices = collections.defaultdict(dict)
    shares = collections.defaultdict(dict)
    for r in conn.execute(
        "SELECT report_date, cusip, SUM(value_usd) v, SUM(shares) s FROM holdings"
        " WHERE put_call='' AND share_type='SH' GROUP BY report_date, cusip"
    ):
        if r["s"]:
            prices[r["cusip"]][r["report_date"]] = r["v"] / r["s"]
            shares[r["cusip"]][r["report_date"]] = r["s"]
    out = {}
    for cusip, pr in prices.items():
        sh = shares[cusip]
        fac = {periods[-1]: 1.0} if periods else {}
        cum = 1.0
        for i in range(len(periods) - 2, -1, -1):
            older, newer = periods[i], periods[i + 1]
            if older in pr and newer in pr:
                cum *= _snap_split(pr[older], pr[newer], sh.get(older), sh.get(newer))
            fac[older] = cum
        out[cusip] = fac
    _split_cache[key] = out
    return out


def split_adjust(conn, cusip, period, shares):
    """Split-adjust a raw share count for one cusip/period to the latest basis."""
    if shares is None:
        return shares
    fac = split_factors(conn).get(cusip, {}).get(period, 1.0)
    return shares * fac


def share_series(conn, institution=None):
    """Common-stock share counts per CUSIP per period.

    Restricted to outright long share positions (put_call empty, share type
    SH) so options and principal-amount debt lines never enter the
    accumulation signal. With `institution` set, only that institution's
    filers are counted; otherwise all tracked institutions are summed.
    """
    periods = all_periods(conn)
    where = "put_call='' AND share_type='SH'"
    params = []
    if institution is not None:
        where += " AND institution=?"
        params.append(institution)
    factors = split_factors(conn)
    series = collections.defaultdict(dict)
    meta = {}
    for r in conn.execute(
        "SELECT report_date, cusip, MAX(issuer) AS issuer,"
        " SUM(shares) AS shares, SUM(value_usd) AS value"
        f" FROM holdings WHERE {where}"
        " GROUP BY report_date, cusip",
        params,
    ):
        fac = factors.get(r["cusip"], {}).get(r["report_date"], 1.0)
        # shares is split-adjusted to the latest basis so accumulation is real;
        # value is split-immune and kept raw (price = value / raw shares).
        series[r["cusip"]][r["report_date"]] = {
            "shares": (r["shares"] * fac) if r["shares"] is not None else None,
            "raw_shares": r["shares"],
            "value": r["value"],
        }
        meta[r["cusip"]] = {"issuer": r["issuer"]}
    return periods, series, meta


def is_accumulating(vals, tol=ACCUM_TOLERANCE, growth=ACCUM_MIN_GROWTH):
    """True when a share-count series trends up across the whole window.

    Each quarter must hold at least `tol` of the prior quarter (so a small
    dip is forgiven) and the end must exceed the start by `growth`.
    """
    if len(vals) < 2 or not vals[0] or vals[0] <= 0:
        return False
    if not all(vals[i + 1] >= vals[i] * tol for i in range(len(vals) - 1)):
        return False
    return vals[-1] / vals[0] >= growth


def _candidates_from(periods, series):
    """CUSIPs in `series` that accumulated across every quarter, price-floored."""
    if len(periods) < 2:
        return []
    last = periods[-1]
    out = []
    for cusip, per in series.items():
        if not all(p in per for p in periods):
            continue
        vals = [per[p]["shares"] or 0 for p in periods]
        if not is_accumulating(vals):
            continue
        rec = per[last]
        price = (rec["value"] / rec["shares"]) if rec["shares"] else 0
        if price < ACCUM_PRICE_FLOOR:
            continue
        out.append(cusip)
    return out


def candidate_cusips(conn):
    """Aggregate accumulation candidates across all institutions.

    The price floor (latest quarter's implied price) drops penny stocks whose
    huge percentage swings are index-rebalancing noise, and bounds how many
    securities the enrichment step must price.
    """
    periods, series, meta = share_series(conn)
    return _candidates_from(periods, series), periods, series, meta


def candidates_by_institution(conn, institutions):
    """{inst_id: {cusips, periods, series, meta}} plus the union of all CUSIPs.

    Each institution is screened on its *own* holdings, so a stock BlackRock
    accumulated is attributed to BlackRock even if the seven-way aggregate was
    flat.
    """
    by_inst = {}
    union = set()
    for inst in institutions:
        periods, series, meta = share_series(conn, inst["id"])
        cusips = _candidates_from(periods, series)
        by_inst[inst["id"]] = {
            "cusips": cusips,
            "periods": periods,
            "series": series,
            "meta": meta,
        }
        union.update(cusips)
    return by_inst, union
