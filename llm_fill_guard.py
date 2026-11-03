#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
llm_fill_guard.py — LLM 轨输出机械复核层（G3 · 2026-09-04）
============================================================
参照 个股盈利面框架 的 handfill_guard 思路：M4 草稿标 _llm_required 的
9 组判断字段由 LLM 补齐后，**必须先过本守卫**才准喂引擎（discipline.py 闸门
仍是最后兜底，本层是上游早失败，省一次引擎运行并给出更可读的归因）。

四道机械检查（全部确定性规则，零 LLM 裁量）：
  ① 枚举/范围校验      volume/price/forward ∈ {up,flat,down}；
                        demand ∈ {growth,cyclical,erosion,collapse}；
                        imagination ∈ {S,A,B,C,D}；prosperity_trend ∈ {up,flat,down}
  ② bands 反算对账(最强) pb_pct 声明值 vs 由 pb_series+pb_now 机械反算分位，
                        偏差 >3pp → MUST（PE 同理）——LLM 抄错数/编数必现形
  ③ 语义矛盾检测        imagination≥A 但 demand∈{erosion,collapse} → MUST；
                        demand=growth 但 erosion_signals≥2 → MUST；
                        prosperity_mom_pct 有值但 prosperity_trend 缺 → MUST；
                        bottom_signal_detail 与 bottom_signals 计数矛盾 → MUST（F4 早检）；
                        forward_evidence 缺 desc/source → MUST（F2 早检）；
                        terminal_price_down=True 但 demand≠collapse → SHOULD；
                        catalysts 结构化缺关键字段 → SHOULD（F3 格式）
  ④ F2 证据方向一致性    forward=up 但全部证据 desc 无上行语义词 → SHOULD（反之 down 同）

用法：
  python llm_fill_guard.py --input case.json     # 复核已填草稿，exit 0/6
  python llm_fill_guard.py --selfcheck           # 内置用例自校验
  run_sector_pipeline.py --guard case.json       # 等效入口（S4.5 挂点）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

PCT_RECHECK_TOL_PP = 3.0     # bands 反算对账容差（百分点）
GUARD_BLOCK_EXIT = 6

_ENUMS = {
    "volume": {"up", "flat", "down"},
    "price": {"up", "flat", "down"},
    "forward": {"up", "flat", "down"},
    "demand": {"growth", "cyclical", "erosion", "collapse"},
    "imagination": {"S", "A", "B", "C", "D"},
    "prosperity_trend": {"up", "flat", "down"},
}

_UP_WORDS = ("上行", "上升", "回升", "增长", "上涨", "扩张", "超预期", "上修", "改善",
             "回暖", "提升", "涨价", "满产", "加速", "放量", "增加", "同比增", "环比增",
             "排产", "订单饱满", "供不应求", "预增", "抬升", "常态化")
_DOWN_WORDS = ("下行", "下滑", "回落", "下跌", "下修", "收缩", "恶化", "降价", "减产",
               "走弱", "疲软", "萎缩", "过剩", "累库", "去库不及")
import re as _re
_UP_NUM = _re.compile(r"同比?[::]?\s*\+\d|同比?[增升]|预[增升]|[+\uFF0B]\d+(\.\d+)?%")
_DOWN_NUM = _re.compile(r"同比?[::]?\s*-\d|同比?[下滑降]|[-\uFF0D]\d+(\.\d+)?%")


def rank_pct(vals: List[float], x: float) -> Optional[float]:
    """当前值 v 在序列中的历史分位 = 序列中 ≤v 的比例 ×100。
    与 parse_mx_pb_daily.pct_rank / discipline.rank_pct 逐字一致（含当前值自身）。"""
    if not vals:
        return None
    return round(100.0 * sum(1 for v in vals if v <= x) / len(vals), 2)


def _as_float(x: Any) -> Optional[float]:
    try:
        if x is None or x == "":
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def _series_vals(series: Any) -> List[float]:
    """兼容 [v,...] 与 [[date, v],...] 两种序列形态。"""
    out = []
    if not isinstance(series, (list, tuple)):
        return out
    for it in series:
        if isinstance(it, (list, tuple)) and len(it) >= 2:
            f = _as_float(it[-1])
        else:
            f = _as_float(it)
        if f is not None:
            out.append(f)
    return out


