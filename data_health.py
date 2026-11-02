#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
data_health.py —— 数据健康度四态（D-8 · 2026-10-25 第三批）

背景（三维审计 · 数据层 D-8）：
  本技能过去只有「新鲜度三档」（fresh ≤7天 / warn ≤15天 / stale >15天，见 data_refresh.py）
  与零散的 warnings 列表；缺一个**统一口径的健康度结论**：字段是「没有」还是「有但不可用」
  还是「有但只单源」在草稿/裁决里混在 warnings 里，无法机械判断结论能不能用。
  对照 XYD-earnings-analysis 的 `_data_health` 四态，本模块把同一口径移植到供需技能。

四态（严重度升序）：
  OK        所有参与决策的字段齐备、可交叉、不过期
  DEGRADED  字段齐备但有**降级**（单源未交叉 / 偏旧但未超限 / 可选证据未取证）
            → 放行，但结论必须带水印，且每项降级须**量化影响**
  MISSING   决策必需字段**缺失**（无序列 / 无分位 / 无现价）→ 结论不得用于决策
  INVALID   数据**有但不可用**（口径突变 / 过期超硬限 / 未来日期）→ 硬拦

纪律：
  · 严重度取最高项（INVALID > MISSING > DEGRADED > OK），不因多数项 OK 而"平均"掉硬问题；
  · **降级必量化**：每条 DEGRADED 项必须带 impact 且含可读的量化后果（见 _add 断言）；
  · 本模块**只做汇总与标注**，不参与胜率/EV/闸门计算（核心逻辑零改动红线）。

用法：
  python data_health.py --selfcheck      # 场景自检（OK/DEGRADED/MISSING/INVALID 各一）
  # 编排层：run_sector_pipeline._run_single 末尾调用 assess() → 写草稿 _data_health
