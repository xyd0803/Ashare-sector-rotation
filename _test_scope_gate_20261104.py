# -*- coding: utf-8 -*-
"""S-22 闸门作用域回归锁（2026-11-04）。

验的是「闸门认不认我这次要分析谁」，不是数据本身新不新。
场景注入而非读真实库：真实库的陈旧只数（30 只）会随刷新变化，拿它当夹具
等于让测试跟着数据漂；且真实库无法稳定构造「目标新鲜 / 目标陈旧 / 目标无缓存」
三种态同时存在。

注入手法：preflight_etf 里 PB_DIR / PE_DIR / MAP_PATH 是函数内引用的模块级
全局，直接改 `P.PB_DIR` 即可生效（与 trade_calendar 自检同源）。

G1 目标陈旧 → 只报目标，无关项折一行，绝不逐条列
G2 目标新鲜 → PASS + 显式「已含 <期> 财报 ✓」（没事也要给证据）
G3 目标无缓存 → WARN（此前完全静默，一路跑到分位才炸）
G4 直接传缓存键（行业指数场景）→ 同样聚焦
G5 不传 codes → 全量行为不变（回归锁：不许为了聚焦而废掉库级巡检）
G6 缓存诚实性分治：as_of 虚报全量查 / stale 只查目标

    python _test_scope_gate_20261104.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from datetime import date

import preflight_etf as P

RT: list[tuple[bool, str]] = []
_fail = 0

# 陈旧夹具固定 2026-10-01：既早于 20260930 报告期披露截止（2026-10-31，lag>0），
# 又距今天 >FRESHNESS_MAX_DAYS（能触发 stale 分支）。两个条件同时满足才测得全。
STALE_TAIL = "2026-10-01"


def chk(ok: bool, msg: str) -> None:
    global _fail
    RT.append((bool(ok), msg))
    if not ok:
        _fail += 1
    print(("[ok  ] " if ok else "[FAIL] ") + msg)


def _mk(d: str, key: str, tail: str, metric: str = "pb", as_of: str | None = None) -> None:
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, key + ".json"), "w", encoding="utf-8") as f:
        json.dump({"code": key, "as_of": as_of or tail, metric: [[tail, 1.23]]}, f)


def main() -> int:
    today = date.today().isoformat()
    tmp = tempfile.mkdtemp(prefix="scope_gate_")
    pb, pe = os.path.join(tmp, "pb"), os.path.join(tmp, "pe")
    _mk(pb, "STALE.CSI", STALE_TAIL)
    _mk(pb, "OTHER.CSI", STALE_TAIL)
    _mk(pb, "FRESH.CSI", today)
    mpath = os.path.join(tmp, "map.json")
    with open(mpath, "w", encoding="utf-8") as f:
        json.dump({
            "100001": {"name": "陈旧示例", "index_code": "STALE.CSI"},
            "100002": {"name": "新鲜示例", "index_code": "FRESH.CSI"},
            "100003": {"name": "无缓存示例", "index_code": "NOCACHE.CSI"},
        }, f, ensure_ascii=False)

    saved = (P.PB_DIR, P.PE_DIR, P.MAP_PATH)
    P.PB_DIR, P.PE_DIR, P.MAP_PATH = pb, pe, mpath
    try:
        # ---- G1 目标陈旧：聚焦 + 无关折叠
        name, lvl, msgs = P.check_report_cycle(["100001"])
        body = "\n".join(msgs)
        chk(name == "财报周期闸", "G1 检查项名不变 → 财报周期闸")
        chk("STALE.CSI" in body, "G1 目标陈旧必须报出来（点名到缓存键）")
        chk("FRESH.CSI" not in body, "G1 无关但新鲜的不得出现在告警里")
        chk("OTHER.CSI" not in body, "G1 无关陈旧不得逐条列出（信号淹没的原病灶）")
        chk("另有 1 只无关缓存" in body, "G1 无关项折一行汇总（不静默隐瞒，也不淹没）")
        chk(lvl in ("WARN", "MUST"), "G1 目标陈旧不得判 OK")

        # ---- G2 目标新鲜：PASS 且带证据
        name, lvl, msgs = P.check_report_cycle(["100002"])
        body = "\n".join(msgs)
        chk(lvl == "OK", "G2 目标已含最新财报 → OK（不是靠『没有输出』装出来的 OK）")
        chk("已含" in body and "财报 ✓" in body, "G2 必须显式给证据『as_of=… 已含 <期> 财报 ✓』")
        chk("FRESH.CSI" in body, "G2 证据里要点出被检查的缓存键")
        chk("STALE.CSI" not in body, "G2 无关陈旧不得污染 PASS 结论")

        # ---- G3 目标无缓存
        name, lvl, msgs = P.check_report_cycle(["100003"])
        body = "\n".join(msgs)
        chk(lvl == "WARN", "G3 目标无本地缓存 → WARN（分位算不出来，必须先取数）")
        chk("无 PB/PE 缓存" in body and "NOCACHE.CSI" in body, "G3 必须点名缺哪个缓存键")
        chk("100003" in body, "G3 必须回指用户输入的 ETF 代码（否则不知道该给谁补数）")

        # ---- G4 直接传缓存键（行业指数场景）
        name, lvl, msgs = P.check_report_cycle(["STALE.CSI"])
        body = "\n".join(msgs)
        chk("STALE.CSI" in body and "OTHER.CSI" not in body,
            "G4 直接传缓存键也聚焦（行业指数分析路径）")

        # ---- G5 无 codes 全量回归
        name, lvl, msgs = P.check_report_cycle()
        body = "\n".join(msgs)
        chk("STALE.CSI" in body and "OTHER.CSI" in body,
            "G5 不传 codes → 全量行为不变（库级巡检不许被聚焦改动废掉）")
        chk("已含" not in body, "G5 全量模式不喷逐只的新鲜证据（否则 56 行噪音）")

        # ---- G6 缓存诚实性分治：虚报全量 / stale 聚焦
        _mk(pb, "LIAR.CSI", "2026-10-23", as_of="2026-10-30")
        name, lvl, msgs = P.check_cache_honesty(["100002"])
        body = "\n".join(msgs)
        chk("LIAR.CSI" in body, "G6 as_of 虚报必须全量报（数据诚信与『本次看谁』无关）")
        chk("STALE.CSI" not in body, "G6 无关 stale 不逐条列")
        chk("另有 2 只无关缓存" in body, "G6 无关 stale 计数（STALE + OTHER）")
    finally:
        P.PB_DIR, P.PE_DIR, P.MAP_PATH = saved
        shutil.rmtree(tmp, ignore_errors=True)

    print("-" * 70)
    if _fail:
        print(f"  ❌ {_fail}/{len(RT)} 条失败")
        return 1
    print(f"  ✅ {len(RT)}/{len(RT)} 全通过（G1~G6）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
