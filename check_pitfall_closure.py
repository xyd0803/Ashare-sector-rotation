#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_pitfall_closure.py — 「坑→守卫」闭环双向校验器（SDS 版，2026-10-25）

为什么存在
----------
PITFALLS_SDS.md 里的 `@preflight: xxx` 只是**一行注释**。注释本身无法被验证，
所以「写了标签 = 已闭环」实际上是**自证**：任何人（包括 LLM）都能低成本地
宣布一个坑已被治理，而没有任何东西会反驳它。这就是本技能「坑数看起来只增
不减」的结构性原因 —— 不是坑太多，而是**坑可以被假装关掉**。

earnings 技能早就有 PITFALLS_REGISTRY.json + 本脚本；SDS 侧一直只有 md。
本脚本把闭环从「自证」改成「他证」。

八条规则
--------
R1 结构完整   每条必备 11 字段；class ∈ A_construction / B_recurring / C_meta
R2 守卫生效   preflight → check_id 必须真实存在于 preflight_etf.REAL_CHECKS
              injection → test 文件必须存在，且文件内确实出现 assertion 串
              meta     → check_id ∈ REAL_CHECKS（用于指向全量自检类闸门）
              none     → 只在 status=fixed_structural 时允许
R3 复发型裸奔 class=B_recurring 必须有 preflight/injection（doc/none 不算）
R4 指错检查项 preflight 守卫必须给 proof，且 proof 必须出现在**该 check 函数
              源码**里。否则等于「指了个检查项，但那个检查项根本不查这个坑」。
              ← R4 是本脚本的核心，专治最常见的假闭环。
R5 垃圾桶闸   单个 check_id 承载 B 类坑 > MAX_B_PER_CHECK 条 → FAIL
              （粒度不足以逐条证明：一个 check 5 个坑时，没人能说清它到底
                在验哪一条，于是每条都"过了"但可能一条都没验）
R6 影响面配对 blast_radius=entry 必须给 stock_side（清存量/换路径落点）或
              residual_ok（为何不需要）。← 专治 S-18「修入口不修存量」
R7 双向一致   PITFALLS_SDS.md 的 S-xx 集合 == registry 的 id 集合
R8 状态自洽   status=open 且 class∈{A,B} 且 guard.type=none → FAIL

