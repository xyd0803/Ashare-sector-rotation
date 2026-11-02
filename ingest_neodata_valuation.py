#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ingest_neodata_valuation.py —— neodata（腾讯 NeoData·免费源）指数估值增量合并
================================================================================
背景（2026-10-25 D-1 落地）：数据层审计发现 PE 日频序列仅 1 只（980032.CNI），
而按估值主锚分类 55 只映射 ETF 中约 3/4（PE 9 + BLEND 33）需要 PE 序列；PB 侧
48/51 缓存 stale。neodata MCP `index_valuation` 提供指数估值日频（PB(LF/MRQ)/
PE(TTM/LYR)/PS/PCF/股息率/风险溢价/总市值…，T+1，**免费·不耗 iFinD 配额与妙想配额**），
实测覆盖：000015.SH / 399976.SZ / 980032.SZ 等（A股沪深京+港股+美股主要指数）。

本脚本把 Agent 经 neodata MCP 取到的结果**增量合并**进估值缓存，合并语义与
iFinD 链完全同构（merge_valuation_gap 唯一实现：同日冲突保留原源 / 重叠期中位
比值归一 / 重叠<3 fail-closed / 幂等 source 标注），管线/闸门对来源无感知。

覆盖边界（诚实声明，勿静默降级）：
  · 后缀口径：neodata 用 .SH/.SZ/.BJ/.CSI/.HK/.US；技能缓存键可能是 .CNI 等
    （如 980032.CNI → neodata 侧须查 980032.SZ，实测有数）。查询后缀由 Agent
    侧保证，本脚本只按「主体代码」匹配行（980032.CNI 与 980032.SZ 视为同一指数）。
  · 并非所有指数都有覆盖（实测 931151.CSI 无返回）→ 无匹配行时显式 SKIP 并
    提示换源，**绝不**把"无覆盖"静默当成"已合并"。
  · 单次 limit ≤500、codes ≤10：补缺口（几十点）1 次调用即可；**新建缓存**
    （5y ~1200 点）须分段拉取（如每年一段）后多文件一次 ingest（--json 支持多文件）。

用法：
  # Agent 先经 neodata MCP 取数（免费）：
  #   mcp__neodata__index_valuation(index_codes="980032.SZ", start_date=..., end_date=..., limit=500)
  # 把返回 JSON 存文件（含 result 数组的原始响应，或仅 output markdown 表）后：
  python ingest_neodata_valuation.py --json nd_980032.json --code 980032.CNI
  python ingest_neodata_valuation.py --json a.json b.json c.json --code 399976.SZ   # 多段合并
  python ingest_neodata_valuation.py --table nd_980032.md --code 980032.CNI        # markdown 表

输出：
  data/index_daily_pb/{CODE}.json ← pb_lf；data/index_daily_pe/{CODE}.json ← pe_ttm
  PB 另同步 data/meta/pb_daily_bands.json（PE 不入，与 fetcher 行为一致）
记账：**无**（免费源，不动 iFinD 账本；对照 ingest_ifind_valuation 的 --charge）。
诚实性：as_of 恒取合并后真实序列末值；source 标注 neodata-mcp-daily-{metric}
（合并 +neodata_incr(_calib)）；无覆盖显式 SKIP；fail-closed 场景 exit 2。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from fetch_pb_mxdata import DATA  # noqa: E402
from merge_valuation_gap import (MergeError, build_record, load_cache,  # noqa: E402
                                 merge_series, report_conflicts, save_record,
                                 sync_bands)

PB_DIR = DATA / "index_daily_pb"
PE_DIR = DATA / "index_daily_pe"

_DATE8 = re.compile(r"^\d{8}$")


def _norm_date(s: str) -> str:
    s = str(s).strip().replace("/", "-")
    if _DATE8.match(s):
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s


def _base_code(code: str) -> str:
    """缓存键/查询码 → 主体代码（去市场后缀）：980032.CNI / 980032.SZ → 980032。"""
    return re.split(r"[.\s]", str(code).strip())[0]