def run(case: Dict[str, Any]) -> Dict[str, Any]:
    """机械复核一个（LLM 补齐后的）case。返回 {ok, blockers, warnings, checks}。
    只读不改：永不修改 case 本身。"""
    blockers: List[str] = []
    warnings: List[str] = []
    checks: Dict[str, Any] = {}

    def must(msg: str) -> None:
        blockers.append(f"[GUARD·MUST] {msg}")

    def should(msg: str) -> None:
        warnings.append(f"[GUARD·SHOULD] {msg}")

    # ---------- ① 枚举/范围校验 ----------
    enum_bad = []
    for f, allowed in _ENUMS.items():
        v = case.get(f)
        if v is None or v == "":
            continue  # 缺失由引擎 MUST 门负责，本层只管"填了但填错"
        if str(v) not in allowed:
            enum_bad.append(f"{f}={v!r}(合法:{'/'.join(sorted(allowed))})")
    checks["enum_ok"] = not enum_bad
    if enum_bad:
        must("枚举字段非法: " + "; ".join(enum_bad))
    crowding = case.get("crowding")
    # 引擎契约（discipline.py:215）：crowding: bool。兼容历史字符串描述，仅拒 dict/list/数值形态。
    if crowding is not None and crowding != "" and not isinstance(crowding, (bool, str)):
        must(f"crowding 须为 bool（引擎契约）或字符串描述，收到 {type(crowding).__name__}")

    # ---------- ② bands 反算对账（最强） ----------
    # G4 锁感知：字段若在 _locked_mechanical（Python 机械预填）中，偏差=数据文件内部不一致
    # （非 LLM 抄错）→ 降级为 SHOULD 数据质量告警（刷新由 data_refresh --rebuild 治理），不阻断引擎；
    # 未在锁定表中的字段（legacy/手工 case / 真 LLM 编著数值）→ 仍 MUST 硬拦截。
    locked = set(case.get("_locked_mechanical") or [])
    for anchor, now_key, pct_key, series_key in (("PB", "pb_now", "pb_pct", "pb_series"),
                                                 ("PE", "pe_now", "pe_pct", "pe_series")):
        now_v = _as_float(case.get(now_key))
        pct_v = _as_float(case.get(pct_key))
        vals = _series_vals(case.get(series_key))
        if now_v is None or pct_v is None or len(vals) < 100:
            checks[f"{anchor.lower()}_pct_recheck"] = "skip(缺序列或声明值)"
            continue
        re = rank_pct(vals, now_v)
        dev = None if re is None else round(abs(re - pct_v), 2)
        is_locked = pct_key in locked
        checks[f"{anchor.lower()}_pct_recheck"] = {
            "declared": pct_v, "recomputed": re, "dev_pp": dev, "n": len(vals),
            "locked": is_locked}
        if re is not None and dev is not None and dev > PCT_RECHECK_TOL_PP:
            if is_locked:
                should(f"{anchor}分位对账偏差 {dev}pp（声明 {pct_v}% vs 序列反算 {re}%，n={len(vals)}）"
                       f"——字段已 _locked_mechanical 锁定，疑似数据文件内部不一致，"
                       f"建议 data_refresh --rebuild 复核（不阻断引擎）")
            else:
                must(f"{anchor}分位对账失败：声明 {pct_v}% vs 序列反算 {re}%（差 {dev}pp > "
                     f"{PCT_RECHECK_TOL_PP}pp，n={len(vals)}）——疑似抄错/编数，禁止入引擎")

    # ---------- ③ 语义矛盾检测 ----------
    demand = case.get("demand")
    imagination = case.get("imagination")
    erosion = case.get("erosion_signals") or []
    n_erosion = len(erosion) if isinstance(erosion, (list, tuple)) else 0
    if imagination in ("S", "A") and demand in ("erosion", "collapse"):
        must(f"语义矛盾：imagination={imagination} 但 demand={demand}"
             "（需求侵蚀/塌方下不可能给出高想象力，复核评级或需求甄别）")
    checks["imagination_demand_ok"] = not (imagination in ("S", "A") and demand in ("erosion", "collapse"))

    if demand == "growth" and n_erosion >= 2:
        must(f"语义矛盾：demand=growth 但 erosion_signals 有 {n_erosion} 项"
             "（侵蚀≥2信号按框架须确认 demand=erosion）")
    checks["erosion_consistent"] = not (demand == "growth" and n_erosion >= 2)

    if case.get("terminal_price_down") is True and demand not in (None, "", "collapse"):
        should(f"terminal_price_down=True 但 demand={demand}≠collapse（终端价下行通常伴随需求塌方，请复核）")

    mom = _as_float(case.get("prosperity_mom_pct"))
    trend = case.get("prosperity_trend")
    if mom is not None and trend not in ("up", "flat", "down"):
        must("prosperity_mom_pct 有值但 prosperity_trend 缺失/非法（§三bis 两字段须成对提供）")
    checks["prosperity_paired"] = not (mom is not None and trend not in ("up", "flat", "down"))

    bsd = case.get("bottom_signal_detail")
    bs = case.get("bottom_signals")
    if isinstance(bsd, (list, tuple)) and bsd and isinstance(bs, int):
        n_evi = sum(1 for it in bsd if isinstance(it, dict) and it.get("evidenced"))
        if n_evi != bs:
            must(f"F4 留痕矛盾：bottom_signal_detail evidenced={n_evi} ≠ bottom_signals={bs}")
    checks["bottom_signal_paired"] = True

    fe = case.get("forward_evidence") or []
    if isinstance(fe, list) and fe:
        bad = [i for i, it in enumerate(fe)
               if not isinstance(it, dict) or not str(it.get("desc") or "").strip()
               or not str(it.get("source") or "").strip()]
        if bad:
            must(f"F2 证据链早检：forward_evidence 第 {bad} 项缺 desc 或 source（须双齐备）")
    checks["forward_evidence_complete"] = not bad if fe else True

    cats = case.get("catalysts") or []
    if isinstance(cats, list):
        for it in cats:
            if isinstance(it, dict):
                missing = [k for k in ("name", "window_months", "direction") if it.get(k) in (None, "")]
                if missing:
                    should(f"F3 结构化催化剂缺字段 {missing}（{it.get('name') or it}），引擎按旧格式降档处理")

    # ---------- ④ F2 证据方向一致性 ----------
    fwd = case.get("forward")
    if fwd in ("up", "down") and isinstance(fe, list) and fe:
        words = _UP_WORDS if fwd == "up" else _DOWN_WORDS
        numpat = _UP_NUM if fwd == "up" else _DOWN_NUM
        descs = [str(it.get("desc") or "") for it in fe if isinstance(it, dict)]
        if descs and not any(any(w in d for w in words) or numpat.search(d) for d in descs):
            should(f"F2 方向一致性：forward={fwd} 但 {len(descs)} 条证据 desc 均无"
                   f"{'上行' if fwd == 'up' else '下行'}语义词（证据与声明方向疑似背离，请复核）")
    checks["forward_direction_hint"] = True

    # ---------- ⑤ 判断字段 provenance（2026-09-07 审议 P2-⑨ · SHOULD 级零回归）----------
    # demand/volume/price/forward/imagination 是裁决最大杠杆，但方向本身过去无留痕要求。
    # analyze_etf.py 合并 FILLS 时自动落 `_fill_meta`；缺 provenance → SHOULD 提示补判断依据。
    _JUDG_FIELDS = ("demand", "volume", "price", "forward", "imagination")
    fill_meta = case.get("_fill_meta") or {}
    no_prov = [f for f in _JUDG_FIELDS
               if case.get(f) not in (None, "") and not str(fill_meta.get(f) or "").strip()]
    if no_prov:
        should(f"判断字段缺 provenance（未填即错 2026-09-07）：{no_prov} 无 _fill_meta 判断依据留痕"
               "——经 analyze_etf.py 编排会自动落 _fill_meta；手工填报请附判断依据（行业数据/研报来源）")
    checks["judgment_provenance"] = not no_prov

    return {"ok": not blockers, "blockers": blockers, "warnings": warnings, "checks": checks}


