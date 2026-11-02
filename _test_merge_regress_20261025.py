#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
_test_merge_regress_20261025.py —— D-2 泛化回归：新旧合并算法逐位对账
====================================================================
证明 `ingest_ifind_valuation.py`（改调 merge_valuation_gap）与重构前的
`_rebuild` 内嵌算法**行为逐位一致**（含同日保留/中位比值归一/幂等 source/
5y 窗口/as_of 诚实/<100 拒绝/空序列拒绝）。参照实现 = 重构前代码 verbatim 拷贝
（2026-10-07 版，见本文件 _rebuild_reference）。

另跑 merge_valuation_gap 自带自校验（6 项）。

用法：python _test_merge_regress_20261025.py
退出码 0 = 全过；1 = 有差异（禁止合入）。
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import tempfile
from pathlib import Path
from statistics import median

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

WINDOW_DAYS = 365 * 5 + 2


# ── 参照实现：重构前 ingest_ifind_valuation._rebuild（verbatim，勿改）─────────
def _rebuild_reference(old, metric, incoming, code, name_arg, source_tag):
    """补缺口式合并（带跨源口径校准）+ 重算分位五档。incoming: {date: value}。"""
    from fetch_pb_mxdata import q, pct_rank
    series = {}
    if old:
        for dt, v in old.get("pb") or []:
            try:
                series[dt] = float(v)
            except (TypeError, ValueError):
                continue
    missing, ratios, conflicts = {}, [], []
    for d, v in sorted(incoming.items()):
        if d in series:
            ov = series[d]
            if ov:
                ratios.append(v / ov)
                if abs(v - ov) / abs(ov) > 0.05:
                    conflicts.append((d, ov, v))
            continue
        missing[d] = v
    calib = ""
    if missing:
        if not old:
            series.update(missing)
            filled = len(missing)
        elif len(ratios) < 3:
            raise ValueError(f"缺口 {len(missing)} 点但重叠点仅 {len(ratios)}(<3)，"
                             f"无法校准跨源口径 → 拒绝拼接（fail-closed；请拉全序列重建或等妙想恢复）")
        else:
            r_med = median(ratios)
            for d, v in missing.items():
                series[d] = round(v / r_med, 4)
            filled = len(missing)
            calib = f"（缺口 {filled} 点已按重叠期中位比值 {r_med:.4f} 归一到原源口径）"
    else:
        filled = 0
    if not series:
        raise ValueError("合并后序列为空")
    if filled == 0:
        return None
    pts = sorted(series.items())
    end_d = datetime.date.fromisoformat(pts[-1][0])
    start_d = end_d - datetime.timedelta(days=WINDOW_DAYS)
    pts = [(d, v) for d, v in pts if datetime.date.fromisoformat(d) >= start_d]
    if len(pts) < 100:
        raise ValueError(f"合并后序列仅 {len(pts)} 点 (<100)，拒绝以残缺序列覆盖缓存")
    vals = [v for _, v in pts]
    now = round(vals[-1], 4)
    old_src = ((old or {}).get("source") or "").replace("+ifind_incr_calib", "").replace("+ifind_incr", "")
    rec = {
        "index_code": code,
        "index_name": (old or {}).get("index_name") or name_arg or code,
        "source": (old_src + ("+ifind_incr_calib" if calib else "+ifind_incr")).lstrip("+")
                  if old else source_tag,
        "as_of": pts[-1][0],
        "n": len(pts), "start": pts[0][0], "end": pts[-1][0],
        "pb": [[d, v] for d, v in pts],
        "p5": round(q(5, vals), 4), "p20": round(q(20, vals), 4),
        "p50": round(q(50, vals), 4), "p80": round(q(80, vals), 4),
        "p95": round(q(95, vals), 4),
        "now": now, "pct_now": pct_rank(now, vals),
    }
    if metric == "pe":
        rec["metric"] = "pe"
    return rec


def _mk_old(n_days: int = 900, base: float = 2.0, start: str = "2023-01-02"):
    """造一条 900 点的旧缓存（mx 口径）。"""
    d0 = datetime.date.fromisoformat(start)
    pts, i = [], 0
    while len(pts) < n_days:
        d = d0 + datetime.timedelta(days=i)
        i += 1
        if d.weekday() < 5:
            pts.append([d.isoformat(), round(base + 0.0005 * len(pts), 4)])
    return {"index_code": "T0001.CSI", "index_name": "测试指数",
            "source": "mx-ds-mcp-daily-pb", "as_of": pts[-1][0],
            "n": len(pts), "pb": pts}


def _mk_incoming(old_pts, extra_days=30, ratio=1.08):
    """造新源观测：与旧序列重叠期末段 + extra_days 个新点（整体 ratio 倍）。"""
    by_day = {d: v for d, v in old_pts}
    days = sorted(by_day)
    tail = days[-40:]
    inc = {}
    for d in tail:
        inc[d] = round(by_day[d] * ratio, 4)
    last = datetime.date.fromisoformat(days[-1])
    k = 0
    while len([d for d in inc if d > days[-1]]) < extra_days:
        last += datetime.timedelta(days=1)
        if last.weekday() < 5:
            inc[last.isoformat()] = round(2.0 * ratio + 0.0001 * k, 4)
            k += 1
    return inc


