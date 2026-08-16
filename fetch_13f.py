"""Ingest 13F-HR holdings for every tracked institution into SQLite.

Incremental: a filing already stored is skipped, so repeat runs only pay for
newly published reports. Run with --force to re-download everything.
"""

import argparse
import datetime as dt
import sys

import db
import sec
from config import INSTITUTIONS, QUARTERS

# The 2022 amendments moved 13F `value` from thousands of dollars to whole
# dollars, effective for filings made on or after 2023-01-03.
WHOLE_DOLLAR_CUTOVER = "2023-01-03"


def recent_periods(n=QUARTERS, today=None):
    """The n most recent quarter-end dates that could plausibly be reported."""
    today = today or dt.date.today()
    q_ends = []
    year = today.year
    for y in (year, year - 1, year - 2):
        for m, d in ((3, 31), (6, 30), (9, 30), (12, 31)):
            q_ends.append(dt.date(y, m, d))
    # A quarter's 13F is due 45 days after quarter end.
    eligible = [q for q in q_ends if q + dt.timedelta(days=45) <= today]
    eligible.sort(reverse=True)
    return [q.isoformat() for q in eligible[:n]]


def wanted_filings(inst, periods):
    """Every 13F-HR (and amendment) for this institution within `periods`."""
    found = []
    for cik in inst["ciks"]:
        try:
            _name, rows = sec.submissions(cik)
        except Exception as e:  # noqa: BLE001
            print(f"  ! CIK {cik}: submissions unavailable ({e})", file=sys.stderr)
            continue
        for r in rows:
            form = (r.get("form") or "").upper()
            # 13F-NT is a notice that holdings are reported elsewhere; it
            # carries no information table, so there is nothing to ingest.
            if not form.startswith("13F-HR"):
                continue
            if (r.get("reportDate") or "") not in periods:
                continue
            found.append(
                {
                    "cik": cik,
                    "accession": r["accessionNumber"],
                    "form": form,
                    "filing_date": r["filingDate"],
                    "report_date": r["reportDate"],
                }
            )
    return found


def ingest_filing(conn, inst, f, force=False):
    acc = f["accession"]
    if not force:
        hit = conn.execute(
            "SELECT 1 FROM filings WHERE accession=?", (acc,)
        ).fetchone()
        if hit:
            return "skip", 0

    names = sec.filing_documents(f["cik"], acc)
    table_name = next(
        (n for n in names if n.lower().endswith(".xml") and "infotable" in n.lower()),
        None,
    )
    if table_name is None:
        # Some filers name the table oddly; fall back to any XML that is not
        # the cover page.
        table_name = next(
            (n for n in names if n.lower().endswith(".xml") and n != "primary_doc.xml"),
            None,
        )
    if table_name is None:
        return "no-table", 0

    amendment_type = None
    if "primary_doc.xml" in names:
        try:
            cover = sec.parse_primary_doc(
                sec.fetch(f"{sec.archive_dir(f['cik'], acc)}/primary_doc.xml")
            )
            amendment_type = cover.get("amendment_type")
        except Exception:  # noqa: BLE001 - cover page is best-effort metadata
            pass

    raw = sec.fetch(f"{sec.archive_dir(f['cik'], acc)}/{table_name}")
    in_thousands = f["filing_date"] < WHOLE_DOLLAR_CUTOVER
    rows = sec.parse_info_table(raw, in_thousands)

    now = dt.datetime.now().isoformat(timespec="seconds")
    with conn:
        conn.execute("DELETE FROM holdings WHERE accession=?", (acc,))
        conn.execute("DELETE FROM filings WHERE accession=?", (acc,))
        conn.execute(
            "INSERT INTO filings(accession, institution, cik, form, amendment_type,"
            " filing_date, report_date, row_count, total_value, fetched_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                acc,
                inst["id"],
                f["cik"],
                f["form"],
                amendment_type,
                f["filing_date"],
                f["report_date"],
                len(rows),
                sum(r["value_usd"] for r in rows),
                now,
            ),
        )
        conn.executemany(
            "INSERT INTO holdings(accession, institution, report_date, cusip, issuer,"
            " title_class, put_call, value_usd, shares, share_type)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    acc,
                    inst["id"],
                    f["report_date"],
                    r["cusip"],
                    r["issuer"],
                    r["title_class"],
                    r["put_call"],
                    r["value_usd"],
                    r["shares"],
                    r["share_type"],
                )
                for r in rows
            ],
        )
    return "ok", len(rows)


# Fraction of the smaller filing's positions that must appear in a later
# filing for us to treat the earlier one as superseded rather than additive.
SUPERSEDE_OVERLAP = 0.5