"""
import argparse
import sys

STATES = ("OK", "DEGRADED", "MISSING", "INVALID")
_RANK = {s: i for i, s in enumerate(STATES)}

# 与 discipline.G["freshness_max_days"] / data_refresh.FRESH_MAX 保持同一口径
FRESH_WARN_DAYS = 7
FRESH_MAX_DAYS = 15

LLM_WATERMARK = {
    "OK": None,
    "DEGRADED": "⚠️ 数据健康度 DEGRADED",
    "MISSING": "⛔ 数据健康度 MISSING（结论不得用于决策）",
    "INVALID": "❌ 数据健康度 INVALID（数据不可用，须重取/核查）",
}


def _months_ago(month_str, as_of=None) -> int | None:
    """'YYYY-MM' 距今月数（基准=as_of 或今日）。解析失败返回 None。"""
    import datetime
    try:
        y, m = int(str(month_str)[:4]), int(str(month_str)[5:7])
    except (TypeError, ValueError):
        return None
    try:
        base = datetime.date.fromisoformat(as_of) if as_of else datetime.date.today()
    except (TypeError, ValueError):
        base = datetime.date.today()
    return (base.year - y) * 12 + (base.month - m)


def assess(*, index_code=None, series_points=None, freshness_days=None,
           pb_pct=None, pb_sources=None, price_cross=None,
           has_quant_raw=False, pb_pct_10y=None, bvps_caliber_mutation=False,
           caliber_events=None, industry_metrics_present=False,
           event_leg_state=None, as_of=None) -> dict:
    """评估数据健康度，返回 {status, items, counts, watermark, ...}。

    所有入参都可缺省（缺省=未知，按 MISSING 处理是 fail-closed 的默认面）。
    """
    items = []

    def _add(level, code, msg, impact=None, quantified=False):
        assert level in STATES, level
        if level in ("DEGRADED", "MISSING", "INVALID"):
            assert impact, f"{code} 须带 impact（降级必量化）"
        items.append({"level": level, "code": code, "msg": msg,
                      "impact": impact, "quantified": bool(quantified)})

    # ---------------- INVALID（有但不可用） ----------------
    # BVPS 口径突变须按「是否落在 5 年主锚窗口内」分级，否则会误杀：
    # D-4 在 10 年月频序列上扫描，历史（如 2014-12）的会计口径变更不影响当前 5y 主锚分位。
    if bvps_caliber_mutation:
        evs = [e for e in (caliber_events or []) if isinstance(e, dict)]
        recent = []
        for e in evs:
            age = _months_ago(e.get("month"), as_of)
            if age is not None and 0 <= age < 60:
                recent.append((age, e))
        if recent or not evs:
            # 窗口内突变，或只有 bool 无明细（无法排除 → fail-closed 按 INVALID）
            _detail = "；".join(f"{e.get('month')} PB跳{e.get('pb_jump_pct')}%" for _, e in recent[:3])
            _add("INVALID", "IV-BVPS-CALIBER",
                 "PB 序列检测到 BVPS 口径突变（同月 PB 跳变而价格几乎不动）"
                 + (f"，且落在 5 年主锚窗口内：{_detail}" if recent else "（无事件明细，按最坏假设）"),
                 "该窗口内的 PB 分位可能整体失真 ≥15%（口径变更非价格变动），主锚不可直接用于决策",
                 quantified=True)
        else:
            _ages = [a for a in (_months_ago(e.get("month"), as_of) for e in evs) if a is not None]
            _oldest = min(_ages) if _ages else None
            _add("DEGRADED", "DG-BVPS-CALIBER-OLD",
                 f"BVPS 口径突变 {len(evs)} 处，但全部早于 5 年主锚窗口"
                 f"（最近一处约 {_oldest} 个月前）",
                 "5y 主锚分位不受影响；受影响的仅是 10y 交叉口径 → O9 若切 10y 须按口径突变提示打折",
                 quantified=True)
    if freshness_days is not None and freshness_days > FRESH_MAX_DAYS:
        _add("INVALID", "IV-STALE-HARD",
             f"序列已 {freshness_days} 天未更新（硬限 {FRESH_MAX_DAYS} 天）",
             f"锚点滞后 {freshness_days} 天，分位/五档带系统性漂移，须重取后再判",
             quantified=True)

    # ---------------- MISSING（决策必需字段缺失） ----------------
    if not series_points:
        _add("MISSING", "MS-SERIES", "无 PB 日频序列（分位无法计算）",
             "PB 分位与五档带不可得 → 赔率/EV 无锚，结论不成立")
    elif pb_pct is None:
        _add("MISSING", "MS-PB-PCT", "有序列但分位未产出",
             "分位缺失 → 赔率定位失败，EV 不可算")
    if price_cross is None or not price_cross.get("ok"):
        _add("MISSING", "MS-PRICE", "现价双源皆失败/未提供",
             "price_now 缺省 → EV 只能走「相对现价=100」口径，绝对目标价不可得")

    # ---------------- DEGRADED（齐备但降级·逐项量化） ----------------
    if price_cross and price_cross.get("ok") and not price_cross.get("multi_source"):
        _add("DEGRADED", "DG-PRICE-SINGLE",
             f"现价仅单源（{price_cross.get('primary')}），无第二源交叉",
             "现价若偏 0.5% → EV base 与三档价同步偏约 0.5pp；无交叉即无法证伪",
             quantified=True)
    if price_cross and price_cross.get("ok") and price_cross.get("diff_pct") is not None \
            and price_cross.get("warn"):
        _add("DEGRADED", "DG-PRICE-DIVERGE",
             f"现价双源分歧 {price_cross.get('diff_pct')}% > 0.5%",
             f"两源差 {price_cross.get('diff_pct')}%，EV base 取值不确定度同量级，须人工复核快照时点",
             quantified=True)
    _n_src = len(pb_sources or {}) if isinstance(pb_sources, dict) else 0
    if _n_src < 2:
        _add("DEGRADED", "DG-PB-SINGLE",
             f"PB 当前值仅 {_n_src} 个来源（<2），未过双源交叉",
             "单源 PB 无独立复算校验；若源口径错配（如行业均值冒充）误差可达 20pp 级",
             quantified=True)
    if freshness_days is not None and FRESH_WARN_DAYS < freshness_days <= FRESH_MAX_DAYS:
        _add("DEGRADED", "DG-STALE-WARN",
             f"序列已 {freshness_days} 天未更新（>「{FRESH_WARN_DAYS} 天，未超硬限 {FRESH_MAX_DAYS}）",
             f"锚点滞后 {freshness_days} 天 → 分位带可能漂移数 pp，结论须标偏旧",
             quantified=True)
    if not has_quant_raw:
        _add("DEGRADED", "DG-QUANT-MISSING",
             "quant_raw（五维量化输入）未装配",
             "胜率回退六档表（非量化路径）→ 胜率数值与量化路径不可比，须带 table 水印",
             quantified=True)
    if pb_pct_10y is None:
        _add("DEGRADED", "DG-NO-10Y",
             "无 10 年月频序列（D-4 未覆盖）",
             "仅 5y 分位 → 长周期极端估值（如 2014/2021 级别）不可见，O9 口径切换失效",
             quantified=True)
    if not industry_metrics_present:
        _add("DEGRADED", "DG-IM-UNRETRIEVED",
             "行业量价 4 指标（产销量/库存/产能利用率/营收增速）未机械取证",
             "量价判断全凭 LLM 叙述，无客观锚 → 量价打分可信度不可量化，须人工复核",
             quantified=True)
    if event_leg_state is None:
        _add("DEGRADED", "DG-EVENT-UNRETRIEVED",
             "板块事件腿未取证（无 side-car）",
             "催化剂盲区：订单/中标/扩产类事件未检索 → 想象力维度可能低估或误判",
             quantified=True)
    elif event_leg_state == "corrupt":
        _add("DEGRADED", "DG-EVENT-CORRUPT",
             "事件腿 side-car 损坏（解析失败）",
             "事件卡不可读 → 催化剂维度按「未取证」处理，须重取")

    # ---------------- 汇总 ----------------
    status = "OK"
    for it in items:
        if _RANK[it["level"]] > _RANK[status]:
            status = it["level"]
    counts = {s: sum(1 for it in items if it["level"] == s) for s in STATES}
    brief = "；".join(f"{it['code']}" for it in items
                      if it["level"] not in ("OK",) and it["level"] == status)
    watermark = None
    if status != "OK":
        watermark = f"{LLM_WATERMARK[status]}：{counts[status]} 项（{brief}）→ 影响见 _data_health.items"
    return {
        "status": status,
        "index_code": index_code,
        "as_of": as_of,
        "freshness_days": freshness_days,
        "counts": counts,
        "items": items,
        "watermark": watermark,
        "schema": "D8-four-state/1",
    }


def render(h: dict) -> str:
    """一行式摘要（供 CLI 与编排层打印）。"""
    if not h:
        return "数据健康度: 未评估"
    c = h.get("counts") or {}
    parts = [f"{s}={c.get(s, 0)}" for s in STATES if c.get(s)]
    return (f"数据健康度: {h.get('status')}（{', '.join(parts) or '无项'}）"
            + (f" · {h['watermark']}" if h.get("watermark") else ""))


# ------------------------------ selfcheck ------------------------------
def _selfcheck() -> int:
    """四态场景自检 + 降级必量化断言。"""
    fails = []

    def chk(cond, msg):
        print(f"  [{'PASS' if cond else 'FAIL'}] {msg}")
        if not cond:
            fails.append(msg)

    # ① OK：全绿
    ok = assess(index_code="X", series_points=1200, freshness_days=1, pb_pct=42.0,
                pb_sources={"a": 1.2, "b": 1.2},
                price_cross={"ok": True, "multi_source": True, "primary": "sina",
                             "diff_pct": 0.01, "warn": None},
                has_quant_raw=True, pb_pct_10y=39.0, bvps_caliber_mutation=False,
                industry_metrics_present=True, event_leg_state="retrieved")
    chk(ok["status"] == "OK" and ok["watermark"] is None, "OK 场景：status=OK 且无水印")

    # ② DEGRADED：单源现价 + 无 10y + 事件腿未取
    dg = assess(index_code="X", series_points=1200, freshness_days=3, pb_pct=42.0,
                pb_sources={"a": 1.2, "b": 1.2},
                price_cross={"ok": True, "multi_source": False, "primary": "sina",
                             "diff_pct": None, "warn": "单源"},
                has_quant_raw=True, pb_pct_10y=None, bvps_caliber_mutation=False,
                industry_metrics_present=True, event_leg_state=None)
    chk(dg["status"] == "DEGRADED" and dg["watermark"], "DEGRADED 场景：带水印")
    chk(all(it["impact"] for it in dg["items"] if it["level"] == "DEGRADED"),
        "降级必量化：每条 DEGRADED 均有 impact")
    chk(all(it["quantified"] for it in dg["items"]
            if it["level"] == "DEGRADED" and it["code"] != "DG-EVENT-CORRUPT"),
        "降级必量化：DEGRADED 项标记 quantified=True")

    # ③ MISSING：无序列
    ms = assess(index_code="X", series_points=0, freshness_days=1, pb_pct=None,
                pb_sources={}, price_cross=None, has_quant_raw=False)
    chk(ms["status"] == "MISSING", "MISSING 场景：无序列 → MISSING")
    chk(any(it["code"] == "MS-SERIES" for it in ms["items"]), "MISSING 场景：命中 MS-SERIES")

    # ④ INVALID：口径突变优先于一切
    iv = assess(index_code="X", series_points=1200, freshness_days=1, pb_pct=42.0,
                pb_sources={"a": 1.2, "b": 1.2},
                price_cross={"ok": True, "multi_source": True, "primary": "sina",
                             "diff_pct": 0.01, "warn": None},
                has_quant_raw=True, pb_pct_10y=39.0, bvps_caliber_mutation=True,
                industry_metrics_present=True, event_leg_state="retrieved")
    chk(iv["status"] == "INVALID", "INVALID 场景：口径突变 → INVALID（压倒 DEGRADED）")

    # ⑤ 严重度单调：INVALID > MISSING > DEGRADED
    chk(_RANK["INVALID"] > _RANK["MISSING"] > _RANK["DEGRADED"] > _RANK["OK"],
        "严重度排序 INVALID>MISSING>DEGRADED>OK")

    # ⑥ 过期超硬限 → INVALID
    st = assess(index_code="X", series_points=1200, freshness_days=40, pb_pct=42.0,
                pb_sources={"a": 1, "b": 1},
                price_cross={"ok": True, "multi_source": True, "primary": "sina",
                             "diff_pct": 0.0, "warn": None},
                has_quant_raw=True, pb_pct_10y=1, industry_metrics_present=True,
                event_leg_state="retrieved")
    chk(st["status"] == "INVALID", "过期 >15 天 → INVALID（对齐 discipline MUST）")

    # ⑦ 历史口径突变（窗口外）→ DEGRADED 而非 INVALID（防误杀·512800 实证）
    old = assess(index_code="X", as_of="2026-10-25", series_points=1213, freshness_days=2,
                 pb_pct=92.25, pb_sources={"a": 1.3, "b": 1.3},
                 price_cross={"ok": True, "multi_source": True, "primary": "sina",
                              "diff_pct": 0.02, "warn": None},
                 has_quant_raw=True, pb_pct_10y=39.3, bvps_caliber_mutation=True,
                 caliber_events=[{"month": "2014-12", "pb_jump_pct": 30.2,
                                  "price_jump_pct": 0.0}],
                 industry_metrics_present=True, event_leg_state="retrieved")
    chk(old["status"] == "DEGRADED"
        and any(it["code"] == "DG-BVPS-CALIBER-OLD" for it in old["items"]),
        "历史口径突变（>60 个月前）→ DEGRADED 并标 DG-BVPS-CALIBER-OLD（不误杀 5y 主锚）")

    # ⑧ 窗口内口径突变（近 5y）→ INVALID
    near = assess(index_code="X", as_of="2026-10-25", series_points=1213, freshness_days=2,
                  pb_pct=92.25, pb_sources={"a": 1.3, "b": 1.3},
                  price_cross={"ok": True, "multi_source": True, "primary": "sina",
                               "diff_pct": 0.02, "warn": None},
                  has_quant_raw=True, pb_pct_10y=39.3, bvps_caliber_mutation=True,
                  caliber_events=[{"month": "2025-06", "pb_jump_pct": 22.0,
                                   "price_jump_pct": 1.0}],
                  industry_metrics_present=True, event_leg_state="retrieved")
    chk(near["status"] == "INVALID", "窗口内口径突变（<60 个月）→ INVALID（主锚失效）")

    print("-" * 60)
    if fails:
        print(f"  ❌ data_health selfcheck 失败 {len(fails)} 项")
        return 1
    print("  ✅ data_health selfcheck 全通过")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="数据健康度四态（D-8）")
    ap.add_argument("--selfcheck", action="store_true", help="场景自检")
    a = ap.parse_args()
    if a.selfcheck:
        return _selfcheck()
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
