#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XYD 供需板块框架 —— 真实回测 (Backtest · 真实数据版)
================================================
口径（用户指令）：
  · 量价信号用 ETF 跟踪指数（非 ETF 自身）的月度 close/volume
  * PB 分位用跟踪指数的市净率（5年日频，point-in-time，禁未来函数）
  * 收益端用 ETF 自身月度净值（月K close）
  * 仅纳入回测区间内已上市的 ETF（list_date ≤ as_of，point-in-time）
  * 信号 as_of 月生成，t+1（次月首个交易日）执行

数据源（本地仓库 data/，由妙想 REST 落地）：
  data/meta/etf_index_map.json     ETF -> 跟踪指数
  data/index_monthly/*.json        各指数月度 PB/close/volume（由 10年日频重采样）
  data/raw/mx/{code}.json          各指数 10年日频 PB/close/volume 原始序列
  data/etf/{code}.json             ETF 月度 close（腾讯 westock，收益端）

纪律引擎 discipline.evaluate() 产出胜率/象限/仓位；本脚本只负责：
  G1 未来函数（as_of 生成，t+1 执行）、G3 幸存者（point-in-time 宇宙）、
  G6 成本/期数真实性、参数指纹冻结。

运行：
  python backtest.py                       # 用 etf_index_map 全部 ETF，2019-01-01~2026-06-30
  python backtest.py --start 2020-01-01 --end 2026-06-30 --cost 5
  python backtest.py --input manifest.json # 自定义 universe/start/end/cost_bps
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, timedelta
from typing import Any, Callable, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from discipline import evaluate, load_params, percentile, rank_pct   # noqa: E402
import guards                                                                 # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")                  # type: ignore[attr-defined]
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
P = load_params()
LOCK = None
_lp = os.path.join(HERE, "params.lock")
if os.path.exists(_lp):
    with open(_lp, "r", encoding="utf-8") as f:
        LOCK = json.load(f).get("fingerprint")

DATA = os.path.join(HERE, "data")
MAP = os.path.join(DATA, "meta", "etf_index_map.json")
IDX_MONTHLY = os.path.join(DATA, "index_monthly")
RAW_MX = os.path.join(DATA, "raw", "mx")
INDEX_DAILY_PB = os.path.join(DATA, "index_daily_pb")
ETF_MONTHLY = os.path.join(DATA, "etf")

VOL_BAND = 0.05     # 量环比 ±5% 死区
PRICE_BAND = 0.005  # 价环比 ±0.5% 死区


# =============================================================================
# 数据加载
# =============================================================================
def _load_json(p):
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def load_universe_from_map() -> List[Dict[str, Any]]:
    mp = _load_json(MAP)
    return [{"code": c, "name": v["name"], "listed_date": v["list_date"]} for c, v in mp.items()]


def load_index_monthly() -> Dict[str, Dict[str, Any]]:
    """index_code -> {pb:{ym:v}, close:{ym:v}, volume:{ym:v}, etf_code}"""
    out = {}
    mp = _load_json(MAP)
    code2idx = {v["index_code"]: c for c, v in mp.items()}
    if not os.path.isdir(IDX_MONTHLY):
        return out
    for fn in os.listdir(IDX_MONTHLY):
        if not fn.endswith(".json"):
            continue
        rec = _load_json(os.path.join(IDX_MONTHLY, fn))
        out[rec["index_code"]] = rec
    return out


def load_raw_daily() -> Dict[str, List[List[Any]]]:
    """index_code -> [[date, pb], ...] (仅 PB 日频，用于 point-in-time 分位)
    优先 data/index_daily_pb/（5 年日频，mx 直拉/重建/兜底），回退 raw/mx。"""
    out = {}
    for src in (INDEX_DAILY_PB, RAW_MX):
        if not os.path.isdir(src):
            continue
        for fn in os.listdir(src):
            if not fn.endswith(".json"):
                continue
            idx = fn[:-5]
            if idx in out:
                continue
            try:
                d = _load_json(os.path.join(src, fn))
            except Exception:
                continue
            pb = d.get("pb") or []
            if pb:
                out[idx] = pb
    return out


def load_etf_returns() -> Dict[str, Dict[str, float]]:
    """etf_code -> {ym: monthly_return}"""
    out = {}
    mp = _load_json(MAP)
    for code in mp:
        p = os.path.join(ETF_MONTHLY, f"{code}.json")
        if not os.path.exists(p):
            continue
        try:
            d = _load_json(p)
        except Exception:
            continue
        monthly = d.get("monthly", [])
        closes = {m["date"][:7]: m["close"] for m in monthly if m.get("date") and m.get("close")}
        ret: Dict[str, float] = {}
        yms = sorted(closes)
        for i, ym in enumerate(yms):
            if i == 0:
                continue
            prev = closes[yms[i - 1]]
            if prev:
                ret[ym] = closes[ym] / prev - 1.0
        out[code] = ret
    return out


# =============================================================================
# 时间工具
# =============================================================================
def month_ends(start: str, end: str) -> List[date]:
    s = date.fromisoformat(start)
    e = date.fromisoformat(end)
    cur = date(s.year, s.month, 1)
    out = []
    while cur <= e:
        if cur.month == 12:
            nxt = date(cur.year + 1, 1, 1)
        else:
            nxt = date(cur.year, cur.month + 1, 1)
        out.append(nxt - timedelta(days=1))
        cur = nxt
    return out


def first_of_next(d: date) -> date:
    if d.month == 12:
        return date(d.year + 1, 1, 1)
    return date(d.year, d.month + 1, 1)


def _is_cross_border(info: Dict[str, Any]) -> bool:
    c = info.get("index_code", "")
    n = info.get("index_name", "")
    return (c.endswith(".HI") or c.endswith(".GI")
            or any(k in n for k in ("港股", "香港", "恒生", "中韩", "日经", "标普", "纳斯达克")))


# =============================================================================
# 真实特征（point-in-time，禁未来函数）
# =============================================================================
_IDX_MONTHLY: Dict[str, Dict[str, Any]] = {}
_RAW_DAILY: Dict[str, List[List[Any]]] = {}
_MAP: Dict[str, Any] = {}


def _state(chg: float, band: float) -> str:
    if chg > band:
        return "up"
    if chg < -band:
        return "down"
    return "flat"


def real_features(etf: Dict[str, Any], as_of: date) -> Optional[Dict[str, Any]]:
    code = etf["code"]
    info = _MAP.get(code)
    if not info:
        return None
    idx_code = info["index_code"]
    idx = _IDX_MONTHLY.get(idx_code)
    if not idx or "close" not in idx or "pb" not in idx:
        return None
    ym = as_of.strftime("%Y-%m")
    months = sorted(idx["close"].keys())
    if ym not in idx["close"] or months.index(ym) < 1:
        return None
    i = months.index(ym)
    prev_ym = months[i - 1]
    close_now = idx["close"][ym]
    close_prev = idx["close"][prev_ym]
    if not close_prev:
        return None
    price_ret = close_now / close_prev - 1.0

    vol_now = idx["volume"].get(ym)
    vol_prev = idx["volume"].get(prev_ym)
    if vol_now is not None and vol_prev:
        vol_chg = vol_now / vol_prev - 1.0
        volume = _state(vol_chg, VOL_BAND)
    else:
        volume = "flat"
    price = _state(price_ret, PRICE_BAND)

    pb_now = idx["pb"].get(ym)
    if pb_now is None:
        return None

    # point-in-time 日频 PB 序列（仅 ≤ as_of 月份）
    daily = _RAW_DAILY.get(idx_code, [])
    series = [v for (d, v) in daily if d[:7] <= ym]
    if len(series) < 20:
        # 回退：用月度 PB 序列
        series = [idx["pb"][m] for m in months if m <= ym and idx["pb"].get(m) is not None]
    if len(series) < 5:
        return None
    pb_pct = rank_pct(series, pb_now)
    if pb_pct is None:
        return None
    p5 = percentile(series, 5)
    p20 = percentile(series, 20)
    p50 = percentile(series, 50)
    p80 = percentile(series, 80)
    p95 = percentile(series, 95)

    # prev_vp（鱼尾检测）
    prev_vp = ""
    if i >= 2:
        p2 = months[i - 2]
        if idx["close"].get(p2):
            pv = _state(idx["close"][prev_ym] / idx["close"][p2] - 1.0, PRICE_BAND)
        else:
            pv = "flat"
        pvol_now = idx["volume"].get(prev_ym)
        pvol_prev = idx["volume"].get(p2)
        if pvol_now and pvol_prev:
            pvv = _state(pvol_now / pvol_prev - 1.0, VOL_BAND)
        else:
            pvv = "flat"
        prev_vp = f"{pvv}|{pv}"

    xb = _is_cross_border(info)
    is_commodity = (idx_code == "AU9999.SGE")
    # ⚠️ 口径铁律（2026-08-15 用户裁决）：本技能「量价」特指 ETF 所跟踪指数的
    # 【行业发展周期维度】供需量价（行业产销量/核心品价格），绝非该指数自身
    # 的价格与成交量关系。下面 volume/price 由跟踪指数月度 close/volume 月环比
    # 近似得出 —— 属【占位/非行业周期口径】，在接入真实行业周期量价特征前，
    # 回测结果【不得】作为决策级结论（仅作信号引擎管线回归）。
    feat = {
        "sector": etf["name"],
        "as_of": as_of.isoformat(),
        "asset_class": "commodity" if is_commodity else "sector",
        "index_code": idx_code,
        "index_name": info["index_name"],
        "demand": "growth",
        "volume": volume,            # TODO(行业周期): 改接行业产销量/出货量同比
        "price": price,              # TODO(行业周期): 改接行业核心品价格(组件/电芯/ARPU…)
        "forward": "flat",          # 无前瞻盈利数据 → 中性（用户校准项）
        "bottom_signals": 0,        # 无见底6信号数据 → 中性
        "imagination": "B",         # 中性（用户校准项）
        "crowding": False,
        "catalyst": False,
        # —— 行业周期量价硬闸门：声明口径 + 诚实标注当前为跟踪标的涨跌近似占位 ——
        "vp_basis": "industry_cycle",
        "industry_volume_metric": "行业产销量(回测特征占位·待接真实行业周期量价)",
        "industry_price_metric": "行业核心品价格(回测特征占位·现量价以跟踪标的价格变化近似，非行业周期口径)",
        "pb_now": round(pb_now, 4),
        "pb_manual": round(pb_now, 4),                 # 单源简化：回测以信号引擎为核心
        "pb_sources": {"mx": round(pb_now, 4), "mx_cross": round(pb_now, 4)},
        "pb_pct": round(pb_pct, 2),
        "pb_pct_period": "5y",
        "pb_pct_10y": None,
        "pb_series": [round(x, 4) for x in series],
        "pb_series_years": round(len(series) / 244.0, 2),
        "pb_series_freq": "daily",
        "pb_p5": round(p5, 4) if p5 is not None else None,
        "pb_p20": round(p20, 4) if p20 is not None else None,
        "pb_p50": round(p50, 4) if p50 is not None else None,
        "pb_p80": round(p80, 4) if p80 is not None else None,
        "pb_p95": round(p95, 4) if p95 is not None else None,
        "today": as_of.isoformat(),
        "is_cross_border": xb,
        "weighting": "mktcap" if xb else "",
        "prev_vp": prev_vp,
    }
    # ---- B-02（2026-10-25）：与 live 共用同一量化派生入口 --------------------
    # 此前回测**从不**产出 quant_raw，引擎只能走六档表（win_rate_mode="table"）；
    # live 则由 quant_inputs.derive_quant_inputs 从 quant_raw 派生 D1-D5 →
    # 等于「回测/实盘各有一套派生实现」，回测闸门对量化路径完全盲。
    # 现在改为调用**同一函数**（单入口），输入是 point-in-time 月频序列（不含未来月）：
    # 当前可得 D1（动量+分位）与 D2 的量同比；库存/产能利用率/资金流/预期上修在
    # 历史窗口无 point-in-time 数据源 → 由派生根函数显式返回 None（引擎据此走六档表，
    # 与 live 的降级语义一致，不造假覆盖率）。
    # 注：price_series 同时供 P* 的 σ 派生链使用 —— live 早已提供，回测此前缺省，
    # 属同一类 live/backtest 口径分歧，此处一并对齐。
    feat["quant_raw"] = {
        "price_series": [idx["close"][m] for m in months
                         if m <= ym and idx["close"].get(m) is not None],
        "volume_series": [idx["volume"][m] for m in months
                          if m <= ym and idx["volume"].get(m) is not None],
        "price_window_days": 1260,
        "provenance": "backtest/index_monthly(point-in-time 月频，禁未来月)",
    }
    return feat


# =============================================================================
# 回测主流程
# =============================================================================
def backtest(universe: List[Dict[str, Any]], start: str, end: str,
             cost_bps: float, features_fn: Callable, verbose: bool = False) -> Dict[str, Any]:
    G = P["guards"]
    ends = month_ends(start, end)
    n_periods = max(0, len(ends) - 1)
    guards.check_backtest_realism(cost_bps, n_periods, strict=True,
                                  min_cost_bps=G["backtest_min_cost_bps"],
                                  min_periods=G["backtest_min_periods"])

    etf_ret = load_etf_returns()
    nav = 1.0
    bm_nav = 1.0                    # 等权基准 NAV（G1）
    nav_series: List[float] = [nav]
    bm_nav_series: List[float] = [bm_nav]
    breaches = []
    prev_w: Dict[str, float] = {}
    signal_wins: List[int] = []
    per_etf: Dict[str, Dict[str, Any]] = {}
    # B-02（2026-10-25）：胜率路径可见化 —— 记录每条信号的 win_rate_mode 与
    # 量化派生字段数，让「回测走 table 还是 quant」不再隐形（此前报告里查不到）。
    mode_counts: Dict[str, int] = {}
    derived_ns: List[int] = []

    for i, sig_end in enumerate(ends[:-1]):
        exec_day = first_of_next(sig_end)
        guards.assert_t_plus_1(sig_end.isoformat(), exec_day.isoformat())
        pit = guards.filter_pit_universe(universe, exec_day.isoformat(), strict=True)

        raw_w: Dict[str, float] = {}
        for etf in pit:
            feat = features_fn(etf, sig_end)
            if feat is None:
                continue
            try:
                v = evaluate(feat)
            except Exception as e:
                breaches.append(f"{etf['code']}@{sig_end}: evaluate 异常 {e}")
                continue
            if v["status"] != "OK":
                continue
            pos = float(str(v["suggested_position"]).rstrip("%")) / 100.0
            if pos <= 0:
                continue
            raw_w[etf["code"]] = pos
            if v["win_rate"] is not None:
                signal_wins.append(v["win_rate"])
            mode_counts[str(v.get("win_rate_mode") or "na")] = (
                mode_counts.get(str(v.get("win_rate_mode") or "na"), 0) + 1)
            try:
                from quant_inputs import derive_quant_inputs as _dqi
                _dn = sum(1 for x in _dqi(feat.get("quant_raw") or {}).values()
                          if x is not None)
                derived_ns.append(_dn)
            except Exception:
                pass
            # 记录每只 ETF 的信号与实现收益（供分析）
            rec = per_etf.setdefault(etf["code"], {"name": etf["name"], "n": 0,
                                                  "pos_sum": 0.0, "fw_win_sum": 0.0,
                                                  "r1": [], "r3": []})
            rec["n"] += 1
            rec["pos_sum"] += pos
            rec["fw_win_sum"] += (v["win_rate"] or 0)
            exec_ym = exec_day.strftime("%Y-%m")
            # 实现收益：持有 exec_ym 当月 ETF 收益；及往后 3 月累计
            r1 = etf_ret.get(etf["code"], {}).get(exec_ym)
            if r1 is not None:
                rec["r1"].append(r1)
            r3 = _realized_3m(etf_ret.get(etf["code"], {}), exec_ym)
            if r3 is not None:
                rec["r3"].append(r3)

        # 归一：框架建议仓位之和 >100% 则等比缩到 100%，否则按实际（余现金）
        tot = sum(raw_w.values())
        if tot <= 0:
            port_w = {}
        elif tot > 1.0:
            port_w = {k: w / tot for k, w in raw_w.items()}
        else:
            port_w = raw_w

        # ---- 等权基准（G1 · 2026-09-04）：PIT 宇宙内全部已上市且当月有收益的 ETF 等权复利 ----
        bm_ws = {etf["code"]: 1.0 for etf in pit
                 if etf_ret.get(etf["code"], {}).get(exec_day.strftime("%Y-%m")) is not None}
        if bm_ws:
            bm_ret = sum(etf_ret[c].get(exec_day.strftime("%Y-%m"), 0.0) for c in bm_ws) / len(bm_ws)
            bm_nav *= (1.0 + bm_ret)
        bm_nav_series.append(bm_nav)

        if port_w:
            turnover = sum(abs(port_w.get(k, 0) - prev_w.get(k, 0))
                           for k in set(port_w) | set(prev_w))
            cost = turnover * cost_bps / 10000.0
            port_ret = 0.0
            for code, w in port_w.items():
                r = etf_ret.get(code, {}).get(exec_day.strftime("%Y-%m"), 0.0)
                port_ret += w * r
            nav = nav * (1.0 - cost) * (1.0 + port_ret)
        else:
            cost = 0.0
        nav_series.append(nav)
        prev_w = port_w
        if verbose and (port_w or True):
            print(f"  {exec_day} 持仓 {len(port_w)} 成本 {cost*10000:.1f}bps "
                  f"NAV {nav:.4f} 信号框架胜率均 {(_mean(signal_wins[-len(port_w):]) if port_w else 0):.0f}")

    peak = nav_series[0]
    max_dd = 0.0
    for x in nav_series:
        peak = max(peak, x)
        max_dd = max(max_dd, (peak - x) / peak)
    # 等权基准对照（G1）：超额 = 策略终值/基准终值 - 1；基准回撤同口径
    bm_peak = bm_nav_series[0]
    bm_dd = 0.0
    for x in bm_nav_series:
        bm_peak = max(bm_peak, x)
        bm_dd = max(bm_dd, (bm_peak - x) / bm_peak)
    excess = (nav_series[-1] / bm_nav_series[-1] - 1.0) if bm_nav_series[-1] > 0 else 0.0
    win_rate_of_signals = (sum(1 for w in signal_wins if w >= 50) / len(signal_wins)
                           if signal_wins else None)

    # 汇总每只 ETF
    per_etf_out = {}
    for code, rec in per_etf.items():
        n = rec["n"]
        r3 = rec["r3"]
        win3 = (sum(1 for x in r3 if x > 0) / len(r3)) if r3 else None
        per_etf_out[code] = {
            "name": rec["name"], "n_signals": n,
            "avg_position_pct": round(rec["pos_sum"] / n * 100, 1),
            "avg_framework_winrate": round(rec["fw_win_sum"] / n, 1),
            "avg_realized_1m_pct": round(_mean(rec["r1"]) * 100, 2) if rec["r1"] else None,
            "avg_realized_3m_pct": round(_mean(r3) * 100, 2) if r3 else None,
            "realized_3m_win_pct": round(win3 * 100, 1) if win3 is not None else None,
        }

    return {
        "n_periods": n_periods, "cost_bps": cost_bps, "universe_size": len(universe),
        "final_nav": round(nav_series[-1], 4), "max_drawdown": round(max_dd, 4),
        "benchmark_equal_weight": {"final_nav": round(bm_nav_series[-1], 4),
                                   "max_drawdown": round(bm_dd, 4),
                                   "excess_vs_strategy": round(excess, 4),
                                   "note": "基准口径：PIT 宇宙内当月有收益的 ETF 满仓等权复利、无成本、"
                                           "无仓位纪律。本框架是『买点择时+仓位纪律』引擎，"
                                           "现金拖累使其在单边牛市窗口必然跑输满仓基准；"
                                           "对照意义=检验信号是否带来选择 alpha（结合 O5 校准 AUC 判读），"
                                           "而非作为策略收益预期。"},
        "signal_count": len(signal_wins),
        "win_rate_of_signals": (round(win_rate_of_signals, 4) if win_rate_of_signals is not None else None),
        "n_etf_signaled": len(per_etf_out),
        "win_rate_mode_counts": dict(sorted(mode_counts.items(), key=lambda kv: -kv[1])),
        "quant_derived_n_avg": (round(sum(derived_ns) / len(derived_ns), 2)
                                if derived_ns else None),
        "quant_derived_n_max": (max(derived_ns) if derived_ns else None),
        "quant_engine_note": ("B-02：回测与引擎共用 quant_inputs.derive_quant_inputs 单入口；"
                              "历史窗口缺 point-in-time 行业量价/资金流 → D2~D4 显式 None，"
                              "故 win_rate_mode 仍以 table 为主（与 live 降级语义一致）。"),
        "breaches": breaches, "nav_series": [round(x, 4) for x in nav_series],
        "per_etf": per_etf_out,
    }


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _realized_3m(ret_map: Dict[str, float], exec_ym: str) -> Optional[float]:
    """exec_ym 起往后 3 个自然月的累计收益（复利）。"""
    y, m = int(exec_ym[:4]), int(exec_ym[5:7])
    cum = 1.0
    hit = 0
    for _ in range(3):
        key = f"{y:04d}-{m:02d}"
        if key not in ret_map:
            break
        cum *= (1.0 + ret_map[key])
        hit += 1
        if m == 12:
            y, m = y + 1, 1
        else:
            m += 1
    return (cum - 1.0) if hit == 3 else None


def build_universe(input_path: Optional[str]):
    if input_path:
        m = _load_json(input_path)
        return m["universe"], m.get("start", "2019-01-01"), m.get("end", "2026-06-30"), float(m.get("cost_bps", 5.0))
    u = load_universe_from_map()
    return u, "2019-01-01", "2026-06-30", 5.0


def main() -> int:
    global _IDX_MONTHLY, _RAW_DAILY, _MAP
    ap = argparse.ArgumentParser(description="XYD 供需板块框架 真实回测")
    ap.add_argument("--input", help="manifest.json（含 universe/start/end/cost_bps）")
    ap.add_argument("--start", help="回测起点 YYYY-MM-DD")
    ap.add_argument("--end", help="回测终点 YYYY-MM-DD")
    ap.add_argument("--cost", type=float, help="单边成本 bps（>3）")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    universe, start, end, cost_bps = build_universe(a.input)
    if a.start:
        start = a.start
    if a.end:
        end = a.end
    if a.cost:
        cost_bps = a.cost

    # 装载真实数据
    _MAP = _load_json(MAP)
    _IDX_MONTHLY = load_index_monthly()
    _RAW_DAILY = load_raw_daily()

    print("=" * 84)
    print("XYD 供需板块框架 · 真实回测 (Backtest)")
    print("=" * 84)
    print(f"  宇宙={len(universe)} ETF  区间={start}~{end}  成本={cost_bps}bps")
    print(f"  指数月度已载={len(_IDX_MONTHLY)}  指数日频PB已载={len(_RAW_DAILY)}")

    fp_ok, fp = guards.assert_params_frozen(P, LOCK)
    print(f"  参数指纹校验: {'PASS 一致' if fp_ok else 'FAIL 不一致'} ({fp[:12]}…)")
    if not fp_ok:
        print("  [FAIL] 回测拒绝：params.json 与冻结锁不一致")
        return 1

    try:
        r = backtest(universe, start, end, cost_bps, real_features, a.verbose)
    except guards.GuardError as e:
        print(f"  [FAIL] 守卫拦截：{e}")
        return 1

    print(f"\n  期数={r['n_periods']}  成本={r['cost_bps']}bps  宇宙={r['universe_size']}只")
    print(f"  信号次数={r['signal_count']}  触发信号ETF数={r['n_etf_signaled']}")
    print(f"  信号框架胜率(≥50)={r['win_rate_of_signals']}")
    print(f"  胜率路径={r.get('win_rate_mode_counts')}  "
          f"量化派生字段均值={r.get('quant_derived_n_avg')}/8（max={r.get('quant_derived_n_max')}）")
    print("     └ B-02：与引擎共用 quant_inputs 单入口；历史窗口无 point-in-time 行业量价"
          "→ D2~D4 显式 None（table 路径为诚实降级，非静默）")
    tot_ret = r["final_nav"] - 1.0
    print(f"  终值NAV={r['final_nav']}  累计收益={tot_ret:+.2%}  最大回撤={r['max_drawdown']:.2%}")
    print(f"  守卫违例={len(r['breaches'])}")
    if r["breaches"][:5]:
        for b in r["breaches"][:5]:
            print(f"    [BREACH] {b}")

    # 每只 ETF 汇总（按信号次数降序）
    pe = sorted(r["per_etf"].items(), key=lambda kv: -kv[1]["n_signals"])
    print("\n  === 每只 ETF 信号汇总（按信号次数）===")
    print(f"  {'code':8} {'name':14} {'n':>3} {'pos%':>5} {'fwWin':>6} "
          f"{'r1m%':>7} {'r3m%':>7} {'win3m%':>7}")
    for code, rec in pe:
        print(f"  {code:8} {rec['name'][:14]:14} {rec['n_signals']:>3} "
              f"{rec['avg_position_pct']:>5} {rec['avg_framework_winrate']:>6} "
              f"{str(rec['avg_realized_1m_pct']):>7} {str(rec['avg_realized_3m_pct']):>7} "
              f"{str(rec['realized_3m_win_pct']):>7}")

    # 落盘报告
    rep_path = os.path.join(HERE, "data", "backtest_report.json")
    with open(rep_path, "w", encoding="utf-8") as f:
        json.dump(r, f, ensure_ascii=False, indent=1)
    print(f"\n  报告已写 -> {rep_path}")
    print("=" * 84)
    return 1 if r["breaches"] else 0


if __name__ == "__main__":
    sys.exit(main())
