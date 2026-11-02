#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XYD 供需板块框架 —— 红队测试 (Red Team)
=====================================
思路：不测"正常情况能不能跑"，而是**主动构造能骗过分析师的输入**，
验证纪律层是否真的挡得住。每个用例都对应一次历史踩坑或一类经典陷阱。

分组
  A 组 框架否决不可被"故事"绕过（价跌 / 塌方 / 想象力D）
  B 组 PB 口径错配（已两次实盘踩坑：高德红外行业均值PB、电池ETF全历史分位）
  C 组 适用边界与新鲜度
  D 组 极端情景与尾部纪律
  E 组 白名单封闭性（禁自创修正项 / 禁定性微调）
  F 组 量化通病（未来函数 / 幸存者偏差 / 零成本 / 多重比较）

运行
  python redteam_sector.py            # 全量
  python redteam_sector.py --verbose  # 打印每个用例的引擎输出
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from datetime import date, timedelta
from typing import Any, Callable, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from discipline import evaluate, render_text                      # noqa: E402
import guards                                                     # noqa: E402
import valuation_anchor as va                                     # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")        # type: ignore[attr-defined]
    except Exception:
        pass

TODAY = date.today().isoformat()


def base(**kw) -> Dict[str, Any]:
    """合规基线：一切 [MUST] 均已满足，尾部友好（避免干扰单项断言）。
    PB=1.00 / 五档 0.90-0.95-1.30-1.60-1.90 → 极端下行10%、极端赔率比3.0、未触极端乐观。"""
    d = {
        "sector": "测试板块", "as_of": TODAY, "today": TODAY, "asset_class": "sector",
        "index_code": "931160", "index_name": "中证全指某行业",
        "demand": "growth", "volume": "up", "price": "up", "forward": "flat",
        # —— 行业周期量价硬闸门基线（2026-08-15）：所有用例默认声明行业供需口径，
        # 专测 Gate 的用例见 G 组（显式传错 basis / 错指标），预期引擎 raise。
        "vp_basis": "industry_cycle",
        "industry_volume_metric": "测试行业产量同比(行业供需周期口径)",
        "industry_price_metric": "测试行业核心品价格(行业供需周期口径)",
        "pb_now": 1.00, "pb_manual": 1.00,
        "pb_sources": {"mx-data": 1.00, "tdx": 1.01},
        "pb_pct": 25.0, "pb_pct_period": "5y", "pb_pct_10y": 20.0,
        "pb_series_years": 5.0, "pb_series_freq": "daily",
        # PE 分位周期默认标注（2026-08-18）：base() 号称"一切 MUST 均已满足"，但此前只标了 PB，
        # 凡声明 PE/BLEND 主锚的用例都会白拿一个"[MUST] PE 分位未标注周期"blocker。
        # 对只断言 dual_source/blocker 的用例无害，却会污染任何要求 status=OK 的正向控制（J2 原 FAIL 根因）。
        "pe_pct_period": "5y",
        # PE 镜像合规数据（2026-09-07 · 锚自动分类落地后必补）：
        # discipline.run() 现会对未声明锚的输入按 valuation_anchor.classify() 自动推断主锚
        # （严禁静默默认 PB），base 的多数 sector（测试板块/电池/芯片/AI算力/传媒…）被
        # 推断为 BLEND/PE → PE 必填字段缺失会白拿"[MUST] 缺少当前 PE"blocker。
        # 故 base 按其"一切 MUST 均已满足"哲学补齐 PE 全套：取值与 PB 五档等比镜像
        # （pe = pb × 20），pe_pct=25 与 pb_pct 相同 → BLEND eff=加权和仍=25，
        # 赔率/仓位/目标价数值与旧 PB-only 世界零漂移。
        # 专测"缺 PE 拦截"的用例（I6/I7）须显式剥离 PE 字段。
        "pe_now": 20.0, "pe_manual": 20.0,
        "pe_sources": {"eastmoney_est": 20.0, "ht_api_est": 20.2},
        "pe_field_label": "市盈率PE(TTM)",
        "pe_pct": 25.0, "pe_pct_10y": 20.0,
        "pe_series_years": 5.0, "pe_series_freq": "daily",
        "pe_p5": 18.0, "pe_p20": 19.0, "pe_p50": 26.0, "pe_p80": 32.0, "pe_p95": 38.0,
        "pb_p5": 0.90, "pb_p20": 0.95, "pb_p50": 1.30, "pb_p80": 1.60, "pb_p95": 1.90,
        "imagination": "B",
        # F2 前瞻证据链（2026-09-04 批次三 · params 1.10.0 起 MUST 档）：
        # base 号称"一切 MUST 均已满足"，故默认附一条合规证据；专测证据缺失的用例见 K1（显式置空）。
        "forward_evidence": [{"type": "orders", "desc": "测试行业订单/排产前瞻证据（红队基线合规项）",
                              "source": "redteam基线"}],
    }
    d.update(kw)
    return d


# =============================================================================
# 断言器
# =============================================================================
import llm_fill_guard as _lfg                                    # noqa: E402  (G3 L 组)


def _guard_base(**kw) -> Dict[str, Any]:
    """L 组 guard 用例基线：仅 guard 视角字段，机械合规（枚举合法/分位对账一致/无矛盾）。"""
    rng = [round(0.8 + 0.001 * i, 4) for i in range(1200)]       # 0.8→1.999 单调序列
    shift = kw.pop("pb_pct_shift", 0.0)
    drop_trend = kw.pop("drop_prosperity_trend", False)
    d = {
        "sector": "红队guard板块", "as_of": TODAY, "asset_class": "sector",
        "demand": "growth", "volume": "up", "price": "up", "forward": "flat",
        "imagination": "B", "crowding": "低",
        "_fill_meta": {"demand": "红队guard基线", "volume": "红队guard基线", "price": "红队guard基线",
                       "forward": "红队guard基线", "imagination": "红队guard基线"},
        "pb_now": 1.20, "pb_pct": round(_lfg.rank_pct(rng, 1.20) + shift, 2), "pb_series": rng,
        "prosperity_trend": "up", "prosperity_mom_pct": 1.5,
        "bottom_signals": 0,
    }
    if drop_trend:
        d.pop("prosperity_trend")
    d.update(kw)
    return d


def _guard(case: Dict[str, Any]) -> Dict[str, Any]:
    return _lfg.run(case)


def has_blocker(v: Dict[str, Any], kw: str) -> bool:
    return any(kw in b for b in v.get("blockers", []))


def has_warning(v: Dict[str, Any], kw: str) -> bool:
    return any(kw in w for w in v.get("warnings", []))


def has_flag(v: Dict[str, Any], kw: str) -> bool:
    return any(kw in f for f in v.get("tail_risk", {}).get("flags", []))


def has_trace(v: Dict[str, Any], kw: str) -> bool:
    return any(kw in t["rule"] and t["triggered"] for t in v.get("trace", []))


def pos_pct(v: Dict[str, Any]) -> float:
    return float(str(v.get("suggested_position", "0%")).rstrip("%"))


