#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
strong_test.py —— 「强信号子集」变体回测：只在框架胜率达标的 ETF 上开仓

模式：
  baseline : 全信号（与 backtest.py 一致，用于校验）
  rolling  : 每期用该 ETF「截至上一期」的滚动平均信号框架胜率过滤（point-in-time，无未来函数）
             threshold ∈ {55,60,65}，warmup：信号数<5 时放行
  static   : 用全样本 avg_framework_winrate≥60 且 realized_3m_win_pct≥55 过滤宇宙后重跑
             （含前视偏差，仅作理论上限参考）

输出：各档 NAV / CAGR / 最大回撤 / 信号数 / 触发ETF数 / 守卫违例
"""
import json, os, sys, argparse
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import backtest as B

MAPF = os.path.join(HERE, "data", "meta", "etf_index_map.json")
REPORT = os.path.join(HERE, "data", "backtest_report.json")

START, END, COST = "2019-01-01", "2026-08-13", 5.0


def load_globals():
    B._MAP = json.load(open(MAPF, encoding="utf-8"))
    B._IDX_MONTHLY = B.load_index_monthly()
    B._RAW_DAILY = B.load_raw_daily()


def run_baseline():
    u = B.load_universe_from_map()
    return B.backtest(u, START, END, COST, B.real_features, verbose=False)


def run_rolling(threshold, warmup=5):
    """滚动过滤主循环（复制 backtest 逻辑 + fwWin 过滤）"""
    u = B.load_universe_from_map()
    etf_ret = B.load_etf_returns()
    ends = B.month_ends(START, END)
    nav, nav_series = 1.0, [1.0]
    prev_w, breaches = {}, []
    signal_wins, per_etf = [], {}
    fw_hist = {}   # code -> [win_rate,...]（历史全部候选信号）

    for sig_end in ends[:-1]:
        exec_day = B.first_of_next(sig_end)
        pit = B.guards.filter_pit_universe(u, exec_day.isoformat(), strict=True)
        raw_w = {}
        for etf in pit:
            feat = B.real_features(etf, sig_end)
            if feat is None:
                continue
            try:
                v = B.evaluate(feat)
            except Exception:
                continue
            if v["status"] != "OK":
                continue
            pos = float(str(v["suggested_position"]).rstrip("%")) / 100.0
            if pos <= 0:
                continue
            code = etf["code"]
            wr = v["win_rate"]
            if wr is None:
                continue
            hist = fw_hist.get(code, [])
            # 强信号过滤：历史信号数≥warmup 且滚动均值<阈值 → 拒
            if len(hist) >= warmup and (sum(hist) / len(hist)) < threshold:
                continue
            raw_w[code] = pos
            signal_wins.append(wr)
            fw_hist.setdefault(code, []).append(wr)
            rec = per_etf.setdefault(code, {"name": etf["name"], "n": 0, "pos_sum": 0.0,
                                            "fw_win_sum": 0.0, "r1": [], "r3": []})
            rec["n"] += 1
            rec["pos_sum"] += pos
            rec["fw_win_sum"] += wr
            exec_ym = exec_day.strftime("%Y-%m")
            r1 = etf_ret.get(code, {}).get(exec_ym)
            if r1 is not None:
                rec["r1"].append(r1)
            r3 = B._realized_3m(etf_ret.get(code, {}), exec_ym)
            if r3 is not None:
                rec["r3"].append(r3)

        tot = sum(raw_w.values())
        if tot <= 0:
            port_w = {}
        elif tot > 1.0:
            port_w = {k: w / tot for k, w in raw_w.items()}
        else:
            port_w = raw_w
        if port_w:
            turnover = sum(abs(port_w.get(k, 0) - prev_w.get(k, 0))
                           for k in set(port_w) | set(prev_w))
            cost = turnover * COST / 10000.0
            port_ret = sum(w * etf_ret.get(code, {}).get(exec_day.strftime("%Y-%m"), 0.0)
                           for code, w in port_w.items())
            nav = nav * (1.0 - cost) * (1.0 + port_ret)
        nav_series.append(nav)
        prev_w = port_w

    peak = nav_series[0]
    max_dd = 0.0
    for x in nav_series:
        peak = max(peak, x)
        max_dd = max(max_dd, (peak - x) / peak)
    wr_sig = (sum(1 for w in signal_wins if w >= 50) / len(signal_wins)) if signal_wins else None
    return {"nav": nav_series[-1], "max_dd": max_dd, "signals": len(signal_wins),
            "win_rate": wr_sig, "n_etf": len(per_etf), "nav_series": nav_series,
            "per_etf": per_etf, "breaches": breaches}


def run_momentum_topk(k, lookback=3):
    """信号内动量轮动：每期从 evaluate OK 的信号 ETF 中，按截至 sig_end 的过去 N 月
    动量（用 ETF 自身月度收益）排名，取前 k 等权持有。无未来函数。"""
    u = B.load_universe_from_map()
    etf_ret = B.load_etf_returns()
    ends = B.month_ends(START, END)
    nav, nav_series = 1.0, [1.0]
    prev_w, breaches = {}, []
    signal_wins, per_etf = [], {}

    def momentum(code, ym, lb):
        rets = etf_ret.get(code, {})
        if ym not in rets:
            return None
        # ym 及往前 lb 个月的累计（含 ym）
        y, m = int(ym[:4]), int(ym[5:7])
        keys = []
        for _ in range(lb):
            keys.append(f"{y:04d}-{m:02d}")
            if m == 1:
                y, m = y - 1, 12
            else:
                m -= 1
        keys = keys[::-1]
        if not all(k in rets for k in keys):
            return None
        cum = 1.0
        for kk in keys:
            cum *= (1.0 + rets[kk])
        return cum - 1.0

    for sig_end in ends[:-1]:
        exec_day = B.first_of_next(sig_end)
        sig_ym = sig_end.strftime("%Y-%m")
        pit = B.guards.filter_pit_universe(u, exec_day.isoformat(), strict=True)
        cands = []
        for etf in pit:
            feat = B.real_features(etf, sig_end)
            if feat is None:
                continue
            try:
                v = B.evaluate(feat)
            except Exception:
                continue
            if v["status"] != "OK" or not (float(str(v["suggested_position"]).rstrip("%")) / 100.0 > 0):
                continue
            wr = v["win_rate"]
            if wr is None:
                continue
            mom = momentum(etf["code"], sig_ym, lookback)
            if mom is None:
                continue
            cands.append((etf["code"], etf["name"], mom, wr))
        if not cands:
            port_w = {}
        else:
            cands.sort(key=lambda x: -x[2])
            top = cands[:k]
            port_w = {c: 1.0 / len(top) for c, _, _, _ in top}
            for c, nm, mom, wr in top:
                signal_wins.append(wr)
                rec = per_etf.setdefault(c, {"name": nm, "n": 0, "pos_sum": 0.0,
                                             "fw_win_sum": 0.0, "r1": [], "r3": []})
                rec["n"] += 1
                rec["pos_sum"] += 1.0 / len(top)
                rec["fw_win_sum"] += wr
                exec_ym = exec_day.strftime("%Y-%m")
                r1 = etf_ret.get(c, {}).get(exec_ym)
                if r1 is not None:
                    rec["r1"].append(r1)
                r3 = B._realized_3m(etf_ret.get(c, {}), exec_ym)
                if r3 is not None:
                    rec["r3"].append(r3)
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
    wr_sig = (sum(1 for w in signal_wins if w >= 50) / len(signal_wins)) if signal_wins else None
    return {"nav": nav_series[-1], "max_dd": max_dd, "signals": len(signal_wins),
            "win_rate": wr_sig, "n_etf": len(per_etf), "breaches": breaches}


def run_static(fw_th=60, r3w_th=55):
    """静态事后过滤（前视，仅上限参考）"""
    base = run_baseline()
    strong = {c: d for c, d in base["per_etf"].items()
              if (d["avg_framework_winrate"] or 0) >= fw_th
              and (d["realized_3m_win_pct"] or 0) >= r3w_th}
    u = [e for e in B.load_universe_from_map() if e["code"] in strong]
    r = B.backtest(u, START, END, COST, B.real_features, verbose=False)
    return r, len(strong)


def fmt(name, nav, dd, sig, netf, wr, breaches, note=""):
    years = 91 / 12.0
    cagr = (nav ** (1.0 / years) - 1) * 100 if nav > 0 else -100
    print(f"{name:<26} NAV={nav:>6.2f}  累计={nav*100-100:>7.1f}%  CAGR={cagr:>5.1f}%  "
          f"回撤={dd*100:>5.1f}%  信号={sig:>4}  触发ETF={netf:>2}  胜率={wr*100 if wr else 0:>5.1f}%  "
          f"违例={len(breaches)}{note}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--thresholds", nargs="*", default=["55", "60", "65"], help="滚动过滤阈值档")
    a = ap.parse_args()
    load_globals()
    print("=" * 120)
    print("强信号子集变体回测  (2019-01 ~ 2026-08, 成本5bps, 宇宙54)")
    print("=" * 120)

    base = run_baseline()
    fmt("baseline 全信号", base["final_nav"], base["max_drawdown"], base["signal_count"],
        base["n_etf_signaled"], base["win_rate_of_signals"], base["breaches"])

    for th in a.thresholds:
        th = float(th)
        r = run_rolling(th)
        fmt(f"rolling 过滤 fwWin>={th:.0f}", r["nav"], r["max_dd"], r["signals"],
            r["n_etf"], r["win_rate"], r["breaches"], f"  (滚动均值, warmup=5)")

    for k in (5, 3):
        r = run_momentum_topk(k)
        fmt(f"信号内动量 Top{k}", r["nav"], r["max_dd"], r["signals"],
            r["n_etf"], r["win_rate"], r["breaches"], "  (纪律信号+动量排名)")

    sr, nstrong = run_static()
    fmt(f"static 全样本fw>={60}&3m胜>={55}", sr["final_nav"], sr["max_drawdown"],
        sr["signal_count"], sr["n_etf_signaled"], sr["win_rate_of_signals"], sr["breaches"],
        f"  (前视·上限参考, 宇宙缩到{nstrong}只)")

    # 静态版保留的 ETF
    base = run_baseline()
    strong = sorted([(c, d["avg_framework_winrate"], d["realized_3m_win_pct"], d["n_signals"])
                     for c, d in base["per_etf"].items()
                     if (d["avg_framework_winrate"] or 0) >= 60 and (d["realized_3m_win_pct"] or 0) >= 55],
                    key=lambda x: -x[1])
    print("-" * 120)
    print("静态强信号 ETF 清单（fw≥60 且 3m胜率≥55）：")
    for c, fw, r3w, n in strong:
        print(f"  {c}  fwWin={fw:.1f}  3m胜率={r3w:.1f}  信号数={n}")


if __name__ == "__main__":
    main()
