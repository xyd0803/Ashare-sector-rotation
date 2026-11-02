#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XYD 供需板块框架 —— 压力检验 (Stress Test)
=========================================
目的：在红队（对抗性"骗过引擎"）之外，做**结构性稳健性**验证。
红队问"坏输入能不能挡住"，压力检验问"正常输入在扰动下结论稳不稳"。

覆盖
  S1 五档赔率单调性不变量（非单调输入必须被拦截，单调输入不得误杀）
  S2 参数 ±20% 敏感性扰动翻转率（仅扰"修正/仓位"调参杆，测结论脆弱度）
  S3 蒙特卡洛 2000 组不变量（胜率∈[0,90]/None、仓位∈[0,40]%、status枚举、零异常）
  S4 幂等性（同输入两次运行结论逐字段一致）
  S5 参数指纹稳定性（params.json 指纹可复现，且与 params.lock 一致）

退出码：任何 [MUST] 不变量破坏 → 1；仅 [ADVISORY] 告警（如敏感性偏高）→ 仍 0。

运行
  python stress_test.py            # 全量 2000 组蒙特卡洛
  python stress_test.py --mc 500   # 调小蒙特卡洛规模
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import random
import sys
from typing import Any, Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from discipline import evaluate, load_params, ENGINE_VERSION                 # noqa: E402
import guards                                                                 # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")                  # type: ignore[attr-defined]
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
P = load_params()


