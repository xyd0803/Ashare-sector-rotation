#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XYD-供需驱动板块投资框架 —— 行业周期量价 Gate（独立可复用节点）
================================================================
存在理由（用户 2026-08-15 裁决）
----------------------------------------------------------------
本技能标题即「供需驱动板块投资」，其「量价关系」特指 **ETF 所跟踪指数的
行业发展周期维度** 的供需量价，即：
    · 量  = 行业产销量 / 出货量 / 装机量 / 订单量 / 产能利用率 / 库存 等
            （国金·一凌产能周期口径；见 SKILL §⑦ 窗2）
    · 价  = 行业核心品价格 / 终端批价 / ASP / ARPU / 货币化率 等
**绝非** 该 ETF 自身的换手率 / 成交量 / 成交额 / 净值，亦非该指数点位，
更非整体市场（大盘 / 沪深300 / 北向 / 融资余额）的涨跌。

历史上曾出现把「ETF 日K换手率 + ETF 价格」误当行业量价 → 结论完全反转
（光伏误判黄金区、电池误判黄金区、港股互联网误判回避）。本 Gate 在
discipline.py 输入契约层强制校验，任何把量价建立在 ETF 交易行为 / 指数
点位 / 整体市场上的分析都会被拒，从而让引擎（及下游 Flash 模型）无法
在错误口径上产出裁决。

判定逻辑
----------------------------------------------------------------
1. vp_basis 必须显式声明为 "industry_cycle"，否则 HARD FAIL。
2. industry_volume_metric / industry_price_metric 必须非空，且：
   · 不得命中任何 FORBIDDEN_TOKENS（ETF/指数/大盘类词汇）→ HARD FAIL；
   · 必须至少命中一个 ALLOWED 行业周期关键词 → 否则 WARN（仍通过但提示）。
3. 若同时提供 industry_volume_trend / industry_price_trend，须与 volume/price
   方向一致，矛盾则 HARD FAIL（捕捉 Flash 把 ETF 量填进 volume 字段的错位）。