def render(res: Dict[str, Any]) -> str:
    lines = []
    if res["ok"]:
        lines.append("✅ GUARD PASS — 机械复核通过（枚举/对账/矛盾/方向 四道检查）")
    else:
        lines.append(f"⛔ GUARD FAIL — {len(res['blockers'])} 项 MUST，禁止喂引擎")
    for b in res["blockers"]:
        lines.append(f"  {b}")
    for w in res["warnings"]:
        lines.append(f"  {w}")
    rc = res["checks"].get("pb_pct_recheck")
    if isinstance(rc, dict):
        lines.append(f"  [对账] PB分位 声明{rc['declared']}% vs 反算{rc['recomputed']}%"
                     f"（差{rc['dev_pp']}pp, n={rc['n']}）")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# selfcheck
# ---------------------------------------------------------------------------
def _mk_base() -> Dict[str, Any]:
    """复核用最小合规基线（仅 guard 视角字段；引擎字段不齐不影响本层）。"""
    rng = [round(0.8 + 0.001 * i, 4) for i in range(1200)]  # 0.8→1.999 单调序列
    return {
        "sector": "自检板块", "as_of": "2026-09-04", "asset_class": "sector",
        "demand": "growth", "volume": "up", "price": "up", "forward": "flat",
        "imagination": "B", "crowding": "低",
        "_fill_meta": {"demand": "selfcheck基线", "volume": "selfcheck基线", "price": "selfcheck基线",
                       "forward": "selfcheck基线", "imagination": "selfcheck基线"},
        "pb_now": 1.20, "pb_pct": rank_pct(rng, 1.20), "pb_series": rng,
        "prosperity_trend": "up", "prosperity_mom_pct": 1.5,
        "forward_evidence": [{"type": "orders", "desc": "排产订单同比增长", "source": "selfcheck"}],
        "bottom_signals": 0,
    }


