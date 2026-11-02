#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
幅度区分度检验 · 真实日频版 (magnitude_test_real.py)
===============================================
升级 magnitude_test.py：把"月度指数聚合 proxy"换成"真实日频指数序列"重跑，
验证"幅度（微升/大升）能否区分 3/6/12m 实现收益"。

数据口径（诚实声明）：
  - 本技能 data/ 无独立"行业产销量/库存/开工率"原始序列；框架以"跟踪指数=行业代表"。
  - price 幅度  ← raw/mx 真实日频指数 close（2019→2026，1848 交易日）
  - pb 幅度     ← raw/mx 日频 pb（优先）/ index_daily_pb 日频 pb（用户点名）
  - volume 幅度 ← index_monthly 月度指数成交量（仅 15/57 含日频量，故用月度，与旧版一致）
  - 真实收益    ← data/etf 真实 ETF 月度收益（t+1 执行、复利，与旧版一致）

方法：
  - 取样网格 month_ends 2019-01 ~ 2026-06，PIT 宇宙（listed_date 过滤）
  - 每个 (ETF, as_of) 用日频序列算 trailing 63 交易日均值 / 前 63 交易日均值 - 1 = 连续幅度
  - 档位：|chg|≤2%→flat；2~10%→micro；10~30%→sig；>30%→strong
  - 对齐 3/6/12m 真实 ETF 收益（t+1 执行，禁未来函数）
检验四组（每类幅度各跑）：
  (1) 连续幅度 AUC / Spearman vs 收益        (2) 按档位经验均值
  (3) 上行桶 强升 vs 微升                    (4) 下行桶 强降 vs 微降
