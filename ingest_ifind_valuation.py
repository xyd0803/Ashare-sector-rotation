#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ingest_ifind_valuation.py —— 把 iFinD MCP 取到的指数估值日频数据 增量合并进估值缓存
================================================================================
背景（2026-10-07）：iFinD MCP 已充会员，与妙想升级为**同级别估值数据源**。
妙想脚本刷新器（fetch_pb_mxdata.py）失败（配额墙/网络/ProxyError）时，Agent 改用
iFinD MCP `get_security_indicators(market=指数)` 取数 → 存 markdown 表 → 本脚本落盘。
管线/闸门对缓存来源无感知（同构），source 字段如实标注 ifind。

节约原则（用户明确「不要铺张浪费」）：
  · 妙想优先（脚本自动刷新），iFinD 只在妙想失败/缺口时用——不做每次强制双拉；
  · **增量合并**：缓存已存在时只让 MCP 拉缺口窗口（几十个点），本脚本按日期 merge
    进现有序列后重算分位——避免每次重拉 5 年 ~1200 点的大响应；
  · 一次 iFinD 调用同时返回 PB+PE 两列 → 一次喂入，两个缓存同时更新。

2026-10-25（D-2/D-3 批次）：
  · 合并语义泛化 → 改调 `merge_valuation_gap.py`（非破坏性合并唯一实现，
    与 neodata 等未来 ingest 共用同一套口径防线；行为与此前 _rebuild 逐位一致）；
  · MCP 取数路径记账：Agent 经 MCP 直调不经 call.py（无闸无账）→ 新增 `--charge`
    （默认 1：产出本表的 MCP 调用数）由 ifind_budget 落账；`--no-charge` 用于
    表格来自缓存/复用（不耗配额）的场景。tag 必填（对齐 require_tag 纪律）。

输入：
  --table  iFinD 返回的 markdown 表格（文件路径；缺省读 stdin）
  --code   指数代码（缓存命名键，用技能侧代码如 980032.CNI；iFinD 侧 .SZ/.CSI 后缀不落盘）
  --name   指数名（新建缓存时用；已有缓存自动沿用原名）
  --charge N / --no-charge / --tag T   MCP 路径配额记账（见上）
输出：
  data/index_daily_pb/{CODE}.json / data/index_daily_pe/{CODE}.json（同构，series 键仍为 "pb"）
  PB 另同步 data/meta/pb_daily_bands.json
诚实性：
  · as_of 一律取合并后**真实序列末值**（不虚报新鲜）；
  · source 标注：新缓存 `ifind-mcp-daily-{metric}`；合并 `原source+ifind_incr(_calib)`。