def selfcheck() -> int:
    cases = []

    # 1) 合规基线 → PASS 零告警
    cases.append(("合规基线", _mk_base(), True, 0))

    # 2) 分位抄错 10pp → MUST
    c = _mk_base(); c["pb_pct"] = round(c["pb_pct"] + 10.0, 2)
    cases.append(("PB分位抄错+10pp", c, False, 1))

    # 3) 分位漂移 2pp → PASS（容差内）
    c = _mk_base(); c["pb_pct"] = round(c["pb_pct"] + 2.0, 2)
    cases.append(("PB分位漂移+2pp(容差内)", c, True, 0))

    # 4) 枚举非法 + 想象力矛盾 → 2 MUST
    c = _mk_base(); c["volume"] = "暴涨"; c["imagination"] = "S"; c["demand"] = "collapse"
    cases.append(("枚举非法+想象力矛盾", c, False, 2))

    # 5) 侵蚀信号矛盾 + 景气不成对 → 2 MUST
    c = _mk_base(); c["erosion_signals"] = ["价格战", "库存高企"]; c.pop("prosperity_trend")
    cases.append(("侵蚀矛盾+景气不成对", c, False, 2))

    # 6) F2 缺 source + down 证据配 up 声明 → 1 MUST + 1 SHOULD
    c = _mk_base(); c["forward"] = "up"
    c["forward_evidence"] = [{"type": "orders", "desc": "出口订单同比下滑", "source": ""}]
    cases.append(("F2缺source+方向背离", c, False, 1))

    # 7) 短序列 → 对账 skip 不误伤
    c = _mk_base(); c["pb_series"] = [1.0, 1.1, 1.2]
    cases.append(("短序列skip", c, True, 0))

    ok_all = True
    for name, case, want_ok, want_n in cases:
        r = run(case)
        got_ok, got_n = r["ok"], len(r["blockers"])
        passed = (got_ok == want_ok) and (got_n == want_n)
        ok_all &= passed
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}: ok={got_ok}(期望{want_ok}) "
              f"MUST={got_n}(期望{want_n}) warnings={len(r['warnings'])}")
        if not passed:
            print(f"        blockers={r['blockers']}")
    print(f"llm_fill_guard selfcheck: {'✅ 全部通过' if ok_all else '⛔ 存在失败'}")
    return 0 if ok_all else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="LLM 轨输出机械复核层（G3）")
    ap.add_argument("--input", help="待复核 case JSON 路径")
    ap.add_argument("--selfcheck", action="store_true")
    a = ap.parse_args()
    if a.selfcheck or not a.input:
        return selfcheck()
    with open(a.input, encoding="utf-8") as f:
        case = json.load(f)
    res = run(case)
    print(render(res))
    return 0 if res["ok"] else GUARD_BLOCK_EXIT


if __name__ == "__main__":
    sys.exit(main())
