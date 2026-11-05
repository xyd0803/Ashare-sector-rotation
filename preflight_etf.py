# -*- coding: utf-8 -*-
"""XYD-供需驱动板块 · 运行前体检（preflight）。

🔴 存在理由（2026-10-07 实测立此脚本）
============================================
两只 ETF 的**纯机器全链路（裁决+卡片+出图）实测仅 3005 ms**，但端到端花掉约 1 小时
→ 放大 ~1200 倍，机器环节占比 0.08%，其余 99.92% 全是 agent"跑→炸→读源码→改→重跑"的往返。

而当天撞到的 **9 堵墙，9/9 都是"运行前即可静态检出"**：
  ① analyze_etf 不接受 --no-refresh（参数契约）  ② fills 含锁定字段（字段比对）
  ③ managed python 无 pandas（依赖）              ④ fetch_one 无 metric 形参（签名）
  ⑤ as_of 虚报新鲜（值校验）                      ⑥ _pipeline_meta 无 warnings（结构契约）
  ⑦ ingest 变量作用域 NameError（静态可抓）       ⑧ 脚本硬编码结论（字面量扫描）
  ⑨ make_stats 统计块不一致（已有脚本却没前置）
全部靠"跑到一半才炸"来暴露 → 每堵墙 = 1 轮完整往返。

本脚本把这 9 类检查**一次性**跑完（秒级、不联网、不消耗积分）：
- exit 0 = 全通过，可以直跑管线
- exit 3 = 存在 MUST 级阻断，**劝阻直跑**（先按报告修完，别去撞墙）
- exit 2 = 脚本自身异常（不应发生）

用法：
  python preflight_etf.py                       # 全量体检
  python preflight_etf.py --codes 159755,515790 # 指定 ETF（查 fills/缓存新鲜度）
  python preflight_etf.py --json                # 机器可读输出
"""
from __future__ import annotations

import argparse
import ast
import glob
import inspect
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime

SKILL = os.path.dirname(os.path.abspath(__file__))
HERE = SKILL
PB_DIR = os.path.join(SKILL, "data", "index_daily_pb")
PE_DIR = os.path.join(SKILL, "data", "index_daily_pe")
MAP_PATH = os.path.join(SKILL, "data", "meta", "etf_index_map.json")
# 定位 managed venv 解释器（多条候选，推导失败不掩盖真实问题）
_cands = []
_p = os.path.dirname(sys.executable)                       # .../versions/3.13.12
for _ in range(2):                                          # -> .../versions -> .../python
    _p = os.path.dirname(_p)
_cands.append(os.path.join(_p, "envs", "default", "Scripts", "python.exe"))
_home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
_cands.append(os.path.join(_home, ".workbuddy", "binaries", "python",
                           "envs", "default", "Scripts", "python.exe"))
VENV_PY = next((c for c in _cands if os.path.exists(c)), _cands[-1])

# 机械派生/锁定字段：由草稿自动生成，FILLS 若含之 → 合并必被 llm_fill_guard 拦（exit 4）
LOCKED_FIELDS = {
    "pb_now", "pb_pct", "pb_p5", "pb_p20", "pb_p50", "pb_p80", "pb_p95", "pb_series",
    "pb_manual", "pb_sources", "pe_now", "pe_manual", "pe_sources", "pe_pct",
    "pe_pct_period", "pe_p5", "pe_p20", "pe_p50", "pe_p80", "pe_p95",
    "pe_series_years", "pe_series_freq", "pe_field_label", "pe_series",
}
# 硬编码结论：**只盯会污染输出的 status/quad 字面量**。UI 标签（"建议仓位"）与配色映射
# key（"黄金买入区"）是合法常量，必须排除——否则全是误报，反而制造新的往返（2026-10-07 教训）。
HARDCODE_STATUS = ["PENDING_VERIFICATION", "VERDICT", "value_trap", "OLD_VERDICT"]
# 结论词只在"孤立短字符串"时才算硬编码：长 HTML 串里罗列规则名属合法；UI 区段标签
# （"鱼头(买)"）与配色映射 key（"黄金买入区"）也合法，一律不再误伤（2026-10-07 教训）。
HARDCODE_CONCLUSION = ["价值陷阱", "假便宜", "左侧[^布]*陷阱"]
HARDCODE_MAXLEN = 12
HARDCODE_WHITELIST = {
    # 测试/回测/红队：用例里出现结论字面量属正常输入
    "redteam_sector.py", "stress_test.py", "cap_test.py", "strong_test.py",
    "invest_test.py", "backtest.py", "backtest_concentrated.py", "pe_test.py",
    "magnitude_test_real.py", "selfcheck_all.py", "discipline.py",
    # 🔴 gen_etf_card.py = SKILL.md 登记的「旧版对照卡片脚本」（有意保留 PENDING 脚注、
    # 用作新旧对照），**无任何脚本调用它**。它的硬编码是设计如此，不是缺陷 → 豁免。
    # 现行链路用 render_verdict_card.py + gen_etf_viz.py（后者已改为动态读取 verdict）。
    "gen_etf_card.py",
}

# preflight 实际能兑现的检查项清单。PITFALLS_SDS.md 的 `@preflight:` 标签必须落在此集合内，
# 否则 check_pitfall_coverage 判为"登记了假兵器" → MUST。新增 check_* 时**务必**同步此集合。
REAL_CHECKS = {
    "依赖 pandas", "fetch_one 签名", "CLI 参数契约", "缓存诚实性",
    "硬编码结论扫描", "warnings 落盘契约", "SKILL 统计块", "FILLS 字段冲突",
    "静态 lint", "财报周期闸", "iFinD 预算闸", "quant_raw 装配匹配",
    "连接器就绪矩阵", "健康度与欠账闸", "坑登记闭环",
}

FRESHNESS_MAX_DAYS = 15
# 财报周期闸宽限期：法定披露截止日后 N 天内只 WARN，超过则 MUST（避免披露当日就锁死）
REPORT_CYCLE_GRACE_DAYS = 30


def _days(a: str, b: str | None = None) -> int | None:
    b = b or date.today().isoformat()
    try:
        return abs((datetime.fromisoformat(b) - datetime.fromisoformat(a)).days)
    except Exception:
        return None


