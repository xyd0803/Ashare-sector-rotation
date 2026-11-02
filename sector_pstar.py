#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
sector_pstar.py —— 供需板块框架 · 风险调整买点 P*（2026-09-08）
================================================================
与盈利面技能 `p_star.py` **公式同源、参数独立**（同一份公式，两套参数）：

    P* = [W·mid + (1−W)·bear_eff] / (1 + r)
    r  = clamp(r_base(W) × s_vol(σ_eff), r_min, r_max)
    s_vol = clamp(σ_eff / σ_ref, s_vol_min, s_vol_max)
    σ_eff = σ_ann · √(H / 12)                    # 持有期尺度换算
    bear_eff = max(bear, P·(1 − stop_pct − friction_pct))   # 止损/摩擦走下行，不进 r

数学性质（框架 EV 闭式等价，已数值验证）
----------------------------------------
    EV(P) = [W·mid + (1−W)·bear_eff]/P − 1
    ⇒ P* > P  ⟺  EV(P) > r
即 **P\* 是既有「要求回报率」判据的价格化重述，不是新规则**，不与回测/交易逻辑冲突。

为什么止损不进 r
----------------
r 已由 σ（波动）表达风险；若再把止损幅度加进 r，等于同一份下行风险计两次。
故止损/摩擦只抬高 bear（下行锚）→ 压低概率加权公允价值，语义干净、可审计。

