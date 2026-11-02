#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Probe a natural-language market-data endpoint and print a *summary* of the
returned tables -- never the raw series.

Why a dedicated probe
---------------------
Every data source in this project was integrated by first asking one question:
"what shape does this vendor actually return?" Hand-written probes answered that
in seconds and avoided dumping thousands of rows into a terminal or an agent
context. This tool keeps that workflow reproducible for the next source.

Credentials come from an environment variable only (default ``MX_APIKEY``).

Usage
-----
    export MX_APIKEY=...
    python probe_mx.py --query "中证证券公司指数399975 每月末市净率PB"
    python probe_mx.py --preset smoke          # a handful of known-tricky cases
    python probe_mx.py --query "..." --save raw/probe.json
"""
import argparse
import json
import os
import re
import sys
import urllib.request

DEFAULT_ENDPOINT = "https://mkapi2.dfcfs.com/finskillshub/api/claw/query"
DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")

# Cases that historically broke naive parsing: HK indices, CSI-prefixed codes,
# and indices with no free daily PB series.
PRESETS = {
    "smoke": [
        "中证证券公司指数399975 每月末市净率PB",
        "中证机床指数931866 每月末市净率PB",
        "恒生科技指数HSTECH 每月末市净率PB",
    ],
    "daily": [
        "中证证券公司指数399975 近10年每个交易日市净率PB",
        "中证证券公司指数399975 近10年每个交易日收盘价和成交量",
    ],
}


def query(endpoint, api_key, text, timeout=90):
    body = json.dumps({"toolQuery": text}).encode("utf-8")
    req = urllib.request.Request(
        endpoint, data=body, method="POST",
        headers={"Content-Type": "application/json", "apikey": api_key})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def tables_of(payload):
    if not isinstance(payload, dict):
        return None, "payload is not a dict"
    if payload.get("status") != 0:
        return None, "status=%s body=%s" % (payload.get("status"), str(payload)[:160])
    node = (((payload.get("data", {}) or {}).get("data", {}) or {})
            .get("searchDataResultDTO", {}) or {}).get("dataTableDTOList", [])
    return node or [], None


def summarize(text, payload, verbose=False):
    tables, err = tables_of(payload)
    if err:
        print("  status: %s" % err)
        return
    print("  tables=%d" % len(tables))
    for i, item in enumerate(tables[:4]):
        tbl = item.get("table") or {}
        heads = [str(h) for h in (tbl.get("headName") or [])]
        labels = [k for k in tbl.keys() if k != "headName"]
        dates = [d for d in (DATE_RE.search(h).group(1) if DATE_RE.search(h) else None
                             for h in heads) if d]
        span = "[%s .. %s]" % (dates[0], dates[-1]) if dates else "[no dated columns]"
        print("    table[%d] title=%r cols=%d %s" % (i, item.get("title"), len(heads), span))
        if labels:
            print("      row_labels=%s" % labels[:8])
        if verbose:
            print("      head_sample=%s" % heads[:3])


def main(argv=None):
    ap = argparse.ArgumentParser(description="Probe an NL market-data endpoint (summary only).")
    ap.add_argument("--query", action="append", default=[],
                    help="query text; repeatable")
    ap.add_argument("--preset", choices=sorted(PRESETS), help="run a built-in query set")
    ap.add_argument("--endpoint", default=os.getenv("SDS_QUERY_ENDPOINT", DEFAULT_ENDPOINT),
                    help="endpoint URL (default: $SDS_QUERY_ENDPOINT)")
    ap.add_argument("--api-key-env", default="MX_APIKEY",
                    help="env var holding the API key")
    ap.add_argument("--save", default="", help="write raw JSON payloads to this path (prefix)")
    ap.add_argument("--verbose", action="store_true", help="also show a few column headers")
    args = ap.parse_args(argv)

    queries = list(args.query)
    if args.preset:
        queries.extend(PRESETS[args.preset])
    if not queries:
        ap.error("give at least one --query, or use --preset")

    api_key = os.getenv(args.api_key_env)
    if not api_key:
        sys.stderr.write("ERROR: env var %s is not set (see docs/DATA_SOURCES.md)\n"
                         % args.api_key_env)
        return 2

    rc = 0
    for idx, text in enumerate(queries):
        print("=== %s" % text)
        try:
            payload = query(args.endpoint, api_key, text)
        except Exception as exc:
            print("  request failed: %r" % (exc,))
            rc = 1
            continue
        if args.save:
            target = "%s_%02d.json" % (args.save, idx)
            os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
            with open(target, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=1)
            print("  saved -> %s" % target)
        summarize(text, payload, verbose=args.verbose)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
