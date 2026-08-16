"""Run the full refresh: 13F → COT → rebuild data.json.

Safe to run on a schedule. Both ingests are incremental and idempotent, so a
run with no new filings costs a handful of API calls and changes nothing.
Exit code is non-zero only if every stage failed.
"""

import datetime as dt
import subprocess
import sys
import os

HERE = os.path.dirname(os.path.abspath(__file__))
STAGES = [
    ("13F (SEC EDGAR)", [sys.executable, "fetch_13f.py"]),
    ("COT (CFTC)", [sys.executable, "fetch_cot.py"]),
    ("enrich market caps", [sys.executable, "enrich.py"]),
    ("build data.json", [sys.executable, "build_site.py"]),
]


def main():
    stamp = dt.datetime.now().isoformat(timespec="seconds")
    print(f"\n{'='*66}\nupdate run {stamp}\n{'='*66}")
    failures = []
    for label, cmd in STAGES:
        print(f"\n--- {label} ---", flush=True)
        proc = subprocess.run(cmd, cwd=HERE)
        if proc.returncode != 0:
            print(f"!!! {label} failed (exit {proc.returncode})", file=sys.stderr)
            failures.append(label)
    if failures:
        print(f"\ncompleted with failures: {', '.join(failures)}", file=sys.stderr)
        # A failed ingest still leaves the previous data.json in place, so only
        # a total wipeout is worth signalling as an error.
        return 1 if len(failures) == len(STAGES) else 0
    print("\nall stages OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
