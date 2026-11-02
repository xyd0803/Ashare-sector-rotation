#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
量化输入契约 (O1, 2026-08-16)
=================================================================
把 D1-D5 八字段从「手填 / LLM 判断」改为「从原始序列确定性派生」。

目的：关闭 V1 漏洞——
  旧实现：胜率量化只保证『聚合方式客观』，但上游八字段 D1-D5 由分析师/LLM 手填，
          缺任一即静默回退旧六档定性表，『禁主观』承诺只在输入已量化时成立。
  新契约：
    1. 案例 JSON 须提供 `quant_raw`（带 provenance 的原始序列/指标），禁止直接手填 q_xxx 数值。
    2. derive_quant_inputs(raw) 按固定公式确定性算出 8 个数；
       任一原始输入缺失/不足 → 该字段显式 None（触发引擎六档回退，且标注『已显式降级』而非静默主观）。
    3. 引擎 enforce_quant_contract()：
       - 有 quant_raw → 派生并覆盖 q_xxx，打 trace『D1-D5 量化派生』；
       - 无 quant_raw 但 q_xxx 直接手填 → SHOULD 告警『D1-D5 未经量化派生，可能含主观输入(V1)』；
       - 全 None → 走旧六档表（明确降级，非静默主观）。

设计原则（与 discipline.py 一致）：零自由裁量、纯标准库、全留痕。
本模块不 import discipline（避免循环依赖），分位法本地实现。
"""
from __future__ import annotations
import sys
import os

# ---- Windows 控制台 UTF-8 ----
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


# ----------------------------------------------------------------------------
# 本地分位工具（与 discipline.percentile / rank_pct 同义，避免循环 import）
# ----------------------------------------------------------------------------
def _percentile(vals, q):
    """线性插值百分位（对齐 numpy.percentile 默认）。真实分位法，非 min~max 插值。"""
    if not vals:
        return None
    s = sorted(vals)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (q / 100.0)
    lo, hi = int(__import__("math").floor(k)), int(__import__("math").ceil(k))
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _rank_pct(vals, x):
    if not vals or x is None:
        return None
    return 100.0 * sum(1 for v in vals if v <= x) / len(vals)


def _safe_ratio(a, b):
    if a is None or b in (None, 0):
        return None
    try:
        return (a / b - 1.0) * 100.0
    except Exception:
        return None


# ----------------------------------------------------------------------------
# D1-D5 八字段定义
# ----------------------------------------------------------------------------
QUANT_FIELDS = [
    "q_price_mom6", "q_price_pct", "q_vol_yoy", "q_inv_yoy", "q_util",
    "q_fund_flow_pct", "q_fund_turn_pct", "q_earning_rev",
]


def _trace(trace, rule, section, level, triggered, note, delta=None):
    trace.append({"rule": rule, "section": section, "level": level,
                  "triggered": triggered, "delta": delta, "note": note})


# ----------------------------------------------------------------------------
# 核心：从 quant_raw 确定性派生八字段
# ----------------------------------------------------------------------------
def derive_quant_inputs(raw: dict) -> dict:
    """从 quant_raw 派生 D1-D5 八字段。

    quant_raw 允许字段（均须带 provenance，由数据脚本产出，非 LLM 生成）：
      price_series:       List[float]  行业指数/ETF 收盘价（行业周期口径，非 ETF 交易行为）
      price_window_days:  int=1260     算价格分位的回看窗口（默认 5y≈1260 交易日）
      volume_series:      List[float]  行业产销量/出货量原始序列（可选，给则算同比）
      volume_yoy:         float        已算好的产销量同比% (须 provenance)
      inventory_yoy:      float        库存同比% (负=去库利好)
      utilization:        float        产能利用率%
      fund_flow_pct:      float        ETF 近N月资金净流入%
      fund_turn_pct:      float        成交额趋势% (正=放量)
      earning_rev_pct:    float        一致预期盈利上修占比% (0-100)

    返回 dict(8 个 q_xxx, 全为 Optional[float])；缺失→None（显式，非猜测）。
    """
    out = {k: None for k in QUANT_FIELDS}

    # --- D1 行业价格强度：6 月动量(确定性) + 价格历史分位(确定性) ---
    ps = raw.get("price_series") or []
    if len(ps) >= 2:
        n = min(126, len(ps) - 1)
        mom6 = _safe_ratio(ps[-1], ps[-1 - n])
        out["q_price_mom6"] = round(mom6, 2) if mom6 is not None else None
        win = max(int(raw.get("price_window_days", 1260)), 2)
        window = ps[-win:] if len(ps) >= win else ps
        out["q_price_pct"] = round(_rank_pct(window, ps[-1]), 2) if ps[-1] is not None else None

    # --- D2 产业供需量能 ---
    if raw.get("volume_yoy") is not None:
        out["q_vol_yoy"] = float(raw["volume_yoy"])
    elif raw.get("volume_series"):
        vs = raw["volume_series"]
        if len(vs) >= 13:
            out["q_vol_yoy"] = round(_safe_ratio(vs[-1], vs[-13]) or 0.0, 2)
    if raw.get("inventory_yoy") is not None:
        out["q_inv_yoy"] = float(raw["inventory_yoy"])
    if raw.get("utilization") is not None:
        out["q_util"] = float(raw["utilization"])

    # --- D3 市场资金共识 ---
    if raw.get("fund_flow_pct") is not None:
        out["q_fund_flow_pct"] = float(raw["fund_flow_pct"])
    if raw.get("fund_turn_pct") is not None:
        out["q_fund_turn_pct"] = float(raw["fund_turn_pct"])

    # --- D4 景气预期边际 ---
    if raw.get("earning_rev_pct") is not None:
        out["q_earning_rev"] = float(raw["earning_rev_pct"])

    return out


# ----------------------------------------------------------------------------
# 引擎钩子：在 run() 开头调用，执行契约
# ----------------------------------------------------------------------------
def enforce_quant_contract(inp, warnings, trace) -> bool:
    """返回是否经量化派生成功（True=量化路径；False=未派生）。

    行为：
      - 有 quant_raw → 派生并覆盖 q_xxx，置 inp._quant_derived=True
      - 无 quant_raw 但手填 q_xxx → SHOULD 告警(V1)，标注可能含主观
      - 全 None → 引擎后续走六档表（明确降级）
    """
    raw = getattr(inp, "quant_raw", None)
    if raw:
        derived = derive_quant_inputs(raw)
        n_filled = sum(1 for k in QUANT_FIELDS if derived.get(k) is not None)
        for k in QUANT_FIELDS:
            if derived.get(k) is not None:
                setattr(inp, k, derived[k])
        setattr(inp, "_quant_derived", True)
        _trace(trace, "D1-D5 量化派生", "§12/O1", "MUST", True,
               f"量化输入契约生效：从 quant_raw 派生 {n_filled}/8 字段"
               f"（缺失字段显式 None→六档回退），杜绝 V1 静默主观")
        return True

    manual = [k for k in QUANT_FIELDS if getattr(inp, k, None) is not None]
    if manual:
        warnings.append(
            "[SHOULD][V1] D1-D5 直接手填、未经量化派生(quant_raw)，可能含主观输入；"
            f"建议改由 quant_raw 序列派生。当前手填字段: {manual}")
        _trace(trace, "D1-D5 手填告警", "§12/O1", "SHOULD", True,
               f"无 quant_raw，{len(manual)} 个 q_xxx 手填 → 可能含主观(V1)")
    setattr(inp, "_quant_derived", False)
    return False


# ----------------------------------------------------------------------------
# 自检
# ----------------------------------------------------------------------------
def _selfcheck():
    ok = True
    # 1) 全缺失 → 全 None
    d0 = derive_quant_inputs({})
    assert all(d0[k] is None for k in QUANT_FIELDS), "空 raw 应全 None"
    # 2) 价格序列派生动量+分位
    ps = [100.0 + i for i in range(130)]  # 单调递增，末值最高
    d1 = derive_quant_inputs({"price_series": ps})
    assert d1["q_price_mom6"] is not None and d1["q_price_mom6"] > 0, "动量应>0"
    assert d1["q_price_pct"] is not None and d1["q_price_pct"] >= 99.0, f"单调序列末值分位应≈100，实得 {d1['q_price_pct']}"
    # 3) 同比派生
    d2 = derive_quant_inputs({"volume_series": [10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 10, 20]})
    assert abs(d2["q_vol_yoy"] - 100.0) < 1e-6, f"量同比应=100%，实得 {d2['q_vol_yoy']}"
    # 4) 显式指标透传
    d3 = derive_quant_inputs({"utilization": 78.5, "fund_flow_pct": 12.3, "earning_rev_pct": 64.0})
    assert d3["q_util"] == 78.5 and d3["q_fund_flow_pct"] == 12.3 and d3["q_earning_rev"] == 64.0
    print("quant_inputs selfcheck: OK")
    return ok


if __name__ == "__main__":
    _selfcheck()
