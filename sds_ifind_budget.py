#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ifind_budget.py —— SDS 侧 iFinD 预算闸适配器 + 记账 CLI（2026-10-25 · D-3 落地）
================================================================================
背景（数据层审计 D-3）：iFinD 是付费源，按调用次数计费，因此本技能侧设了**每日调用
上限**（默认值见 `_DEFAULT_CAP`，须与共享账本一侧口径一致）。审计发现 SDS 技能的
iFinD 消耗有三类路径，护栏状态不一：

  ① ifind_fetch.py / ifind_fix_pb.py  → 经共享的 iFinD `call.py`，
     该 call() 已内置预算硬闸 + **require_tag 无 tag 拒绝执行**。但两个 SDS
     脚本从未 set_attribution → 一跑就被 fail-closed 拒（或被迫设全局 env，
     掩盖归因缺失）。→ 本批次给两个脚本补 `--tag` + set_attribution（见脚本内）。
  ② ingest_ifind_valuation.py 的取数路径 = **Agent 经 MCP 直调**
     （get_security_indicators 等），完全不经 call.py → **无记账、无闸**。
     → 本批次给 ingest 脚本补 `--charge`（默认 1：产出该表的 MCP 调用数），
        由本模块落账；`--no-charge` 用于表格来自缓存/复用（不消耗配额）的场景。
  ③ preflight_etf.py 新增 `check_ifind_budget`：报告今日用量/上限，耗尽且
     本次运行会尝试刷新（未传 --no-refresh）→ MUST exit 3（fail-closed），
     解法 = 本地优先（--no-refresh）或逃生门 XYD_IFIND_BUDGET_OK=1。

单一账本原则：配额按**账号**计，不按项目计 → 本适配器优先委托本机上提供
`ifind_budget` 的共享模块（单一真相源），与其**共用同一账本**文件；共享模块
不可导入时（跨环境/目录移位）启用的内嵌等价闸使用**同一账本、同一 cap 口径**
（默认值与共享模块一致；注意某些历史实现自带更宽松的兜底值，本适配器不复制
——闸的方向必须更严不能更松）。共享模块目录不写死：先读环境变量
`XYD_SKILL_DIR`，否则在 `XYD_SKILLS_ROOT`（默认 `~/.workbuddy/skills`）下
自动发现实际含 `ifind_budget.py` 的目录。

CLI：
  python sds_ifind_budget.py --report                      # 今日用量报告
  python sds_ifind_budget.py --charge --script ingest_ifind_valuation \
      --note "980032.CNI 近40点" --tag sds_ingest      # MCP 路径记账（默认 n=1）
  python sds_ifind_budget.py --enforce --need 3 --tag sds_backfill   # 预检（超限 exit 3）

命名纪律：本模块**不得**命名 ifind_budget.py——与共享模块
同名会自引用递归（import 到自身半初始化模块 → today_count 无限递归 · 2026-10-25
实测 RecursionError 后改名）。共享模块恒经 `import ifind_budget`（_EARN_SKILL 路径）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# ── 定位共享 ifind_budget：环境变量优先，否则在同机 skills 目录里自动发现 ──────
def _discover_shared_skill():
    """找到本机上提供 ifind_budget 模块的目录。

    不写死任何具体目录名：先看 XYD_SKILL_DIR，再扫描 skills 根目录下实际存在
    ifind_budget.py 的目录。找不到返回空串，调用方自然回退到内嵌闸。
    """
    explicit = os.environ.get("XYD_SKILL_DIR")
    if explicit:
        return explicit
    root = os.path.expanduser(
        os.environ.get("XYD_SKILLS_ROOT") or os.path.join("~", ".workbuddy", "skills"))
    try:
        for name in sorted(os.listdir(root)):
            if os.path.isfile(os.path.join(root, name, "ifind_budget.py")):
                return os.path.join(root, name)
    except OSError:
        pass
    return ""

_EARN_SKILL = _discover_shared_skill()
_LEDGER = os.environ.get("XYD_IFIND_LEDGER") or os.path.join(
    _EARN_SKILL or HERE, ".evidence", "ifind_ledger.jsonl")
_DEFAULT_CAP = 80          # 日上限默认值，须与共享账本一侧保持一致
_OVER_QUOTA_DEFAULT = 20   # 逃生门单次授权额度（与 earnings 侧同口径）

_ib = None
_IB_ERR = ""
try:
    if _EARN_SKILL not in sys.path:
        sys.path.insert(0, _EARN_SKILL)
    import ifind_budget as _ib  # type: ignore
except Exception as e:  # 共享模块缺失 → 内嵌等价闸（同账本同口径，绝不空转）
    _IB_ERR = f"{type(e).__name__}: {e}"


