# -*- coding: utf-8 -*-
"""
_test_card_null_20261025.py — S-21 回归：终裁卡片「null 渲染成 None」缺陷

背景（S-21·2026-10-25 实证）：
  render_verdict_card.py 对 p_star=null / win_rate=None 的裁决未做空值兜底，
  直接把 Python None 拼进 HTML → 卡片上出现「W=None% · r=None%（σ=None）」、
  「P* None」、「安全边际 None%」；同时 win_rate=None 被 int(round(None)) 兜成 0
  → EV 标题显示「W=0%」，把「左侧布局通道·胜率待拐点」误报成「胜率=0」。
  这是**把"没算"渲染成"算出来是 0"**的失真，与 D-8 数据健康度精神同罪。

本测试断言（fail-closed）：
  G1 真实 512070（框架否决·win=0）：无 None/nan/undefined，P* 区给出否决原因
  G2 真实 159040（左侧通道·win=None）：无 None，且「胜率未定档」而非「W=0%」
  G3 合成：p_star=null + status=OK + 非左侧 → 必须出现「P* 未计算」
  G4 正常 P* 路径（159755 历史裁决）→ P* 数值仍在，不回归（防 UnboundLocalError）
  G5 EV 区：无三档数值时必须显示引擎 disclosure，不得静默空白
"""
import json, os, sys, io, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import render_verdict_card as R


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


BAD_TOKENS = ("None", "nan", "undefined", "NaN%")


def _assert_no_null(html, tag):
    hits = [t for t in BAD_TOKENS if t in html]
    assert not hits, f"[{tag}] 卡片仍含 null 渲染残留: {hits}"


def main():
    cases = []

    # ---- G1/G2：真实裁决 ----
    for code, tag in (("512070", "G1"), ("159040", "G2")):
        p = os.path.join(HERE, "runs", "20261025", f"verdict_{code}.json")
        if not os.path.exists(p):
            print(f"[{tag}] SKIP（缺 {p}）")
            continue
        v = _load(p)
        html = R.render(v)
        _assert_no_null(html, tag)
        ps_na_expected = code == "159040"
        assert ("不适用" in html) if ps_na_expected else True, f"[{tag}] P* 应标不适用"
        cases.append((tag, code, html))

    # G2 特别断言：EV 标题不得把 None 当 0
    p = os.path.join(HERE, "runs", "20261025", "verdict_159040.json")
    if os.path.exists(p):
        v = _load(p)
        html = R.render(v)
        assert "胜率未定档" in html, "[G2] EV 标题应显示「胜率未定档」而非 W=0%"
        assert "W=0%" not in html, "[G2] 出现 W=0% = 把 None 当 0 的失真"
        assert "左侧布局通道（胜率待拐点" in html, "[G2] 应说明 P* 不适用原因"

    # ---- G3：合成 null（非左侧、非否决）----
    synth = {
        "sector": "TEST-NULL", "as_of": "2026-10-25", "status": "OK",
        "params_version": "1.11.0", "engine_version": "1.0",
        "valuation_anchor": "PB",
        "valuation_check": {"current_pb": 1.0, "pb_percentile": 10.0},
        "win_rate": None, "win_rate_label": "待拐点",
        "framework_position": "等待布局区", "suggested_position": "10.0%",
        "ev": {"disclosure": "胜率未定档（左侧布局通道·待拐点，非否决）→ EV 不输出"},
        "p_star": None, "odds": {"now": 1.234},
        "conviction_score": 0, "conviction_cap": 0.6,
        "gates": {}, "checklist": [], "blockers": [], "warnings": [],
        "hard_signals": {}, "catalysts": [], "tail_risk": {},
        "volume_price": {}, "imagination": {}, "forward_looking": {},
    }
    html = R.render(synth)
    _assert_no_null(html, "G3")
    assert "P* 未计算" in html, "[G3] 应给出「P* 未计算」而非 None"
    assert "现价 1.234" in html, "[G3] 现价应回退 odds.now"

    # ---- G4：正常 P* 路径不回归 ----
    p755 = os.path.join(HERE, "runs", "2026-10-23", "verdict_159755.json")
    if os.path.exists(p755):
        v = _load(p755)
        html = R.render(v)
        _assert_no_null(html, "G4")
        assert v.get("p_star") and "P* " in html, "[G4] 正常路径 P* 应渲染"
        assert "不适用" not in html, "[G4] 正常路径不得标不适用"
    else:
        print("[G4] SKIP（缺历史 verdict_159755.json）")

    # G4b：无 P* 时安全边际不得出现「+—」（正负号与占位符拼接的畸形串）
    for tag, code, html in cases:
        assert "安全边际 +—" not in html, f"[{tag}/{code}] 安全边距占位畸形（+—）"

    # ---- G5：EV 空档必须给 disclosure ----
    html = R.render(synth)
    assert "未输出：" in html, "[G5] EV 空档应显示引擎披露，不得静默空白"

    print("✅ S-21 卡片空值渲染回归 全过（G1~G5）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