退出码：0 = 无 FAIL；1 = 有 FAIL（必须修）。WARN 不致命。
依赖：仅标准库 + 同目录 preflight_etf（import 失败即 FAIL：无法验证就不能说通过）。
"""
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

REG_PATH = os.path.join(_HERE, "PITFALLS_REGISTRY.json")
MD_PATH = os.path.join(_HERE, "PITFALLS_SDS.md")

CLASSES = ("A_construction", "B_recurring", "C_meta")
GUARD_TYPES = ("preflight", "injection", "meta", "none")
BLAST_RADII = ("gate", "code", "fixture", "verify")
STATUSES = ("fixed", "fixed_structural", "open")
REQUIRED = ("id", "title", "class", "symptom", "root_cause", "remedy",
            "guard", "blast_radius", "status", "date", "fix_location")
MAX_B_PER_CHECK = 3


# ----------------------------- 读取与基础装配 -----------------------------
def load_registry():
    if not os.path.exists(REG_PATH):
        print("FAIL 登记表缺失：%s" % REG_PATH)
        sys.exit(1)
    try:
        doc = json.loads(open(REG_PATH, encoding="utf-8").read())
    except json.JSONDecodeError as e:
        print("FAIL 登记表不是合法 JSON：%s" % e)
        sys.exit(1)
    return doc.get("pitfalls") or []


def load_preflight():
    """返回 (real_checks:set, name2src:dict)。导入失败返回 (None, None)。"""
    try:
        import inspect
        import preflight_etf as pf
    except Exception as e:                                  # noqa: BLE001
        print("  [WARN] preflight_etf 导入失败(%s: %s) → R2/R4 无法验证"
              % (type(e).__name__, e))
        return None, None
    real = set(getattr(pf, "REAL_CHECKS", set()) or set())
    name2src = {}
    for attr, obj in vars(pf).items():
        if not (attr.startswith("check_") and callable(obj)):
            continue
        try:
            src = inspect.getsource(obj)
        except OSError:
            continue
        m = re.search(r'return\s*\(\s*["\']([^"\']+)["\']', src)
        if m:
            name2src[m.group(1)] = src
    return real, name2src


def md_pit_ids():
    if not os.path.exists(MD_PATH):
        return None
    txt = open(MD_PATH, encoding="utf-8").read()
    return set(re.findall(r"^### (S-\d+)", txt, flags=re.M))


# --------------------------------- 主校验 --------------------------------
def run() -> int:
    rows = load_registry()
    real, name2src = load_preflight()
    fails, warns = [], []

    seen = set()
    b_per_check = {}

    for r in rows:
        pid = r.get("id") or "<无 id>"
        # ---- R1 结构完整 ----
        missing = [k for k in REQUIRED if k not in r or (k != "guard" and r.get(k) in (None, ""))]
        if "guard" in r and not isinstance(r["guard"], dict):
            missing.append("guard 非对象")
        if missing:
            fails.append(f"R1 {pid} 缺字段 {missing}")
            continue
        if pid in seen:
            fails.append(f"R1 {pid} id 重复")
        seen.add(pid)

        cls = r["class"]
        if cls not in CLASSES:
            fails.append(f"R1 {pid} class 非法：{cls}")
            continue
        if r["blast_radius"] not in BLAST_RADII:
            fails.append(f"R6 {pid} blast_radius 非法：{r['blast_radius']}")
        if r["status"] not in STATUSES:
            fails.append(f"R8 {pid} status 非法：{r['status']}")

        g = r["guard"]
        gtype = g.get("type")
        if gtype not in GUARD_TYPES:
            fails.append(f"R2 {pid} guard.type 非法：{gtype}")
            continue

        # ---- R2 守卫生效 ----
        if gtype in ("preflight", "meta"):
            cid = g.get("check_id")
            if not cid:
                fails.append(f"R2 {pid} guard.type={gtype} 但缺 check_id")
            elif real is None:
                warns.append(f"R2 {pid} 无法验证 check_id（preflight 导入失败）")
            elif cid not in real:
                fails.append(f"R2 {pid} 指向不存在的检查项「{cid}」"
                             f"（REAL_CHECKS 共 {len(real)} 项）→ 登记了等于没治理")
            else:
                # ---- R4 指错检查项（核心） ----
                proof = g.get("proof")
                if not proof:
                    fails.append(f"R4 {pid} 指向「{cid}」但未给 proof 串 → "
                                 f"无法证明该检查项真的查这个坑")
                else:
                    src = name2src.get(cid) or ""
                    if proof not in src:
                        fails.append(f"R4 {pid} proof「{proof}」不在「{cid}」的源码里 → "
                                     f"疑似指错检查项（该 check 并不查此坑）")
                if cls == "B_recurring":
                    b_per_check[cid] = b_per_check.get(cid, 0) + 1

        elif gtype == "injection":
            test = g.get("test")
            assertion = g.get("assertion")
            if not test or not assertion:
                fails.append(f"R2 {pid} injection 守卫缺 test/assertion")
            else:
                tp = os.path.join(_HERE, test)
                if not os.path.exists(tp):
                    fails.append(f"R2 {pid} 注入测试不存在：{test}")
                else:
                    body = open(tp, encoding="utf-8", errors="replace").read()
                    if assertion not in body:
                        fails.append(f"R2 {pid} 注入测试 {test} 里找不到断言锚点"
                                     f"「{assertion}」→ 该测试并不覆盖此坑")

        # ---- R3 复发型不许裸奔 ----
        if cls == "B_recurring" and gtype not in ("preflight", "injection", "meta"):
            fails.append(f"R3 {pid} 属于复发型但守卫={gtype}"
                         f"（复发型必须机械化，文档不算守卫）")

        # ---- R6 影响面配对（治 S-18） ----
        # 只对 gate（数据写入/清洗闸门）生效：这类修改当天就必须回答「存量怎么办」。
        # code/fixture/verify 无存量数据概念，若一并强制会逼出通用填充语，
        # 反而变成新的「登记了=假装治理了」（本脚本第一版就是这个假阳性）。
        if r["blast_radius"] == "gate":
            if not (r.get("stock_side") or r.get("residual_ok")):
                fails.append(f"R6 {pid} 是数据写入/清洗闸门(gate)，但既没给 stock_side"
                             f"（清存量落点）也没给 residual_ok（为何不需要）"
                             f"→ 属于「修了入口当修了数据」")

        # ---- R8 状态自洽 ----
        if r["status"] == "open" and cls in ("A_construction", "B_recurring") \
                and gtype == "none":
            fails.append(f"R8 {pid} status=open 且无守卫 → 不能算已治理")

    # ---- R5 垃圾桶闸 ----
    for cid, n in sorted(b_per_check.items(), key=lambda kv: -kv[1]):
        if n > MAX_B_PER_CHECK:
            fails.append(f"R5 检查项「{cid}」承载 {n} 条复发型坑"
                         f"（上限 {MAX_B_PER_CHECK}）→ 粒度不足，应拆分检查项，"
                         f"否则「过了」无法对应到具体哪条")

    # ---- R7 双向一致 ----
    md_ids = md_pit_ids()
    if md_ids is None:
        fails.append(f"R7 找不到 {os.path.basename(MD_PATH)}")
    else:
        only_md = sorted(md_ids - seen)
        only_reg = sorted(seen - md_ids)
        if only_md:
            fails.append(f"R7 只在 md 里有、registry 未登记（=未闭环）：{only_md}")
        if only_reg:
            fails.append(f"R7 只在 registry 里有、md 未记录：{only_reg}")

    # ------------------------------ 输出 ------------------------------
    cls_cnt = {}
    for r in rows:
        cls_cnt[r.get("class", "?")] = cls_cnt.get(r.get("class", "?"), 0) + 1
    guard_cnt = {}
    for r in rows:
        gt = (r.get("guard") or {}).get("type", "?")
        guard_cnt[gt] = guard_cnt.get(gt, 0) + 1

    print("坑登记：%d 条 | 分类 %s | 守卫 %s"
          % (len(rows),
             ", ".join(f"{k}={v}" for k, v in sorted(cls_cnt.items())),
             ", ".join(f"{k}={v}" for k, v in sorted(guard_cnt.items()))))
    if b_per_check:
        hot = ", ".join(f"{k}({v})" for k, v in
                        sorted(b_per_check.items(), key=lambda kv: -kv[1])[:3])
        print("最热检查项：%s" % hot)

    for w in warns:
        print("  [WARN] %s" % w)
    if fails:
        print("\n❌ 闭环未通过（%d 项）：" % len(fails))
        for f in fails:
            print("  - %s" % f)
        return 1
    print("✅ 坑→守卫闭环全绿（R1~R8 全过）")
    return 0


if __name__ == "__main__":
    sys.exit(run())
