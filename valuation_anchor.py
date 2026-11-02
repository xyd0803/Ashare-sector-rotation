#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XYD 供需板块框架 —— 行业估值主锚分类 (Valuation Anchor Classification)
======================================================================

把行业/板块/ETF 跟踪指数按「估值主锚」分为三类：

  PB   市净率  —— 大部分行业（资产重、周期性强、盈利波动大，PE 失真）
  PE   市盈率  —— 少部分成长行业（轻资产、盈利高增且为正，账面价值失真）
  BLEND PE+PB 动态混合 —— 资产与盈利均重要、或处于成长/周期切换的行业

设计原则（用户 2026-08-15 裁定）：
  ① 大部分行业以 PB 作主锚；
  ② 少部分成长行业以 PE 作主锚，或 PE+PB 动态混合（blend）；
  ③ 分类须「逻辑合理」，而非枚举穷尽——用【规则 + 精选表】两层映射：
     - 精选表命中 → 直接采用（带理由）；
     - 未命中 → 回退到规则（资产重/周期→PB；轻资产高增→PE；其余→BLEND）。

本模块是「分类的权威来源」，被 gate_final / redteam 校验引用，确保
分析师填的 valuation_anchor 与该行业真实主锚一致。
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Dict, List, Tuple

ANCHOR_PB = "PB"
ANCHOR_PE = "PE"
ANCHOR_BLEND = "BLEND"
ANCHORS = (ANCHOR_PB, ANCHOR_PE, ANCHOR_BLEND)


@dataclass
class AnchorResult:
    anchor: str
    reason: str
    matched: str = ""          # 命中关键词
    method: str = "table"      # table / rule-fallback


# =============================================================================
# 精选映射表（关键词 → (主锚, 理由)）。关键词小写，匹配时做子串包含。
# 命中优先级：先 PE 表、再 BLEND 表、再 PB 表（成长/混合优先于默认 PB，
# 避免把成长行业误判成 PB）。
# =============================================================================
_PE_TABLE: Dict[str, str] = {
    # 轻资产、盈利高增且为正、账面价值失真 → PE 主锚
    "软件": "轻资产高毛利，盈利高增且为正，账面价值失真→PE 主锚",
    "计算机": "轻资产高毛利，盈利高增且为正，账面价值失真→PE 主锚",
    "saas": "订阅制轻资产，盈利高增→PE 主锚",
    "云服务": "轻资产高毛利→PE 主锚",
    "半导体设计": "轻资产 IP 型，盈利高增→PE 主锚",
    "芯片设计": "轻资产 IP 型，盈利高增→PE 主锚",
    "ai芯片": "轻资产高研发、盈利高增→PE 主锚",
    "人工智能": "轻资产高成长→PE 主锚",
    "互联网": "平台型轻资产、盈利高增、账面价值失真→PE 主锚",
    "港股互联网": "平台型轻资产、盈利高增、账面价值失真→PE 主锚",
    "创新药": "研发驱动、盈利高增预期→PE 主锚（管线折现）",
    "生物科技": "研发驱动、盈利高增预期→PE 主锚",
    "biotech": "研发驱动、盈利高增预期→PE 主锚",
    "游戏": "轻资产内容，盈利随产品周期高增→PE 主锚",
    "传媒": "轻资产内容，盈利弹性大→PE 主锚（新业态）",
    "机器人": "轻资产高成长主题，盈利高增→PE 主锚",
    "算力": "轻资产高成长，盈利高增→PE 主锚",
    "恒生科技": "港股科技平台（互联网/硬件生态）轻资产高成长→PE 主锚（2026-09-07 补缺口）",
    "科技互联网": "平台型轻资产、盈利高增→PE 主锚",
}

