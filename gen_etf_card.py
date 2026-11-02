# -*- coding: utf-8 -*-
"""生成 ETF 决策卡片（胜率 + EV三档 + 各档目标价），仿盈利面十方块相关区块。
数据来源（2026-09-07 产物归档后统一在 runs/2026-09-07/）：
  159755 → runs/2026-09-07/verdict_159755_blend.json（锚修复后 BLEND 重算：
           PE 日频序列 1212 点(mx-ds-mcp) + 权威分类 电池→BLEND(PE0.40/PB0.60)）
  515790 → runs/2026-09-07/verdict_summary_2026-09-07.json（PB 锚正确：权威分类 光伏→PB）
输出：viz_card_159755.html / viz_card_515790.html
"""
import json, os, glob

HERE = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.join(HERE, "runs", "2026-09-07")

s = json.load(open(os.path.join(RUNS, "verdict_summary_2026-09-07.json"), encoding="utf-8"))
# 草稿现价/PB分位
drafts = {}
for f in glob.glob(os.path.join(HERE, "drafts", "_draft_*.json")) + \
         glob.glob(os.path.join(HERE, "archive", "2026-09-07", "drafts", "_draft_*.json")):
    d = json.load(open(f, encoding="utf-8"))
    etf = f.split("_draft_")[1].split("_2026")[0]
    drafts[etf] = d


def get_etf(etf):
    """159755 用 BLEND 重算 verdict；其余用 summary（按 sector 匹配）。"""
    if etf == "159755":
        p = os.path.join(RUNS, "verdict_159755_blend.json")
        if os.path.exists(p):
            return json.load(open(p, encoding="utf-8"))
    for v in s:
        if v.get("sector") == ("锂电池" if etf == "159755" else "光伏"):
            return v
    return None


# 颜色（语义 hex，跨主题稳定）
GREEN = "#3B6D11"; GREEN_BG = "#EAF3DE"
RED = "#A32D2D"; RED_BG = "#FCEBEB"
AMBER = "#BA7517"; AMBER_BG = "#FAEEDA"
BLUE = "#185FA5"; GRAY = "#5F5E5A"

def bar(value, color, maxv=None, suffix="%"):
    if maxv is None or maxv <= 0:
        maxv = max(value, 1)
    w = max(2, min(100, value / maxv * 100))
    return f'<div style="flex:1;height:10px;background:var(--color-background-tertiary);border-radius:5px;overflow:hidden"><div style="width:{w:.1f}%;height:100%;background:{color}"></div></div>'

# 每只 ETF 的锚口径元数据（与引擎 verdict / valuation_anchor 权威分类一致）
ANCHOR_META = {
    "159755": {
        "anchor_label": "BLEND 混合锚（PB 0.60 + PE 0.40）",
        "anchor_detail": "权威分类：电池→BLEND（自动推断）· PE 序列：妙想日频 PE(TTM) 1212 点（2021-09-03~2026-09-04）",
        "show_pe": True,
    },
    "515790": {
        "anchor_label": "PB 单锚",
        "anchor_detail": "权威分类：光伏→PB（重资产强周期，PE 失真）",
        "show_pe": False,
    },
}

