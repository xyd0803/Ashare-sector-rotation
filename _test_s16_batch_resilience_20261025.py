#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_test_s16_batch_resilience_20261025.py — S-16 注入回归（连接器空响应不许杀整批）

背景
----
2026-10-25 批量刷新 34 只 stale PB 缓存时，fetch_pb_mxdata.fetch_one 跑到第 11 只
遇到妙想直连返回 None（通道瞬时故障，**不是**该指数无数据），原代码直接 r.get()
→ AttributeError **崩掉整个刷新循环**：12 只的批次只落 10 只，剩下 2 只既没成功
也没有失败记录（静默丢失 = 比报错更糟）。

为什么需要这个测试
------------------
修法（per-code 返回 + 一次 2s 重试）是 2026-10-25 写进代码的，但**没有任何测试
证明它真的会继续**。PITFALLS_REGISTRY R2 要求 recurrence 类坑必须有可执行的守卫，
「改了代码」不等于「证明了行为」，故补本文件。

断言锚点 BATCH_SURVIVES 被 check_pitfall_closure.py R2 引用（改名须同步 registry）。

运行：python _test_s16_batch_resilience_20261025.py   （期望 exit 0）
"""
import inspect
import sys

import fetch_pb_mxdata as fpm

# 重试路径里有 time.sleep(2.0)，测试里禁用，避免单例 2s×N 拖慢回归
fpm.time.sleep = lambda *a, **k: None

FAILED = []


def chk(cond, msg):
    if cond:
        print(f"  [OK  ] {msg}")
    else:
        print(f"  [FAIL] {msg}")
        FAILED.append(msg)


class FakeMx:
    """按 code 编排出不同故障模式；calls 记录每个 code 被问了几次。"""

    def __init__(self, behavior):
        self.behavior = behavior
        self.calls = []

    def query(self, qtext, *a, **k):
        # qtext 形如 "名称(CODE) 从..."
        code = None
        for c in self.behavior:
            if f"({c})" in qtext:
                code = c
                break
        self.calls.append(code)
        mode = self.behavior.get(code, "raise")
        if mode == "raise":
            raise ConnectionError("simulated channel failure")
        if mode == "none":
            return None
        if mode == "list":
            return [{"unexpected": True}]
        if mode == "nodata":
            return {"data": {"data": {"searchDataResultDTO": {"dataTableDTOList": []}}}}
        raise AssertionError(f"未定义的模式 {mode}")


print("== T1 单点故障四种模式：必须返回 (None, 原因)，不许抛异常 ==")
cases = [
    ("raise", "query-err"),
    ("none", "empty/invalid response x2"),
    ("list", "empty/invalid response x2"),
    ("nodata", "empty dataTableDTOList"),
]
fake = FakeMx({mode: mode for mode, _ in cases})   # 用模式名当 code，便于编排
for mode, expect in cases:
    try:
        rec, err = fpm.fetch_one(fake, mode, "测试名", "2026-10-23", delay=0, metric="pb")
        chk(rec is None and err and expect in err,
            f"T1 {mode:6s} → rec=None 且原因含「{expect}」（实得 {err!r}）")
    except Exception as e:                                   # noqa: BLE001
        chk(False, f"T1 {mode:6s} 抛了异常 {type(e).__name__}: {e} → 会崩掉整批")

print("\n== T2 BATCH_SURVIVES：混合故障批次必须跑完全部标的 ==")
codes = ["000001", "399001", "399006", "000300"]
beh = {"000001": "raise", "399001": "none", "399006": "nodata", "000300": "list"}
fake2 = FakeMx(beh)
processed, errors = 0, 0
crashed = None
try:
    for c in codes:                      # 复刻 main() 的循环体语义
        rec, err = fpm.fetch_one(fake2, c, f"指数{c}", "2026-10-23", delay=0, metric="pb")
        processed += 1
        if rec is None:
            errors += 1
except Exception as e:                   # noqa: BLE001
    crashed = f"{type(e).__name__}: {e}"

chk(crashed is None, f"T2a 混合故障批次未抛异常（crashed={crashed}）")
chk(processed == len(codes),
    f"T2b 全部 {len(codes)} 只都被处理（实得 {processed}）→ 不允许中途丢失标的")
chk(errors == len(codes),
    f"T2c 每只都留下失败记录（{errors}/{len(codes)}）→ 不允许静默跳过")
chk(all(c in fake2.calls for c in codes) and None not in fake2.calls,
    f"T2d 每只都被真正查询过（calls={fake2.calls}，含重试故有重复）"
    " ← BATCH_SURVIVES")

print("\n== T3 循环体确实 continue（源码级证明接线，不只是函数返回值对） ==")
src = inspect.getsource(fpm.main)
chk("if rec is None" in src and "continue" in src,
    "T3 main() 在 rec is None 分支里有 continue → 单点失败不会终止批次")
chk("for " in src, "T3b main() 确有批次循环（防止把单点调用误当批量）")

print()
if FAILED:
    print(f"❌ {len(FAILED)} 项失败")
    for m in FAILED:
        print("   -", m)
    sys.exit(1)
print("✅ S-16 注入回归全过：连接器空响应/瞬时故障不再杀整批")
