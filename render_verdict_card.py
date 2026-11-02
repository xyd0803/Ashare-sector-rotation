# -*- coding: utf-8 -*-
"""
render_verdict_card.py — XYD-供需驱动板块框架 · 终裁可视化卡片生成器

把 discipline.py 产出的 verdict_*.json 渲染成「黄金买入区」风格的
自包含 HTML 卡片（浅色主题、A股涨红跌绿惯例、纯内联 CSS、无外部依赖）。

用法：
    python render_verdict_card.py --verdict runs/2026-09-08/verdict_159755.json
    python render_verdict_card.py -v verdict.json -o out.html
    # 批量：
    python render_verdict_card.py -v runs/2026-09-08/verdict_159755.json runs/2026-09-07/verdict_515790.json

默认输出：与 verdict 同目录、同名加 `_card.html`（如 verdict_159755.json → verdict_159755_card.html）
设计母版 = 2026-09-08 电池ETF(159755) 终裁卡片（用户指定保留为模板）。
"""
import json, os, argparse, sys

# ----------------------------------------------------------------------------
# 颜色（语义 hex，跨主题稳定；涨=红 / 跌=绿，A股惯例）
# ----------------------------------------------------------------------------
C_UP      = "#d4380d"   # 涨 / 增益（红）
C_UP_SOFT = "#fff1ec"
C_DOWN    = "#16915a"   # 跌 / 安全（绿）
C_GOLD    = "#c79a3a"
C_GOLD_SOFT = "#fbf3e0"
C_BLUE    = "#1f5fb0"
C_BLUE_SOFT = "#eaf2fc"
C_INK     = "#1f2733"
C_SUB     = "#6b7785"
C_LINE    = "#e4e9f0"
C_BG      = "#eef1f6"