def _codes_to_index_keys(codes: list[str] | None) -> dict[str, str]:
    """ETF 代码 → 本地缓存键（{缓存键: 用户输入的原始代码}）。空输入返回 {}。

    为什么要这层转换（2026-11-04）：新鲜度/财报周期两条闸原先**无条件遍历
    `data/index_daily_pb|pe` 全量缓存**。库里 56 只指数有 30 只停在旧期时，
    分析一只**已刷新**的 ETF 也会被 30 条无关告警淹没——信号被噪音吃掉，
    「检查数据新鲜度」变成不可执行的口号。本函数把 `--codes` 的 ETF 映射到
    它真正的缓存键（`etf_index_map.json` 的 `index_code`），让闸能聚焦目标。

    两种合法输入：
      · ETF 代码（`513180`）→ 经映射表取 `HSTECH.HI`；
      · 直接给缓存键（行业指数场景，`000510.CSI`）→ 原样使用（映射表查不到即按缓存键处理）。
    映射表读不到时降级为「原样当缓存键」——宁可查不到也不虚构映射。
    """
    codes = [c.strip() for c in (codes or []) if c and c.strip()]
    if not codes:
        return {}
    try:
        m = json.load(open(MAP_PATH, encoding="utf-8"))
        if not isinstance(m, dict):
            m = {}
    except Exception:
        m = {}
    out: dict[str, str] = {}
    for c in codes:
        ent = m.get(c) or m.get(c.upper())
        ic = ent.get("index_code") if isinstance(ent, dict) else None
        out[str(ic) if ic else c] = c
    return out


# ----------------------------------------------------------------- 检查项实现

def check_deps() -> tuple[str, str, list[str]]:
    """③ 刷新器依赖：任一可用解释器能 import pandas（缺 → 刷新静默失效，缓存永远 stale）。"""
    errs = []
    ok = False
    for py in [sys.executable, VENV_PY]:
        if not py or not os.path.exists(py):
            continue
        try:
            r = subprocess.run([py, "-c", "import pandas"], capture_output=True, timeout=60)
            if r.returncode == 0:
                ok = True
                break
        except Exception:
            continue
    if not ok:
        errs.append("无任一解释器可 import pandas → 自动刷新必静默失效（缓存长期 stale 的真凶）")
    return ("依赖 pandas", "MUST" if not ok else "OK", errs)


def check_fetcher_signature() -> tuple[str, str, list[str]]:
    """④ fetch_one 必须接受 metric（否则 PE 路径 100% 崩溃 TypeError）。"""
    errs = []
    try:
        sys.path.insert(0, SKILL)
        import fetch_pb_mxdata as m  # noqa: E402
        sig = inspect.signature(m.fetch_one)
        if "metric" not in sig.parameters:
            errs.append(f"fetch_one{sig} 不接受 metric → --metric pe 必崩 TypeError")
    except Exception as e:
        errs.append(f"fetch_pb_mxdata 导入失败: {type(e).__name__}: {e}")
    return ("fetch_one 签名", "MUST" if errs else "OK", errs)


def check_cache_honesty(codes: list[str] | None = None) -> tuple[str, str, list[str]]:
    """⑤ 缓存诚实性：as_of 必须 == 序列末值日期（虚报新鲜会让新鲜度门失效）。

    2026-11-04 分治：`as_of 虚报` 全量查（数据诚信问题，与"本次看谁"无关）；
    `stale > FRESHNESS_MAX_DAYS` 按 `--codes` 聚焦（有 codes 时只报目标，
    其余折成一行汇总）。原因同 `_codes_to_index_keys` 注释：无关陈旧的 30 只
    会把真实目标的 stale 信号淹没。无 codes → 保持全量（零回归）。
    """
    keys = _codes_to_index_keys(codes)
    scoped = bool(keys)
    errs, warns, infos = [], [], []
    stale_others = 0
    for d, tag in [(PB_DIR, "PB"), (PE_DIR, "PE")]:
        if not os.path.isdir(d):
            continue
        for fp in sorted(glob.glob(os.path.join(d, "*.json"))):
            try:
                j = json.load(open(fp, encoding="utf-8"))
            except Exception:
                continue
            series = j.get("pb") or []
            if not series:
                continue
            tail = str(series[-1][0])
            aof = str(j.get("as_of") or "")
            name = os.path.basename(fp)
            if aof and aof != tail:
                errs.append(f"{name}({tag}) as_of={aof} 但序列末值={tail} → 虚报新鲜 {_days(aof, tail)} 天")
            gap = _days(tail)
            if gap is not None and gap > FRESHNESS_MAX_DAYS:
                if scoped and os.path.basename(fp)[:-5] not in keys:
                    stale_others += 1
                else:
                    warns.append(f"{name}({tag}) stale {gap} 天 >{FRESHNESS_MAX_DAYS} 天")
    if stale_others:
        infos.append(f"库内另有 {stale_others} 只无关缓存 stale >{FRESHNESS_MAX_DAYS} 天"
                     f"（本次目标未命中·已忽略；要看全量就去掉 --codes）")
    lvl = "MUST" if errs else ("WARN" if warns else "OK")
    return ("缓存诚实性", lvl, errs + warns + infos)


def _report_deadlines(today: date | None = None) -> list[tuple[str, date]]:
    """近两年的「报告期 → 法定披露截止日」：Q1=04-30 / H1=08-31 / Q3=10-31 / FY=次年04-30。"""
    t = today or date.today()
    out = []
    for y in (t.year - 1, t.year):
        out += [(f"{y}0331", date(y, 4, 30)), (f"{y}0630", date(y, 8, 31)),
                (f"{y}0930", date(y, 10, 31)), (f"{y}1231", date(y + 1, 4, 30))]
    return sorted(out, key=lambda x: x[1])