_BLEND_TABLE: Dict[str, str] = {
    # 资产与盈利均重要、或成长/周期切换 → PE+PB 动态混合
    "白酒": "品牌资产+稳定盈利，PE 与 PB 均重要→动态混合",
    "食品饮料": "消费品牌，PE 与 PB 均重要→动态混合",
    "家电": "制造资产+品牌盈利，双锚→动态混合",
    "消费": "消费品牌，PE 与 PB 均重要→动态混合",
    "零售": "渠道资产+盈利，双锚→动态混合",
    "电池": "制造重资产+成长，双锚动态（周期/成长切换）→动态混合",
    "锂电": "制造重资产+成长，双锚动态→动态混合",
    "新能源": "制造重资产+成长，双锚动态→动态混合",
    "新能源车": "制造资产+成长，双锚动态→动态混合",
    "新能车": "制造资产+成长，双锚动态→动态混合（新能源车简写，2026-09-07 补缺口）",
    "医药": "制造/渠道资产+研发盈利，双锚→动态混合",
    "医疗": "医疗服务+器械制造混合，双锚→动态混合（2026-09-07 补缺口，防规则回退）",
    "中药": "制造资产+品牌盈利，双锚→动态混合（2026-09-07 补缺口）",
    "电网设备": "重资产制造+成长盈利（一次/二次设备混合），双锚→动态混合（2026-09-07 补缺口）",
    "cxo": "制造服务资产+成长盈利，双锚→动态混合",
    "医疗器械": "制造资产+盈利，双锚→动态混合",
    "消费电子": "制造资产+产品周期盈利，双锚→动态混合",
    "电子": "制造资产+盈利，双锚→动态混合",
    "半导体制造": "重资产+周期盈利，双锚→动态混合",
    "半导体": "重资产+周期盈利，双锚→动态混合",
    "封测": "重资产+周期盈利，双锚→动态混合",
    "通信设备": "制造资产+盈利，双锚→动态混合",
    "通信": "制造资产+盈利，双锚→动态混合",
    "军工": "重资产+政策盈利，双锚→动态混合",
    "国防": "重资产+政策盈利，双锚→动态混合",
    "特种化工": "制造资产+盈利，双锚→动态混合",
    "新材料": "制造资产+成长盈利，双锚→动态混合",
}

_PB_TABLE: Dict[str, str] = {
    # 资产重、周期性强、盈利波动大（PE 失真）→ PB 主锚
    "银行": "高杠杆资产驱动，PE 失真→PB 主锚（唯一基准）",
    "保险": "资产端驱动，PB/内含价值→PB 主锚",
    "证券": "净资产驱动、盈利周期波动大→PB 主锚",
    "券商": "净资产驱动、盈利周期波动大→PB 主锚",
    "非银": "非银行金融（证券+保险+多元金融），资产驱动→PB 主锚",
    "房地产": "重资产、盈利波动大→PB 主锚",
    "钢铁": "重资产周期，盈利波动大→PB 主锚",
    "有色": "重资产周期，盈利波动大→PB 主锚",
    "金属": "重资产周期，盈利波动大→PB 主锚",
    "煤炭": "重资产周期，盈利波动大→PB 主锚",
    "石油": "重资产周期，盈利波动大→PB 主锚",
    "石化": "重资产周期，盈利波动大→PB 主锚",
    "电力": "重资产公用事业，盈利稳定→PB 主锚",
    "公用事业": "重资产，盈利稳定→PB 主锚",
    "燃气": "重资产公用事业→PB 主锚",
    "水务": "重资产公用事业→PB 主锚",
    "建筑": "重资产周期→PB 主锚",
    "基建": "重资产周期→PB 主锚",
    "建材": "重资产周期→PB 主锚",
    "水泥": "重资产周期→PB 主锚",
    "航运": "重资产周期，盈利波动大→PB 主锚",
    "港口": "重资产周期→PB 主锚",
    "海运": "重资产周期，盈利波动大→PB 主锚",
    "交运": "重资产→PB 主锚",
    "化工": "重资产周期，盈利波动大→PB 主锚",
    "工程机械": "重资产周期→PB 主锚",
    "机械": "重资产周期→PB 主锚",
    "造纸": "重资产周期→PB 主锚",
    "农业": "重资产周期→PB 主锚",
    "养殖": "重资产周期，盈利波动大→PB 主锚",
    "牧业": "重资产周期→PB 主锚",
    "光伏": "重资产强周期，下行期盈利常为负/PE失真，PB为更干净的主锚（PE仅辅助）→PB 主锚",
    "航空": "重资产周期，盈利波动大→PB 主锚",
    "机场": "重资产→PB 主锚",
    "电信": "重资产公用事业→PB 主锚",
    "传统汽车": "重资产制造→PB 主锚",
    "重卡": "重资产制造→PB 主锚",
    "高股息": "收息型（银行/煤炭/电信/公用事业等重资产），盈利稳波动小→PB 主锚（2026-09-07 补缺口）",
    "红利": "收息型重资产，股息来自净资产回报→PB 主锚（2026-09-07 补缺口）",
    "股息低波": "收息型重资产低波→PB 主锚（2026-09-07 补缺口）",
}


def _norm(text: str) -> str:
    return (text or "").lower().replace(" ", "").replace("-", "").replace("_", "")


def classify(name: str, index_code: str = "", index_name: str = "") -> AnchorResult:
    """返回该行业/板块的估值主锚分类。"""
    text = _norm(f"{name} {index_code} {index_name}")
    # 命中优先级：PE → BLEND → PB
    for kw, reason in _PE_TABLE.items():
        if _norm(kw) in text:
            return AnchorResult(ANCHOR_PE, reason, kw, "table")
    for kw, reason in _BLEND_TABLE.items():
        if _norm(kw) in text:
            return AnchorResult(ANCHOR_BLEND, reason, kw, "table")
    for kw, reason in _PB_TABLE.items():
        if _norm(kw) in text:
            return AnchorResult(ANCHOR_PB, reason, kw, "table")
    # 规则回退
    return AnchorResult(
        ANCHOR_BLEND,
        "未命中精选表→规则回退：资产/盈利重要性不明，默认 PE+PB 动态混合（更稳健）",
        "", "rule-fallback")


