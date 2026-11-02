#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XYD 供需板块框架 —— 数据双源验证 Gate（三道闸之②）
====================================================
按【活跃估值主锚】校验估值数据的双源交叉 + 手算兜底是否达标：

  · 主锚 = PB        → 校验 pb_now / pb_manual / pb_sources(≥2，差异<5%) / pb_series末值≈手算
  · 主锚 = PE        → 校验 pe_now / pe_manual / pe_sources(≥2，差异<5%) / pe_series末值≈手算
  · 主锚 = BLEND     → 上述 PB 与 PE 两者【同时】达标（缺一即为 FAIL）

阈值与 §四 协议①②③ 一致（手算差 ≤20%、多源差 ≤5%）。
可独立运行：python gate_dual_source.py --selfcheck
也被 discipline.py 在 run() 内调用，结果挂到 Verdict.gates['dual_source']。
"""
from __future__ import annotations

import sys
from typing import Any, Dict

import gate_common as gc

# 与 params.json gates 保持一致（gate 独立、不依赖 discipline 以免循环导入）
PB_MANUAL_MAX_DIFF_PCT = 20.0
PB_CROSS_SRC_MAX_DIFF_PCT = 5.0


def _pct_diff(a: float, b: float) -> float:
    if a == 0 and b == 0:
        return 0.0
    base = abs(a) if abs(a) >= abs(b) else abs(b)
    if base == 0:
        return 0.0
    return abs(a - b) / base * 100.0


def _check_one(now, manual, sources, series, tag: str, r: gc.GateResult) -> None:
    if now is None:
        r.fail(f"{tag} 缺少当前值（{tag.lower()}_now）→ 赔率无法定位")
        return
    if manual is None:
        r.fail(f"{tag} 缺少手算兜底（{tag.lower()}_manual）→ 无唯一基准（§四 协议①）")
        return
    d_manual = _pct_diff(now, manual)
    if d_manual > PB_MANUAL_MAX_DIFF_PCT:
        r.fail(f"{tag} 与手算差 {d_manual:.1f}% > 20% → 字段口径错配（§四 协议②）")
        return
    vals = [x for x in (sources or {}).values() if isinstance(x, (int, float))]
    if len(vals) < 2:
        r.fail(f"{tag} 仅单源（{len(vals)} 个）→ 未满足≥2 独立源差异<5%（§四 协议②）")
        return
    spread = _pct_diff(max(vals), min(vals))
    if spread > PB_CROSS_SRC_MAX_DIFF_PCT:
        r.fail(f"{tag} 多源分歧 {spread:.1f}% > 5% → 不采信（§四 协议②④）")
        return
    if series:
        d_tail = _pct_diff(series[-1], manual)
        if d_tail > PB_MANUAL_MAX_DIFF_PCT:
            r.fail(f"{tag} 序列末值与手算差 {d_tail:.1f}% → 序列口径错（§四 协议③）")
            return
    r.note(f"{tag} 双源一致(差{spread:.1f}%)+手算校验✓")


def validate_dual_source(inp: Dict[str, Any]) -> gc.GateResult:
    raw_anchor = inp.get("valuation_anchor")
    r = gc.GateResult(name="数据双源验证(dual_source)")
    # 2026-09-07 修复：未声明锚不得静默默认 PB（同类锚定 bug）。
    # discipline.run() 会先自动分类再调本闸，此处空锚=上游未走引擎 → 直接 FAIL。
    if raw_anchor is None or not str(raw_anchor).strip():
        r.fail("估值主锚未声明（须 PB/PE/BLEND；留空应由引擎按 valuation_anchor.classify 自动推断后再校验）")
        return r
    anchor = str(raw_anchor).upper()
    is_pb = anchor in ("PB", "BLEND")
    is_pe = anchor in ("PE", "BLEND")

    if anchor not in ("PB", "PE", "BLEND"):
        r.fail(f"估值主锚声明非法：{anchor}（须 PB/PE/BLEND）")
        return r

    if is_pb:
        _check_one(inp.get("pb_now"), inp.get("pb_manual"), inp.get("pb_sources"),
                   inp.get("pb_series"), "PB", r)
    if is_pe:
        _check_one(inp.get("pe_now"), inp.get("pe_manual"), inp.get("pe_sources"),
                   inp.get("pe_series"), "PE", r)
    if not (is_pb or is_pe):
        r.fail("未声明任何可校验的估值主锚")
    return r


# =============================================================================
# 自检
# =============================================================================
def _selfcheck() -> int:
    print("== gate_dual_source 自检 ==")
    fails = 0

    def run(name, d, expect_ok):
        nonlocal fails
        r = validate_dual_source(d)
        ok = (r.ok == expect_ok)
        if not ok:
            fails += 1
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:<34} level={r.level} "
              f"expect_ok={expect_ok} | {r.errors or r.info}")
        return r

    # 通过：PB 双源达标
    run("PB-双源达标", {
        "valuation_anchor": "PB", "pb_now": 1.36, "pb_manual": 1.35,
        "pb_sources": {"a": 1.36, "b": 1.37}, "pb_series": [1.0, 1.2, 1.35]},
        True)
    # 通过：PE 双源达标
    run("PE-双源达标", {
        "valuation_anchor": "PE", "pe_now": 28.0, "pe_manual": 27.5,
        "pe_sources": {"a": 28.0, "b": 27.8}, "pe_series": [25, 27, 27.5]},
        True)
    # 失败：PB 单源
    run("PB-仅单源", {
        "valuation_anchor": "PB", "pb_now": 1.36, "pb_manual": 1.35,
        "pb_sources": {"a": 1.36}},
        False)
    # 失败：PE 缺手算
    run("PE-缺手算兜底", {
        "valuation_anchor": "PE", "pe_now": 28.0, "pe_sources": {"a": 28.0, "b": 27.8}},
        False)
    # 失败：BLEND 仅 PB 达标、PE 单源
    run("BLEND-PE未达标", {
        "valuation_anchor": "BLEND",
        "pb_now": 1.36, "pb_manual": 1.35, "pb_sources": {"a": 1.36, "b": 1.37},
        "pe_now": 28.0, "pe_manual": 27.5, "pe_sources": {"a": 28.0}},
        False)
    # 通过：BLEND 双锚均达标
    run("BLEND-双锚达标", {
        "valuation_anchor": "BLEND",
        "pb_now": 1.36, "pb_manual": 1.35, "pb_sources": {"a": 1.36, "b": 1.37},
        "pe_now": 28.0, "pe_manual": 27.5, "pe_sources": {"a": 28.0, "b": 27.8}},
        True)
    # 失败：主锚非法
    run("主锚非法", {"valuation_anchor": "XYZ"}, False)
    # 失败：主锚未声明（2026-09-07 新增：不得静默默认 PB）
    run("主锚未声明", {"pb_now": 1.36, "pb_manual": 1.35,
                       "pb_sources": {"a": 1.36, "b": 1.37}}, False)

    print(f"\n双源闸自检：{8-fails}/8 通过" + ("" if fails == 0 else f"，{fails} 项失败"))
    return 1 if fails else 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass
    rc = _selfcheck()
    sys.exit(rc)
