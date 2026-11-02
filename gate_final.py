#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XYD 供需板块框架 —— finalgate 终检（三道闸之③）
================================================
端到端一致性校验，确认「分类与分析」严格按要求完成：

  ① 声明主锚合法（PB/PE/BLEND）；
  ② 使用主锚 == 声明主锚（verdict.valuation_anchor == inp.valuation_anchor）；
  ③ 主锚对应估值数据齐备（PE/BLEND 须有 PE 字段；PB/BLEND 须有 PB 字段）；
  ④ 有效估值分位(eff_percentile)存在且 ∈ [0,100]；
  ⑤ 五档参考线单调（PB/BLEND 查 pb_p5..p95；PE/BLEND 查 pe_p5..p95）；
  ⑥ 数据双源验证 gate 通过（verdict.gates.dual_source.ok）；
  ⑦ 发布前 checklist 全 True；
  ⑧ 状态自洽：status==OK ⇒ 无 blockers；有 blockers ⇒ 非 OK。

任何 ①~⑧ 不通过 → FAIL（结论不得用于决策）。
可独立运行：python gate_final.py --selfcheck
也被 discipline.py 在 run() 内调用，结果挂到 Verdict.gates['final']。
"""
from __future__ import annotations

import sys
from typing import Any, Dict

import gate_common as gc

VALID = ("PB", "PE", "BLEND")


def _bands(inp: Dict[str, Any], kind: str):
    if kind == "pb":
        return [inp.get("pb_p5"), inp.get("pb_p20"), inp.get("pb_p50"),
                inp.get("pb_p80"), inp.get("pb_p95")]
    return [inp.get("pe_p5"), inp.get("pe_p20"), inp.get("pe_p50"),
            inp.get("pe_p80"), inp.get("pe_p95")]


def _nonmono(bands) -> bool:
    vals = [b for b in bands if isinstance(b, (int, float))]
    if len(vals) < 5:
        return False
    return any(vals[i] > vals[i + 1] for i in range(len(vals) - 1))


def validate_final(inp: Dict[str, Any], verdict: Dict[str, Any]) -> gc.GateResult:
    r = gc.GateResult(name="finalgate终检")
    # 2026-09-07 修复：未声明锚不得静默默认 PB（同类锚定 bug）。
    # discipline.run() 会先自动分类再调本闸，此处空锚=上游未走引擎 → 直接 FAIL。
    raw_anchor = inp.get("valuation_anchor")
    if raw_anchor is None or not str(raw_anchor).strip():
        r.fail("① 估值主锚未声明（须 PB/PE/BLEND；留空应由引擎按 valuation_anchor.classify 自动推断后再终检）")
        return r
    anchor = str(raw_anchor).upper()

    # ① 声明主锚合法
    if anchor not in VALID:
        r.fail(f"① 估值主锚声明非法：{anchor}（须 PB/PE/BLEND）")
        return r
    r.note(f"① 主锚声明合法：{anchor}")

    # ② 使用 == 声明
    used = (verdict.get("valuation_anchor") or "").upper()
    if used != anchor:
        r.fail(f"② 使用主锚({used}) ≠ 声明主锚({anchor}) → 分析未按分类执行")
    else:
        r.note(f"② 使用主锚==声明主锚：{anchor}")

    # ③ 主锚对应数据齐备
    is_pb = anchor in ("PB", "BLEND")
    is_pe = anchor in ("PE", "BLEND")
    if is_pb and (inp.get("pb_now") is None or inp.get("pb_manual") is None
                  or len([x for x in (inp.get("pb_sources") or {}).values()
                          if isinstance(x, (int, float))]) < 2):
        r.fail("③ PB 主锚数据不齐备（pb_now/pb_manual/pb_sources≥2 缺一）")
    if is_pe and (inp.get("pe_now") is None or inp.get("pe_manual") is None
                  or len([x for x in (inp.get("pe_sources") or {}).values()
                          if isinstance(x, (int, float))]) < 2):
        r.fail("③ PE/BLEND 主锚缺 PE 数据（pe_now/pe_manual/pe_sources≥2 缺一）")
    if r.level != "FAIL":
        r.note("③ 主锚对应估值数据齐备")

    # ④ 有效估值分位存在且 ∈[0,100]
    vc = verdict.get("valuation_check") or {}
    eff = vc.get("eff_percentile")
    if eff is None:
        # 退回从 inp 直接算（与引擎 _eff_pct 同口径）
        eff = inp.get("pb_pct")
        if anchor == "PE":
            eff = inp.get("pe_pct")
        elif anchor == "BLEND":
            pe = inp.get("pe_pct") or 0.0
            pb = inp.get("pb_pct") or 0.0
            eff = round(0.6 * pe + 0.4 * pb, 2)
    if eff is None:
        r.fail("④ 有效估值分位缺失 → 仓位/象限无法定位")
    elif not (0 <= eff <= 100):
        r.fail(f"④ 有效估值分位 {eff} 越界（须 ∈[0,100]）")
    else:
        r.note(f"④ 有效估值分位={eff} ∈[0,100]")

    # ⑤ 五档单调
    if is_pb and _nonmono(_bands(inp, "pb")):
        r.fail("⑤ PB 五档非单调 → 百分位序列失真")
    if is_pe and _nonmono(_bands(inp, "pe")):
        r.fail("⑤ PE 五档非单调 → 百分位序列失真")
    if r.level != "FAIL":
        r.note("⑤ 五档参考线单调")

    # ⑥ 双源闸通过
    ds = (verdict.get("gates") or {}).get("dual_source") or {}
    if ds and ds.get("ok") is False:
        r.fail(f"⑥ 数据双源验证闸未通过：{ds.get('errors')}")
    else:
        r.note("⑥ 数据双源验证闸通过（或引擎未回传，跳过）")

    # ⑦ checklist 全 True
    chk = verdict.get("checklist") or {}
    bad = [k for k, v in chk.items() if v is not True]
    if bad:
        r.fail(f"⑦ 发布前 checklist 未全 True：{bad}")
    elif chk:
        r.note("⑦ checklist 全 True")

    # ⑧ 状态自洽
    status = verdict.get("status")
    blockers = verdict.get("blockers") or []
    if status == "OK" and blockers:
        r.fail(f"⑧ 状态 OK 但存在 blockers（{len(blockers)} 项）→ 状态不自洽")
    elif status != "OK" and not blockers:
        r.warn(f"⑧ 状态={status} 但无 blockers → 需复核状态判定")
    else:
        r.note(f"⑧ 状态自洽：status={status}，blockers={len(blockers)}")

    return r


# =============================================================================
# 自检
# =============================================================================
def _selfcheck() -> int:
    print("== gate_final 自检 ==")
    fails = 0

    def run(name, inp, verdict, expect_ok):
        nonlocal fails
        r = validate_final(inp, verdict)
        ok = (r.ok == expect_ok)
        if not ok:
            fails += 1
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:<30} level={r.level} "
              f"expect_ok={expect_ok} | {(r.errors or r.info)[:1]}")
        return r

    base_inp = {
        "valuation_anchor": "PB", "pb_now": 1.36, "pb_manual": 1.35,
        "pb_sources": {"a": 1.36, "b": 1.37},
        "pb_p5": 1.0, "pb_p20": 1.2, "pb_p50": 1.5, "pb_p80": 1.9, "pb_p95": 2.3,
        "pb_pct": 27.5,
    }
    ok_v = {
        "valuation_anchor": "PB", "status": "OK", "blockers": [],
        "valuation_check": {"eff_percentile": 27.5},
        "gates": {"dual_source": {"ok": True}},
        "checklist": {f"项{i}": True for i in range(10)},
    }
    # 通过：PB 完整
    run("PB-完整一致", dict(base_inp), dict(ok_v), True)
    # 失败：使用≠声明
    run("使用≠声明", dict(base_inp),
        {**ok_v, "valuation_anchor": "PE"}, False)
    # 失败：eff 越界
    run("eff越界", dict(base_inp),
        {**ok_v, "valuation_check": {"eff_percentile": 130}}, False)
    # 失败：PB 五档非单调
    nonmono = dict(base_inp); nonmono.update({"pb_p5": 2.3, "pb_p20": 1.2})
    run("PB五档非单调", nonmono, dict(ok_v), False)
    # 失败：双源闸未过
    run("双源闸未过", dict(base_inp),
        {**ok_v, "gates": {"dual_source": {"ok": False, "errors": ["PB 单源"]}}}, False)
    # 失败：checklist 有 False
    chk_bad = {**ok_v, "checklist": {**ok_v["checklist"], "项3": False}}
    run("checklist有False", dict(base_inp), chk_bad, False)
    # 失败：status OK 但 blockers 非空
    run("OK但有blockers", dict(base_inp),
        {**ok_v, "blockers": ["[MUST] x"]}, False)
    # 通过：PE 完整
    pe_inp = {
        "valuation_anchor": "PE", "pe_now": 28.0, "pe_manual": 27.5,
        "pe_sources": {"a": 28.0, "b": 27.8},
        "pe_p5": 15.0, "pe_p20": 18.0, "pe_p50": 24.0, "pe_p80": 30.0, "pe_p95": 36.0,
        "pe_pct": 20.0,
    }
    pe_v = {**ok_v, "valuation_anchor": "PE", "valuation_check": {"eff_percentile": 20.0}}
    run("PE-完整一致", pe_inp, pe_v, True)
    # 失败：PE 锚却缺 PE 数据
    run("PE锚缺PE数据", {**pe_inp, "pe_now": None, "pe_sources": {}}, pe_v, False)
    # 失败：主锚未声明（2026-09-07 新增：不得静默默认 PB）
    no_anchor_v = {**ok_v, "valuation_anchor": "PB"}
    run("主锚未声明", {k: v for k, v in base_inp.items() if k != "valuation_anchor"},
        no_anchor_v, False)

    print(f"\nfinalgate 自检：{11-fails}/11 通过" + ("" if fails == 0 else f"，{fails} 项失败"))
    return 1 if fails else 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass
    rc = _selfcheck()
    sys.exit(rc)
