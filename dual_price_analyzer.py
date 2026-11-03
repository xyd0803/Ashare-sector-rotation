# -*- coding: utf-8 -*-
"""
dual_price_analyzer.py — 双价格维度量价分析模块（出厂价/官方价 vs 市场成交价）

========================================================================
解决的问题
------------------------------------------------------------------------
在个股（如茅台）或行业 ETF 的量价分析中，"出厂价/官方定价/NAV" 与
"市场实际成交价/批价/市价" 是**两个不同维度的价格**。若混为一谈，
会出现典型误判：

    · 出厂价上行（公司主动提价，黏性强、滞后）
    + 市场成交价下行（终端需求弱、渠道去库存）
  → 若只看"价格涨了"会误读为「量缩价升」（看似稀缺看涨），
    实为「价格传导失效」（渠道承压、倒挂风险）。

本模块职责
------------------------------------------------------------------------
1. 输入须包含**两类价格字段**（出厂价、市场成交价）及对应成交量；
2. 分别就出厂价 / 市场成交价 计算量价趋势，独立输出各自的
   「量价共振 / 量价背离」结论；
3. 交叉校验两者方向：
     出厂价↑ + 市场价↓  → 明确标记「价格传导失效」，
     绝不简单归为「量缩价升」；
4. 封装为可复用函数 analyze_dual_price(data)，支持传入任意个股 / ETF
   历史数据，自动检测并预警「官方价与市价背离」；
5. 输出结构化结果（字段说明 + 趋势判断 + 风险提示），可直接并入：
     · XYD-supply-demand-sector 的 volume_price（市场价侧为主输入）
     · 个股盈利面框架 的 PX 量价景气（消费股 动销/批价/出厂价）

设计原则
------------------------------------------------------------------------
· 纯标准库（无第三方依赖），离线可跑；
· 趋势判定用「近 N 期均值 / 前 N 期均值 − 1」比值，抗单点噪声；
· 不假设数据频率（日/周/月均可），只认字段名 + 顺序；
· 所有阈值外置为参数，可调；
· 不引入任何自由裁量——输入相同永远得到相同结论。
"""

from __future__ import annotations

import json
import math
import statistics as _st
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any


# ============================ 可调参数 ============================

DEFAULT_RECENT = 20        # 近 N 期（趋势窗口）
DEFAULT_BASE = 60          # 对照前 N 期（基线窗口）
DEFAULT_TREND_PCT = 0.02   # 涨跌幅度门槛（2% 以下判 flat）


# ============================ 数据结构 ============================

@dataclass
class SideAnalysis:
    """单侧（出厂价 或 市场成交价）的量价分析"""
    role: str                     # "factory" / "market"
    role_cn: str                  # 中文角色名
    price_trend: str              # up / flat / down
    price_chg_pct: float         # 近窗相对基窗的价格变动 %
    volume_trend: str             # up / flat / down
    volume_chg_pct: float         # 成交量变动 %
    vp_pattern: str               # 量价组合文字（量升价升 / 量缩价跌 ...）
    vp_quality: int               # 双击质量星级 0~5
    note: str = ""


@dataclass
class DualPriceResult:
    meta: Dict[str, Any]
    field_schema: Dict[str, str]
    factory: Dict[str, Any]
    market: Dict[str, Any]
    cross: Dict[str, Any]
    risk_warnings: List[str]
    volume_price_for_framework: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ============================ 工具函数 ============================

def _safe_mean(xs: List[float]) -> float:
    xs = [x for x in xs if x is not None and not (isinstance(x, float) and math.isnan(x))]
    return _st.mean(xs) if xs else float("nan")


def _trend(values: List[Optional[float]], recent: int, base: int, thr: float):
    """
    返回 (label, chg_pct)：
      label ∈ {up, flat, down}，chg_pct = 近窗均值/基窗均值 − 1。
    窗口不足时返回 (flat, nan) 并标注。
    """
    clean = [float(v) for v in values if v is not None]
    n = len(clean)
    if n < max(recent, 4):
        return "flat", float("nan")
    rec = clean[-recent:]
    bs = clean[-(recent + base):-recent] if len(clean) >= recent + base else clean[:-recent]
    m_rec, m_base = _safe_mean(rec), _safe_mean(bs)
    if m_base == 0 or math.isnan(m_base):
        return "flat", float("nan")
    chg = m_rec / m_base - 1.0
    if chg > thr:
        return "up", chg
    if chg < -thr:
        return "down", chg
    return "flat", chg


