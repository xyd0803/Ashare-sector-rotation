#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
selfcheck_all.py —— 模块级自检聚合器（2026-09-07 审议 P1-⑤）

把散落各模块的 `--selfcheck` 串成一步，挂入 run_all.py 第二步。
覆盖：数据双源闸 / finalgate / 估值主锚分类 / LLM轨 guard / 五维量化胜率 /
量化输入契约 / 双价格分析器 / SKILL.md 统计块一致性（make_stats --check）。
任一失败 → exit 1。
"""
import subprocess
import sys

HERE = __import__("os").path.dirname(__import__("os").path.abspath(__file__))
PY = sys.executable

MODULES = [
    ("gate_dual_source", "数据双源验证闸"),
    ("gate_final", "finalgate 终检"),
    ("valuation_anchor", "估值主锚分类"),
    ("llm_fill_guard", "LLM轨机械复核(G3)"),
    ("quant_winrate", "五维量化胜率(§12)"),
    ("sector_pstar", "风险调整买点 P*(§P*)"),
    ("quant_inputs", "量化输入契约(O1)"),
    ("dual_price_analyzer", "双价格分析器"),
    ("data_health", "数据健康度四态(D-8)"),
    ("check_backlog", "欠账登记表(L-4)"),
    ("check_pitfall_closure", "坑→守卫闭环(S-20)"),
    ("_test_card_null_20261025", "卡片空值渲染(S-21)"),
    ("_test_scope_gate_20261104", "闸门作用域聚焦(S-22)"),
    ("trade_calendar", "分市场交易日历(B-03)"),
]


def main() -> int:
    print("=" * 84)
    print("Selfcheck · 模块级自检")
    print("=" * 84)
    fails = []
    for mod, label in MODULES:
        rc = subprocess.run([PY, mod + ".py", "--selfcheck"], cwd=HERE).returncode
        tag = "PASS" if rc == 0 else "FAIL"
        print(f"  [{tag}] {label}（{mod} --selfcheck）exit={rc}")
        if rc != 0:
            fails.append(mod)
    rc = subprocess.run([PY, "make_stats.py", "--check"], cwd=HERE).returncode
    tag = "PASS" if rc == 0 else "FAIL"
    print(f"  [{tag}] SKILL.md 统计块一致性（make_stats --check）exit={rc}")
    if rc != 0:
        fails.append("make_stats")
    print("-" * 84)
    if fails:
        print(f"  ❌ selfcheck 失败模块: {fails}")
        return 1
    print("  ✅ 全部模块自检通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