def check_report_cycle(codes: list[str] | None = None) -> tuple[str, str, list[str]]:
    """⑤b 财报周期闸（2026-10-24·跨技能同类漏检修复；2026-11-04 按目标聚焦）。

    病灶：本 skill 的 PB/PE 序列是「日频点位 ÷ 慢变量 BVPS/EPS」重建出来的
    （rebuild_pb_daily.py 明文："BVPS 为慢变量（财报级更新）"）。一旦新的法定披露期
    已过、而序列 as_of 仍停在该截止日之前，BVPS/EPS 锚点就**不可能**包含最新财报
    → PB = 点位/BVPS 中分母偏小 → PB 系统性偏大 → 分位偏高 → 锚价偏低 → 结论方向性失真。
    这与 个股盈利面框架 的 Q3-EARNINGS-STALE-01 是**同一类病害的另一个载体**：
    旧 preflight 只有「as_of 是否虚报 / 是否 >15 天」两条，都没有"是否跨过财报期"这个维度。

    判定：取**已过截止日**里最晚那个报告期 D；序列 as_of < D → 该序列未含该期财报。
      · 距 D 已 > REPORT_CYCLE_GRACE_DAYS → MUST（不该拖这么久还没重建）
      · 否则 → WARN（刚过披露期，留出重建窗口，不误杀）

    2026-11-04 增加 `codes` 聚焦（默认 None = 原有全量行为，零回归）：
      传 `--codes` 时，只有**本次目标命中的缓存**能升 MUST/WARN，无关陈旧缓存折成一行
      汇总；目标全部新鲜时返回 OK 并显式列出「已含 rp 财报」，让人一眼确认。
      目标在本地**没有缓存**时升 WARN（分位根本算不出来，必须先取数）——
      这是"检查新鲜度"最该报出来的一种情况，此前完全静默。
    """
    t = date.today()
    passed = [(p, d) for p, d in _report_deadlines(t) if d <= t]
    if not passed:
        return ("财报周期闸", "OK", [])
    rp, dl = passed[-1]
    tgt = _codes_to_index_keys(codes)
    scoped = bool(tgt)
    errs, warns, infos = [], [], []
    checked = 0
    hit: dict[str, str] = {}      # 缓存键 → 该目标最新 as_of
    stale_tgt: set[str] = set()
    stale_others = 0
    for d, tag in [(PB_DIR, "PB"), (PE_DIR, "PE")]:
        if not os.path.isdir(d):
            continue
        for fp in sorted(glob.glob(os.path.join(d, "*.json"))):
            try:
                j = json.load(open(fp, encoding="utf-8"))
            except Exception:
                continue
            series = j.get("pb") or j.get("pe") or []
            if not series:
                continue
            checked += 1
            tail = str(series[-1][0])[:10]
            stroke = os.path.basename(fp)[:-5] if fp.endswith(".json") else os.path.basename(fp)
            is_tgt = (stroke in tgt) if scoped else True
            if is_tgt and stroke in tgt:
                prev = hit.get(stroke)
                hit[stroke] = tail if not prev else max(prev, tail)
            try:
                lag = (dl - date.fromisoformat(tail)).days
            except Exception:
                continue
            if lag <= 0:
                continue
            name = os.path.basename(fp)
            # 宽限按「距披露截止日已过多少天」计（不是 as_of 差几天）：
            # 披露截止日刚过时数据本就来不及重建 → 给 30 天窗口；过 30 天仍停在截止日前 = 拖账。
            overdue = (t - dl).days
            msg = (f"{name}({tag}) as_of={tail} 早于 {rp} 报告期披露截止 {dl}"
                   f"（as_of 差 {lag} 天·披露截止已过 {overdue} 天）"
                   f"→ BVPS/EPS 锚点未含该期财报，PB/PE 分位系统性失真")
            if is_tgt:
                stale_tgt.add(stroke)
                (errs if overdue > REPORT_CYCLE_GRACE_DAYS else warns).append(msg)
            else:
                stale_others += 1
    if scoped:
        missing = sorted(k for k in tgt if k not in hit)
        fresh = sorted(k for k in hit if k not in stale_tgt)
        for k in missing:
            src = tgt.get(k, "")
            warns.append(f"{k}（输入 {src}）本地无 PB/PE 缓存 → 分位/锚价算不出来，"
                         f"先取数再分析（etf_index_map 无此映射时确认传的是缓存键）")
        for k in fresh:
            infos.append(f"{k} as_of={hit[k]} 已含 {rp} 财报 ✓")
        if stale_others:
            infos.append(f"库内另有 {stale_others} 只无关缓存停在该截止日前"
                         f"（本次目标未命中·已忽略；要看全量就去掉 --codes）")
    if errs:
        errs = errs[:6] + ([f"…另有 {len(errs) - 6} 个同类（共 {len(errs)}/{checked}）"] if len(errs) > 6 else [])
    lvl = "MUST" if errs else ("WARN" if warns else "OK")
    return ("财报周期闸", lvl, errs + warns + infos)


def check_hardcode() -> tuple[str, str, list[str]]:
    """⑧ 硬编码结论：用 AST 取真实字符串常量（跳过 docstring/注释，避免误报）。"""
    errs = []
    for fp in sorted(glob.glob(os.path.join(SKILL, "*.py"))):
        b = os.path.basename(fp)
        if b in HARDCODE_WHITELIST or b == "preflight_etf.py":
            continue
        try:
            tree = ast.parse(open(fp, encoding="utf-8").read())
        except Exception:
            continue
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if (node.body and isinstance(node.body[0], ast.Expr)
                        and isinstance(node.body[0].value, ast.Constant)
                        and isinstance(node.body[0].value.value, str)):
                    docstrings.add(node.body[0].value.value)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                s = node.value
                if s in docstrings:
                    continue
                hit = None
                if any(kw in s for kw in HARDCODE_STATUS):
                    hit = "status/常量"
                elif len(s) <= HARDCODE_MAXLEN and any(kw in s for kw in HARDCODE_CONCLUSION):
                    hit = "结论词(孤立)"
                if hit:
                    errs.append(f"{b}:{node.lineno} 硬编码[{hit}] {s[:60]!r} → 须改为动态读取 verdict")
    return ("硬编码结论扫描", "MUST" if errs else "OK", errs)


def check_cli_contract() -> tuple[str, str, list[str]]:
    """① 参数契约：analyze_etf 必须支持 --etf/--sector/--fills/--out。"""
    errs = []
    try:
        src = open(os.path.join(SKILL, "analyze_etf.py"), encoding="utf-8").read()
        for opt in ["--etf", "--sector", "--fills", "--out"]:
            if opt not in src:
                errs.append(f"analyze_etf.py 缺少 {opt}")
    except Exception as e:
        errs.append(f"读取 analyze_etf.py 失败: {e}")
    return ("CLI 参数契约", "MUST" if errs else "OK", errs)


