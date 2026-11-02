#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
data_refresh.py — 日频 PB 数据统一刷新器（G2 · 2026-09-04）
============================================================
把「妙想直拉 → iFinD 真实值替换 → 点位兜底重建」三段人肉配方固化为统一入口。
S2 新鲜度门（>15天 exit=2）从"只能检测过期"升级为"有自动续期兜底"。

三种模式：
  --status    55 只新鲜度报告（fresh ≤7天 / warn ≤15天 / stale >15天）
  --rebuild   离线一致性复算：对每只缓存文件用与 parse_mx_pb_daily 逐字同口径
              的算法重算五档+pct_now，核对【文件内自洽】与【registry 同步】，
              配四道守卫（PB 合理区间 / 五档单调 / n≥100 / now=末点）。--write 才回写 registry
  --refresh   对 stale 指数走三级源链刷新：
              ① mx-ds-mcp（会话内工具，脚本不可调 → 报告提示会话内补）
              ② ifind_fix_pb.py（iFinD 真实采样点，子进程调用）
              ③ westock/东财点位重建——插值禁令不放开，仅提示手工路径

退出码：0 OK | 2 存在 stale | 3 rebuild 不一致 | 4 入参错误

用法：
  python data_refresh.py --status
  python data_refresh.py --rebuild            # 纯检查，不落盘
  python data_refresh.py --rebuild --write    # 同步 registry（回填 as_of 等 schema）
  python data_refresh.py --rebuild --fix-bands  # 用【文件自身序列同口径复算】修复五档污染
  python data_refresh.py --refresh --codes 980032.CNI,H30269.CSI
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

PB_DIR = os.path.join(HERE, "data", "index_daily_pb")
REG_PATH = os.path.join(HERE, "data", "meta", "pb_daily_bands.json")
MAP_PATH = os.path.join(HERE, "data", "meta", "etf_index_map.json")

# 标准序列文件名：<CODE>.<MKT>.json（过滤 *_pb/_pb_merged/_pe 等中间产物；MKT=SH/SZ/CSI/CNI/HI/GI/HK…）
_STD_FNAME = re.compile(r"^[0-9A-Z]+\.[A-Z]{2,4}\.json$")

FRESH_MAX, FRESH_WARN = 15, 7
PB_MIN, PB_MAX = 0.05, 40.0          # build_daily.py 同款合理性守卫
MIN_POINTS = 100

STALE_EXIT, MISMATCH_EXIT, ARG_EXIT = 2, 3, 4

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")   # type: ignore[attr-defined]
    except Exception:
        pass


# ---------- 与 parse_mx_pb_daily / discipline 逐字同口径 ----------
def q(p: float, vals: List[float]) -> Optional[float]:
    if not vals:
        return None
    s = sorted(vals)
    n = len(s)
    pos = p * (n - 1) / 100.0
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    if lo == hi:
        return s[lo]
    frac = pos - lo
    return s[lo] * (1 - frac) + s[hi] * frac


def pct_rank(v: float, vals: List[float]) -> Optional[float]:
    if not vals:
        return None
    return round(100.0 * sum(1 for x in vals if x <= v) / len(vals), 2)


def _days_since(dstr: str) -> Optional[int]:
    try:
        return (date.today() - datetime.strptime(dstr, "%Y-%m-%d").date()).days
    except ValueError:
        return None


def _load(path: str) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------- 序列扫描 ----------
def scan_series() -> Dict[str, Dict[str, Any]]:
    out = {}
    if not os.path.isdir(PB_DIR):
        return out
    for fn in sorted(os.listdir(PB_DIR)):
        if not fn.endswith(".json") or not _STD_FNAME.match(fn):
            continue  # *_pb/_pe 等中间产物不在扫描范围
        code = fn[:-5]
        try:
            d = _load(os.path.join(PB_DIR, fn))
        except json.JSONDecodeError as e:
            out[code] = {"_corrupt": str(e)}
            continue
        pts = d.get("pb") or []
        vals = [p[1] for p in pts if isinstance(p, (list, tuple)) and len(p) >= 2
                and isinstance(p[1], (int, float))]
        dates = [p[0] for p in pts if isinstance(p, (list, tuple)) and len(p) >= 2]
        out[code] = {"file": d, "pts": pts, "vals": vals, "dates": dates,
                     "name": d.get("index_name", code), "source": d.get("source", "")}
    return out


