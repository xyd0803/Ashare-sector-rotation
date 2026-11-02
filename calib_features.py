#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
V4/O5 特征导出 (calib_features.py)
=================================
为 calibrate_winrate.py 产出 backtest_features.json：
  每条 = {as_of, sector, index_code, S, win_3m, win_6m, win_12m, fwd_ret_3m, ...}

为什么需要它（关键事实）：
  现有 backtest.py 的 real_features 只填 volume/price/demand/pb_pct，
  从不填量化输入 q_price_mom6/q_vol_yoy…，导致引擎走「§12.1 六档量价表」
  而非 quant_winrate.compute_quant —— 回测里【根本没有 S】可供 g(S) 校准。

本脚本把「量化输入契约(O1)」接进回测：用【可获得的指数量价历史】派生
D1~D5 量化输入（行业周期量价的 proxy 占位，与 real_features 同一来源），
让每只信号算出真实的 S，再配对 ETF 实现收益给出 win 标签。

诚实标注：这里的量化输入是【跟踪指数涨跌/量能的 proxy】，不是真实行业
周期量价（产销量/库存/开工率）。因此本校准是「管线可行性 + 占位特征诊断」，
不是决策级权重。若校准显示 S 对收益无区分度，结论应是「保留保守占位 g(S)，
待真实行业周期量价接入后再校」。

口径（禁未来函数）：
  - as_of 月生成信号；exec = 次月首日；收益从 exec 起算（t+1 执行）。
  - 仅纳入 list_date <= as_of 的 ETF（point-in-time 宇宙）。
  - 成本不作为特征（仅收益端隐含，3m 收益来自 ETF 月度净值）。
