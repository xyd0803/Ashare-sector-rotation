#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pe_test.py —— 「低估买入 + 高估卖出」估值轮动变体（用户框架的量化版）

理念：不用动量，用 PB 分位（point-in-time）+ 量价供需状态做轮动。
变体：
  v1 纯低估：每期持有 PB 分位最低的 5 只（等权）→ 测"低估值因子"本身
  v2 低估池：每期持有 PB 分位 <30% 的全部（等权）→ 低估集合
  v3 低估+量价：PB<30% 且 价不跌、量不缩（用户框架：低估+量价供需确认）
  v4 低估+量价+高估卖出：在 v3 基础上，持仓中 PB>70% 或 价跌 则强制卖出（高估兑现/价跌一票否决）
  v5 高估卖出+买入低估：持有低 PB<30%，一旦持仓 PB>70% 即轮出换入当前低估（最贴近"买低卖高"）
对比：baseline 全信号 1.85 / 信号内动量Top3 2.83
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


def run_valuation(mode, k=5, pb_lo=30.0, pb_hi=70.0, keep_hi=False):
    u = B.load_universe_from_map()
    etf_ret = B.load_etf_returns()
    ends = B.month_ends(START, END)
    nav, nav_series = 1.0, [1.0]
    prev_w, breaches = {}, []
    signal_wins, per_etf = [], {}

    def _pick(mode, cands):
        """cands: [(code,name,pb_pct,price,volume,wr)] 返回 [(code,w)]"""
        if mode == "v1":
            top = sorted(cands, key=lambda x: x[2])[:k]
            return [(c, 1.0 / len(top)) for c, _, _, _, _, _ in top]
        if mode == "v2":
            low = [c for c in cands if c[2] is not None and c[2] <= pb_lo]
            return [(c, 1.0 / len(low)) for c, _, _, _, _, _ in low] if low else []
        if mode in ("v3", "v4"):
            low = [c for c in cands if c[2] is not None and c[2] <= pb_lo
                   and c[3] != "down" and c[4] != "down"]
            return [(c, 1.0 / len(low)) for c, _, _, _, _, _ in low] if low else []
        if mode == "v5":
            low = [c for c in cands if c[2] is not None and c[2] <= pb_lo]
            return [(c, 1.0 / len(low)) for c, _, _, _, _, _ in low] if low else []
        return []

    for sig_end in ends[:-1]:
        exec_day = B.first_of_next(sig_end)
        pit = B.guards.filter_pit_universe(u, exec_day.isoformat(), strict=True)
        cands = []
        for etf in pit:
            feat = B.real_features(etf, sig_end)
            if feat is None or feat["pb_pct"] is None:
                continue
            cands.append((etf["code"], etf["name"], feat["pb_pct"],
                          feat["price"], feat["volume"], feat.get("pb_now")))
        port_w = dict(_pick(mode, cands))
        if mode == "v4" and port_w:
            # 高估/价跌 强制轮出：PB>hi 或 价跌 的持仓剔除
            held = {c for c in port_w}
            new_w = {}
            for c, w in port_w.items():
                pb = None
                pr = None
                for cd, nm, pbct, price, vol, pbnow in cands:
                    if cd == c:
                        pb, pr = pbct, price
                        break
                if pb is not None and (pb > pb_hi or pr == "down"):
                    continue
                new_w[c] = w
            if new_w:
                # 轮出的权重按比例分给剩余的
                s = sum(new_w.values())
                port_w = {c: w / s for c, w in new_w.items()}
            else:
                port_w = {}
        if mode == "v5" and port_w:
            new_w = {}
            for c, w in port_w.items():
                pb = next((cd[2] for cd in cands if cd[0] == c), None)
                pr = next((cd[3] for cd in cands if cd[0] == c), None)
                if pb is not None and (pb > pb_hi or pr == "down"):
                    continue
                new_w[c] = w
            if new_w:
                s = sum(new_w.values())
                port_w = {c: w / s for c, w in new_w.items()}
            else:
                port_w = {}

        # 记录信号统计
        for c in port_w:
            for cd, nm, pbct, price, vol, pbnow in cands:
                if cd == c:
                    rec = per_etf.setdefault(c, {"name": nm, "n": 0, "pos_sum": 0.0,
                                                 "fw_win_sum": 0.0, "r1": [], "r3": []})
                    rec["n"] += 1
                    rec["pos_sum"] += port_w[c]
                    rec["fw_win_sum"] += 0.0
                    exec_ym = exec_day.strftime("%Y-%m")
                    r1 = etf_ret.get(c, {}).get(exec_ym)
                    if r1 is not None:
                        rec["r1"].append(r1)
                    r3 = B._realized_3m(etf_ret.get(c, {}), exec_ym)
                    if r3 is not None:
                        rec["r3"].append(r3)
                    break

        if port_w:
            turnover = sum(abs(port_w.get(kk, 0) - prev_w.get(kk, 0))
                           for kk in set(port_w) | set(prev_w))
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
    return {"nav": nav_series[-1], "max_dd": max_dd, "signals": sum(r["n"] for r in per_etf.values()),
            "n_etf": len(per_etf), "breaches": breaches}


def fmt(name, r, note=""):
    years = 91 / 12.0
    nav = r["nav"]
    cagr = (nav ** (1.0 / years) - 1) * 100 if nav > 0 else -100
    print(f"{name:<30} NAV={nav:>6.2f}  累计={nav*100-100:>7.1f}%  CAGR={cagr:>5.1f}%  "
          f"回撤={r['max_dd']*100:>5.1f}%  持仓月={r['signals']:>4}  涉及ETF={r['n_etf']:>2}  {note}")


def main():
    print("=" * 112)
    print("「低估买入+高估卖出」估值轮动变体  (2019-01 ~ 2026-08, 成本5bps)")
    print("=" * 112)
    base = B.backtest(B.load_universe_from_map(), START, END, COST, B.real_features, verbose=False)
    fmt("baseline 纪律框架(全信号)", {"nav": base["final_nav"], "max_dd": base["max_drawdown"],
                                        "signals": base["signal_count"], "n_etf": base["n_etf_signaled"],
                                        "breaches": base["breaches"]})
    for mode, k in (("v1", 5), ("v1", 3), ("v2", 0), ("v3", 0), ("v4", 0), ("v5", 0)):
        r = run_valuation(mode, k=k)
        if mode == "v1":
            fmt(f"v1 纯低估 最低PB{k}只", r, "（低估值因子裸测）")
        elif mode == "v2":
            fmt("v2 低估池 PB<30%", r, "（低估集合等权）")
        elif mode == "v3":
            fmt("v3 低估+量价 PB<30%且量价不坏", r, "（用户框架量化版）")
        elif mode == "v4":
            fmt("v4 低估+量价+高估卖出", r, "（PB>70%或价跌强制轮出）")
        elif mode == "v5":
            fmt("v5 买低估卖高估(严格轮动)", r, "（PB>70%即换入低估）")


if __name__ == "__main__":
    main()
