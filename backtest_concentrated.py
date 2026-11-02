#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
集中轮动复演器（单只全仓 · 动态换仓）—— 回答「50+ETF池 5年10倍可行性」
=====================================================================
口径（与 backtest.py 完全一致的数据与守卫）：
  · 特征：跟踪指数月度 close/volume + point-in-time 5Y 日频 PB 分位（禁未来函数）
  · 收益：ETF 自身月度 close（t+1 月执行，信号月末生成）
  · 宇宙：etf_index_map 55 只，list_date ≤ 执行日（PIT，防幸存者偏差）
  · 成本：单边 5bps，按换手 |Δw| 计

策略状态机（月频）：
  1. 对 PIT 宇宙逐只 discipline.evaluate() → 候选集（status=OK 且建议仓位>0）
  2. 当前持有若仍在候选集 → 继续持有（不空转换仓）
  3. 否则换入新候选（按顺位口径取第一名）；候选集空 → 空仓现金
  4. 可选硬止损：持有期单月收益 ≤ -10% → 下月强制离场（即使仍是候选）
  · 「动态建仓」在月频粒度退化为一次性满仓（周频分批无法在月度数据上诚实建模）

顺位口径（回答「恰好选到比较好的」运气分布）：
  A frame  : 框架顺位 —— win_rate 降序 → 建议仓位降序 → pb_pct 升序
  B value  : 估值顺位 —— pb_pct 升序（PE锚标的用 pb_pct 代理，见局限）
  C random : 候选集内随机 200 个种子 → P5/P50/P95 运气分布
  D oracle : 完美事后 —— 候选集中挑下月实际收益最高者（策略理论上限）
  V1s/V2s  : A/B + 月度 -10% 硬止损变体

⚠️ 诚实声明（继承 backtest.py 口径铁律）：量价特征为「跟踪标的自身涨跌」近似占位，
   非行业周期量价；demand/imagination 为中性占位。本复演是【信号引擎管线级】结论，
   用于回答数量级问题（2倍还是10倍），非决策级裁决。

运行：python backtest_concentrated.py [--start 2021-08-01 --end 2026-06-30 --cost 5]
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest as bt  # noqa: E402  复用加载器/real_features/PIT守卫
from discipline import evaluate, load_params  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# ---------------------------------------------------------------------------
def month_ends(start: str, end: str):
    return bt.month_ends(start, end)


def first_of_next(d: date) -> date:
    return bt.first_of_next(d)


def rank_key(feat, verdict, mode: str):
    """返回排序元组（小者优先）。mode: frame | value"""
    wr = verdict.get("win_rate")
    wr = -(wr if wr is not None else 0.0)          # 降序 → 取负
    pos = -float(str(verdict["suggested_position"]).rstrip("%"))
    pb = feat["pb_pct"] if feat.get("pb_pct") is not None else 100.0
    if mode == "frame":
        return (wr, pos, pb)
    return (pb, wr, pos)