# ---------- 守卫 + 复算 ----------
def recompute(rec: Dict[str, Any]) -> Dict[str, Any]:
    """对单只缓存做四道守卫 + 五档/pct 复算。返回 {ok, guards, derived, diffs}。"""
    guards: List[str] = []
    vals: List[float] = rec.get("vals") or []
    dates: List[str] = rec.get("dates") or []
    d = rec.get("file") or {}

    n = len(vals)
    if n < MIN_POINTS:
        guards.append(f"[信息性·窗口不足] n={n} < {MIN_POINTS}（历史兜底口径，O9 引擎侧处理）")
        short_n = True
    else:
        short_n = False
    bad_pb = sum(1 for v in vals if not (PB_MIN <= v <= PB_MAX))
    if bad_pb:
        guards.append(f"PB 越界[{PB_MIN},{PB_MAX}] {bad_pb} 点（污染守卫）")
    bands_re = {"p5": round(q(5, vals), 4), "p20": round(q(20, vals), 4),
                "p50": round(q(50, vals), 4), "p80": round(q(80, vals), 4),
                "p95": round(q(95, vals), 4)} if n >= 2 else {}
    bk = ["p5", "p20", "p50", "p80", "p95"]
    if any(bands_re.get(b) is None for b in bk):
        guards.append("五档复算失败（序列空）")
    elif not all(bands_re[bk[i]] <= bands_re[bk[i + 1]] + 1e-9 for i in range(4)):
        guards.append(f"五档非单调: {bands_re}")
    now_re = round(vals[-1], 4) if vals else None
    if now_re is not None and d.get("now") is not None and abs(now_re - float(d["now"])) > 1e-6:
        guards.append(f"now({d['now']}) ≠ 序列末点({now_re})")
    pct_re = pct_rank(vals[-1], vals) if vals else None

    diffs = []
    for b in bk:
        old, new = d.get(b), bands_re.get(b)
        if old is not None and new is not None and abs(float(old) - new) > 5e-4:
            diffs.append(f"{b}: 文件{old} vs 复算{new}")
    if d.get("pct_now") is not None and pct_re is not None and abs(float(d["pct_now"]) - pct_re) > 0.01:
        diffs.append(f"pct_now: 文件{d['pct_now']} vs 复算{pct_re}")

    return {"ok": not guards and not diffs, "guards": guards, "diffs": diffs,
            "bands_re": bands_re, "pct_re": pct_re, "now_re": now_re,
            "as_of_re": dates[-1] if dates else None, "n": n, "short_n": short_n}


# ---------- 模式实现 ----------
def mode_status() -> int:
    recs = scan_series()
    rows = []
    for code, rec in sorted(recs.items()):
        if "_corrupt" in rec:
            rows.append((999, code, "⛔ 文件损坏", rec["_corrupt"][:50]))
            continue
        as_of = rec["file"].get("as_of") or rec["dates"][-1] if rec["dates"] else None
        stale = _days_since(as_of) if as_of else None
        if stale is None:
            grade = "❓ 无日期"
        elif stale > FRESH_MAX:
            grade = f"🔴 stale {stale}天"
        elif stale > FRESH_WARN:
            grade = f"🟡 warn {stale}天"
        else:
            grade = f"🟢 fresh {stale}天"
        rows.append((stale if stale is not None else 999, code, grade,
                     f"n={rec['file'].get('n')} now={rec['file'].get('now')} pct={rec['file'].get('pct_now')}"))
    n_stale = sum(1 for r in rows if "stale" in r[2])
    print(f"日频 PB 缓存新鲜度（{len(rows)} 只，{PB_DIR}）")
    for _, code, grade, info in sorted(rows, reverse=True):
        print(f"  {code:14s} {grade:14s} {info}")
    print(f"\n汇总: fresh={sum(1 for r in rows if 'fresh' in r[2])} "
          f"warn={sum(1 for r in rows if 'warn' in r[2])} stale={n_stale}")
    if n_stale:
        print(f"→ {n_stale} 只过期，须刷新：python data_refresh.py --refresh（或会话内 mx-ds-mcp 补拉）")

    # —— PE 序列目录扫描（2026-09-07 P1-⑥：BLEND/PE 锚 ETF 的 PE 序列同口径监控）——
    PE_DIR = os.path.join(HERE, "data", "index_daily_pe")
    if os.path.isdir(PE_DIR):
        pe_rows = []
        for fn in sorted(os.listdir(PE_DIR)):
            if not fn.endswith(".json"):
                continue
            d = _load(os.path.join(PE_DIR, fn))
            if not isinstance(d, dict):
                pe_rows.append((999, fn, "⛔ 损坏", "非 dict"))
                continue
            pts = d.get("pb") or []
            dates = [p[0] for p in pts if isinstance(p, (list, tuple)) and len(p) >= 2]
            as_of = d.get("as_of") or (dates[-1] if dates else None)
            stale = _days_since(as_of) if as_of else None
            grade = ("❓ 无日期" if stale is None
                     else f"🔴 stale {stale}天" if stale > FRESH_MAX
                     else f"🟡 warn {stale}天" if stale > FRESH_WARN
                     else f"🟢 fresh {stale}天")
            pe_rows.append((stale if stale is not None else 999, fn.replace(".json", ""), grade,
                            f"n={d.get('n')} metric={d.get('metric', 'pe')} now={d.get('now')}"))
        print(f"\n日频 PE 缓存新鲜度（{len(pe_rows)} 只，{PE_DIR}）")
        for _, code, grade, info in sorted(pe_rows, reverse=True):
            print(f"  {code:14s} {grade:14s} {info}")
        print("→ PE 刷新：python fetch_pb_mxdata.py --metric pe --codes <code>")
    return STALE_EXIT if n_stale else 0