"""
from __future__ import annotations
import sys, os, json
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import backtest as BT                                   # 复用数据装载
from discipline import percentile, rank_pct             # noqa: E402
from quant_winrate import compute_quant                 # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _load_json(p):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# 量化输入派生（V4 契约：从可获得指数量价历史）→ D1~D5
# ---------------------------------------------------------------------------
def derive_quant_inputs(idx: dict, months: list, i: int, daily_pb=None):
    """返回喂给 compute_quant 的 quant dict（proxy 占位特征）。

    idx: {pb, close, volume} 月度映射
    months: 排序后的 ym 列表；i: as_of 所在下标
    """
    ym = months[i]
    close_now = idx["close"].get(ym)
    if close_now is None:
        return None
    # —— D1 价格强度：6月动量 + 价格历史分位 ——
    price_mom6 = None
    if i >= 6:
        c6 = idx["close"].get(months[i - 6])
        if c6:
            price_mom6 = (close_now / c6 - 1.0) * 100.0
    # 价格历史分位（proxy for 行业核心品价格分位）：截至 as_of 的月度收盘序列
    price_series = [idx["close"][m] for m in months[:i + 1]
                    if idx["close"].get(m) is not None]
    price_pct = rank_pct(price_series, close_now) if len(price_series) >= 5 else None

    # —— D2 量能：量同比（proxy for 产销量同比）；库存/开工率无数据→中性 ——
    vol_now = idx["volume"].get(ym)
    vol_yoy = None
    if i >= 12:
        v12 = idx["volume"].get(months[i - 12])
        if vol_now is not None and v12:
            vol_yoy = (vol_now / v12 - 1.0) * 100.0
    # —— D3 资金：成交额趋势 = 量环比（proxy）——
    fund_turn_pct = None
    if i >= 1:
        vol_prev = idx["volume"].get(months[i - 1])
        if vol_now is not None and vol_prev:
            fund_turn_pct = (vol_now / vol_prev - 1.0) * 100.0
    # inv_yoy / util / fund_flow_pct / earning_rev：无数据 → 留 None（中性 50）

    # —— D5 结构：用指数月度收盘序列自动算技术分 ——
    prices = price_series if len(price_series) >= 20 else None

    q = {
        "price_mom6": round(price_mom6, 2) if price_mom6 is not None else None,
        "price_pct": round(price_pct, 2) if price_pct is not None else None,
        "vol_yoy": round(vol_yoy, 2) if vol_yoy is not None else None,
        "inv_yoy": None,
        "util": None,
        "fund_flow_pct": None,
        "fund_turn_pct": round(fund_turn_pct, 2) if fund_turn_pct is not None else None,
        "earning_rev": None,
    }
    if prices:
        q["prices"] = [round(x, 4) for x in prices]
    return q


def realized_nm(ret_map: dict, exec_ym: str, n: int):
    """exec_ym 起往后 n 个自然月累计收益（复利）；不足 n 月返回 None。"""
    y, m = int(exec_ym[:4]), int(exec_ym[5:7])
    cum = 1.0
    hit = 0
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


def main(start="2019-01-01", end="2026-06-30"):
    BT._MAP = _load_json(BT.MAP)
    BT._IDX_MONTHLY = BT.load_index_monthly()
    BT._RAW_DAILY = BT.load_raw_daily()
    universe = BT.load_universe_from_map()
    etf_ret = BT.load_etf_returns()

    ends = BT.month_ends(start, end)
    rows = []
    skipped = {"no_idx": 0, "no_feature": 0, "no_win": 0}
    for etf in universe:
        code = etf["code"]
        info = BT._MAP.get(code)
        if not info:
            continue
        idx_code = info["index_code"]
        idx = BT._IDX_MONTHLY.get(idx_code)
        if not idx or "close" not in idx:
            skipped["no_idx"] += 1
            continue
        months = sorted(idx["close"].keys())
        ret_map = etf_ret.get(code, {})
        for sig_end in ends[:-1]:
            # PIT：ETF 须已上市
            ld = etf.get("listed_date")
            if ld and ld > sig_end.isoformat():
                continue
            ym = sig_end.strftime("%Y-%m")
            if ym not in idx["close"] or months.index(ym) < 1:
                continue
            i = months.index(ym)
            q = derive_quant_inputs(idx, months, i)
            if q is None:
                skipped["no_feature"] += 1
                continue
            try:
                qr = compute_quant(q)
            except Exception:
                skipped["no_feature"] += 1
                continue
            S = qr["S"]
            exec_day = BT.first_of_next(sig_end)
            exec_ym = exec_day.strftime("%Y-%m")
            r3 = realized_nm(ret_map, exec_ym, 3)
            r6 = realized_nm(ret_map, exec_ym, 6)
            r12 = realized_nm(ret_map, exec_ym, 12)
            if r3 is None:
                skipped["no_win"] += 1
                continue
            rows.append({
                "as_of": sig_end.isoformat(),
                "sector": etf["name"],
                "index_code": idx_code,
                "S": S,
                "win": 1 if r3 > 0 else 0,           # 契约主字段（3m 持有期）
                "win_3m": 1 if r3 > 0 else 0,
                "win_6m": (1 if r6 > 0 else 0) if r6 is not None else None,
                "win_12m": (1 if r12 > 0 else 0) if r12 is not None else None,
                "fwd_ret_3m_pct": round(r3 * 100, 2),
                "fwd_ret_6m_pct": round(r6 * 100, 2) if r6 is not None else None,
                "fwd_ret_12m_pct": round(r12 * 100, 2) if r12 is not None else None,
            })
    out = os.path.join(HERE, "data", "backtest_features.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    print(f"写出特征 {len(rows)} 条 → {out}")
    print(f"跳过统计: {skipped}")
    # 简要分桶预览（按 S 十分位经验胜率）
    from calibrate_winrate import fit_empirical, validate
    emp = fit_empirical(rows, n_bins=10)
    print(f"\n[预览] 经验分桶 win_map(3m持有期): {emp}")
    v = validate(rows, emp, n_bins=10)
    print(f"[预览] 校验: AUC={v['auc']} 单调={v['monotonic']} 校准误差={v['calibration_error_pctpts']}pp")
    return rows


if __name__ == "__main__":
    args = sys.argv[1:]
    s = args[args.index("--start") + 1] if "--start" in args else "2019-01-01"
    e = args[args.index("--end") + 1] if "--end" in args else "2026-06-30"
    main(s, e)