def _check_3_real_cases() -> bool:
    """端到端确认三真实案例(光伏PB / 电池BLEND / 港股互联网PE)的分类与分析一致：
    引擎产出 verdict 的 dual_source / final 两道闸须全绿。"""
    here = os.path.dirname(os.path.abspath(__file__))
    fp = os.path.join(here, "cases_3etf_corrected.json")
    if not os.path.exists(fp):
        raise FileNotFoundError(fp)
    cases = json.load(open(fp, encoding="utf-8"))
    for c in cases:
        v = evaluate(copy.deepcopy(c))
        ds = v["gates"]["dual_source"]
        fg = v["gates"]["final"]
        if not ds.get("ok") or not fg.get("ok"):
            raise AssertionError(
                f"{c['sector']} 闸未通过: dual_source={ds} final={fg}")
        # 使用主锚 == 声明主锚
        if v["valuation_anchor"] != (c.get("valuation_anchor") or "PB"):
            raise AssertionError(
                f"{c['sector']} 使用主锚{v['valuation_anchor']}≠声明{c.get('valuation_anchor')}")
    return True


def _mt_dual_records(n: int = 24) -> List[Dict[str, Any]]:
    """构造茅台双价格序列：出厂价 969→1369 提价↑，市场批价恒定 1695（flat，验证非 factory_up_market_down）"""
    recs = []
    fp = [969] * (n - 6) + [1369] * 6
    mp = [1695] * n
    fv = [100 - (i // 4) for i in range(n)]
    mv = [80 - (i // 6) for i in range(n)]
    for i in range(n):
        y, m = 2024 + i // 12, i % 12 + 1
        recs.append({"date": f"{y}-{m:02d}-15", "factory_price": fp[i],
                     "factory_volume": fv[i], "market_price": mp[i], "market_volume": mv[i]})
    return recs


_MT_DUAL = {"meta": {"symbol": "600519.SH", "name": "贵州茅台", "as_of": "2026-08-15",
                     "asset_type": "stock"}, "records": _mt_dual_records()}


CASES: List[Dict[str, Any]] = [
    # ---------------- A 组：框架否决不可被故事绕过 ----------------
    dict(id="A1", group="A", name="S级想象力+催化+超低PB 想救『价跌』",
         why="最常见的自我说服：AI/机器人题材跌了也想抄，用『想象力S+政策催化』合理化",
         data=base(sector="AI算力(价跌)", volume="up", price="down", imagination="S",
                   catalyst=True, catalysts=["大模型资本开支超预期"], forward="up",
                   pb_pct=5.0, bottom_signals=1),
         check=lambda v: (v["status"] == "REJECTED_FRAMEWORK" and v["win_rate"] == 0
                          and pos_pct(v) == 0.0),
         expect="胜率强制0、仓位0（价跌无板块牛市，§12.1 gate 优先于一切修正项）"),

    dict(id="A2", group="A", name="需求结构性塌方 + 量升价升",
         why="塌方行业短期也会有量价齐升的反弹（报纸涨价），框架必须拒绝",
         data=base(sector="报纸传媒", demand="collapse", volume="up", price="up",
                   imagination="D", pb_pct=3.0),
         check=lambda v: v["win_rate"] == 0 and v["status"] == "REJECTED_FRAMEWORK",
         expect="需求塌方 gate 触发，胜率0"),

    dict(id="A3", group="A", name="想象力D级（无想象）但量价与PB完美",
         why="D级=需求塌方级，仓位系数0，不能因量价好就放行",
         data=base(sector="传统媒体", imagination="D", volume="up", price="up", pb_pct=2.0),
         check=lambda v: v["win_rate"] == 0 and pos_pct(v) == 0.0,
         expect="D级直接出局"),

    dict(id="A4", group="A", name="价跌 + 见底6信号仅2个（未达3）",
         why="『快见底了』是最贵的错觉，未达3个信号必须继续回避",
         data=base(sector="光伏", volume="up", price="down", bottom_signals=2, pb_pct=15.0),
         check=lambda v: (v["win_rate"] == 0 and not v["left_side_channel"]
                          and v["status"] == "REJECTED_FRAMEWORK"),
         expect="6信号<3 → 仍然否决，不得进入左侧通道"),

    dict(id="A5", group="A", name="价跌 + 见底6信号4个 → 左侧通道",
         why="电池ETF 场景：允许左侧布局，但不得套用六档表给出30%胜率、不得重仓",
         data=base(sector="电池(锂盐见底)", volume="up", price="down", bottom_signals=4,
                   pb_pct=11.2, imagination="A"),
         check=lambda v: (v["win_rate"] is None and v["left_side_channel"]
                          and "待拐点" in v["win_rate_label"] and pos_pct(v) <= 10.0
                          and "等待布局" in v["quadrant"]),
         expect="胜率标『待拐点』不产出数值、仓位≤10%、落等待布局区"),

    dict(id="A6", group="A", name="价值陷阱：需求侵蚀确认 + PB分位0%",
         why="白酒案例：PB 0% 看着赔率100，实则 BV/EPS 将随需求下沉重估",
         data=base(sector="白酒", demand="erosion", erosion_signals=3,
                   terminal_price_down=True, volume="flat", price="up",
                   pb_pct=0.0, imagination="C"),
         check=lambda v: (v["value_trap"] and "陷阱" in v["quadrant"]
                          and has_trace(v, "价值陷阱")),
         expect="标价值陷阱、胜率−10、象限判『陷阱回避』而非等待布局"),

    # ---------------- B 组：PB 口径错配（赔率生命线）----------------
    dict(id="B1", group="B", name="行业均值PB 冒充所选指数PB（高德红外原型）",
         why="通达信 ph_agf10_gzfx 返回军工行业PB均值3.04，真实个股8.44 —— 满盘皆输",
         data=base(sector="军工电子", pb_now=3.04, pb_manual=8.44,
                   pb_field_label="申万一级行业均值PB",
                   pb_sources={"tdx": 3.04, "mx-data": 3.05}),
         check=lambda v: (v["status"] == "PENDING_VERIFICATION"
                          and (has_blocker(v, "警戒词") or has_blocker(v, "手算"))),
         expect="警戒词+手算差>20% 双重拦截，结论标待核验"),

    dict(id="B2", group="B", name="全历史分位当5年分位（电池ETF原型）",
         why="理杏仁 64.28% 是11.5年全历史，真实5年≈33% —— 结论会整体反转",
         data=base(sector="电池ETF", pb_pct=64.28, pb_pct_period="all"),
         check=lambda v: (v["status"] == "PENDING_VERIFICATION"
                          and has_blocker(v, "全历史")),
         expect="全历史分位禁止决策"),

    dict(id="B3", group="B", name="PB 分位未标周期",
         why="不标周期就无法判断口径，历史上两次错都源于此",
         data=base(pb_pct_period=""),
         check=lambda v: has_blocker(v, "未标注周期"),
         expect="视为待核验，不得进入胜率映射"),

    dict(id="B4", group="B", name="PB 单源（未做双源交叉）",
         why="§四 协议②要求≥2独立源差异<5%",
         data=base(pb_sources={"mx-data": 1.00}),
         check=lambda v: has_blocker(v, "双源"),
         expect="单源 → 待核验"),

    dict(id="B5", group="B", name="多源 PB 分歧 12%（>5%）",
         why="不同指数编译器/加权方式混用的典型表征",
         data=base(pb_sources={"mx-data": 1.00, "ifind": 1.12}),
         check=lambda v: has_blocker(v, "多源 PB 分歧"),
         expect="不采信，须核对编译器口径"),

    dict(id="B6", group="B", name="跨境ETF 未标加权方式（港股通互联网原型）",
         why="市值加权38% vs 等权71%，差32pp，不标注就等于没有结论",
         data=base(sector="港股通互联网", is_cross_border=True, weighting=""),
         check=lambda v: has_blocker(v, "加权方式"),
         expect="强制标注加权方式+数据源"),

    dict(id="B7", group="B", name="ETF 未查跟踪指数代码",
         why="申万通信服务29.95% ≠ 中证全指通信设备97.85%，混用即致命",
         data=base(index_code=""),
         check=lambda v: has_blocker(v, "跟踪指数代码"),
         expect="必须先查指数代码再取分位"),

    dict(id="B8", group="B", name="成分股 PB 离散度 45pp 未下钻",
         why="新能源内锂矿/隔膜/电池差50pp+，一级行业PB是平均出来的假象",
         data=base(constituent_pb_dispersion_pp=45.0, dispersion_drilled=False),
         check=lambda v: has_blocker(v, "离散度"),
         expect="强制下钻子行业"),

    dict(id="B9", group="B", name="五档分位非单调（P80 < P50）",
         why="§12.3 示例曾因回填导致 P80(1.89)<P50(2.10)，极端情景全失真",
         data=base(pb_p50=1.60, pb_p80=1.30),
         check=lambda v: has_blocker(v, "非单调"),
         expect="百分位序列失真 → 重算"),

    dict(id="B10", group="B", name="分位窗口仅1.2年（新指数）",
         why="窗口不足的分位没有统计意义，却最容易被当成『历史低位』",
         data=base(pb_series_years=1.2),
         check=lambda v: (has_warning(v, "窗口") and
                          any(h["no"] == 6 and h["triggered"] for h in v["hard_signals"])),
         expect="降级标『窗口不足』，可靠性低"),

    # ---------------- C 组：适用边界 / 新鲜度 ----------------
    dict(id="C1", group="C", name="宽基ETF（沪深300）套用行业供需框架",
         why="宏观beta 非行业供需，框架明确排除",
         data=base(sector="沪深300ETF", asset_class="broad"),
         check=lambda v: v["status"] == "REJECTED_SCOPE" and pos_pct(v) == 0.0,
         expect="直接判不适用"),

    dict(id="C2", group="C", name="黄金ETF（商品）",
         why="无量价格局可言",
         data=base(sector="黄金ETF", asset_class="commodity"),
         check=lambda v: v["status"] == "REJECTED_SCOPE",
         expect="直接判不适用"),

    dict(id="C3", group="C", name="数据 120 天前（过期）",
         why="板块量价/PB 两个月以上就可能反转",
         data=base(as_of=(date.today() - timedelta(days=120)).isoformat()),
         check=lambda v: has_blocker(v, "数据过期"),
         expect="须重取，不得决策"),

    dict(id="C4", group="C", name="as_of 是未来日期（未来数据）",
         why="回测/实盘里最隐蔽的未来函数入口",
         data=base(as_of=(date.today() + timedelta(days=5)).isoformat()),
         check=lambda v: has_blocker(v, "未来数据") or has_blocker(v, "晚于当前日期"),
         expect="识别为未来函数并拦截"),

    dict(id="C5", group="C", name="数据 10 天前（O7 偏旧预警档，非过期）",
         why="O7 收紧 freshness_max_days=15 后，7~15 天应给『偏旧』warning 但不 blocker，结论可用但提示分位带可能漂移",
         data=base(as_of=(date.today() - timedelta(days=10)).isoformat()),
         check=lambda v: (has_warning(v, "数据已") and not has_blocker(v, "数据过期")
                          and v["status"] == "OK"),
         expect="给偏旧预警、不拦截、status=OK"),

    # ---------------- D 组：极端情景 / 尾部纪律 ----------------
    dict(id="D1", group="D", name="触及极端乐观×90%（涨过头）",
         why="『还有空间』是牛市末期最贵的幻觉",
         data=base(sector="芯片", pb_now=1.75, pb_manual=1.75,
                   pb_sources={"mx-data": 1.75, "tdx": 1.76},
                   pb_pct=95.0, pb_pct_10y=96.0),
         check=lambda v: (v["forced_reduce"] and pos_pct(v) == 0.0
                          and has_flag(v, "涨过头")),
         expect="强制减仓/清仓，目标仓位归零"),

    dict(id="D2", group="D", name="极端下行 40%（尾部风险高）",
         why="三档悲观看着可控，真崩盘 PB 回历史最低还能跌40%",
         data=base(pb_p5=0.60, pb_p20=0.95),
         check=lambda v: (has_flag(v, "尾部风险高")
                          and any("分批" in n for n in v["execution_notes"])),
         expect="标红+强制分批建仓"),

    dict(id="D3", group="D", name="左尾极厚（极端下行 > 悲观下行×2）",
         why="分布左尾厚 → 三档模型系统性低估风险，结论必须降一档",
         data=base(pb_p5=0.70, pb_p20=0.98),
         check=lambda v: (v["tail_risk"].get("downgrade_one_notch") is True
                          and any("降一档" in n for n in v["execution_notes"])),
         expect="仓位×0.5 实际降档（不只是口头提示）"),

    dict(id="D4", group="D", name="已跌破历史最惨 P5",
         why="『破净破历史』常被当机会，实为基本面崩塌信号",
         data=base(pb_now=0.85, pb_manual=0.85,
                   pb_sources={"mx-data": 0.85, "tdx": 0.85}, pb_pct=0.5),
         check=lambda v: has_flag(v, "禁止接飞刀"),
         expect="系统性崩盘警示"),

    dict(id="D5", group="D", name="五档缺失（未实查 P5/P95）",
         why="没有极端档就等于没有尾部认知",
         data=base(pb_p5=None, pb_p95=None),
         check=lambda v: has_blocker(v, "五档参考线/现价缺失"),
         expect="强制实查近5年日频序列"),

    # ---------------- E 组：白名单封闭性 ----------------
    dict(id="E1", group="E", name="自创字段夹带主观修正",
         why="『我觉得景气好 +15%』是框架最想杜绝的行为",
         data=dict(base(), **{"my_bonus": 15}),
         raises=ValueError,
         expect="引擎直接 raise，无自由裁量入口"),

    dict(id="E2", group="E", name="修正项超调（C级 + 前瞻↓ = −20）",
         why="不 clamp 会让单类修正把胜率打穿",
         data=base(volume="up", price="flat", imagination="C", forward="down",
                   pb_pct=50.0, pb_pct_10y=50.0),
         check=lambda v: v["win_rate"] == 55 and has_trace(v, "分组 clamp"),
         expect="想象/前瞻合并 clamp 到 −10 → 65−10=55"),

    dict(id="E3", group="E", name="修正项叠满（S+前瞻↑+催化+低PB）",
         why="正向也要封顶，避免『样样都好』推出 100% 胜率",
         data=base(imagination="S", forward="up", catalyst=True, pb_pct=10.0,
                   volume="up", price="up"),
         check=lambda v: v["win_rate"] <= 90,
         expect="全局封顶 90%"),

    dict(id="E4", group="E", name="非法量价组合",
         why="枚举外输入必须失败而非静默给默认值",
         data=base(volume="soaring"),
         raises=ValueError,
         expect="raise，不静默"),

    # ---------------- F 组：量化通病 ----------------
    dict(id="F1", group="F", name="未来函数：引用 as_of 之后的数据点",
         why="回测里最常见的作弊",
         guard=lambda: guards.assert_no_lookahead(
             "2026-06-30", [("PB", "2026-07-15")]),
         raises=guards.LookaheadError,
         expect="assert_no_lookahead 拦截"),

    dict(id="F2", group="F", name="同日成交（信号日=执行日）",
         why="用收盘信号做收盘成交 = 变相未来函数",
         guard=lambda: guards.assert_t_plus_1("2026-06-30", "2026-06-30"),
         raises=guards.LookaheadError,
         expect="强制 t+1 执行"),

    dict(id="F3", group="F", name="幸存者偏差：缺 listed_date 的宇宙",
         why="只用今天还活着的 ETF 回测 → 收益虚高",
         guard=lambda: guards.filter_pit_universe(
             [{"code": "515880", "name": "通信ETF"}], "2026-06-30", strict=True),
         raises=guards.SurvivorshipError,
         expect="拒绝无上市日期的宇宙"),

    dict(id="F4", group="F", name="point-in-time 过滤（未成立/已退市）",
         why="ETF 成立于 2025 却参与 2020 回测",
         guard=lambda: guards.filter_pit_universe(
             [{"code": "A", "listed_date": "2019-01-01"},
              {"code": "B", "listed_date": "2025-01-01"},
              {"code": "C", "listed_date": "2018-01-01", "delisted_date": "2021-01-01"}],
             "2022-06-30"),
         check_guard=lambda r: [x["code"] for x in r] == ["A"],
         expect="只留当时确实存在的标的"),

    dict(id="F5", group="F", name="零成本回测",
         why="不计成本 = 系统性高估收益",
         guard=lambda: guards.check_backtest_realism(0.0, 36, strict=True),
         raises=guards.GuardError,
         expect="成本必须 >0"),

    dict(id="F6", group="F", name="样本期不足（6期）",
         why="6 个月数据谈不上验证",
         guard=lambda: guards.check_backtest_realism(5.0, 6, strict=True),
         raises=guards.GuardError,
         expect="期数 ≥12"),

    dict(id="F7", group="F", name="过拟合：样本量不足参数量门槛",
         why="9 个自由参数至少需 90 样本，否则只能叫『启发式未校准』",
         guard=lambda: guards.OverfitGuard(9).check_sample_size(25, strict=True),
         raises=guards.OverfitGuardError,
         expect="拒绝宣称『回测验证有效』"),

    dict(id="F8", group="F", name="多重比较：试 12 组参数",
         why="试得越多，最优参数越可能是噪声",
        guard=lambda: (lambda g: (lambda _: g.multiple_testing_report())(
                        [g.record_trial(f"t{i}") for i in range(12)]))(guards.OverfitGuard(9)),
        check_guard=lambda r: r["warn"] and r["bonferroni_alpha"] < 0.005,
         expect="Bonferroni 收紧显著性门槛并告警"),

    dict(id="F9", group="F", name="参数指纹篡改（偷偷调阈值）",
         why="事后调参凑结论是过拟合的温床",
         guard=lambda: guards.assert_params_frozen(
             {"win_base": {"up|up": 95}}, "deadbeef" * 8, strict=True),
         raises=guards.OverfitGuardError,
         expect="指纹不一致即报错"),

    dict(id="F10", group="F", name="as-of 滚动分位（不用全样本）",
         why="用全样本分位给历史时点打标是最隐蔽的未来函数",
         guard=lambda: guards.asof_percentile(
             [(f"2021-{m:02d}-01", 1.0 + m * 0.01) for m in range(1, 13)] +
             [(f"2022-{m:02d}-01", 5.0) for m in range(1, 13)],
             "2021-12-31", window_years=5.0, min_points=5),
         check_guard=lambda r: r is not None and r["n"] == 12 and r["p95"] < 2.0,
         expect="2022 的高值不得污染 2021 时点的分位"),

    # ---------------- G 组：行业周期量价 Gate（2026-08-15 新增）----------------
    # 防止把「ETF 换手率/指数点位/整体市场」误当量价的同类错误（含 Flash 模型）。
    dict(id="G1", group="G", name="vp_basis=etf_turnover（ETF换手率当量价）",
         why="历史实错：把 ETF 日K 换手率当行业量 → 光伏/电池误判黄金区",
         data=base(sector="光伏", vp_basis="etf_turnover",
                   industry_volume_metric="ETF 近20日换手率",
                   industry_price_metric="光伏组件价格"),
         raises=ValueError,
         expect="引擎拒绝产出裁决（行业周期量价 Gate 拦截）"),

    dict(id="G2", group="G", name="行业量指标含禁止词 ETF换手率",
         why="vp_basis 正确但指标文本仍混入 ETF 交易口径",
         data=base(sector="电池", industry_volume_metric="ETF 60日成交量",
                   industry_price_metric="电芯价格(元/Wh)"),
         raises=ValueError,
         expect="命中禁止词 → 硬失败"),

    dict(id="G3", group="G", name="未声明 vp_basis",
         why="任何分析必须显式声明量价口径，禁止留空默认",
         data=base(sector="港股互联网", vp_basis="",
                   industry_volume_metric="", industry_price_metric=""),
         raises=ValueError,
         expect="未声明 vp_basis → 硬失败"),

    dict(id="G4", group="G", name="行业价=指数点位（把指数自身价格当量价）",
         why="用户明确排除：量价非『该指数本身的价格与成交量关系』",
         data=base(sector="光伏", industry_volume_metric="全球光伏新增装机量(GW)",
                   industry_price_metric="指数点位 2554"),
         raises=ValueError,
         expect="命中禁止词『指数点位』→ 硬失败"),

    dict(id="G5", group="G", name="行业量方向与 volume 字段矛盾",
         why="捕捉 Flash 把 ETF 量填进 volume 的错位：行业量下行却填量升",
         data=base(sector="光伏", volume="up", industry_volume_trend="down",
                   industry_volume_metric="全球光伏新增装机量同比",
                   industry_price_metric="光伏组件价格"),
         raises=ValueError,
         expect="industry_volume_trend 与 volume 矛盾 → 硬失败"),

    # ---------------- H 组：量升价跌·成本通缩降档通道（§12.1b · 2026-08-15）----------------
    dict(id="H1", group="H", name="量升价跌+结构性增长+cost_deflation → 降档放行",
         why="电池场景：装车+41.6%/储能+75% 量升，电芯价跌源于锂价成本传导（非供过于求）→ 不应硬置零",
         data=base(sector="电池(成本通缩)", volume="up", price="down", demand="growth",
                   forward="flat", imagination="A", pb_pct=50.0, bottom_signals=1,
                   industry_volume_metric="动力电池装车量同比 +41.6%",
                   industry_price_metric="电芯价格(元/Wh) 成本传导下行",
                   price_decline_nature="cost_deflation"),
         check=lambda v: (v["status"] == "OK" and v["win_rate"] is not None
                          and 25 <= v["win_rate"] <= 40 and pos_pct(v) <= 15.0
                          and v["win_rate"] != 0),
         expect="胜率 25~40%（非0）、仓位≤15%『谨慎参与』、status=OK"),

    dict(id="H2", group="H", name="量升价跌但未声明价跌性质 → 仍硬否决0%",
         why="降档须显式声明，缺声明一律维持『价跌无板块牛市』",
         data=base(sector="电池(未声明)", volume="up", price="down", demand="growth",
                   bottom_signals=1,
                   industry_volume_metric="动力电池装车量同比",
                   industry_price_metric="电芯价格(元/Wh)"),
         check=lambda v: v["win_rate"] == 0 and v["status"] == "REJECTED_FRAMEWORK"
                         and pos_pct(v) == 0.0,
         expect="未声明 cost_deflation → 胜率0、框架否决"),

    dict(id="H3", group="H", name="量升价跌+oversupply_war（供过于求价格战）→ 仍0%",
         why="光伏2023-25原型：量升但产能过剩价格战，绝不能因『量升』就放行",
         data=base(sector="光伏(供给过剩)", volume="up", price="down", demand="growth",
                   bottom_signals=1, imagination="C",
                   industry_volume_metric="全球光伏装机量同比",
                   industry_price_metric="光伏组件价格(元/W)",
                   price_decline_nature="oversupply_war"),
         check=lambda v: v["win_rate"] == 0 and v["status"] == "REJECTED_FRAMEWORK",
         expect="oversupply_war → 仍价跌硬否决0%（降档通道只认 cost_deflation）"),

    dict(id="H4", group="H", name="cost_deflation 但无成本传导佐证 → Gate 拒绝",
         why="防 Flash 把任何价跌都声明成 cost_deflation 逃避否决：必须有成本传导证据文本",
         data=base(sector="电池(无佐证)", volume="up", price="down", demand="growth",
                   industry_volume_metric="装车量同比",
                   industry_price_metric="电芯价格(元/Wh)",
                   price_decline_nature="cost_deflation"),
         raises=ValueError,
         expect="指标/注记无成本传导关键词 → Gate 硬失败"),

    dict(id="H5", group="H", name="cost_deflation 但量非升（量缩价跌）→ Gate 拒绝",
         why="降档通道只对『量升价跌』开放，量缩价跌仍是结构性排除",
         data=base(sector="光伏(量缩价跌)", volume="down", price="down", demand="growth",
                   industry_volume_metric="装机量同比 -26%",
                   industry_price_metric="组件价格，成本传导",
                   price_decline_nature="cost_deflation"),
         raises=ValueError,
         expect="volume≠up → 降档不成立 → Gate 硬失败"),

    # ---------------- I 组：估值主锚分类 + 双源/finalgate 分发（2026-08-15 用户裁定）----------------
    # 验证「行业按 PB/PE/BLEND 分类」逻辑合理，且分析确实按分类执行（双源闸+finalgate 守住）。
    dict(id="I1", group="I", name="分类表：光伏→PB",
         why="重资产强周期，下行期盈利常失真，PB 为更干净主锚",
         guard=lambda: va.classify("光伏产业").anchor,
         check_guard=lambda r: r == "PB",
         expect="光伏→PB"),

    dict(id="I2", group="I", name="分类表：电池→BLEND",
         why="重资产制造+结构性成长，PE 与 PB 均重要→动态混合",
         guard=lambda: va.classify("电池").anchor,
         check_guard=lambda r: r == "BLEND",
         expect="电池→BLEND"),

    dict(id="I3", group="I", name="分类表：港股通互联网→PE",
         why="平台型轻资产、盈利高增、账面价值失真→PE 主锚",
         guard=lambda: va.classify("港股通互联网").anchor,
         check_guard=lambda r: r == "PE",
         expect="港股通互联网→PE"),

    dict(id="I4", group="I", name="PB 主锚：引擎按 PB 计算且双源/finalgate 通过",
         why="声明 PB 的板块，估值赔率应仅看 PB 分位，且双源闸+finalgate 全绿",
         data=base(sector="银行", valuation_anchor="PB", pb_pct=12.0, pb_pct_10y=8.0,
                   pb_p5=0.55, pb_p20=0.70, pb_p50=0.95, pb_p80=1.20, pb_p95=1.60),
         check=lambda v: (v["valuation_anchor"] == "PB"
                          and v["status"] == "OK"
                          and v["gates"]["dual_source"]["ok"] is True
                          and v["gates"]["final"]["ok"] is True),
         expect="使用主锚==PB、status=OK、双源闸+finalgate 均通过"),

    dict(id="I5", group="I", name="PE 主锚：引擎按 PE 计算且双源/finalgate 通过",
         why="声明 PE 的板块，估值赔率应看 PE 分位，PB 双源校验不适用（由 gate 正确跳过）",
         data=base(sector="互联网平台", valuation_anchor="PE",
                   pe_now=20.0, pe_manual=19.6,
                   pe_sources={"eastmoney_est": 20.0, "ht_api_est": 19.8},
                   pe_pct=28.0, pe_pct_period="5y", pe_pct_10y=25.0,
                   pe_series_years=5.0, pe_series_freq="monthly",
                   pe_p5=14.0, pe_p20=17.0, pe_p50=24.0, pe_p80=32.0, pe_p95=40.0),
         check=lambda v: (v["valuation_anchor"] == "PE"
                          and v["status"] == "OK"
                          and v["gates"]["dual_source"]["ok"] is True
                          and v["gates"]["final"]["ok"] is True
                          and "不适用" in v["valuation_check"]["pb_cross"]),
         expect="使用主锚==PE、status=OK、双源闸+finalgate 通过、PB 校验正确标注『不适用』"),

    dict(id="I6", group="I", name="PE 主锚但仅填 PB 数据 → finalgate 拦截",
         why="声明 PE 却缺 PE 字段 → finalgate ③ 数据不齐备必须 FAIL，不得静默按 PB 算",
         data=base(sector="银行(误填PE锚)", valuation_anchor="PE",
                   pe_now=None, pe_manual=None, pe_sources={}, pe_pct=None,
                   pe_p5=None, pe_p20=None, pe_p50=None, pe_p80=None, pe_p95=None),
         check=lambda v: (v["gates"]["final"]["ok"] is False
                          and v["status"] != "OK"
                          and has_blocker(v, "PE/BLEND 主锚缺 PE")),
         expect="finalgate ③ 失败→status≠OK、标 PE 数据缺失 blocker"),

    dict(id="I7", group="I", name="BLEND 主锚但缺 PE 数据 → 双源闸拦截",
         why="BLEND要求 PB 与 PE 双源同时达标，缺 PE → 双源闸 FAIL",
         data=base(sector="半导体(误缺PE)", valuation_anchor="BLEND",
                   pb_pct=45.0, pb_pct_10y=40.0,
                   pb_p5=2.0, pb_p20=2.5, pb_p50=3.5, pb_p80=4.5, pb_p95=6.0,
                   pe_now=None, pe_manual=None, pe_sources={}, pe_pct=None,
                   pe_p5=None, pe_p20=None, pe_p50=None, pe_p80=None, pe_p95=None),
         check=lambda v: (v["gates"]["dual_source"]["ok"] is False
                          and has_blocker(v, "PE 缺少")),
         expect="双源闸 PE 项 FAIL→标 PE 缺少 blocker"),

    dict(id="I8", group="I", name="三真实案例(光伏PB/电池BLEND/港股PE)全过双源+finalgate",
         why="端到端确认分类后的三案例分析严格按要求完成（分类与分析一致）",
         guard=lambda: _check_3_real_cases(),
         check_guard=lambda r: r is True,
         expect="三案例 verdict 的 dual_source.ok 与 final.ok 全 True"),

    dict(id="I9", group="I", name="O6 BLEND自适应权重：电池PB偏高/白酒PE偏高",
         why="关V6：重资产成长(电池)应 PB 权重0.60、轻资产消费(白酒)应 PE 权重0.60，非全局0.6/0.4；未命中回退默认",
         guard=lambda: (va.blend_weights_for("电池")[0:2],
                        va.blend_weights_for("白酒")[0:2],
                        va.blend_weights_for("某未知行业X")[0:2]),
         check_guard=lambda r: r == ((0.40, 0.60), (0.60, 0.40), (0.6, 0.4)),
         expect="电池→(0.40,0.60) 白酒→(0.60,0.40) 未知→默认(0.6,0.4)"),

    dict(id="I10", group="I", name="O9 制度周期断裂：regime_break 且10y可用 → 切10y",
         why="关V12：上游标记结构性断裂时，有效分位切至10y口径（更稳健），并发 SHOULD 告警，status 仍可 OK",
         data=base(sector="某重构指数", valuation_anchor="PB",
                   pb_pct=25.0, pb_pct_10y=15.0, regime_break=True),
         check=lambda v: (has_warning(v, "[O9]") and v["status"] == "OK"
                          and abs(v["valuation_check"]["eff_percentile"] - 15.0) < 0.01),
         expect="eff 切至10y=15.0、发[O9]告警、status=OK"),

    dict(id="I11", group="I", name="O9 制度周期断裂：regime_break 但10y缺失 → 维持5y降级告警",
         why="10y 分位缺失时不应幻觉切换，维持5y但发可靠性降级告警",
         data=base(sector="某重构指数(无10y)", pb_pct=25.0, pb_pct_10y=None, regime_break=True),
         check=lambda v: (has_warning(v, "[O9]") and v["status"] == "OK"
                          and abs(v["valuation_check"]["eff_percentile"] - 25.0) < 0.01),
         expect="eff 维持5y=25.0、发[O9]告警、status=OK"),

    dict(id="I12", group="I", name="O8 EV 持有期/年化：默认24月，年化<点估计",
         why="关V7：EV 原 horizon-agnostic；现须带 horizon_months 且年化口径随持有期压缩（正EV年化低于点估计）",
         data=base(sector="EV年化检验", volume="up", price="up", pb_pct=25.0, pb_pct_10y=20.0,
                   pb_p5=0.90, pb_p20=0.95, pb_p50=1.30, pb_p80=1.60, pb_p95=1.90),
         check=lambda v: (v["ev"] is not None
                          and v["ev"].get("horizon_months") == 24
                          and v["ev"].get("ev_mid_annualized_pct") is not None
                          and v["ev"]["ev_mid_annualized_pct"] < v["ev"]["ev_mid_pct"]),
         expect="horizon_months=24、年化EV中<点估计EV中"),

    # ---------------- J 组：双价格强制溯源闸门（2026-08-18 修复512690酒ETF误判根因）----------------
    dict(id="J1", group="J", name="酒ETF 出厂提价误读为量缩价升（精确复现512690根因）",
         why="512690原型：分析师把『茅台出厂三次提价(1499→1639→1753)』提炼为 price=up，但行业价口径已写『批价企稳』，"
              "且整个 case 无 dual_price_data → 旧引擎静默放行、⑪缺失即True。新闸门须阻断。",
         data=base(sector="中证酒(399987)", index_name="中证酒", index_code="399987.SZ",
                   volume="down", price="up",
                   industry_volume_metric="白酒产量/渠道动销(行业供需周期口径)",
                   industry_price_metric="飞天茅台批价(1695-1700元企稳)及五粮液批价(730→800元回升)·"
                                          "茅台一个月三次提价(1499→1639→1753)",
                   pb_now=4.00, pb_manual=4.00, pb_sources={"mx-data": 4.00, "ifind": 4.01},
                   pb_pct=5.9, pb_pct_10y=4.0, pb_series_years=5.0, pb_series_freq="daily",
                   pb_p5=3.0, pb_p20=3.6, pb_p50=6.0, pb_p80=8.0, pb_p95=11.0,
                   pe_now=19.95, pe_manual=19.8, pe_sources={"eastmoney_est": 19.95, "ht_api_est": 19.9},
                   pe_pct=7.0, pe_pct_10y=5.0, pe_series_years=5.0, pe_series_freq="monthly",
                   pe_p5=14.0, pe_p20=17.0, pe_p50=24.0, pe_p80=32.0, pe_p95=40.0,
                   valuation_anchor="BLEND", demand="growth", forward="up", bottom_signals=4,
                   imagination="B", dual_price_data={}),
         check=lambda v: (v["status"] != "OK"
                          and v["checklist"]["⑪双价格传导校验"] is False
                          and has_blocker(v, "双价格溯源缺失")
                          and has_blocker(v, "量价方向矛盾")),
         expect="status≠OK、⑪=False、双价格溯源缺失+量价方向矛盾 双 blocker（出厂提价误读被拦截）"),

    dict(id="J2", group="J", name="酒ETF 补 dual_price_data → 闸门放行（单变量正向控制）",
         why="与 J1 **唯一差异 = 补上 dual_price_data**（出厂 969→1369↑、市场批价恒 1695 flat）。"
              "同样的 price=up 误报输入，引擎应：①按市场侧把量价重写为(下|稳) ②方向矛盾随之消解 "
              "③不再触发溯源缺失 blocker ④⑪=True、status=OK。确认闸门不误伤已尽调的正常路径。",
         data=base(sector="中证酒(399987)", index_name="中证酒", index_code="399987.SZ",
                   volume="down", price="up",
                   industry_volume_metric="白酒产量/渠道动销(行业供需周期口径)",
                   industry_price_metric="飞天茅台批价(1695-1700元企稳)及五粮液批价(730→800元回升)·"
                                          "茅台一个月三次提价(1499→1639→1753)",
                   pb_now=4.00, pb_manual=4.00, pb_sources={"mx-data": 4.00, "ifind": 4.01},
                   pb_pct=5.9, pb_pct_10y=4.0, pb_series_years=5.0, pb_series_freq="daily",
                   pb_p5=3.0, pb_p20=3.6, pb_p50=6.0, pb_p80=8.0, pb_p95=11.0,
                   pe_now=19.95, pe_manual=19.8, pe_sources={"eastmoney_est": 19.95, "ht_api_est": 19.9},
                   pe_pct=7.0, pe_pct_10y=5.0, pe_series_years=5.0, pe_series_freq="monthly",
                   pe_p5=14.0, pe_p20=17.0, pe_p50=24.0, pe_p80=32.0, pe_p95=40.0,
                   valuation_anchor="BLEND", demand="growth", forward="up", bottom_signals=4,
                   imagination="B", dual_price_data=_MT_DUAL),
         check=lambda v: (v["status"] == "OK"
                          and v["checklist"]["⑪双价格传导校验"] is True
                          and not has_blocker(v, "双价格溯源缺失")
                          and not has_blocker(v, "量价方向矛盾")
                          # 关键：引擎须真的把量价收敛到市场侧（price 不再是输入的 up）
                          and (v.get("dual_price_check") or {}).get(
                              "_effective_volume_price", {}).get("price") != "up"),
         expect="status=OK、⑪=True、无溯源/方向 blocker、量价已重写至市场侧(price≠up)"),

    dict(id="J3", group="J", name="分歧行业但如实申报价跌 → 方向门放行（防 I8 过度拦截回归）",
         why="电池ETF原型：价格口径含『锂价成本下行传导』命中分歧行业关键词，但已如实申报 price=down。"
              "误判风险是单向的——只有 price=up 才可能把出厂提价误读为价升而**虚高**结论；"
              "price=down 已在保守侧。若此处仍硬阻断，会把如实申报价跌的正常分析打成待核验"
              "（2026-08-18 I8 电池931719 实际死因：⑪=False → 终闸⑦ FAIL）。",
         data=base(sector="电池", index_name="中证电池", index_code="931719.CSI",
                   volume="up", price="down", demand="growth", forward="flat",
                   imagination="A", pb_pct=50.0, bottom_signals=1,
                   industry_volume_metric="动力电池装车量(GWh)+储能出货量 同比 +41.6%/+75%",
                   industry_price_metric="动力电池电芯价格(元/Wh) 0.48→0.44，"
                                          "主因锂价成本下行传导+以价换量(非供过于求)",
                   price_decline_nature="cost_deflation"),
         check=lambda v: (v["status"] == "OK"
                          and v["checklist"]["⑪双价格传导校验"] is True
                          and not has_blocker(v, "双价格溯源缺失")
                          and has_warning(v, "[SHOULD] 双价格溯源缺失")),
         expect="status=OK、⑪=True、无 blocker、仅留 [SHOULD] 溯源提示（保守侧不阻断）"),

    dict(id="K1", group="K", name="F2-MUST：forward=up 无前瞻证据 → 硬拦截",
         why="params 1.10.0 起 forward_evidence_required=True。forward=up 是翻转右侧买点/"
              "价值陷阱判定的最大杠杆，无带来源证据（订单/排产/远期曲线/上修/政策）不得放行。",
         data=base(forward="up", forward_evidence=[]),
         check=lambda v: (v["status"] == "PENDING_VERIFICATION"
                          and v["suggested_position"] == "0.0%"
                          and (v.get("forward_check") or {}).get("blocked") is True
                          and has_blocker(v, "F2 前瞻方向证据链缺失")),
         expect="status=PENDING_VERIFICATION、仓位0、forward_check.blocked=True、F2 blocker 在案"),
    dict(id="K2", group="K", name="F2-MUST：附带来源证据 → 放行（正向控制）",
         why="与 K1 唯一差异 = 补上 forward_evidence（带 desc+source）。确认 MUST 档不误伤已尽调路径。",
         data=base(forward="up",
                   forward_evidence=[{"type": "consensus_revision",
                                      "desc": "测试行业一致预期上修占比70%", "source": "redteam正向控制"}]),
         check=lambda v: (v["status"] == "OK"
                          and (v.get("forward_check") or {}).get("evidence_count") == 1
                          and (v.get("forward_check") or {}).get("blocked") is False
                          and not has_blocker(v, "F2")),
         expect="status=OK、evidence_count=1、无 F2 blocker"),
    dict(id="K3", group="K", name="F2：forward=flat 无前瞻主张 → 不要求证据",
         why="F2 只约束 forward=up/down 的前瞻主张；flat（含老用例）两档均零要求，保证向后兼容。",
         data=base(forward="flat", forward_evidence=[]),
         check=lambda v: (v["status"] == "OK"
                          and not has_blocker(v, "F2")
                          and (v.get("forward_check") or {}).get("note") == "forward=flat 无前瞻主张 → 不要求证据链"),
         expect="status=OK、无 F2 blocker、note=flat 不要求"),

    # ================= L 组 · LLM 轨机械复核层（G3 · llm_fill_guard）=================
    # 用例直接打 guard（不进引擎）：guard 返回 {ok, blockers, warnings}。
    dict(id="L1", group="L", name="GUARD：PB分位抄错 10pp → MUST 拦截",
         why="LLM 从研报抄分位最常见错误=数值抄错。guard 用 pb_series 反算分位，"
             "与声明差 >3pp 即拦——抄错/编数必现形（bands 反算对账，四道检查里最强）。",
         guard=lambda: _guard(_guard_base(pb_pct_shift=10.0)),
         check_guard=lambda r: (not r["ok"] and len(r["blockers"]) == 1
                                and "分位对账失败" in r["blockers"][0]),
         expect="ok=False、1 条 MUST（分位对账失败）"),
    dict(id="L2", group="L", name="GUARD：PB分位漂移 2pp（容差内）→ 放行",
         why="窗口边界/含否当前值的口径差可产生 ≤3pp 自然漂移，不得误伤。",
         guard=lambda: _guard(_guard_base(pb_pct_shift=2.0)),
         check_guard=lambda r: r["ok"] and not r["blockers"],
         expect="ok=True、零 blocker"),
    dict(id="L3", group="L", name="GUARD：枚举非法 + 想象力与需求矛盾 → 2 MUST",
         why="LLM 幻觉输出（volume='暴涨'）与语义矛盾（demand=collapse 却给 imagination=S）"
             "是填报两大类错；引擎的枚举 raise 前由 guard 给出可读归因。",
         guard=lambda: _guard(_guard_base(volume="暴涨", imagination="S", demand="collapse")),
         check_guard=lambda r: (not r["ok"] and len(r["blockers"]) == 2
                                and any("枚举" in b for b in r["blockers"])
                                and any("语义矛盾" in b for b in r["blockers"])),
         expect="2 条 MUST：枚举非法 + 想象力与需求矛盾"),
    dict(id="L4", group="L", name="GUARD：侵蚀信号矛盾 + 景气不成对 → 2 MUST",
         why="demand=growth 却列 ≥2 侵蚀信号（§③侵蚀≥2须确认）与 prosperity_mom_pct "
             "缺 prosperity_trend（§三bis 两字段成对）都属结构性填报错误。",
         guard=lambda: _guard(_guard_base(erosion_signals=["价格战", "库存高企"], drop_prosperity_trend=True)),
         check_guard=lambda r: (not r["ok"] and len(r["blockers"]) == 2
                                and any("erosion_signals" in b for b in r["blockers"])
                                and any("prosperity" in b for b in r["blockers"])),
         expect="2 条 MUST：侵蚀矛盾 + 景气不成对"),
    dict(id="L5", group="L", name="GUARD：F2 证据缺 source + 方向背离 → 1 MUST + 1 SHOULD",
         why="forward=up 但证据 desc 全是下行语义 → 证据与声明背离（SHOULD 复核提示）；"
             "缺 source 在引擎 F2 拦截前由 guard 早失败并给出精确归因。",
         guard=lambda: _guard(_guard_base(forward="up",
                                          forward_evidence=[{"type": "orders", "desc": "出口订单同比下滑", "source": ""}])),
         check_guard=lambda r: (not r["ok"] and len(r["blockers"]) == 1
                                and len(r["warnings"]) >= 1
                                and any("desc 或 source" in b for b in r["blockers"])
                                and any("方向一致性" in w for w in r["warnings"])),
         expect="1 MUST（F2 早检）+ ≥1 SHOULD（方向背离提示）"),
    dict(id="L6", group="L", name="GUARD：合规填报 → 全绿零告警（正向控制）",
         why="确认 guard 不误伤规范填报：真实用例（case_battery/3etf）须零 blocker 零 warning。",
         guard=lambda: _guard(_guard_base()),
         check_guard=lambda r: r["ok"] and not r["blockers"] and not r["warnings"],
         expect="ok=True、零 blocker 零 warning"),
    dict(id="L7", group="L", name="GUARD(G4)：pb_pct 已锁定+偏差 10pp → SHOULD 不阻断",
         why="G4 锁定后该字段由 Python 预填，偏差=数据文件内部不一致而非 LLM 抄错，"
             "guard 降级为 SHOULD 数据质量告警、放行引擎（刷新由 data_refresh --rebuild 治理）。"
             "与 L1（未锁定→MUST）形成对照，证明锁感知生效。",
         guard=lambda: _guard({**_guard_base(pb_pct_shift=10.0), "_locked_mechanical": ["pb_pct"]}),
         check_guard=lambda r: (r["ok"] and not r["blockers"]
                                and len(r["warnings"]) >= 1
                                and any("数据文件内部不一致" in w for w in r["warnings"])),
         expect="ok=True、零 blocker、≥1 SHOULD（数据文件内部不一致提示）"),
    # ---- M 组：未填即错（2026-09-07 · 审议 RC1 根治）----
    dict(id="M1", group="M", name="未填即错：判断字段全缺 → MUST 拦截，不再静默吃默认值",
         why="demand 旧默认='growth'（最乐观档）、volume/price/forward 旧默认='flat'——"
             "LLM 漏填时引擎静默按默认值出完整裁决（09-07 胜率70→45假象事故根因现场）。"
             "新口径：缺失→[MUST] 判断字段缺失 blocker→PENDING_VERIFICATION、仓位0。",
         data={k: v for k, v in base().items()
               if k not in ("demand", "volume", "price", "forward", "imagination")},
         check=lambda v: (v["status"] == "PENDING_VERIFICATION"
                          and v["suggested_position"] == "0.0%"
                          and any("判断字段缺失" in b and "demand" in b for b in v["blockers"])),
         expect="status=PENDING_VERIFICATION、仓位0、[MUST] 判断字段缺失（含 demand）在案"),
    dict(id="M2", group="M", name="未填即错：仅漏 demand → 拦截且不按 growth 出裁决",
         why="单变量对照：只漏 demand 时旧版会按'结构性增长'（最乐观需求档）算完并可能 OK；"
             "新版必须拦截。量价字段保留 → 无其他缺失干扰，隔离验证 demand 一项。",
         data={k: v for k, v in base().items() if k != "demand"},
         check=lambda v: (v["status"] == "PENDING_VERIFICATION"
                          and any("判断字段缺失" in b for b in v["blockers"])
                          and all("demand" in b for b in v["blockers"] if "判断字段缺失" in b)),
         expect="status=PENDING_VERIFICATION、判断字段缺失 blocker 仅点名 demand"),
]


def run(verbose: bool = False) -> int:
    print("=" * 84)
    print("XYD 供需板块框架 · 红队测试 (Red Team)")
    print("=" * 84)
    groups = {"A": "框架否决不可绕过", "B": "PB口径错配", "C": "适用边界/新鲜度",
              "D": "极端情景/尾部纪律", "E": "白名单封闭性", "F": "量化通病",
              "G": "行业周期量价Gate", "H": "量升价跌·降档通道",
              "I": "估值主锚分类+双源/finalgate",
              "J": "双价格强制溯源闸门",
              "K": "F2 前瞻方向证据链",
              "L": "LLM轨机械复核层（G3 guard）",
              "M": "未填即错（判断字段缺失拦截）"}
    passed, failed = 0, []
    cur = None
    for c in CASES:
        if c["group"] != cur:
            cur = c["group"]
            print(f"\n--- {cur} 组：{groups[cur]} ---")
        ok, detail = _exec(c, verbose)
        print(f"  [{'PASS' if ok else 'FAIL'}] {c['id']} {c['name']}")
        if verbose or not ok:
            print(f"         期望: {c['expect']}")
            if detail:
                print(f"         实际: {detail}")
        if ok:
            passed += 1
        else:
            failed.append(c["id"])
    total = len(CASES)
    print("\n" + "=" * 84)
    print(f"红队结果: {passed}/{total} PASS" + (f"  FAIL={failed}" if failed else "  全部通过"))
    print("=" * 84)
    return 0 if not failed else 1


def _exec(c: Dict[str, Any], verbose: bool):
    exp_exc = c.get("raises")
    try:
        if "guard" in c:
            r = c["guard"]()
            if exp_exc:
                return False, f"未抛出 {exp_exc.__name__}，返回 {r}"
            return (bool(c["check_guard"](r)), f"返回 {r}")
        v = evaluate(copy.deepcopy(c["data"]))
        if exp_exc:
            return False, f"未抛出 {exp_exc.__name__}"
        ok = bool(c["check"](v))
        detail = (f"status={v['status']} 胜率={v['win_rate_label']} 象限={v['quadrant']} "
                  f"仓位={v['suggested_position']} blockers={v['blockers'][:2]}")
        if verbose:
            print(render_text(v))
        return ok, detail
    except Exception as e:
        if exp_exc and isinstance(e, exp_exc):
            return True, f"按预期抛出 {type(e).__name__}: {str(e)[:70]}"
        return False, f"异常 {type(e).__name__}: {str(e)[:140]}"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    sys.exit(run(a.verbose))