def mode_rebuild(write: bool, fix_bands: bool = False) -> int:
    recs = scan_series()
    reg = _load(REG_PATH) if os.path.exists(REG_PATH) else {}
    bad_total, info_total = 0, 0
    reg_changed = []
    fixed_files = []
    print(f"离线一致性复算（{len(recs)} 只）· 守卫: PB∈[{PB_MIN},{PB_MAX}] / 五档单调 / now=末点 / 五档自洽"
          f"（n<{MIN_POINTS} 为信息性）")
    for code, rec in sorted(recs.items()):
        if "_corrupt" in rec:
            print(f"  ⛔ {code}: JSON 损坏 — {rec['_corrupt'][:60]}")
            bad_total += 1
            continue
        r = recompute(rec)
        hard = [g for g in r["guards"] if not g.startswith("[信息性")]
        if r["short_n"] and not hard:
            info_total += 1
            print(f"  🟡 {code}: {[g for g in r['guards'] if g.startswith('[信息性')][0]}")
        for g in hard:
            print(f"  🔴 {code}: 守卫失败 — {g}")
        for df in r["diffs"]:
            print(f"  🟠 {code}: 五档漂移 — {df}")
        if hard or r["diffs"]:
            bad_total += 1
            if fix_bands and r["bands_re"] and r["now_re"] is not None:
                d = rec["file"]
                old_bands = {b: d.get(b) for b in ("p5", "p20", "p50", "p80", "p95")}
                d.update(r["bands_re"])
                d["now"] = r["now_re"]
                d["pct_now"] = r["pct_re"]
                d["as_of"] = r["as_of_re"]
                d["n"] = r["n"]
                if "start" not in d and rec["dates"]:
                    d["start"] = rec["dates"][0]
                path = os.path.join(PB_DIR, f"{code}.json")
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(d, f, ensure_ascii=False, indent=1)
                fixed_files.append(code)
                print(f"  🔧 {code}: 五档已用自身序列复算修复 "
                      f"({old_bands['p50']}→{r['bands_re']['p50']} 等)")
        # registry 同步核对（旧 schema 无 as_of → 以 end 比对，write 时回填 as_of）
        d = rec["file"]
        if code in reg:
            rg = reg[code]
            for k in ("n", "start", "end", "p5", "p20", "p50", "p80", "p95", "now", "pct_now"):
                if rg.get(k) != d.get(k):
                    print(f"  🟡 {code}: registry.{k}={rg.get(k)} ≠ 文件{d.get(k)}")
                    reg_changed.append(code)
                    break
        else:
            print(f"  ⚪ {code}: 未登记于 pb_daily_bands.json")
            reg_changed.append(code)
    # 派生值回填 registry（同口径复算值）
    for code in dict.fromkeys(reg_changed + fixed_files):
        rec = recs.get(code)
        if not rec or "_corrupt" in rec:
            continue
        r = recompute(rec)
        d = rec["file"]
        reg[code] = {"index_code": code, "index_name": d.get("index_name", code),
                     "source": d.get("source", ""), "n": r["n"],
                     "start": d.get("start"), "end": d.get("end"), "as_of": r["as_of_re"] or d.get("end"),
                     "p5": r["bands_re"].get("p5"), "p20": r["bands_re"].get("p20"),
                     "p50": r["bands_re"].get("p50"), "p80": r["bands_re"].get("p80"),
                     "p95": r["bands_re"].get("p95"), "now": r["now_re"],
                     "pct_now": r["pct_re"], "warn": ""}
    print(f"\n汇总: 硬失败/漂移 {bad_total} 只 | 窗口不足(信息性) {info_total} 只 | registry 待同步 {len(set(reg_changed))} 只"
          + (f" | 五档修复 {len(fixed_files)} 只 {fixed_files}" if fix_bands else ""))
    if write and (reg_changed or fixed_files):
        with open(REG_PATH, "w", encoding="utf-8") as f:
            json.dump(reg, f, ensure_ascii=False, indent=1)
        print(f"✅ registry 已回写 {REG_PATH}（共 {len(reg)} 只）")
    elif reg_changed and not write:
        print("（--write 未指定：registry 未改动。漂移若为真，请人工确认后加 --write）")
    return MISMATCH_EXIT if bad_total else 0