# 文档用：导出三张表（过滤掉下划线开头的内部键）
def tables() -> Dict[str, Dict[str, str]]:
    return {"PE": _PE_TABLE, "BLEND": _BLEND_TABLE, "PB": _PB_TABLE}


# =============================================================================
# BLEND 自适应权重（O6，2026-08-16 用户裁定）：替代全局 0.6/0.4。
# 设计意图（关 V6「BLEND 全局 0.6/0.4 非行业自适应」）：
#   - 重资产 + 成长（电池/锂/新能源/半导体制造/封测/军工…）：PE 受 capex 周期与
#     margin 大幅摆动失真，PB 提供更干净的安全边际 → PB 权重偏高（0.55~0.65）。
#   - 轻资产消费/服务（白酒/食品/家电/CXO/游戏/传媒…）：账面价值失真、盈利质量
#     由 PE 捕捉 → PE 权重偏高（0.55~0.65）。
#   - 制造中性（电子/通信/医药/医疗器械/新材料…）：双锚均衡（0.50/0.50）。
# 未命中 → 回退全局默认（由 discipline 从 params.valuation_blend 取 0.60/0.40）。
# 关键词顺序：具体子类(半导体制造/新能源车)须排在父类(半导体/新能源)之前，避免误匹配。
# =============================================================================
BLEND_WEIGHTS: Dict[str, Tuple[float, float]] = {
    # —— 重资产成长：PB 偏高 ——
    "电池": (0.40, 0.60), "锂电": (0.40, 0.60), "新能源": (0.40, 0.60),
    "新能源车": (0.40, 0.60), "光伏": (0.35, 0.65),
    "半导体制造": (0.40, 0.60), "半导体": (0.45, 0.55), "封测": (0.40, 0.60),
    "军工": (0.45, 0.55), "国防": (0.45, 0.55),
    "特种化工": (0.45, 0.55),
    # —— 制造中性：双锚均衡 ——
    "消费电子": (0.50, 0.50), "电子": (0.50, 0.50),
    "通信设备": (0.50, 0.50), "通信": (0.50, 0.50),
    "医药": (0.50, 0.50), "医疗器械": (0.50, 0.50), "新材料": (0.50, 0.50),
    # —— 轻资产消费/服务：PE 偏高 ——
    "白酒": (0.60, 0.40), "食品饮料": (0.60, 0.40), "家电": (0.55, 0.45),
    "消费": (0.60, 0.40), "零售": (0.60, 0.40),
    "cxo": (0.60, 0.40), "游戏": (0.65, 0.35), "传媒": (0.65, 0.35),
}


def blend_weights_for(name: str, index_code: str = "", index_name: str = "",
                      default_pe: float = 0.6, default_pb: float = 0.4
                      ) -> Tuple[float, float, str]:
    """返回 (pe_weight, pb_weight, matched_keyword)。

    命中 BLEND_WEIGHTS → 用专用权重（关 V6）；否则回退全局默认 (default_pe, default_pb)。
    keyword 命中顺序即 BLEND_WEIGHTS 插入序（具体子类优先于父类）。
    """
    text = _norm(f"{name} {index_code} {index_name}")
    for kw, (pe, pb) in BLEND_WEIGHTS.items():
        if _norm(kw) in text:
            return pe, pb, kw
    return default_pe, default_pb, ""


