"""Thin SEC EDGAR client: throttled HTTP plus 13F document parsing."""

import gzip
import io
import json
import re
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

from config import SEC_USER_AGENT

# SEC's published limit is 10 requests/second. Stay well under it.
_MIN_INTERVAL = 0.15
_lock = threading.Lock()
_last_request = [0.0]


def _throttle():
    with _lock:
        wait = _MIN_INTERVAL - (time.monotonic() - _last_request[0])
        if wait > 0:
            time.sleep(wait)
        _last_request[0] = time.monotonic()


def fetch(url, retries=4):
    """GET a URL with SEC-compliant headers, retrying on transient errors."""
    last_err = None
    for attempt in range(retries):
        _throttle()
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": SEC_USER_AGENT,
                "Accept-Encoding": "gzip",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 404:
                raise
            # 403/429 mean we're being throttled; back off hard.
            time.sleep(2 ** attempt)
        except Exception as e:  # noqa: BLE001 - network flakiness
            last_err = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"failed to fetch {url}: {last_err}")


def fetch_json(url):
    return json.loads(fetch(url).decode("utf-8"))


def submissions(cik):
    """All filings for a CIK, following EDGAR's paginated older-filings files."""
    base = fetch_json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
    recent = base["filings"]["recent"]
    rows = _rows_from(recent)
    for extra in base["filings"].get("files", []):
        # Older pages are only worth loading if they could hold recent periods.
        if extra.get("filingTo", "") < "2024-01-01":
            continue
        page = fetch_json(f"https://data.sec.gov/submissions/{extra['name']}")
        rows.extend(_rows_from(page))
    return base.get("name", ""), rows


def _rows_from(block):
    keys = ("form", "filingDate", "reportDate", "accessionNumber", "primaryDocument")
    n = len(block.get("form", []))
    out = []
    for i in range(n):
        out.append({k: (block.get(k) or [None] * n)[i] for k in keys})
    return out


def _strip_ns(tag):
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def archive_dir(cik, accession):
    return (
        f"https://www.sec.gov/Archives/edgar/data/{cik}/"
        f"{accession.replace('-', '')}"
    )


def filing_documents(cik, accession):
    """Filenames inside a filing, via its index.json."""
    idx = fetch_json(f"{archive_dir(cik, accession)}/index.json")
    return [it["name"] for it in idx["directory"]["item"]]


def parse_primary_doc(raw):
    """Pull report type / amendment info off a 13F cover page."""
    root = ET.fromstring(raw)
    out = {"report_type": None, "amendment_type": None, "is_amendment": False}
    for el in root.iter():
        tag = _strip_ns(el.tag)
        text = (el.text or "").strip()
        if tag == "reportType" and text:
            out["report_type"] = text
        elif tag == "amendmentType" and text:
            out["amendment_type"] = text
        elif tag == "isAmendment" and text:
            out["is_amendment"] = text.lower() in ("true", "1", "y", "yes")
    return out


_NUM = re.compile(r"[^0-9.\-]")


def _to_int(text):
    if not text:
        return 0
    cleaned = _NUM.sub("", text)
    if not cleaned or cleaned in ("-", ".", "-."):
        return 0
    try:
        return int(round(float(cleaned)))
    except ValueError:
        return 0


def parse_info_table(raw, values_in_thousands):
    """Stream a 13F information table into holding dicts.

    BlackRock's table is >20MB, so this uses iterparse and clears each
    element as it goes rather than building a full DOM.
    """
    holdings = []
    for _event, el in ET.iterparse(io.BytesIO(raw), events=("end",)):
        if _strip_ns(el.tag) != "infoTable":
            continue
        rec = {
            "issuer": "",
            "title_class": "",
            "cusip": "",
            "value_usd": 0,
            "shares": 0,
            "share_type": "",
            "put_call": "",
        }
        for child in el.iter():
            tag = _strip_ns(child.tag)
            text = (child.text or "").strip()
            if tag == "nameOfIssuer":
                rec["issuer"] = text
            elif tag == "titleOfClass":
                rec["title_class"] = text
            elif tag == "cusip":
                rec["cusip"] = text.upper()
            elif tag == "value":
                rec["value_usd"] = _to_int(text)
            elif tag == "sshPrnamt":
                rec["shares"] = _to_int(text)
            elif tag == "sshPrnamtType":
                rec["share_type"] = text
            elif tag == "putCall":
                rec["put_call"] = text.capitalize()
        if values_in_thousands:
            rec["value_usd"] *= 1000
        if rec["cusip"]:
            holdings.append(rec)
        el.clear()
    return holdings
