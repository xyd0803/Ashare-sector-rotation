#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fetch daily PB + K-line (close/volume) for every tracking index, resample to
month-end, and persist normalized monthly series.

Why this exists
---------------
Index-level PB history is the denominator of every valuation-percentile gate in
this project, and most free APIs cap index PB at a few years or serve only the
latest value. The approach below drives a natural-language query endpoint that
returns full daily history, then resamples locally. Any provider exposing the
same ``table.run/headName`` shape can be swapped in via ``--endpoint``.

Credentials
-----------
The API key is read from an environment variable (default ``MX_APIKEY``); there
is no key literal anywhere in this file. See docs/DATA_SOURCES.md for the
credential model and for open, key-less alternatives.

Outputs
-------
``<data-dir>/index_monthly/<index_code>.json``  normalized monthly series
``<data-dir>/raw/<index_code>.json``            raw daily points (audit trail)
``<data-dir>/fetch_index_summary.txt``          one line per index

None of these are committed: derived market data is re-distributable only by its
original vendor.

Usage
-----
    export MX_APIKEY=...                       # or --api-key-env OTHER_VAR
    python fetch_index_monthly.py --data-dir ./data --map etf_index_map.json
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request
from collections import defaultdict

DEFAULT_ENDPOINT = "https://mkapi2.dfcfs.com/finskillshub/api/claw/query"
DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


# --------------------------------------------------------------------------- #
# parsing helpers
# --------------------------------------------------------------------------- #
def parse_date(h):
    m = DATE_RE.search(str(h))
    return m.group(1) if m else None


def parse_float(s):
    """Tolerate vendor decorations: '1.23倍', '2.5%', '3.1万', '4亿'."""
    if s is None:
        return None
    s = str(s)
    for junk in ("倍", "点", "股", "手", "%"):
        s = s.replace(junk, "")
    s = s.replace("亿", "e8").replace("万", "e4")
    s = re.sub(r"[^\d.eE+-]", "", s)
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def extract_tables(payload):
    if not isinstance(payload, dict) or payload.get("status") != 0:
        return []
    node = (((payload.get("data", {}) or {}).get("data", {}) or {})
            .get("searchDataResultDTO", {}) or {}).get("dataTableDTOList", [])
    return node or []


def parse_pb(tables):
    for tbl_obj in tables:
        if "市净率" not in (tbl_obj.get("title") or ""):
            continue
        tbl = tbl_obj.get("table") or {}
        heads = tbl.get("headName") or []
        for row in (tbl.get("items") or []):
            if row and "市净率" in str(row[0]):
                dates = [parse_date(h) for h in heads]
                vals = [parse_float(v) for v in row[1:]]
                return [(d, v) for d, v in zip(dates, vals) if d and v is not None]
    return None


def parse_kline(tables):
    """Pick the table with a long date axis, then split close / volume rows."""
    best = None
    for tbl_obj in tables:
        heads = (tbl_obj.get("table") or {}).get("headName") or []
        title = tbl_obj.get("title") or ""
        if len(heads) > 30 and ("收盘" in title or "成交量" in title):
            best = tbl_obj
    if best is None:
        return None, None
    tbl = best.get("table") or {}
    heads = tbl.get("headName") or []
    dates = [parse_date(h) for h in heads]
    close, vol = None, None
    for row in (tbl.get("items") or []):
        if not row:
            continue
        label = str(row[0])
        vals = [parse_float(v) for v in row[1:]]
        pairs = [(d, v) for d, v in zip(dates, vals) if d and v is not None]
        if "收盘" in label and close is None:
            close = pairs
        elif "成交量" in label and vol is None:
            vol = pairs
    return close, vol


# --------------------------------------------------------------------------- #
# transport
# --------------------------------------------------------------------------- #
def make_query(endpoint, api_key, timeout=120):
    def _query(text):
        body = json.dumps({"toolQuery": text}).encode("utf-8")
        req = urllib.request.Request(
            endpoint, data=body, method="POST",
            headers={"Content-Type": "application/json", "apikey": api_key})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    return _query


def query_retry(query, text, parser, attempts=3, backoff=1.5):
    """Run one NL query with retries. Returns (result, last_error)."""
    last_err = None
    for _ in range(attempts):
        try:
            return parser(extract_tables(query(text))), None
        except Exception as exc:  # network, JSON, vendor shape drift
            last_err = repr(exc)
        time.sleep(backoff)
    return None, last_err


def fetch_one(query, name, code, min_points=50):
    """Try three phrasings per metric: '<name><code>' -> '<name>指数<code>' -> '<code>'."""
    out = {"pb": None, "close": None, "volume": None, "errors": []}
    variants = (f"{name}{code}", f"{name}指数{code}", f"{code}")

    for variant in variants:
        pb, err = query_retry(query, f"{variant} 近10年每个交易日市净率PB", parse_pb)
        if pb and len(pb) > min_points:
            out["pb"] = pb
            break
        out["errors"].append("pb:%s:%s" % (variant, err or "short"))

    for variant in variants:
        got, err = query_retry(query, f"{variant} 近10年每个交易日收盘价和成交量", parse_kline)
        close, vol = got or (None, None)
        if close and len(close) > min_points:
            out["close"] = close
            out["volume"] = vol
            break
        out["errors"].append("kl:%s:%s" % (variant, err or "short"))

    return out


