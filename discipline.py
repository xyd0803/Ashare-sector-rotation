#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XYD 供需驱动板块投资框架 —— 纪律层执行引擎 (Discipline Layer Engine)
================================================================
唯一权威实现。技能执行时，所有「胜率 / 赔率 / 象限 / 仓位 / 否决」结论
**必须**由本引擎产出，禁止在对话中口算或定性微调（§12.1 [MUST] 唯一映射源）。

规则来源：《供需驱动板块投资框架》(2026-08-14 A/B/C 三层级纪律补丁版)
  §零   分级字典 / 适用边界 / 新鲜度门 / 分位周期硬约束
  §一   需求塌方否决 + 需求慢性侵蚀 + 价值陷阱
  §二   量价组合矩阵 + 前瞻矩阵 + 价格见底 6 信号（独立左侧通道）
  §四   PB 分位双周期 + PB 数据交叉验证协议 + 成分股离散度下钻
  §五   买卖点决策四象限
  §八.1 板块硬信号必勾清单
  §十   想象力评级与仓位调控公式
  §12.1 胜率机械映射（六档 + 白名单修正 + 硬否决 gate）
  §12.2 赔率位置条（五档参考线）
  §12.5 极端情景层（尾部风险 / 涨过头兑现）
  §十三 发布前 8 项自检清单

设计原则
  1. 参数外置 params.json 并做指纹冻结 —— 防偷偷调参 / 防过拟合。
  2. 无自由裁量入口 —— 修正项只认白名单 key，未知 key 直接 raise。
  3. 全程留痕 trace —— 每条规则是否触发、加减多少、出自哪一节。
  4. 纯标准库，零第三方依赖 —— 运营环境可直接跑。

CLI
  python discipline.py --input case.json          # 单标的
  python discipline.py --input list.json --batch   # 多标的（JSON 数组）
  python discipline.py --demo                      # 内置样例
  python discipline.py --selfcheck                 # 引擎自检（不含红队/压力）
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

# ---- Windows 控制台 UTF-8（避免 GBK 下中文/符号 UnicodeEncodeError）----
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
PARAMS_PATH = os.path.join(HERE, "params.json")
if HERE not in sys.path:
    sys.path.insert(0, HERE)

VP_LABEL = {
    "up|up": "量升价升", "up|flat": "量升价稳", "up|down": "量升价跌",
    "flat|up": "量稳价升", "flat|flat": "量稳价稳", "flat|down": "量稳价跌",
    "down|up": "量缩价升", "down|flat": "量缩价稳", "down|down": "量缩价跌",
}
VP_STARS = {"up|up": 5, "up|flat": 4, "flat|up": 4, "down|up": 3}
DEMAND_LABEL = {"growth": "结构性增长", "cyclical": "周期性波动",
                "erosion": "需求慢性侵蚀", "collapse": "结构性塌方"}

# F1 EV盈利增长项截断边界（2026-09-04 用户裁定：上行 +60% 上限截断；下行 −30% 下限截断）。
# g 为年化一致预期增速%，持有期内复利 (1+g)^(H/12)；越界截断并在 trace/disclosure 留痕。
EV_GROWTH_MIN_PCT = -30.0
EV_GROWTH_MAX_PCT = 60.0

# ---- 五维量化胜率模块（§十二，2026-08-15 用户裁定：胜率须由客观指标确定性算出，禁 LLM 主观拍定）----
try:
    from quant_winrate import compute_quant as _compute_quant
except Exception:  # 模块缺失时降级（不影响六档表回退路径）
    _compute_quant = None

# ---- O1 量化输入契约（2026-08-16）：D1-D5 八字段从原始序列确定性派生，关闭 V1 静默主观漏洞 ----
try:
    from quant_inputs import enforce_quant_contract as _enforce_quant_contract
except Exception:  # 模块缺失时降级（不影响既有路径）
    _enforce_quant_contract = None

# ---- O6 BLEND 自适应权重（2026-08-16）：按行业细分 PE/PB 权重，替代全局 0.6/0.4，关 V6 ----
try:
    from valuation_anchor import blend_weights_for as _blend_wf
except Exception:  # 模块缺失时降级（回退全局默认权重）
    _blend_wf = None


def load_params(path: str = PARAMS_PATH) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


PARAMS = load_params()


# =============================================================================
# 输入契约
# =============================================================================
@dataclass
class SectorInput:
    """板块/ETF 分析输入。字段后标 [MUST] 者缺失将导致 status=PENDING_VERIFICATION。"""
    sector: str
    as_of: str                                  # [MUST] 数据日期 YYYY-MM-DD（§零 0.3）
    asset_class: str = "sector"                 # [MUST] sector/theme/broad/commodity/bond/money
    index_code: str = ""                        # [MUST] ETF 须查明跟踪指数代码（§六 Step0）
    index_name: str = ""
    weighting: str = ""                         # [MUST for 跨境] mktcap / equal
    is_cross_border: bool = False

    # §一 需求
    demand: str = ""                            # growth/cyclical/erosion/collapse（空=未填→MUST 拦截·未填即错 2026-09-07）
    erosion_signals: int = 0                    # 0~4，≥2 确认侵蚀
    terminal_price_down: bool = False           # 终端批价趋势性下行

    # §二 量价
    volume: str = ""                            # up/flat/down（空=未填→MUST 拦截·未填即错 2026-09-07）
    price: str = ""                             # up/flat/down（空=未填→MUST 拦截）
    forward: str = ""                           # up/flat/down 前瞻方向（空=未填→MUST 拦截）
    bottom_signals: int = 0                     # §二 价格见底 6 信号点亮数
    prev_vp: str = ""                           # 上期量价（判鱼尾），如 "up|up"

    # §二 量价来源（行业周期硬闸门 · 2026-08-15 用户裁决）
    # 本技能「量价关系」特指 ETF 所跟踪指数的【行业发展周期维度】供需量价：
    #   量 = 行业产销量/出货量/装机量/订单量/产能利用率/库存…
    #   价 = 行业核心品价格/终端批价/ASP/ARPU/货币化率…
    # 严禁以 ETF 自身换手率/成交量/净值、指数点位、整体市场涨跌 充当量价。
    vp_basis: str = ""                          # [MUST] 必须 = "industry_cycle"
    industry_volume_metric: str = ""            # 行业量口径（如『全球光伏新增装机量(GW)』）
    industry_price_metric: str = ""             # 行业价口径（如『光伏组件价格(元/W)』）
    industry_volume_trend: str = ""             # 可选：行业量方向，须与 volume 一致
    industry_price_trend: str = ""              # 可选：行业价方向，须与 price 一致
    vp_basis_note: str = ""
    # 价跌性质（§12.1b 降档通道 · 2026-08-15 用户裁定）
    # 仅当 量升价跌 + demand=growth + 显式声明 cost_deflation 才开放降档通道；
    # oversupply_war(供过于求价格战) / demand_collapse / 未声明 → 仍价跌硬否决 0%。
    price_decline_nature: str = ""              # "" | cost_deflation | oversupply_war | demand_collapse

    # §三bis 景气环比门槛（2026-08-29 用户裁定：买点加景气环比门槛防价值陷阱，分左侧/右侧买点）
    # 景气环比 ΔG = 行业景气的边际变化方向/幅度（参考张启尧 118 细分行业景气坐标：横=景气分位、纵=景气环比 ΔG）。
    # 低 PB 单独成立可能是价值陷阱（诺普信式：PB 极低但景气仍下行=接刀），须用景气环比交叉校验。
    # 未提供（prosperity_trend="" 且 prosperity_mom_pct=None）→ 仅建议性、不强制门槛（向后兼容、零回归）。
    prosperity_trend: str = ""                  # 景气环比方向: up / flat / down（空=未提供）
    prosperity_mom_pct: Optional[float] = None  # 景气环比数值%(客观优先)；缺则按 trend 代理(±1/0)

    # §四 PB（赔率生命线）
    pb_now: Optional[float] = None
    pb_manual: Optional[float] = None           # [MUST] 手算兜底 = 指数点位 ÷ BVPS
    pb_sources: Dict[str, float] = field(default_factory=dict)
    pb_field_label: str = ""                    # 数据源字段名（警戒词扫描用）
    pb_pct: Optional[float] = None              # [MUST] 主决策分位（应为 5y）
    pb_pct_period: str = ""                     # [MUST] 5y / 10y / all
    pb_pct_10y: Optional[float] = None
    pb_series_years: Optional[float] = None      # 分位样本窗口年数
    pb_series_freq: str = "daily"
    pb_series: List[float] = field(default_factory=list)  # 可选：日频真实序列（自动算五档）
    constituent_pb_dispersion_pp: Optional[float] = None  # 成分股 PB P90−P10
    dispersion_drilled: bool = False            # 是否已下钻子行业
    pb_p5: Optional[float] = None
    pb_p20: Optional[float] = None
    pb_p50: Optional[float] = None
    pb_p80: Optional[float] = None
    pb_p95: Optional[float] = None

    # §估值主锚（2026-08-15 用户裁定：行业按 PB / PE / BLEND 分类）
    # 大部分行业以 PB 作主锚；少部分成长行业以 PE 作主锚，或 PE+PB 动态混合。
    # valuation_anchor 须与行业真实主锚一致，由 gate_final 终检校验。
    # 2026-09-07 修复：默认值由 "PB" 改为 ""——空值时 run() 内由 valuation_anchor.classify()
    # （权威分类器）自动推断；严禁静默默认 PB（曾致 15 只 ETF 批量全 PB 的锚定 bug）。
    valuation_anchor: str = ""                 # [MUST] PB / PE / BLEND（空=自动分类）

    # §四 PE（仅当 valuation_anchor ∈ {PE, BLEND} 时必填，字段口径同 PB）
    pe_now: Optional[float] = None              # 当前 PE(TTM)
    pe_manual: Optional[float] = None           # [MUST for PE/BLEND] 手算兜底 = 指数点位 ÷ EPS(TTM)
    pe_sources: Dict[str, float] = field(default_factory=dict)
    pe_field_label: str = ""                    # 数据源字段名（警戒词扫描用）
    pe_pct: Optional[float] = None              # [MUST for PE/BLEND] 主决策分位（应为 5y）
    pe_pct_period: str = ""                     # [MUST for PE/BLEND] 5y / 10y
    pe_pct_10y: Optional[float] = None
    pe_series_years: Optional[float] = None
    pe_series_freq: str = "daily"
    pe_series: List[float] = field(default_factory=list)
    pe_p5: Optional[float] = None
    pe_p20: Optional[float] = None
    pe_p50: Optional[float] = None
    pe_p80: Optional[float] = None
    pe_p95: Optional[float] = None

    # §十二 五维量化胜率输入（2026-08-15 用户裁定：胜率须由客观指标确定性算出，禁 LLM 主观拍定）
    # 全部可选；提供则引擎走五维量化基础胜率，否则回退旧六档表（向后兼容）。
    # 指标口径须为【行业周期维度】（与 §二 量价来源一致），禁止 ETF 交易/指数点位。
    #   D1 行业价格强度：行业核心品价格近6月涨跌幅(%) + 历史分位(0-100)
    #   D2 产业供需量能：产销量/出货/装机量同比(%) + 库存同比(%·负=去库利好) + 产能利用率(%)
    #   D3 市场资金共识：ETF 资金净流入近N月(%) + 成交额趋势(%)（正=放量）
    #   D4 景气预期边际：一致预期盈利上修占比(%)
    #   D5 趋势结构质量：价格序列(自动算均线/突破/背离) 或 显式给 ma/breakout/divergence
    q_price_mom6: Optional[float] = None
    q_price_pct: Optional[float] = None
    q_vol_yoy: Optional[float] = None
    q_inv_yoy: Optional[float] = None
    q_util: Optional[float] = None
    q_fund_flow_pct: Optional[float] = None
    q_fund_turn_pct: Optional[float] = None
    q_earning_rev: Optional[float] = None
    q_prices: List[float] = field(default_factory=list)
    q_ma_score: Optional[float] = None
    q_breakout_score: Optional[float] = None
    q_divergence_penalty: Optional[float] = None

    # O1 量化输入契约（2026-08-16）：D1-D5 八字段须由此原始序列确定性派生，禁止手填主观。
    # 提供 quant_raw → 引擎 enforce_quant_contract 派生并覆盖 q_xxx；缺失→显式 None→六档回退。
    quant_raw: Dict[str, Any] = field(default_factory=dict)

    # §十 / §八 / §六
    imagination: str = ""                       # S/A/B/C/D（空=未填→MUST 拦截·未填即错 2026-09-07）
    crowding: bool = False                      # §八 三条任一触发
    catalyst: bool = False
    catalysts: List[Any] = field(default_factory=list)  # F3(2026-09-04)：兼容 str 老格式与 {name,window_months,direction} 结构化格式

    # ---- F1 EV盈利增长项（2026-09-04 · 前瞻修复批次一 · 关"基本面不变"静态化盲区上行侧）----
    # 行业一致预期口径的年化盈利/净资产增速%（PB锚=BVPS增速、PE锚=EPS增速、BLEND=混合），
    # 由 LLM 带来源(provenance)填入。缺省 None → 引擎行为与旧版逐字段一致（零回归）。
    # 引擎按 (1+g)^(持有期/12) 对四档目标价复利；g 截断于 [EV_GROWTH_MIN_PCT, EV_GROWTH_MAX_PCT]=[-30,+60]。
    earnings_fwd_growth_pct: Optional[float] = None

    # ---- F4 见底6信号逐项留痕（2026-09-04 · 左侧通道可审计）----
    # 可选：[{name, value, evidenced(bool)}] ×6；与 bottom_signals 整数不一致 → [MUST] 矛盾 blocker。
    # 缺省空 → 整数口径照旧（零回归；bottom_signals≥3 时引擎发 [SHOULD] 建议补留痕）。
    bottom_signal_detail: List[Dict[str, Any]] = field(default_factory=list)

    # ---- F2 前瞻方向证据链（2026-09-04 批次二/三 · 恢复前瞻矩阵②的独立证据要求）----
    # forward 判定须有独立前瞻证据（订单/排产/期货远期曲线/一致预期上修/政策 等），禁止"当前量价惯性外推"。
    # 格式：[{type, desc, source}]（type∈orders/capex/futures_curve/consensus_revision/policy/other）。
    # 行为双档（params.gates.forward_evidence_required）：
    #   · false（SHOULD 档，缺省）：forward≠flat 且无有效证据 → [SHOULD] 告警，不阻断；
    #   · true（MUST 档）：同情形 → [MUST] blocker → PENDING_VERIFICATION。
    # forward=flat 无前瞻主张 → 两档均不要求（零回归）。
    forward_evidence: List[Dict[str, Any]] = field(default_factory=list)

    today: str = ""                             # 留空取系统日期（新鲜度门）
    notes: str = ""
    price_now: Optional[float] = None           # [可选] ETF/指数当前价；用于输出绝对目标价。
                                                # 缺省则用「相对现价=100」的目标价口径（无需绝对价数据）。

    # ---- P* 风险调整买点（2026-09-08 · 只读披露，默认全空→零回归）----
    # 公式与盈利面 p_star 同源、参数独立（params.p_star）：
    #   P*=[W·mid+(1−W)·bear_eff]/(1+r)；r=clamp(r_base(W)×s_vol,3%,60%)；
    #   s_vol=clamp(σ_eff/σ_ref,0.25,2.0)；σ_eff=σ_ann·√(H/12)（H 缺省=O8 持有期）。
    # σ 解析链：p_star_sigma_ann > 由 quant_raw.price_series / q_prices 派生 > 资产类别默认表
    # > 缺失(→s_vol=1.0，r 不缩放)。止损+摩擦走 bear_eff，不进 r（避免与 σ 重复计入）。
    p_star_sigma_ann: Optional[float] = None    # 年化波动率（如 0.24）；None=走解析链
    p_star_horizon_months: Optional[float] = None  # σ 尺度换算持有期(月)；None=用 EV 持有期
    p_star_stop_pct: Optional[float] = None     # 止损幅度（0.15=15%）；None=cfg默认；<=0=关闭修正
    p_star_friction_pct: Optional[float] = None # 交易摩擦；None=用 cfg 默认
    # g(S) 胜率映射模式覆盖：None=用 params.win_quant.win_map_mode；
    # "linear"=旧分段线性（逐位一致）；"convex"=段内凸(γ=1.5，保守不虚高)。
    win_map_mode: Optional[str] = None

    # O8 EV 持有期（2026-08-16 · 关 V7）：显式持有期（月），缺省取 params.ev_horizon.default_horizon_months
    ev_horizon_months: Optional[float] = None
    # O9 制度周期断裂标记（2026-08-16 · 关 V12）：上游检测到结构性断裂(政策/指数重构/并购)时置 True
    # → 引擎将有效分位切换至 10y 口径并发 SHOULD 告警；引擎自身不推断断裂，仅消费此标记。
    regime_break: bool = False

    # ---- O-双价格维度量价校验（出厂价/官方价 vs 市场成交价 · 2026-08-17）----
    # 提供 dual_price_data（含 records：factory_price/market_price 及对应 volume）时，
    # 引擎在消费 volume/price 前先跑 dual_price_analyzer.analyze_dual_price：
    #   · 出厂价↑+市场价↓ → 自动把 volume/price 重写为【市场成交价侧】并打「价格传导失效」告警
    #     （严禁沿用"出厂价涨=量缩价升看涨"误判）；
    #   · 其余情形以市场成交价侧为框架主量价（出厂价侧仅作传导校验维度），
    #     仅在 verdict.dual_price_check 附加交叉校验信息，不阻断分析。
    # 不提供则完全不触发，向后兼容（既有的 sector/ETF 输入零回归）。
    dual_price_data: Dict[str, Any] = field(default_factory=dict)

    # ---- 便捷属性 ----
    @property
    def vp(self) -> str:
        return f"{self.volume}|{self.price}"

    @property
    def vp_label(self) -> str:
        return VP_LABEL.get(self.vp, self.vp)

    @property
    def has_price_down(self) -> bool:
        return self.price == "down"

    @property
    def is_vp_down_cost_deflation(self) -> bool:
        """量升价跌·成本通缩/以价换量 降档通道（§12.1b 2026-08-15 用户裁定）。
        满足才开放：量升 + 价跌 + 结构性增长 + 显式声明 price_decline_nature=cost_deflation。
        缺任一 → 仍走价跌硬否决（0%）。"""
        return (self.volume == "up" and self.has_price_down
                and self.demand == "growth"
                and self.price_decline_nature == "cost_deflation")

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "SectorInput":
        allowed = {f for f in SectorInput.__dataclass_fields__}          # type: ignore[attr-defined]
        unknown = set(d) - allowed
        if unknown:
            raise ValueError(f"未知输入字段（禁止自创字段夹带主观修正）: {sorted(unknown)}")
        # —— 行业周期量价硬闸门（2026-08-15）：任何把量价建立在 ETF 交易/指数
        # 点位/整体市场 口径上的输入，引擎直接拒绝产出裁决，杜绝 Flash/下游重犯。
        from gate_industry_cycle import validate_vp_industry_cycle
        g = validate_vp_industry_cycle(d)
        if not g.ok:
            raise ValueError("量价来源校验未通过(industry_cycle gate): " + "; ".join(g.errors))
        return SectorInput(**d)