def parse_neodata_json(payload) -> list[dict]:
    """解析 neodata MCP 原始响应（dict 含 result 数组，或直接 list）。

    返回 [{index_code, trading_day, pb, pe, name}]（pb←pb_lf, pe←pe_ttm）。
    """
    if isinstance(payload, str):
        payload = json.loads(payload)
    rows = payload.get("result") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("JSON 中无 result 数组（须为 neodata MCP 原始响应）")
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        d = _norm_date(r.get("trading_day") or "")
        try:
            import datetime as _dt
            _dt.date.fromisoformat(d)
        except ValueError:
            continue
        def _f(v):
            try:
                fv = float(v)
                return fv if fv == fv and abs(fv) < 1e12 else None
            except (TypeError, ValueError):
                return None
        out.append({"index_code": str(r.get("index_code") or ""),
                    "trading_day": d,
                    "pb": _f(r.get("pb_lf")),
                    "pe": _f(r.get("pe_ttm")),
                    "name": str(r.get("index_abbr") or r.get("index_name") or "")})
    return out


def parse_neodata_table(md: str) -> list[dict]:
    """解析 neodata `output` markdown 表（兜底路径；JSON 优先更稳）。"""
    rows, header = [], None
    for line in md.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if header is None and any("交易日" in c or "日期" in c for c in cells):
            header = cells
            continue
        if line.startswith("|-") or set(line) <= {"|", "-", " ", ":"}:
            continue
        if header and len(cells) == len(header):
            rows.append(dict(zip(header, cells)))
    if not rows:
        raise ValueError("未解析到数据行（表头须含『交易日』列）")
    date_col = next(c for c in header if "交易日" in c or "日期" in c)
    code_col = next((c for c in header if "指数代码" in c), None)
    name_col = next((c for c in header if "简称" in c), None)
    pb_col = next((c for c in header if "市净率" in c and "LF" in c), None) \
        or next((c for c in header if "市净率" in c), None)
    pe_col = next((c for c in header if "市盈率" in c and "TTM" in c), None) \
        or next((c for c in header if "市盈率" in c), None)

    out = []
    for r in rows:
        d = _norm_date(r.get(date_col, ""))
        try:
            import datetime as _dt
            _dt.date.fromisoformat(d)
        except ValueError:
            continue

        def _f(v):
            try:
                return float(str(v).replace(",", ""))
            except (TypeError, ValueError):
                return None
        out.append({"index_code": r.get(code_col, "") if code_col else "",
                    "trading_day": d,
                    "pb": _f(r.get(pb_col)) if pb_col else None,
                    "pe": _f(r.get(pe_col)) if pe_col else None,
                    "name": r.get(name_col, "") if name_col else ""})
    return out


def _collect(files: list[str], tables: list[str]) -> list[dict]:
    rows = []
    for fp in files:
        rows += parse_neodata_json(Path(fp).read_text(encoding="utf-8"))
    for tp in tables:
        rows += parse_neodata_table(Path(tp).read_text(encoding="utf-8"))
    return rows