_VP_MATRIX = {
    ("up", "up"):   ("量升价升", 5, "量价共振·最强（量驱双击）"),
    ("flat", "up"): ("量稳价升", 4, "价驱双击"),
    ("up", "flat"): ("量升价稳", 4, "量驱双击"),
    ("down", "up"): ("量缩价升", 3, "警惕：需验证需求韧性（供给驱动 or 需求衰退？）"),
    ("flat", "flat"): ("量平价平", 2, "观望"),
    ("down", "flat"): ("量缩价稳", 2, "观望"),
    ("up", "down"): ("量升价跌", 0, "派发/出货 → 回避"),
    ("flat", "down"): ("量平价跌", 0, "走弱"),
    ("down", "down"): ("量缩价跌", 0, "结构性排除 / 缩量筑底"),
}


def _vp_pattern(price_trend: str, volume_trend: str):
    key = (volume_trend, price_trend)  # (量, 价)
    pat, stars, note = _VP_MATRIX.get(key, ("未知", 0, ""))
    return pat, stars, note


def _pearson(a: List[float], b: List[float]) -> float:
    """逐期收益率的相关系数（衡量传导同步性）"""
    n = min(len(a), len(b))
    if n < 3:
        return float("nan")
    # 用一阶差分（收益率）
    da = [a[i] - a[i - 1] for i in range(1, n)]
    db = [b[i] - b[i - 1] for i in range(1, n)]
    ma, mb = _safe_mean(da), _safe_mean(db)
    num = sum((da[i] - ma) * (db[i] - mb) for i in range(len(da)))
    da2 = sum((x - ma) ** 2 for x in da)
    db2 = sum((x - mb) ** 2 for x in db)
    if da2 == 0 or db2 == 0:
        return float("nan")
    return num / math.sqrt(da2 * db2)


# ============================ 主分析函数 ============================