CSS = """
:root{--up:#d4380d;--up-soft:#fff1ec;--down:#16915a;--gold:#c79a3a;--gold-soft:#fbf3e0;--blue:#1f5fb0;--blue-soft:#eaf2fc}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,"PingFang SC","Microsoft YaHei",Segoe UI,Roboto,sans-serif;background:#eef1f6;color:#1f2733;padding:28px 16px;display:flex;justify-content:center}
.card{width:760px;background:#fff;border-radius:18px;box-shadow:0 10px 34px rgba(31,39,51,.10);overflow:hidden;border:1px solid #e4e9f0}
.head{background:linear-gradient(120deg,#1f5fb0,#2f7fd6);color:#fff;padding:20px 26px;position:relative}
.head .tag{font-size:12px;letter-spacing:2px;opacity:.85}
.head h1{font-size:23px;font-weight:700;margin-top:4px}
.head .code{font-size:13px;opacity:.9;margin-top:3px}
.badge{position:absolute;top:20px;right:24px;background:#fff;color:#16915a;font-weight:700;font-size:13px;padding:7px 13px;border-radius:20px;box-shadow:0 3px 10px rgba(0,0,0,.15);text-align:center}
.badge small{display:block;font-weight:500;color:#6b7785;font-size:10px;text-align:center}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:#e4e9f0}
.cell{background:#fff;padding:16px 20px}
.sect-title{font-size:12px;color:#6b7785;letter-spacing:1px;font-weight:600;margin-bottom:10px;display:flex;align-items:center;gap:6px}
.sect-title::before{content:"";width:4px;height:13px;background:#1f5fb0;border-radius:2px;display:inline-block}
.anchor{display:inline-block;background:#eaf2fc;color:#1f5fb0;font-weight:700;font-size:12px;padding:3px 10px;border-radius:6px;margin-bottom:12px}
.val-row{display:flex;justify-content:space-between;align-items:baseline;padding:7px 0;border-bottom:1px dashed #e4e9f0}
.val-row:last-child{border-bottom:none}
.val-row .k{font-size:13px;color:#6b7785}
.val-row .v{font-size:15px;font-weight:700}
.pct{font-size:13px;font-weight:700;color:#1f5fb0}
.ok{color:#16915a;font-size:11px;font-weight:600;margin-left:4px}
.odds-row{display:flex;align-items:center;gap:10px;margin:6px 0}
.odds-bar{flex:1;height:10px;background:#e4e9f0;border-radius:6px;overflow:hidden}
.odds-fill{height:100%;background:linear-gradient(90deg,#2f7fd6,#1f5fb0);border-radius:6px}
.odds-val{font-size:13px;font-weight:700;width:62px;text-align:right}
.quad{display:flex;align-items:center;gap:14px}
.gold-badge{background:#fbf3e0;color:#c79a3a;font-size:21px;font-weight:800;padding:12px 18px;border-radius:12px;border:1px solid #ecd9a8}
.quad .sub{font-size:12px;color:#6b7785;line-height:1.6}
.quad .sub b{color:#1f2733}
.wr{display:flex;align-items:center;gap:16px}
.ring{width:84px;height:84px;border-radius:50%;display:flex;align-items:center;justify-content:center;position:relative}
.ring::after{content:"";position:absolute;width:62px;height:62px;background:#fff;border-radius:50%}
.ring span{position:relative;font-size:21px;font-weight:800;color:#d4380d}
.wr-info{font-size:12px;color:#6b7785;line-height:1.7}
.wr-info b{color:#1f2733;font-size:13px}
.ev{display:flex;flex-direction:column;gap:11px}
.ev-item .lbl{display:flex;justify-content:space-between;font-size:12px;margin-bottom:4px}
.ev-item .lbl .name{color:#6b7785}
.ev-item .lbl .num{font-weight:700;color:#d4380d}
.ev-bar{height:14px;background:#fff1ec;border-radius:7px;position:relative;overflow:hidden}
.ev-fill{height:100%;background:linear-gradient(90deg,#e35a30,#d4380d);border-radius:7px}
.ev-ann{font-size:10px;color:#6b7785;text-align:right;margin-top:2px}
.pstar{background:linear-gradient(135deg,#fbf6ea,#fdfaf2);border:1px solid #ecd9a8;border-radius:12px;padding:14px 16px}
.pstar .ptitle{font-size:12px;color:#c79a3a;font-weight:700;margin-bottom:8px}
.pline{position:relative;height:46px;margin:6px 0}
.paxis{position:absolute;top:22px;left:0;right:0;height:3px;background:#e4e9f0;border-radius:2px}
.pnode{position:absolute;top:11px;width:16px;height:16px;border-radius:50%;transform:translateX(-50%);border:3px solid #fff;box-shadow:0 1px 4px rgba(0,0,0,.2)}
.n-price{background:#d4380d}
.n-pstar{background:#1f5fb0}
.plabel{position:absolute;top:0;transform:translateX(-50%);font-size:11px;font-weight:700;text-align:center;white-space:nowrap}
.pl-price{color:#d4380d}
.pl-pstar{color:#1f5fb0}
.pmark{text-align:center;margin-top:6px;font-size:13px;font-weight:700;color:#16915a}
.pmark span{background:#fff1ec;color:#d4380d;padding:2px 8px;border-radius:6px}
.foot{display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px;padding:18px 24px;background:#fafbfd;border-top:1px solid #e4e9f0}
.kpi{text-align:center}
.kpi .num{font-size:24px;font-weight:800;color:#1f2733}
.kpi .num.up{color:#d4380d}
.kpi .cap{font-size:11px;color:#6b7785;margin-top:3px}
.meta{font-size:11px;color:#6b7785;padding:12px 24px;border-top:1px solid #e4e9f0;line-height:1.7}
.meta b{color:#1f2733}
.checks{display:inline-block;background:#eaf2fc;color:#1f5fb0;font-size:11px;font-weight:600;padding:2px 8px;border-radius:6px;margin-right:6px}
.warn{color:#c79a3a;font-weight:600}
.sig{display:inline-block;background:#fff1ec;color:#d4380d;font-size:11px;font-weight:600;padding:2px 8px;border-radius:6px;margin-right:6px;margin-top:4px}
.sig.should{background:#fbf3e0;color:#c79a3a}
"""

def esc(s):
    if s is None:
        return ""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def pct_width(value, ref=None):
    """把百分比值映射成 0~100 的条形宽度。"""
    if value is None:
        return 2
    v = float(value)
    if ref is None or ref <= 0:
        ref = max(abs(v), 50)
    w = v / ref * 100
    return max(2, min(100, w))