# =============================================================================
# 输出契约
# =============================================================================
@dataclass
class Verdict:
    sector: str
    as_of: str
    status: str                       # OK / PENDING_VERIFICATION / REJECTED_SCOPE / REJECTED_FRAMEWORK
    params_version: str
    params_fingerprint: str
    engine_version: str
    valuation_anchor: str = ""       # 实际使用的估值主锚（PB/PE/BLEND）
    valuation_check: Dict[str, Any] = field(default_factory=dict)  # 替代原 pb_check，锚感知
    valuation_check_meta: Dict[str, Any] = field(default_factory=dict)  # 2026-09-07 锚来源留痕（declared/auto-classified + 权威分类值）
    gates: Dict[str, Any] = field(default_factory=dict)            # 双源闸 + finalgate 结果

    demand_check: Dict[str, Any] = field(default_factory=dict)
    volume_price: Dict[str, Any] = field(default_factory=dict)
    forward_looking: Dict[str, Any] = field(default_factory=dict)
    pb_check: Dict[str, Any] = field(default_factory=dict)
    imagination: Dict[str, Any] = field(default_factory=dict)
    framework_position: str = ""
    suggested_position: str = ""
    catalysts: List[str] = field(default_factory=list)

    win_rate: Optional[int] = None
    win_rate_label: str = ""
    # L-1（2026-10-25）：胜率来源模式水印——顶层可见，杜绝「把六档表回退当量化胜率用」。
    # 取值：quant=五维量化 / table=六档表 / downgrade_quant / downgrade_table /
    #       channel=左侧通道（无胜率数值）/ rejected=框架否决（胜率=0）/ na=不适用。
    win_rate_mode: str = ""
    odds: Dict[str, Any] = field(default_factory=dict)
    ev: Optional[Dict[str, Any]] = None       # §EV 三档价 + EV中/乐/尾部（参照 XYD-earnings-analysis）
    p_star: Optional[Dict[str, Any]] = None   # P* 风险调整买点（2026-09-08 · 只读；None=未产出）
    conviction_score: Optional[float] = None  # O10 跨板块可比风险预算 = 仓位 × 胜率/100
    conviction_cap: float = 0.60              # O10 组合层总上限（跨板块 Σconviction 须≤此）
    tail_risk: Dict[str, Any] = field(default_factory=dict)
    quadrant: str = ""
    left_side_channel: bool = False
    value_trap: bool = False
    forced_reduce: bool = False
    buy_side: str = ""                          # §三bis 买点侧别：左侧(左侧布局)/右侧(确认买点)/未标注
    prosperity_check: Dict[str, Any] = field(default_factory=dict)  # §三bis 景气环比门槛校验

    hard_signals: List[Dict[str, Any]] = field(default_factory=list)
    execution_notes: List[str] = field(default_factory=list)
    blockers: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    checklist: Dict[str, bool] = field(default_factory=dict)
    dual_price_check: Optional[Dict[str, Any]] = None   # O-双价格维度校验结果（None=未触发）
    catalyst_check: Optional[Dict[str, Any]] = None     # F3 催化剂窗口校验（None=纯老格式/无催化剂）
    forward_check: Optional[Dict[str, Any]] = None      # F2 前瞻方向证据链校验（恒挂；MUST/SHOULD 由 params 档位定）
    trace: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


ENGINE_VERSION = "1.8.0"


# =============================================================================
# 工具函数
# =============================================================================
def percentile(vals: List[float], q: float) -> Optional[float]:
    """线性插值百分位（与 numpy.percentile 默认一致）。真实分位法，非 min~max 插值。"""
    if not vals:
        return None
    s = sorted(vals)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (q / 100.0)
    lo, hi = int(math.floor(k)), int(math.ceil(k))
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def rank_pct(vals: List[float], x: float) -> Optional[float]:
    if not vals:
        return None
    return 100.0 * sum(1 for v in vals if v <= x) / len(vals)


def _days_between(a: str, b: str) -> Optional[int]:
    try:
        da = datetime.strptime(a, "%Y-%m-%d").date()
        db = datetime.strptime(b, "%Y-%m-%d").date()
        return (db - da).days
    except Exception:
        return None


def _pct_diff(a: float, b: float) -> float:
    if b == 0:
        return float("inf")
    return abs(a - b) / abs(b) * 100.0