# =============================================================================
# 自检
# =============================================================================
_SELFTEST: List[Tuple[str, str]] = [
    # (输入名, 期望主锚)
    ("银行", ANCHOR_PB),
    ("保险", ANCHOR_PB),
    ("房地产", ANCHOR_PB),
    ("钢铁", ANCHOR_PB),
    ("煤炭", ANCHOR_PB),
    ("有色金属", ANCHOR_PB),
    ("证券", ANCHOR_PB),
    ("沪深300非银", ANCHOR_PB),       # 512070 沪深300非银（证券+保险 composite）
    ("非银金融", ANCHOR_PB),          # 非银 命中校验
    ("中证非银行金融", ANCHOR_PB),    # 非银行 命中校验
    ("航运", ANCHOR_PB),
    ("基础化工", ANCHOR_PB),
    ("软件", ANCHOR_PE),
    ("计算机", ANCHOR_PE),
    ("互联网", ANCHOR_PE),
    ("港股互联网ETF", ANCHOR_PE),
    ("创新药", ANCHOR_PE),
    ("半导体设计", ANCHOR_PE),
    ("白酒", ANCHOR_BLEND),
    ("食品饮料", ANCHOR_BLEND),
    ("家用电器", ANCHOR_BLEND),
    ("光伏", ANCHOR_PB),
    ("电池", ANCHOR_BLEND),
    ("新能源汽车", ANCHOR_BLEND),
    ("医药", ANCHOR_BLEND),
    ("消费电子", ANCHOR_BLEND),
    ("半导体", ANCHOR_BLEND),
    ("军工", ANCHOR_BLEND),
    # —— 2026-09-07 补缺口（ETF 批量分析锚定 bug 根治）——
    ("锂电池", ANCHOR_BLEND),          # 159755 国证新能源车电池
    ("新能车", ANCHOR_BLEND),          # 515030
    ("医疗", ANCHOR_BLEND),            # 512170
    ("中药", ANCHOR_BLEND),            # 560080
    ("电网设备", ANCHOR_BLEND),        # 159326
    ("卫星通信", ANCHOR_BLEND),        # 159206（通信命中）
    ("恒生科技", ANCHOR_PE),           # 513180 HSTECH
    ("港股通创新药", ANCHOR_PE),       # 159570
    ("创新药ETF", ANCHOR_PE),          # 159992
    ("香港证券", ANCHOR_PB),           # 513090（证券命中）
    ("港股通高股息", ANCHOR_PB),       # 513530
    ("港股高股息低波", ANCHOR_PB),     # 513950
    ("红利ETF", ANCHOR_PB),
]


def _selfcheck() -> int:
    print("== valuation_anchor 自检 ==")
    fails = 0
    for name, expect in _SELFTEST:
        got = classify(name).anchor
        ok = (got == expect)
        if not ok:
            fails += 1
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:<10} → {got:<6} (期望 {expect})"
              + ("" if ok else f"  ✗ 理由: {classify(name).reason}"))
    print(f"\n分类自检：{len(_SELFTEST)-fails}/{len(_SELFTEST)} 通过"
          + ("" if fails == 0 else f"，{fails} 项失败"))
    # 统计占比（粗略）
    counts = {a: 0 for a in ANCHORS}
    sample = ["银行","保险","房地产","钢铁","煤炭","有色","石油","电力","建筑","建材","水泥",
              "航运","港口","化工","机械","造纸","农业","养殖","航空","电信","券商","光伏",
              "软件","计算机","互联网","创新药","游戏","半导体设计","ai芯片","算力",
              "白酒","食品饮料","家电","电池","新能源车","医药","cxo","消费电子",
              "半导体制造","通信设备","军工","新材料"]
    for s in sample:
        counts[classify(s).anchor] += 1
    total = len(sample)
    print(f"示例 {total} 行业分布：PB={counts[ANCHOR_PB]}({counts[ANCHOR_PB]*100//total}%) "
          f"PE={counts[ANCHOR_PE]}({counts[ANCHOR_PE]*100//total}%) "
          f"BLEND={counts[ANCHOR_BLEND]}({counts[ANCHOR_BLEND]*100//total}%)")
    largest = max(counts, key=counts.get)
    ok_prop = (largest == ANCHOR_PB) and (counts[ANCHOR_PB] * 100 // total >= 40)
    print("（符合『大部分 PB、少部分 PE/blend』的格局）" if ok_prop else "（⚠️ 比例异常，请复核分类表）")
    # O6 BLEND 自适应权重自检
    print("\n== O6 BLEND 自适应权重自检 ==")
    bw_cases = [("电池", (0.40, 0.60)), ("半导体制造", (0.40, 0.60)),
                ("白酒", (0.60, 0.40)), ("家电", (0.55, 0.45)),
                ("医药", (0.50, 0.50)), ("未知行业X", (0.6, 0.4))]
    for nm, expect in bw_cases:
        pe, pb, kw = blend_weights_for(nm)
        ok_bw = (round(pe, 2), round(pb, 2)) == expect
        if not ok_bw:
            fails += 1
        print(f"  [{'PASS' if ok_bw else 'FAIL'}] {nm:<10} → PE{pe}/PB{pb}"
              + (f" (命中『{kw}』)" if kw else " (回退默认)"))
    return 1 if fails else 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass
    import os
    if len(sys.argv) > 1 and sys.argv[1] in ("--selfcheck", "selfcheck"):
        rc = _selfcheck()
        sys.exit(rc)
    # 默认：交互式查单个
    if len(sys.argv) > 1:
        r = classify(" ".join(sys.argv[1:]))
        print(f"分类：{r.anchor}  | 命中：{r.matched or '(规则回退)'} | {r.reason}")
    else:
        sys.exit(_selfcheck())
