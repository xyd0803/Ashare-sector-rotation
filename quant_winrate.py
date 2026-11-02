#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XYD 供需板块框架 —— 五维加权胜率量化模块 (Quant WinRate)
========================================================
把「胜率」从「量价六档查表 + 白名单修正」升级为「五维客观打分 → 确定性综合分 → 历史校准胜率」。

设计原则（对齐用户 2026-08-15 诉求：不要凭大模型主观判断）：
  1. 五维打分 D1~D5 全部由【客观指标】计算（0-100），公式与阈值写在 params.json[win_quant]，指纹冻结。
  2. 行业适配代理用【固定映射表】INDUSTRY_PROXY（周期看库存/开工率、科技看订单/出货量、消费看动销/社零），
     不是 AI 临时选（防主观自由裁量）。
  3. 综合分 S = Σ w_i·D_i 是确定性线性组合。
  4. 胜率 W = g(S)，g 为分段映射；**2026-08-16 已完成 54 ETF 回测校准（V4/O5）**：
     用 proxy 行业量价特征派生 S 并对齐 3/6/12 月实现收益拟合，结果 AUC 全在 0.50±0.03（≈随机）、
     corr(S,收益)≈0.03~0.07（近零）、经验 map 全退化成单值平线 → 确认 proxy 特征无真实预测力，
     **拒绝部署经验 map，保留下方保守占位 g(S)**（floor 20 / cap 80）。待真实 D1~D5 接入后重校。
  5. 链式可追溯：每个分、S、W 都留 trace，同样的输入永远同样的输出。

五维与权重（用户给定）：
  D1 行业价格强度 42%  —— 供需最终直接体现（行业核心品价格动量 + 历史分位）
  D2 产业供需量能 27%  —— 量确认价（产销量/出货同比 + 库存去化 + 产能利用率）
  D3 市场资金共识 13%  —— 二级市场筹码供需（ETF 资金净流入 + 成交额趋势）
  D4 景气预期边际 10%  —— 前瞻信号（一致预期盈利上修占比）
  D5 趋势结构质量  8%  —— 过滤诱多/诱空（均线多头 + 突破有效 - 量价背离）

量化输入契约（quant dict，原始指标，单位见注释）：
  price_mom6     行业核心品价格近6月涨跌幅(%)            [行业周期口径·合规]
  price_pct      行业核心品价格在历史序列中的分位(0-100) [行业周期口径·合规]
  vol_yoy        行业产销量/出货/装机量同比(%)           [行业周期口径·合规]
  inv_yoy        行业库存同比(%)  (负=去库，利好)
  util           产能利用率/开工率(%)
  fund_flow_pct  ETF 资金净流入近N月(%)  (可负)
  fund_turn_pct  成交额趋势(%)  (可负；正=放量)
  earning_rev    一致预期盈利上修占比(%)  (0-100，负=下修主导)
  prices         (可选) 价格/指数序列 list[float]，用于自动计算技术面 D5；
                缺则须显式给 ma_score/breakout_score/divergence_penalty
  ma_score / breakout_score / divergence_penalty  (可选, 0-100；给了就不依赖 prices)
