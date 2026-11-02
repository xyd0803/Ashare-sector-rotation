#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cap_test.py —— 轮动 ETF 框架的收益天花板测算（真实月度净值，2019-01 ~ 2026-08）

对照锚点：
  A. 等权 Buy&Hold 基准（每期对已上市 ETF 等权）
  B. 最强单 ETF Buy&Hold（54 只中累计涨幅最大）
  C. 上帝视角 Monthly Best（每月末已知次月涨幅，选最强满仓）→ 绝对理论上限【未来函数】
  D. 上帝视角 Top3/Top5 等权（次月涨幅前 N 等权）
  E. 上帝视角「只做涨的」（次月最强且必须为正，否则空仓）→ 择时+选股上限
  F. 纪律框架实测（backtest_report.json：NAV 1.8471）

注意：C/D/E 含未来函数，仅为"如果信息完全对称"的理论上限，不可直接作为可实现收益。
"""
import json, os, math

HERE = os.path.dirname(os.path.abspath(__file__))
ETF_DIR = os.path.join(HERE, "data", "etf")

# ---- 载入月度净值 ----
series = {}   # code -> {ym: close}
names = {}
for fn in os.listdir(ETF_DIR):
    if not fn.endswith(".json"):
        continue
    d = json.load(open(os.path.join(ETF_DIR, fn), encoding="utf-8"))
    code = d["code"]
    series[code] = {}
    for m in d.get("monthly", []):
        series[code][m["date"][:7]] = m["close"]
    # ETF 名称（从 backtest_report 或 map）
    names[code] = fn.replace(".json", "")

# ---- 月网格（并集，升序）----
months = sorted({ym for s in series.values() for ym in s})
months = [m for m in months if "2019-01" <= m <= "2026-08"]
n_m = len(months)
years = n_m / 12.0

def cagr(nav):
    return (nav ** (1.0 / years) - 1) * 100 if nav > 0 else -100

# ---- 各月各 ETF 收益率 ----
ret = {}  # code -> {ym: 月收益(相对上月末)}
for code, s in series.items():
    ym_list = sorted(s)
    r = {}
    for i in range(1, len(ym_list)):
        p0, p1 = ym_list[i-1], ym_list[i]
        if s[p1] and s[p0]:
            r[p1] = s[p1] / s[p0] - 1
    ret[code] = r

# 每个月末点可投的 ETF（该月有收益 = 有 close 且上月有 close）
def investable(ym):
    return [c for c in ret if ym in ret[c]]

# ============ A. 等权 Buy&Hold ============
nav_a = 1.0
first = True
for ym in months:
    inv = investable(ym)
    if not inv:
        continue
    avg = sum(ret[c][ym] for c in inv) / len(inv)
    nav_a *= (1 + avg)

# ============ B. 最强单票 Buy&Hold ============
best_single, best_nav = None, 0
for code, s in series.items():
    ys = sorted(s)
    if len(ys) < 6:
        continue
    nv = s[ys[-1]] / s[ys[0]]
    if nv > best_nav:
        best_nav, best_single = nv, code

# ============ C. 上帝 Monthly Best ============
nav_c = 1.0
for ym in months:
    inv = investable(ym)
    if not inv:
        continue
    best_r = max(ret[c][ym] for c in inv)
    nav_c *= (1 + best_r)

# ============ D. Top3 / Top5 等权 ============
def god_topk(k):
    nav = 1.0
    for ym in months:
        inv = investable(ym)
        if not inv:
            continue
        top = sorted(inv, key=lambda c: ret[c][ym], reverse=True)[:k]
        avg = sum(ret[c][ym] for c in top) / len(top)
        nav *= (1 + avg)
    return nav

nav_d3, nav_d5 = god_topk(3), god_topk(5)

# ============ E. 只做涨的（次月最强必须>0，否则空仓）============
nav_e = 1.0
pos_months = 0
for ym in months:
    inv = investable(ym)
    if not inv:
        continue
    best_r = max(ret[c][ym] for c in inv)
    if best_r > 0:
        nav_e *= (1 + best_r)
        pos_months += 1

# ============ F. 纪律框架实测 ============
rep = json.load(open(os.path.join(HERE, "data", "backtest_report.json"), encoding="utf-8"))
nav_f = rep["final_nav"]

# ============ G. 可实现动量轮动（无未来函数）============
# 每月末 t-1：用截至 t-1 的过去3个月累计涨幅排序，选前5名等权持有 t 月；成本 5bps/次
def momentum_topk(k, lookback=3, cost=5e-4):
    nav = 1.0
    held = []          # 当前持仓 code 列表
    for ym in months:
        inv = investable(ym)
        if not inv:
            continue
        # 计算 t-1 时的动量（past lookback 个月累计收益）
        ym_idx = months.index(ym)
        if ym_idx < lookback:
            continue
        mom = {}
        for c in inv:
            cl = sorted(series[c].keys())
            # 找 ym 之前 lookback 个月（用 ym 及之前共 lookback+1 个点）
            pts = [m for m in cl if m < ym][-lookback-1:]
            if len(pts) >= lookback + 1:
                mom[c] = series[c][pts[-1]] / series[c][pts[0]] - 1
        if len(mom) < k:
            continue
        top = sorted(mom, key=lambda c: mom[c], reverse=True)[:k]
        # t 月收益（等权，扣成本：换仓时扣）
        turnover = len(set(top) ^ set(held)) / max(len(top), 1)
        avg = sum(ret[c][ym] for c in top) / len(top)
        nav *= (1 + avg) * (1 - cost * turnover)
        held = top
    return nav

nav_g3 = momentum_topk(3)
nav_g5 = momentum_topk(5)

# ---- 输出 ----
print(f"月网格 {n_m} 个月（{months[0]} ~ {months[-1]}），约 {years:.2f} 年")
print(f"十倍目标所需年化：{10**(1/years)*100-100:.1f}%")
print("-" * 62)
rows = [
    ("A 等权Buy&Hold(全部54只)", nav_a),
    ("B 最强单ETF Buy&Hold", best_nav),
    ("G 现实动量轮动 Top5(无未来函数)", nav_g5),
    ("G 现实动量轮动 Top3(无未来函数)", nav_g3),
    ("F 纪律框架实测(50/53覆盖)", nav_f),
    ("E 上帝:只做涨的(选最强,跌则空仓)", nav_e),
    ("D 上帝:Top5等权轮动", nav_d5),
    ("D 上帝:Top3等权轮动", nav_d3),
    ("C 上帝:Monthly Best满仓(绝对上限)", nav_c),
]
for name, nav in rows:
    tag = ""
    if nav >= 10:
        tag = "  <== 达十倍"
    elif nav >= 5:
        tag = "  <== 达五倍"
    print(f"{name:<36} NAV={nav:>10.2f}  累计={nav*100-100:>10.1f}%  CAGR={cagr(nav):>6.1f}%{tag}")
print("-" * 62)
print("C/D/E 为上帝视角（未来函数），理论上限，非可实现收益；B 为单票幸存者上限。")
print(f"最强单票 {best_single} 首末月涨幅 {best_nav:.2f}x")
