#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_daily.py —— 从妙想 MCP 落地文件重建日频指数数据并下采样为月度
================================================================================
扫描 MCP 客户端的「工具结果落盘目录」下的全部结果文件（目录与文件名前缀见下方
_TOOL_RESULTS_GLOB / _CONNECTOR_PREFIX，均可用环境变量覆盖）：
  · chatcmpl-tool-*.txt            （会话内联落盘）
  · mcp-connector-proxy-*.txt      （连接器代理落盘）
解析宽表结构 {data:[{columns:[header, 日期...], items:[[指标名,值...]], sheetName}]}
提取 收盘价/成交量/市净率PB（及 市盈率PE/股息率），去后缀、去 '-'，按交易日对齐。

产出：
  data/raw/mx/{INDEX_CODE}.json   {index_code,index_name,source,as_of,
                                   pb:[[date,val]...]升序, close:[...], volume:[...]}
  data/index_monthly/{INDEX_CODE}.json  {index_code,index_name,source,
                                   pb:{ym:v}, close:{ym:v}, volume:{ym:v}}  (月末值/pb, 月求和volume)

用法：
  python build_daily.py            # 扫描+重建+下采样+打印覆盖报告
  python build_daily.py --dry      # 仅扫描打印覆盖，不写文件
"""
from __future__ import annotations
import argparse, json, os, re, sys, glob
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
MAP = os.path.join(DATA, "meta", "etf_index_map.json")
RAW_MX = os.path.join(DATA, "raw", "mx")
IDX_MON = os.path.join(DATA, "index_monthly")

# ---- 工具 -------------------------------------------------------------------
def to_float(s):
    if s is None:
        return None
    t = str(s).strip()
    if t in ("", "-", "--", "—", "无", "None", "nan"):
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", t)
    if not m:
        return None
    try:
        return float(m.group(0))
    except Exception:
        return None

def norm_metric(name):
    n = str(name)
    if "收盘" in n:
        return "close"
    if "成交量" in n:
        return "volume"
    if "市净" in n:
        return "pb"
    if "市盈" in n:
        return "pe"
    if "股息" in n:
        return "div"
    return None  # 成交额 等忽略

CODE_RE = re.compile(r"\(([^()]*\.[^()]*)\)")          # 含点的括号 token，如 (000510.CSI)
CODE_RE2 = re.compile(r"\(([0-9]{4,6}[A-Za-z]{0,4})\)")  # 退化：纯数字代码如 (399986)
DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")

def extract_code(header):
    if not header:
        return None
    m = CODE_RE.search(header)
    if m:
        return m.group(1).upper()
    m = CODE_RE2.search(header)
    if m:
        return m.group(1).upper()
    return None

def extract_name(header):
    if not header:
        return ""
    return header.split("(")[0].strip()

# ---- 扫描落地文件 -----------------------------------------------------------
# MCP 客户端把工具返回落盘的位置和文件前缀因客户端而异，因此两者都做成可覆盖
# 的配置项：换一个客户端只需设环境变量，不必改代码。
_TOOL_RESULTS_GLOB = os.getenv(
    "MCP_TOOL_RESULTS_GLOB", "~/.workbuddy/projects/**/tool-results")
# 连接器代理落盘的文件名前缀（按自己的客户端调整）
_CONNECTOR_PREFIX = os.getenv(
    "MCP_CONNECTOR_FILE_PREFIX", "mcp-connector-proxy-mx-ds-mcp")

def scan_files():
    cand = []
    for r in glob.glob(os.path.expanduser(_TOOL_RESULTS_GLOB), recursive=True):
        if not os.path.isdir(r):
            continue
        for fn in os.listdir(r):
            if fn.startswith(_CONNECTOR_PREFIX) and fn.endswith(".txt"):
                cand.append(os.path.join(r, fn))
            elif fn.startswith("chatcmpl-tool-") and fn.endswith(".txt"):
                cand.append(os.path.join(r, fn))
    return sorted(set(cand))

def infer_metric_from_sheet(sheet):
    if not sheet:
        return None
    s = str(sheet)
    if "市净" in s:
        return "pb"
    if "市盈" in s:
        return "pe"
    if "收盘" in s:
        return "close"
    if "成交量" in s:
        return "volume"
    if "股息" in s:
        return "div"
    return None

def _pb_plausible(v):
    """PB 合理性守卫（2026-08-15 新增）：0.05 < PB < 40。
    曾致污染：收盘点位(数千)被误写入 pb 字段，此守卫拒绝异常值。"""
    return v is not None and 0.05 < v < 40


def _store(acc, code, metric, name, dates, vals):
    if not code or metric is None:
        return 0
    rec = acc.setdefault(code, {"name": name or "", "metrics": {}, "source": "mx-ds-mcp-daily"})
    if name and not rec["name"]:
        rec["name"] = name
    mm = rec["metrics"].setdefault(metric, {})
    cnt = 0
    for j, v in enumerate(vals):
        d = dates[j] if j < len(dates) else None
        if d is None:
            continue
        fv = to_float(v)
        if fv is None:
            continue
        if metric == "pb" and not _pb_plausible(fv):
            continue  # 点位污染防线
        mm[d] = fv
        cnt += 1
    return cnt

# acc: code -> {metric: {date: float}, name, source}
def parse_file(path, acc):
    try:
        raw = open(path, encoding="utf-8", errors="replace").read()
    except Exception:
        return 0
    try:
        obj = json.loads(raw)
    except Exception:
        return 0
    blocks = []
    if isinstance(obj, dict):
        d = obj.get("data")
        if isinstance(d, list):
            blocks = d
        elif isinstance(d, dict):
            blocks = [d]
    elif isinstance(obj, list):
        blocks = obj
    n = 0
    for blk in blocks:
        if not isinstance(blk, dict):
            continue
        cols = blk.get("columns")
        items = blk.get("items")
        if not isinstance(cols, list) or not isinstance(items, list):
            continue
        if len(cols) < 2:
            continue
        # 仅保留可解析为日期的列
        dates = []
        for c in cols[1:]:
            m = DATE_RE.search(str(c))
            dates.append(m.group(1) if m else None)
        if not any(dates):
            continue
        col0 = str(cols[0])
        metric_in_col0 = norm_metric(col0)        # Layout B：columns[0] 是指标名
        code_in_col0 = extract_code(col0)         # Layout A：columns[0] 是指数表头
        if metric_in_col0 is not None:
            # ---- Layout B: 每个 item 是一只指数，columns[0] 为统一指标 ----
            metric = metric_in_col0
            for it in items:
                if not isinstance(it, list) or not it:
                    continue
                code = extract_code(str(it[0]))
                name = extract_name(str(it[0]))
                n += _store(acc, code, metric, name, dates, it[1:])
        elif code_in_col0 is not None:
            # ---- Layout A: columns[0] 是指数表头，每个 item 是一个指标 ----
            code = code_in_col0
            name = extract_name(col0)
            for it in items:
                if not isinstance(it, list) or not it:
                    continue
                metric = norm_metric(it[0])
                if metric is None:
                    continue
                n += _store(acc, code, metric, name, dates, it[1:])
        else:
            # 退化：cols[0] 既非指标也非代码 -> 从 sheetName 推断指标
            # 修复(2026-08-15)：混合布局下逐行判定指标，禁止把整 sheet 套同一 metric
            # （曾致 H30202/931752 等 6 只 pb 字段被写入指数点位：收盘价行被误存为 pb）
            sheet_metric = infer_metric_from_sheet(blk.get("sheetName"))
            for it in items:
                if not isinstance(it, list) or not it:
                    continue
                row_metric = norm_metric(it[0])       # 行首是指标名 → 用行指标
                metric = row_metric if row_metric is not None else sheet_metric
                if metric is None:
                    continue
                code = extract_code(str(it[0]))
                if code is None:
                    # 行首是指标名（无代码）→ 需要从别的 item 拿代码；
                    # 但本行无代码时跳过（代码来自 cols[0]，此处无）
                    continue
                name = extract_name(str(it[0]))
                n += _store(acc, code, metric, name, dates, it[1:])
    return n

# ---- 主流程 -----------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="仅扫描不写文件")
    a = ap.parse_args()

    mp = json.load(open(MAP, encoding="utf-8"))
    target_codes = sorted({v["index_code"] for v in mp.values()})

    files = scan_files()
    print(f"扫描到妙想落地文件 {len(files)} 个")
    acc: dict = {}
    total_blocks = 0
    for f in files:
        total_blocks += parse_file(f, acc)
    print(f"解析出指数 {len(acc)} 个, 指标-日期点 {total_blocks}")

    # 覆盖报告
    missing = [c for c in target_codes if c not in acc]
    covered = [c for c in target_codes if c in acc]
    print(f"\n目标指数(来自etf_index_map) {len(target_codes)} 个")
    print(f"  已覆盖 {len(covered)} 个")
    print(f"  缺失   {len(missing)} 个: {missing}")

    if a.dry:
        return

    os.makedirs(RAW_MX, exist_ok=True)
    os.makedirs(IDX_MON, exist_ok=True)

    written_daily = 0
    written_monthly = 0
    for code, rec in acc.items():
        metrics = rec["metrics"]
        # 升序日频
        daily = {}
        for key in ("pb", "close", "volume", "pe", "div"):
            if key in metrics:
                pts = sorted(([d, v] for d, v in metrics[key].items()))
                daily[key] = pts
        as_of = daily.get("close", daily.get("pb", [["", 0]]))[-1][0] if any(
            k in metrics for k in ("close", "pb")) else ""
        raw_out = {
            "index_code": code,
            "index_name": rec["name"],
            "source": rec["source"],
            "as_of": as_of,
            **daily,
        }
        with open(os.path.join(RAW_MX, f"{code}.json"), "w", encoding="utf-8") as f:
            json.dump(raw_out, f, ensure_ascii=False, indent=1)
        written_daily += 1

        # 月度下采样
        ym_map = {}  # key -> ym -> list of (date,val)
        for key in ("pb", "close", "volume"):
            if key not in metrics:
                continue
            for d, v in metrics[key].items():
                ym = d[:7]
                ym_map.setdefault(key, {}).setdefault(ym, []).append((d, v))
        months = set()
        for k in ym_map:
            months |= set(ym_map[k].keys())
        months = sorted(months)
        out = {"index_code": code, "index_name": rec["name"], "source": rec["source"],
               "pb": {}, "close": {}, "volume": {}}
        for ym in months:
            if "close" in ym_map and ym in ym_map["close"]:
                lst = sorted(ym_map["close"][ym])
                out["close"][ym] = lst[-1][1]          # 月末收盘
            if "pb" in ym_map and ym in ym_map["pb"]:
                lst = sorted(ym_map["pb"][ym])
                out["pb"][ym] = lst[-1][1]             # 月末 PB (point-in-time)
            if "volume" in ym_map and ym in ym_map["volume"]:
                out["volume"][ym] = sum(v for _, v in ym_map["volume"][ym])  # 月成交量合计
        with open(os.path.join(IDX_MON, f"{code}.json"), "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        written_monthly += 1

    print(f"\n写出日频 raw/mx: {written_daily} 个")
    print(f"写出月度 index_monthly: {written_monthly} 个")
    print(f"仍缺失目标指数: {len(missing)} 个 -> 需补抓: {missing}")

if __name__ == "__main__":
    main()