零回归保证
----------
本模块只**新增** verdict.p_star 字段（默认 None），不修改任何既有字段/判据；
σ 缺失且资产类型无默认值 → s_vol=1.0（r 不缩放）。
"""
from __future__ import annotations

import math
import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))

FORMULA = "P* = [W·mid + (1−W)·bear_eff] / (1 + r)"

# ---- 参数（供需/板块口径，**独立于**盈利面 p_star.SIGMA_CFG）----
P_STAR_CFG = {
    "enabled": True,
    # 要求回报率基准档（W 下限, r）—— 与盈利面同构
    "r_bands": [[50.0, 0.10], [40.0, 0.15], [25.0, 0.25], [0.0, 0.40]],
    # σ 缩放
    "sigma_ref_ann": 0.30,          # 板块/ETF 年化波动基准
    "s_vol_min": 0.25,
    "s_vol_max": 2.00,
    "r_min": 0.03,
    "r_max": 0.60,
    "horizon_ref_months": 12.0,     # σ_eff 换算基准；实际 H 由调用方传入（O8 持有期）
    # 资产类别 σ 缺省表（无序列可派生时兜底）
    "asset_sigma_default": {
        "etf": 0.18, "broad": 0.15, "sector": 0.28, "theme": 0.32,
        "commodity": 0.25, "bond": 0.05, "money": 0.01,
    },
    # 止损 / 摩擦（走 bear_eff，不进 r）
    "stop_loss": {"enabled": True, "default_stop_pct": 0.15, "friction_pct": 0.01},
    # 胜率饱和：soft（softplus 平滑，默认）/ hard（硬 clamp）
    "sat_mode": "soft",
    "sat_k": 0.02,                  # softplus 温度（pp 尺度越小越接近硬截断）
    "w_floor": 0.01,                # 概率下界（防 w=0 把 bear 权重顶满/除零退化）
    "w_cap": 0.99,                  # 概率上界（防 w=1 完全忽略下行）
    "trading_days": 252.0,
    "derive_min_points": 20,
}


# =============================================================================
# 工具
# =============================================================================
def _finite(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return v


def _softplus(z: float, k: float) -> float:
    """k·ln(1+e^{z/k})，数值稳定。"""
    if k <= 0:
        return z if z > 0 else 0.0
    t = z / k
    if t > 40.0:
        return z
    return k * math.log1p(math.exp(t))


def _soft_min(x: float, cap: float, k: float) -> float:
    """平滑 min(x, cap)：单调不减、C¹、恒 < min(x,cap)，远离 cap 时≈x。"""
    return cap - _softplus(cap - x, k)


def _soft_max(x: float, floor: float, k: float) -> float:
    """平滑 max(x, floor)：单调不减、C¹、恒 > max(x,floor)，远离 floor 时≈x。"""
    return floor + _softplus(x - floor, k)


def saturate_w(w: float, cfg: dict = None, mode: str = None):
    """胜率饱和（值域 [0,1]）。

    hard → clamp；soft → softplus 平滑（默认）。极端输入（w<0 / w>1）先硬 clamp
    到 [0,1] 再饱和，避免 log/exp 溢出与语义越界；越界情况在 note 中留痕。
    """
    C = dict(P_STAR_CFG)
    if cfg:
        C.update({k: v for k, v in cfg.items() if k in C})
    m = (mode or C.get("sat_mode") or "soft").lower()
    k = float(C.get("sat_k", 0.02))
    floor, cap = float(C["w_floor"]), float(C["w_cap"])
    v = _finite(w)
    note = ""
    if v is None:
        return None, "invalid"
    if v < 0.0:
        v, note = 0.0, "W<0 越界→先硬夹到 0"
    elif v > 1.0:
        v, note = 1.0, "W>1 越界→先硬夹到 1"
    if m == "hard":
        out = max(floor, min(cap, v))
        if out != v:
            note = (note + "；" if note else "") + f"hard 夹到 [{floor},{cap}]"
        return out, (note or "hard")
    out = _soft_min(_soft_max(v, floor, k), cap, k)
    if abs(out - v) > 1e-9:
        note = (note + "；" if note else "") + f"soft 饱和 {v:.4f}→{out:.4f}(k={k})"
    return out, (note or "soft")


def derive_sigma_ann(prices, cfg: dict = None):
    """由日频收盘序列派生年化波动率（对数收益 std × √252）。样本不足/非法 → None。"""
    C = dict(P_STAR_CFG)
    if cfg:
        C.update({k: v for k, v in cfg.items() if k in C})
    if not prices or len(prices) < int(C["derive_min_points"]):
        return None
    rets, prev = [], None
    for p in prices:
        v = _finite(p)
        if v is None or v <= 0:
            continue
        if prev is not None and prev > 0:
            rets.append(math.log(v / prev))
        prev = v
    n = len(rets)
    if n < int(C["derive_min_points"]):
        return None
    mu = sum(rets) / n
    var = sum((x - mu) ** 2 for x in rets) / (n - 1) if n > 1 else 0.0
    return math.sqrt(max(var, 0.0)) * math.sqrt(float(C["trading_days"]))


def resolve_sigma(sigma_ann=None, prices=None, asset_class="sector", cfg: dict = None):
    """σ 解析链：显式入参 > 价格序列派生 > 资产类别缺省表 > None（→s_vol=1.0）。

    返回 (sigma_ann, source)；source ∈ provided / derived / asset_default / missing。
    """
    s = _finite(sigma_ann)
    if s is not None and s >= 0:
        return s, "provided"
    d = derive_sigma_ann(prices, cfg) if prices else None
    if d is not None:
        return d, "derived"
    C = dict(P_STAR_CFG)
    if cfg:
        C.update({k: v for k, v in cfg.items() if k in C})
    tbl = C.get("asset_sigma_default") or {}
    if asset_class in tbl:
        v = _finite(tbl[asset_class])
        if v is not None:
            return v, "asset_default"
    return None, "missing"


def vol_scalar(sigma_ann, horizon_months=None, cfg: dict = None) -> dict:
    """年化波动 → 波动缩放系数 s_vol（含持有期尺度换算）。"""
    C = dict(P_STAR_CFG)
    if cfg:
        C.update({k: v for k, v in cfg.items() if k in C})
    h = _finite(horizon_months)
    if h is None or h <= 0:
        h = float(C["horizon_ref_months"])
    s = _finite(sigma_ann)
    if s is None or s < 0:
        return {"s_vol": 1.0, "sigma_ann": None, "sigma_eff": None,
                "horizon_months": h, "source": "missing",
                "note": "σ 缺失/非法 → s_vol=1.0（r 不缩放，零回归）"}
    sigma_eff = s * math.sqrt(h / float(C["horizon_ref_months"]))
    raw = sigma_eff / float(C["sigma_ref_ann"])
    sv = max(float(C["s_vol_min"]), min(float(C["s_vol_max"]), raw))
    return {"s_vol": sv, "sigma_ann": s, "sigma_eff": sigma_eff,
            "horizon_months": h, "source": "provided",
            "note": f"σ_eff={sigma_eff:.2%}(H={h:g}月)/σ_ref={C['sigma_ref_ann']:.0%}"
                    f" → s_vol={sv:.2f}"
                    + ("（触下限）" if raw < C["s_vol_min"] else
                       "（触上限）" if raw > C["s_vol_max"] else "")}


def required_return(w_pct, sigma_ann=None, horizon_months=None, cfg: dict = None):
    """r = clamp(r_base(W) × s_vol, r_min, r_max)。返回 (r, r_base, vs_dict)。"""
    C = dict(P_STAR_CFG)
    if cfg:
        C.update({k: v for k, v in cfg.items() if k in C})
    w = _finite(w_pct)
    r_base = float(C["r_bands"][-1][1])
    if w is not None:
        for lo, r in C["r_bands"]:
            if w >= float(lo):
                r_base = float(r)
                break
    vs = vol_scalar(sigma_ann, horizon_months, C)
    scaled = r_base * float(vs["s_vol"])
    r = max(float(C["r_min"]), min(float(C["r_max"]), scaled))
    return r, r_base, vs


# =============================================================================
# 主函数
# =============================================================================
def compute_p_star(win_rate, mid, bear, price=None, sigma_ann=None,
                   horizon_months=None, stop_pct=None, friction_pct=None,
                   asset_class="sector", prices=None, cfg: dict = None,
                   sat_mode=None) -> dict:
    """风险调整买点 P*。

    参数
    ----
    win_rate : 百分制胜率（如 62.0）或小数（如 0.62，>1 视为百分制）
    mid/bear : 中性/悲观目标价（同一价格口径；相对价时 base=100）
    price    : 现价（可选；给出「可击球/等回调」结论）
    sigma_ann: 年化波动率（可选；缺失→派生→资产默认→s_vol=1.0）
    horizon_months : 持有期(月)，用于 σ 尺度换算（缺省 12）
    stop_pct / friction_pct : 止损/摩擦（走 bear_eff；None→用 cfg 默认；显式 0 可关闭）
    prices   : 日频价格序列（用于派生 σ）
    """
    C = dict(P_STAR_CFG)
    if cfg:
        C.update({k: v for k, v in cfg.items() if k in C})
    notes = []

    W = _finite(win_rate)
    m = _finite(mid)
    b = _finite(bear)
    p = _finite(price)
    if W is None or m is None or b is None:
        return None
    if W > 1.0:
        W = W / 100.0                      # 百分制 → 小数
    if W > 1.0 or W < 0.0:
        notes.append(f"W={W:.4f} 越界（>1 或 <0）→ 饱和层处理")

    W_sat, sat_note = saturate_w(W, C, sat_mode)
    if sat_note and "soft 饱和" in sat_note or (sat_note and "hard" in sat_note and "夹" in sat_note):
        notes.append(f"胜率饱和：{sat_note}")

    if b <= 0:
        notes.append("悲观价 ≤0（异常）→ 仍按原值计算，结论需复核")
    if m < b:
        notes.append("中性价 < 悲观价（倒挂）→ 三档价口径异常，P* 仅供参考")

    # --- 止损/摩擦 → bear_eff（不进 r）---
    sl = C.get("stop_loss") or {}
    stop = _finite(stop_pct)
    if stop is None and sl.get("enabled", True):
        stop = _finite(sl.get("default_stop_pct"))
    fric = _finite(friction_pct)
    if fric is None:
        fric = _finite(sl.get("friction_pct")) or 0.0
    stop_eff = None
    bear_eff = b
    # 语义：stop_pct 显式 <=0 = 关闭止损修正；None = 用 cfg 默认
    if stop is not None and stop <= 0:
        notes.append("止损修正已显式关闭（stop_pct<=0）")
        stop = None
    if stop is not None and stop > 0 and p is not None and p > 0:
        stop_eff = max(0.0, stop + max(0.0, fric))
        cand = p * (1.0 - stop_eff)
        if cand > bear_eff:
            bear_eff = cand
            notes.append(f"止损修正：bear {b:.4f} → bear_eff {bear_eff:.4f}"
                         f"（止损{stop:.0%}+摩擦{fric:.0%}）")

    # --- r ---
    sig, sig_src = resolve_sigma(sigma_ann, prices, asset_class, C)
    r, r_base, vs = required_return(W * 100.0, sig, horizon_months, C)

    fair = W_sat * m + (1.0 - W_sat) * bear_eff
    ps = fair / (1.0 + r)

    out = {
        "formula": FORMULA,
        "W": round(W * 100.0, 1),
        "W_sat": round(W_sat, 4),
        "sat_mode": (sat_mode or C.get("sat_mode") or "soft"),
        "mid_anchor": round(m, 4),
        "bear_anchor": round(b, 4),
        "bear_eff": round(bear_eff, 4),
        "stop_eff_pct": round(stop_eff * 100.0, 2) if stop_eff is not None else None,
        "weighted_fair_value": round(fair, 4),
        "r_base": r_base,
        "s_vol": vs["s_vol"],
        "sigma_ann": round(sig, 4) if sig is not None else None,
        "sigma_eff": round(vs["sigma_eff"], 4) if vs["sigma_eff"] is not None else None,
        "sigma_source": sig_src,
        "horizon_months": vs["horizon_months"],
        "required_return": r,
        "r_clamped": abs(r - r_base * float(vs["s_vol"])) > 1e-12,
        "p_star": round(ps, 4),
        "notes": notes,
        "vs_note": vs["note"],
    }
    if p is not None and p > 0:
        out["price_cur"] = round(p, 4)
        ev_eff = fair / p - 1.0
        out["ev_eff_pct"] = round(ev_eff * 100.0, 2)      # 与 P* 同口径（含止损修正）
        out["p_star_vs_price_pct"] = round((ps / p - 1.0) * 100.0, 2)
        if ps >= p:
            out["verdict"] = "现价 ≤ P*：可击球"
            out["safety_margin_pct"] = round((ps / p - 1.0) * 100.0, 2)
        else:
            out["verdict"] = "现价 > P*：等回调"
            out["need_drop_pct"] = round((p - ps) / p * 100.0, 2)
        # 等价性自检（内联，便于审计）：P*>P ⟺ EV_eff>r
        equiv = (ps > p) == (ev_eff > r)
        out["equivalence_ok"] = bool(equiv)
    return out


def from_verdict(ev: dict, win_rate, price_now=None, **kw):
    """便捷入口：由 discipline 的 ev 结果 + 胜率直接算 P*。

    ev 需含 tiered_prices{mid,bear}（相对价时 base=100 → price 缺省 100）。
    """
    if not ev:
        return None
    tp = ev.get("tiered_prices") or {}
    mid, bear = tp.get("mid"), tp.get("bear")
    price = price_now if price_now is not None else tp.get("base")
    if price is None:
        price = 100.0
    horizon = kw.pop("horizon_months", None)
    if horizon is None:
        horizon = ev.get("horizon_months")
    return compute_p_star(win_rate, mid, bear, price=price,
                          horizon_months=horizon, **kw)


# =============================================================================
# 自检
# =============================================================================
def _selfcheck() -> int:
    print("== sector_pstar 自检 ==")
    fails = []

    def ck(cond, msg):
        print(f"  [{'PASS' if cond else 'FAIL'}] {msg}")
        if not cond:
            fails.append(msg)

    # 1) 公式/等价性：P*>P ⟺ EV_eff>r（多组遍历）
    eq_ok = True
    for w in (0, 5, 20, 40, 50, 62, 80, 95, 100):
        for sg in (None, 0.0, 0.16, 0.30, 0.60, 1.5):
            for h in (6, 12, 24, 36):
                r = compute_p_star(w, 120.0, 80.0, price=100.0,
                                   sigma_ann=sg, horizon_months=h)
                if r is None or not r.get("equivalence_ok", False):
                    eq_ok = False
    ck(eq_ok, "等价性 P*>P ⟺ EV_eff>r（9W×6σ×4H=216 组）")

    # 2) 边界退化
    b1 = compute_p_star(60, 120.0, 80.0, price=100.0, sigma_ann=0.0)
    ck(b1["required_return"] == P_STAR_CFG["r_min"], f"σ=0 → r 触下限 {P_STAR_CFG['r_min']:.0%}")
    b2 = compute_p_star(20, 120.0, 80.0, price=100.0, sigma_ann=5.0)
    ck(b2["required_return"] == P_STAR_CFG["r_max"], f"低W+极端σ → r 触上限 {P_STAR_CFG['r_max']:.0%}")
    b3 = compute_p_star(60, 120.0, 80.0, price=100.0, sigma_ann=None,
                        prices=[], asset_class="unknown_asset")
    ck(b3["s_vol"] == 1.0 and b3["sigma_source"] == "missing",
       "σ 全缺失（无入参/无序列/无资产默认）→ s_vol=1.0 零回归")
    b4 = compute_p_star(0, 120.0, 80.0, price=100.0)
    b5 = compute_p_star(100, 120.0, 80.0, price=100.0)
    ck(b4 is not None and b5 is not None and b5["p_star"] > b4["p_star"],
       "W=0 / W=100 均不崩且 P*(W=100)>P*(W=0)")
    b6 = compute_p_star(-20, 120.0, 80.0, price=100.0)
    b7 = compute_p_star(180, 120.0, 80.0, price=100.0)
    ck(b6 is not None and b7 is not None and b6["W_sat"] <= b7["W_sat"],
       "分数越界（-20 / 180）不崩、饱和后仍单调")
    b8 = compute_p_star(60, 120.0, 80.0, price=0)
    ck(b8 is not None and "verdict" not in b8, "现价=0 → 不产生除零结论（仅出 P*）")
    b9 = compute_p_star(60, None, 80.0, price=100.0)
    ck(b9 is None, "mid 缺失 → 返回 None（不臆造）")

    # 3) 单调性
    prev, mono_w = -1e9, True
    for w in range(0, 101, 5):
        v = compute_p_star(w, 120.0, 80.0, price=100.0)
        if v["p_star"] < prev - 1e-9:
            mono_w = False
        prev = v["p_star"]
    ck(mono_w, "单调：W↑ → P*↑")
    prev, mono_s = 1e9, True
    for sg in (0.0, 0.1, 0.2, 0.3, 0.5, 0.8, 1.2):
        v = compute_p_star(60, 120.0, 80.0, price=100.0, sigma_ann=sg)
        if v["p_star"] > prev + 1e-9:
            mono_s = False
        prev = v["p_star"]
    ck(mono_s, "单调：σ↑ → P*↓")
    prev, mono_m = -1e9, True
    for md in (100, 110, 120, 140, 180):
        v = compute_p_star(60, md, 80.0, price=100.0)
        if v["p_star"] < prev - 1e-9:
            mono_m = False
        prev = v["p_star"]
    ck(mono_m, "单调：mid↑ → P*↑")

    # 4) 饱和：soft vs hard；正常区间几乎无偏移，极端才生效
    s90 = compute_p_star(90, 120.0, 80.0, price=100.0)
    s100 = compute_p_star(100, 120.0, 80.0, price=100.0)
    h100 = compute_p_star(100, 120.0, 80.0, price=100.0, sat_mode="hard")
    ck(abs(s90["W_sat"] - 0.90) < 0.001, f"soft 饱和在常规 W=90 几乎无偏移（{s90['W_sat']}）")
    ck(s100["W_sat"] < h100["W_sat"] + 1e-9 and s100["W_sat"] < 1.0,
       f"饱和上界生效：W=100 soft={s100['W_sat']} < hard={h100['W_sat']}")

    # 5) 止损走 bear_eff 而不进 r
    n0 = compute_p_star(60, 120.0, 80.0, price=100.0, stop_pct=0.0, friction_pct=0.0)
    n1 = compute_p_star(60, 120.0, 80.0, price=100.0, stop_pct=0.15, friction_pct=0.01)
    ck(n0["required_return"] == n1["required_return"] and n1["bear_eff"] > n0["bear_eff"],
       "止损只抬 bear_eff、不改 r（避免与 σ 重复计入）")

    # 6) 幂等
    a1 = compute_p_star(62, 120.0, 80.0, price=100.0, sigma_ann=0.24, horizon_months=24)
    a2 = compute_p_star(62, 120.0, 80.0, price=100.0, sigma_ann=0.24, horizon_months=24)
    ck(a1 == a2, "幂等：同输入两次结果一致")

    # 7) 派生 σ
    import random
    random.seed(7)
    px = [100.0]
    for _ in range(250):
        px.append(px[-1] * math.exp(random.gauss(0.0004, 0.02)))
    sd = derive_sigma_ann(px)
    ck(sd is not None and 0.20 < sd < 0.42, f"序列派生 σ 合理（{sd:.2%}，真值≈32%）")
    ck(derive_sigma_ann([100.0] * 30) == 0.0, "常量序列 σ=0")
    ck(derive_sigma_ann([100.0] * 5) is None, "样本不足 → None")

    # 8) 示例（证券样例复现：W=90 / σ28% / H=24）
    ex = compute_p_star(90, 120.0, 100.0, price=100.0, sigma_ann=0.28, horizon_months=24)
    print(f"  示例 W=90 σ=28% H=24：r={ex['required_return']:.2%} P*={ex['p_star']:.2f} "
          f"{ex.get('verdict','')} (EV_eff={ex.get('ev_eff_pct')}%)")
    ex2 = compute_p_star(90, 120.0, 100.0, price=100.0, sigma_ann=0.16, horizon_months=24)
    print(f"  示例 W=90 σ=16%(ETF) H=24：r={ex2['required_return']:.2%} P*={ex2['p_star']:.2f} "
          f"{ex2.get('verdict','')}")

    print(f"\n自检：{'PASS' if not fails else 'FAIL ' + str(fails)}")
    return 0 if not fails else 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("--selfcheck", "selfcheck"):
        sys.exit(_selfcheck())
    import json as _j
    demo = compute_p_star(62, 120.0, 80.0, price=100.0, sigma_ann=0.24, horizon_months=24)
    print(_j.dumps(demo, ensure_ascii=False, indent=2))
