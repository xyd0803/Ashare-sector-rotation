#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
_test_ifind_budget_gate_20261025.py —— D-3 预算闸注入测试（2026-10-25）
====================================================================
按项目纪律：**加完任何保护机制必须做注入测试证明它真会触发**。本脚本用
子进程 + 临时账本（XYD_IFIND_LEDGER 重定向）对 check_ifind_budget 做 6 组
注入，全部必须按预期触发/放行：

  T1 委托健康：有共享模块时必须 is_shared()==True；没有时（干净克隆/CI 常态）
     退为「回退实现可用且不自引用递归」——两种环境测的是同一件事的不同分支
     （回归 2026-10-25 实测 RecursionError：SDS 模块曾命名 ifind_budget.py，
      与共享模块同名 → import 到自身半初始化模块 → today_count 无限递归）
  T2 耗尽拦截：临时账本预置 80 笔（=cap）→ check_ifind_budget level==MUST
  T3 本地优先放行：耗尽 + sys.argv 含 --no-refresh → 不 MUST
  T4 逃生门放行：耗尽 + XYD_IFIND_BUDGET_OK=1 → 不 MUST
  T5 归因接线：ifind_fetch.py / ifind_fix_pb.py 必须含 set_attribution(
     （防"假兵器"：漏接 = call.py require_tag 必拒 → 检查必须能发现）
  T6 记账闭环：charge() 落账一行，today_count +1

用法：python _test_ifind_budget_gate_20261025.py
退出码 0 = 全过；1 = 有失败（禁止合入）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = sys.executable
FAILS = []
SKIPS = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def skip(name, why):
    """环境前提不成立：不判过、也不判红，但要显式说出来，不能静默。"""
    print(f"  [SKIP] {name} — {why}")
    SKIPS.append(f"{name}（{why}）")


def _run(code: str, env_extra: dict | None = None) -> str:
    env = dict(os.environ)
    env.update(env_extra or {})
    r = subprocess.run([PY, "-c", code], cwd=str(HERE), capture_output=True,
                       text=True, encoding="utf-8", errors="replace", env=env,
                       timeout=120)
    return (r.stdout or "") + (r.stderr or "")


def main() -> int:
    print("== T1 委托健康（防自引用递归回归）==")
    out = _run("import sys; sys.path.insert(0, r'.'); import sds_ifind_budget as ib;"
               "print('SHARED' if ib.is_shared() else 'LOCAL'); print(ib.budget_report())")
    if "SHARED" in out:
        check("is_shared()==True（已委托共享账本）", True)
        check("budget_report 可读", "iFinD 今日" in out)
    elif "LOCAL" in out:
        # 干净克隆 / CI 就是这种环境：共享模块根本没装，委托无从发生。这时仍要证明
        # 被守护的性质——回退实现能正常返回，而不是 import 到自身半初始化模块后递归。
        skip("is_shared()==True", "本环境未发现共享模块，委托断言不适用")
        check("无共享模块时回退可用（不递归、可读）", "iFinD 今日" in out,
              out.strip().splitlines()[-1] if out.strip() else "")
    else:
        check("sds_ifind_budget 可导入", False, out.strip()[-200:])

    print("== T6 记账闭环（先建临时账本）==")
    with tempfile.TemporaryDirectory() as td:
        ledger = os.path.join(td, "ledger.jsonl")
        out = _run(
            "import sys; sys.path.insert(0, r'.'); import sds_ifind_budget as ib;"
            "ib.charge('test_script', note='注入测试', n=2, tag='t_gate');"
            "print('COUNT', ib.today_count())",
            {"XYD_IFIND_LEDGER": ledger})
        # 账本按「行」计数（一行=一次调用；n=批量元数据，与共享账本语义一致）
        check("charge 后 today_count==1（一行）", "COUNT 1" in out, out.strip().splitlines()[-1] if out.strip() else "")
        rows = [json.loads(l) for l in Path(ledger).read_text(encoding="utf-8").splitlines() if l.strip()]
        check("落账行含 script/tag/n", len(rows) == 1 and rows[0]["script"] == "test_script"
              and rows[0]["tag"] == "t_gate" and rows[0]["n"] == 2)

        print("== T2 耗尽拦截（cap=80·预置 80 笔）==")
        t = time.strftime("%Y-%m-%d")
        with open(ledger, "a", encoding="utf-8") as fh:
            for i in range(79):   # 已有 1 笔 → 共 80 = cap
                fh.write(json.dumps({"ts": f"{t} 09:{i % 60:02d}:00",
                                     "script": "seed", "note": "seed", "n": 1}) + "\n")
        out = _run(
            "import sys; sys.path.insert(0, r'.'); import preflight_etf as pf;"
            "name, level, msgs = pf.check_ifind_budget();"
            "print('LEVEL', level); print('ALLMSG', ' || '.join(msgs))",
            {"XYD_IFIND_LEDGER": ledger})
        check("耗尽 → MUST", "LEVEL MUST" in out, [l for l in out.splitlines() if l.startswith("LEVEL")])
        check("MUST 文案含本地优先指引", "--no-refresh" in out)

        print("== T3 本地优先放行（--no-refresh）==")
        out = _run(
            "import sys; sys.argv = ['analyze_etf.py', '--etf', '159755', '--no-refresh'];"
            "sys.path.insert(0, r'.'); import preflight_etf as pf;"
            "print('LEVEL', pf.check_ifind_budget()[1])",
            {"XYD_IFIND_LEDGER": ledger})
        check("--no-refresh → 不 MUST", "LEVEL MUST" not in out,
              [l for l in out.splitlines() if l.startswith("LEVEL")])

        print("== T4 逃生门放行（XYD_IFIND_BUDGET_OK=1）==")
        out = _run(
            "import sys; sys.path.insert(0, r'.'); import preflight_etf as pf;"
            "print('LEVEL', pf.check_ifind_budget()[1])",
            {"XYD_IFIND_LEDGER": ledger, "XYD_IFIND_BUDGET_OK": "1"})
        check("逃生门 → 不 MUST", "LEVEL MUST" not in out,
              [l for l in out.splitlines() if l.startswith("LEVEL")])

    print("== T5 归因接线核验（两脚本必须 set_attribution）==")
    for fn in ("ifind_fetch.py", "ifind_fix_pb.py"):
        src = (HERE / fn).read_text(encoding="utf-8")
        check(f"{fn} 含 set_attribution", "set_attribution(" in src)
        check(f"{fn} 含 --tag CLI", '"--tag"' in src)

    print()
    if FAILS:
        print(f"❌ 预算闸注入测试失败 {len(FAILS)} 项: {FAILS}")
        return 1
    if SKIPS:
        print(f"[SKIP] {len(SKIPS)} 项环境前提不成立: {SKIPS}")
    print("✅ D-3 预算闸注入测试全过（6 组：委托健康/记账闭环/耗尽拦截/本地优先/逃生门/归因）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