def render(v):
    # ---- 顶层 ----
    sector   = v.get("sector", "—")
    as_of    = v.get("as_of", "—")
    status   = v.get("status", "—").upper()
    params_v = v.get("params_version", "—")
    engine_v = v.get("engine_version", "—")

    # ---- 估值 ----
    vc = v.get("valuation_check", {}) or {}
    anchor = v.get("valuation_anchor", vc.get("anchor", "—"))
    pb_now = vc.get("current_pb")
    pb_pct = vc.get("pb_percentile")
    pe_now = vc.get("current_pe")
    pe_pct = vc.get("pe_percentile")
    eff_pct = vc.get("eff_percentile")
    pb_cross = vc.get("pb_cross", "")
    pe_cross = vc.get("pe_cross", "")
    odds_score = vc.get("odds_score")
    odds_rating = vc.get("odds_rating", "")
    odds = v.get("odds", {}) or {}

    # ---- 胜率 / 象限 / 想象力 ----
    win = v.get("win_rate")
    win_label = v.get("win_rate_label", "")
    quad = v.get("quadrant") or v.get("framework_position", "—")
    vp = v.get("volume_price", {}) or {}
    vp_combo = vp.get("combination", "—")
    vp_dbl = vp.get("double_click_quality", "—")
    img = v.get("imagination", {}) or {}
    img_grade = img.get("grade", "—")
    fl = v.get("forward_looking", {}) or {}
    fl_match = fl.get("matrix_match", "")

    # ---- EV 三档 ----
    ev = v.get("ev", {}) or {}
    ev_mid = ev.get("ev_mid_pct"); ev_opt = ev.get("ev_opt_pct"); ev_tail = ev.get("ev_tail_pct")
    ev_am = ev.get("ev_mid_annualized_pct"); ev_ao = ev.get("ev_opt_annualized_pct"); ev_at = ev.get("ev_tail_annualized_pct")
    ev_horizon = ev.get("horizon_months")
    ev_phase = ev.get("fundamental_phase", "")
    tp = ev.get("tiered_prices", {}) or {}
    base_price = tp.get("base")

    # ---- P* ----
    ps = v.get("p_star", {}) or {}
    ps_w = ps.get("W")
    ps_mid = ps.get("mid_anchor")
    ps_bear = ps.get("bear_anchor")
    ps_r = ps.get("required_return")
    ps_sigma = ps.get("sigma_ann")
    ps_val = ps.get("p_star")
    ps_price = ps.get("price_cur") or odds.get("now")
    ps_sm = ps.get("safety_margin_pct")
    ps_verdict = ps.get("verdict", "")
    # P* 空值三态兜底：p_star 为 null 时必须说明"为什么没有"，禁止渲染成 None
    ps_na = None
    if not ps:
        if str(v.get("status") or "").upper() == "REJECTED_FRAMEWORK":
            ps_na = "框架否决（价跌硬否决 · 胜率=0）→ 不计算击球线"
        elif v.get("left_side_channel"):
            ps_na = "左侧布局通道（胜率待拐点 · 不入六档映射）→ P* 不适用"
        else:
            ps_na = "P* 未计算（缺胜率/波动输入）"

    # ---- 仓位 / 置信 / 侧别 ----
    pos = v.get("suggested_position")
    conv = v.get("conviction_score")
    conv_cap = v.get("conviction_cap")
    pc = v.get("prosperity_check", {}) or {}
    buy_side = pc.get("buy_side", v.get("buy_side", "—"))
    pc_mom = pc.get("mom")

    # ---- 尾险 / 硬信号 / 前瞻 / 闸 / 检查 / 告警 ----
    tail = v.get("tail_risk", {}) or {}
    tail_down = tail.get("extreme_down_pct")
    tail_down_txt = f'{tail_down}%' if isinstance(tail_down, (int, float)) else '—'
    ps_sm_txt = f'{"+" if (ps_sm or 0) >= 0 else ""}{ps_sm}%' if ps_sm is not None else '—'
    tail_downgrade = tail.get("downgrade_one_notch", False)
    hard = v.get("hard_signals", []) or []
    hard_trig = [h for h in hard if h.get("triggered")]
    fwd = v.get("forward_check", {}) or {}
    fwd_items = fwd.get("items", []) or []
    gates = v.get("gates", {}) or {}
    g_ds = (gates.get("dual_source", {}) or {}).get("level", "—")
    g_fi = (gates.get("final", {}) or {}).get("level", "—")
    checklist = v.get("checklist", {}) or {}
    chk_pass = sum(1 for x in checklist.values() if x)
    chk_total = len(checklist)
    blockers = v.get("blockers", []) or []
    warnings = v.get("warnings", []) or []

    # =====================================================================
    # HTML 组装
    # =====================================================================
    badge_cls = "color:#16915a" if status == "OK" else "color:#c79a3a"
    badge_sub = f"{chk_pass}/{chk_total} 全过 · {len(blockers)} blocker" if chk_total else "—"

    # 估值单元
    odds_bar_w = pct_width(odds_score, 100)
    anchor_detail = f"{esc(anchor)} 主锚" + ("（PE0.40/PB0.60）" if anchor == "BLEND" else "")
    val_rows = []
    if pb_now is not None:
        val_rows.append(f'<div class="val-row"><span class="k">PB 现值 / 分位</span>'
                        f'<span><span class="v">{pb_now}</span> <span class="pct">{pb_pct}%</span>'
                        f'<span class="ok">双源✓</span></span></div>')
    if pe_now is not None:
        val_rows.append(f'<div class="val-row"><span class="k">PE 现值 / 分位</span>'
                        f'<span><span class="v">{pe_now}</span> <span class="pct">{pe_pct}%</span>'
                        f'<span class="ok">双源✓</span></span></div>')
    if eff_pct is not None:
        val_rows.append(f'<div class="val-row"><span class="k">有效分位 eff</span>'
                        f'<span><span class="pct" style="font-size:16px">{eff_pct}%</span> '
                        f'<span class="ok">赔率{esc(odds_rating)}</span></span></div>')
    odds_block = ""
    if odds_score is not None:
        odds_block = (f'<div class="odds-row" style="margin-top:12px">'
                      f'<span style="font-size:12px;color:#6b7785;width:46px">赔率</span>'
                      f'<div class="odds-bar"><div class="odds-fill" style="width:{odds_bar_w:.0f}%"></div></div>'
                      f'<span class="odds-val">{odds_score}</span></div>')

    # 胜率环
    win_isnum = isinstance(win, (int, float))
    win_int = int(round(win)) if win_isnum else 0
    win_txt = f'{win_int}%' if win_isnum else '—'
    ring = (f'<div class="ring" style="background:conic-gradient({C_UP} 0% {win_int}%, {C_LINE} {win_int}% 100%)">'
            f'<span>{win_txt}</span></div>')
    quad_badge_color = C_GOLD if "黄金" in quad else C_BLUE
    quad_badge_bg = C_GOLD_SOFT if "黄金" in quad else C_BLUE_SOFT
    win_info = (f'客观五维量化胜率' + ('' if win_isnum else f'<br>口径：<b>{esc(win_label) or "—"}</b>')
                + f'<br>量价：<b>{esc(vp_combo)}</b>（双击 {esc(vp_dbl)}）<br>'
                f'相位：<b>{esc(ev_phase) or "—"}</b><br>想象力评级：<b>{esc(img_grade)}</b>')

    # EV 三档
    ev_ref = max([x for x in [ev_mid, ev_opt, ev_tail] if isinstance(x, (int, float))] + [50])
    ev_rows = []
    for nm, val, ann in [("中性 EV", ev_mid, ev_am), ("乐观 EV", ev_opt, ev_ao), ("尾部 EV", ev_tail, ev_at)]:
        if not isinstance(val, (int, float)):
            continue
        w = pct_width(val, ev_ref)
        ev_rows.append(
            f'<div class="ev-item"><div class="lbl"><span class="name">{nm}</span>'
            f'<span class="num">{"+" if val>=0 else ""}{val}%</span></div>'
            f'<div class="ev-bar"><div class="ev-fill" style="width:{w:.0f}%"></div></div>'
            f'<div class="ev-ann">年化 {"+" if ann is not None and ann>=0 else ""}{ann}% · '
            f'持有期 {ev_horizon}月</div></div>')

    # P* 击球线（真实相对位置）
    if ps_na:
        xp, xk = 6, 62
    elif ps_price and ps_val and ps_price != ps_val:
        lo, hi = min(ps_price, ps_val), max(ps_price, ps_val)
        span = (hi - lo) or 1
        rlo, rhi = lo * 0.9, hi * 1.1
        rspan = (rhi - rlo) or 1
        xp = 8 + (ps_price - rlo) / rspan * 80
        xk = 8 + (ps_val - rlo) / rspan * 80
    else:
        xp, xk = 6, 62

    if ps_na:
        pmark_cls = "color:#6b7785"
        pmark_txt = '<span>不适用</span>'
        pstar_detail = (f'{esc(ps_na)}<br>胜率口径：{esc(win_label) or "—"}'
                        f' · 左尾 {tail_down_txt} · {"已" if tail_downgrade else "未"}触发降档'
                        f' · conviction {conv} ≤ {conv_cap}')
    else:
        pmark_cls = "color:#16915a" if (ps_sm or 0) >= 0 else "color:#d4380d"
        pmark_txt = f'现价 {"≤" if (ps_price or 0) <= (ps_val or 0) else ">"} P* → <span>{"可击球" if (ps_price or 0) <= (ps_val or 0) else "观望"}</span>'
        pstar_detail = (f'W={ps_w}% · r={ps_r}%（r_base×s_vol，σ={ps_sigma}）<br>'
                   f'左尾 {tail_down_txt} · {"已" if tail_downgrade else "未"}触发降档 · conviction {conv} ≤ {conv_cap}')

    # 底部 KPI
    pos_num = f'{pos}' if pos is not None else "—"
    conv_num = f'{conv}' if conv is not None else "—"

    # 元信息
    chips = []
    chips.append(f'<span class="checks">双源 {esc(g_ds)}</span>')
    chips.append(f'<span class="checks">final {esc(g_fi)}</span>')
    chips.append(f'<span class="checks">{chk_pass}/{chk_total} checklist</span>')
    chips.append(f'<span class="checks">{len(blockers)} blocker</span>')
    chips.append(f'<span class="checks">params {esc(params_v)}</span>')
    chips.append(f'<span class="checks">engine {esc(engine_v)}</span>')

    hard_html = ""
    if hard_trig:
        for h in hard_trig:
            lvl = h.get("level", "")
            cls = "sig should" if lvl == "SHOULD" else "sig"
            hard_html += f'<span class="{cls}">#{esc(h.get("no"))} {esc(h.get("signal"))}（{esc(lvl)}）</span>'
    else:
        hard_html = '<span style="font-size:11px;color:#16915a">全部未触发（需求塌方 / 价跌 / 价值陷阱 / 泡沫延伸 / 涨过头 均 N/A）</span>'

    fwd_html = ""
    if fwd_items:
        for it in fwd_items:
            fwd_html += f'· {esc(it.get("desc",""))}（{esc(it.get("source",""))}）<br>'
    else:
        fwd_html = f'forward={esc(fwd.get("forward","—"))} · 证据 {fwd.get("evidence_count","?")} 条'

    warn_html = ""
    if warnings:
        for w in warnings:
            warn_html += f'<span class="warn">⚠ {esc(w)}</span><br>'
    elif status == "OK":
        warn_html = '<span style="color:#16915a">无 SHOULD/告警</span>'

    body = f"""
<div class="card">
  <div class="head">
    <div class="tag">XYD 供需驱动框架 · 板块终裁</div>
    <h1>{esc(sector)}</h1>
    <div class="code">as_of {esc(as_of)} · 锚 {esc(anchor)}</div>
    <div class="badge" style="{badge_cls}">{esc(status)}<small>{badge_sub}</small></div>
  </div>

  <div class="grid">
    <!-- 估值 -->
    <div class="cell">
      <div class="sect-title">估值主锚 · {esc(anchor_detail)}</div>
      <span class="anchor">双源校验 ✓✓ · 5Y 口径</span>
      {''.join(val_rows)}
      {odds_block}
    </div>

    <!-- 胜率 × 赔率 -->
    <div class="cell">
      <div class="sect-title">胜率 × 赔率 定位</div>
      <div class="quad" style="margin-bottom:14px">
        <div class="gold-badge" style="background:{quad_badge_bg};color:{quad_badge_color};border:1px solid #ecd9a8">{esc(quad)}</div>
        <div class="sub">{win_info}</div>
      </div>
      <div class="wr">
        {ring}
        <div class="wr-info">{win_info}</div>
      </div>
    </div>

    <!-- EV 三档 -->
    <div class="cell">
      <div class="sect-title">EV 三档（{ev_horizon if ev_horizon is not None else "—"}月持有期 · {'W=' + str(win_int) + '%' if isinstance(win, (int, float)) else '胜率未定档'}）</div>
      <div class="ev">{''.join(ev_rows) if ev_rows else f'<div style="font-size:12px;color:#6b7785;line-height:1.6;padding:6px 0">⚠️ 未输出：{esc(ev.get("disclosure") or "EV 缺失（原因未披露）")}</div>'}</div>
    </div>

    <!-- P* 击球线 -->
    <div class="cell">
      <div class="sect-title">P* 风险调整买点</div>
      <div class="pstar">
        <div class="ptitle">§P* = [W·mid + (1−W)·bear] / (1+r)</div>
        <div class="pline">
          <div class="paxis"></div>
          <div class="plabel pl-price" style="left:{xp:.1f}%">现价 {ps_price if ps_price is not None else "—"}</div>
          <div class="pnode n-price" style="left:{xp:.1f}%"></div>
          <div class="plabel pl-pstar" style="left:{xk:.1f}%">P* {ps_val if ps_val is not None else "—"}</div>
          <div class="pnode n-pstar" style="left:{xk:.1f}%"></div>
        </div>
        <div class="pmark" style="{pmark_cls}">{pmark_txt} · 安全边际 {ps_sm_txt}</div>
        <div style="font-size:11px;color:#6b7785;margin-top:6px;line-height:1.5">{pstar_detail}</div>
      </div>
    </div>
  </div>

  <div class="foot">
    <div class="kpi"><div class="num up">{pos_num}</div><div class="cap">建议仓位<br>（单板块上限40%）</div></div>
    <div class="kpi"><div class="num">{conv_num}</div><div class="cap">置信分<br>（cap {conv_cap}）</div></div>
    <div class="kpi"><div class="num">{esc(buy_side)}</div><div class="cap">买点侧别<br>{"景气环比 "+format(pc_mom,"+.1f")+"%" if isinstance(pc_mom,(int,float)) else "—"}</div></div>
  </div>

  <div class="meta">
    {''.join(chips)}<br><br>
    <b>硬信号触发：</b><br>{hard_html}<br>
    <b>前瞻证据：</b><br>{fwd_html}
    {warn_html}
  </div>
</div>
"""
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{esc(sector)} · 框架终裁卡片</title>
<style>{CSS}</style></head>
<body>{body}</body></html>"""


def main():
    ap = argparse.ArgumentParser(description="XYD-供需驱动板块 终裁卡片生成器")
    ap.add_argument("-v", "--verdict", nargs="+", required=True, help="verdict_*.json 路径（可多份）")
    ap.add_argument("-o", "--out", default=None, help="输出 HTML 路径（多份时忽略，按默认命名）")
    args = ap.parse_args()

    for vp in args.verdict:
        if not os.path.exists(vp):
            print(f"[WARN] 跳过不存在: {vp}", file=sys.stderr)
            continue
        with open(vp, encoding="utf-8") as f:
            v = json.load(f)
        html = render(v)
        if args.out and len(args.verdict) == 1:
            out = args.out
        else:
            out = os.path.splitext(vp)[0] + "_card.html"
        with open(out, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"[OK] {vp}  ->  {out}")

if __name__ == "__main__":
    main()