"""
from __future__ import annotations
import argparse, json, os, re, sys, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
# 复用 fetcher 的分位/百分位/bands 定义（单一真相源，不复制）
from fetch_pb_mxdata import q, pct_rank, BANDS, DATA  # noqa: E402,F401
# 非破坏性合并唯一实现（D-2 泛化）
from merge_valuation_gap import (MergeError, build_record, load_cache,  # noqa: E402
                                 merge_series, report_conflicts, save_record,
                                 sync_bands)

PB_DIR = DATA / "index_daily_pb"
PE_DIR = DATA / "index_daily_pe"
WINDOW_DAYS = 365 * 5 + 2  # 与 fetcher 同：5 年窗口（保留供外部引用）

_DATE_RE = re.compile(r"^\d{8}$")


def _norm_date(s: str) -> str:
    """8 位(20260930)/带斜杠 → YYYY-MM-DD；其余原样（供字典序比较与去重）。"""
    s = str(s).strip().replace("/", "-")
    if _DATE_RE.match(s):
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s


def parse_ifind_table(md: str):
    """解析 iFinD markdown 表 → {date: {"pb": v|None, "pe": v|None, "name": str}}。

    表头形如 |证券代码|证券简称|日期|市净率（PB）|市盈率（PE，TTM）|，
    按表头关键词定位列（PB→市净率，PE→市盈率），缺列跳过。
    """
    rows = []
    header = None
    for line in md.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if header is None and any("日期" in c for c in cells):
            header = cells
            continue
        if line.startswith("|-") or set(line) <= {"|", "-", " ", ":"}:
            continue
        if header and len(cells) == len(header):
            rows.append(dict(zip(header, cells)))
    if not rows:
        raise ValueError("未解析到数据行（表头须含『日期』列）")
    pb_col = next((c for c in header if "市净率" in c), None)
    pe_col = next((c for c in header if "市盈率" in c), None)
    name_col = next((c for c in header if "简称" in c), None)
    date_col = next((c for c in header if "日期" in c), None)
    if not date_col:
        raise ValueError("表头缺『日期』列")

    pts: dict[str, dict] = {}
    for r in rows:
        d = _norm_date(r[date_col])
        try:
            datetime.date.fromisoformat(d)
        except ValueError:
            continue
        rec = pts.setdefault(d, {"pb": None, "pe": None, "name": ""})
        rec["name"] = rec["name"] or (r.get(name_col, "") if name_col else "")
        for key, col in (("pb", pb_col), ("pe", pe_col)):
            if col and r.get(col):
                try:
                    rec[key] = round(float(str(r[col]).replace("倍", "").strip()), 4)
                except ValueError:
                    pass
    return pts


def _charge_mcp(n: int, tag: str, note: str) -> None:
    """MCP 路径记账（D-3）：Agent 直调 iFinD MCP 不经 call.py，此处落账。"""
    try:
        import sds_ifind_budget
        sds_ifind_budget.charge("ingest_ifind_valuation", note=note, n=n, tag=tag)
        print(f"  [记账] iFinD MCP 调用 {n} 次已入账（tag={tag}）· "
              f"{sds_ifind_budget.budget_report()}")
    except Exception as e:
        print(f"  [WARN] 配额记账失败（不影响合并）: {type(e).__name__}: {e}",
              file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description="iFinD 指数估值增量合并进缓存")
    ap.add_argument("--table", default="", help="iFinD markdown 表格文件（缺省读 stdin）")
    ap.add_argument("--code", required=True, help="技能侧指数代码（缓存键，如 980032.CNI）")
    ap.add_argument("--name", default="", help="指数名（新建缓存时用）")
    ap.add_argument("--tag", default="sds_ingest",
                    help="配额归因 tag（默认 sds_ingest；批次外调用必须显式归因）")
    ap.add_argument("--charge", type=int, default=1, metavar="N",
                    help="产出本表的 iFinD MCP 调用次数（默认 1；记账用）")
    ap.add_argument("--no-charge", action="store_true",
                    help="表格来自缓存/复用（不耗配额）→ 不记账")
    ap.add_argument("--trade-day-only", dest="trade_day_only", action="store_true", default=True,
                    help="按交易日过滤（默认开；B-03：iFinD 返回日历日含周末/节假日顺延值）")
    ap.add_argument("--no-trade-day-filter", dest="trade_day_only", action="store_false",
                    help="关闭交易日过滤（仅调试/对比用；不推荐——会污染分位）")
    a = ap.parse_args()

    md = Path(a.table).read_text(encoding="utf-8") if a.table else sys.stdin.read()
    pts = parse_ifind_table(md)
    name = next((v["name"] for v in pts.values() if v["name"]), a.name)

    # B-03（2026-10-25）：交易日过滤（此前靠人工在合并前过滤 = 手抬不变量，漏一次就污染分位）
    cal_rep: dict = {}
    if a.trade_day_only:
        import trade_calendar as _tc
        _pairs, cal_rep = _tc.filter_pairs([[d, 0] for d in sorted(pts)], a.code, verbose=True)
        kept = {d for d, _ in _pairs}
        for d in [d for d in pts if d not in kept]:
            pts.pop(d, None)
        if cal_rep.get("reason"):
            print(f"  [交易日过滤] {cal_rep['reason']}", file=sys.stderr)
        if not pts:
            print("[FAIL] 交易日过滤后无有效数据点 → 拒绝落盘（fail-closed）", file=sys.stderr)
            return 2
    else:
        print("  [WARN] --no-trade-day-filter：未做交易日过滤，非交易日点将进入分位计算",
              file=sys.stderr)

    if not a.no_charge and a.charge > 0:
        _charge_mcp(a.charge, a.tag, f"{a.code} iFinD 估值表 ingest")

    updated, failed = [], False
    for metric in ("pb", "pe"):
        incoming = {d: v[metric] for d, v in pts.items() if v[metric] is not None}
        if not incoming:
            print(f"  [SKIP] {metric.upper()}：表格无此列/无有效值")
            continue
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
            res = merge_series(old_series, incoming, label="ifind", has_old=bool(old))
        except MergeError as e:
            print(f"  [FAIL] {metric.upper()}：{e}", file=sys.stderr)
            failed = True
            continue
        report_conflicts(res, metric, label="ifind")
        if res.skipped:
            print(f"  [SKIP] {metric.upper()}：无缺口可补（冲突 {len(res.conflicts)} 个已报告）")
            continue
        try:
            rec = build_record(a.code, name, res.series,
                               f"ifind-mcp-daily-{metric}", metric=metric, old=old,
                               provider="ifind", calib=res.calib)
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
        # PB 同步 bands registry（与 fetcher 行为一致；PE 不入）
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