def main():
    ap = argparse.ArgumentParser(description="neodata 指数估值增量合并进缓存（免费源）")
    ap.add_argument("--code", required=True,
                    help="技能侧指数代码（缓存键，如 980032.CNI；按主体代码匹配行）")
    ap.add_argument("--json", nargs="*", default=[],
                    help="neodata MCP 原始响应 JSON 文件（含 result 数组；可多文件分段）")
    ap.add_argument("--table", nargs="*", default=[],
                    help="neodata output markdown 表文件（可多个）")
    ap.add_argument("--name", default="", help="指数名（新建缓存时用）")
    ap.add_argument("--trade-day-only", dest="trade_day_only", action="store_true", default=True,
                    help="按交易日过滤（默认开；B-03 统一闸门）")
    ap.add_argument("--no-trade-day-filter", dest="trade_day_only", action="store_false",
                    help="关闭交易日过滤（调试用；不推荐）")
    a = ap.parse_args()
    if not a.json and not a.table:
        print("用法：--json <文件...> 或 --table <文件...>（至少一个）")
        return 4
    missing = [fp for fp in (a.json + a.table) if not Path(fp).is_file()]
    if missing:
        print(f"输入文件不存在: {missing}", file=sys.stderr)
        return 4

    try:
        rows = _collect(a.json, a.table)
    except (ValueError, json.JSONDecodeError) as e:
        print(f"输入解析失败: {type(e).__name__}: {e}", file=sys.stderr)
        return 4
    base = _base_code(a.code)
    mine = [r for r in rows if _base_code(r["index_code"]) == base]
    if not mine:
        got = sorted({r["index_code"] for r in rows})
        print(f"  [SKIP] neodata 无 {a.code}（主体 {base}）覆盖——输入中的指数: {got or '无'}")
        print("     （覆盖缺口≠数据错误：换 iFinD（ingest_ifind_valuation）或妙想"
              "（fetch_pb_mxdata）；勿把无覆盖静默当已合并）")
        return 0
    name = next((r["name"] for r in mine if r["name"]), a.name)
    # 同日多行取最后出现值（neodata 一日一行，防御性去重）
    by_day: dict[str, dict] = {}
    for r in mine:
        slot = by_day.setdefault(r["trading_day"], {})
        if r["pb"] is not None:
            slot["pb"] = r["pb"]
        if r["pe"] is not None:
            slot["pe"] = r["pe"]

    updated, failed = [], False
    for metric, field in (("pb", "pb"), ("pe", "pe")):
        incoming = {}
        for d, r in by_day.items():
            v = r.get(field)
            if v is not None:
                incoming[d] = round(float(v), 4)
        if not incoming:
            print(f"  [SKIP] {metric.upper()}：neodata 无此指标值（pb_lf/pe_ttm 均空）")
            continue
        # B-03（2026-10-25）：交易日过滤（neodata 侧列名已是「交易日」，此处为统一闸门兜底）
        cal_rep: dict = {}
        if a.trade_day_only:
            import trade_calendar as _tc
            _pairs, cal_rep = _tc.filter_pairs([[d, 0] for d in sorted(incoming)],
                                              a.code, verbose=True)
            _keep = {d for d, _ in _pairs}
            incoming = {d: v for d, v in incoming.items() if d in _keep}
        out_dir = PE_DIR if metric == "pe" else PB_DIR
        path = out_dir / f"{a.code}.json"
        old = load_cache(path)
        before = (old or {}).get("as_of")
        old_series = {}
        for dt, v in (old or {}).get("pb") or []:
            try:
                old_series[_norm_date(dt)] = float(v)
            except (TypeError, ValueError):
                continue
        try:
            res = merge_series(old_series, incoming, label="neodata", has_old=bool(old))
        except MergeError as e:
            print(f"  [FAIL] {metric.upper()}：{e}", file=sys.stderr)
            failed = True
            continue
        report_conflicts(res, metric, label="neodata")
        if res.skipped:
            print(f"  [SKIP] {metric.upper()}：无缺口可补（冲突 {len(res.conflicts)} 个已报告）")
            continue
        try:
            rec = build_record(a.code, name, res.series,
                               f"neodata-mcp-daily-{metric}", metric=metric, old=old,
                               provider="neodata", calib=res.calib)
        except MergeError as e:
            print(f"  [FAIL] {metric.upper()}：{e}", file=sys.stderr)
            failed = True
            continue
        if cal_rep:
            rec["calendar"] = "trade-calendar/2"
            rec["calendar_market"] = cal_rep.get("market")
            rec["nontrading_dropped"] = len(cal_rep.get("dropped") or [])
        save_record(out_dir, a.code, rec)
        updated.append((metric, rec))
        if metric == "pb":
            sync_bands(a.code, rec)
        tag = "合并" if old else "新建"
        print(f"  [OK ] {metric.upper()} {tag}: n={rec['n']} {rec['start']}~{rec['end']} "
              f"as_of {before}→{rec['as_of']} now={rec['now']} 分位={rec['pct_now']}% "
              f"source={rec['source']}")

    if failed:
        print("[FAIL] 存在被拒绝的合并（fail-closed·上表已列明）；缓存未被污染",
              file=sys.stderr)
        return 2
    if not updated:
        print("[OK ] 无缺口可补，缓存已为最新（冲突/口径差异见上方报告）")
        return 0
    print("（免费源·无配额记账；PB 已同步 bands registry，PE 不入）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
