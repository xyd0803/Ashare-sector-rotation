#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XYD 供需板块框架 —— 一键运营自检闸门 (Run All Gate)
====================================================
把四道验证串成一条流水线，运营一键跑完即可确认"纪律层没有悄悄失效"：

  Preflight  → 参数指纹 vs params.lock（防事后调参/过拟合）
  Selfcheck  → 各模块 --selfcheck（双源闸/finalgate/锚分类/LLM轨guard/量化胜率/量化契约/双价格/文档统计）
  RedTeam    → 对抗用例（坏输入能不能骗过框架）
  Stress     → 单调性 / 敏感性 / 蒙特卡洛 / 幂等 / 指纹 不变量
  Backtest   → 回测管线贯通（point-in-time / t+1 / 成本 / 指纹）

任何一步返回非 0 → 闸门 FAIL，整体退出码 1。
红队/压力/回测的 [ADVISORY] 不阻断（仅提示），[FAIL] 才阻断。

运行
  python run_all.py            # 一键全跑（蒙特卡洛 2000）
  python run_all.py --mc 500   # 缩小蒙特卡洛规模
  python run_all.py --quick    # 仅跑 Preflight+Selfcheck（模块自校验），跳过 RedTeam/Stress/Backtest 重型步骤
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from typing import List

PY = sys.executable
HERE = __import__("os").path.dirname(__import__("os").path.abspath(__file__))


def _step(name: str, cmd: List[str]) -> int:
    print("\n" + "█" * 84)
    print(f"█ 步骤：{name}")
    print("█" * 84)
    rc = subprocess.run(cmd, cwd=HERE).returncode
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description="XYD 供需板块框架 一键自检闸门")
    ap.add_argument("--mc", type=int, default=2000, help="蒙特卡洛组数")
    ap.add_argument("--quick", action="store_true",
                    help="仅跑 Preflight+Selfcheck（模块自校验），跳过 RedTeam/Stress(蒙特卡洛)/Backtest 重型步骤")
    a = ap.parse_args()

    print("=" * 84)
    print("XYD 供需板块框架 · 纪律层运营自检闸门 (Run All Gate)")
    print("=" * 84)

    steps = []
    # 🔴 EtfPreflight · 运行前体检（2026-10-07 新增·必须第一位）
    # 存在理由：两只 ETF 纯机器链路实测仅 3.0s，但端到端约 1h → 99.9% 耗在
    # "跑→炸→读源码→改→重跑"往返。当天撞的 9 堵墙**9/9 均可在运行前静态检出**。
    # 本步骤把这些检查一次性跑完（~2s·不联网），exit=3 时禁止直跑管线。
    steps.append(("EtfPreflight · 运行前体检(依赖/签名/诚实性/硬编码/契约)", [PY, "preflight_etf.py"]))
    # Preflight：指纹锁（guards.py 自带 INIT/校验，单独跑一次确认一致）
    steps.append(("Preflight · 参数指纹冻结锁", [PY, "guards.py"]))
    # Selfcheck：模块级自检（2026-09-07 审议 P1-⑤ 增挂；此前靠手动记得跑）
    steps.append(("Selfcheck · 模块自检(闸/锚/guard/量化/统计)", [PY, "selfcheck_all.py"]))
    if a.quick:
        # 轻型模式：跳过 RedTeam/Stress/Backtest 重型步骤，仅做模块自校验。
        # 用于日常改完代码后的快检，不替代完整闸门（完整闸门仍须发布前跑一遍）。
        print("⚡ --quick：跳过 RedTeam / Stress(蒙特卡洛) / Backtest，仅做模块自校验。")
    else:
        # RedTeam
        steps.append(("RedTeam · 对抗用例", [PY, "redteam_sector.py"]))
        # Stress
        steps.append(("Stress · 压力检验", [PY, "stress_test.py", "--mc", str(a.mc)]))
        # Backtest
        steps.append(("Backtest · 回测管线", [PY, "backtest.py"]))

    results = []
    for name, cmd in steps:
        rc = _step(name, cmd)
        results.append((name, rc))

    print("\n" + "=" * 84)
    print("闸门汇总")
    print("=" * 84)
    overall = 0
    for name, rc in results:
        tag = "PASS" if rc == 0 else "FAIL"
        if rc != 0:
            overall = 1
        print(f"  [{tag}] {name}  (exit={rc})")
    print("=" * 84)
    if overall == 0:
        if a.quick:
            print("  ✅ 模块自校验全绿（Preflight+Selfcheck）。注意：--quick 未跑红队/压力/回测，"
                  "发布前仍须跑完整 `run_all.py` 闸门。")
        else:
            print("  ✅ 闸门全绿：纪律层当前有效（红队/压力/回测/指纹 全部通过）。")
    else:
        print("  ❌ 闸门 FAIL：上述非 0 步骤须先修复，禁止带病发布/上线。")
    print("=" * 84)
    return overall


if __name__ == "__main__":
    sys.exit(main())