def prune_superseded(conn):
    """Drop filings for a period that a later filing already restates.

    The declared amendmentType cannot be trusted: JPMorgan's 2025-Q3
    amendment is labelled "NEW HOLDINGS" but repeats the entire 32k-row
    table, and summing it with the original would double the portfolio.
    Genuine additive amendments (Vanguard's 54- and 80-row ones) share no
    positions with the original at all, so overlap separates the two cases
    cleanly. Anything landing between the extremes is left alone and
    reported, rather than silently guessed at.
    """
    rows = conn.execute(
        "SELECT accession, institution, cik, report_date, form, amendment_type,"
        " filing_date, superseded FROM filings ORDER BY filing_date"
    ).fetchall()
    by_period = {}
    for r in rows:
        by_period.setdefault((r["cik"], r["report_date"]), []).append(r)

    def keys(acc):
        return {
            (h["cusip"], h["put_call"])
            for h in conn.execute(
                "SELECT cusip, put_call FROM holdings WHERE accession=?", (acc,)
            )
        }

    def supersede(r, tag, overlap=None):
        with conn:
            conn.execute("DELETE FROM holdings WHERE accession=?", (r["accession"],))
            conn.execute(
                "UPDATE filings SET superseded=1 WHERE accession=?", (r["accession"],)
            )
        if overlap is not None and overlap < 0.99:
            ambiguous.append((r, overlap, tag))

    dropped, ambiguous = 0, []
    for group in by_period.values():
        if len(group) < 2:
            continue

        # Semantic pass first: a RESTATEMENT is the authoritative table for its
        # (filer, period) by SEC definition, so it supersedes every other
        # filing from the *same CIK* that is not newer than it — whatever the
        # overlap. This catches JPMorgan filing a 378-row 13F-HR alongside a
        # 33k-row RESTATEMENT on the same day, where a pure overlap test would
        # keep the small one and double-count the securities in both.
        restatements = [
            r for r in group
            if (r["amendment_type"] or "").upper().startswith("RESTATE")
        ]
        if restatements:
            latest_rs = max(restatements, key=lambda r: r["filing_date"])
            for r in group:
                if r["accession"] == latest_rs["accession"] or r["superseded"]:
                    continue
                if r["cik"] == latest_rs["cik"] and r["filing_date"] <= latest_rs["filing_date"]:
                    supersede(r, "restatement")
                    dropped += 1
        group = [r for r in group if not conn.execute(
            "SELECT superseded FROM filings WHERE accession=?", (r["accession"],)
        ).fetchone()["superseded"]]
        if len(group) < 2:
            continue

        # Overlap pass: walk newest first, keeping a filing only if it adds
        # positions the newer ones do not already cover. Catches full re-files
        # mislabelled "NEW HOLDINGS" (100% overlap) while leaving genuinely
        # additive cross-entity amendments (0% overlap) in place.
        group.sort(key=lambda r: r["filing_date"], reverse=True)
        covered = set()
        for r in group:
            k = keys(r["accession"])
            if not k:
                # Already superseded on a prior run (its holdings were dropped)
                # or an empty table; nothing to compare or re-drop.
                continue
            overlap = len(k & covered) / len(k) if covered else 0.0
            if overlap > SUPERSEDE_OVERLAP:
                # Keep the filing row (marked) so a later run recognises it and
                # does not re-download the table; drop only its holdings so the
                # aggregation over the holdings table stays free of duplicates.
                supersede(r, "overlap", overlap)
                dropped += 1
            else:
                if 0.0 < overlap:
                    ambiguous.append((r, overlap, "kept"))
                covered |= k

    return dropped, ambiguous


def prune_old_periods(conn, periods):
    placeholders = ",".join("?" for _ in periods)
    with conn:
        conn.execute(
            f"DELETE FROM holdings WHERE report_date NOT IN ({placeholders})", periods
        )
        conn.execute(
            f"DELETE FROM filings WHERE report_date NOT IN ({placeholders})", periods
        )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="re-download stored filings")
    ap.add_argument("--only", help="limit to one institution id")
    args = ap.parse_args()

    periods = recent_periods()
    print(f"periods: {', '.join(periods)}")
    conn = db.connect()

    new_filings = 0
    for inst in INSTITUTIONS:
        if args.only and inst["id"] != args.only:
            continue
        print(f"\n{inst['name_en']} ({inst['id']})")
        filings = wanted_filings(inst, periods)
        if not filings:
            print("  no 13F-HR filings in window")
            continue
        for f in sorted(filings, key=lambda x: (x["report_date"], x["filing_date"])):
            try:
                status, n = ingest_filing(conn, inst, f, force=args.force)
            except Exception as e:  # noqa: BLE001
                print(f"  ! {f['accession']} {f['report_date']}: {e}", file=sys.stderr)
                continue
            if status == "ok":
                new_filings += 1
                print(f"  + {f['report_date']} {f['form']:10s} {n:>7,} positions")
            elif status == "no-table":
                print(f"  - {f['report_date']} {f['form']}: no information table")

    dropped, ambiguous = prune_superseded(conn)
    if dropped:
        print(f"\ndropped {dropped} superseded filing(s)")
    for r, ov, action in ambiguous:
        print(
            f"  ? partial overlap {ov:.0%}: {r['institution']} {r['report_date']}"
            f" {r['accession']} ({r['form']}/{r['amendment_type']}) -> {action}",
            file=sys.stderr,
        )
    prune_old_periods(conn, periods)

    with conn:
        db.set_meta(conn, "13f_last_run", dt.datetime.now().isoformat(timespec="seconds"))
        db.set_meta(conn, "13f_periods", ",".join(periods))
    print(f"\ndone: {new_filings} filing(s) ingested")


if __name__ == "__main__":
    main()
