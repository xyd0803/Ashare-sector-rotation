#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_test_closure_gate_20261025.py — 闭环校验器自身的红队注入测试（S-20 的自证）

为什么必须存在
--------------
S-20 的教训是「登记了 ≠ 治理了」。如果我只写一个 check_pitfall_closure.py 然后
宣布"现在闭环可靠了"，那就是在**用同一种病治病**。所以这个文件的作用是：
**先把校验器打坏，看它会不会叫。**

每个用例都在 registry 的临时副本上注入一种真实可能发生的违规，断言对应规则
真的 FAIL。如果哪天有人把某条规则改软了（或写成永远 return 0），本测试立刻红。

用例 → 规则映射（与 PITFALLS_REGISTRY._meta.rules 一一对应）
  T1 正常 registry                → 必须 rc=0（防止"永远 FAIL"的假阳性）
  T2 proof 换成不存在的字符串      → R4（指错检查项）
  T3 check_id 换成不存在的检查项   → R2（登记了假兵器）
  T4 一个检查项塞 4 条复发型坑     → R5（垃圾桶闸）
  T5 gate 类坑撤掉 stock_side      → R6（修入口当修数据）
  T6 复发型坑守卫改成 none         → R3（复发型裸奔）
  T7 临时表删掉一条 md 里有的坑    → R7（双向不一致）

运行：python _test_closure_gate_20261025.py   （期望 exit 0）
"""
import copy
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout

import check_pitfall_closure as cpc

FAILED = []
REAL_REG = json.loads(open(cpc.REG_PATH, encoding="utf-8").read())


def chk(cond, msg):
    print(f"  [{'OK  ' if cond else 'FAIL'}] {msg}")
    if not cond:
        FAILED.append(msg)


def run_with(mutator, label):
    """在临时副本上改坏 registry，跑校验，返回 (rc, stdout)。"""
    doc = copy.deepcopy(REAL_REG)
    mutator(doc)
    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                      encoding="utf-8")
    try:
        json.dump(doc, tmp, ensure_ascii=False)
        tmp.close()
        old = cpc.REG_PATH
        cpc.REG_PATH = tmp.name
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                rc = cpc.run()
        finally:
            cpc.REG_PATH = old
        return rc, buf.getvalue()
    finally:
        os.unlink(tmp.name)


def find(doc, pid):
    return next(p for p in doc["pitfalls"] if p["id"] == pid)


print("== T1 正常 registry 必须全绿（防「永远 FAIL」的假阳性） ==")
rc, out = run_with(lambda d: None, "baseline")
chk(rc == 0 and "闭环全绿" in out, f"T1 baseline rc={rc}（期望 0）")

print("\n== T2 R4 指错检查项：proof 改成本不存在的字符串 ==")
def m2(d):
    find(d, "S-05")["guard"]["proof"] = "THIS_TOKEN_NEVER_EXISTS"
rc, out = run_with(m2, "R4")
chk(rc == 1 and "R4" in out and "疑似指错检查项" in out,
    "T2 校验器识别「指向的检查项并不查这个坑」")

print("\n== T3 R2 登记了假兵器：check_id 改成不存在的检查项 ==")
def m3(d):
    find(d, "S-05")["guard"]["check_id"] = "宇宙无敌检查项"
rc, out = run_with(m3, "R2")
chk(rc == 1 and "R2" in out and "不存在" in out,
    "T3 校验器拒绝不存在的检查项")

print("\n== T4 R5 垃圾桶闸：往同一检查项塞 4 条复发型坑 ==")
def m4(d):
    base = copy.deepcopy(find(d, "S-05"))
    for i in range(3):
        p = copy.deepcopy(base)
        p["id"] = f"S-9{i}"
        p["status"] = "fixed"
        d["pitfalls"].append(p)
    # 同时在 md 侧不存在 → 会额外触发 R7，故把 md 集合比对的期望放宽为「R5 必须出现」
rc, out = run_with(m4, "R5")
chk(rc == 1 and "R5" in out and "垃圾桶" not in out and "粒度不足" in out,
    "T4 校验器识别单检查项承载过多复发型坑（拒绝用一个大检查项假装覆盖多条）")

print("\n== T5 R6 修入口当修数据：gate 类坑撤掉 stock_side/residual_ok ==")
def m5(d):
    p = find(d, "S-18")
    p["stock_side"] = None
    p["residual_ok"] = None
rc, out = run_with(m5, "R6")
chk(rc == 1 and "R6" in out and "修了入口当修了数据" in out,
    "T5 校验器强制数据写入闸门回答「存量怎么办」")

print("\n== T6 R3 复发型裸奔：guard.type 改成 none ==")
def m6(d):
    find(d, "S-14")["guard"] = {"type": "none"}
rc, out = run_with(m6, "R3")
chk(rc == 1 and "R3" in out and "复发型" in out,
    "T6 校验器拒绝复发型坑只用「文档」当守卫")

print("\n== T7 R7 双向不一致：临时表删掉一条 md 里有的坑 ==")
def m7(d):
    d["pitfalls"] = [p for p in d["pitfalls"] if p["id"] != "S-09"]
rc, out = run_with(m7, "R7")
chk(rc == 1 and "R7" in out and "S-09" in out,
    "T7 校验器识别 md 有、registry 没登记（=未闭环）")

print("\n== T8 S-16 守卫真实性：注入测试锚点必须真的在文件里 ==")
def m8(d):
    find(d, "S-16")["guard"]["assertion"] = "NO_SUCH_ANCHOR"
rc, out = run_with(m8, "R2-anchor")
chk(rc == 1 and "找不到断言锚点" in out,
    "T8 校验器不认「指向一个存在但并不覆盖此坑的测试」")

print()
if FAILED:
    print(f"❌ 闭环校验器的红队测试失败 {len(FAILED)} 项 → 说明校验器不能真的拦违规")
    for m in FAILED:
        print("   -", m)
    sys.exit(1)
print("✅ 闭环校验器红队全过：R2/R3/R4/R5/R6/R7 六条规则均能真实触发")