def _results(mc: int) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    fails: List[str] = []

    # ----------------------------------------------------------------- S1 单调性
    print("--- S1 五档赔率单调性不变量 ---")
    # 1a 单调输入：不得触发"非单调" blocker
    mono = {"sector": "M", "as_of": "2026-06-30", "asset_class": "sector",
            "index_code": "X", "demand": "growth", "volume": "up", "price": "up",
            "vp_basis": "industry_cycle",
            "industry_volume_metric": "测试行业产量同比(行业供需周期口径)",
            "industry_price_metric": "测试行业核心品价格(行业供需周期口径)",
            "pb_now": 1.50, "pb_manual": 1.50, "pb_sources": {"a": 1.50, "b": 1.51},
            "pb_pct": 40.0, "pb_pct_period": "5y", "pb_pct_10y": 35.0,
            "pb_p5": 0.90, "pb_p20": 1.10, "pb_p50": 1.50, "pb_p80": 1.90, "pb_p95": 2.30,
            "imagination": "B"}
    v1 = evaluate(mono)
    mono_ok = not any("非单调" in b for b in v1["blockers"])
    print(f"  [{'PASS' if mono_ok else 'FAIL'}] 单调五档不被误杀")
    if not mono_ok:
        fails.append("S1a 单调五档被误判非单调")

    # 1b 非单调输入：必须触发"非单调" blocker
    bad = dict(mono, pb_p50=1.60, pb_p80=1.30)   # P80 < P50
    v2 = evaluate(bad)
    nonmono_ok = any("非单调" in b for b in v2["blockers"])
    print(f"  [{'PASS' if nonmono_ok else 'FAIL'}] 非单调五档被拦截")
    if not nonmono_ok:
        fails.append("S1b 非单调五档未被拦截")
    out["S1"] = "PASS" if (mono_ok and nonmono_ok) else "FAIL"

    # ----------------------------------------------------------------- S2 敏感性
    print("\n--- S2 参数 ±20% 敏感性扰动翻转率（仅扰修正/仓位调参杆）---")
    # 仅扰"修正项 + 仓位系数"——这些是调参杆；基础胜率映射是机械锚不扰。
    perturb_paths = [
        ["win_adjust", "forward_up"], ["win_adjust", "forward_down"],
        ["win_adjust", "pb_pct_low"], ["win_adjust", "pb_pct_high"],
        ["win_adjust", "imagination_S"], ["win_adjust", "imagination_A"],
        ["win_adjust", "imagination_C"], ["win_adjust", "crowding"],
        ["win_adjust", "catalyst"], ["win_adjust", "extreme_optimistic_hit"],
        ["win_adjust", "value_trap"],
        ["position", "base_by_pb_pct", "excellent"],
        ["position", "base_by_pb_pct", "neutral"],
        ["position", "base_by_pb_pct", "high"],
        ["position", "imagination_coef", "S"], ["position", "imagination_coef", "A"],
        ["position", "imagination_coef", "B"], ["position", "imagination_coef", "C"],
        ["position", "vp_coef", "up|up"], ["position", "vp_coef", "up|flat"],
        ["position", "vp_coef", "flat|up"], ["position", "vp_coef", "down|up"],
        ["position", "forward_coef", "up"], ["position", "forward_coef", "flat"],
        ["position", "forward_coef", "down"],
    ]

    # 构造在决策边界附近的用例，灵敏性才看得出来
    def near_case(i: int) -> Dict[str, Any]:
        # 在胜率/分位临界点附近游走
        pb = 28.0 + (i % 7) * 9.0          # 28~82 跨 30/60/70 边界
        win_base = ["up|up", "up|flat", "flat|up", "down|up", "flat|flat", "down|flat"][i % 6]
        v, pr = win_base.split("|")
        return {"sector": f"S{i}", "as_of": "2026-06-30", "asset_class": "sector",
                "index_code": "X", "demand": "growth", "volume": v, "price": pr,
                "forward": random.choice(["up", "flat", "down"]),
                "vp_basis": "industry_cycle",
                "industry_volume_metric": "测试行业产量同比(行业供需周期口径)",
                "industry_price_metric": "测试行业核心品价格(行业供需周期口径)",
                "pb_now": 1.40, "pb_manual": 1.40, "pb_sources": {"a": 1.40, "b": 1.41},
                "pb_pct": pb, "pb_pct_period": "5y", "pb_pct_10y": pb - 5.0,
                "pb_p5": 0.90, "pb_p20": 1.10, "pb_p50": 1.50, "pb_p80": 1.90, "pb_p95": 2.30,
                "imagination": random.choice(["S", "A", "B", "C"])}
    cases = [near_case(i) for i in range(24)]

    def evaluator(c, scale):
        pp = guards.scale_numeric_thresholds(P, scale, perturb_paths)
        from discipline import DisciplineEngine
        v = DisciplineEngine(pp).run(
            __import__("discipline").SectorInput.from_dict(c)).to_dict()
        return (v["win_rate"], v["quadrant"])

    rep = guards.sensitivity_flip_rate(cases, evaluator,
                                       perturb_pct=P["guards"]["sensitivity_perturb_pct"])
    fragile = rep["flip_rate"] > P["guards"]["sensitivity_flip_rate_fragile"]
    print(f"  扰动 {P['guards']['sensitivity_perturb_pct']}% → 翻转率 {rep['flip_rate']:.2%}"
          f"（脆弱阈值 {P['guards']['sensitivity_flip_rate_fragile']:.0%}）")
    print(f"  [{'ADVISORY' if fragile else 'PASS'}] "
          f"{'结论较敏感（调参杆±20%翻转率高）→ 运营时改参数须谨慎' if fragile else '结论稳健'}")
    # 敏感性为 [ADVISORY]：不阻断管线，但记录
    out["S2"] = "ADVISORY" if fragile else "PASS"
    out["S2_flip_rate"] = rep["flip_rate"]

    # ----------------------------------------------------------------- S3 蒙特卡洛
    print(f"\n--- S3 蒙特卡洛 {mc} 组不变量 ---")
    vp_pairs = [("up", "up"), ("up", "flat"), ("flat", "up"), ("down", "up"),
                ("flat", "flat"), ("down", "flat"), ("up", "down"), ("down", "down")]
    demands = ["growth", "cyclical", "erosion", "collapse"]
    imag = ["S", "A", "B", "C", "D"]
    fwd = ["up", "flat", "down"]
    inv = {"OK", "PENDING_VERIFICATION", "REJECTED_SCOPE", "REJECTED_FRAMEWORK"}
    bad_inv = 0
    exc = 0
    for i in range(mc):
        v, pr = random.choice(vp_pairs)
        pb = random.uniform(2.0, 98.0)
        # 单调五档
        p5 = random.uniform(0.5, 2.0)
        p20 = p5 + random.uniform(0.05, 1.2)
        p50 = p20 + random.uniform(0.05, 1.5)
        p80 = p50 + random.uniform(0.05, 2.0)
        p95 = p80 + random.uniform(0.05, 3.0)
        pb_now = random.uniform(p5, p95)
        c = {"sector": f"MC{i}", "as_of": "2026-06-30", "asset_class": "sector",
             "index_code": "X", "demand": random.choice(demands),
             "volume": v, "price": pr, "forward": random.choice(fwd),
             "vp_basis": "industry_cycle",
             "industry_volume_metric": "测试行业产量同比(行业供需周期口径)",
             "industry_price_metric": "测试行业核心品价格(行业供需周期口径)",
             "pb_now": pb_now, "pb_manual": pb_now, "pb_sources": {"a": pb_now, "b": pb_now + 0.01},
             "pb_pct": pb, "pb_pct_period": "5y", "pb_pct_10y": pb - 3.0,
             "pb_p5": p5, "pb_p20": p20, "pb_p50": p50, "pb_p80": p80, "pb_p95": p95,
             "imagination": random.choice(imag),
             "bottom_signals": random.choice([0, 4]),
             "erosion_signals": random.choice([0, 3])}
        try:
            r = evaluate(c)
        except Exception:
            exc += 1
            continue
        wr = r["win_rate"]
        pos = float(str(r["suggested_position"]).rstrip("%"))
        if r["status"] not in inv:
            bad_inv += 1
        if not (wr is None or (0 <= wr <= 90)):
            bad_inv += 1
        if not (0.0 <= pos <= 40.0):
            bad_inv += 1
    print(f"  样本 {mc} 组 → 异常 {exc} 次，不变量违例 {bad_inv} 次")
    mc_ok = (exc == 0 and bad_inv == 0)
    print(f"  [{'PASS' if mc_ok else 'FAIL'}] 胜率∈[0,90]/None、仓位∈[0,40]%、status枚举、零异常")
    if not mc_ok:
        fails.append(f"S3 蒙特卡洛违例：异常{exc}/违例{bad_inv}")
    out["S3"] = "PASS" if mc_ok else "FAIL"

    # ----------------------------------------------------------------- S4 幂等性
    print("\n--- S4 幂等性 ---")
    a = evaluate(dict(mono))
    b = evaluate(dict(mono))
    same = (a == b)
    print(f"  [{'PASS' if same else 'FAIL'}] 同输入两次运行结论逐字段一致")
    if not same:
        fails.append("S4 幂等性破坏")
    out["S4"] = "PASS" if same else "FAIL"

    # ----------------------------------------------------------------- S5 指纹
    print("\n--- S5 参数指纹稳定性 ---")
    fp1 = guards.params_fingerprint(P)
    fp2 = guards.params_fingerprint(copy.deepcopy(P))
    lock_path = os.path.join(HERE, "params.lock")
    lock_fp = None
    if os.path.exists(lock_path):
        with open(lock_path, "r", encoding="utf-8") as f:
            lock_fp = json.load(f).get("fingerprint")
    stable = (fp1 == fp2)
    matched = (lock_fp is None) or (fp1 == lock_fp)
    print(f"  指纹复现: {'OK' if stable else 'FAIL'}  与 params.lock 一致: "
          f"{'OK' if matched else 'FAIL (' + (lock_fp or '无')[:12] + ')'}")
    fp_ok = stable and matched
    print(f"  [{'PASS' if fp_ok else 'FAIL'}] 指纹可复现且与冻结锁一致")
    if not fp_ok:
        fails.append("S5 指纹稳定性/锁一致性破坏")
    out["S5"] = "PASS" if fp_ok else "FAIL"

    out["fails"] = fails
    return out


def run(mc: int = 2000) -> int:
    print("=" * 84)
    print(f"XYD 供需板块框架 · 压力检验 (Stress Test)  engine={ENGINE_VERSION}")
    print("=" * 84)
    r = _results(mc)
    print("\n" + "=" * 84)
    hard = [k for k in ("S1", "S3", "S4", "S5") if r.get(k) == "FAIL"]
    adv = [k for k in ("S2",) if r.get(k) == "ADVISORY"]
    print(f"压力检验结果: 硬不变量={'全部通过' if not hard else 'FAIL ' + str(hard)}"
          f"{'  [ADVISORY] ' + str(adv) if adv else ''}")
    print("=" * 84)
    return 1 if hard else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mc", type=int, default=2000, help="蒙特卡洛组数")
    a = ap.parse_args()
    sys.exit(run(a.mc))