# ── 内嵌等价闸（仅共享模块不可用时启用）──────────────────────────────────────
def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _read_ledger() -> list:
    rows = []
    try:
        with open(_LEDGER, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        pass
    except Exception:
        pass
    return rows


def _cap() -> int:
    try:
        return int(os.environ.get("XYD_IFIND_DAILY_CAP", _DEFAULT_CAP))
    except (TypeError, ValueError):
        return _DEFAULT_CAP


def _over_quota() -> int:
    try:
        return int(os.environ.get("XYD_IFIND_OVER_N", _OVER_QUOTA_DEFAULT))
    except (TypeError, ValueError):
        return _OVER_QUOTA_DEFAULT


def _today_count() -> int:
    t = _today()
    return sum(1 for r in _read_ledger() if str(r.get("ts", "")).startswith(t))


def _today_over_count() -> int:
    t = _today()
    return sum(1 for r in _read_ledger()
               if str(r.get("ts", "")).startswith(t) and bool(r.get("over")))


def _charge_local(script: str, note: str = "", n: int = 1, tag: str = "",
                  over: bool = False) -> None:
    try:
        os.makedirs(os.path.dirname(_LEDGER), exist_ok=True)
        rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "script": str(script)[:40],
               "note": str(note)[:80], "n": int(n), "over": bool(over)}
        tg = str(tag or "").strip()
        if tg:
            rec["tag"] = tg[:40]
        with open(_LEDGER, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _allow_over_budget() -> bool:
    return (os.environ.get("XYD_IFIND_BUDGET_OK") == "1"
            or "--allow-over-budget" in sys.argv)


# ── 统一门面（共享优先，内嵌兜底）────────────────────────────────────────────
def budget_report() -> str:
    if _ib is not None:
        return _ib.budget_report()
    ov = _today_over_count()
    ovs = f" · 逃生门超限 {ov}/{_over_quota()}" if ov else ""
    return f"iFinD 今日 {_today_count()}/{_cap()} 次{ovs}（ledger={os.path.basename(_LEDGER)}）"


def today_count() -> int:
    return _ib.today_count() if _ib is not None else _today_count()


def cap() -> int:
    return _ib.cap() if _ib is not None else _cap()


def charge(script: str, note: str = "", n: int = 1, tag: str = "") -> None:
    """记账（只记录不拦截）。共享优先；内嵌模式按最近 enforce 判定 over。"""
    if _ib is not None:
        _ib.charge(script, note=note, n=n, tag=tag)
        return
    _charge_local(script, note=note, n=n, tag=tag)


def enforce(script: str, need: int = 1, tag: str = "") -> bool:
    """硬闸：超 cap → 打印账单并 exit(3)（fail-closed）。逃生门单次授权。"""
    if _ib is not None:
        _ib.enforce_or_die(script, need=need, tag=tag)
        return True
    used, cp, need = _today_count(), _cap(), max(1, int(need))
    if used + need <= cp:
        return True
    if _allow_over_budget():
        ou, oq = _today_over_count(), _over_quota()
        if ou + need <= oq:
            print(f"  ⚠️ iFinD 预算闸：今日已用 {used} + 本次 {need} > 上限 {cp} —— "
                  f"逃生门单次授权放行（超限额度 {ou}+{need}/{oq}）")
            return True
        print(f"\n🔴 iFinD 预算硬闸（fail-closed）：逃生门额度已用尽 —— "
              f"今日超限 {ou}/{oq} 次（累计 {used}·上限 {cp}）。")
        sys.exit(3)
    print(f"\n🔴 iFinD 预算硬闸（fail-closed）：今日已用 {used} + 本次 {need} > "
          f"日上限 {cp}。iFinD 是付费源，本地/免费源能满足的一律不得打 iFinD。")
    print("   处置：① 本地优先（--no-refresh / 缓存复用）；"
          "② 确属必要 → XYD_IFIND_BUDGET_OK=1（单次授权 XYD_IFIND_OVER_N 次）；"
          "③ set XYD_IFIND_DAILY_CAP=<N>")
    sys.exit(3)


def is_shared() -> bool:
    """是否委托共享模块（False=内嵌等价闸；供诊断/测试断言）。"""
    return _ib is not None


def shared_error() -> str:
    return _IB_ERR


def _main() -> int:
    ap = argparse.ArgumentParser(description="SDS iFinD 预算闸适配器（共用 earnings 账本）")
    ap.add_argument("--report", action="store_true", help="打印今日用量报告")
    ap.add_argument("--charge", action="store_true", help="记账一次/多次 MCP 调用")
    ap.add_argument("--enforce", action="store_true", help="预检：超限 exit 3")
    ap.add_argument("--need", type=int, default=1, help="--enforce 的预计调用数")
    ap.add_argument("--script", default="", help="归因脚本名")
    ap.add_argument("--note", default="", help="归因备注")
    ap.add_argument("--tag", default="", help="批次归因 tag（必填，对齐 require_tag 纪律）")
    ap.add_argument("--n", type=int, default=1, help="--charge 的调用次数")
    a = ap.parse_args()

    if a.report or not (a.charge or a.enforce):
        print(budget_report())
        if not is_shared():
            print(f"  [注] 共享 ifind_budget 不可用（{shared_error()}）→ 内嵌等价闸"
                  f"（同账本·cap={cap()}）")
        return 0
    if not str(a.tag or "").strip():
        print("🔴 拒绝记账/预检（fail-closed）：未提供 --tag。批次外 iFinD 消耗必须归因"
              "（earnings 侧 10-12 审计实证：无 tag 调用占日配额 48% 且无法归因）。")
        return 3
    if a.charge:
        charge(a.script or "sds_manual", note=a.note, n=a.n, tag=a.tag)
        print(f"已记账 {a.n} 次（script={a.script or 'sds_manual'} tag={a.tag}）· "
              f"{budget_report()}")
        return 0
    enforce(a.script or "sds_manual", need=a.need, tag=a.tag)
    print(f"预算预检通过 · {budget_report()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