def analyze_dual_price(
    data: Dict[str, Any],
    recent: int = DEFAULT_RECENT,
    base: int = DEFAULT_BASE,
    trend_pct: float = DEFAULT_TREND_PCT,
) -> DualPriceResult:
    """
    参数
    ----
    data: 输入契约，必须含：
      meta:        {symbol, name, as_of, asset_type('stock'|'etf'), price_role_hint?}
      records:     [{date, factory_price, factory_volume, market_price, market_volume}, ...]
                    （顺序即时间序，最新在末；volume 可缺，缺则标 unknown）
      可选 column_map: 把自定义列名映射到上述标准键
    返回 DualPriceResult
    """
    meta = data.get("meta", {})
    column_map = data.get("column_map", {})
    raw = data.get("records", [])

    if not raw:
        raise ValueError("[MUST] records 为空，无法分析")

    def col(r: Dict[str, Any], std: str):
        src = column_map.get(std, std)
        return r.get(src)

    fp = [col(r, "factory_price") for r in raw]
    mp = [col(r, "market_price") for r in raw]
    fv = [col(r, "factory_volume") for r in raw]
    mv = [col(r, "market_volume") for r in raw]

    if all(v is None for v in fp) or all(v is None for v in mp):
        raise ValueError("[MUST] factory_price 与 market_price 至少一侧须有数据")

    # O-双价格·窗口自适应（2026-08-31 修复）：DEFAULT_RECENT/BASE(20/60)按日频序列设计，
    # 月频/稀疏序列(n<20)点数不足时 _trend 恒判 flat 且 chg=null，导致分层判读整体失效。
    # 修复：可用点数 < recent 时自动缩窗 recent=n//2(≥2)、base=n−recent，并在 cross 记录模式。
    n_avail = max(
        len([x for x in fp if x is not None]),
        len([x for x in mp if x is not None]),
    )
    window_mode = "default(recent=%d,base=%d)" % (recent, base)
    if n_avail < max(recent, 4):
        if n_avail >= 4:
            recent = max(2, n_avail // 2)
            base = n_avail - recent
            window_mode = "auto_sparse(n=%d,recent=%d,base=%d)" % (n_avail, recent, base)
        else:
            window_mode = "insufficient(n=%d<4,恒判flat)" % n_avail

    # ---------- 单侧分析 ----------
    def side(role, role_cn, prices, vols):
        p_label, p_chg = _trend(prices, recent, base, trend_pct)
        v_label, v_chg = ("unknown", float("nan")) if all(v is None for v in vols) \
            else _trend(vols, recent, base, trend_pct)
        pat, stars, note = _vp_pattern(p_label, v_label)
        return {
            "role": role,
            "role_cn": role_cn,
            "price_trend": p_label,
            "price_chg_pct": round(p_chg * 100, 2) if not math.isnan(p_chg) else None,
            "volume_trend": v_label,
            "volume_chg_pct": round(v_chg * 100, 2) if not math.isnan(v_chg) else None,
            "vp_pattern": pat,
            "vp_quality": stars,
            "vp_note": note,
            "n_points": len([x for x in prices if x is not None]),
        }

    fac = side("factory", "出厂价/官方定价/NAV", fp, fv)
    mkt = side("market", "市场实际成交价/批价/市价", mp, mv)

    # ---------- 交叉校验（核心）----------
    f_t, m_t = fac["price_trend"], mkt["price_trend"]
    is_factory_up = f_t == "up"
    is_market_down = m_t == "down"
    is_factory_down = f_t == "down"
    is_market_up = m_t == "up"

    # 传导效率：两侧收益率相关系数
    eff = _pearson([x for x in fp if x is not None], [x for x in mp if x is not None])

    # 方向一致性判定
    if is_factory_up and is_market_down:
        transmission = "价格传导失效"
        divergence = True
        note = ("出厂价/官方价上行（公司提价或锚定上调，黏性强）而市场成交价下行（终端需求弱、"
                "渠道去库存）→ 真实供需偏弱。严禁误读为「量缩价升」看涨信号；"
                "框架量价组合应以市场价侧为准。")
    elif is_factory_down and is_market_up:
        transmission = "市场抢跑/预期先行"
        divergence = True
        note = "出厂价下行而市场价上行：市场提前交易复苏预期，需基本面确认方可采信。"
    elif f_t == m_t and f_t != "flat":
        transmission = "传导顺畅·共振" if f_t == "up" else "双弱·供需双杀"
        divergence = False
        note = "出厂价与市场价同向，量价信号一致。"
    elif f_t == "flat" and m_t != "flat":
        transmission = "锚定平稳·市场单动"
        divergence = (m_t == "down")
        note = "出厂价黏稳，市场价单独变动，按市场价侧判定。"
    elif m_t == "flat" and f_t != "flat":
        transmission = "市场平稳·官方单动"
        divergence = (f_t == "up")
        note = "市场价平稳，出厂价单独变动（多为公司行为），不反映终端供需。"
    else:
        transmission = "方向分歧·待观察"
        divergence = True
        note = "出厂价与市场价方向不一致，需结合成交量与行业数据判读。"

    cross = {
        "transmission_status": transmission,
        "divergence_flag": divergence,
        "divergence_type": transmission if divergence else None,
        "transmission_efficiency": round(eff, 3) if not math.isnan(eff) else None,
        "is_factory_up_market_down": is_factory_up and is_market_down,  # 用户点名的典型陷阱
        "factory_price_trend": f_t,
        "market_price_trend": m_t,
        "note": note,
        "window_mode": window_mode,
    }

    # ---------- 风险提示 ----------
    warnings: List[str] = []
    if is_factory_up and is_market_down:
        warnings.append(
            "⚠️ 价格传导失效：出厂价↑但市场价↓。若框架只看'价涨'会误判为量缩价升，"
            "实为渠道承压/倒挂风险。请以市场成交价侧的量价组合作为 volume_price 主输入。")
    if eff is not None and not math.isnan(eff) and eff < 0:
        warnings.append(f"⚠️ 出厂价与市场价收益率负相关（效率={eff:.2f}），传导链条断裂。")
    if fac["vp_pattern"] == "量缩价升" and mkt["vp_pattern"] != "量缩价升":
        warnings.append(
            "⚠️ '量缩价升'仅出现在出厂价侧，市场价侧并非如此——该信号不可作为板块/个股看涨依据。")
    if fac["volume_trend"] == "unknown" or mkt["volume_trend"] == "unknown":
        warnings.append("[SHOULD] 一侧成交量缺失，量价组合仅基于价格侧，完整性受限。")

    # ---------- 框架可并入字段 ----------
    # 供给框架 volume_price 应取【市场成交价】侧（真实交易口径）；
    # 出厂价仅作传导校验维度，不喧宾夺主。
    vp_framework = {
        "factory_side": {
            "volume": fac["volume_trend"],
            "price": fac["price_trend"],
            "combination": fac["vp_pattern"],
            "double_click_quality": fac["vp_quality"],
        },
        "market_side": {
            "volume": mkt["volume_trend"],
            "price": mkt["price_trend"],
            "combination": mkt["vp_pattern"],
            "double_click_quality": mkt["vp_quality"],
        },
        "integrated_volume_price": {
            "volume": mkt["volume_trend"],
            "price": mkt["price_trend"],
            "combination": mkt["vp_pattern"],
            "double_click_quality": mkt["vp_quality"],
            "basis": "market_transaction_price",
        },
        "integrated_recommendation": (
            "价格传导失效 → 按【市场成交价】侧量价组合判定，出厂价↑不得读作量缩价升。"
            if (is_factory_up and is_market_down)
            else "以市场成交价侧量价组合为主输入；出厂价侧作传导校验。"
        ),
    }

    field_schema = {
        "factory_price": "出厂价 / 官方定价 / ETF 净值(NAV·IOPV) — 黏性强、公司或指数层可控",
        "market_price": "市场实际成交价 / 批价 / ETF 市价 — 自由交易形成，反映真实供需",
        "factory_volume": "对应出厂侧量：出货量 / 厂商出货量 / ETF 申赎额",
        "market_volume": "对应市场侧量：终端动销 / 实际成交额 / ETF 成交额",
    }

    return DualPriceResult(
        meta=meta,
        field_schema=field_schema,
        factory=fac,
        market=mkt,
        cross=cross,
        risk_warnings=warnings,
        volume_price_for_framework=vp_framework,
    )


# ============================ 内置 Demo ============================

def _maotai_demo_records() -> List[Dict[str, Any]]:
    """
    茅台双价格情景：
      出厂价：前 50 期 969 黏稳，第 51 期起提价至 1369（7/17 提价事件）→ 出厂价↑
      市场批价：1730 缓慢下行至 1650（终端需求弱、渠道去库存）→ 市场价↓
      出货量(factory_volume)：主动控量，缓降
      终端动销(market_volume)：弱，缓降
    → 期望结论：价格传导失效（出厂↑+市场↓），不误判量缩价升。
    """
    recs = []
    for i in range(80):
        # 价格
        fp = 969.0 if i < 50 else 1369.0
        # 市场价：1730 线性缓降 + 轻微噪声
        mp = round(1730.0 - (i / 79) * 80.0 + (1 if i % 7 == 0 else 0), 1)
        # 量：缓降
        fv = round(100.0 - i * 0.4 + (2 if i % 5 == 0 else 0), 1)
        mv = round(80.0 - i * 0.3 + (1.5 if i % 6 == 0 else 0), 1)
        recs.append({
            "date": f"2026-{1 + i // 12:02d}-{1 + i % 28:02d}",
            "factory_price": fp,
            "factory_volume": max(fv, 20.0),
            "market_price": mp,
            "market_volume": max(mv, 15.0),
        })
    return recs


def _etf_demo_records() -> List[Dict[str, Any]]:
    """
    ETF 双价格情景（NAV vs 市价）：
      NAV（官方净值）：平稳上行 1.00 → 1.20
      市价（二级市场交易价）：因折价走 1.00 → 0.95（折价扩大）
      → 期望结论：价格传导失效（NAV↑ + 市价↓ = 折价扩大，需求弱）
    """
    recs = []
    for i in range(80):
        nav = round(1.00 + (i / 79) * 0.20, 4)
        mkt = round(1.00 - (i / 79) * 0.05, 4)   # 折价
        recs.append({
            "date": f"2026-{1 + i // 12:02d}-{1 + i % 28:02d}",
            "factory_price": nav,         # 这里 factory=NAV（官方）
            "factory_volume": 50.0 + (i % 10),
            "market_price": mkt,          # market=市价
            "market_volume": 30.0 + (i % 8),
        })
    return recs


# ============================ CLI ============================

def _print_result(res: DualPriceResult):
    d = res.to_dict()
    print(json.dumps(d, ensure_ascii=False, indent=2))


def main():
    import argparse
    ap = argparse.ArgumentParser(description="双价格维度量价分析（出厂价/官方价 vs 市场成交价）")
    ap.add_argument("--input", help="输入 JSON 文件（含 meta + records）")
    ap.add_argument("--selfcheck", action="store_true", help="内置 demo 自校验")
    ap.add_argument("--demo", choices=["maotai", "etf"], default="maotai", help="打印某 demo")
    args = ap.parse_args()

    if args.selfcheck:
        run_selfcheck()
        return

    if args.input:
        with open(args.input, encoding="utf-8") as f:
            data = json.load(f)
        _print_result(analyze_dual_price(data))
        return

    demo = _maotai_demo_records() if args.demo == "maotai" else _etf_demo_records()
    name = "白酒·贵州茅台(双价格demo)" if args.demo == "maotai" else "行业ETF(NAV vs 市价 demo)"
    data = {"meta": {"symbol": "600519.SH" if args.demo == "maotai" else "ETF.XXX",
                     "name": name, "as_of": "2026-08-16",
                     "asset_type": "stock" if args.demo == "maotai" else "etf"},
            "records": demo}
    _print_result(analyze_dual_price(data))


def run_selfcheck():
    print("===== SELFCHECK: 茅台双价格（出厂↑ + 市场↓）=====")
    res = analyze_dual_price({
        "meta": {"symbol": "600519.SH", "name": "白酒·贵州茅台", "as_of": "2026-08-16",
                 "asset_type": "stock"},
        "records": _maotai_demo_records(),
    })
    d = res.to_dict()
    print(f"  出厂价趋势={d['factory']['price_trend']}({d['factory']['price_chg_pct']}%)  "
          f"市场价趋势={d['market']['price_trend']}({d['market']['price_chg_pct']}%)")
    print(f"  出厂量价={d['factory']['vp_pattern']}  市场量价={d['market']['vp_pattern']}")
    print(f"  传导状态={d['cross']['transmission_status']}  "
          f"is_factory_up_market_down={d['cross']['is_factory_up_market_down']}")
    print(f"  框架并入 volume_price（市场侧）= {d['volume_price_for_framework']['integrated_volume_price']['combination']}")

    # 关键断言：出厂↑+市场↓ → 价格传导失效，且不被标为量缩价升
    assert d["cross"]["is_factory_up_market_down"] is True, "应为出厂↑+市场↓"
    assert d["cross"]["transmission_status"] == "价格传导失效", "应标记价格传导失效"
    assert d["market"]["vp_pattern"] != "量缩价升", "市场侧不应是量缩价升"
    assert "价格传导失效" in d["risk_warnings"][0], "风险提示须含价格传导失效"
    assert d["volume_price_for_framework"]["integrated_volume_price"]["combination"] == d["market"]["vp_pattern"], \
        "框架主输入须取市场侧"

    print("\n===== SELFCHECK: ETF（NAV↑ + 市价↓ 折价扩大）=====")
    res2 = analyze_dual_price({
        "meta": {"symbol": "ETF.XXX", "name": "行业ETF", "as_of": "2026-08-16", "asset_type": "etf"},
        "records": _etf_demo_records(),
    })
    d2 = res2.to_dict()
    print(f"  NAV趋势={d2['factory']['price_trend']}  市价趋势={d2['market']['price_trend']}  "
          f"传导={d2['cross']['transmission_status']}")
    assert d2["cross"]["is_factory_up_market_down"] is True
    assert d2["cross"]["transmission_status"] == "价格传导失效"

    print("\n✅ SELFCHECK 全部通过：出厂价↑+市场价↓ 被正确识别为「价格传导失效」，"
          "未误判为「量缩价升」。")


if __name__ == "__main__":
    main()
