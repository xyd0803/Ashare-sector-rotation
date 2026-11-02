#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mx_fetch.py —— 轻量妙想 Skills API 抓取器（stdlib only，无需 pandas）
POST https://mkapi2.dfcfs.com/finskillshub/api/claw/query
body: {"toolQuery": "自然语言问句"}  header: {"apikey": MX_APIKEY}

用途：为缺失的目标指数抓取 日频 收盘价/成交量/市净率PB（2019-01-01 至今），
直接写出与 build_daily.py 相同格式的：
  data/raw/mx/{CODE}.json        {index_code,index_name,source,as_of, pb:[[d,v]], close:[...], volume:[...]}
  data/index_monthly/{CODE}.json {index_code,index_name,source, pb:{ym:v}, close:{ym:v}, volume:{ym:v}}
"""
from __future__ import annotations
import argparse, json, os, re, sys, urllib.request, urllib.error
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
MAP = os.path.join(DATA, "meta", "etf_index_map.json")
RAW_MX = os.path.join(DATA, "raw", "mx")
IDX_MON = os.path.join(DATA, "index_monthly")
BASE_URL = "https://mkapi2.dfcfs.com/finskillshub/api/claw/query"

def metric_name(zh):
    if "收盘" in zh: return "close"
    if "成交量" in zh: return "volume"
    if "市净" in zh or "PB" in zh.upper(): return "pb"
    if "市盈" in zh: return "pe"
    if "股息" in zh: return "div"
    return None

def call_api(apikey, query):
    req = urllib.request.Request(BASE_URL, data=json.dumps({"toolQuery": query}).encode("utf-8"),
                                 headers={"Content-Type": "application/json", "apikey": apikey}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))

def parse_one(dto):
    """返回 (code, name, {metric: {date:float}})"""
    code = dto.get("code") or ""
    name = (dto.get("entityName") or "").split("(")[0].strip()
    tbl = dto.get("table") or {}
    name_map = dto.get("nameMap") or {}
    head = tbl.get("headName") or []
    if not head:
        return code, name, {}
    out = {}
    for k, v in tbl.items():
        if k == "headName":
            continue
        zh = str(name_map.get(k, k))
        m = metric_name(zh)
        if not m or not isinstance(v, list):
            continue
        d = {head[j]: to_float(v[j]) for j in range(min(len(head), len(v)))}
        d = {dd: vv for dd, vv in d.items() if vv is not None and DATE_RE.match(dd)}
        if d:
            out[m] = d
    return code, name, out

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
def to_float(s):
    if s is None: return None
    t = str(s).strip()
    if t in ("", "-", "--", "—", "None", "nan"): return None
    m = re.search(r"-?\d+(?:\.\d+)?", t)
    return float(m.group(0)) if m else None

def write_outputs(rec):
    code = rec["code"]
    metrics = rec["metrics"]
    # raw daily
    daily = {}
    for key in ("pb", "close", "volume", "pe", "div"):
        if key in metrics:
            daily[key] = sorted([[d, v] for d, v in metrics[key].items()])
    as_of = daily.get("close", daily.get("pb", [["", 0]]))[-1][0] if any(k in metrics for k in ("close", "pb")) else ""
    raw_out = {"index_code": code, "index_name": rec["name"], "source": "mx-skills-daily",
               "as_of": as_of, **daily}
    os.makedirs(RAW_MX, exist_ok=True)
    with open(os.path.join(RAW_MX, f"{code}.json"), "w", encoding="utf-8") as f:
        json.dump(raw_out, f, ensure_ascii=False, indent=1)
    # monthly
    ym_map = {}
    for key in ("pb", "close", "volume"):
        if key not in metrics: continue
        for d, v in metrics[key].items():
            ym_map.setdefault(key, {}).setdefault(d[:7], []).append((d, v))
    months = sorted(set().union(*[set(v.keys()) for v in ym_map.values()])) if ym_map else []
    out = {"index_code": code, "index_name": rec["name"], "source": "mx-skills-daily",
           "pb": {}, "close": {}, "volume": {}}
    for ym in months:
        if "close" in ym_map: out["close"][ym] = sorted(ym_map["close"][ym])[-1][1]
        if "pb" in ym_map: out["pb"][ym] = sorted(ym_map["pb"][ym])[-1][1]
        if "volume" in ym_map: out["volume"][ym] = sum(v for _, v in ym_map["volume"][ym])
    os.makedirs(IDX_MON, exist_ok=True)
    with open(os.path.join(IDX_MON, f"{code}.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", required=True, help="逗号分隔的 index_code 列表")
    ap.add_argument("--start", default="2019-01-01")
    ap.add_argument("--end", default="2026-08-14")
    ap.add_argument("--query", default=None, help="自定义自然语言查询（覆盖自动构造）")
    ap.add_argument("--probe", action="store_true", help="只打印结构不写文件")
    a = ap.parse_args()
    apikey = os.getenv("MX_APIKEY")
    if not apikey:
        print("ERROR: MX_APIKEY 未设置"); sys.exit(2)
    mp = json.load(open(MAP, encoding="utf-8"))
    name_of = {v["index_code"]: v.get("index_name", "") for v in mp.values()}
    codes = [c.strip() for c in a.codes.split(",") if c.strip()]
    # 构造查询：每个 code 用 名称+code
    parts = []
    for c in codes:
        nm = name_of.get(c, c)
        parts.append(f"{nm}指数{c}")
    if a.query:
        query = a.query
    else:
        query = f"查询{'、'.join(parts)} 从{a.start}到{a.end} 每个交易日的收盘价、成交量、市净率PB"
    print("QUERY:", query)
    try:
        res = call_api(apikey, query)
    except urllib.error.HTTPError as e:
        print("HTTP ERROR", e.code, e.read().decode("utf-8", "replace")[:500]); sys.exit(3)
    except Exception as e:
        print("NET ERROR", e); sys.exit(3)
    print("TOP status:", res.get("status"), "message:", res.get("message"))
    inner = res.get("data", {}).get("data", {}).get("searchDataResultDTO", {})
    dtos = inner.get("dataTableDTOList", []) or []
    print("dataTableDTOList 数量:", len(dtos))
    acc = {}
    for dto in dtos:
        code, name, metrics = parse_one(dto)
        if not code:
            code = re.search(r"\(([^()]*\.[^()]*)\)", str(dto.get("entityName", "")))
            code = code.group(1).upper() if code else ""
        if not code:
            continue
        if not metrics:
            print("  [空指标]", name or code, dto.get("title"))
            continue
        acc[code] = {"code": code, "name": name, "metrics": metrics}
        print(f"  {code} {name}: 指标={list(metrics.keys())} 点数={ {k:len(v) for k,v in metrics.items()} }")
    if a.probe:
        return
    for code, rec in acc.items():
        write_outputs(rec)
        print("写出", code)

if __name__ == "__main__":
    main()
