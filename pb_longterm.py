#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
D-4（2026-10-25）· 长周期 PB 交叉校验 + BVPS 口径突变检测。

背景（审计定义）：月频 PB 从更长窗口（理想 10y，数据可得则 10y，否则如实标注窗口年数）
交叉校验 5y 日频主分位；并检测 BVPS 口径突变——当 PB 单月跳变幅度远超同期价格变动时，
说明是 BVPS 会计口径变更（而非真实估值变化），此时 PB 分位将失真，须告警。

设计纪律：
- 纯计算、零外部依赖（managed python 可跑）。
- 不改动 discipline.py 引擎任何逻辑——只产出"更长的窗口分位 + 口径突变标记"，
  由上游（run_sector_pipeline）写进草稿 bands，引擎在 O9 触发时自行消费 pb_pct_10y。
- 诚实性：窗口年数如实标注；数据只有 5y（妙想 NL 截断）时，pb_pct_period 标 "5y_long"
  而非谎称 10y；10y 交叉校验在 ≥10y 数据就绪后自动激活（机制就位）。
- 口径突变 = fail-closed 告警（SHOULD 级，不阻断分析）：标出月份 + 幅度，供人工核验。

用法：
    python pb_longterm.py --index 000015.SH
    （模块也可被 run_sector_pipeline 直接 import: compute_longterm_pb(index_code)）
"""
import json
import os
import sys
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
MONTHLY_DIR = os.path.join(HERE, "data", "index_monthly")
PB_DAILY_DIR = os.path.join(HERE, "data", "index_daily_pb")

# 口径突变判据：PB 单月跳变 > CALIBER_PB_JUMP% 且 同期价格变动 < CALIBER_PRICE_JUMP%
# → 价格没动但 PB 大跳，极可能是 BVPS 分母口径变更（会计重述/并购/指数重构）。
CALIBER_PB_JUMP = 0.15       # 15%
CALIBER_PRICE_JUMP = 0.05    # 5%
MIN_MONTHS = 12              # 不足 12 个月不计算长周期分位


def _load_monthly_pb(index_code: str):
    path = os.path.join(MONTHLY_DIR, f"{index_code}.json")
    if not os.path.exists(path):
        return None, None
    d = json.load(open(path, encoding="utf-8"))
    return d.get("pb") or {}, d.get("close") or {}


def compute_longterm_pb(index_code: str) -> dict:
    """计算长周期 PB 分位 + BVPS 口径突变检测。

    返回 dict（always 含 'available' 布尔）：
      available, pb_pct_10y(=长窗口分位), window_years, n_months, as_of,
      caliber_mutation(bool), caliber_events(list of {month, pb_jump_pct, price_jump_pct}),
      note(str)
    """
    pb, close = _load_monthly_pb(index_code)
    out = {
        "available": False, "pb_pct_10y": None, "window_years": None,
        "n_months": 0, "as_of": None, "caliber_mutation": False,
        "caliber_events": [], "note": "",
    }
    if not pb or len(pb) < MIN_MONTHS:
        out["note"] = f"月频 PB 序列仅 {len(pb) if pb else 0} 个月（<{MIN_MONTHS}）→ 不计算长周期分位"
        return out

    months = sorted(pb.keys())
    vals = [pb[m] for m in months]
    cur = vals[-1]
    n = len(vals)
    # 分位 = 当前值在历史中的百分位（同 fetch_pb_mxdata.pct_rank 口径：≤当前值的占比×100）
    le = sum(1 for v in vals if v <= cur)
    pct = round(le / n * 100, 2)

    # 窗口年数（末月 − 首月）
    y0, m0 = months[0].split("-")
    y1, m1 = months[-1].split("-")
    yrs = round((int(y1) - int(y0)) * 12 + (int(m1) - int(m0)) + 1) / 12.0

    # ---- BVPS 口径突变检测 ----
    caliber_events = []
    if close and len(close) >= 2:
        cmonths = sorted(close.keys())
        cvals = [close[m] for m in cmonths]
        # 对齐到 pb 的月份序列（close 可能与 pb 月份不完全一致，取交集）
        for i in range(1, len(months)):
            m = months[i]
            pm = months[i - 1]
            if m not in close or pm not in close:
                continue
            pb_jump = abs(pb[m] - pb[pm]) / pb[pm] if pb[pm] else 0.0
            price_jump = abs(close[m] - close[pm]) / close[pm] if close[pm] else 0.0
            if pb_jump >= CALIBER_PB_JUMP and price_jump < CALIBER_PRICE_JUMP:
                caliber_events.append({
                    "month": m, "pb_jump_pct": round(pb_jump * 100, 1),
                    "price_jump_pct": round(price_jump * 100, 1),
                })

    out.update({
        "available": True,
        "pb_pct_10y": pct,
        "window_years": round(yrs, 1),
        "n_months": n,
        "as_of": months[-1],
        "caliber_mutation": len(caliber_events) > 0,
        "caliber_events": caliber_events,
    })
    if yrs < 10:
        out["note"] = (f"长窗口仅 {round(yrs,1)} 年（妙想 NL 截断，未达 10y）；"
                       f"pb_pct_10y 实为 {round(yrs,1)}y 口径，10y 交叉校验待 ≥10y 数据就绪后自动激活")
    return out


def _render(out: dict) -> str:
    if not out["available"]:
        return f"[D-4] 不可用：{out['note']}"
    lines = [
        f"[D-4] 长周期 PB 分位 = {out['pb_pct_10y']}% （窗口 {out['window_years']}y · "
        f"{out['n_months']} 个月 · as_of {out['as_of']}）",
    ]
    if out["caliber_mutation"]:
        lines.append(f"  ⚠️ BVPS 口径突变 {len(out['caliber_events'])} 处（价格未动而 PB 大跳→疑似会计口径变更）：")
        for e in out["caliber_events"][:6]:
            lines.append(f"    · {e['month']}: PB跳{e['pb_jump_pct']}% / 价格仅{e['price_jump_pct']}%")
        lines.append("    → PB 分位可能失真，须人工核验分母 BVPS 口径（O9 建议切 10y 交叉验证）")
    else:
        lines.append("  ✓ 无 BVPS 口径突变（PB 跳变均有对应价格变动支撑）")
    if out["note"]:
        lines.append(f"  · {out['note']}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="D-4 长周期 PB 交叉校验 + BVPS 口径突变检测")
    ap.add_argument("--index", required=True, help="指数代码（如 000015.SH）")
    args = ap.parse_args()
    out = compute_longterm_pb(args.index)
    print(_render(out))
    # 不可用或口径突变 → 退出码非零（供脚本链感知），但不阻断（SHOULD 级）
    sys.exit(0 if (out["available"] and not out["caliber_mutation"]) else 1)


if __name__ == "__main__":
    main()