结论判据：AUC≈0.5 / |corr|≈0 / 档位无单调差 → 幅度无信号，不进模型。
"""
from __future__ import annotations
import sys, os, json, math, glob
from datetime import date, timedelta
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import backtest as BT

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else float("nan")


def _parse(d):
    y, m, dd = d[:4], d[5:7], d[8:10]
    return date(int(y), int(m), int(dd))


def daily_chg(daily, as_of: date, recent=63, base=63):
    """日频序列 → trailing recent 交易日均值 / 前 base 交易日均值 - 1。"""
    vals = []
    for d, v in daily:
        if v is None:
            continue
        try:
            dt = _parse(d) if isinstance(d, str) else d
        except Exception:
            continue
        if dt <= as_of:
            vals.append((dt, float(v)))
    if len(vals) < recent + base:
        return None
    rec = vals[-recent:]
    bs = vals[-(recent + base):-recent]
    mr = _mean([v for _, v in rec])
    mb = _mean([v for _, v in bs])
    if mb == 0 or math.isnan(mb):
        return None
    return mr / mb - 1.0


def monthly_chg(mdict, ym, recent=3, base=3):
    """月度序列 {ym: v} → 近 recent 月均值 / 前 base 月均值 - 1。"""
    keys = sorted(mdict.keys())
    if ym not in keys:
        return None
    i = keys.index(ym)
    if i < recent + base:
        return None
    rec = [mdict[k] for k in keys[i - recent:i]]
    bs = [mdict[k] for k in keys[i - (recent + base):i - recent]]
    mr, mb = _mean(rec), _mean(bs)
    if mb == 0 or math.isnan(mb):
        return None
    return mr / mb - 1.0


def tier(chg):
    if chg is None:
        return "na"
    a = abs(chg)
    if a <= 0.02:
        return "flat"
    if a <= 0.10:
        return "micro"
    if a <= 0.30:
        return "sig"
    return "strong"


def realized_nm(ret_map: dict, exec_ym: str, n: int):
    y, m = int(exec_ym[:4]), int(exec_ym[5:7])
    cum, hit = 1.0, 0
    for _ in range(n):
        key = f"{y:04d}-{m:02d}"
        if key not in ret_map:
            break
        cum *= (1.0 + ret_map[key])
        hit += 1
        if m == 12:
            y, m = y + 1, 1
        else:
            m += 1
    return (cum - 1.0) if hit == n else None


def _rank(v):
    order = sorted(range(len(v)), key=lambda k: v[k])
    ranks = [0.0] * len(v)
    i = 0
    while i < len(v):
        j = i
        while j + 1 < len(v) and v[order[j + 1]] == v[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _paired(scores, labels):
    return [(s, l) for s, l in zip(scores, labels) if l is not None and s is not None]


def auc(scores, labels):
    pairs = _paired(scores, labels)
    scores = [p[0] for p in pairs]
    labels = [p[1] for p in pairs]
    n = len(scores)
    n_pos = sum(labels)
    n_neg = n - n_pos
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    order = sorted(range(n), key=lambda k: scores[k])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    sum_pos = sum(ranks[k] for k in range(n) if labels[k] == 1)
    return (sum_pos - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


def spearman(x, y):
    pairs = [(a, b) for a, b in zip(x, y) if a is not None and b is not None]
    x = [p[0] for p in pairs]
    y = [p[1] for p in pairs]
    n = len(x)
    rx = _rank(x)
    ry = _rank(y)
    mx, my = _mean(rx), _mean(ry)
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    dx = math.sqrt(sum((rx[i] - mx) ** 2 for i in range(n)))
    dy = math.sqrt(sum((ry[i] - my) ** 2 for i in range(n)))
    return num / (dx * dy) if dx and dy else float("nan")


def _tier_table(title, samples, key):
    print(f"\n  [{title}] 幅度档位 → 经验收益")
    order = ["flat", "micro", "sig", "strong", "na"]
    agg = defaultdict(lambda: {"n": 0, "w3": 0, "w6": 0, "w12": 0,
                               "r3": [], "r6": [], "r12": []})
    for s in samples:
        t = s[key]
        a = agg[t]
        a["n"] += 1
        a["w3"] += s["w3"]
        if s["w6"] is not None:
            a["w6"] += s["w6"]
        if s["w12"] is not None:
            a["w12"] += s["w12"]
        a["r3"].append(s["r3"])
        if s["r6"] is not None:
            a["r6"].append(s["r6"])
        if s["r12"] is not None:
            a["r12"].append(s["r12"])
    print(f"  {'档位':<7}{'N':>6}{'命中3m':>8}{'命中6m':>8}{'命中12m':>9}{'均收益3m':>10}{'均收益6m':>10}{'均收益12m':>11}")
    for t in order:
        if t not in agg:
            continue
        a = agg[t]
        n = a["n"]
        print(f"  {t:<7}{n:>6}{a['w3']/n*100:>7.1f}%{a['w6']/n*100:>7.1f}%{a['w12']/n*100:>8.1f}%"
              f"{_mean(a['r3'])*100:>9.1f}%{_mean(a['r6'])*100:>9.1f}%{_mean(a['r12'])*100:>10.1f}%")


def _split(title, samples, is_a, is_b, la, lb, field):
    A = [s[field] for s in samples if is_a(s)]
    B = [s[field] for s in samples if is_b(s)]
    ma, mb = _mean(A), _mean(B)
    print(f"  {title}：{la}(N={len(A)}) 均{field}={ma*100:+.1f}%  vs  {lb}(N={len(B)}) 均{field}={mb*100:+.1f}%"
          f"  差={(mb-ma)*100:+.1f}pp")


def main(start="2019-01-01", end="2026-06-30"):
    BT._MAP = json.load(open(BT.MAP, encoding="utf-8"))
    universe = BT.load_universe_from_map()
    etf_ret = BT.load_etf_returns()
    ends = BT.month_ends(start, end)

    # ---- 加载真实日频序列 ----
    raw_mx = {}
    for fn in glob.glob(os.path.join(BT.RAW_MX, "*.json")):
        rec = json.load(open(fn, encoding="utf-8"))
        raw_mx[rec.get("index_code")] = rec
    idx_daily_pb = {}
    for fn in glob.glob(os.path.join(BT.INDEX_DAILY_PB, "*.json")):
        rec = json.load(open(fn, encoding="utf-8"))
        idx_daily_pb[rec.get("index_code")] = rec
    idx_monthly = BT.load_index_monthly()

    # 指数成交量（月度）
    idx_vol = {}
    for code, rec in idx_monthly.items():
        if "volume" in rec and rec["volume"]:
            idx_vol[code] = rec["volume"]

    n_loaded = {"raw_mx": len(raw_mx), "idx_daily_pb": len(idx_daily_pb),
                "idx_monthly": len(idx_monthly), "idx_vol": len(idx_vol)}

    samples = []   # 每条含 price/pb/vol 三类幅度
    skipped = 0
    for etf in universe:
        code = etf["code"]
        info = BT._MAP.get(code)
        if not info:
            continue
        idx_code = info["index_code"]
        rm = raw_mx.get(idx_code)
        if not rm or "close" not in rm:
            continue
        ret_map = etf_ret.get(code, {})
        if not ret_map:
            continue
        # pb 来源：优先 raw/mx（回溯到2019），回退 index_daily_pb
        pb_series = rm.get("pb")
        if not pb_series and idx_daily_pb.get(idx_code):
            pb_series = idx_daily_pb[idx_code].get("pb")
        vol_m = idx_vol.get(idx_code)
        for sig_end in ends[:-1]:
            ld = etf.get("listed_date")
            if ld and ld > sig_end.isoformat():
                continue
            as_of = sig_end
            pc = daily_chg(rm["close"], as_of)
            pbc = daily_chg(pb_series, as_of) if pb_series else None
            vc = monthly_chg(vol_m, sig_end.strftime("%Y-%m")) if vol_m else None
            if pc is None:
                skipped += 1
                continue
            exec_ym = BT.first_of_next(sig_end).strftime("%Y-%m")
            r3 = realized_nm(ret_map, exec_ym, 3)
            r6 = realized_nm(ret_map, exec_ym, 6)
            r12 = realized_nm(ret_map, exec_ym, 12)
            if r3 is None:
                skipped += 1
                continue
            samples.append({
                "price": pc, "pb": pbc, "vol": vc,
                "pt": tier(pc), "pbt": tier(pbc) if pbc is not None else "na",
                "vt": tier(vc) if vc is not None else "na",
                "r3": r3, "r6": r6, "r12": r12,
                "w3": 1 if r3 > 0 else 0,
                "w6": (1 if r6 > 0 else 0) if r6 is not None else None,
                "w12": (1 if r12 > 0 else 0) if r12 is not None else None,
            })
    print(f"有效样本 {len(samples)} 条（跳过 {skipped}）")
    print(f"数据加载：raw_mx={n_loaded['raw_mx']}  idx_daily_pb={n_loaded['idx_daily_pb']}  "
          f"idx_monthly={n_loaded['idx_monthly']}  idx_vol(月度量)={n_loaded['idx_vol']}")

    # ============ 三类幅度各跑四组检验 ============
    srcs = [("价格(日频close)", "price", "pt"),
            ("PB(日频pb)", "pb", "pbt"),
            ("量(月度指数成交量)", "vol", "vt")]
    report = {"n_samples": len(samples), "n_skipped": skipped,
              "data_loaded": n_loaded,
              "method": "日频指数序列 trailing63/前63交易日均值比-1；PB优先raw/mx日频pb回退index_daily_pb；"
                        "量用月度指数成交量；3/6/12m真实ETF收益t+1执行复利",
              "sources": {}}

    for label, fld, tk in srcs:
        print(f"\n{'='*70}\n>>> 幅度源：{label}\n{'='*70}")
        # (1) AUC / Spearman
        print("\n  (1) 连续幅度 vs 收益 区分度")
        roc = {}
        for fkey, fget in (("原值", lambda s: s[fld]),
                           ("|绝对值|", lambda s: abs(s[fld]) if s[fld] is not None else None)):
            line = {}
            for hor in (3, 6, 12):
                fv = [fget(s) for s in samples]
                wc = [s[f"w{hor}"] for s in samples]
                rv = [s[f"r{hor}"] for s in samples]
                a = auc(fv, wc)
                c = spearman(fv, rv)
                line[f"{hor}m"] = {"auc": round(a, 3), "spearman": round(c, 3)}
                print(f"    {fkey:>6} vs {hor}m : AUC={a:.3f}  Spearman(r,ret)={c:+.3f}")
            roc[fkey] = line
        # (2) 档位表
        _tier_table(label, samples, tk)
        # (3) 上行桶 强升 vs 微升
        print(f"\n  (3) 上行桶 强升 vs 微升（{label}）")
        _split("    3m", samples,
               lambda t, tk=tk: t[tk] == "micro" and t[fld] is not None and t[fld] > 0,
               lambda t, tk=tk: t[tk] == "strong" and t[fld] is not None and t[fld] > 0,
               "微升", "强升", "r3")
        _split("    12m", samples,
               lambda t, tk=tk: t[tk] == "micro" and t[fld] is not None and t[fld] > 0,
               lambda t, tk=tk: t[tk] == "strong" and t[fld] is not None and t[fld] > 0,
               "微升", "强升", "r12")
        # (4) 下行桶
        print(f"\n  (4) 下行桶 强降 vs 微降（{label}）")
        _split("    3m", samples,
               lambda t, tk=tk: t[tk] == "micro" and t[fld] is not None and t[fld] < 0,
               lambda t, tk=tk: t[tk] == "strong" and t[fld] is not None and t[fld] < 0,
               "微降", "强降", "r3")
        _split("    12m", samples,
               lambda t, tk=tk: t[tk] == "micro" and t[fld] is not None and t[fld] < 0,
               lambda t, tk=tk: t[tk] == "strong" and t[fld] is not None and t[fld] < 0,
               "微降", "强降", "r12")
        a_abs = roc["|绝对值|"]["3m"]["auc"]
        report["sources"][label] = {"auc_spearman": roc,
                                    "abs_auc_3m": a_abs,
                                    "verdict": "no_signal" if abs(a_abs - 0.5) < 0.03 else "signal"}

    # ---- 总结 ----
    print("\n" + "=" * 70)
    print("汇总结论（真实日频口径 vs 旧版月度proxy）")
    print("=" * 70)
    for label, fld, tk in srcs:
        a_abs = report["sources"][label]["abs_auc_3m"]
        v = report["sources"][label]["verdict"]
        print(f"  {label:<22} |幅度绝对值| 3m AUC={a_abs:.3f} → {'无信号' if v=='no_signal' else '有信号'}")
    print("\n诚实声明：本技能 data/ 无独立行业产销量/库存原始序列；上述'真实'=跟踪指数日频序列")
    print("（框架以指数=行业代表）。若要真正独立的行业供需量价，需经 iFinD/妙想 另行取数。")

    with open(os.path.join(HERE, "data", "magnitude_test_real_report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("\n报告已落盘 → data/magnitude_test_real_report.json")


if __name__ == "__main__":
    args = sys.argv[1:]
    s = args[args.index("--start") + 1] if "--start" in args else "2019-01-01"
    e = args[args.index("--end") + 1] if "--end" in args else "2026-06-30"
    main(s, e)