def _norm(rec):
    """对账前归一：pb 键统一为 list（两边本来就一致，仅防御性）。"""
    r = dict(rec)
    r["pb"] = [[d, v] for d, v in r["pb"]]
    return r


def main() -> int:
    from merge_valuation_gap import (MergeError, build_record, incr_source,
                                     merge_series)

    fails = []

    def check(name, cond, detail=""):
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
        if not cond:
            fails.append(name)

    print("== 1. merge_valuation_gap 自带自校验 ==")
    import merge_valuation_gap as mvg
    check("自校验 6 项", mvg._selfcheck() == 0)

    print("== 2. 新旧算法逐位对账（正常缺口合并·ratio=1.08）==")
    old = _mk_old()
    old_series = {d: float(v) for d, v in old["pb"]}
    inc = _mk_incoming(old["pb"], extra_days=30, ratio=1.08)
    ref = _rebuild_reference(old, "pb", inc, old["index_code"], old["index_name"],
                             "ifind-mcp-daily-pb")
    res = merge_series(old_series, inc, label="ifind", has_old=True)
    new = build_record(old["index_code"], old["index_name"], res.series,
                       "ifind-mcp-daily-pb", metric="pb", old=old,
                       provider="ifind", calib=res.calib)
    check("record 逐位一致", _norm(ref) == _norm(new),
          f"n={new['n']} source={new['source']}")
    check("同日保留原源", all(new_dict_v == old_series[d]
                              for d, new_dict_v in new["pb"] if d in old_series))
    check("归一后缀 _calib", new["source"].endswith("+ifind_incr_calib"), new["source"])

    print("== 3. 边界：无缺口（filled=0）==")
    inc_same = {d: v for d, v in list(inc.items())[:40]}
    ref_none = _rebuild_reference(old, "pb", inc_same, old["index_code"],
                                  old["index_name"], "ifind-mcp-daily-pb")
    res2 = merge_series(old_series, inc_same, has_old=True)
    check("参照返回 None / 新管线 skipped", ref_none is None and res2.skipped)

    print("== 4. 边界：重叠<3 fail-closed ==")
    # B-03（2026-10-25）：原夹具 2099-01-04 是**周日**，会先被「周末 fail-closed」拦住，
    # 导致文案对比失效（两个闸都对，但不是同一个闸）。改用周一/周二，专测重叠<3。
    inc_tiny = {"2099-01-05": 2.2, "2099-01-06": 2.2}
    ref_err = new_err = None
    try:
        _rebuild_reference(old, "pb", inc_tiny, old["index_code"], old["index_name"], "x")
    except ValueError as e:
        ref_err = str(e)
    try:
        merge_series(old_series, inc_tiny, has_old=True)
    except MergeError as e:
        new_err = str(e)
    check("两侧均 fail-closed 且文案一致", ref_err is not None and ref_err == new_err,
          new_err or "")

    print("== 5. 边界：新缓存直落（source=源标签·无 incr 后缀）==")
    base = _mk_old(n_days=900)
    inc_new = {d: float(v) for d, v in base["pb"]}          # 新源全序列（ratio=1.0）
    last = datetime.date.fromisoformat(sorted(inc_new)[-1])
    k = 0
    while k < 30:                                            # + 30 个更新点
        last += datetime.timedelta(days=1)
        if last.weekday() < 5:
            inc_new[last.isoformat()] = round(2.0 + 0.0001 * k, 4)
            k += 1
    ref_new = _rebuild_reference(None, "pb", inc_new, "T0002.CSI", "新指数",
                                 "ifind-mcp-daily-pb")
    res_new = merge_series(None, inc_new, has_old=False)
    new_new = build_record("T0002.CSI", "新指数", res_new.series, "ifind-mcp-daily-pb",
                           metric="pb", old=None, provider="ifind", calib="")
    check("新缓存 record 逐位一致", _norm(ref_new) == _norm(new_new))
    check("新缓存 source 无 incr 后缀", new_new["source"] == "ifind-mcp-daily-pb",
          new_new["source"])

    print("== 6. 边界：合并后 <100 点拒绝 ==")
    short_old = _mk_old(n_days=60)
    short_series = {d: float(v) for d, v in short_old["pb"]}
    inc_short = _mk_incoming(short_old["pb"], extra_days=20, ratio=1.05)
    ref_short = new_short = None
    try:
        _rebuild_reference(short_old, "pb", inc_short, "T0003.CSI", "短", "x")
    except ValueError as e:
        ref_short = str(e)
    try:
        r = merge_series(short_series, inc_short, has_old=True)
        build_record("T0003.CSI", "短", r.series, "x", old=short_old, provider="ifind")
    except MergeError as e:
        new_short = str(e)
    check("两侧均拒绝且文案一致", ref_short is not None and ref_short == new_short,
          new_short or "")

    print("== 7. 幂等标注（反复合并不累加）==")
    s = incr_source("mx-ds-mcp-daily-pb+ifind_incr", "ifind", calib=False)
    check("剥离旧后缀后重贴", s == "mx-ds-mcp-daily-pb+ifind_incr", s)

    print()
    if fails:
        print(f"❌ 回归失败 {len(fails)} 项: {fails}")
        return 1
    print("✅ D-2 泛化回归全过：新旧算法行为逐位一致（7 组对账）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