def check_meta_warnings_contract() -> tuple[str, str, list[str]]:
    """⑥ _pipeline_meta 必须落 warnings（否则刷新失败/stale 水印在草稿里完全不可见）。"""
    warns = []
    try:
        src = open(os.path.join(SKILL, "run_sector_pipeline.py"), encoding="utf-8").read()
        if '"warnings"' not in src and "'warnings'" not in src:
            warns.append("run_sector_pipeline 未见 warnings 落盘 → 失败不可见")
    except Exception as e:
        warns.append(f"读取失败: {e}")
    return ("warnings 落盘契约", "WARN" if warns else "OK", warns)


def check_release_stats() -> tuple[str, str, list[str]]:
    """⑨ 发布闸：SKILL.md 统计块必须与脚本实际一致（改脚本后必跑 make_stats）。"""
    errs = []
    r = subprocess.run([sys.executable, os.path.join(SKILL, "make_stats.py"), "--check"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    if r.returncode != 0:
        errs.append("SKILL.md 统计块过期 → 跑 python make_stats.py 再生（否则 run_all 发布闸必红）")
    return ("SKILL 统计块", "MUST" if errs else "OK", errs)


def check_fills(codes: list[str]) -> tuple[str, str, list[str]]:
    """② FILLS 不得含锁定机械字段（含之 → 合并 exit 4）。"""
    errs, checked = [], 0
    pats = []
    if codes:
        for c in codes:
            pats += glob.glob(os.path.join(SKILL, "runs", "*", f"fills_*{c}*.json"))
    else:
        # 无 codes → 扫全量，避免检查空转跳过（2026-10-07 自修）
        pats = glob.glob(os.path.join(SKILL, "runs", "*", "fills_*.json"))
        pats = [p for p in pats if "_legacy_fills" not in p]
    for fp in sorted(set(pats)):
        checked += 1
        try:
            j = json.load(open(fp, encoding="utf-8"))
        except Exception:
            continue
        bad = sorted(LOCKED_FIELDS & set(j))
        if bad:
            errs.append(f"{os.path.basename(fp)} 含锁定字段 {bad} → 合并必被拦（清空这些键）")
    if not checked:
        errs.append(f"未找到任何 fills（codes={codes or 'ALL'}）→ 跳过此项")
        return ("FILLS 字段冲突", "WARN", errs)
    return ("FILLS 字段冲突", "MUST" if errs else "OK", errs)


def check_ifind_budget() -> tuple[str, str, list[str]]:
    """D-3（2026-10-25）iFinD 预算闸：付费源消耗必须在**运行前**可见、可问责。

    三条子检查：
      ① 归因接线：ifind_fetch.py / ifind_fix_pb.py 必须 set_attribution——
         两脚本经共享 call.py 取数，该 call() 内置 require_tag（无 tag 拒绝执行），
         漏接 = 一跑就被 fail-closed 拒（或被迫设全局 env 掩盖归因缺失）；
      ② 今日用量 vs 日上限（80·用户 2026-10-16 指定）：耗尽且本次运行会尝试
         刷新（未传 --no-refresh）且无逃生门 → **MUST exit 3**（fail-closed；
         解法 = 本地优先 --no-refresh，或 XYD_IFIND_BUDGET_OK=1 单次授权）；
      ③ ≥80% 上限 → WARN（接近耗尽，非免费源优先）。
    体检自身异常（账本读失败等）→ WARN 不阻断（工具坏不成为新阻断源）。
    """
    msgs, level = [], "OK"
    try:
        if SKILL not in sys.path:
            sys.path.insert(0, SKILL)
        import sds_ifind_budget as ib
        used, cp = ib.today_count(), ib.cap()
        # ① 归因接线（防"假兵器"：脚本没接线 = require_tag 必拒）
        for fn in ("ifind_fetch.py", "ifind_fix_pb.py"):
            p = os.path.join(SKILL, fn)
            if not os.path.exists(p):
                msgs.append(f"[MUST] {fn} 不存在（归因接线无法核验）")
                level = "MUST"
                continue
            src = open(p, encoding="utf-8").read()
            if "set_attribution(" not in src:
                msgs.append(f"[MUST] {fn} 未 set_attribution → call.py require_tag 会 "
                            f"fail-closed 拒绝执行（补 --tag + set_attribution）")
                level = "MUST"
        # ② 耗尽且将尝试刷新 → 阻断（本地优先/逃生门两选）
        escaped = (os.environ.get("XYD_IFIND_BUDGET_OK") == "1"
                   or "--allow-over-budget" in sys.argv)
        local_first = "--no-refresh" in sys.argv
        if used >= cp and not escaped and not local_first:
            msgs.append(f"[MUST] iFinD 今日已用 {used}/{cp} 次（日上限已耗尽）且本次运行"
                        f"会尝试刷新 → call() 将 fail-closed 拦截。解法：① 本地优先 "
                        f"analyze_etf/run_sector_pipeline 加 --no-refresh；"
                        f"② 确属必要 XYD_IFIND_BUDGET_OK=1（单次授权 XYD_IFIND_OVER_N 次）；"
                        f"③ 免费源优先（neodata ingest_neodata_valuation）")
            level = "MUST"
        elif used >= cp and escaped:
            msgs.append(f"[INFO] 预算已耗尽但逃生门已开（单次授权）→ 放行并逐笔记账")
            if level == "OK":
                level = "OK"
        elif used >= 0.8 * cp:
            msgs.append(f"[WARN] iFinD 今日已用 {used}/{cp}（≥80% 上限）→ 非免费源克制使用")
            if level == "OK":
                level = "WARN"
        else:
            msgs.append(f"[OK] {ib.budget_report()}")
        if not ib.is_shared():
            msgs.append(f"[WARN] 共享 ifind_budget 不可用（{ib.shared_error()}）→ 内嵌等价闸"
                        f"（同账本·cap={cp}）")
            if level == "OK":
                level = "WARN"
    except Exception as e:  # 体检自身异常 → 不阻断
        return ("iFinD 预算闸", "WARN", [f"预算体检自身异常（不阻断）: {type(e).__name__}: {e}"])
    return ("iFinD 预算闸", level, msgs)


def check_quant_raw_assembly() -> tuple[str, str, list[str]]:
    """S-12（2026-10-25）：quant_raw 机械装配健康度——两类缺陷显式化。

    ① 匹配缺陷（MUST）：data/etf 下存在含某 6 位 ETF 代码的缓存文件，但
       `_assemble_quant_raw` 找不到它（历史精确名匹配曾漏 `etf_159755_daily.json`
       这类命名；现已 glob，此检查防回归）；
    ② 覆盖缺口（WARN）：etf_index_map 里的 ETF 无月频缓存 → 五维量化胜率
       静默回退六档表（2026-10-23 159755 实证：覆盖 54/55，缺口不可见）。
    体检自身异常（import 失败等）→ WARN 不阻断。
    """
    try:
        if SKILL not in sys.path:
            sys.path.insert(0, SKILL)
        import run_sector_pipeline as rp
        # ① 匹配缺陷：缓存文件在、装配却找不到
        miss_match = []
        for p in sorted(glob.glob(os.path.join(SKILL, "data", "etf", "*.json"))):
            b = os.path.basename(p)
            m = re.search(r"(\d{6})", b)
            if not m:
                continue
            qr, reason = rp._assemble_quant_raw(m.group(1))
            if qr is None:
                miss_match.append(f"{b} → _assemble_quant_raw({m.group(1)}) 返回 None（{reason}）")
        if miss_match:
            return ("quant_raw 装配匹配", "MUST",
                    ["[MUST] 缓存文件存在但装配找不到（匹配回归）："] + miss_match)
        # ② 覆盖缺口：map 里的 ETF 无月频缓存 → 五维量化静默回退
        try:
            mp = json.load(open(MAP_PATH, encoding="utf-8"))
            etfs = sorted(mp.keys())
        except Exception:
            etfs = []
        gaps = []
        for e in etfs:
            qr, reason = rp._assemble_quant_raw(e)
            if qr is None:
                gaps.append(f"{e}（{reason}）")
        if gaps:
            return ("quant_raw 装配匹配", "WARN",
                    [f"[WARN] {len(gaps)}/{len(etfs)} 只 ETF 无月频缓存 → 五维量化胜率"
                     f"静默回退六档表（S-12）："] + gaps[:20])
        return ("quant_raw 装配匹配", "OK",
                [f"[OK] data/etf 缓存全部可装配，映射 {len(etfs)} 只 ETF 全覆盖"])
    except Exception as e:
        return ("quant_raw 装配匹配", "WARN",
                [f"装配体检自身异常（不阻断）: {type(e).__name__}: {e}"])


# ----------------------------------------------------------------- 主流程

def check_connector_matrix() -> tuple[str, str, list[str]]:
    """E-1（2026-10-25）连接器就绪矩阵：把「源挂了才被动应对」变成**运行前可见**。

    病灶（earnings skill 10-20/10-21 七批次实证的同病害）：mx-ds SSL EOF /
    东财超时反复出现，每次都是"源失败了才救火"，救火方案每批各写一套 →
    数据救火占一半以上 agent 轮次。SDS 侧同源：fetch_pb_mxdata 妙想直连偶发
    0 返回、run_sector_pipeline 新浪取价、MCP 侧 neodata/ifind/tdx/westock。

    机械化边界（诚实声明）：
      · 脚本进程只能探测**宿主网络面**：新浪行情 HTTP、妙想直连 mkapi2 TLS、
        妙想/neodata MCP 宿主 mxapi TLS、iFinD 账本余量；
      · tdx/westock 是本地 client（无固定端口可探）→ 标注「agent 侧」，
        由 agent 用 list_installed_plugins 核验（≥3/5 开跑纪律）；
      · 源 down **不阻断**（本地缓存可离线跑）→ WARN + 选路提示，不做 MUST；
      · 探测自身异常 → WARN fail-open（工具坏不成为新阻断源）。
    落盘 .evidence/source_health_<date>.json 供审计（防"源挂了但记录说健康"）。
    """
    msgs = []
    matrix = {}
    try:
        import socket
        import urllib.request
        # ① 新浪行情（run_sector_pipeline 的现价源）
        try:
            req = urllib.request.Request("https://hq.sinajs.cn/list=sh000001",
                                         headers={"Referer": "https://finance.sina.com.cn"})
            with urllib.request.urlopen(req, timeout=6) as r:
                matrix["新浪行情"] = "UP" if r.status == 200 else f"DOWN(http {r.status})"
        except Exception as e:
            matrix["新浪行情"] = f"DOWN({type(e).__name__})"
        # ② 妙想直连 mkapi2（fetch_pb_mxdata 的指数估值主源）
        for label, host in (("妙想直连mkapi2", "mkapi2.dfcfs.com"),
                            ("妙想/neodata MCP宿主mxapi", "mxapi.eastmoney.com")):
            try:
                with socket.create_connection((host, 443), timeout=6) as s:
                    matrix[label] = "UP"
            except Exception as e:
                matrix[label] = f"DOWN({type(e).__name__})"
        # ③ iFinD 账本余量（付费源：有余额=可用）
        try:
            if SKILL not in sys.path:
                sys.path.insert(0, SKILL)
            import sds_ifind_budget as ib
            used, cp = ib.today_count(), ib.cap()
            matrix["iFinD(账本)"] = "UP" if used < cp else f"DOWN(配额 {used}/{cp})"
        except Exception as e:
            matrix["iFinD(账本)"] = f"UNKNOWN({type(e).__name__})"
        # ④ agent 侧连接器（脚本进程不可探·如实标注）
        matrix["tdx(agent侧)"] = "UNKNOWN(须list_installed_plugins核验)"
        matrix["westock(agent侧)"] = "UNKNOWN(须list_installed_plugins核验)"
        # 审计落盘（best-effort，失败不阻断）
        try:
            ev = os.path.join(SKILL, ".evidence")
            os.makedirs(ev, exist_ok=True)
            json.dump({"date": date.today().isoformat(), "matrix": matrix},
                      open(os.path.join(ev, f"source_health_{date.today().isoformat()}.json"),
                           "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        except Exception:
            pass
        up = sum(1 for v in matrix.values() if v == "UP")
        msgs.append(f"矩阵：{json.dumps(matrix, ensure_ascii=False)}（UP {up}/4 可探面）")
        if up < 3:
            msgs.append("[WARN] 可探面 UP <3 → 按选路提示执行："
                        "妙想mkapi2 down → neodata ingest_neodata_valuation（免费优先）"
                        "或 iFinD 预算内降级；新浪 down → tdx/westock data_quote（agent 侧）；"
                        "全断 → 本地缓存优先（--no-refresh），禁 silent 降级")
            level = "WARN"
        else:
            level = "OK"
    except Exception as e:
        msgs.append(f"[WARN] 探测自身异常（fail-open 不阻断）：{type(e).__name__}: {e}")
        level = "WARN"
    return ("连接器就绪矩阵", level, msgs)


def run(codes: list[str] | None = None) -> list[dict]:
    codes = codes or []
    results = []
    # 逐项注册（`codes` 只喂给需要聚焦目标的闸：缓存诚实性/财报周期/FILLS；
    # 其余为库级健康闸，传了也无意义 · 2026-11-04）
    for fn in (check_deps, check_fetcher_signature, check_cli_contract,
               check_hardcode, check_release_stats,
               check_meta_warnings_contract, check_ifind_budget, check_quant_raw_assembly,
               check_lint_optional, check_connector_matrix, check_health_backlog,
               check_pitfall_closure_gate, check_pitfall_coverage):
        name, level, msgs = fn()
        results.append({"check": name, "level": level, "msgs": msgs})
    for fn in (check_cache_honesty, check_report_cycle):
        name, level, msgs = fn(codes)
        results.append({"check": name, "level": level, "msgs": msgs})
    if True:  # FILLS 检查始终跑（无 codes 时扫全量）
        name, level, msgs = check_fills(codes)
        results.append({"check": name, "level": level, "msgs": msgs})
    return results


def check_health_backlog() -> tuple[str, str, list[str]]:
    """D-8/L-4（2026-10-25）：健康度与欠账闸——两个新模块的**可用性硬闸**。

    为什么要在 preflight 里也查一遍（selfcheck_all 已经查过）：
      ① `data_health` 是标注层，`_assess_data_health` 里是 fail-open（模块坏了返回
         deferred），不查的话**整批跑完才发现健康度全是「未评估」**——正是"跑到一半才炸"；
      ② `BACKLOG.md` 是纯文本契约，格式腐烂不会报错，只会让欠账再次"看起来登记了"。
    两者都秒级、零联网。
    MUST：模块导入失败 / assess() 语义不成立 / BACKLOG 表结构破损。
    """
    errs, warns = [], []
    # ① data_health 可导入且四态语义成立
    try:
        sys.path.insert(0, SKILL)
        import data_health as _dh  # noqa: E402
        h_ok = _dh.assess(series_points=1200, freshness_days=1, pb_pct=50.0,
                          pb_sources={"a": 1, "b": 1},
                          price_cross={"ok": True, "multi_source": True,
                                       "primary": "sina", "diff_pct": 0.0, "warn": None},
                          has_quant_raw=True, pb_pct_10y=40.0,
                          industry_metrics_present=True, event_leg_state="retrieved")
        h_ms = _dh.assess(series_points=0, freshness_days=1, pb_pct=None,
                          pb_sources={}, price_cross=None)
        if h_ok.get("status") != "OK":
            errs.append(f"data_health 全绿场景未得 OK（得 {h_ok.get('status')}）→ 四态判定失效")
        if h_ms.get("status") != "MISSING":
            errs.append(f"data_health 无序列场景未得 MISSING（得 {h_ms.get('status')}）")
        if set(_dh.STATES) != {"OK", "DEGRADED", "MISSING", "INVALID"}:
            errs.append(f"data_health 状态集被改动：{_dh.STATES}")
        # D-7↔D-8 接线断言：现价双源分歧（>=0.5% 时 D-7 必置 warn）必须被 D-8 识别为降级，
        # 否则「告警了但健康度还是 OK」= 水印丢失（S-14 的回归守卫）。
        h_dv = _dh.assess(series_points=1200, freshness_days=1, pb_pct=50.0,
                          pb_sources={"a": 1, "b": 1},
                          price_cross={"ok": True, "multi_source": True, "primary": "sina",
                                       "diff_pct": 1.2, "warn": "现价双源分歧 1.20%"},
                          has_quant_raw=True, pb_pct_10y=40.0,
                          industry_metrics_present=True, event_leg_state="retrieved")
        if not any(it["code"] == "DG-PRICE-DIVERGE" for it in h_dv.get("items", [])):
            errs.append("D-7→D-8 断链：现价双源分歧未产出 DG-PRICE-DIVERGE → 分歧告警不会进水印")
    except Exception as e:
        errs.append(f"data_health 不可用({type(e).__name__}: {e}) → 健康度水印将静默降级为「未评估」")
    # ② BACKLOG 表结构
    try:
        import check_backlog as _cb  # noqa: E402
        if not os.path.exists(_cb.PATH):
            errs.append("BACKLOG.md 不存在 → 欠账重新变回「文档里躺着的待定」")
        else:
            rows, perr = _cb.parse(open(_cb.PATH, encoding="utf-8").read())
            perr = _cb.validate(rows, perr)
            if perr:
                errs += [f"BACKLOG 校验失败：{x}" for x in perr[:5]]
            else:
                _open = sum(1 for r in rows if r["状态"] in _cb.OPEN_STATUSES)
                warns.append(f"欠账 {len(rows)} 条（open {_open}）→ 见 BACKLOG.md")
    except Exception as e:
        errs.append(f"check_backlog 不可用({type(e).__name__}: {e}) → 欠账表无法校验")
    # ③ B-03 交易日历可用性（MUST）
    #    为什么必须前置：日历缺失/损坏时，ingest/fetch 侧要么过滤不了（污染分位），
    #    要么抛 CalendarUnavailable 中断落盘 —— 两者都该在**开跑前**看到，而不是跑到
    #    第 N 只才发现。这里只断言「cn 日历可用 + 关键判例正确」，零联网、~0.2s。
    try:
        import trade_calendar as _tc  # noqa: E402
        st = _tc.calendar_status()
        cn = st.get("cn") or {}
        if not cn.get("available"):
            errs.append(f"cn 交易日历不可用（{cn.get('n_series')} 个序列）→ "
                        f"估值序列无法做交易日过滤（B-03 闸失效）")
        elif (cn.get("n") or 0) < 1000:
            errs.append(f"cn 日历仅 {cn.get('n')} 个交易日（<1000）→ 覆盖不足，"
                        f"分位窗口可能残缺")
        # 关键判例：周末/节假日必须判非交易日（污染源曾把它们写成有值）
        for d, why in (("2026-10-03", "周六"), ("2026-10-01", "国庆")):
            if _tc.is_trading_day(d, "cn") is not False:
                errs.append(f"交易日历判例失效：{d}（{why}）未被判为非交易日")
        # 存量缓存不得含非交易日点（A 股）；hk/us 样本不足时不做要求
        bad = _tc.audit_caches()
        if bad:
            tot = sum(v["n_dropped"] for v in bad.values())
            errs.append(f"存量估值缓存含 {tot} 个非交易日点（{len(bad)} 个文件，"
                        f"如 {list(bad)[:3]}）→ 跑 repair_nontrading.py 修复；"
                        f"未修复会污染 pct_now/五档")
        warns.append(f"交易日历：cn {cn.get('n')} 日（{cn.get('start')}~{cn.get('end')}）"
                     f"· hk/us 样本不足→不判休市（见 BACKLOG B-11）")
    except Exception as e:
        errs.append(f"trade_calendar 不可用({type(e).__name__}: {e}) → B-03 交易日闸失效")
    # ④ B-02 单一派生入口（MUST）
    #    回测必须调用 quant_inputs.derive_quant_inputs 而不是自带一套派生；
    #    否则 live/backtest 又回到「两条派生实现」，回测闸门对量化路径失明。
    try:
        import inspect  # noqa: E402
        import backtest as _bt  # noqa: E402
        src = inspect.getsource(_bt.real_features)
        if "quant_inputs" not in src and "derive_quant_inputs" not in src:
            errs.append("回测 real_features 未走 quant_inputs 单入口 → live/backtest "
                        "量化派生分叉（B-02）")
        else:
            import quant_inputs as _qi  # noqa: E402
            probe = _qi.derive_quant_inputs({"price_series": [1.0, 2.0, 3.0]})
            if "q_price_mom6" not in probe:
                errs.append("quant_inputs.derive_quant_inputs 输出字段缺失 → 派生契约破坏")
    except Exception as e:
        errs.append(f"backtest/quant_inputs 不可用({type(e).__name__}: {e}) → B-02 一致性无法校验")
    return ("健康度与欠账闸", "MUST" if errs else ("WARN" if warns else "OK"), errs + warns)


def check_pitfall_coverage() -> tuple[str, str, list[str]]:
    """闭环校验（粗粒度）：PITFALLS_SDS.md 里每条坑都必须映射到**真实存在**的守卫。

    没有这条，preflight 会退化成一张静态清单：新坑照漏，"登记"变成形式主义。
    支持两类守卫（2026-10-25 扩）：
      - `@preflight: <检查项>`  → 必须在 REAL_CHECKS 里
      - `@test: <文件>::<锚点>` → 文件必须存在且文件内确实含该锚点
        （补的动因：S-13/S-14/S-17/S-19 的真实守卫是注入测试而非 preflight；
          只认 @preflight 会逼着把它们挂到一个并不查它们的检查项上 = 假闭环）
    - 两类都没有                      → WARN（未闭环，下个 LLM 还会撞）
    - 标签指向不存在的检查项/测试      → MUST（登记了假兵器）
    """
    path = os.path.join(SKILL, "PITFALLS_SDS.md")
    if not os.path.exists(path):
        return ("坑登记覆盖度", "WARN", ["PITFALLS_SDS.md 不存在 → 新坑无法沉淀"])
    text = open(path, encoding="utf-8").read()
    blocks = [b for b in re.split(r"^### ", text, flags=re.M)[1:] if b.strip()]
    miss_tag, broken_tag, covered = [], [], set()
    for b in blocks:
        title = b.split("\n")[0].strip()[:40]
        # 只认行首标签：正文里叙述性的 `@preflight:` / `@test:` 提及不算登记
        # （S-20 的正文就在讲这件事，写成贪婪匹配会把叙述当标签 → 假阳性）
        m = re.search(r"^- @preflight:\s*(.+)", b, flags=re.M)
        t = re.search(r"^- @test:\s*(\S+)", b, flags=re.M)
        if not m and not t:
            miss_tag.append(title)
            continue
        if m:
            tag = re.sub(r"[（(].*?[)）]", "", m.group(1)).strip()
            if tag not in REAL_CHECKS:
                broken_tag.append(f"{title} → 标签 {tag!r} 不存在")
            else:
                covered.add(tag)
        if t:
            spec = re.sub(r"[（(].*?[)）]", "", t.group(1)).strip()
            fname, _, anchor = spec.partition("::")
            fp = os.path.join(SKILL, fname)
            if not os.path.exists(fp):
                broken_tag.append(f"{title} → 测试文件 {fname!r} 不存在")
            elif anchor and anchor not in open(fp, encoding="utf-8",
                                               errors="replace").read():
                broken_tag.append(f"{title} → {fname} 里找不到锚点 {anchor!r}")
    msgs = []
    if broken_tag:
        msgs += [f"[MUST] {x}" for x in broken_tag]
    if miss_tag:
        msgs += [f"[WARN] 未登记守卫标签（未闭环）：{x}" for x in miss_tag]
    uncovered = sorted(REAL_CHECKS - covered)
    if uncovered:
        msgs.append(f"[INFO] 尚无坑样例映射到：{uncovered}（覆盖不全也可能意味着检查没被实战验证）")
    return ("坑登记覆盖度", "MUST" if broken_tag else ("WARN" if msgs else "OK"), msgs)


def check_pitfall_closure_gate() -> tuple[str, str, list[str]]:
    """闭环校验（细粒度）：跑 check_pitfall_closure.py 的 R1~R8 双向校验。

    与 check_pitfall_coverage 的分工：
      coverage = 标签层（md 里指向的检查项/测试是否存在）
      closure  = 语义层（registry 里这条坑的守卫**是否真的查这个坑**：
                 R4 要求 proof 出现在该 check 源码里，R6 要求数据闸门回答存量，
                 R5 禁止一个检查项当垃圾桶承载 >3 条复发型坑）
    只靠 coverage 会漏掉最常见的假闭环：标签指向一个真实存在、但根本不检查这个
    坑的检查项 —— 那种情况 coverage 给 OK，closure 给 MUST。
    """
    try:
        import check_pitfall_closure as cpc
    except Exception as e:                                   # noqa: BLE001
        return ("坑登记闭环", "MUST",
                [f"check_pitfall_closure 不可用({type(e).__name__}: {e}) → 闭环无法验证"])
    import contextlib
    import io
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = cpc.run()
    except SystemExit as e:
        rc = e.code or 0
    except Exception as e:                                   # noqa: BLE001
        return ("坑登记闭环", "MUST", [f"闭环校验异常({type(e).__name__}: {e})"])
    out = buf.getvalue()
    if rc == 0:
        return ("坑登记闭环", "OK", [])
    lines = [ln.strip() for ln in out.splitlines() if ln.strip().startswith("- ")]
    return ("坑登记闭环", "MUST", lines or [out.strip()[-500:]])


def check_lint_optional() -> tuple[str, str, list[str]]:
    """静态 lint（可选）：装了 pyflakes 才生效，用于拦 S-07 类"变量未定义/作用域"坑
    ——那类坑当年崩在**写盘之后**，直接污染缓存，代价最高。"""
    have = False
    for mod in ("pyflakes", "pylint"):
        r = subprocess.run([sys.executable, "-c", f"import {mod}"],
                           capture_output=True, timeout=60)
        if r.returncode == 0:
            have = mod
            break
    if not have:
        return ("静态 lint", "WARN",
                ["未安装 pyflakes/pylint → S-07 类坑（未定义名/作用域崩在写盘后）无法自动拦截；"
                 f"建议 `python -m pip install pyflakes`（当前 {len(REAL_CHECKS)} 项检查中此项降级）"])
    try:
        r = subprocess.run([sys.executable, "-m", "pyflakes", SKILL], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=180)
        out = (r.stdout or "") + (r.stderr or "")
        # 2026-10-25 精确分级（E-2 落地）：S-07 的真正灾害类 = 未定义名/作用域
        # （F821 undefined name / F823 引用先于赋值）——当年崩在写盘后污染缓存；
        # F401 未用导入 / F841 未用变量 / f-string 缺占位符等属 benign 风格问题，
        # 一律降 WARN（否则 20 条历史遗留 benign 会把管线误拦死，制造新往返）。
        fatal, benign = [], []
        for l in out.splitlines():
            if not l.strip():
                continue
            (fatal if ("F821" in l or "F823" in l) else benign).append(l)
        msgs = []
        if fatal:
            msgs += [f"[MUST] {l}" for l in fatal[:10]]
        if benign:
            msgs.append(f"[WARN] benign 风格问题 {len(benign)} 条（未用导入/变量等，不阻断）："
                        + "；".join(benign[:3]) + ("…" if len(benign) > 3 else ""))
        if fatal:
            return ("静态 lint", "MUST", msgs)
        return ("静态 lint", "WARN" if benign else "OK", msgs or ["pyflakes 零发现"])
    except Exception as e:
        return ("静态 lint", "WARN", [f"lint 执行失败: {e}"])


def enforce(extra_msgs: list | None = None, codes: list | None = None) -> None:
    """硬闸版：给各入口（analyze_etf / run_sector_pipeline）共用，避免两处实现漂移。

    - MUST → **exit 3 阻断**（fail-closed），无逃生门。
    - 体检**自身异常** → 仅警告放行（fail-open）：工具坏了不能反过来成为新的阻断源。
    - `_XYD_PREFLIGHT_DONE=1` 防重入（子进程继承父进程环境变量，不重复体检）。
    """
    import sys as _sys
    if os.environ.get("_XYD_PREFLIGHT_DONE") == "1":
        return
    os.environ["_XYD_PREFLIGHT_DONE"] = "1"
    try:
        res = run(codes or [])
    except Exception as e:  # pragma: no cover
        print(f"[PREFLIGHT] ⚠️ 体检自身异常（{type(e).__name__}: {e}）→ 跳过，不阻断",
              file=_sys.stderr)
        return

    musts = [r for r in res if r["level"] == "MUST"]
    if extra_msgs:
        musts.append({"check": "入口附加检查", "level": "MUST", "msgs": list(extra_msgs)})
    if not musts:
        n = len([r for r in res if r["level"] == "WARN"])
        print(f"[PREFLIGHT] ✅ 体检通过（{n} 项 WARN，结论须带相应水印）"
              if n else "[PREFLIGHT] ✅ 体检通过", file=_sys.stderr)
        return

    bar = "=" * 78
    print("\n" + bar, file=_sys.stderr)
    print("  ❌ PREFLIGHT 阻断：下列问题**在运行前就能修好**，别带着它们硬跑", file=_sys.stderr)
    print("     （每一条留到运行时才炸 = 一轮完整往返，成本是分钟级）", file=_sys.stderr)
    print(bar, file=_sys.stderr)
    for r in musts:
        print(f"  [FAIL] {r['check']}", file=_sys.stderr)
        for m in r["msgs"][:10]:
            print(f"          - {m}", file=_sys.stderr)
        if len(r["msgs"]) > 10:
            print(f"          ... 另有 {len(r['msgs']) - 10} 条", file=_sys.stderr)
    print("-" * 78, file=_sys.stderr)
    print("  → 修完再跑；或先走 `python preflight_etf.py`（仅报告不阻断）", file=_sys.stderr)
    print(bar + "\n", file=_sys.stderr)
    _sys.exit(3)


def main() -> int:
    ap = argparse.ArgumentParser(description="XYD-供需驱动板块 运行前体检")
    ap.add_argument("--codes", default="", help="ETF 代码，逗号分隔（用于 fills 冲突检查）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args()

    codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    res = run(codes)

    musts = [r for r in res if r["level"] == "MUST"]
    warns = [r for r in res if r["level"] == "WARN"]

    if args.json:
        print(json.dumps({"ok": not musts, "results": res}, ensure_ascii=False, indent=2))
    else:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
        print("=" * 78)
        print("  XYD-供需驱动板块 · 运行前体检 preflight_etf.py")
        print("=" * 78)
        icon = {"OK": "[PASS]", "WARN": "[WARN]", "MUST": "[FAIL]"}
        for r in res:
            print(f"  {icon[r['level']]} {r['check']}")
            for m in r["msgs"][:12]:
                print(f"          - {m}")
            if len(r["msgs"]) > 12:
                print(f"          ... 另有 {len(r['msgs']) - 12} 条")
        print("-" * 78)
        if musts:
            print(f"  ❌ MUST 阻断 {len(musts)} 项 → exit=3：**禁止直跑管线**，先修完再跑")
            print("     （每一条在运行时都会变成一次完整往返，成本 ≈ 分钟级）")
        elif warns:
            print(f"  ⚠️  仅 WARN {len(warns)} 项 → 可跑，但结论须带相应水印")
        else:
            print("  ✅ 全通过 → 可直跑管线")
        print("=" * 78)

    return 3 if musts else 0


if __name__ == "__main__":
    sys.exit(main())