def mode_refresh(codes: List[str], dry_run: bool) -> int:
    recs = scan_series()
    targets = codes or [c for c, r in recs.items() if "_corrupt" not in r
                        and (lambda s: s is not None and s > FRESH_MAX)(_days_since(
                            (r["file"].get("as_of") or (r["dates"][-1] if r["dates"] else ""))))]
    if not targets:
        print("✅ 无 stale 指数，无需刷新")
        return 0
    print(f"刷新计划（{len(targets)} 只）— 三级源链：①mx-ds-mcp(会话内) ②iFinD ③点位重建(插值禁用)")
    plan = []
    for code in targets:
        plan.append((code, recs.get(code, {}).get("file", {}).get("as_of")))
    for code, as_of in plan:
        print(f"  → {code} (as_of={as_of})")
    if dry_run:
        print("\n[dry-run] 未执行。去掉 --dry-run 以实际调用 iFinD 源。")
        return 0
    results = []
    for code, _as_of in plan:
        base = code.split(".")[0]
        print(f"\n[{code}] 调用 ifind_fix_pb.py --codes {base} ...")
        try:
            p = subprocess.run(
                [sys.executable, os.path.join(HERE, "ifind_fix_pb.py"), "--codes", base],
                capture_output=True, text=True, timeout=120, encoding="utf-8", errors="replace")
            tail = (p.stdout or "").strip().splitlines()[-3:]
            for ln in tail:
                print(f"    {ln}")
            results.append((code, p.returncode == 0))
        except Exception as e:
            print(f"    ⛔ 调用失败: {type(e).__name__}: {e}")
            results.append((code, False))
    ok = [c for c, s in results if s]
    fail = [c for c, s in results if not s]
    print(f"\n刷新结果: iFinD 段成功 {len(ok)} / 失败 {len(fail)} {fail or ''}")
    print("提醒：① mx-ds-mcp 段须会话内补拉（脚本不可调 MCP）；② 刷新后跑 --rebuild 复核一致性。")
    return 0 if not fail else STALE_EXIT


def main() -> int:
    ap = argparse.ArgumentParser(description="日频 PB 数据统一刷新器（G2）")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--codes", help="逗号分隔指数代码（--refresh 指定目标，缺省=全部 stale）")
    ap.add_argument("--write", action="store_true", help="rebuild 模式回写 registry")
    ap.add_argument("--fix-bands", dest="fix_bands", action="store_true",
                    help="rebuild 模式：用文件自身序列复算修复五档污染（非插值，同口径）")
    ap.add_argument("--dry-run", dest="dry_run", action="store_true", help="refresh 只打印计划")
    a = ap.parse_args()
    if sum(bool(x) for x in (a.status, a.rebuild, a.refresh)) != 1:
        print("须且仅须指定 --status / --rebuild / --refresh 之一", file=sys.stderr)
        return ARG_EXIT
    if a.status:
        return mode_status()
    if a.rebuild:
        return mode_rebuild(a.write, a.fix_bands)
    codes = [c.strip() for c in a.codes.split(",")] if a.codes else []
    return mode_refresh(codes, a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