# --------------------------------------------------------------------------- #
# resampling
# --------------------------------------------------------------------------- #
def resample_last(pairs):
    """Last observation in each calendar month."""
    last = {}
    for day, val in pairs:
        last[day[:7]] = val
    return {ym: round(last[ym], 6) for ym in sorted(last)}


def resample_sum(pairs):
    tot = defaultdict(float)
    for day, val in pairs:
        tot[day[:7]] += val
    return {ym: round(tot[ym], 4) for ym in sorted(tot)}


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main(argv=None):
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="Fetch index PB / K-line monthly series.")
    ap.add_argument("--map", default=os.path.join(here, "etf_index_map.json"),
                    help="ETF->index map (see build_etf_meta.py)")
    ap.add_argument("--data-dir", default=os.getenv("SDS_DATA_DIR", "./data"),
                    help="output root (default: $SDS_DATA_DIR or ./data)")
    ap.add_argument("--endpoint", default=os.getenv("SDS_QUERY_ENDPOINT", DEFAULT_ENDPOINT),
                    help="NL query endpoint (default: $SDS_QUERY_ENDPOINT)")
    ap.add_argument("--api-key-env", default="MX_APIKEY",
                    help="name of the env var holding the API key")
    ap.add_argument("--only", default="", help="comma-separated ETF codes to limit the run")
    ap.add_argument("--sleep", type=float, default=0.3, help="pause between indices")
    args = ap.parse_args(argv)

    api_key = os.getenv(args.api_key_env)
    if not api_key:
        sys.stderr.write("ERROR: env var %s is not set (see docs/DATA_SOURCES.md)\n"
                         % args.api_key_env)
        return 2
    if not os.path.exists(args.map):
        sys.stderr.write("ERROR: map not found: %s\n" % args.map)
        return 2

    raw_dir = os.path.join(args.data_dir, "raw")
    monthly_dir = os.path.join(args.data_dir, "index_monthly")
    os.makedirs(raw_dir, exist_ok=True)
    os.makedirs(monthly_dir, exist_ok=True)

    with open(args.map, "r", encoding="utf-8") as f:
        mapping = json.load(f)

    wanted = {c.strip() for c in args.only.split(",") if c.strip()}
    if wanted:
        mapping = {k: v for k, v in mapping.items() if k in wanted}

    query = make_query(args.endpoint, api_key)
    print("[fetch_index] entries=%d data_dir=%s" % (len(mapping), args.data_dir), flush=True)

    summary = []
    for etf_code, info in mapping.items():
        idx_code = info["index_code"]
        idx_name = info["index_name"]
        res = fetch_one(query, idx_name, idx_code)

        rec = {"index_code": idx_code, "index_name": idx_name, "etf_code": etf_code}
        if res["pb"]:
            rec["pb"] = resample_last(res["pb"])
        if res["close"]:
            rec["close"] = resample_last(res["close"])
        if res["volume"]:
            rec["volume"] = resample_sum(res["volume"])

        with open(os.path.join(raw_dir, "%s.json" % idx_code), "w", encoding="utf-8") as f:
            json.dump({"pb": res["pb"], "close": res["close"], "volume": res["volume"]},
                      f, ensure_ascii=False)
        if rec.get("pb") or rec.get("close"):
            with open(os.path.join(monthly_dir, "%s.json" % idx_code), "w", encoding="utf-8") as f:
                json.dump(rec, f, ensure_ascii=False)

        status = "OK" if (rec.get("pb") and rec.get("close")) else "PARTIAL"
        n_months = len(rec.get("close") or {})
        summary.append((etf_code, idx_code, n_months,
                        len(res["pb"] or []), len(res["close"] or []),
                        len(res["volume"] or []), status, ";".join(res["errors"][:2])))
        print("  %s %s: months=%d pb_d=%d kl_d=%d vol_d=%d %s %s"
              % (etf_code, idx_code, n_months, len(res["pb"] or []),
                 len(res["close"] or []), len(res["volume"] or []), status,
                 res["errors"][0] if res["errors"] else ""), flush=True)
        time.sleep(args.sleep)

    out_txt = os.path.join(args.data_dir, "fetch_index_summary.txt")
    with open(out_txt, "w", encoding="utf-8") as f:
        for row in summary:
            f.write("\t".join(str(x) for x in row) + "\n")
    ok = sum(1 for r in summary if r[6] == "OK")
    print("[fetch_index] done: %d/%d OK -> %s" % (ok, len(summary), out_txt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