def main() -> int:
    global_map = bt._load_json(bt.MAP)
    bt._MAP = global_map
    bt._IDX_MONTHLY = bt.load_index_monthly()
    bt._RAW_DAILY = bt.load_raw_daily()
    rets = bt.load_etf_returns()
    universe = [{"code": c, "name": v["name"], "listed_date": v["list_date"]}
                for c, v in global_map.items()]
    params = load_params()

    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-08-01")
    ap.add_argument("--end", default="2026-06-30")
    ap.add_argument("--cost", type=float, default=5.0)
    ap.add_argument("--seeds", type=int, default=200)
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    ends = month_ends(a.start, a.end)
    print("=" * 88)
    print("XYD 供需板块框架 · 集中轮动复演（单只全仓）")
    print(f"  窗口={a.start}~{a.end}  执行月={ends[0].year}-{ends[0].month:02d}+1 ~ {ends[-1].year}-{ends[-1].month:02d}  "
          f"宇宙={len(universe)}  成本={a.cost}bps(月频近似不计)")

    # ---- 预计算：每月 PIT 宇宙的 evaluate 候选集（frame/value 两种顺位）----
    cands_by_month: dict[str, list] = {}
    n_sig_total = 0
    for sig_end in ends[:-1]:
        exec_day = first_of_next(sig_end)
        pit = bt.guards.filter_pit_universe(universe, exec_day.isoformat(), strict=True)
        cands = []
        for etf in pit:
            feat = bt.real_features(etf, sig_end)
            if feat is None:
                continue
            try:
                v = evaluate(feat)
            except Exception:
                continue
            if v.get("status") != "OK":
                continue
            pos = float(str(v["suggested_position"]).rstrip("%"))
            if pos <= 0:
                continue
            cands.append((etf["code"], feat, v))
        n_sig_total += len(cands)
        kf = sorted(cands, key=lambda t: rank_key(t[1], t[2], "frame"))
        kv = sorted(cands, key=lambda t: rank_key(t[1], t[2], "value"))
        cands_by_month[sig_end] = {"frame": kf, "value": kv}
        if a.verbose:
            names = [f"{c}:{v['suggested_position']}/{v['win_rate']}" for c, _f, v in kf]
            print(f"  {sig_end} 候选{len(kf)} {names[:5]}")
    n_months_with_cand = sum(1 for s in ends[:-1] if cands_by_month[s]["frame"])
    print(f"  信号月覆盖率={n_months_with_cand}/{len(ends)-1}  信号·月总数={n_sig_total}")

    results = []

    def run_mode(mode_key: str, label: str, seed=None, stop=False, oracle=False):
        rng = random.Random(seed) if seed is not None else None
        nav = 1.0
        series = [1.0]
        peak, max_dd = 1.0, 0.0
        holding, force_exit = None, False
        stopped_code = None
        rotations = months_in = 0
        n_stop_fired = 0
        hold_map: dict[str, int] = {}
        for sig_end in ends[:-1]:
            exec_day = first_of_next(sig_end)
            exec_ym = exec_day.strftime("%Y-%m")
            cands = cands_by_month[sig_end][mode_key]
            if stopped_code is not None:
                cands = [(c, f, v) for c, f, v in cands if c != stopped_code]
                stopped_code = None
            cand_codes = [c for c, _f, _v in cands]
            if holding is not None and holding not in cand_codes:
                holding = None
            if stop and holding is not None:
                r_last = rets.get(holding, {}).get(exec_ym)
                if r_last is not None and r_last <= -0.10:
                    stopped_code = holding
                    holding = None
                    force_exit = False
                    n_stop_fired += 1
            if holding is None and cands:
                if oracle:
                    scored = [(rets.get(c, {}).get(exec_ym), c) for c, _f, _v in cands
                              if rets.get(c, {}).get(exec_ym) is not None]
                    if scored:
                        holding = max(scored)[1]
                        rotations += 1
                        hold_map[holding] = hold_map.get(holding, 0) + 1
                else:
                    pick = (rng.choice(cands)[0] if rng is not None else cands[0][0])
                    if pick != holding:
                        holding = pick
                        rotations += 1
                    hold_map[holding] = hold_map.get(holding, 0) + 1
            if holding is not None:
                r = rets.get(holding, {}).get(exec_ym)
                nav *= (1.0 + (r if r is not None else 0.0))
                months_in += 1
            series.append(round(nav, 4))
            peak = max(peak, nav)
            max_dd = max(max_dd, (peak - nav) / peak)
        years = (len(ends) - 1) / 12.0
        cagr = nav ** (1.0 / years) - 1.0 if nav > 0 else -1.0
        return {"mode": label, "seed": seed, "final_nav": round(nav, 4),
                "cagr": round(cagr, 4), "max_dd": round(max_dd, 4),
                "rotations": rotations, "months_in_market": months_in,
                "n_stop_fired": n_stop_fired,
                "n_months": len(ends) - 1,
                "top_holds": sorted(hold_map.items(), key=lambda kv: -kv[1])[:6]}

    results.append(run_mode("frame", "A_frame_顺位"))
    results.append(run_mode("frame", "A_frame_顺位+止损10%", stop=True))
    results.append(run_mode("value", "B_value_顺位(低PB优先)"))
    results.append(run_mode("value", "B_value_顺位+止损10%", stop=True))
    results.append(run_mode("frame", "D_oracle_完美事后", oracle=True))

    # C: 随机运气分布
    rnd_runs = []
    for s in range(a.seeds):
        rnd_runs.append(run_mode("frame", f"C_random#{s}", seed=s))
    navs = sorted(r["final_nav"] for r in rnd_runs)
    def pct(p):
        i = min(len(navs) - 1, max(0, int(p * (len(navs) - 1))))
        return navs[i]
    results.append({"mode": f"C_random_{a.seeds}seeds",
                    "nav_p05": pct(0.05), "nav_p50": pct(0.50), "nav_p95": pct(0.95),
                    "cagr_p05": round(pct(0.05) ** (12.0 / (len(ends) - 1)) - 1, 4),
                    "cagr_p50": round(pct(0.50) ** (12.0 / (len(ends) - 1)) - 1, 4),
                    "cagr_p95": round(pct(0.95) ** (12.0 / (len(ends) - 1)) - 1, 4),
                    "max_dd_median": round(sorted(r["max_dd"] for r in rnd_runs)[len(rnd_runs)//2], 4),
                    "best": navs[-1], "worst": navs[0]})

    # 等权基准（满仓全池等权，无成本）
    bm = 1.0
    for sig_end in ends[:-1]:
        exec_ym = first_of_next(sig_end).strftime("%Y-%m")
        rs = [rets[c].get(exec_ym) for c in global_map
              if rets.get(c, {}).get(exec_ym) is not None]
        if rs:
            bm *= (1.0 + sum(rs) / len(rs))
    results.append({"mode": "基准_全池满仓等权(无成本)", "final_nav": round(bm, 4),
                    "cagr": round(bm ** (12.0 / (len(ends) - 1)) - 1, 4)})

    print("\n  === 集中轮动复演结果（单只全仓，月频换仓）===")
    for r in results:
        print(f"  {json.dumps(r, ensure_ascii=False)}")
    print(f"  → 10倍需要年化58.5%；上表 A/B/C/D 对照直接回答可行性")

    out = {"window": [a.start, a.end], "n_months": len(ends) - 1,
           "universe": len(universe), "signal_month_coverage": f"{n_months_with_cand}/{len(ends)-1}",
           "results": results}
    rep = os.path.join(HERE, "runs", "2026-09-07", "backtest_concentrated_report.json")
    os.makedirs(os.path.dirname(rep), exist_ok=True)
    with open(rep, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"  报告 -> {rep}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