def render(etf):
    v = get_etf(etf)
    d = drafts.get(etf, {})
    am = ANCHOR_META[etf]
    name = d.get("name") or v.get("sector")
    idx_code = d.get("index_code", "")
    idx_name = d.get("index_name", "")
    pb_now = d.get("pb_now")
    pct = d.get("pb_pct")
    price_now = d.get("price_now")
    odds = v.get("odds", {})
    ev = v.get("ev", {}) or {}
    win = v.get("win_rate")
    win_label = v.get("win_rate_label", "")
    quad = v.get("quadrant", "")
    tail = v.get("tail_risk", {}) or {}
    chk = v.get("valuation_check", {}) or {}
    pe_pct = chk.get("pe_percentile")
    eff_pct = chk.get("eff_percentile")
    pe_now = chk.get("current_pe")

    # 象限配色
    if "黄金买入" in quad:
        qc, qb = GREEN, GREEN_BG
    elif "价值陷阱" in quad or "双杀" in quad:
        qc, qb = RED, RED_BG
    elif "持有兑现" in quad:
        qc, qb = BLUE, "#E6F1FB"
    else:
        qc, qb = AMBER, AMBER_BG

    # 头部分位 chips（锚感知：BLEND 显示 eff 主决策分位 + PB/PE 两腿）
    chips = []
    if am["show_pe"] and eff_pct is not None:
        chips.append((f'eff分位 {eff_pct}%', AMBER_BG, AMBER))
    chips.append((f'PB分位 {pct}%', AMBER_BG, AMBER))
    if am["show_pe"] and pe_pct is not None:
        chips.append((f'PE分位 {pe_pct}%', AMBER_BG, AMBER))
    chips.append((quad, qb, qc))

    # 目标价五档
    anchors = odds.get("anchors", {})
    sp = odds.get("space_pct", {})
    now_pb = odds.get("now", pb_now)
    tp = (ev.get("tiered_prices", {}) or {})
    base_price = tp.get("base") or price_now
    tiers = []
    if tp and am["show_pe"]:
        # BLEND：直接用引擎 tiered_prices（混合锚五档价），极端乐观用 anchored_p95
        tiers = [
            ("极端悲观", tp.get("ext"), sp.get("p5")),
            ("悲观", tp.get("bear"), sp.get("p20")),
            ("中性", tp.get("mid"), sp.get("p50")),
            ("乐观", tp.get("opt"), sp.get("p80")),
        ]
        if base_price and sp.get("anchored_p95") is not None:
            tiers.append(("极端乐观", base_price * (1 + sp["anchored_p95"] / 100), sp.get("anchored_p95")))
    elif anchors and now_pb and base_price:
        mapping = [
            ("极端悲观", anchors.get("p5"), sp.get("p5"), tp.get("ext")),
            ("悲观", anchors.get("p20"), sp.get("p20"), tp.get("bear")),
            ("中性", anchors.get("p50"), sp.get("p50"), tp.get("mid")),
            ("乐观", anchors.get("p80"), sp.get("p80"), tp.get("opt")),
            ("极端乐观", anchors.get("anchored_p95"), sp.get("anchored_p95"), base_price * anchors.get("anchored_p95") / now_pb if now_pb else None),
        ]
        for nm, a_val, pct_a, px in mapping:
            p = px if px is not None else (base_price * a_val / now_pb if (a_val and now_pb) else None)
            tiers.append((nm, p, pct_a))
    tiers_sorted = sorted([t for t in tiers if t[2] is not None], key=lambda x: x[2])

    # EV 三档
    ev_mid = ev.get("ev_mid_pct"); ev_opt = ev.get("ev_opt_pct"); ev_tail = ev.get("ev_tail_pct")
    ev_ann_mid = ev.get("ev_mid_annualized_pct"); ev_ann_opt = ev.get("ev_opt_annualized_pct"); ev_ann_tail = ev.get("ev_tail_annualized_pct")
    horizon = ev.get("horizon_months")
    ev_has = bool(ev) and ("ev_mid_pct" in ev)

    # 赔率倍数
    r_neu = odds.get("odds_ratio_neutral"); r_opt = odds.get("odds_ratio_optimistic")

    # §三bis 买点侧别
    pc = v.get("prosperity_check", {}) or {}
    side = pc.get("buy_side", "") or "未标注"
    mom = pc.get("mom")
    side_note = pc.get("note", "") or ""
    mm = (v.get("forward_looking", {}) or {}).get("matrix_match", "")
    if "价值陷阱" in side_note or "价值陷阱" in quad:
        sc, sb = RED, RED_BG
    elif side.startswith("右侧") and "背离" in side_note:
        sc, sb = AMBER, AMBER_BG
    elif side.startswith("右侧"):
        sc, sb = GREEN, GREEN_BG
    elif side.startswith("左侧"):
        sc, sb = AMBER, AMBER_BG
    else:
        sc, sb = GRAY, "var(--color-background-secondary)"

    # ---- HTML ----
    h = []
    h.append(f'<div style="max-width:760px;background:var(--color-background-primary);border:0.5px solid var(--color-border-tertiary);border-radius:var(--border-radius-lg);padding:1.25rem 1.5rem;font-family:var(--font-sans);color:var(--color-text-primary)">')
    # header
    h.append(f'<div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:8px;margin-bottom:4px">')
    h.append(f'<div><p style="font-weight:500;font-size:15px;margin:0">{name} · {etf}</p><p style="font-size:12px;color:var(--color-text-secondary);margin:2px 0 0">{idx_name} · {idx_code}</p></div>')
    h.append(f'<div style="display:flex;gap:6px;flex-wrap:wrap">' +
             "".join(f'<span style="background:{bg};color:{c};font-size:12px;padding:3px 8px;border-radius:6px">{t}</span>' for t, bg, c in chips) +
             '</div>')
    h.append('</div>')
    pe_disp = f' · 当前PE(TTM) <b>{pe_now}</b>' if (am["show_pe"] and pe_now) else ""
    h.append(f'<p style="font-size:12px;color:var(--color-text-secondary);margin:0 0 14px">现价 <b>{base_price}</b> 元 · 当前PB <b>{pb_now}</b>{pe_disp} · 锚法 <b>{am["anchor_label"]}</b></p>')

    # ===== §三bis 买点侧别 =====
    h.append('<div style="margin-bottom:16px">')
    h.append('<p style="font-size:13px;font-weight:500;margin:0 0 8px">买点侧别（§三bis 景气环比门槛）</p>')
    h.append(f'<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">'
             f'<span style="background:{sb};color:{sc};font-size:13px;font-weight:500;padding:4px 10px;border-radius:6px">{side}</span>'
             + (f'<span style="font-size:12px;color:var(--color-text-secondary)">景气环比 ΔG = <b style="color:{GREEN if (mom or 0) >= 0 else RED}">{mom:+.1f}%</b></span>' if mom is not None else "")
             + (f'<span style="font-size:12px;color:var(--color-text-secondary)">· {mm}</span>' if mm else "")
             + '</div>')
    if side_note:
        h.append(f'<p style="font-size:12px;color:{sc};margin:6px 0 0">{side_note}</p>')
    h.append('</div>')

    # ===== 胜率 =====
    h.append('<div style="margin-bottom:16px">')
    h.append('<p style="font-size:13px;font-weight:500;margin:0 0 8px">胜率 Win Rate</p>')
    if win is not None:
        wc = GREEN if win >= 50 else (AMBER if win >= 30 else RED)
        h.append(f'<div style="display:flex;align-items:center;gap:12px">'
                 f'<span style="font-size:24px;font-weight:500;color:{wc};min-width:64px">{win}%</span>'
                 f'{bar(win, wc, 100)}</div>')
        h.append(f'<p style="font-size:12px;color:var(--color-text-secondary);margin:6px 0 0">{win_label}</p>')
    else:
        h.append(f'<div style="display:flex;align-items:center;gap:12px">'
                 f'<span style="font-size:24px;font-weight:500;color:{RED};min-width:64px">0%</span>'
                 f'{bar(0, RED, 100)}</div>')
        h.append(f'<p style="font-size:12px;color:{RED};margin:6px 0 0">{win_label}</p>')
    h.append('</div>')

    # ===== EV 三档 =====
    h.append('<div style="margin-bottom:16px">')
    h.append(f'<p style="font-size:13px;font-weight:500;margin:0 0 8px">EV 三档（持有期 {horizon} 个月 · 年化仅跨板块可比）</p>')
    if ev_has:
        rows = [("EV 中（中性情景）", ev_mid, ev_ann_mid, GREEN),
                ("EV 乐（乐观情景）", ev_opt, ev_ann_opt, BLUE),
                ("EV 尾（尾部情景）", ev_tail, ev_ann_tail, AMBER)]
        maxv = max([r[1] for r in rows if r[1] is not None] + [1])
        for nm, val, ann, col in rows:
            if val is None:
                continue
            h.append(f'<div style="display:flex;align-items:center;gap:12px;margin:7px 0">'
                     f'<span style="font-size:12px;color:var(--color-text-secondary);min-width:128px">{nm}</span>'
                     f'{bar(val, col, maxv)}'
                     f'<span style="font-size:14px;font-weight:500;color:{col};min-width:74px;text-align:right">{val:+.1f}%</span>'
                     f'<span style="font-size:11px;color:var(--color-text-tertiary);min-width:62px;text-align:right">年化 {ann:+.1f}%</span></div>')
        disc = ev.get("disclosure", "")
        if disc:
            h.append(f'<p style="font-size:11px;color:var(--color-text-tertiary);margin:8px 0 0">{disc}</p>')
    else:
        h.append(f'<div style="background:{RED_BG};border-radius:var(--border-radius-md);padding:10px 12px">'
                 f'<p style="font-size:12px;color:{RED};margin:0">{ev.get("disclosure","EV 未输出（框架否决）")}</p></div>')
    h.append('</div>')

    # ===== 各档目标价 =====
    anchor_title = "BLEND 混合锚" if am["show_pe"] else "PB分位锚定"
    h.append('<div style="margin-bottom:16px">')
    h.append(f'<p style="font-size:13px;font-weight:500;margin:0 0 8px">各档目标价（{anchor_title} · 绝对价元）</p>')
    if tiers:
        h.append('<div style="display:grid;grid-template-columns:repeat(5,1fr);gap:8px">')
        order = {"极端悲观":0,"悲观":1,"中性":2,"乐观":3,"极端乐观":4}
        for nm, tpv, pct_a in sorted([t for t in tiers if t[1] is not None], key=lambda x: order.get(x[0], 9)):
            up = (pct_a or 0) >= 0
            col = GREEN if up else RED
            h.append(f'<div style="background:var(--color-background-secondary);border-radius:var(--border-radius-md);padding:8px 6px;text-align:center">'
                     f'<div style="font-size:11px;color:var(--color-text-secondary)">{nm}</div>'
                     f'<div style="font-size:15px;font-weight:500;color:var(--color-text-primary);margin:3px 0">{tpv:.3f}</div>'
                     f'<div style="font-size:11px;color:{col}">{pct_a:+.1f}%</div></div>')
        h.append('</div>')
        # 位置条
        pmin = tiers_sorted[0][2]; pmax = tiers_sorted[-1][2]
        span = (pmax - pmin) or 1
        h.append(f'<div style="position:relative;height:34px;margin-top:12px;background:var(--color-background-tertiary);border-radius:6px">')
        h.append(f'<div style="position:absolute;left:0;top:0;bottom:0;width:33%;background:{GREEN_BG};opacity:.5"></div>')
        h.append(f'<div style="position:absolute;left:33%;top:0;bottom:0;width:34%;background:{AMBER_BG};opacity:.5"></div>')
        h.append(f'<div style="position:absolute;left:67%;top:0;bottom:0;width:33%;background:{RED_BG};opacity:.5"></div>')
        h.append(f'<div style="position:absolute;left:0;top:-4px;bottom:-4px;width:2px;background:{GRAY}"></div>')
        h.append(f'<div style="position:absolute;left:0;top:-18px;transform:translateX(-50%);font-size:10px;color:{GRAY}">现价</div>')
        for nm, tpv, pct_a in tiers_sorted:
            pos = (pct_a - pmin) / span * 100
            col = GREEN if pct_a >= 0 else RED
            h.append(f'<div style="position:absolute;left:{pos:.1f}%;top:6px;transform:translateX(-50%);font-size:9px;color:{col}">{pct_a:+.0f}%</div>')
        h.append('</div>')
        h.append(f'<p style="font-size:11px;color:var(--color-text-tertiary);margin:6px 0 0">绿=鱼头(低估区) · 橙=鱼身 · 红=鱼尾(高估区)；现价位于最左(0%)，右移=上行空间</p>')
    else:
        h.append(f'<p style="font-size:12px;color:var(--color-text-secondary)">目标价未输出（{win_label}）</p>')
    h.append('</div>')

    # ===== 赔率 & 尾部风险 =====
    h.append('<div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:12px">')
    if r_neu is not None:
        h.append(f'<div style="background:var(--color-background-secondary);border-radius:var(--border-radius-md);padding:8px 12px;flex:1;min-width:140px">'
                 f'<div style="font-size:11px;color:var(--color-text-secondary)">赔率倍数(中性/乐观)</div>'
                 f'<div style="font-size:14px;font-weight:500">{r_neu:.1f}× / {r_opt:.1f}×</div></div>')
    if tail:
        h.append(f'<div style="background:var(--color-background-secondary);border-radius:var(--border-radius-md);padding:8px 12px;flex:1;min-width:140px">'
                 f'<div style="font-size:11px;color:var(--color-text-secondary)">左尾风险(极端下行)</div>'
                 f'<div style="font-size:14px;font-weight:500;color:{RED}">{tail.get("extreme_down_pct")}%</div></div>')
    h.append('</div>')
    if tail and tail.get("flags"):
        for fl in tail["flags"]:
            h.append(f'<p style="font-size:11px;color:{AMBER};margin:2px 0 0">⚠ {fl}</p>'.replace("⚠ ", ""))

    # footer PENDING + 锚口径溯源
    h.append(f'<div style="border-top:0.5px solid var(--color-border-tertiary);margin-top:8px;padding-top:8px">'
             f'<p style="font-size:11px;color:var(--color-text-tertiary);margin:0">⚠ PENDING_VERIFICATION：未过 手算兜底+双源交叉（pb_manual/pe_manual） 与 forward_evidence(前瞻证据) 双闸，本卡仅作方向扫描，<b>不得作为买卖决策依据</b>。</p></div>')
    h.append(f'<p style="font-size:11px;color:var(--color-text-tertiary);margin:6px 0 0">锚口径：{am["anchor_detail"]}</p>')
    if am["show_pe"]:
        h.append(f'<p style="font-size:11px;color:{GREEN};margin:4px 0 0">✓ 本卡已按权威分类 BLEND 重算（eff分位 = 0.40×PE分位 + 0.60×PB分位 = {eff_pct}%），不再是旧 PB 单锚口径。</p>')

    h.append('</div>')
    return "".join(h)

for etf in ["159755", "515790"]:
    html = render(etf)
    out = os.path.join(HERE, f"viz_card_{etf}.html")
    full = f'<!DOCTYPE html><html><head><meta charset="utf-8"><style>body{{margin:0;padding:16px;background:var(--color-background-tertiary)}}</style></head><body>{html}</body></html>'
    open(out, "w", encoding="utf-8").write(full)
    print("wrote", out)
