# Data preparation

Scripts that acquire and normalise input data. **No data files live here or
anywhere else in this repository** — only the code that produces them.

## Why data is not committed

1. **Re-distribution rights.** Index PB history and ETF quotes were obtained from
   commercial vendors. Their terms do not permit re-hosting, and a public repo
   is re-hosting.
2. **Size.** Raw daily series for 53 indices run to several MB and change daily.
   Committed snapshots go stale immediately and bloat history permanently
   (`git` cannot forget them).

All output paths are argument-driven, so you can write wherever you like —
including outside the repo.

## Credentials

Every script reads its key from an **environment variable** and exits with
code `2` when it is missing. There is no key literal in this directory, and
`scripts/check_no_secrets.py` enforces that on every commit.

```bash
export MX_APIKEY=...                 # or pass --api-key-env OTHER_NAME
```

See [`docs/DATA_SOURCES.md`](../../docs/DATA_SOURCES.md) for the credential model
and for open, key-less alternatives (AKShare, Tushare, Baostock, exchange
publications) if you would rather not use a commercial endpoint at all.

## Scripts

| Script | Purpose |
|---|---|
| `build_etf_meta.py` | Emits `etf_index_map.json`: 53 ETFs → tracking index name / vendor code / listing date. Pure metadata, safe to commit. |
| `fetch_index_monthly.py` | Pulls daily index PB + close/volume, resamples to month-end, writes `<data-dir>/index_monthly/*.json` plus a raw audit copy. |
| `probe_mx.py` | Prints a **summary** (table count, date span, row labels) of what a natural-language endpoint returns. Use it before writing a parser. |

`probe_mx.py` deliberately never prints raw series: a full daily history dumped
into a terminal or an agent context costs far more than the answer is worth.

## Typical sequence

```bash
cd scripts/data_prep

# 1. metadata (works with no credentials at all)
python build_etf_meta.py --out etf_index_map.json

# 2. what does the endpoint actually return?
export MX_APIKEY=...
python probe_mx.py --preset smoke

# 3. bulk fetch into a directory outside the repo
python fetch_index_monthly.py --map etf_index_map.json --data-dir ~/sds-data
```

## Note on `data/*.py`

The four original ad-hoc scripts (`stage1_build.py`, `fetch_index.py`,
`probe_mx.py`, `test_fetch.py`) still exist under `data/` as working copies.
They hard-code absolute paths from the machine they were written on, which is
exactly why `data/` is git-ignored. **The versions in this directory supersede
them** — edit here, not there.