"""
from __future__ import annotations
import sys, os
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))

# ---- 默认参数（与 params.json[win_quant] 同步；改须 bump version + 更新指纹锁）----
DEFAULT_QUANT = {
    "weights": {"D1": 0.42, "D2": 0.27, "D3": 0.13, "D4": 0.10, "D5": 0.08},
    # D1 价格强度：动量权重0.6 + 分位权重0.4
    "d1": {"mom_w": 0.6, "pct_w": 0.4,
           "mom_score": [[-30, 0], [-10, 25], [0, 50], [10, 75], [20, 100]]},
    # D2 量能：量45% + 库存30% + 产能利用率25%（库存同比为负=去库→高分）
    "d2": {"vol_w": 0.45, "inv_w": 0.30, "util_w": 0.25,
           "vol_score": [[-20, 0], [0, 50], [10, 70], [30, 100]],
           "inv_score": [[20, 0], [5, 30], [0, 55], [-10, 80], [-25, 100]],
           "util_score": [[60, 30], [70, 50], [80, 70], [90, 90], [95, 100]]},
    # D3 资金共识：净流入50% + 成交额趋势50%
    "d3": {"flow_map": [[-20, 0], [-5, 30], [0, 50], [10, 75], [25, 100]],
           "turn_map": [[-20, 0], [-5, 30], [0, 50], [10, 75], [25, 100]]},
    # D5 结构：均线50% + 突破30% + (100-背离)20%
    "d5": {"ma_w": 0.5, "break_w": 0.3, "div_w": 0.2},
    # 胜率映射 g(S)：保守占位（**2026-08-16 V4/O5 校准后保留**）。
    # 54 ETF 回测校准显示 proxy 特征 S 对 3/6/12 月收益 AUC≈0.50、corr≈0.03~0.07，无区分度，
    # 经验 map 退化成平线，故不部署经验值、沿用此保守分段（floor 20 / cap 80）。
    # 设计：地板 20%（最差也有个底）、天花板 80%（再强也不许虚报 90%+）
    "win_map": [[0, 20], [25, 35], [40, 48], [55, 60], [70, 70], [85, 80]],
    # g(S) 映射形态（2026-09-08 凸映射扩展）：
    #   linear = 分段线性（= 旧 _lerp，逐位一致，零回归）
    #   convex = **段内凸**（t^gamma）：段内曲线位于弦下方 → 除锚点外胜率一律不高估，
    #            锚点值不变、单调不减；节点处斜率由 γΔ/Δx 突降到 0（凹折点）。
    # ⚠️ 数学硬约束：本锚点集各段弦斜率 0.60→0.867→0.80→0.667→0.667 非单调（S 型），
    #    故「过锚点」与「全局凸」不可兼得 —— 框架所谓凸 = 段内凸，非全局凸。
    "win_map_mode": "convex",
    "win_map_gamma": 1.5,
    # V4 置信治理（2026-09-01）：g(S) 未经验证（validated=false）时，有效胜率向基准 50% 收缩，
    # 避免把无预测力的占位刻度当精确胜率驱动仓位。真实 D1-D5 接入且 calibrate_winrate
    # 验证 AUC>0.55 后，置 validated=true + shrink=1.0 解除收缩。
    "shrink": 0.5,
    "validated": False,
}

# ---- 行业适配代理表（规则化，非 AI 临时选）----
# 不同行业用不同「量能代理指标集」喂给同一个 D2 公式，避免用错代理导致误判。
INDUSTRY_PROXY = {
    # 周期品：重点看库存、开工率
    "煤炭": {"vol": "产量同比", "inv": "全社会库存同比", "util": "开工率"},
    "化工": {"vol": "产量同比", "inv": "社会库存同比", "util": "开工率"},
    "有色": {"vol": "产量同比/开工率", "inv": "交易所库存同比", "util": "冶炼开工率"},
    "钢铁": {"vol": "粗钢产量同比", "inv": "钢材社会库存同比", "util": "高炉开工率"},
    "石油": {"vol": "原油加工量同比", "inv": "商业库存同比", "util": "炼厂开工率"},
    "航运": {"vol": "运价指数同比", "inv": "闲置运力占比", "util": "运力利用率"},
    "工程机械": {"vol": "挖机销量同比", "inv": "渠道库存同比", "util": "产能利用率"},
    # 科技：重点看订单、出货量
    "半导体": {"vol": "晶圆出货量同比/订单", "inv": "渠道库存同比", "util": "产能利用率"},
    "电池": {"vol": "动力电池装车量+储能出货同比", "inv": "电芯库存同比", "util": "产能利用率"},
    "光伏": {"vol": "全球新增装机量同比", "inv": "组件库存同比", "util": "硅料产能利用率"},
    "消费电子": {"vol": "出货量与货量同比", "inv": "渠道库存同比", "util": "产能利用率"},
    "通信设备": {"vol": "基站/设备出货同比", "inv": "渠道库存同比", "util": "产能利用率"},
    # 消费：重点看终端动销、社零
    "白酒": {"vol": "动销同比/批价量", "inv": "渠道库存同比", "util": "-"},
    "食品饮料": {"vol": "终端动销同比", "inv": "渠道库存同比", "util": "-"},
    "家电": {"vol": "零售量同比/社零", "inv": "渠道库存同比", "util": "-"},
    "畜牧": {"vol": "出栏量同比", "inv": "冻品库存同比", "util": "-"},
    "零售": {"vol": "社零同比", "inv": "渠道库存同比", "util": "-"},
    # 金融：看业务量
    "证券": {"vol": "A股日均成交额同比(行业需求体量)", "inv": "-", "util": "-"},
    "银行": {"vol": "社融/信贷同比", "inv": "-", "util": "-"},
    "保险": {"vol": "保费收入同比", "inv": "-", "util": "-"},
    # 医药/成长
    "医药": {"vol": "终端消费/院端量同比", "inv": "渠道库存同比", "util": "-"},
    "创新药": {"vol": "临床/授权交易活跃度", "inv": "-", "util": "-"},
    "_default": {"vol": "行业产销量/出货量同比", "inv": "行业库存同比", "util": "产能利用率/开工率"},
}


def _load_params() -> dict:
    p = os.path.join(HERE, "params.json")
    if os.path.exists(p):
        try:
            import json
            with open(p, "r", encoding="utf-8") as f:
                Q = json.load(f).get("win_quant")
            if Q:
                return Q
        except Exception:
            pass
    return DEFAULT_QUANT


def _lerp(x: float, table) -> float:
    """分段线性映射（table: list[[x,y]]，x 顺序无关，内部按 x 升序）。越界取端点。"""
    if x is None:
        return 50.0
    t = sorted(table, key=lambda p: p[0])  # 防御：无论输入升/降序都正确
    if x <= t[0][0]:
        return float(t[0][1])
    if x >= t[-1][0]:
        return float(t[-1][1])
    for i in range(len(t) - 1):
        x0, y0 = t[i]
        x1, y1 = t[i + 1]
        if x0 <= x <= x1:
            return float(y0 + (y1 - y0) * (x - x0) / (x1 - x0))
    return float(t[-1][1])


def g_score(x: float, table, mode: str = "linear", gamma: float = 1.5) -> float:
    """分数 → 胜率映射 g(S)（2026-09-08 凸映射扩展）。

    mode="linear"：与 `_lerp` **逐位一致**（旧行为零回归通道）。
    mode="convex"：段内凸 t^gamma——段内取值 y0+(y1−y0)·t^γ。
        · γ>1 时曲线位于弦**下方** → 除锚点外胜率一律 ≤ 线性（更保守，防高估）；
        · 锚点处 Δ=0（严格过锚点）；单调不减；段内二阶导>0（凸）；
        · 节点处斜率由 γΔ/Δx 突降到 0 → 节点是**凹折点**（S 型锚点集下全局凸不可达）。

    边界与异常：
        · x 为 None → 50.0（与 _lerp 同）；
        · 越界取端点；表长度<2 → 端点值；
        · 段宽为 0（x1==x0）→ 取该段 y0，防除零；
        · γ 非法/≤0 → 退化 linear（不抛异常）。
    """
    if x is None:
        return 50.0
    t = sorted(table, key=lambda p: p[0])          # 防御：输入升/降序都正确
    if len(t) < 2:
        return float(t[0][1]) if t else 50.0
    if x <= t[0][0]:
        return float(t[0][1])
    if x >= t[-1][0]:
        return float(t[-1][1])
    try:
        g = float(gamma)
    except (TypeError, ValueError):
        g = 1.0
    m = (mode or "linear").lower()
    for i in range(len(t) - 1):
        x0, y0 = t[i]
        x1, y1 = t[i + 1]
        if x0 <= x <= x1:
            if x1 == x0:
                return float(y0)
            frac = (x - x0) / (x1 - x0)
            if m == "convex" and g > 0:
                return float(y0 + (y1 - y0) * (frac ** g))
            return float(y0 + (y1 - y0) * frac)
    return float(t[-1][1])


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def _tech_scores(prices: list) -> tuple:
    """由价格序列自动算技术面 D5 三要素（均线多头 / 突破有效 / 背离惩罚）。非主观规则。"""
    if not prices or len(prices) < 20:
        return 50.0, 50.0, 0.0
    cur = prices[-1]
    n = len(prices)
    ma = lambda k: (sum(prices[-k:]) / k) if n >= k else cur
    ma5, ma20, ma60 = ma(5), ma(20), ma(60)
    if cur >= ma5 >= ma20 >= ma60:
        ma_score = 90.0
    elif cur >= ma20:
        ma_score = 65.0
    elif cur >= ma60:
        ma_score = 50.0
    else:
        ma_score = 25.0
    hi = max(prices[-60:]) if n >= 60 else max(prices)
    dist = (hi - cur) / hi * 100.0 if hi else 0.0
    if dist < 5:
        breakout = 85.0
    elif dist < 15:
        breakout = 65.0
    elif dist < 30:
        breakout = 45.0
    else:
        breakout = 25.0
    divergence = 30.0 if (dist < 10 and ma5 < ma20) else 0.0
    return ma_score, breakout, divergence


def compute_quant(quant: dict, Q: dict = None) -> dict:
    """五维打分 → S → W。确定性，可复现。quant 缺字段时按中性 50 处理并标注。"""
    Q = Q or _load_params()
    trace = []
    miss = []

    # ---- D1 行业价格强度 ----
    mom6 = quant.get("price_mom6")
    pct = quant.get("price_pct")
    if mom6 is None or pct is None:
        miss.append("D1")
    d1m = _lerp(mom6, Q["d1"]["mom_score"])
    d1 = _clamp(Q["d1"]["mom_w"] * d1m + Q["d1"]["pct_w"] * (pct or 50))
    trace.append({"dim": "D1价格强度", "mom6": mom6, "pct": pct,
                  "mom_score": round(d1m, 1), "D1": round(d1, 1)})

    # ---- D2 产业供需量能 ----
    vol = quant.get("vol_yoy")
    inv = quant.get("inv_yoy")
    util = quant.get("util")
    if vol is None or inv is None or util is None:
        miss.append("D2")
    d2 = _clamp(Q["d2"]["vol_w"] * _lerp(vol, Q["d2"]["vol_score"])
               + Q["d2"]["inv_w"] * _lerp(inv, Q["d2"]["inv_score"])
               + Q["d2"]["util_w"] * _lerp(util, Q["d2"]["util_score"]))
    trace.append({"dim": "D2量能", "vol_yoy": vol, "inv_yoy": inv, "util": util, "D2": round(d2, 1)})

    # ---- D3 市场资金共识 ----
    ff = quant.get("fund_flow_pct")
    ft = quant.get("fund_turn_pct")
    if ff is None or ft is None:
        miss.append("D3")
    d3 = _clamp(0.5 * _lerp(ff, Q["d3"]["flow_map"]) + 0.5 * _lerp(ft, Q["d3"]["turn_map"]))
    trace.append({"dim": "D3资金共识", "flow_pct": ff, "turn_pct": ft, "D3": round(d3, 1)})

    # ---- D4 景气预期边际 ----
    rev = quant.get("earning_rev")
    if rev is None:
        miss.append("D4")
    d4 = _clamp(rev if rev is not None else 50)
    trace.append({"dim": "D4预期边际", "earning_rev": rev, "D4": round(d4, 1)})

    # ---- D5 趋势结构质量 ----
    if "ma_score" in quant and "breakout_score" in quant and "divergence_penalty" in quant:
        ma_s, br_s, dv_p = quant["ma_score"], quant["breakout_score"], quant["divergence_penalty"]
    else:
        prices = quant.get("prices")
        ma_s, br_s, dv_p = _tech_scores(prices)
        if prices is None:
            miss.append("D5")
    d5 = _clamp(Q["d5"]["ma_w"] * ma_s + Q["d5"]["break_w"] * br_s
               + Q["d5"]["div_w"] * (100 - dv_p))
    trace.append({"dim": "D5结构质量", "ma": round(ma_s, 1), "breakout": round(br_s, 1),
                  "divergence": round(dv_p, 1), "D5": round(d5, 1)})

    # ---- 综合分 S ----
    w = Q["weights"]
    S = w["D1"] * d1 + w["D2"] * d2 + w["D3"] * d3 + w["D4"] * d4 + w["D5"] * d5
    S = round(S, 1)

    # ---- 胜率映射 g(S) ----
    # 2026-09-08：默认走段内凸（mode=convex, γ=1.5），linear 通道保留且逐位等于旧值。
    _mode = str(Q.get("win_map_mode", "linear") or "linear")
    _gamma = Q.get("win_map_gamma", 1.5)
    W = g_score(S, Q["win_map"], mode=_mode, gamma=_gamma)
    W = int(round(W))

    # ---- V4 置信治理（2026-09-01）：未经验证的占位 g(S) 向基准 50% 收缩 ----
    shrink = float(Q.get("shrink", 1.0))
    validated = bool(Q.get("validated", False))
    if validated:
        W_eff = W
        confidence = "VALIDATED"
    else:
        # W_eff = 50 + (W-50)*shrink：把无信号占位的极端值拉回基准，防过度自信
        W_eff = int(round(50.0 + (W - 50.0) * shrink))
        confidence = "LOW"  # proxy 特征无预测力（2026-08-16 校准确认）→ 低置信

    # ---- 映射到六档标签（兼容现有 render_text）----
    if W >= 75:
        label = "高胜率区"
    elif W >= 60:
        label = "偏强"
    elif W >= 45:
        label = "中性"
    elif W >= 30:
        label = "偏弱"
    else:
        label = "弱"

    return {
        "D1_price": round(d1, 1), "D2_volume": round(d2, 1), "D3_fund": round(d3, 1),
        "D4_expect": round(d4, 1), "D5_structure": round(d5, 1),
        "S": S, "win_rate": W, "win_rate_eff": W_eff, "win_label": f"{W}%", "tier": label,
        "confidence": confidence, "map_validated": validated, "shrink": shrink,
        "missing_dims": miss, "trace": trace,
        "calibration": "V4/O5 已校准（2026-08-16）：54 ETF 回测 AUC≈0.50、corr(S,收益)≈0.03~0.07，proxy 特征无预测力→保留保守占位 g(S)（floor20/cap80）；V4 置信治理(2026-09-01)：未验证→W_eff=50+(W-50)*shrink(0.5) 收缩、标 LOW 置信，纪律重心转 EV三档+硬否决+分散；真实 D1~D5 接入且 calibrate_winrate 验证 AUC>0.55 后解除收缩",
    }


# =============================================================================
# 自检
# =============================================================================
_SELFTEST = [
    # (名称, quant输入, 期望 W 区间)
    ("证券双升", {"price_mom6": 18, "price_pct": 12, "vol_yoy": 35, "inv_yoy": -15,
                  "util": 88, "fund_flow_pct": 22, "fund_turn_pct": 18, "earning_rev": 70}, (70, 90)),
    ("光伏价跌量升(cost_deflation)", {"price_mom6": -8, "price_pct": 40, "vol_yoy": 25,
                  "inv_yoy": 5, "util": 75, "fund_flow_pct": 0, "fund_turn_pct": 2, "earning_rev": 40}, (40, 60)),
    ("消费低迷", {"price_mom6": -5, "price_pct": 35, "vol_yoy": -3, "inv_yoy": 10,
                  "util": 70, "fund_flow_pct": -8, "fund_turn_pct": -5, "earning_rev": 35}, (25, 55)),
    ("周期顶部", {"price_mom6": 30, "price_pct": 92, "vol_yoy": 5, "inv_yoy": 25,
                  "util": 95, "fund_flow_pct": 10, "fund_turn_pct": 8, "earning_rev": 55}, (55, 75)),
]


def _selfcheck() -> int:
    print("== quant_winrate 自检 ==")
    fails = 0
    for name, q, (lo, hi) in _SELFTEST:
        r = compute_quant(q)
        ok = (lo <= r["win_rate"] <= hi)
        if not ok:
            fails += 1
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:<28} S={r['S']:<5} W={r['win_rate']}% "
              f"(期望 {lo}-{hi})  D1={r['D1_price']} D2={r['D2_volume']} D3={r['D3_fund']} "
              f"D4={r['D4_expect']} D5={r['D5_structure']}")
    # 单调性：S 越高 W 不下降
    mono_ok = True
    prev = -1
    for s in range(0, 101, 5):
        w = _lerp(s, DEFAULT_QUANT["win_map"])
        if w < prev - 1e-6:
            mono_ok = False
        prev = w
    print(f"  单调性 g(S) 非递减: {'PASS' if mono_ok else 'FAIL'}")
    if not mono_ok:
        fails += 1

    # ---- 凸映射 g_score（2026-09-08 扩展）----
    tbl = DEFAULT_QUANT["win_map"]
    gm = DEFAULT_QUANT.get("win_map_gamma", 1.5)
    ok_lin = all(abs(g_score(s, tbl, "linear") - _lerp(s, tbl)) < 1e-12
                 for s in range(-10, 111, 3))
    print(f"  linear 通道与旧 _lerp 逐位一致: {'PASS' if ok_lin else 'FAIL'}")
    ok_anchor = all(abs(g_score(a, tbl, "convex", gm) - g_score(a, tbl, "linear")) < 1e-12
                    for a, _ in tbl)
    print(f"  凸映射严格过锚点: {'PASS' if ok_anchor else 'FAIL'}")
    prev, mono_c = -1e9, True
    for s in range(-10, 111):
        v = g_score(s, tbl, "convex", gm)
        if v < prev - 1e-9:
            mono_c = False
        prev = v
    print(f"  凸映射单调不减: {'PASS' if mono_c else 'FAIL'}")
    ok_cons = all(g_score(s, tbl, "convex", gm) <= g_score(s, tbl, "linear") + 1e-12
                  for s in range(0, 101))
    print(f"  凸映射段内≤线性（保守不虚高）: {'PASS' if ok_cons else 'FAIL'}")
    ok_b = (g_score(-999, tbl, "convex", gm) == 20.0
            and g_score(999, tbl, "convex", gm) == 80.0
            and g_score(None, tbl, "convex", gm) == 50.0)
    print(f"  越界/None 取端点: {'PASS' if ok_b else 'FAIL'}")
    ok_deg = (abs(g_score(30, tbl, "convex", 0) - _lerp(30, tbl)) < 1e-12
              and abs(g_score(30, tbl, "convex", None) - _lerp(30, tbl)) < 1e-12)
    print(f"  γ非法(≤0/None)退化 linear: {'PASS' if ok_deg else 'FAIL'}")
    ok_zw = g_score(1, [[1, 50], [1, 60], [5, 70]], "convex", gm) == 50.0
    print(f"  零段宽防除零: {'PASS' if ok_zw else 'FAIL'}")
    dmax = max(g_score(s, tbl, "linear") - g_score(s, tbl, "convex", gm)
               for s in range(0, 101))
    print(f"  凸 vs 线性 最大偏差: -{dmax:.2f}pp（保守侧，锚点处 0）")
    for ok in (ok_lin, ok_anchor, mono_c, ok_cons, ok_b, ok_deg, ok_zw):
        if not ok:
            fails += 1

    print(f"\n自检：{'PASS' if fails == 0 else 'FAIL'}"
          + ("" if fails == 0 else f"（{fails} 例失败）"))
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("--selfcheck", "selfcheck"):
        sys.exit(_selfcheck())
    # 默认打印示例
    import json as _j
    r = compute_quant(_SELFTEST[0][1])
    print(_j.dumps(r, ensure_ascii=False, indent=2))
