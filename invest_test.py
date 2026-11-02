#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
invest_test.py —— 「低估分批加仓 + 高估分批卖出」PB 分位定仓变体

核心：PB 分位 → 目标仓位（单调映射），低估重仓、高估轻仓、中间线性过渡。
这等价于"逐渐加仓/减仓"的连续化：分位每降一档，仓位升一档。

变体：
  s1 单票定仓：只做一只 ETF（默认515220煤炭），仓位=f(pb_pct)
  s2 全池定仓：54 只全算权重，权重=f(pb_pct)，每期持有（总仓位=sum(w)，自然不满仓）
  s3 低估池定仓：只持 PB<50% 的，按权重（结合"低估布局"）
  s4 低估池+量价加分：s3 基础上 价涨或量增 的权重 ×1.2（结合供需/发展）
对比：满仓煤炭4.10x/26%、纪律框架1.85x/28%
"""
import json, os, sys
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import backtest as B

START, END, COST = "2019-01-01", "2026-08-13", 5.0
B._MAP = json.load(open(os.path.join(HERE, "data", "meta", "etf_index_map.json"), encoding="utf-8"))
B._IDX_MONTHLY = B.load_index_monthly()
B._RAW_DAILY = B.load_raw_daily()


def weight_of(pb_pct):
    """PB 分位 → 仓位权重（单调降）：越低估越重仓"""
    if pb_pct is None:
        return 0.0
    if pb_pct <= 10: return 1.00
    if pb_pct <= 20: return 0.85
    if pb_pct <= 30: return 0.70
    if pb_pct <= 40: return 0.55
    if pb_pct <= 50: return 0.42
    if pb_pct <= 60: return 0.30
    if pb_pct <= 70: return 0.20
    if pb_pct <= 80: return 0.12
    if pb_pct <= 90: return 0.06
    return 0.02


def run(mode, single_code="515220", pb_cap=50.0, momentum_boost=False):
    u = B.load_universe_from_map()
    etf_ret = B.load_etf_returns()
    ends = B.month_ends(START, END)
    nav, nav_series = 1.0, [1.0]
    prev_w, breaches = {}, []
    pos_months = 0
    per_etf = {}

    for sig_end in ends[:-1]:
        exec_day = B.first_of_next(sig_end)
        sig_ym = sig_end.strftime("%Y-%m")
        pit = B.guards.filter_pit_universe(u, exec_day.isoformat(), strict=True)
        weights = {}
        for etf in pit:
            code = etf["code"]
            if mode == "s6" and code not in ("515220", "512400", "512480", "512890"):
                continue
            if mode == "s1" and code != single_code:
                continue
            feat = B.real_features(etf, sig_end)
            if feat is None or feat.get("pb_pct") is None:
                continue
            w = weight_of(feat["pb_pct"])
            if mode == "s5":
                w = feat["pb_pct"]  # 暂存分位，稍后取最低3只
            if mode in ("s3", "s4") and feat["pb_pct"] > pb_cap:
                w = 0.0
            if mode == "s4" and w > 0:
                if feat["price"] == "up":
                    w *= 1.2
                elif feat["volume"] == "up":
                    w *= 1.1
            if w >= 0.05:
                weights[code] = w
        if mode == "s5" and weights:
            # 取分位最低的 3 只，权重用 weight_of 定仓
            top3 = sorted(weights.items(), key=lambda kv: kv[1])[:3]
            w3 = {c: weight_of(pb) for c, pb in top3}
            weights = w3
        if not weights:
            port_w = {}
        else:
            # 权重归一：保持相对大小但总仓位 <=1（不满仓，余现金）
            s = sum(weights.values())
            port_w = {c: (w / s) * min(s, 1.0) for c, w in weights.items()}
        if port_w:
            pos_months += 1
            turnover = sum(abs(port_w.get(k, 0) - prev_w.get(k, 0))
                           for k in set(port_w) | set(prev_w))
            cost = turnover * COST / 10000.0
            port_ret = sum(w * etf_ret.get(c, {}).get(exec_day.strftime("%Y-%m"), 0.0)
                           for c, w in port_w.items())
            nav = nav * (1.0 - cost) * (1.0 + port_ret)
        nav_series.append(nav)
        prev_w = port_w

    peak = nav_series[0]
    max_dd = 0.0
    for x in nav_series:
        peak = max(peak, x)
        max_dd = max(max_dd, (peak - x) / peak)
    return {"nav": nav_series[-1], "max_dd": max_dd, "pos_months": pos_months,
            "breaches": breaches, "nav_series": [round(x, 4) for x in nav_series]}


def fmt(name, nav, dd, pos, note=""):
    years = 91 / 12.0
    cagr = (nav ** (1.0 / years) - 1) * 100 if nav > 0 else -100
    avg_pos = pos / 91 * 100
    print(f"{name:<34} NAV={nav:>6.2f}  累计={nav*100-100:>7.1f}%  CAGR={cagr:>5.1f}%  "
          f"回撤={dd*100:>5.1f}%  平均仓位≈{avg_pos:>4.0f}%  {note}")


def main():
    print("=" * 118)
    print("「低估分批加仓+高估分批卖出」PB定仓变体  (2019-01 ~ 2026-08, 5bps)")
    print("=" * 118)
    fmt("满仓持有 煤炭515220(对照)", 4.10, 0.260, 91, "（用户所说单票基准）")
    fmt("满仓持有 红利低波512890(对照)", 2.37, 0.116, 91)
    base = B.backtest(B.load_universe_from_map(), START, END, COST, B.real_features, verbose=False)
    fmt("纪律框架 全信号(对照)", base["final_nav"], base["max_drawdown"], 91)

    for mode, note in (
        ("s1", "单煤炭 PB定仓(低估重仓高估轻仓)"),
        ("s2", "全池54只 PB定仓(自然不满仓)"),
        ("s3", "低估池PB<50% 定仓"),
        ("s4", "低估池PB<50% + 量价加分(结合发展)"),
    ):
        r = run(mode)
        fmt(f"{mode} {note}", r["nav"], r["max_dd"], r["pos_months"])
    r5 = run("s5")
    fmt("s5 低估3只轮动+PB定仓", r5["nav"], r5["max_dd"], r5["pos_months"],
        "（每期自动换最低估3只，各自定仓）")
    r6 = run("s6")
    fmt("s6 精选4板块PB定仓", r6["nav"], r6["max_dd"], r6["pos_months"],
        "（煤炭+有色+半导体+红利低波，各按PB定仓）")


if __name__ == "__main__":
    main()