CLI
----------------------------------------------------------------
python gate_industry_cycle.py --input case.json
python gate_industry_cycle.py --selfcheck        # 内置正反用例
"""
from __future__ import annotations
import sys, os, json, argparse

# ---- Windows 控制台 UTF-8 ----
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))

# =============================================================================
# 词表
# =============================================================================
# 注意：禁止词必须指向「ETF/指数/大盘的交易或点位」语境，避免误伤合法行业量价
# （如 证券行业的「A股成交额」、制造业的「行业成交量」属于行业供需口径，应放行）。
FORBIDDEN_TOKENS = [
    # ETF / 基金交易行为（绝对禁止当作行业量价）
    "ETF", "换手率", "换手", "基金份额", "份额申赎", "申赎", "申购", "赎回",
    "净值", "ETF价格", "ETF净值", "ETF涨跌", "本基金", "该ETF", "标的成交额",
    "标的成交量", "指数成交量", "指数成交额",
    # 指数 / 行情点位（禁止把指数自身价格当行业价）
    "指数点位", "指数收盘", "指数价格", "K线", "日K", "周K", "量能", "量比",
    "指数涨", "指数跌", "指数走势", "近20日", "近20交易", "60日",
    # 整体市场 / 大盘资金面（禁止当作行业量）
    "大盘", "整体市场", "全市场", "市场成交额", "全市场成交额", "大盘成交额",
    "沪深300", "上证指数", "创业板指", "北向资金", "北向", "融资余额",
    "融资融券", "主力资金", "资金流向",
]

# 行业周期「量」的正向关键词（命中其一即视为合法行业量口径）
ALLOWED_VOLUME_KEYWORDS = [
    "装机", "装车", "出货量", "产量", "销量", "产销量", "订单", "订单量",
    "产能", "产能利用率", "开工率", "库存", "库存周期",
    "渗透率", "交付", "交付量", "排产", "批签发", "出栏", "投产", "产量同比",
    "出货量同比", "装机同比", "订单同比", "GMV", "gmv", "用户", "月活", "DAU",
    "MAU", "活跃", "单量", "包裹", "件量", "吞吐量", "客流量", "产能出清",
    "供需", "供给", "需求", "产量增速", "出货增速", "成交额", "成交量", "A股",
]

# 行业周期「价」的正向关键词
ALLOWED_PRICE_KEYWORDS = [
    "价格", "批价", "出厂价", "现货价", "现货", "报价", "ASP", "asp", "单价",
    "ARPU", "arpu", "货币化率", "货币化", "客单价", "提价", "涨价", "降价",
    "均价", "成本", "锂价", "硅料", "硅片", "组件", "电芯", "碳酸锂", "锂盐",
    "钢价", "煤价", "猪价", "粮价", "运价", "租金", "保费", "利率", "费率",
    "eCPM", "ecpm", "加载率", "广告单价", "终端价", "零售价", "批零", "佣金率",
    "ROE", "roe",
]

VALID_BASIS = "industry_cycle"
FORBIDDEN_BASIS = {
    "etf_turnover", "etf_price", "index_price_volume", "broad_market",
    "undefined", "unknown", "market", "etf", "index",
}


# =============================================================================
# 核心校验
# =============================================================================
class GateResult:
    def __init__(self):
        self.ok = True
        self.level = "PASS"          # PASS / WARN / FAIL
        self.errors = []             # HARD FAIL 原因
        self.warnings = []           # WARN 提示
        self.info = []

    def fail(self, msg):
        self.ok = False
        self.level = "FAIL"
        self.errors.append(msg)

    def warn(self, msg):
        if self.level == "PASS":
            self.level = "WARN"
        self.warnings.append(msg)

    def to_dict(self):
        return {
            "ok": self.ok, "level": self.level,
            "errors": self.errors, "warnings": self.warnings, "info": self.info,
        }

    def __repr__(self):
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


def _hit(text: str, tokens) -> list:
    return [t for t in tokens if t in (text or "")]


def _has_any(text: str, tokens) -> bool:
    return any(t in (text or "") for t in tokens)


def validate_vp_industry_cycle(inp: dict) -> GateResult:
    """校验一个分析输入的量价是否建立在正确的行业周期定义上。

    返回 GateResult；HARD FAIL 时 ok=False。供 discipline.py 在 from_dict 中
    调用并 raise，亦可供下游/Flash 模型在产出裁决前自检。
    """
    r = GateResult()
    if not isinstance(inp, dict):
        r.fail("输入非字典，无法校验量价来源")
        return r

    basis = (inp.get("vp_basis") or "").strip()
    vol_metric = (inp.get("industry_volume_metric") or "").strip()
    price_metric = (inp.get("industry_price_metric") or "").strip()

    # ---- 1. 必须声明 basis = industry_cycle ----
    if not basis:
        r.fail("未声明 vp_basis：量价分析必须显式声明 vp_basis='industry_cycle' "
               "（行业供需周期口径），禁止留空。")
    elif basis in FORBIDDEN_BASIS or basis != VALID_BASIS:
        r.fail(f"vp_basis='{basis}' 非法：本技能量价仅接受行业周期口径 "
               f"(vp_basis='{VALID_BASIS}')，禁止使用 ETF 交易/指数点位/整体市场 口径。")

    # ---- 2. 行业量 / 行业价 指标必须非空且不含禁止词 ----
    for label, metric in (("行业量(industry_volume_metric)", vol_metric),
                          ("行业价(industry_price_metric)", price_metric)):
        if not metric:
            r.fail(f"{label} 为空：必须填写该行业周期的实际量/价指标"
                   f"（如『全球光伏新增装机量(GW)』『光伏组件价格(元/W)』）。")
            continue
        bad = _hit(metric, FORBIDDEN_TOKENS)
        if bad:
            r.fail(f"{label} 命中禁止词 {bad} → 这是 ETF 交易/指数点位/大盘 口径，"
                   f"不是行业供需周期量价。请改用行业产销量/核心品价格。")
        # 正向关键词检查（仅 WARN）
        allowed = ALLOWED_VOLUME_KEYWORDS if "volume" in label else ALLOWED_PRICE_KEYWORDS
        if not _has_any(metric, allowed):
            r.warn(f"{label}=『{metric}』未命中任何行业周期关键词，请确认其确为"
                   f"行业供需指标而非其他口径。")

    # ---- 3. 方向一致性（捕捉 volume 字段被错填为 ETF 量）----
    vt = (inp.get("industry_volume_trend") or "").strip()
    pt = (inp.get("industry_price_trend") or "").strip()
    if vt and vt in ("up", "flat", "down") and inp.get("volume") not in (None, "", vt):
        r.fail(f"industry_volume_trend='{vt}' 与 volume='{inp.get('volume')}' 矛盾："
               f"量价方向必须以行业周期口径填写，不得与 ETF 交易行为混填。")
    if pt and pt in ("up", "flat", "down") and inp.get("price") not in (None, "", pt):
        r.fail(f"industry_price_trend='{pt}' 与 price='{inp.get('price')}' 矛盾："
               f"价格方向必须以行业核心品价格填写，不得与 ETF/指数价格混填。")

    # ---- 4. 价跌性质声明校验（§12.1b 降档通道 · 2026-08-15）----
    # 只有「量升价跌·成本通缩/以价换量」且显式声明才可免于硬置零；
    # 缺声明 / oversupply_war / demand_collapse → 维持价跌硬否决 0%。
    pdn = (inp.get("price_decline_nature") or "").strip()
    if pdn:
        if pdn not in ("cost_deflation", "oversupply_war", "demand_collapse"):
            r.fail(f"price_decline_nature='{pdn}' 非法：仅允许 "
                   f"cost_deflation / oversupply_war / demand_collapse（留空=按硬否决处理）。")
        elif pdn == "cost_deflation":
            if inp.get("volume") != "up" or inp.get("price") != "down":
                r.fail("声明 price_decline_nature=cost_deflation 但量价非『量升价跌』"
                       "（volume 须 up 且 price 须 down），降档通道不成立。")
            if inp.get("demand") != "growth":
                r.fail("声明 cost_deflation 但 demand 非 growth（结构性增长）："
                       "仅结构性高增下的以价换量才可降档，周期性/侵蚀/塌方维持硬否决。")
            cost_hint = (inp.get("industry_price_metric") or "") + \
                        (inp.get("vp_basis_note") or "")
            if not any(k in cost_hint for k in
                       ("成本", "以价换量", "成本通缩", "降本", "传导", "成本下行", "成本下移")):
                r.fail("声明 cost_deflation 但 industry_price_metric / vp_basis_note 无成本传导类佐证"
                       "（成本/以价换量/降本/传导…）：无法证明价跌源于成本下移，降档通道拒绝。")
            else:
                r.info.append("降档通道声明已核：量升价跌+结构性增长+成本通缩佐证齐备。")
        else:
            r.info.append(f"price_decline_nature='{pdn}'：维持价跌硬否决（不开放降档）。")

    if r.ok and r.level == "PASS":
        r.info.append("量价来源校验通过：建立在 ETF 跟踪指数的行业供需周期口径上。")
    return r


# =============================================================================
# 便捷封装：注入合法行业周期字段（供 demo / redteam / 回测 兼容旧用例）
# =============================================================================
def inject_industry_cycle(inp: dict, vol_metric: str, price_metric: str,
                          vt: str = "", pt: str = "") -> dict:
    """给一个（可能旧的）输入字典补齐合法的行业周期字段，避免硬闸门误伤。"""
    out = dict(inp)
    out["vp_basis"] = VALID_BASIS
    out["industry_volume_metric"] = vol_metric
    out["industry_price_metric"] = price_metric
    if vt:
        out["industry_volume_trend"] = vt
    if pt:
        out["industry_price_trend"] = pt
    return out


# =============================================================================
# 自检
# =============================================================================
def _selfcheck() -> int:
    cases = [
        # (描述, 输入, 期望通过)
        ("正确-行业周期", {
            "vp_basis": "industry_cycle",
            "industry_volume_metric": "全球光伏新增装机量(GW) 同比",
            "industry_price_metric": "光伏组件价格(元/W) 现货",
            "volume": "down", "price": "up",
        }, True),
        ("错误-ETF换手率", {
            "vp_basis": "industry_cycle",
            "industry_volume_metric": "ETF 近20日换手率",
            "industry_price_metric": "组件价格",
            "volume": "down", "price": "up",
        }, False),
        ("错误-未声明basis", {
            "industry_volume_metric": "装机量",
            "industry_price_metric": "组件价",
        }, False),
        ("错误-basis=etf", {
            "vp_basis": "etf_turnover",
            "industry_volume_metric": "装机量",
            "industry_price_metric": "组件价",
        }, False),
        ("错误-指数点位当价", {
            "vp_basis": "industry_cycle",
            "industry_volume_metric": "装机量",
            "industry_price_metric": "指数点位 2554",
        }, False),
        ("错误-方向矛盾", {
            "vp_basis": "industry_cycle",
            "industry_volume_metric": "装机量同比",
            "industry_price_metric": "组件价格",
            "volume": "up", "industry_volume_trend": "down",
        }, False),
        ("正确-降档声明齐全(量升价跌+成本通缩)", {
            "vp_basis": "industry_cycle",
            "industry_volume_metric": "动力电池装车量(GWh) 同比 +41.6%",
            "industry_price_metric": "电芯价格(元/Wh) 下行，源于锂价成本传导",
            "volume": "up", "price": "down", "demand": "growth",
            "price_decline_nature": "cost_deflation",
        }, True),
        ("错误-降档但缺成本佐证", {
            "vp_basis": "industry_cycle",
            "industry_volume_metric": "装车量同比",
            "industry_price_metric": "电芯价格(元/Wh)",
            "volume": "up", "price": "down", "demand": "growth",
            "price_decline_nature": "cost_deflation",
        }, False),
        ("错误-降档但量非升", {
            "vp_basis": "industry_cycle",
            "industry_volume_metric": "装机量同比 -26%",
            "industry_price_metric": "组件价格，成本传导",
            "volume": "down", "price": "down", "demand": "growth",
            "price_decline_nature": "cost_deflation",
        }, False),
        ("错误-降档但需求非结构性增长", {
            "vp_basis": "industry_cycle",
            "industry_volume_metric": "装车量同比",
            "industry_price_metric": "电芯价格，成本传导",
            "volume": "up", "price": "down", "demand": "cyclical",
            "price_decline_nature": "cost_deflation",
        }, False),
        ("正确-oversupply_war 维持硬否决语义", {
            "vp_basis": "industry_cycle",
            "industry_volume_metric": "装机量同比",
            "industry_price_metric": "组件价格(元/W)",
            "volume": "up", "price": "down", "demand": "growth",
            "price_decline_nature": "oversupply_war",
        }, True),
        ("错误-价跌性质非法值", {
            "vp_basis": "industry_cycle",
            "industry_volume_metric": "装车量同比",
            "industry_price_metric": "电芯价格",
            "volume": "up", "price": "down", "demand": "growth",
            "price_decline_nature": "flash_hack",
        }, False),
    ]
    fails = 0
    for desc, inp, expect_ok in cases:
        res = validate_vp_industry_cycle(inp)
        status = "PASS✓" if res.ok else "FAIL✗"
        mark = "OK" if res.ok == expect_ok else "*** 期望不符 ***"
        if res.ok != expect_ok:
            fails += 1
        print(f"[{status}] {desc} -> level={res.level} {mark}")
        for e in res.errors:
            print(f"      ✗ {e}")
        for w in res.warnings:
            print(f"      ⚠ {w}")
    print(f"\n自检: {'全部通过' if fails == 0 else str(fails) + ' 个用例不符'}")
    return 0 if fails == 0 else 1


def main():
    ap = argparse.ArgumentParser(description="行业周期量价 Gate")
    ap.add_argument("--input", help="待校验的 case.json 路径")
    ap.add_argument("--selfcheck", action="store_true", help="内置正反用例自检")
    args = ap.parse_args()
    if args.selfcheck:
        raise SystemExit(_selfcheck())
    if args.input:
        with open(args.input, "r", encoding="utf-8") as f:
            inp = json.load(f)
        res = validate_vp_industry_cycle(inp)
        print(repr(res))
        raise SystemExit(0 if res.ok else 2)
    print("用法: gate_industry_cycle.py --input case.json | --selfcheck")
    raise SystemExit(1)


if __name__ == "__main__":
    main()