# =============================================================================
# 纪律层引擎
# =============================================================================
class DisciplineEngine:
    def __init__(self, params: Optional[Dict[str, Any]] = None):
        self.P = params or PARAMS
        from guards import params_fingerprint  # 延迟导入，避免循环
        self.fingerprint = params_fingerprint(self.P)
        self._win_rate_mode = "na"   # L-1：胜率模式水印（_win_rate 各返回点覆写）

    # ---------- trace ----------
    @staticmethod
    def _t(trace: List[Dict[str, Any]], rule: str, section: str, level: str,
           triggered: bool, note: str = "", delta: Optional[float] = None) -> None:
        trace.append({"rule": rule, "section": section, "level": level,
                      "triggered": triggered, "delta": delta, "note": note})

    # ---------- 主流程 ----------
    def run(self, inp: SectorInput) -> Verdict:
        P, G = self.P, self.P["gates"]
        trace: List[Dict[str, Any]] = []
        blockers: List[str] = []
        warnings: List[str] = []

        v = Verdict(sector=inp.sector, as_of=inp.as_of, status="OK",
                    params_version=P["version"], params_fingerprint=self.fingerprint,
                    engine_version=ENGINE_VERSION)

        # ---------- O-估值主锚自动分类（2026-09-07 · 根治"批量静默默认PB"）----------
        # SKILL.md 📐：分析赔率前必须先定主锚，**严禁默认 PB**。输入未显式声明锚时，
        # 由 valuation_anchor.classify()（权威分类器·精选表+规则回退）按
        # sector/index_code/index_name 自动推断；显式声明则尊重声明，但与权威分类
        # 冲突时打 SHOULD 告警（分析师可有意覆盖，如已备齐双锚全套数据，但须留痕）。
        try:
            from valuation_anchor import classify as _va_classify
            _ar = _va_classify(inp.sector, inp.index_code or "", inp.index_name or "")
        except Exception as _e:  # 分类器自身异常：才允许保守回退 PB 并留痕
            _ar = None
            warnings.append(f"[锚分类] valuation_anchor.classify 异常({_e})→保守回退 PB（请排查模块）")
        if not (inp.valuation_anchor or "").strip():
            inp.valuation_anchor = _ar.anchor if _ar else "PB"
            self._t(trace, "估值主锚自动分类", "📐 valuation_anchor", "MUST", True,
                    f"输入未声明锚→自动推断={inp.valuation_anchor}"
                    + (f"（{'命中『' + _ar.matched + '』' if _ar.matched else '规则回退'}）{_ar.reason}" if _ar else ""))
            _anchor_source = "auto-classified"
        else:
            _anchor_source = "declared"
            if _ar is not None and _ar.anchor != inp.valuation_anchor:
                warnings.append(
                    f"[锚交叉·SHOULD] 声明主锚={inp.valuation_anchor} 与权威分类={_ar.anchor}"
                    f"（{'命中『' + _ar.matched + '』' if _ar.matched else '规则回退'}）不一致；"
                    f"若为有意覆盖（已备齐 {_ar.anchor} 锚双源+手算全套）请留痕说明，否则按权威分类重跑。")
                self._t(trace, "估值主锚声明交叉", "📐 valuation_anchor", "SHOULD", True,
                        f"声明={inp.valuation_anchor} vs 权威={_ar.anchor}")
        v.valuation_check_meta = {"anchor_source": _anchor_source,
                                  "authoritative_anchor": _ar.anchor if _ar else None}
        # ---------- O-未填即错（2026-09-07 · 根治"漏填判断字段静默吃默认值"）----------
        # 审议 RC1：demand 旧默认="growth"（最乐观档）、volume/price/forward 旧默认="flat"——
        # LLM 漏填时引擎静默按默认值出完整裁决（09-07 胜率 70→45 假象事故根因现场）。
        # 新口径：判断字段缺失 → [MUST] 拦截（status=PENDING_VERIFICATION、仓位归零）；
        # 内部仅映射保守值继续计算防 crash（结论不得用于决策）。
        _judg_specs = (("demand", inp.demand, "cyclical"), ("volume", inp.volume, "flat"),
                       ("price", inp.price, "flat"), ("forward", inp.forward, "flat"),
                       ("imagination", inp.imagination, "B"))
        _missing_judg = [n for n, val, _safe in _judg_specs if not str(val or "").strip()]
        if _missing_judg:
            for _n, _val, _safe in _judg_specs:
                if not str(_val or "").strip():
                    setattr(inp, _n, _safe)
            blockers.append("[MUST] 判断字段缺失（未填即错 2026-09-07）：" + "、".join(_missing_judg)
                            + "——禁止以默认值静默出裁决，须由 LLM 带判断依据补齐后重跑（analyze_etf.py FILLS 预检可提前拦截）")
            self._t(trace, "判断字段缺失拦截", "填表契约·未填即错", "MUST", True,
                    f"缺失={_missing_judg}（旧版此处静默吃默认值 demand=growth / 量价=flat）")
        # ---------- Gate A：适用边界（§零 0.2）----------
        if inp.asset_class in G["rejected_asset_class"]:
            self._t(trace, "适用边界否决", "§零 0.2", "MUST", True,
                    f"asset_class={inp.asset_class} 属宽基/商品/债券/货币，非行业供需框架适用范围")
            v.status = "REJECTED_SCOPE"
            v.framework_position = "不适用本框架"
            v.suggested_position = "0%"
            v.win_rate, v.win_rate_label = None, "不适用"
            v.win_rate_mode = "na"
            v.trace = trace
            v.blockers = [f"[MUST] 适用边界：{inp.asset_class} 须走其他框架"
                          f"（宽基/商品/债券/货币 ETF 已被框架明确排除，§零 0.2）"]
            v.checklist = {"⑧适用边界": False}
            return v
        if inp.asset_class not in G["allowed_asset_class"]:
            blockers.append(f"[MUST] asset_class 非法：{inp.asset_class}（应为 sector/theme）")
        self._t(trace, "适用边界", "§零 0.2", "MUST", False, f"asset_class={inp.asset_class} 通过")

        # ---------- O1 量化输入契约（2026-08-16）：D1-D5 八字段从 quant_raw 确定性派生 ----------
        if _enforce_quant_contract is not None:
            _enforce_quant_contract(inp, warnings, trace)

        # ---------- O9 制度周期断裂检测（2026-08-16 · 关 V12）：早设 _regime_use_10y 供 _eff_pct/_ev 消费 ----------
        _use_10y, _regime_reason = self._regime_check(inp, trace, warnings)
        try:
            inp._regime_use_10y = _use_10y  # type: ignore[attr-defined]
        except Exception:
            pass

        # ---------- O-双价格维度量价校验（2026-08-17）：消费 volume/price 前挂入 ----------
        # 若输入含 dual_price_data.records，先跑双价格分析，出厂价↑+市场价↓ 时自动把
        # 框架 volume/price 重写为【市场成交价侧】并打「价格传导失效」告警（防量缩价升误判）。
        # 其余情形以市场侧为主输入；无论是否改写，结果都挂到 verdict.dual_price_check。
        dp_check = self._dual_price_precheck(inp, trace, warnings)

        # ---------- O-双价格 Fix A/C/D（2026-08-18 · 修复512690酒ETF误判根因）----------
        # Fix C/D：量价方向 vs 行业价口径文本一致性 + 分歧行业价格溯源校验（捕获文本已写企稳却填up）
        self._verify_price_metric_consistency(inp, trace, warnings, blockers)
        # Fix A：分歧行业（出厂/官方价 vs 市场成交价 分层）+ 声明价格上行 → 必须提供 dual_price_data
        # 方向门（2026-08-18 修 I8）：仅 price=up 存在『出厂提价被误读为价升→虚高』风险，故硬阻断；
        # price=down/flat 已在保守侧，降为 SHOULD 告警，避免误伤如实申报价跌的分析（电池ETF原型）。
        if dp_check is None and self._dual_price_mandatory(inp):
            blockers.append(
                "[MUST] 双价格溯源缺失：本行业存在 出厂/官方价 vs 市场成交价 分层"
                "（白酒/食品饮料/医药/地产/猪周期/农业/煤炭/有色/锂/能源/半导体等），"
                "且已声明行业价『上行』→ 必须提供 dual_price_data（含 records 分层序列），"
                "否则量价不可信、结论标『待核验』（防出厂价提价被误读为量缩价升看涨）。")
            warnings.append("⚠️ 双价格溯源缺失：分歧行业声明价升但未提供 dual_price_data，量价沿用单源、"
                            "可能为误判源头，请补 factory_price/market_price 分层序列后重跑。")
            self._t(trace, "双价格强制溯源(方向门·上行)", "O-双价格/A", "MUST", True,
                    f"分歧行业+price={inp.price} 且无 dual_price_data → 硬阻断")
        elif dp_check is None and self._sector_needs_dual_price(inp):
            warnings.append(
                "[SHOULD] 双价格溯源缺失(保守侧·B4已知盲点): 本行业存在 出厂/官方价 vs 市场成交价 分层，"
                f"但当前声明行业价『{inp.price}』（非上行、已在保守侧、无虚高风险）→ 不阻断。"
                "⚠️ 保守侧盲点：若实际为出厂价主动降价向市场传导、却只填了市场侧 price=down 漏填 factory 序列，"
                "引擎会保守误判、可能漏判潜在机会（非亏损风险）。建议补 factory_price/market_price 分层序列确认口径。")
            self._t(trace, "双价格强制溯源(方向门·非上行·保守侧盲点)", "O-双价格/A", "SHOULD", True,
                    f"分歧行业+price={inp.price} → 降级告警(保守侧漏机会风险已标注)")

        # ---------- Gate B：数据合规（§零 0.3 / 0.4 / §四 交叉验证）----------
        chk = self._compliance(inp, trace, blockers, warnings)

        # ---------- 五档赔率参考线 ----------
        odds, tail = self._odds(inp, trace, blockers, warnings)
        v.odds, v.tail_risk = odds, tail

        # ---------- §三bis 景气环比门槛（买点侧别 + 价值陷阱校验）----------
        prosp = self._prosperity_gate(inp, trace, warnings)

        # ---------- F4 见底信号留痕 / F2 前瞻证据链 / F3 催化剂窗口（2026-09-04 前瞻修复）----------
        # 须在状态裁决前调用：F4 矛盾与 F2-MUST 档缺失均为 [MUST] blocker → PENDING_VERIFICATION
        self._bottom_signal_check(inp, trace, warnings, blockers)
        fwd_check = self._forward_evidence_check(inp, trace, warnings, blockers)
        cat_check = self._catalyst_struct_check(inp, trace, warnings)

        # ---------- §8.1 硬信号必勾 ----------
        hard, value_trap, forced_reduce = self._hard_signals(inp, tail, prosp, trace, warnings)
        v.hard_signals, v.value_trap, v.forced_reduce = hard, value_trap, forced_reduce
        v.buy_side = prosp["buy_side"]
        v.prosperity_check = {
            "provided": prosp["provided"],
            "prosperity_trend": inp.prosperity_trend,
            "prosperity_mom_pct": inp.prosperity_mom_pct,
            "mom": prosp["mom"],
            "buy_side": prosp["buy_side"],
            "left_ok": prosp["left_ok"],
            "right_ok": prosp["right_ok"],
            "is_value_trap": prosp["is_value_trap"],
            "note": prosp["note"],
        }

        # ---------- §12.1 胜率 ----------
        win, win_label, channel, rejected = self._win_rate(
            inp, value_trap, forced_reduce, tail, trace, warnings)
        v.win_rate, v.win_rate_label, v.left_side_channel = win, win_label, channel
        v.win_rate_mode = self._win_rate_mode   # L-1：胜率来源模式水印（顶层可见）

        # ---------- §EV 三档价 + EV 中/乐/尾部（基于主锚分位带，参照盈利面技能）----------
        v.ev = self._ev(inp, odds, win, trace, warnings)

        # ---------- §P* 风险调整买点（2026-09-08 · 只读披露，不参与仓位/回测）----------
        v.p_star = self._p_star(inp, v.ev, win, trace)

        # ---------- §五 象限 ----------
        eff = self._eff_pct(inp)
        odds_score = None if eff is None else round(100.0 - eff, 2)
        v.quadrant = self._quadrant(win, odds_score, channel, value_trap, rejected)

        # ---------- §十 仓位 + §12.5.2 尾部纪律落地 ----------
        pos = self._position(inp, win, channel, rejected, forced_reduce, trace, warnings)
        pos, exec_notes = self._tail_discipline(pos, tail, trace)
        v.execution_notes = exec_notes

        # ---------- 状态裁决 ----------
        if rejected:
            v.status = "REJECTED_FRAMEWORK"
        if blockers:
            v.status = "PENDING_VERIFICATION"
            pos = 0.0
            warnings.append("存在 [MUST] 未通过项 → 结论标『待核验』，不得用于决策（§十三）")

        # ---------- 八大字段 ----------
        erosion_confirmed = (inp.demand == "erosion"
                            and inp.erosion_signals >= G["erosion_signals_confirm_min"])
        v.demand_check = {
            "pass": inp.demand != "collapse",
            "type": DEMAND_LABEL.get(inp.demand, inp.demand),
            "erosion_confirmed": erosion_confirmed,
            "erosion_signals": inp.erosion_signals,
            "reason": "结构性塌方→直接排除" if inp.demand == "collapse"
                      else ("需求慢性侵蚀确认（≥2信号）→想象力降级+价值陷阱监测" if erosion_confirmed
                            else "需求前提通过"),
        }
        v.volume_price = {
            "current_volume": {"up": "升", "flat": "稳", "down": "缩"}.get(inp.volume, inp.volume),
            "current_price": {"up": "升", "flat": "稳", "down": "跌"}.get(inp.price, inp.price),
            "combination": inp.vp_label,
            "double_click_quality": (
                "量升价跌·降档通道（谨慎参与）" if inp.is_vp_down_cost_deflation else
                f"{VP_STARS.get(inp.vp, 0)}星" if VP_STARS.get(inp.vp) else "不可参与"),
        }
        # O-双价格维度校验结果（None=未提供 dual_price_data；否则含原始/有效量价与传导状态）
        v.dual_price_check = dp_check
        v.forward_looking = {
            "direction": {"up": "↑", "flat": "→", "down": "↓"}.get(inp.forward, "→"),
            "bottom_signals": inp.bottom_signals,
            "matrix_match": ("左侧等待布局通道（6信号≥3）" if channel else
                             "双击升级" if (inp.vp in ("up|flat", "up|up") and inp.forward == "up") else
                             "恶化/收窄→减仓" if inp.forward == "down" else "维持"),
        }
        # F4：仅当逐项留痕提供时写入（缺省不写键，保持旧输出逐键一致）
        if inp.bottom_signal_detail:
            v.forward_looking["bottom_signal_detail"] = inp.bottom_signal_detail
        v.valuation_anchor = inp.valuation_anchor or "PB"
        v.valuation_check = {
            "anchor": inp.valuation_anchor or "PB",
            "current_pb": inp.pb_now,
            "pb_manual": inp.pb_manual,
            "pb_cross": chk["pb_cross"],
            "current_pe": inp.pe_now,
            "pe_manual": inp.pe_manual,
            "pe_cross": chk["pe_cross"],
            "eff_percentile": eff,
            "pb_percentile": inp.pb_pct,
            "pe_percentile": inp.pe_pct,
            "pb_percentile_period": inp.pb_pct_period or "未标注",
            "pe_percentile_period": inp.pe_pct_period or "未标注",
            "pb_percentile_10y": inp.pb_pct_10y,
            "pe_percentile_10y": inp.pe_pct_10y,
            "dual_period": chk["dual_period"],
            "odds_score": odds_score,
            "odds_rating": self._odds_rating(eff),
        }
        coef = P["position"]["imagination_coef"].get(inp.imagination, 1.0)
        v.imagination = {"grade": inp.imagination, "coefficient": coef,
                         "downgraded_by_erosion": erosion_confirmed}
        v.framework_position = ("不适用本框架" if v.status == "REJECTED_SCOPE" else
                                "待核验" if v.status == "PENDING_VERIFICATION" else
                                "回避区（框架否决）" if rejected else v.quadrant)
        v.suggested_position = f"{round(pos * 100, 1)}%"
        # ---------- O10 组合层 conviction cap（2026-08-16 · 关 V9/V10 文档缺口）----------
        # 胜率(W)与仓位(pos)是相互独立的两套杠杆：W 影响 EV，pos 由 估值分位×想象力×量价×前瞻 决定，
        # 二者不相乘。故引入「风险预算」conviction_score = pos × W/100，作为跨板块可比的加权暴露度，
        # 用户多板块加总时须保证 Σ conviction_score ≤ position.conviction_cap（默认 60%），防单板块高胜率高仓位叠加超配。
        Pp = self.P["position"]
        conv_cap = float(Pp.get("conviction_cap", 0.60))
        conv_thr = float(Pp.get("single_high_conviction_threshold", 0.30))
        conv = round(pos * (win / 100.0 if win else 0.0), 4)
        v.conviction_score = conv
        v.conviction_cap = conv_cap
        if pos >= conv_thr:
            warnings.append(
                f"[O10] 单板块仓位 {pos:.1%} ≥ 高确信阈值 {conv_thr:.0%} → 跨板块加总须保证"
                f" Σconviction(=仓位×胜率/100) ≤ 组合总上限 {conv_cap:.0%}，避免多板块高胜率高仓位叠加超配")
            self._t(trace, "组合层 conviction 提醒", "§十/O10", "SHOULD", True,
                    f"conviction={conv:.3f}（<组合总上限 {conv_cap:.0%} 须多板块约束）")
        if cat_check is not None:
            # F3：结构化催化剂 → 展示串「名称（N月,方向）」；纯老格式走 else 原样输出（零漂移）
            _disp = []
            for p in cat_check["items"]:
                s = p["name"]
                if p["window_months"] is not None or p["direction"]:
                    s += "（"
                    if p["window_months"] is not None:
                        s += f"{p['window_months']:g}月"
                    if p["direction"]:
                        s += ("，" if p["window_months"] is not None else "") + ("上行" if p["direction"] == "up" else "下行")
                    s += "）"
                _disp.append(s)
            v.catalysts = _disp
            v.catalyst_check = cat_check
        else:
            v.catalysts = list(inp.catalysts)
        v.forward_check = fwd_check   # F2：前瞻方向证据链校验结果（恒挂，evidence_count 可为 0）
        v.blockers, v.warnings, v.trace = blockers, warnings, trace
        v.checklist = self._checklist(inp, chk, odds, tail, win, channel, hard, blockers)
        # O-双价格传导校验（2026-08-17）：未提供=N/A 通过；提供则必须已收敛到市场侧（防量缩价升误判）
        if dp_check is None:
            # Fix B（2026-08-18）：分歧行业缺失溯源即判失败；非分歧行业无双价格层→保持通过。
            # 口径必须与 Fix A 硬阻断完全一致（方向门）：否则 price=down 的分歧行业会 ⑪=False
            # 却无 blocker，终闸 ⑦ 仍 FAIL → 结论被静默打成待核验（I8 电池ETF 直接死因）。
            v.checklist["⑪双价格传导校验"] = not self._dual_price_mandatory(inp)
        elif dp_check.get("cross", {}).get("is_factory_up_market_down"):
            eff = dp_check.get("_effective_volume_price", {})
            mkt = dp_check.get("market", {})
            v.checklist["⑪双价格传导校验"] = (
                eff.get("price") == mkt.get("price_trend")
                and eff.get("volume") == mkt.get("volume_trend"))
        else:
            v.checklist["⑪双价格传导校验"] = True
        # —— 三道闸之②：数据双源验证 gate（按活跃主锚）——
        from gate_dual_source import validate_dual_source
        ds = validate_dual_source(inp.__dict__)
        v.gates["dual_source"] = ds.to_dict()
        if not ds.ok:
            for e in ds.errors:
                blockers.append(f"[GATE·双源] {e}")
            if v.status == "OK":
                v.status = "PENDING_VERIFICATION"
        # —— 三道闸之③：finalgate 终检（端到端一致性）——
        from gate_final import validate_final
        fg = validate_final(inp.__dict__, v.__dict__)
        v.gates["final"] = fg.to_dict()
        if not fg.ok:
            for e in fg.errors:
                blockers.append(f"[GATE·final] {e}")
            if v.status == "OK":
                v.status = "PENDING_VERIFICATION"
        return v

    # ---------- 数据合规 ----------
    def _compliance(self, inp: SectorInput, trace, blockers, warnings) -> Dict[str, Any]:
        G = self.P["gates"]
        out: Dict[str, Any] = {"pb_cross": "未校验", "dual_period": "n/a",
                               "fresh": False, "period_ok": False, "index_ok": False}

        # 指数代码（§六 Step0 / §一 输入归一化）
        if not inp.index_code:
            blockers.append("[MUST] 未查明跟踪指数代码，PB 分位口径无法锁定（§六 Step0 纪律4）")
            self._t(trace, "指数代码匹配", "§六 Step0", "MUST", True, "index_code 缺失")
        else:
            out["index_ok"] = True
            self._t(trace, "指数代码匹配", "§六 Step0", "MUST", False, f"index_code={inp.index_code}")

        # 跨境加权方式（§零 0.2）
        if inp.is_cross_border and inp.weighting not in ("mktcap", "equal"):
            blockers.append("[MUST] 跨境/港股指数未标注加权方式（市值加权/等权分位可差 30pp+）")
            self._t(trace, "跨境加权标注", "§零 0.2", "MUST", True, "weighting 缺失")

        # 新鲜度（§零 0.3）
        today = inp.today or date.today().isoformat()
        gap = _days_between(inp.as_of, today)
        if gap is None:
            blockers.append("[MUST] as_of 日期格式非法（应 YYYY-MM-DD）")
        elif gap < 0:
            blockers.append(f"[MUST] as_of={inp.as_of} 晚于当前日期 {today} → 疑似未来数据（未来函数）")
            self._t(trace, "未来数据检测", "guards", "MUST", True, "as_of > today")
        elif gap > G["freshness_max_days"]:
            blockers.append(f"[MUST] 数据过期 {gap} 天 (>{G['freshness_max_days']}) → 须重取（§零 0.3）")
            self._t(trace, "数据新鲜度门", "§零 0.3", "MUST", True, f"gap={gap}d")
        elif gap > G.get("freshness_warn_days", 7):
            warnings.append(f"[注意] 数据已 {gap} 天未更新（>{G.get('freshness_warn_days', 7)}天），"
                            "PB分位/动量等快变量可能已漂移，结论仅供参考，建议重取后再决策")
            out["fresh"] = True
            self._t(trace, "数据新鲜度预警", "§零 0.3", "SHOULD", True, f"gap={gap}d（偏旧）")
        else:
            out["fresh"] = True
            self._t(trace, "数据新鲜度门", "§零 0.3", "MUST", False, f"gap={gap}d")

        # —— 估值主锚分发（§估值主锚 · 2026-08-15）——
        anchor = (inp.valuation_anchor or "PB").upper()
        is_pb = anchor in ("PB", "BLEND")
        is_pe = anchor in ("PE", "BLEND")

        def _period_check(period_field, tag):
            per = (period_field or "").lower()
            if per in [x.lower() for x in G["percentile_period_forbidden"]]:
                blockers.append(f"[MUST] 使用全历史分位决策被禁止 → {tag} 须取 5年日频分位")
                self._t(trace, f"{tag}分位周期硬约束", "§零 0.4", "MUST", True, f"period={period_field}")
            elif per not in [x.lower() for x in G["percentile_period_allowed"]]:
                blockers.append(f"[MUST] {tag} 分位未标注周期（5y/10y）→ 视为待核验")
                self._t(trace, f"{tag}分位周期标注", "§零 0.4", "MUST", True, "period 缺失")
            else:
                out["period_ok"] = True
                if per != G["percentile_period_decision_primary"]:
                    warnings.append(f"主决策分位周期为 {period_field}，框架主依据是 5年日频（§四）")
                self._t(trace, f"{tag}分位周期标注", "§零 0.4", "MUST", False, f"period={period_field}")

        if is_pb:
            _period_check(inp.pb_pct_period, "PB")
        if is_pe:
            _period_check(inp.pe_pct_period, "PE")

        # 分位窗口长度（§8.1-6）
        if inp.pb_series_years is not None and inp.pb_series_years < G["percentile_window_min_years"]:
            warnings.append(f"[SHOULD] 分位样本仅 {inp.pb_series_years} 年 (<2年) → 分位失真，标『窗口不足』，可靠性低")
            self._t(trace, "分位窗口不足", "§8.1-6", "SHOULD", True, f"{inp.pb_series_years}y")

        # 频率
        if inp.pb_series_freq != "daily":
            warnings.append(f"[SHOULD] 分位频率={inp.pb_series_freq}，框架主口径为日频（§12.3）")

        # 字段警戒词（§四 交叉验证协议）：PB 与 PE 同扫
        for _lab, _tag in ((inp.pb_field_label, "PB"), (inp.pe_field_label, "PE")):
            label = _lab or ""
            hit = [k for k in self.P["guards"]["forbidden_field_keywords"] if k in label]
            if hit:
                blockers.append(f"[MUST] {_tag} 字段含警戒词 {hit} → 疑为行业均值/加权冒充所选指数，弃用该源")
                self._t(trace, f"{_tag} 字段警戒词", "§四 协议", "MUST", True, f"label={label}")

        # 双源 + 手算兜底（§四 协议 ①②③）：按锚分发
        ds = self._dual_source_cross(inp, trace, blockers, warnings)
        out["pb_cross"] = ds.get("pb_cross", "不适用(非PB/blend锚)")
        out["pe_cross"] = ds.get("pe_cross", "不适用(非PE/blend锚)")

        # 成分股离散度（§四 [MUST] 下钻）：仅 PB 相关锚
        if is_pb:
            d = inp.constituent_pb_dispersion_pp
            if d is not None and d > G["constituent_pb_dispersion_max_pp"] and not inp.dispersion_drilled:
                blockers.append(f"[MUST] 成分股 PB 离散度 {d}pp > 40pp 且未下钻子行业 → 一级行业 PB 失真")
                self._t(trace, "成分股离散度下钻", "§四", "MUST", True, f"{d}pp 未下钻")
            elif d is not None and d > G["constituent_pb_dispersion_max_pp"]:
                warnings.append(f"离散度 {d}pp 已下钻子行业，须在报告标注『原一级行业分位失真』")

        # 双周期交叉（§四 Step2）：按锚
        def _dual_period(pct, pct_10y, tag):
            if pct is not None and pct_10y is not None:
                a, b = pct, pct_10y
                if a < 30 and b < 30:
                    return f"{tag}双重低估（赔率极佳）"
                elif a > 70 and b > 70:
                    return f"{tag}双重高估（赔率极差）"
                elif abs(a - b) > 30:
                    warnings.append(f"{tag}分歧信号（差{round(abs(a - b), 1)}pp>30pp）→ 以5年为主，10年作风险提示")
                    return f"{tag}分歧信号（差{round(abs(a - b), 1)}pp>30pp）"
                return f"{tag}无显著分歧"
            return None
        dp_pb = _dual_period(inp.pb_pct, inp.pb_pct_10y, "PB") if is_pb else None
        dp_pe = _dual_period(inp.pe_pct, inp.pe_pct_10y, "PE") if is_pe else None
        if anchor == "BLEND":
            out["dual_period"] = f"{dp_pb}；{dp_pe}"
        else:
            out["dual_period"] = dp_pb or dp_pe or "n/a"
        return out

    def _dual_source_cross(self, inp: SectorInput, trace, blockers, warnings) -> Dict[str, str]:
        """按估值主锚做双源+手算校验（§四 协议 ①②③）。PB/PE 各自独立校验。"""
        G = self.P["gates"]
        anchor = (inp.valuation_anchor or "PB").upper()
        res: Dict[str, str] = {}

        def _check(now, manual, sources, series, field_tag):
            if now is None:
                blockers.append(f"[MUST] 缺少当前 {field_tag} → 赔率无法定位（§四）")
                return "缺失"
            if manual is None:
                blockers.append(f"[MUST] 缺少手算兜底 {field_tag}（指数点位÷对应分母）→ 无唯一基准（§四 协议①）")
                self._t(trace, f"{field_tag} 手算兜底", "§四 协议①", "MUST", True, "manual 缺失")
                return "未过校验"
            d_manual = _pct_diff(now, manual)
            if d_manual > G["pb_manual_max_diff_pct"]:
                blockers.append(f"[MUST] {field_tag} 与手算差 {d_manual:.1f}% > 20% → 字段口径错配，弃用该源（§四 协议②）")
                self._t(trace, f"{field_tag} 手算比对", "§四 协议②", "MUST", True, f"diff={d_manual:.1f}%")
                return f"手算不符({d_manual:.1f}%)"
            vals = [x for x in sources.values() if isinstance(x, (int, float))]
            if len(vals) < 2:
                warnings.append(f"[MUST→降级] {field_tag} 仅单源，未满足『≥2 独立源差异<5%』→ 标『待核验』（§四 协议②）")
                self._t(trace, f"{field_tag} 多源一致", "§四 协议②", "MUST", True, f"仅{len(vals)}源")
                blockers.append(f"[MUST] {field_tag} 未过双源交叉验证（至少 2 个独立源）")
                return f"单源(手算符合 {d_manual:.1f}%)"
            spread = _pct_diff(max(vals), min(vals))
            if spread > G["pb_cross_src_max_diff_pct"]:
                blockers.append(f"[MUST] 多源 {field_tag} 分歧 {spread:.1f}% > 5% → 不采信，须核对口径（§四 协议②④）")
                self._t(trace, f"{field_tag} 多源一致", "§四 协议②", "MUST", True, f"spread={spread:.1f}%")
                return f"多源分歧({spread:.1f}%)"
            if series:
                d_tail = _pct_diff(series[-1], manual)
                if d_tail > G["pb_manual_max_diff_pct"]:
                    blockers.append(f"[MUST] {field_tag} 分位序列末值与手算差 {d_tail:.1f}% → 序列口径错，须重取（§四 协议③）")
                    return f"序列口径不符({d_tail:.1f}%)"
            self._t(trace, f"{field_tag} 双源+手算校验", "§四 协议", "MUST", False,
                    f"手算差{d_manual:.1f}% / 多源差{spread:.1f}% 均达标")
            return f"双源一致(差{spread:.1f}%)+手算校验✓"

        if anchor in ("PB", "BLEND"):
            res["pb_cross"] = _check(inp.pb_now, inp.pb_manual, inp.pb_sources, inp.pb_series, "PB")
        if anchor in ("PE", "BLEND"):
            res["pe_cross"] = _check(inp.pe_now, inp.pe_manual, inp.pe_sources, inp.pe_series, "PE")
        return res

    # ---------- 五档赔率 + 极端情景（锚感知 · 2026-08-15）----------
    def _blend_weights(self, inp: "SectorInput") -> Dict[str, Any]:
        """O6：BLEND 自适应权重。命中 valuation_anchor.BLEND_WEIGHTS 用专用权重，
        否则回退 params.valuation_blend 全局默认（0.6/0.4）。关 V6。"""
        gdef = self.P.get("valuation_blend", {})
        dpe = gdef.get("pe_weight", 0.6)
        dpb = gdef.get("pb_weight", 0.4)
        if _blend_wf is not None:
            pe, pb, kw = _blend_wf(inp.sector, inp.index_code or "",
                                   inp.index_name or "", dpe, dpb)
            return {"pe_weight": pe, "pb_weight": pb, "matched": kw}
        return {"pe_weight": dpe, "pb_weight": dpb, "matched": ""}

    def _eff_pct(self, inp: "SectorInput") -> Optional[float]:
        """用于仓位分层/象限/价值陷阱的有效估值分位（BLEND 用 O6 自适应加权）。
        O9：若 _regime_use_10y 置位（5y 窗口未含完整周期 / 上游标记 regime_break 且 10y 可用），
        有效分位切换至 10y 口径（更稳健），避免 5y 单一牛熊被当常态。"""
        a = (inp.valuation_anchor or "PB").upper()
        use_10y = bool(getattr(inp, "_regime_use_10y", False))
        if a == "PE":
            return inp.pe_pct_10y if (use_10y and inp.pe_pct_10y is not None) else inp.pe_pct
        if a == "BLEND":
            w = self._blend_weights(inp)
            pe = (inp.pe_pct_10y if (use_10y and inp.pe_pct_10y is not None) else inp.pe_pct) or 0.0
            pb = (inp.pb_pct_10y if (use_10y and inp.pb_pct_10y is not None) else inp.pb_pct) or 0.0
            return round(w["pe_weight"] * pe + w["pb_weight"] * pb, 2)
        return inp.pb_pct_10y if (use_10y and inp.pb_pct_10y is not None) else inp.pb_pct

    # ---------- O9 制度周期断裂检测（2026-08-16 · 关 V12）----------
    def _regime_check(self, inp: "SectorInput", trace, warnings) -> Tuple[bool, str]:
        """5y 窗口须含 ≥1 完整牛熊周期；否则应切 10y 口径。仅 SHOULD 级（不阻断）：
        - 上游标记 regime_break（政策/指数重构/并购等结构性断裂）→ 切 10y
        - 5y 窗口年数 < regime_full_cycle_min_years（默认 4y）→ 窗口未含完整周期，切 10y
        10y 分位缺失则维持 5y 并降级告警（不阻断分析）。"""
        G = self.P["gates"]
        if not bool(G.get("regime_break_use_10y", True)):
            return False, ""
        reason = ""
        if inp.regime_break:
            reason = "上游标记 regime_break（结构性断裂：政策/指数重构/并购等）"
        elif (inp.pb_pct_period or "5y") == "5y" and inp.pb_series_years is not None \
                and inp.pb_series_years < float(G.get("regime_full_cycle_min_years", 4.0)):
            reason = (f"5y 窗口仅 {inp.pb_series_years:.0f} 年 < 完整周期阈值 "
                      f"{float(G.get('regime_full_cycle_min_years', 4.0)):.0f} 年 → 未含完整牛熊周期")
        if not reason:
            return False, ""
        a = (inp.valuation_anchor or "PB").upper()
        has_10y = (inp.pb_pct_10y is not None) if a in ("PB", "BLEND") else (inp.pe_pct_10y is not None)
        if has_10y:
            self._t(trace, "制度周期断裂检测(O9)", "§EV/O9", "SHOULD", True,
                    f"{reason} → 有效分位切换至 10y（更稳健）")
            warnings.append(f"[O9] {reason} → 有效分位已切换至 10y 口径（更稳健），"
                            "EV/分位结论为该 10y 制度周期条件下，建议与 5y 交叉验证")
            return True, reason
        self._t(trace, "制度周期断裂检测(O9)", "§EV/O9", "SHOULD", True,
                f"{reason} → 但 10y 分位缺失，维持 5y 并降级告警")
        warnings.append(f"[O9] {reason} → 但 10y 分位缺失，维持 5y 口径"
                        "（结论为该窗口条件下，跨周期可靠性降低）")
        return False, reason

    # ---------- O-双价格维度量价校验（2026-08-17）：消费 volume/price 前挂入 ----------
    def _dual_price_precheck(self, inp: "SectorInput", trace, warnings
                             ) -> Optional[Dict[str, Any]]:
        """若输入含 dual_price_data.records，先跑 dual_price_analyzer.analyze_dual_price，
        在引擎消费 volume/price 之前把框架量价统一收敛到【市场成交价侧】：

          · 出厂价↑ + 市场价↓（价格传导失效）→ 自动改写 volume/price 为市场侧
            + 打「价格传导失效」MUST 告警（严禁沿用『出厂价涨=量缩价升看涨』误判）；
          · 其余分歧/对齐 → 同样以市场侧为框架主量价（出厂价侧仅作传导校验维度），
            分歧时打 SHOULD 提示，对齐时仅留痕。

        返回 dict 挂 verdict.dual_price_check（始终含 原始输入量价 / 有效量价 / 传导状态），
        便于事后审计；模块缺失或执行失败则降级跳过（不阻断原有分析）。"""
        dd = getattr(inp, "dual_price_data", None)
        if not isinstance(dd, dict) or not dd.get("records"):
            return None
        try:
            from dual_price_analyzer import analyze_dual_price
        except Exception as e:  # 模块缺失降级
            warnings.append(f"[O-双价格] dual_price_analyzer 模块缺失，跳过校验：{e}")
            return None
        try:
            res = analyze_dual_price(dd)
        except Exception as e:  # 执行失败降级
            warnings.append(f"[O-双价格] 分析执行失败，跳过：{e}")
            return None

        rd = res.to_dict()
        cross = rd.get("cross", {})
        vpf = rd.get("volume_price_for_framework", {}).get("integrated_volume_price", {})
        orig_v, orig_p = inp.volume, inp.price
        new_v, new_p = vpf.get("volume"), vpf.get("price")
        rd["_original_volume_price"] = {"volume": orig_v, "price": orig_p}
        rd["_effective_volume_price"] = {"volume": new_v, "price": new_p}

        # 框架主量价统一收敛到【市场成交价侧】（dual_price_analyzer 已定 market 侧为 integrated basis）
        # 契约修复（2026-08-31）：analyzer 允许 volume 缺失(→'unknown')，但 _win_rate 拒绝 unknown 量价组合
        # → 价格层照常收敛市场侧；量层缺失时回退输入声明的行业量方向（行业量口径独立于价格分层，
        #   出厂/市场价分层不影响量端判定），留痕 volume_fallback 供审计。
        if new_p is not None:
            inp.price = new_p
        if new_v in (None, "unknown"):
            inp.volume = orig_v or inp.volume
            rd["_effective_volume_price"]["volume"] = inp.volume
            rd["_effective_volume_price"]["volume_fallback"] = "input_declared(市场侧量缺失→回退行业量口径)"
        else:
            inp.volume = new_v

        if cross.get("is_factory_up_market_down"):
            self._t(trace, "双价格校验·价格传导失效改写", "O-双价格", "MUST", True,
                    f"出厂价↑+市场价↓ → 框架量价由({orig_v}|{orig_p})重写为市场侧({new_v}|{new_p})")
            warnings.append(
                "⚠️ 价格传导失效：出厂价/官方价↑但市场成交价↓（渠道承压/倒挂风险），"
                "框架量价已自动重写为【市场成交价侧】，严禁沿用『出厂价涨=量缩价升看涨』误判。")
        elif cross.get("divergence_flag"):
            self._t(trace, "双价格校验·采用市场侧主量价", "O-双价格", "MUST", False,
                    f"出厂/市场方向分歧（{cross.get('transmission_status')}）"
                    f"→ 框架量价取市场侧({new_v}|{new_p})")
            warnings.append(
                f"[O-双价格] 出厂价与市场价方向分歧（{cross.get('transmission_status')}），"
                "已按【市场成交价侧】量价为主输入；出厂价侧仅作传导校验。")
        else:
            self._t(trace, "双价格校验·与输入一致", "O-双价格", "MUST", False,
                    f"市场侧量价({new_v}|{new_p})成为框架主量价")
        return rd

    # ---- O-双价格 强制溯源 + 一致性校验（2026-08-18 · 修复512690酒ETF误判根因）----
    def _sector_needs_dual_price(self, inp: "SectorInput") -> bool:
        """判断行业是否存在 出厂/官方价 vs 市场成交价 分层（双价格层）。
        基于 sector/index_name/index_code/行业价量口径 的关键词匹配，读取 params.gates.divergence_sectors.keywords。
        用关键词而非自由文本，避免 512690『中证酒』曾因无『白酒』二字漏检。"""
        kws = (self.P.get("gates", {}).get("divergence_sectors", {}) or {}).get("keywords", [])
        if not kws:
            return False
        hay = " ".join([
            str(inp.sector or ""), str(inp.index_name or ""), str(inp.index_code or ""),
            str(inp.industry_price_metric or ""), str(inp.industry_volume_metric or "")
        ])
        return any(k in hay for k in kws)

    def _dual_price_mandatory(self, inp: "SectorInput") -> bool:
        """分歧行业中，双价格溯源何时升为 MUST（硬阻断）—— 方向门（2026-08-18 修 I8 过度拦截）。

        误判风险是**单向**的：
          · price=up  → 可能把『出厂/官方价提价』误读为行业价升，**虚高**胜率/EV（512690 酒ETF 原型）
                        → MUST 硬阻断，必须提供 dual_price_data；
          · price=down/flat → 已站在保守侧。即便出厂/市场存在分歧，最坏是结论**偏保守**
                        （框架偏好保守），不构成高估风险 → 降为 SHOULD 告警，不阻断。
        否则会误伤『电池ETF：电芯价跌、锂价成本下行传导』这类本就如实申报价跌的正常分析。"""
        if not self._sector_needs_dual_price(inp):
            return False
        return (inp.price == "up") or (getattr(inp, "industry_price_trend", None) == "up")

    def _verify_price_metric_consistency(self, inp: "SectorInput", trace, warnings, blockers) -> None:
        """Fix C/D（2026-08-18）：量价方向 vs 行业价口径文本一致性 + 分歧行业价格溯源校验。
        捕获 512690 模式：文本已写『批价企稳/回落』却填 price=up；或分歧行业 price≠flat 却未带具体价源。"""
        FLAT_DOWN = ["企稳", "持平", "横盘", "平稳", "回落", "下行", "阴跌", "倒挂", "下跌",
                     "走低", "弱势", "探底", "磨底", "底部震荡", "环比降", "同比降", "跌"]
        SRC_KW = ["批价", "现货", "长协", "出厂", "终端", "招标", "挂牌", "成交价", "ASP", "渠道价"]
        needs = self._sector_needs_dual_price(inp)
        declared_up = (inp.price == "up") or (inp.industry_price_trend == "up")
        metric = inp.industry_price_metric or ""

        # C1：方向矛盾——声明上行但口径文本含企稳/回落/下行词（疑似只看出厂/官方价单边）
        if declared_up and metric:
            hit = [w for w in FLAT_DOWN if w in metric]
            if hit:
                msg = (f"量价方向矛盾：price={inp.price}/industry_price_trend={inp.industry_price_trend} "
                       f"声明『上行』，但行业价口径写『{hit}』（{metric[:40]}）→ 疑似只看出厂/官方价单边、"
                       f"未核对市场成交价侧。请补 dual_price_data 或修正方向。")
                if needs:
                    blockers.append("[MUST] " + msg + "（分歧行业方向矛盾且未提供双价格溯源，结论标待核验）")
                else:
                    warnings.append("⚠️ " + msg)
                self._t(trace, "量价方向一致性校验", "O-双价格/C", "MUST" if needs else "SHOULD",
                        bool(needs), msg)

        # D1：分歧行业 price!=flat 必须带具体价源口径（防模糊价源导致误判）
        if needs and inp.price != "flat":
            if not metric:
                blockers.append("[MUST] 分歧行业 price≠flat 必须提供 industry_price_metric 具体价源"
                                "（批价/现货/长协/出厂/终端/招标/挂牌），否则量价不可信、结论标待核验。")
                self._t(trace, "价格溯源校验", "O-双价格/D", "MUST", True, "分歧行业缺价格口径溯源")
            elif not any(k in metric for k in SRC_KW):
                warnings.append(f"[O-双价格] 分歧行业 price={inp.price} 但行业价口径未含具体价源"
                                f"（批价/现货/长协/出厂/终端/招标/挂牌）：{metric[:40]} → 请明确价源后再判方向。")
                self._t(trace, "价格溯源校验", "O-双价格/D", "SHOULD", False,
                        f"分歧行业价源含糊：{metric[:40]}")

    def _build_bands(self, inp, kind, trace, warnings):
        """返回 (anchors, now)。kind='pb'|'pe'。缺失返回 (None, None)。"""
        O = self.P["odds"]
        if kind == "pb":
            now, series = inp.pb_now, inp.pb_series
            p5, p20, p50, p80, p95 = inp.pb_p5, inp.pb_p20, inp.pb_p50, inp.pb_p80, inp.pb_p95
            pct = inp.pb_pct
        else:
            now, series = inp.pe_now, inp.pe_series
            p5, p20, p50, p80, p95 = inp.pe_p5, inp.pe_p20, inp.pe_p50, inp.pe_p80, inp.pe_p95
            pct = inp.pe_pct
        anchors = {"p5": p5, "p20": p20, "p50": p50, "p80": p80, "p95": p95}
        if series and len(series) >= 20:
            for q, k in zip(O["percentiles"], ["p5", "p20", "p50", "p80", "p95"]):
                anchors[k] = round(percentile(series, q), 4)
            self._t(trace, f"{kind.upper()}五档参考线", "§12.2", "MUST", False,
                    f"由 {len(series)} 点日频真实序列计算（真实分位法）")
            if pct is not None and now is not None:
                implied = rank_pct(series, now)
                if implied is not None and abs(implied - pct) > 10:
                    warnings.append(
                        f"{kind.upper()} 输入分位 {pct}% 与序列反算 {implied:.1f}% 差>10pp → 口径可能不一致（§四 协议③）")
        # V10 泡沫延伸检测基线：剔除近 anchor_exclusion_months 个月的「锚定 p95」
        # （trailing 5y p95 在两年慢牛中随价上行而滞后，锚定 p95 不随泡沫滚动→能早抓涨过头，关掉触发偏晚漏洞）
        anchored_p95 = None
        if series and len(series) >= 20:
            excl = int(O.get("anchor_exclusion_months", 18) * 21)  # ~交易日/月
            base_series = series[:-excl] if len(series) > excl else series
            if len(base_series) >= 20:
                anchored_p95 = round(percentile(base_series, 95), 4)
        anchors["anchored_p95"] = anchored_p95
        return anchors, now

    @staticmethod
    def _nonmono(anchors):
        seq = [anchors["p5"], anchors["p20"], anchors["p50"], anchors["p80"], anchors["p95"]]
        return any(seq[i] > seq[i + 1] for i in range(len(seq) - 1))

    def _core_odds(self, now, anchors, O, trace):
        """给定 now+anchors，算 space / 赔率比 / 尾部。返回 (space, orn, oro, tail)。"""
        # V10 修复（2026-09-04）：anchored_p95 可能为 None（无序列回退），space 只算非 None 锚
        space = {k: round((x - now) / now * 100.0, 2) for k, x in anchors.items()
                 if x is not None}  # type: ignore[operator]
        down_p20 = abs(space["p20"]) if space["p20"] < 0 else None
        orn = round(space["p50"] / down_p20, 2) if down_p20 and down_p20 > 0 else None
        oro = round(space["p80"] / down_p20, 2) if down_p20 and down_p20 > 0 else None
        ext_down = abs(space["p5"]) if space["p5"] < 0 else 0.0
        tail = {
            "extreme_down_pct": round(ext_down, 2),
            "extreme_odds_ratio": (round(space["p50"] / ext_down, 2) if ext_down > 0 else None),
            "flags": [], "downgrade_one_notch": False, "extreme_optimistic_hit": False,
            "bubble_hard": False, "bubble_warn": False,
        }
        if now < anchors["p5"]:                                  # type: ignore[operator]
            tail["flags"].append("已破历史最惨区（P5）→ 系统性崩盘信号，禁止接飞刀")
            self._t(trace, "破极端悲观线", "§12.2/§12.5", "MUST", True, f"now {now} < P5 {anchors['p5']}")
        if ext_down > O["extreme_down_red_flag"]:
            tail["flags"].append(f"⚠️ 尾部风险高（极端下行 {ext_down:.1f}%>30%）：抄底须分批+严控单笔下注")
            self._t(trace, "尾部风险标红", "§12.5.2", "MUST", True, f"{ext_down:.1f}%")
        if down_p20 and ext_down > down_p20 * O["extreme_down_vs_pessimistic_multiple"]:
            tail["flags"].append("极端下行 > 悲观下行×2 → 左尾极厚，结论降一档")
            tail["downgrade_one_notch"] = True
            self._t(trace, "左尾极厚降档", "§12.5.2", "MUST", True, f"ext={ext_down:.1f}% vs p20={down_p20:.1f}%")
        r = tail["extreme_odds_ratio"]
        if r is not None and r < O["extreme_ratio_conservative_below"]:
            tail["flags"].append("极端赔率比<1 → 尾部得不偿失，须显著偏保守")
            self._t(trace, "极端赔率比<1", "§12.5.2", "MUST", True, str(r))
        if now >= anchors["p95"] * O["extreme_optimistic_hit_ratio"]:     # type: ignore[operator]
            tail["extreme_optimistic_hit"] = True
            tail["flags"].append("现价≥极端乐观价×90% → 涨过头区，强制减仓/清仓")

        # ===== V10 泡沫延伸检测（锚定 p95，关 trailing-window 滞后漏洞）=====
        # trailing 5y p95 在两年慢牛中会随价上行滞后→「涨过头区」触发偏晚（类似2021新能车）。
        # 锚定 p95 = 剔除近 anchor_exclusion_months 个月后的序列 p95（不随泡沫滚动），
        # now/锚定p95 比率直接衡量"涨过头"程度。需 pb_series/pe_series 日频序列方生效。
        anch_p95 = anchors.get("anchored_p95")
        if anch_p95 and anch_p95 > 0 and now is not None:
            ratio = now / anch_p95
            warn_r = float(O.get("bubble_warn_ratio", 1.3))
            hard_r = float(O.get("bubble_hard_ratio", 1.6))
            if ratio >= hard_r:
                tail["bubble_hard"] = True
                tail["flags"].append(
                    f"⚠️ 泡沫延伸超锚定上沿 {ratio:.2f}× (≥{hard_r}) → 强制减仓/清仓（关闭O4前瞻↑豁免）")
                self._t(trace, "泡沫延伸硬否决(V10)", "§12.5/V10", "MUST", True,
                        f"now/锚定p95={ratio:.2f}≥{hard_r}")
            elif ratio >= warn_r:
                tail["bubble_warn"] = True
                tail["flags"].append(
                    f"泡沫延伸警示 {ratio:.2f}× (≥{warn_r}) → 涨过头区，收紧O4前瞻↑豁免、显著降仓")
                self._t(trace, "泡沫延伸警示(V10)", "§12.5/V10", "SHOULD", False,
                        f"now/锚定p95={ratio:.2f}≥{warn_r}")
        return space, orn, oro, tail

    def _odds(self, inp: SectorInput, trace, blockers, warnings
              ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        O = self.P["odds"]
        out: Dict[str, Any] = {}
        tail: Dict[str, Any] = {}
        anchor = (inp.valuation_anchor or "PB").upper()

        def _missing(tag, anchors, now):
            # V10 修复（2026-09-04）：anchored_p95 是泡沫延伸检测的【辅助锚】，
            # 非 §12.5 五档必填项——无日频序列时应回退（消费端已判 None 跳过），
            # 不得在此按缺失 MUST 拦截（否则所有无序列输入被一律打成待核验，违背
            # SKILL.md「V10 需日频序列方生效，否则回退」设计，红队 19 用例连带挂死）。
            miss = [k for k, x in anchors.items() if x is None and k != "anchored_p95"]
            if miss or now is None:
                blockers.append(f"[MUST] {tag} 五档参考线/现价缺失 {miss} → 极端情景失真（§12.5）")
                self._t(trace, f"{tag}五档完整性", "§12.5", "MUST", True, f"缺 {tag} 五档/现价")
                return True
            if self._nonmono(anchors):
                blockers.append(f"[MUST] {tag} 五档非单调 → 百分位序列失真，须重算")
                self._t(trace, f"{tag}五档单调性", "§12.3", "MUST", True, str(list(anchors.values())))
                return True
            return False

        if anchor == "PE":
            anchors, now = self._build_bands(inp, "pe", trace, warnings)
            if _missing("PE", anchors, now):
                return out, tail
            space, orn, oro, tail = self._core_odds(now, anchors, O, trace)
            out = {"anchors": anchors, "space_pct": space, "now": now,
                   "odds_ratio_neutral": orn, "odds_ratio_optimistic": oro, "anchor": "PE"}
            return out, tail
        if anchor == "PB":
            anchors, now = self._build_bands(inp, "pb", trace, warnings)
            if _missing("PB", anchors, now):
                return out, tail
            space, orn, oro, tail = self._core_odds(now, anchors, O, trace)
            out = {"anchors": anchors, "space_pct": space, "now": now,
                   "odds_ratio_neutral": orn, "odds_ratio_optimistic": oro, "anchor": "PB"}
            return out, tail
        # BLEND：双锚各自算，再按 valuation_blend 权重动态混合
        ba, bnow = self._build_bands(inp, "pb", trace, warnings)
        ea, enow = self._build_bands(inp, "pe", trace, warnings)
        if _missing("PB", ba, bnow) or _missing("PE", ea, enow):
            return out, tail
        w = self._blend_weights(inp)
        we, wp = w["pe_weight"], w["pb_weight"]
        if w.get("matched"):
            self._t(trace, "BLEND自适应权重(O6)", "O6", "MUST", True,
                    f"命中『{w['matched']}』→ PE{w['pe_weight']:.2f}/PB{w['pb_weight']:.2f}"
                    f"（替代全局0.60/0.40，关V6）")
        # V10 修复（2026-09-04）：跳过任一侧为 None 的锚（anchored_p95 无序列时为 None）。
        # 两侧各自的 V10 泡沫标志已在 _core_odds 内按边计算并在此处 OR 合并，
        # 合并锚无需携带 anchored_p95（携带反而会在混合 None 时 TypeError）。
        anchors = {k: round(wp * ba[k] + we * ea[k], 4) for k in ba
                   if ba.get(k) is not None and ea.get(k) is not None}
        sb, obn, obo, tb = self._core_odds(bnow, ba, O, trace)
        se, oen, oeo, te = self._core_odds(enow, ea, O, trace)
        # 修复（2026-09-16）：BLEND 混合时两侧 core-odds space 键可能不对称
        # （如 PB 侧有 pb_series→anchored_p95 被算出并带入 space，PE 侧无 pe_series→
        #  anchored_p95=None 被 _core_odds 过滤掉）→ 仅对两侧共有的键做加权混合。
        # anchored_p95 仅是 V10 泡沫延伸辅助锚，不进入 space_pct/EV 计算，缺失无害。
        space = {k: round(wp * sb[k] + we * se[k], 2) for k in sb if k in se}
        orn = round(wp * obn + we * oen, 2) if (obn is not None and oen is not None) else None
        oro = round(wp * obo + we * oeo, 2) if (obo is not None and oeo is not None) else None
        rb, re_ = tb.get("extreme_odds_ratio"), te.get("extreme_odds_ratio")
        ratio = (round(min(rb, re_), 2) if (rb is not None and re_ is not None)
                 else rb if rb is not None else (re_ if re_ is not None else None))
        tail = {
            "extreme_down_pct": round(max(tb["extreme_down_pct"], te["extreme_down_pct"]), 2),
            "extreme_odds_ratio": ratio,
            "downgrade_one_notch": bool(tb.get("downgrade_one_notch") or te.get("downgrade_one_notch")),
            "extreme_optimistic_hit": bool(tb.get("extreme_optimistic_hit") or te.get("extreme_optimistic_hit")),
            "bubble_hard": bool(tb.get("bubble_hard") or te.get("bubble_hard")),
            "bubble_warn": bool(tb.get("bubble_warn") or te.get("bubble_warn")),
            "flags": tb["flags"] + te["flags"],
        }
        out = {"anchors": anchors, "space_pct": space, "odds_ratio_neutral": orn,
               "odds_ratio_optimistic": oro, "anchor": "BLEND",
               "blend_weights": {"pe": we, "pb": wp},
               "now": round(wp * bnow + we * enow, 4)}
        return out, tail

    # ---------- §12.5.2 尾部纪律落地（警示 → 实际仓位约束）----------
    def _tail_discipline(self, pos: float, tail: Dict[str, Any], trace
                         ) -> Tuple[float, List[str]]:
        O = self.P["odds"]
        notes: List[str] = []
        if not tail:
            return pos, notes
        if tail.get("downgrade_one_notch"):
            pos *= O["tail_downgrade_coef"]
            notes.append(f"左尾极厚（极端下行>悲观×2）→ 结论降一档，仓位 ×{O['tail_downgrade_coef']}")
            self._t(trace, "尾部降档落地", "§12.5.2", "MUST", True, notes[-1])
        r = tail.get("extreme_odds_ratio")
        if r is not None and r < O["extreme_ratio_conservative_below"]:
            pos *= O["tail_conservative_coef"]
            notes.append(f"极端赔率比 {r}<1 → 显著偏保守，仓位 ×{O['tail_conservative_coef']}")
            self._t(trace, "极端赔率比偏保守落地", "§12.5.2", "MUST", True, notes[-1])
        if tail.get("extreme_down_pct", 0) > O["extreme_down_red_flag"]:
            notes.append("极端下行>30% → 建仓方式强制『分批+严控单笔』，禁止一次打满")
        if tail.get("extreme_optimistic_hit"):
            notes.append("触及极端乐观×90% → 强制减仓/清仓，目标仓位归零")
        return round(pos, 4), notes

    # ---------- §EV 三档价 + EV 中/乐/尾部（参照 XYD-earnings-analysis · 2026-08-15）----------
    def _ev(self, inp: SectorInput, odds: Dict[str, Any], win: Optional[int],
            trace, warnings) -> Optional[Dict[str, Any]]:
        """基于估值主锚的 p5/p20/p50/p80/p95 分位带，算三档目标价与 EV。
        核心假设（与盈利面技能同构）：估值回归至某分位带对应
        目标价 = 现价 × (band / now)。由此：
          u_mid%  = space_pct[p50]   (中性上行空间)
          u_opt%  = space_pct[p80]   (乐观上行空间)
          d_bear% = -space_pct[p20]  (悲观下行损失，恒≥0；若 p20>now 则无下行=0)
          d_ext%  = -space_pct[p5]   (极端下行损失，恒≥0)
        EV = W×U − (1−W)×D_pess（W=胜率；U=上行空间%；D_pess=下行损失%恒≥0，深止损正确压低EV）。
        三档单调保底：乐观≥中性（倒挂则乐观取中性保底·2026-08-06 裁，与盈利面一致）。
        F1（2026-09-04 · 前瞻修复）：提供 earnings_fwd_growth_pct=g 时，四档目标价按
        (1+g)^(H/12) 复利（BVPS/EPS 不再冻结），g 截断 [-30,+60]；g<0 时上行侧与 O2
        折价取更保守者、不复利叠加；缺省 None → 行为与旧版逐字段一致（零回归）。
        """
        if not odds or "space_pct" not in odds:
            return None
        space = odds.get("space_pct")
        if space is None:
            return None
        if win is None:
            # 左侧布局通道（胜率待拐点·不入六档映射）与"胜率=0框架否决"是两种不同状态，
            # 共用一条"胜率=0"文案会把左侧通道误报成框架否决（159040 实证）→ 必须分述。
            return {"disclosure": "胜率未定档（左侧布局通道·待拐点，非否决）→ EV 不输出（无胜率则期望不可计算）"}
        if win <= 0:
            return {"disclosure": "胜率=0（框架否决/价跌硬否决）→ EV 不输出（纯下行期望无买入价值）"}
        p5, p20, p50, p80 = (space.get("p5"), space.get("p20"),
                              space.get("p50"), space.get("p80"))
        if None in (p5, p20, p50, p80):
            return {"disclosure": "五档空间缺失 → EV 不输出"}

        W = win / 100.0
        u_mid_stat, u_opt_stat = float(p50), float(p80)   # 统计口径（F1 非叠加比较基准）
        u_mid = u_mid_stat
        u_opt = u_opt_stat
        d_bear = -p20 if p20 < 0 else 0.0
        d_ext = -p5 if p5 < 0 else 0.0

        # ===== O8 持有期（前置：F1 复利因子需要 H）=====
        ev_cfg = self.P.get("ev_horizon", {})
        ev_hor = float(inp.ev_horizon_months if inp.ev_horizon_months
                       else ev_cfg.get("default_horizon_months", 24))
        ev_hor = max(1.0, ev_hor)

        # ===== F1 EV盈利增长项（2026-09-04 · 前瞻修复批次一）=====
        # 目标价 = 分位带锚 × BVPS/EPS未来值 → 四档价统一乘 (1+g)^(H/12)。
        # g 截断 [EV_GROWTH_MIN_PCT, EV_GROWTH_MAX_PCT]=[-30,+60]；缺省 None 零回归。
        g_raw = inp.earnings_fwd_growth_pct
        g = None
        factor = 1.0
        if g_raw is not None:
            g = max(EV_GROWTH_MIN_PCT, min(EV_GROWTH_MAX_PCT, float(g_raw)))
            factor = (1.0 + g / 100.0) ** (ev_hor / 12.0)
            # 注意：u/d 在本函数内一律为【百分比口径】(17.6 = +17.6%)，复利后须 ×100 回百分比
            u_mid = ((1.0 + u_mid / 100.0) * factor - 1.0) * 100.0
            u_opt = ((1.0 + u_opt / 100.0) * factor - 1.0) * 100.0
            d_bear = max(0.0, -((1.0 + float(p20) / 100.0) * factor - 1.0)) * 100.0
            d_ext = max(0.0, -((1.0 + float(p5) / 100.0) * factor - 1.0)) * 100.0
            self._t(trace, "F1 EV盈利增长项", "§EV/F1", "MUST", True,
                    f"g={g:+.1f}%/年"
                    + (f"（输入{float(g_raw):+.1f}%越界→截断[{EV_GROWTH_MIN_PCT:+.0f},{EV_GROWTH_MAX_PCT:+.0f}]）"
                       if float(g_raw) != g else "")
                    + f"→四档价×{factor:.4f}（H={ev_hor:.0f}月复利）"
                    + ("；g<0 与 O2 上行折价取更保守者不叠加" if g < 0 else ""))

        # ===== O2/O3 基本面相位叠加 + 下行地板（2026-08-16 · 关 V2/V3）=====
        ov = self.P.get("ev_fundamental_overlay", {})
        beta = float(ov.get("earnings_decline_haircut_beta", 0.5))
        floor_min = float(ov.get("downside_floor_min_pp", 10.0))
        floor_max = float(ov.get("downside_floor_max_pp", 40.0))
        collapse_floor = float(ov.get("collapse_downside_floor_pp", 30.0))
        earn_rev = inp.q_earning_rev
        earn_declining = ((earn_rev is not None and earn_rev < 0)
                          or inp.forward == "down" or inp.demand in ("erosion", "collapse"))
        fundamental_phase = ("需求塌方" if inp.demand == "collapse" else
                            "需求侵蚀" if inp.demand == "erosion" else
                            "前瞻下行" if inp.forward == "down" else
                            "盈利下修" if (earn_rev is not None and earn_rev < 0) else
                            "盈利上修" if (earn_rev is not None and earn_rev > 0) else
                            "盈利平稳")
        # O3 下行地板：仅基本面恶化时叠加于统计下行之上（防价值陷阱盲区）
        d_floor = 0.0
        if inp.demand == "collapse":
            d_floor = collapse_floor
        elif earn_declining:
            if earn_rev is not None and earn_rev < 0:
                d_floor = max(floor_min, min(floor_max, abs(earn_rev) * beta))
            elif inp.forward == "down":
                d_floor = floor_min
            elif inp.demand == "erosion":
                d_floor = (floor_min + floor_max) / 2.0
        d_bear = max(d_bear, d_floor)
        d_ext = max(d_ext, d_floor)
        # O2 上行折价：盈利下行相位下 BVPS/EPS 将缩水，p50/p80 绝对价失真
        upside_haircut = 0.0
        if earn_rev is not None and earn_rev < 0:
            upside_haircut = max(0.0, min(0.6, abs(earn_rev) / 100.0 * beta))
        elif inp.forward == "down":
            upside_haircut = 0.30
        elif inp.demand == "erosion":
            upside_haircut = 0.15
        if upside_haircut > 0:
            if g_raw is None:
                u_mid *= (1.0 - upside_haircut)
                u_opt *= (1.0 - upside_haircut)
            else:
                # F1 非叠加规则：g 调整后 与 纯统计×(1−h) 取更保守（更低）者，不叠加复利
                u_mid = min(u_mid, u_mid_stat * (1.0 - upside_haircut))
                u_opt = min(u_opt, u_opt_stat * (1.0 - upside_haircut))

        # 三档单调保底（乐观≥中性，倒挂则乐观取中性保底）
        if u_opt < u_mid - 0.1:
            u_opt = u_mid
            warnings.append("EV：乐观上行空间<中性上行空间（倒挂）→ 乐观取中性保底"
                            "（2026-08-06 裁，与盈利面一致）")
            self._t(trace, "EV乐观保底", "§EV", "MUST", True, "u_opt:=u_mid")

        ev_mid = W * u_mid - (1 - W) * d_bear
        ev_opt = W * u_opt - (1 - W) * d_bear
        ev_tail = W * u_mid - (1 - W) * d_ext

        # ===== O8 EV 持有期/年化（2026-08-16 · 关 V7）=====
        # 原 EV 为 horizon-agnostic 点估计（回归至 p50 的百分比），3月与3年回归数值相同、跨板块不可比。
        # 现加显式持有期 + 年化：默认 24 月（≈供需周期半程），输入 ev_horizon_months 可覆盖；
        # 年化 = (1 + ev/100)^(12/H) − 1，仅作跨板块可比口径，非收益保证。
        # （ev_hor 已前置至 F1 段计算；此处仅保留年化口径）
        ann = bool(ev_cfg.get("annualize", True))

        def _ann(p: float) -> float:
            return ((1.0 + p / 100.0) ** (12.0 / ev_hor) - 1.0) * 100.0
        ev_mid_ann = round(_ann(ev_mid), 2) if ann else None
        ev_opt_ann = round(_ann(ev_opt), 2) if ann else None
        ev_tail_ann = round(_ann(ev_tail), 2) if ann else None
        horizon_label = "输入覆盖" if inp.ev_horizon_months else "默认"

        base = inp.price_now if inp.price_now else 100.0
        price_unit = "绝对价(元)" if inp.price_now else "相对现价=100"
        tiered = {
            "base": round(base, 4),
            "bear": round(base * (1 + (p20 or 0) / 100.0), 4),
            "mid": round(base * (1 + u_mid / 100.0), 4),
            "opt": round(base * (1 + u_opt / 100.0), 4),
            "ext": round(base * (1 + p5 / 100.0), 4),
            "unit": price_unit,
        }
        res = {
            "anchor": odds.get("anchor"),
            "tiered_prices": tiered,
            "u_mid_pct": round(u_mid, 2),
            "u_opt_pct": round(u_opt, 2),
            "d_bear_pct": round(d_bear, 2),
            "d_ext_pct": round(d_ext, 2),
            "ev_mid_pct": round(ev_mid, 2),
            "ev_opt_pct": round(ev_opt, 2),
            "ev_tail_pct": round(ev_tail, 2),
            "horizon_months": round(ev_hor, 1),
            "ev_mid_annualized_pct": ev_mid_ann,
            "ev_opt_annualized_pct": ev_opt_ann,
            "ev_tail_annualized_pct": ev_tail_ann,
            "fundamental_phase": fundamental_phase,
            "downside_floor_pct": round(d_floor, 2),
            "upside_haircut_pct": round(upside_haircut * 100.0, 2),
            "disclosure": "EV=W×U−(1−W)×D_pess（W=胜率；U=上行空间%；D_pess=下行损失%恒≥0）。"
                          "三档价=现价×(分位带/当前锚值)，原假设基本面(BVPS/EPS)不变；"
                          f"已叠加 O2/O3 基本面相位（相位={fundamental_phase}，"
                          f"下行地板={d_floor:.1f}%，上行折价={upside_haircut * 100:.1f}%）。"
                          f"持有期={horizon_label} {ev_hor:.0f}个月，年化仅作跨板块可比口径（O8，非收益保证）"
                          + (f"；[O9] 有效分位已切10y，但EV三档价仍基于5y分位带（10y完整带未提供），"
                             "跨周期结论以10y有效分位为主" if getattr(inp, "_regime_use_10y", False)
                             and inp.valuation_anchor != "PE" else ""),
        }
        if g_raw is not None:
            # F1 增量字段（缺省 None 时不写入，保持旧输出逐键一致）
            res["earnings_fwd_growth_pct"] = g
            res["earnings_growth_factor"] = round(factor, 4)
            res["disclosure"] += (
                f"；[F1] 盈利增长项 g={g:+.1f}%/年"
                + (f"（输入{float(g_raw):+.1f}%越界→截断至[{EV_GROWTH_MIN_PCT:+.0f},"
                   f"{EV_GROWTH_MAX_PCT:+.0f}]）" if float(g_raw) != g else "")
                + f"，四档价按(1+g)^(H/12)={factor:.3f}复利（BVPS/EPS不再冻结）"
                + ("；g<0 时上行侧与 O2 折价取更保守者、不叠加复利" if g < 0 else ""))
        self._t(trace, "EV三档复算", "§EV", "MUST", True,
                f"W={win}% → EV中{ev_mid:+.1f}% / EV乐{ev_opt:+.1f}% / EV尾{ev_tail:+.1f}%"
                f"（u_mid={u_mid:+.1f}% d_bear={d_bear:.1f}% d_ext={d_ext:.1f}%）")
        self._t(trace, "EV基本面相位叠加", "§EV/O2O3", "MUST", earn_declining,
                f"相位={fundamental_phase} 下行地板={d_floor:.1f}% 上行折价={upside_haircut * 100:.1f}%")
        return res

    # ---------- §P* 风险调整买点（2026-09-08）----------
    # 与盈利面 p_star.py **公式同源、参数独立**（params.p_star 段）：
    #   P* = [W·mid + (1−W)·bear_eff] / (1+r)
    #   r  = clamp(r_base(W)×s_vol, 3%, 60%)；s_vol = clamp(σ_eff/σ_ref, 0.25, 2.0)
    #   σ_eff = σ_ann·√(H/12)（H 缺省=EV 持有期 O8）；止损+摩擦走 bear_eff（不进 r）
    # 设计要点：
    #   · **只读披露**：不参与仓位/回测决策，不修改任何既有字段；
    #   · **零回归**：win=None（硬否决/左侧通道）或 ev 缺失 → v.p_star=None；
    #   · 数学等价：P*>现价 ⟺ EV_eff>r（既有「要求回报率」判据的价格化重述，非新规则）。
    def _p_star(self, inp: SectorInput, ev: Optional[Dict[str, Any]],
                win: Optional[int], trace) -> Optional[Dict[str, Any]]:
        cfg = self.P.get("p_star") or {}
        if not cfg.get("enabled", True):
            return None
        if win is None or not ev or not (ev.get("tiered_prices") or {}):
            self._t(trace, "P* 风险调整买点", "§P*", "SHOULD", True,
                    "win=None（硬否决/左侧通道）或无 EV 三档价 → 不产出 P*（只读字段，零回归）")
            return None
        try:
            import sector_pstar as _sps
        except Exception as e:                       # 模块缺失不得阻断主流程
            self._t(trace, "P* 风险调整买点", "§P*", "SHOULD", True,
                    f"sector_pstar 不可用（{type(e).__name__}）→ 跳过")
            return None
        try:
            prices = (getattr(inp, "q_prices", None)
                      or (inp.quant_raw or {}).get("price_series") or [])
            res = _sps.from_verdict(
                ev, win,
                price_now=inp.price_now,
                sigma_ann=inp.p_star_sigma_ann,
                horizon_months=(inp.p_star_horizon_months or ev.get("horizon_months")),
                stop_pct=inp.p_star_stop_pct,
                friction_pct=inp.p_star_friction_pct,
                asset_class=inp.asset_class or "sector",
                prices=prices, cfg=cfg)
        except Exception as e:                       # 任何异常都不影响既有裁决
            self._t(trace, "P* 风险调整买点", "§P*", "SHOULD", True,
                    f"计算异常（{type(e).__name__}: {e}）→ 跳过")
            return None
        if not res:
            return None
        self._t(trace, "P* 风险调整买点", "§P*", "SHOULD", True,
                f"W={res['W']}% r={res['required_return']:.2%}"
                f"（r_base={res['r_base']:.0%}×s_vol={res['s_vol']:.2f}，"
                f"σ={res['sigma_ann'] if res['sigma_ann'] is not None else 'NA'}"
                f"[{res['sigma_source']}]）→ P*={res['p_star']:.2f}"
                + (f" vs 现价{res['price_cur']:.2f}：{res['verdict']}"
                   if res.get("verdict") else ""), res["p_star"])
        return res

    # ---------- F4 见底6信号逐项留痕（2026-09-04 · 前瞻修复批次一 · 左侧通道可审计）----------
    # bottom_signal_detail = [{name, value, evidenced}] ×6（可选）。
    #   · 缺省空：整数口径照旧；仅 bottom_signals≥3（左侧通道相关）时发 [SHOULD] 建议补留痕；
    #     bottom_signals<3 完全静默（老用例零漂移）。
    #   · 提供且 evidenced 计数 ≠ bottom_signals 整数 → [MUST] 矛盾 blocker（PENDING_VERIFICATION）。
    def _bottom_signal_check(self, inp: SectorInput, trace, warnings, blockers) -> None:
        detail = inp.bottom_signal_detail or []
        if not detail:
            if inp.bottom_signals >= 3:
                warnings.append(
                    f"[SHOULD] §二/F4 见底信号留痕：bottom_signals={inp.bottom_signals}≥3 已触发"
                    "左侧通道，建议补 bottom_signal_detail 逐项留痕（{name,value,evidenced}×6）以便审计")
                self._t(trace, "F4 见底信号留痕", "§二/F4", "SHOULD", True,
                        f"bottom_signals={inp.bottom_signals}≥3 未留痕→建议补充")
            return
        evidenced = sum(1 for it in detail if isinstance(it, dict) and it.get("evidenced"))
        if evidenced != inp.bottom_signals:
            blockers.append(
                f"[MUST] §二/F4 见底信号留痕矛盾：bottom_signal_detail 点亮 {evidenced} 项 ≠ "
                f"bottom_signals={inp.bottom_signals}，两者须一致后重跑")
            self._t(trace, "F4 见底信号留痕", "§二/F4", "MUST", True,
                    f"矛盾：detail点亮{evidenced} ≠ 整数{inp.bottom_signals}")
        else:
            self._t(trace, "F4 见底信号留痕", "§二/F4", "MUST", True,
                    f"detail与整数一致（点亮 {evidenced}）")

    # ---------- F3 催化剂窗口校验（2026-09-04 · 前瞻修复批次一 · 对齐 O8 持有期）----------
    # catalysts 兼容两种格式：老 List[str]（window=None，完全零漂移、不追加任何 trace/warning）
    # 与新 List[{name, window_months, direction}]：
    #   · window > O8 持有期 → 仅披露「窗口外」（对 EV 无兑现意义）；
    #   · direction=down 且 window ≤ 持有期 → [SHOULD] 下行催化剂风险提示。
    def _catalyst_struct_check(self, inp: SectorInput, trace, warnings) -> Optional[Dict[str, Any]]:
        items = inp.catalysts or []
        if not items:
            return None
        ev_cfg = self.P.get("ev_horizon", {})
        h = float(inp.ev_horizon_months if inp.ev_horizon_months
                  else ev_cfg.get("default_horizon_months", 24))
        parsed, findings, has_struct = [], [], False
        for it in items:
            if isinstance(it, dict):
                has_struct = True
                name = str(it.get("name") or it)
                try:
                    w = float(it["window_months"]) if it.get("window_months") is not None else None
                except (TypeError, ValueError):
                    w = None
                d = it.get("direction") if it.get("direction") in ("up", "down") else None
                parsed.append({"name": name, "window_months": w, "direction": d})
                if w is not None and w > h:
                    findings.append(f"『{name}』窗口{w:g}月>持有期{h:g}月→窗口外（对本期EV无兑现意义）")
                if d == "down" and w is not None and w <= h:
                    findings.append(f"『{name}』持有期内({w:g}月)下行催化剂")
                    warnings.append(
                        f"[SHOULD] §六/F3 持有期内下行催化剂：{name}（窗口{w:g}月≤持有期{h:g}月），"
                        "兑现将压制三档价，请评估对冲或降低仓位")
            else:
                parsed.append({"name": str(it), "window_months": None, "direction": None})
        if not has_struct:
            return None
        n_win = sum(1 for p in parsed if p["window_months"] is not None)
        self._t(trace, "F3 催化剂窗口校验", "§六/F3", "SHOULD", True,
                f"结构化 {n_win}/{len(parsed)} 项带窗口（持有期 {h:g} 月）；"
                + ("；".join(findings) if findings else "全部窗口内且无下行催化"))
        return {"horizon_months": h, "items": parsed, "findings": findings}

    # ---------- F2 前瞻方向证据链（2026-09-04 批次二/三 · 恢复前瞻矩阵②的独立证据要求）----------
    # forward=up/down 是全场杠杆最大的枚举值之一（右侧买点/价值陷阱翻转、白名单±分），
    # 但此前无任何证据要求——"当前量价惯性外推"即可蒙混。本检查要求附带来源的前瞻证据。
    # 双档由 params.gates.forward_evidence_required 控制：false=SHOULD（缺省）/ true=MUST。
    # forward=flat（无前瞻主张）两档均不要求。
    def _forward_evidence_check(self, inp: SectorInput, trace, warnings, blockers) -> Dict[str, Any]:
        flag = bool(self.P.get("gates", {}).get("forward_evidence_required", False))
        evidence = inp.forward_evidence or []
        valid = [e for e in evidence
                 if isinstance(e, dict) and str(e.get("desc", "")).strip()
                 and str(e.get("source", "")).strip()]
        out = {"required": flag, "forward": inp.forward,
               "evidence_count": len(valid), "items": valid, "blocked": False}
        if inp.forward == "flat":
            out["note"] = "forward=flat 无前瞻主张 → 不要求证据链"
            self._t(trace, "F2 前瞻证据链", "§二/F2", "SHOULD", False, "forward=flat，不要求")
            return out
        if valid:
            self._t(trace, "F2 前瞻证据链", "§二/F2", "MUST", True,
                    f"forward={inp.forward} 附 {len(valid)} 条带来源证据（"
                    + "；".join(f"{e.get('type','?')}:{str(e.get('source'))[:24]}" for e in valid[:3])
                    + ("…" if len(valid) > 3 else "") + ")")
            out["note"] = "证据链通过"
            return out
        msg = (f"[MUST] §二/F2 前瞻方向证据链缺失：forward={inp.forward} 须附 forward_evidence"
               "（≥1条带 desc+source 的 订单/排产/远期曲线/一致预期上修/政策 证据），"
               "禁止以当前量价惯性外推充当前瞻判断" if flag else
               f"[SHOULD] §二/F2 前瞻方向证据链缺失：forward={inp.forward} 建议附 forward_evidence"
               "（订单/排产/远期曲线/一致预期上修/政策，带来源），前瞻矩阵②不应由量价惯性外推代替")
        if flag:
            blockers.append(msg)
            out["blocked"] = True
            self._t(trace, "F2 前瞻证据链", "§二/F2", "MUST", True, "缺失 → MUST 拦截")
        else:
            warnings.append(msg)
            self._t(trace, "F2 前瞻证据链", "§二/F2", "SHOULD", True, "缺失 → SHOULD 告警（档位未开）")
        out["note"] = "证据链缺失"
        return out

    # ---------- §三bis 景气环比门槛（2026-08-29 用户裁定）----------
    # 买点加景气环比门槛防价值陷阱，并区分左侧/右侧买点：
    #   · 左侧(左侧布局)：PB 低但量价尚未确认。价值陷阱风险最高，故须 景气环比 ≥ left_min(默认0，企稳/不再恶化) 才放行；
    #     若 景气环比仍下行(ΔG<0) → 低 PB 疑似接刀 → 标左侧价值陷阱(与 §8.1-3 同质 win-10+红圈)。
    #   · 右侧(确认买点)：量价已确认(量升价升/量升价稳/价升量稳 + 前瞻↑)。景气环比须明显回升(>right_min)，
    #     否则量价确认与景气背离→假突破预警（不标价值陷阱，仅 [SHOULD] 警示）。右侧已付溢价、赔率变薄，须核对 PB 是否已重估。
    #   · 未提供景气数据 → 仅建议性，不强制门槛（向后兼容、零回归）。
    def _prosperity_gate(self, inp: SectorInput, trace, warnings
                         ) -> Dict[str, Any]:
        G = self.P["gates"]
        left_min = float(G.get("prosperity_left_min_mom", 0.0))
        right_min = float(G.get("prosperity_right_min_mom", 0.0))

        provided = (inp.prosperity_mom_pct is not None) or (inp.prosperity_trend in ("up", "flat", "down"))
        if not provided:
            self._t(trace, "景气环比门槛", "§三bis", "SHOULD", False,
                    "未提供景气环比数据→仅建议补充，不强制门槛（向后兼容、零回归）")
            return {"provided": False, "buy_side": "未标注", "mom": 0.0,
                    "left_ok": True, "right_ok": True, "is_value_trap": False,
                    "note": "未提供景气环比数据（建议补充以校验价值陷阱）"}

        # 数值化 ΔG（优先用客观数值，否则按趋势代理 ±1/0）
        if inp.prosperity_mom_pct is not None:
            mom = float(inp.prosperity_mom_pct)
        else:
            mom = {"up": 1.0, "flat": 0.0, "down": -1.0}.get(inp.prosperity_trend, 0.0)

        # 买点侧别判定：右侧=量价已确认(量升价升/量升价稳/价升量稳 + 前瞻↑)；否则左侧
        confirmed = (inp.vp in ("up|up", "up|flat", "flat|up")) and inp.forward == "up"
        if confirmed:
            buy_side = "右侧(确认买点)"
            ok = mom > right_min
            if ok:
                note = ("右侧确认：景气环比回升(ΔG>0)+量价确认，确定性高但已付溢价"
                        "(赔率变薄)，须核对 PB 是否已重估")
            else:
                note = "右侧买点但景气环比未回升(ΔG≤0)→量价确认与景气背离，警惕假突破/脉冲"
        else:
            buy_side = "左侧(左侧布局)"
            ok = mom >= left_min
            if ok:
                note = "左侧布局：景气环比企稳(ΔG≥0)+PB低，提前埋伏，确定性较低但赔率厚"
            else:
                note = "左侧价值陷阱预警：景气环比仍下行(ΔG<0)，低 PB 疑似接刀，须更高安全边际或回避"
        is_vt = (buy_side.startswith("左侧") and not ok)

        self._t(trace, "景气环比门槛", "§三bis", "MUST", provided,
                f"buy_side={buy_side} ΔG={mom:+.1f}% "
                f"→ {'通过' if ok else ('左侧价值陷阱(景气仍下行)' if is_vt else '右侧背离预警')}")
        if not provided:
            pass
        elif is_vt:
            warnings.append(f"[MUST] §三bis 左侧价值陷阱：{inp.sector} 景气环比仍下行(ΔG={mom:+.1f}%)"
                            "，低 PB 疑似接刀，胜率−10% + 红圈，须更高安全边际或回避")
        elif confirmed and not ok:
            warnings.append(f"[SHOULD] §三bis 右侧背离：{inp.sector} 量价已确认但景气环比未回升"
                            "(ΔG≤0)，警惕假突破，建议等景气环比转正再加码")
        return {"provided": True, "buy_side": buy_side, "mom": mom,
                "left_ok": (mom >= left_min), "right_ok": (mom > right_min),
                "is_value_trap": is_vt, "note": note}

    # ---------- §8.1 硬信号 ----------
    def _hard_signals(self, inp: SectorInput, tail, prosp, trace, warnings
                      ) -> Tuple[List[Dict[str, Any]], bool, bool]:
        G = self.P["gates"]
        out: List[Dict[str, Any]] = []

        def add(no, name, level, hit, action):
            out.append({"no": no, "signal": name, "level": level, "triggered": hit, "action": action})
            self._t(trace, f"硬信号{no} {name}", "§8.1", level, hit, action if hit else "未触发")

        add(1, "需求塌方否决", "MUST", inp.demand == "collapse", "直接排除，胜率=0")
        add(2, "价跌否决", "MUST",
            inp.has_price_down and not inp.is_vp_down_cost_deflation,
            "胜率=0（§二 6信号≥3 例外转等待布局通道；§12.1b 量升价跌·成本通缩降档通道除外）")
        if inp.is_vp_down_cost_deflation:
            warnings.append("[SHOULD] 量升价跌·成本通缩降档通道：胜率≤40%、仓位≤15%『谨慎参与』，"
                            "须确证价跌源于成本传导/以价换量而非供过于求价格战")

        erosion_confirmed = (inp.demand == "erosion"
                            and inp.erosion_signals >= G["erosion_signals_confirm_min"])
        trap = bool(erosion_confirmed and self._eff_pct(inp) is not None
                    and self._eff_pct(inp) < self.P["pb_pct_bands"]["value_trap_pct_max"])
        add(3, "价值陷阱", "MUST", trap, "标『价值陷阱预警』，胜率−10%，赔率图红圈虚线")

        # ===== §三bis 左侧景气环比下行·价值陷阱（2026-08-29 用户裁定）=====
        # 低 PB + 景气环比仍下行(ΔG<0) = 接刀/价值陷阱（诺普信式），与 #3 同质（win-10+红圈）。
        # 右侧(景气已回升) 不触发；右侧背离仅 [SHOULD] 警示（在 _prosperity_gate 内）。
        prosp_vt = bool(prosp.get("is_value_trap"))
        if prosp_vt:
            trap = True
        add(8, "左侧景气环比下行·价值陷阱", "MUST", prosp_vt,
            "低PB+景气环比仍下行(ΔG<0)=接刀，胜率−10%+红圈，与#3同质")

        forced = bool(tail.get("extreme_optimistic_hit") or tail.get("bubble_hard"))
        b_hard = bool(tail.get("bubble_hard"))
        b_warn = bool(tail.get("bubble_warn"))
        add(4, "涨过头兑现", "MUST", forced, "强制减仓/清仓，非持有目标")
        add("V10h", "泡沫延伸硬否决", "MUST", b_hard, "现价超锚定上沿1.6×→强制减仓/清仓，关闭O4前瞻↑豁免")
        add("V10w", "泡沫延伸警示", "SHOULD", b_warn, "现价超锚定上沿1.3×→收紧O4前瞻↑豁免、显著降仓")

        fishtail = bool(inp.prev_vp == "up|up" and inp.vp in ("up|flat", "down|up")
                        and inp.pb_pct is not None and inp.pb_pct > 70)
        add(5, "鱼尾止盈", "SHOULD", fishtail, "逐步兑现（§二 前瞻矩阵）")
        if fishtail:
            warnings.append("[SHOULD] 鱼尾信号：量升价升→量升价稳/量缩价升 且 PB分位>70% → 逐步兑现")

        窗口不足 = bool(inp.pb_series_years is not None
                    and inp.pb_series_years < G["percentile_window_min_years"])
        add(6, "分位窗口不足", "SHOULD", 窗口不足, "降级标『窗口不足』，可靠性低")

        # ===== O4 对称泡沫硬否决（2026-08-16 · 关 V5）=====
        # 对称于"价跌硬否决"：买点有硬否决、卖点（过热）此前无 MUST。
        # 有效分位 > bubble_pct_max(95%) 且前瞻未↑ → 强制减仓/清仓（非持有目标）。
        bubble_max = G.get("bubble_pct_max", 95)
        eff_now = self._eff_pct(inp)
        eff_disp = f"{eff_now:.0f}" if eff_now is not None else "N/A"
        bubble = bool(eff_now is not None and eff_now > bubble_max and inp.forward != "up")
        add(7, "泡沫硬否决", "MUST", bubble,
            f"有效分位 {eff_disp}%>{bubble_max}% 且前瞻未↑ → 强制减仓/清仓（对称于价跌硬否决）")
        if bubble:
            warnings.append(f"[MUST] 泡沫硬否决：有效分位 {eff_now:.0f}%>{bubble_max}% 且前瞻未↑"
                            " → 强制减仓/清仓，目标仓位 0%（O4 对称风险约束）")
        return out, trap, bool(forced or bubble or b_hard)

    # ---------- §12.1 胜率机械映射 ----------
    # ---------- §十二 五维量化胜率（2026-08-15 用户裁定）----------
    def _has_quant(self, inp: SectorInput) -> bool:
        """是否提供五维量化核心指标（D1~D4）。D5 可由序列自动或中性 60 兜底。"""
        core = [inp.q_price_mom6, inp.q_price_pct, inp.q_vol_yoy,
                inp.q_inv_yoy, inp.q_util, inp.q_fund_flow_pct,
                inp.q_fund_turn_pct, inp.q_earning_rev]
        return _compute_quant is not None and all(v is not None for v in core)

    def _quant_params(self, inp: SectorInput) -> Dict[str, Any]:
        """win_quant 参数（允许输入覆盖 g(S) 映射模式；未覆盖则与 _load_params 同源）。"""
        Q = self.P.get("win_quant") or {}
        if inp.win_map_mode:
            Q = dict(Q)
            Q["win_map_mode"] = inp.win_map_mode
        return Q

    def _build_quant(self, inp: SectorInput) -> Dict[str, Any]:
        q: Dict[str, Any] = {
            "price_mom6": inp.q_price_mom6, "price_pct": inp.q_price_pct,
            "vol_yoy": inp.q_vol_yoy, "inv_yoy": inp.q_inv_yoy, "util": inp.q_util,
            "fund_flow_pct": inp.q_fund_flow_pct, "fund_turn_pct": inp.q_fund_turn_pct,
            "earning_rev": inp.q_earning_rev,
        }
        if inp.q_prices:
            q["prices"] = list(inp.q_prices)
        if (inp.q_ma_score is not None and inp.q_breakout_score is not None
                and inp.q_divergence_penalty is not None):
            q["ma_score"] = inp.q_ma_score
            q["breakout_score"] = inp.q_breakout_score
            q["divergence_penalty"] = inp.q_divergence_penalty
        return q

    def _win_rate(self, inp: SectorInput, value_trap: bool, forced_reduce: bool,
                  tail: Dict[str, Any], trace, warnings) -> Tuple[Optional[int], str, bool, bool]:
        P, G = self.P, self.P["gates"]
        W, A, C = P["win_base"], P["win_adjust"], P["win_clamp"]
        band = P["pb_pct_bands"]

        # --- 硬否决 gate（优先级最高）---
        if inp.demand == "collapse":
            self._t(trace, "gate 需求塌方", "§12.1", "MUST", True, "胜率强制=0，不进入基础映射")
            self._win_rate_mode = "rejected"
            return 0, "0%（框架否决·需求塌方）", False, True
        downgrade = inp.is_vp_down_cost_deflation
        if inp.has_price_down:
            if downgrade:
                self._t(trace, "gate 量升价跌·成本通缩降档", "§12.1b", "MUST", True,
                        "量升+结构性增长+显式声明cost_deflation → 不再硬置零，走降档通道")
            elif inp.bottom_signals >= G["bottom_signals_channel_min"]:
                self._t(trace, "gate 价跌→左侧通道", "§二/§12.1", "MUST", True,
                        f"6信号点亮 {inp.bottom_signals}≥3 → 独立等待布局通道，不产出胜率数值")
                self._win_rate_mode = "channel"
                return None, "待拐点（左侧布局通道·不入六档映射）", True, False
            else:
                self._t(trace, "gate 价跌否决", "§12.1", "MUST", True,
                        f"含价跌且非降档通道、6信号仅 {inp.bottom_signals} 个 (<3) → 胜率=0")
                self._win_rate_mode = "rejected"
                return 0, "0%（框架否决·价跌无板块牛市）", False, True
        if inp.imagination == "D":
            self._t(trace, "gate 想象力D", "§十/§12.1", "MUST", True, "需求结构性塌方级 → 直接出局")
            self._win_rate_mode = "rejected"
            return 0, "0%（框架否决·想象力D）", False, True

        # --- 基础胜率（五维量化优先 / 六档表回退，向后兼容）---
        key = inp.vp
        quant_base: Optional[int] = None
        quant_base_eff: Optional[float] = None
        if self._has_quant(inp):
            qr = _compute_quant(self._build_quant(inp), self._quant_params(inp))
            quant_base = qr["win_rate"]
            quant_base_eff = qr.get("win_rate_eff", quant_base)
            self._t(trace, "基础胜率·五维量化",
                    f"D1={qr['D1_price']} D2={qr['D2_volume']} D3={qr['D3_fund']} "
                    f"D4={qr['D4_expect']} D5={qr['D5_structure']} → S={qr['S']}",
                    "MUST", True,
                    f"五维加权（W={quant_base}%→W_eff={quant_base_eff}%，{qr.get('confidence')}置信·"
                    f"V4收缩shrink={qr.get('shrink')}；g(S)占位proxy无信号→EV三档+硬否决主导）", quant_base)
            if qr["missing_dims"]:
                warnings.append(f"五维量化缺维 {qr['missing_dims']}（按中性50处理，结论需补数据复核）")
        if downgrade:
            D = P["win_base_downgrade"]
            if quant_base_eff is not None:
                # 降档通道：地板25% / 封顶40%（约束五维量化结果，防虚高）；V4 收缩值入基
                base = float(max(D["base_win_rate_up_down"],
                                 min(D["cap_win_rate_pct"], quant_base_eff)))
            else:
                base = float(D["base_win_rate_up_down"])
            src = f"§12.1b 量升价跌·成本通缩降档通道（基础 {base:.0f}%，封顶 {D['cap_win_rate_pct']}%，仓位≤{D['position_cap']:.0%}）"
            self._win_rate_mode = "downgrade_quant" if quant_base_eff is not None else "downgrade_table"
        elif key in W:
            base = quant_base_eff if quant_base_eff is not None else float(W[key])
            src = "§十二 五维量化" if quant_base_eff is not None else "§12.1 六档表"
            self._win_rate_mode = "quant" if quant_base_eff is not None else "table"
        elif key in P["win_base_extended"]:
            base = quant_base_eff if quant_base_eff is not None else float(P["win_base_extended"][key])
            src = ("§十二 五维量化[EXTENDED]" if quant_base_eff is not None
                   else "[EXTENDED] 框架未列档·代码补全（须人工确认）")
            self._win_rate_mode = "quant" if quant_base_eff is not None else "table"
            if quant_base is None:
                warnings.append(f"量价组合『{inp.vp_label}』非框架六档原档，取扩展值 {base}%，须人工确认")
        else:
            raise ValueError(f"非法量价组合: {key}")
        self._t(trace, "基础胜率", src, "MUST", True, f"{inp.vp_label} → {base}%", base)

        # --- 白名单修正（分组 clamp）---
        imag_fwd = 0.0
        if inp.forward == "up":
            imag_fwd += A["forward_up"]
            self._t(trace, "修正·前瞻↑", "§12.1", "MUST", True, "", A["forward_up"])
        elif inp.forward == "down":
            imag_fwd += A["forward_down"]
            self._t(trace, "修正·前瞻↓", "§12.1", "MUST", True, "", A["forward_down"])
        if inp.imagination in ("S", "A"):
            imag_fwd += A[f"imagination_{inp.imagination}"]
            self._t(trace, f"修正·想象力{inp.imagination}", "§12.1", "MUST", True,
                    "同源§十仓位系数，胜率修正限±5不重复计", A[f"imagination_{inp.imagination}"])
        elif inp.imagination == "C":
            imag_fwd += A["imagination_C"]
            self._t(trace, "修正·想象力C", "§12.1", "MUST", True, "", A["imagination_C"])
        raw_group = imag_fwd
        imag_fwd = max(C["group_imag_forward_min"], min(C["group_imag_forward_max"], imag_fwd))
        if raw_group != imag_fwd:
            self._t(trace, "分组 clamp", "§12.1", "MUST", True,
                    f"想象/前瞻合并 {raw_group:+.0f} → clamp 至 {imag_fwd:+.0f}（防超调）")

        other = 0.0
        if inp.pb_pct is not None:
            if inp.pb_pct < band["win_low_threshold"]:
                other += A["pb_pct_low"]
                self._t(trace, "修正·PB分位<30%", "§12.1", "MUST", True, "下行保护厚", A["pb_pct_low"])
            elif inp.pb_pct > band["win_high_threshold"] and not forced_reduce:
                other += A["pb_pct_high"]
                self._t(trace, "修正·PB分位>70%", "§12.1", "MUST", True, "泡沫", A["pb_pct_high"])
        if inp.crowding:
            other += A["crowding"]
            self._t(trace, "修正·筹码拥挤", "§八/§12.1", "MUST", True, "", A["crowding"])
        if inp.catalyst:
            other += A["catalyst"]
            self._t(trace, "修正·催化确认", "§六 Step4", "MUST", True, "", A["catalyst"])
        if forced_reduce:
            other += A["extreme_optimistic_hit"]
            self._t(trace, "修正·极端乐观触发", "§12.5", "MUST", True, "且强制减仓",
                    A["extreme_optimistic_hit"])
        if tail.get("bubble_hard"):
            other += A["bubble_hard"]
            self._t(trace, "修正·泡沫延伸硬触发(V10)", "§12.5/V10", "MUST", True, "强制减仓",
                    A["bubble_hard"])
        elif tail.get("bubble_warn"):
            other += A["bubble_warn"]
            self._t(trace, "修正·泡沫延伸警示(V10)", "§12.5/V10", "SHOULD", False, "显著降仓",
                    A["bubble_warn"])
        if value_trap:
            other += A["value_trap"]
            self._t(trace, "修正·价值陷阱", "§一/§8.1-3", "MUST", True, "低PB是假便宜",
                    A["value_trap"])

        win = base + imag_fwd + other
        if downgrade:
            cap = int(P["win_base_downgrade"]["cap_win_rate_pct"])
            if win > cap:
                self._t(trace, "降档通道胜率封顶", "§12.1b", "MUST", True,
                        f"{win:.0f} → {cap}（量升价跌·成本通缩 封顶 {cap}%，不入高胜率档）")
                win = float(cap)
        clipped = max(C["global_min"], min(C["global_max"], win))
        if clipped != win:
            self._t(trace, "全局 clamp", "§12.1", "MUST", True, f"{win:.0f} → {clipped:.0f}")
        return int(round(clipped)), f"{int(round(clipped))}%", False, False

    # ---------- §五 四象限 ----------
    def _quadrant(self, win: Optional[int], odds_score: Optional[float],
                  channel: bool, value_trap: bool, rejected: bool) -> str:
        if rejected:
            return "回避区（框架否决）"
        if value_trap:
            return "价值陷阱·回避（表面高赔率是假便宜）"
        if channel:
            return "等待布局区（左侧通道·低胜率高赔率）"
        if win is None or odds_score is None:
            return "待核验"
        O = self.P["odds"]
        hi_w, hi_o = win >= O["win_split"], odds_score >= O["odds_score_split"]
        if hi_w and hi_o:
            return "黄金买入区"
        if not hi_w and hi_o:
            return "等待布局区"
        if hi_w and not hi_o:
            return "持有兑现区"
        return "双杀危险区"

    # ---------- §十 仓位 ----------
    def _position(self, inp: SectorInput, win: Optional[int], channel: bool,
                  rejected: bool, forced_reduce: bool, trace, warnings) -> float:
        Pp = self.P["position"]
        band = self.P["pb_pct_bands"]
        if rejected:
            self._t(trace, "仓位", "§十", "MUST", True, "框架否决 → 0%")
            return 0.0
        if forced_reduce:
            self._t(trace, "仓位", "§12.5/§8.1-4", "MUST", True, "涨过头强制减仓 → 目标仓位 0%")
            return 0.0
        eff = self._eff_pct(inp)
        if eff is None:
            warnings.append("缺估值分位（PB/PE/BLEND）→ 无法定标准仓位")
            return 0.0
        if eff < band["excellent_max"]:
            base, tag = Pp["base_by_pb_pct"]["excellent"], "估值分位<30%→30%"
        elif eff <= band["neutral_max"]:
            base, tag = Pp["base_by_pb_pct"]["neutral"], "估值分位30~60%→15%"
        else:
            base, tag = Pp["base_by_pb_pct"]["high"], "估值分位>60%→≤10%"
        ci = Pp["imagination_coef"].get(inp.imagination, 1.0)
        cv = Pp["vp_coef"].get(inp.vp, 0.6)
        cf = Pp["forward_coef"].get(inp.forward, 1.0)
        pos = base * ci * cv * cf
        pos = min(pos, Pp["single_sector_cap"])
        if channel:
            pos = min(pos, Pp["left_side_channel_cap"])
            self._t(trace, "左侧通道限仓", "§二", "MUST", True, f"上限 {Pp['left_side_channel_cap']:.0%}")
        if inp.is_vp_down_cost_deflation:
            cap_d = float(self.P["win_base_downgrade"]["position_cap"])
            pos = min(pos, cap_d)
            self._t(trace, "降档通道限仓", "§12.1b", "MUST", True,
                    f"量升价跌·成本通缩 → 上限 {cap_d:.0%}『谨慎参与』")
        self._t(trace, "仓位公式", "§十", "MUST", True,
                f"{tag} × 想象力{ci} × 量价{cv} × 前瞻{cf} = {pos:.1%}（单板块上限 40%）")
        return round(pos, 4)

    @staticmethod
    def _odds_rating(pct: Optional[float]) -> str:
        if pct is None:
            return "待核验"
        if pct < 30:
            return "极佳"
        if pct <= 60:
            return "中性"
        if pct <= 80:
            return "偏高（收窄）"
        return "差"

    # ---------- §十三 发布前自检 ----------
    def _checklist(self, inp, chk, odds, tail, win, channel, hard, blockers) -> Dict[str, bool]:
        ac = (inp.valuation_anchor or "PB").upper()
        if ac == "PE":
            ok_dual = "双源一致" in str(chk.get("pe_cross", ""))
        elif ac == "BLEND":
            ok_dual = ("双源一致" in str(chk.get("pb_cross", ""))
                       and "双源一致" in str(chk.get("pe_cross", "")))
        else:
            ok_dual = "双源一致" in str(chk.get("pb_cross", ""))
        # V10 修复（2026-09-04）：⑤只校验 §12.5 五个正档锚（anchored_p95 是辅助锚，
        # 无序列时为 None 属合法回退，不得参与本项判定）；⑥改为清单子集校验
        # （V10 增补 V10h/V10w 后硬信号已达 10 条，固定 len==N 会在每次增补时静默失配）。
        five = ("p5", "p20", "p50", "p80", "p95")
        required_nos = {"1", "2", "3", "4", "5", "6", "7", "8", "V10h", "V10w"}
        return {
            "①指数代码匹配": bool(chk["index_ok"]),
            "②估值双源+手算(按主锚)": bool(ok_dual),
            "③分位标周期": bool(chk["period_ok"]),
            "④胜率机械映射": True,  # 引擎内无定性入口，恒真
            "⑤极端下行已标红": bool(odds.get("anchors") and all(
                odds.get("anchors", {}).get(k) is not None for k in five)),
            "⑥硬信号已查": required_nos <= {str(h.get("no")) for h in (hard or [])},
            "⑦数据新鲜度": bool(chk["fresh"]),
            "⑧适用边界": inp.asset_class in self.P["gates"]["allowed_asset_class"],
            "⑨量价来源=行业周期": bool(inp.vp_basis == "industry_cycle"
                                      and inp.industry_volume_metric
                                      and inp.industry_price_metric),
            "⑩估值主锚声明合法": inp.valuation_anchor in ("PB", "PE", "BLEND"),
        }


# =============================================================================
# 对外 API
# =============================================================================
_ENGINE: Optional[DisciplineEngine] = None


def engine() -> DisciplineEngine:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = DisciplineEngine()
    return _ENGINE


def evaluate(data: Dict[str, Any]) -> Dict[str, Any]:
    """字典进、字典出。技能/回测/红队统一入口。"""
    return engine().run(SectorInput.from_dict(data)).to_dict()


def render_text(v: Dict[str, Any]) -> str:
    """人类可读摘要（技能输出报告时可直接引用）。"""
    L = []
    L.append(f"【{v['sector']}】as_of={v['as_of']}  status={v['status']}"
             f"  params={v['params_version']}#{v['params_fingerprint'][:8]}")
    L.append(f"  需求: {v['demand_check']['type']}  量价: {v['volume_price']['combination']}"
             f" ({v['volume_price']['double_click_quality']})  前瞻: {v['forward_looking']['direction']}")
    pc = v.get("prosperity_check") or {}
    if pc:
        vt = "⚠️左侧价值陷阱" if pc.get("is_value_trap") else ("✅" if (pc.get("left_ok") or pc.get("right_ok")) else "⚠️背离")
        L.append(f"  [景气环比·§三bis] 买点侧别={v.get('buy_side','')}  ΔG={pc.get('mom')}  {vt}  — {pc.get('note','')}")
    vc = v["valuation_check"]
    L.append(f"  估值主锚: {vc['anchor']}")
    L.append(f"  PB: {vc['current_pb']}  分位 {vc['pb_percentile']}% [{vc['pb_percentile_period']}]"
             f"  校验: {vc['pb_cross']}")
    if vc["anchor"] in ("PE", "BLEND"):
        L.append(f"  PE: {vc['current_pe']}  分位 {vc['pe_percentile']}% [{vc['pe_percentile_period']}]"
                 f"  校验: {vc['pe_cross']}")
    L.append(f"  有效分位(eff): {vc['eff_percentile']}%  赔率 {vc['odds_rating']}")
    L.append(f"  胜率: {v['win_rate_label']}  象限: {v['quadrant']}  建议仓位: {v['suggested_position']}")
    if v.get("win_rate_mode"):
        _wm = {"quant": "五维量化", "table": "六档表(回退)",
               "downgrade_quant": "五维量化·降档通道", "downgrade_table": "六档表·降档通道(回退)",
               "channel": "左侧通道(无胜率数值)", "rejected": "框架否决", "na": "不适用"}.get(
                   v["win_rate_mode"], v["win_rate_mode"])
        L.append(f"  胜率模式: {_wm}（{'⚠️ 非量化路径·结论须带此水印' if 'table' in v['win_rate_mode'] else '量化路径'}）")
    if v.get("conviction_score") is not None:
        L.append(f"  [O10] 风险预算 conviction={v['conviction_score']:.3f}"
                 f"（=仓位×胜率/100，跨板块加总须≤组合上限 {v.get('conviction_cap', 0.60):.0%}）")
    ev = v.get("ev")
    if ev:
        if ev.get("disclosure") and "不输出" in ev["disclosure"]:
            L.append(f"  [EV] {ev['disclosure']}")
        else:
            tp = ev.get("tiered_prices", {})
            unit = tp.get("unit", "")
            L.append(f"  [EV] 三档价({unit}): 悲观{tp.get('bear')} / 中性{tp.get('mid')} / 乐观{tp.get('opt')}"
                     f" / 极端{tp.get('ext')}（基准{tp.get('base')}）")
            L.append(f"  [EV] 上行空间 u_mid={ev['u_mid_pct']:+}% u_opt={ev['u_opt_pct']:+}%"
                     f"  下行损失 d_bear={ev['d_bear_pct']}% d_ext={ev['d_ext_pct']}%")
            L.append(f"  [EV] EV中={ev['ev_mid_pct']:+}%  EV乐={ev['ev_opt_pct']:+}%  EV尾={ev['ev_tail_pct']:+}%")
            if ev.get("horizon_months"):
                L.append(f"  [EV·O8] 持有期={ev['horizon_months']:.0f}月  年化: 中{ev['ev_mid_annualized_pct']:+}%"
                         f" / 乐{ev['ev_opt_annualized_pct']:+}% / 尾{ev['ev_tail_annualized_pct']:+}%（仅可比口径）")
            if ev.get("fundamental_phase") and ev["fundamental_phase"] != "盈利平稳":
                L.append(f"  [EV·O2/O3] 基本面相位={ev['fundamental_phase']}  下行地板={ev.get('downside_floor_pct')}%"
                         f"  上行折价={ev.get('upside_haircut_pct')}%")
    if v["tail_risk"].get("flags"):
        for f in v["tail_risk"]["flags"]:
            L.append(f"  [尾部] {f}")
    dp = v.get("dual_price_check")
    if dp:
        c = dp.get("cross", {})
        o = dp.get("_original_volume_price", {})
        e = dp.get("_effective_volume_price", {})
        fac, mkt = dp.get("factory", {}), dp.get("market", {})
        L.append(f"  [双价格校验] 传导状态={c.get('transmission_status')}  "
                 f"出厂价趋势={c.get('factory_price_trend')}({fac.get('vp_pattern')}) / "
                 f"市场价趋势={c.get('market_price_trend')}({mkt.get('vp_pattern')})  "
                 f"is_factory_up_market_down={c.get('is_factory_up_market_down')}")
        L.append(f"  [双价格校验] 框架量价: 原输入({o.get('volume')}|{o.get('price')}) → "
                 f"有效({e.get('volume')}|{e.get('price')}) [市场成交价侧为主]")
        if c.get("is_factory_up_market_down"):
            L.append(f"  [双价格校验] ⚠️ 价格传导失效：出厂价↑+市场价↓，"
                     f"已防『量缩价升』误判，框架按市场侧({e.get('volume')}|{e.get('price')})出量价裁决")
    for n in v.get("execution_notes", []):
        L.append(f"  [执行] {n}")
    for b in v["blockers"]:
        L.append(f"  [BLOCK] {b}")
    for w in v["warnings"][:6]:
        L.append(f"  [WARN ] {w}")
    hit = [f"{h['no']}.{h['signal']}" for h in v["hard_signals"] if h["triggered"]]
    L.append(f"  硬信号触发: {', '.join(hit) if hit else '无'}")
    fail = [k for k, ok in v["checklist"].items() if not ok]
    L.append(f"  §十三自检: {'全通过' if not fail else '未通过 ' + ', '.join(fail)}")
    return "\n".join(L)


DEMO = {
    "sector": "证券", "as_of": date.today().isoformat(), "asset_class": "sector",
    "index_code": "399975", "index_name": "中证全指证券公司", "demand": "growth",
    "volume": "up", "price": "up", "forward": "up",
    # 行业周期量价口径：证券行业的需求体量=A股成交额；盈利价格=佣金率/ROE
    "vp_basis": "industry_cycle",
    "industry_volume_metric": "A股日均成交额(行业需求体量,亿元)",
    "industry_price_metric": "券商净佣金率/ROE(行业盈利价格)",
    "pb_now": 1.36, "pb_manual": 1.35, "pb_sources": {"mx-data": 1.36, "tdx": 1.37},
    "pb_pct": 27.5, "pb_pct_period": "5y", "pb_pct_10y": 8.2, "pb_series_years": 5.0,
    "pb_p5": 0.95, "pb_p20": 1.08, "pb_p50": 1.50, "pb_p80": 1.89, "pb_p95": 2.35,
    "imagination": "B", "catalyst": True, "catalysts": ["中报头部券商+50%"],
    # F2 前瞻证据链（2026-09-07 P2-⑦ demo 夹具再生）：params 1.10.0 起 forward=up 须附
    # ≥1 条 desc+source 齐备证据，否则 demo 自身 PENDING、无法展示完整 OK 路径。
    "forward_evidence": [{"type": "consensus_revision", "desc": "中报后券商盈利一致预期上修（demo 演示数据）",
                          "source": "discipline.py 内置 demo"}],
}


def _selfcheck() -> int:
    """引擎自检：证券样例的每一步都可手算复现，用于确认无隐性漂移。"""
    print("== discipline.py 引擎自检 ==")
    v = evaluate(DEMO)
    print(render_text(v))
    # 手算：基础 80(量升价升) + 前瞻↑+5(组内clamp内) + PB<30%+5 + 催化+5 = 95 → 全局封顶 90
    # 仓位：0.30(<30%分位) × 1.0(B) × 1.0(量升价升) × 1.2(前瞻↑) = 36% ≤ 40% 上限
    #      → 尾部：极端赔率比 0.34<1 → ×0.7 = 25.2%（极端下行 30.1% 另标分批）
    checks = [
        ("status=OK", v["status"] == "OK"),
        ("胜率=90（80+5+5+5 封顶90）", v["win_rate"] == 90),
        ("象限=黄金买入区", v["quadrant"] == "黄金买入区"),
        ("仓位=25.2%（36%×0.7 极端赔率比偏保守）", v["suggested_position"] == "25.2%"),
        ("尾部标红已触发", any("尾部风险高" in f for f in v["tail_risk"].get("flags", []))),
        ("§十三自检全通过", all(v["checklist"].values())),
    ]
    print()
    for name, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    bad = [n for n, ok in checks if not ok]
    print(f"\n自检结果: {'PASS 全部通过' if not bad else 'FAIL ' + str(bad)}")
    return 0 if not bad else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="XYD 供需板块框架 纪律层引擎")
    ap.add_argument("--input", help="输入 JSON 文件（单对象或数组）")
    ap.add_argument("--batch", action="store_true", help="输入为数组")
    ap.add_argument("--json", action="store_true", help="输出原始 JSON")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--selfcheck", action="store_true")
    a = ap.parse_args()

    if a.selfcheck:
        return _selfcheck()
    if a.demo:
        v = evaluate(DEMO)
        print(json.dumps(v, ensure_ascii=False, indent=2) if a.json else render_text(v))
        return 0
    if not a.input:
        ap.print_help()
        return 2
    with open(a.input, "r", encoding="utf-8") as f:
        data = json.load(f)
    items = data if isinstance(data, list) else [data]
    out = [evaluate(x) for x in items]
    if a.json:
        print(json.dumps(out if len(out) > 1 else out[0], ensure_ascii=False, indent=2))
    else:
        for v in out:
            print(render_text(v))
            print("-" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
