# -*- coding: utf-8 -*-
"""为 XYD-供需驱动板块 ETF 裁决生成自包含 SVG 可视化（PB/PE 5年走势 + 五档带 + 当前位置条 + 三档空间）。

🔴 铁律（2026-10-07 重写）：
- **禁止硬编码结论**：所有数值一律从 verdict_*.json（discipline 引擎终裁）与指数缓存动态读取。
  旧版把 quad/status 写死为 PENDING_VERIFICATION / "价值陷阱"，会交付过期甚至错误的结论。
- **终裁级才免水印**：仅当 verdict.status == OK 且 MUST blockers 为空时标注「终裁级 FINAL」；
  否则一律打醒目的草稿水印（"不可作为买卖决策依据"），贯彻十方图终裁铁律。
- 不依赖任何外部 CDN，SVG 内联，预览稳定。

用法：
  python gen_etf_viz.py -v runs/batt_pv_1007/verdict_159755.json runs/batt_pv_1007/verdict_515790.json
  python gen_etf_viz.py                      # 默认扫描 runs/*/verdict_*.json
  python gen_etf_viz.py -v X.json -o DIR     # 指定输出目录
"""
import argparse
import glob
import json
import os
import sys

SKILL = os.path.dirname(os.path.abspath(__file__))
PB_DIR = os.path.join(SKILL, "data", "index_daily_pb")
PE_DIR = os.path.join(SKILL, "data", "index_daily_pe")
MAP_PATH = os.path.join(SKILL, "data", "meta", "etf_index_map.json")


def _load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def load_map():
    try:
        return _load(MAP_PATH)
    except Exception:
        return {}


def load_series(idx, metric="pb"):
    d = PE_DIR if metric == "pe" else PB_DIR
    p = os.path.join(d, f"{idx}.json")
    if not os.path.exists(p):
        return None
    return _load(p)


# ---------------------------------------------------------------- SVG builders

def build_ts_svg(d, w=760, h=390, title="PB"):
    pb = d["pb"]
    p5, p20, p50, p80, p95 = d["p5"], d["p20"], d["p50"], d["p80"], d["p95"]
    now = d["now"]
    ymax = max(p95, max(v for _, v in pb)) * 1.06
    px0, py0, pw, ph = 60, 24, 672, 296

    def x(i, n):
        return px0 + (i / (n - 1)) * pw

    def y(v):
        return py0 + (1 - v / ymax) * ph

    n = len(pb)
    step = max(1, n // 200)
    pts = pb[::step]
    if pts[-1][0] != pb[-1][0]:
        pts = pts + [pb[-1]]
    poly = " ".join(f"{x(i, len(pts)):.1f},{y(v):.1f}" for i, (dt, v) in enumerate(pts))
    bands = [("P5", p5, "#2E7D32"), ("P20", p20, "#66BB6A"), ("P50", p50, "#FB8C00"),
             ("P80", p80, "#EF5350"), ("P95", p95, "#C62828")]
    band_lines = ""
    for name, val, col in bands:
        yy = y(val)
        band_lines += (f'<line x1="{px0}" y1="{yy:.1f}" x2="{px0+pw}" y2="{yy:.1f}" '
                       f'stroke="{col}" stroke-width="1" stroke-dasharray="6 4" opacity="0.8"/>')
        band_lines += (f'<text x="{px0+pw+4}" y="{yy+3:.1f}" font-size="10" fill="{col}">{name} {val:.2f}</text>')
    cx, cy = x(len(pts) - 1, len(pts)), y(now)
    cur = (f'<line x1="{cx:.1f}" y1="{py0}" x2="{cx:.1f}" y2="{py0+ph}" stroke="#1565C0" stroke-width="1" stroke-dasharray="3 3"/>'
           f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="4.5" fill="#1565C0" stroke="#fff" stroke-width="1.5"/>'
           f'<text x="{cx-4:.1f}" y="{cy-9:.1f}" font-size="11" fill="#1565C0" text-anchor="end" font-weight="bold">'
           f'当前 {now:.2f}</text>')
    xticks, last_year = "", None
    for i, (dt, val) in enumerate(pts):
        yr = dt[:4]
        if yr != last_year:
            last_year = yr
            xx = x(i, len(pts))
            xticks += f'<line x1="{xx:.1f}" y1="{py0+ph}" x2="{xx:.1f}" y2="{py0+ph+4}" stroke="#999"/>'
            xticks += f'<text x="{xx:.1f}" y="{py0+ph+17}" font-size="10" fill="#666" text-anchor="middle">{yr}</text>'
    yticks = ""
    for gv in [0, ymax / 4, ymax / 2, ymax * 3 / 4, ymax]:
        yy = y(gv)
        yticks += f'<text x="{px0-6:.1f}" y="{yy+3:.1f}" font-size="10" fill="#666" text-anchor="end">{gv:.1f}</text>'
    return (f'<svg viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg" font-family="Segoe UI, Microsoft YaHei, sans-serif">'
            f'<rect x="0" y="0" width="{w}" height="{h}" fill="#fafafa"/>'
            f'<text x="{px0}" y="14" font-size="13" fill="#333" font-weight="bold">{title} 5年历史走势（含五档分位带）</text>'
            f'<line x1="{px0}" y1="{py0}" x2="{px0}" y2="{py0+ph}" stroke="#ccc"/>'
            f'<line x1="{px0}" y1="{py0+ph}" x2="{px0+pw}" y2="{py0+ph}" stroke="#ccc"/>'
            f'{band_lines}{yticks}{xticks}'
            f'<polyline points="{poly}" fill="none" stroke="#37474F" stroke-width="1.6" opacity="0.9"/>'
            f'{cur}</svg>')


def build_bar_svg(d, w=760, h=150, metric="PB", pct=None):
    p5, p20, p50, p80, p95 = d["p5"], d["p20"], d["p50"], d["p80"], d["p95"]
    now = d["now"]
    pct = d["pct_now"] if pct is None else pct
    xmin, xmax = p5 * 0.92, p95 * 1.05
    bx0, bw = 60, 660

    def x(v):
        return bx0 + (v - xmin) / (xmax - xmin) * bw

    zones = [(p5, p20, "#C8E6C9", "鱼头(买)"), (p20, p80, "#FFF3E0", "鱼身"), (p80, p95, "#FFCDD2", "鱼尾(卖)")]
    rects = ""
    for a, b, col, lab in zones:
        xa, xb = x(a), x(b)
        rects += f'<rect x="{xa:.1f}" y="40" width="{xb-xa:.1f}" height="34" fill="{col}" stroke="#bbb" stroke-width="0.5"/>'
        rects += f'<text x="{(xa+xb)/2:.1f}" y="61" font-size="10" fill="#555" text-anchor="middle">{lab}</text>'
    ticks = ""
    for name, val, col in [("P5", p5, "#2E7D32"), ("P20", p20, "#388E3C"), ("P50", p50, "#FB8C00"),
                           ("P80", p80, "#EF5350"), ("P95", p95, "#C62828")]:
        xx = x(val)
        ticks += f'<line x1="{xx:.1f}" y1="36" x2="{xx:.1f}" y2="{78}" stroke="{col}" stroke-width="0.8" stroke-dasharray="3 2"/>'
        ticks += f'<text x="{xx:.1f}" y="92" font-size="9" fill="{col}" text-anchor="middle">{name} {val:.2f}</text>'
    cxm = x(now)
    pc = f"（分位 {pct:.1f}%）" if isinstance(pct, (int, float)) else ""
    cur = (f'<line x1="{cxm:.1f}" y1="30" x2="{cxm:.1f}" y2="84" stroke="#1565C0" stroke-width="2"/>'
           f'<polygon points="{cxm-6:.1f},30 {cxm+6:.1f},30 {cxm:.1f},40" fill="#1565C0"/>'
           f'<text x="{cxm:.1f}" y="22" font-size="11" fill="#1565C0" text-anchor="middle" font-weight="bold">'
           f'当前 {now:.2f}{pc}</text>')
    return (f'<svg viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg" font-family="Segoe UI, Microsoft YaHei, sans-serif">'
            f'<rect x="0" y="0" width="{w}" height="{h}" fill="#fafafa"/>'
            f'<text x="{bx0}" y="16" font-size="13" fill="#333" font-weight="bold">{metric} 分位位置条（P05 ~ P95）</text>'
            f'{rects}{ticks}{cur}</svg>')


def build_space_svg(space, w=760, h=200):
    """锚定估值 → 上行/下行空间。涨=红、跌=绿（A股配色）。"""
    items = [("P95 尾部", space.get("p95"), "#B71C1C"), ("P80 乐观", space.get("p80"), "#D32F2F"),
             ("P50 中性", space.get("p50"), "#F57C00"), ("P20 承压", space.get("p20"), "#388E3C"),
             ("P05 极端悲", space.get("p5"), "#2E7D32")]
    items = [(k, v, c) for k, v, c in items if isinstance(v, (int, float))]
    if not items:
        return ""
    vmax = max(abs(v) for _, v, _ in items) * 1.18 or 1.0
    bx0, bw, base, bh = 150, 520, 44, 26
    zero_x = bx0
    rows = (f'<line x1="{zero_x}" y1="{base-6}" x2="{zero_x}" y2="{base+len(items)*30-4}" '
            f'stroke="#888" stroke-width="1"/>')
    for i, (name, v, col) in enumerate(items):
        yy = base + i * 30
        lw = abs(v) / vmax * bw
        xx = zero_x if v >= 0 else zero_x - lw
        sign = "+" if v >= 0 else ""
        rows += (f'<text x="{bx0-10}" y="{yy+17}" font-size="11" fill="#444" text-anchor="end">{name}</text>'
                 f'<rect x="{xx:.1f}" y="{yy}" width="{lw:.1f}" height="{bh}" fill="{col}" opacity="0.88" rx="2"/>'
                 f'<text x="{xx+lw+6:.1f}" y="{yy+17}" font-size="11" fill="{col}" font-weight="bold">{sign}{v:.1f}%</text>')
    return (f'<svg viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg" font-family="Segoe UI, Microsoft YaHei, sans-serif">'
            f'<rect x="0" y="0" width="{w}" height="{h}" fill="#fafafa"/>'
            f'<text x="60" y="22" font-size="13" fill="#333" font-weight="bold">'
            f'锚定估值 → 上行 / 下行空间（相对当前价的 %）</text>'
            f'<text x="60" y="38" font-size="10" fill="#999">左侧=下行空间（绿），右侧=上行空间（红）</text>{rows}</svg>')


# ---------------------------------------------------------------- HTML assembly

def _esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_html(vp, etf_map):
    v = _load(vp)
    etf = os.path.basename(vp).replace("verdict_", "").replace(".json", "")
    meta = etf_map.get(etf, {}) if isinstance(etf_map, dict) else {}
    idx = meta.get("index_code") or ""
    name = meta.get("name") or etf
    idx_name = meta.get("index_name") or idx

    vc = v.get("valuation_check") or {}
    odds = v.get("odds") or {}
    space = odds.get("space_pct") or {}
    anchor = v.get("valuation_anchor") or vc.get("anchor") or "—"
    status = v.get("status") or "UNKNOWN"
    blockers = [b for b in (v.get("blockers") or []) if isinstance(b, dict) and b.get("level") == "MUST"]
    final_ok = (status == "OK" and not blockers)

    pos = v.get("framework_position") or "—"
    pos_s = str(pos)
    chip_col = {"黄金买入区": "#2E7D32", "近黄金区": "#388E3C", "观察区": "#FB8C00"}.get(pos_s, "#757575")
    if any(k in pos_s for k in ("回避", "陷阱", "卖出", "止盈", "减持")):
        chip_col = "#C62828"

    pb_now, pb_pct = vc.get("current_pb"), vc.get("pb_percentile")
    pe_now, pe_pct = vc.get("current_pe"), vc.get("pe_percentile")
    eff_pct = vc.get("eff_percentile")
    wr = v.get("win_rate")
    sug = v.get("suggested_position") or "—"
    img = (v.get("imagination") or {}).get("grade") or "—"
    fwd = v.get("forward_looking") or {}
    cops = odds.get("odds_ratio_neutral") or odds.get("odds_ratio_optimistic")

    svgs, missing = "", []
    d_pb = load_series(idx, "pb") if idx else None
    if d_pb:
        svgs += build_ts_svg(d_pb, title="PB")
        svgs += build_bar_svg(d_pb, metric="PB", pct=pb_pct)
    else:
        missing.append(f"PB 日频序列缺失({idx or 'no-index'})")
    if idx and pe_now is not None:
        d_pe = load_series(idx, "pe")
        if d_pe:
            svgs += build_bar_svg(d_pe, metric="PE", pct=pe_pct)
        else:
            missing.append(f"PE 日频序列缺失({idx})")
    svgs += build_space_svg(space)

    def f2(x):
        return f"{x:.2f}" if isinstance(x, (int, float)) else "—"

    cells = [
        ("当前 PB", f2(pb_now), f"5Y 分位 {pb_pct:.1f}%" if isinstance(pb_pct, (int, float)) else ""),
        ("当前 PE(TTM)", f2(pe_now), f"5Y 分位 {pe_pct:.1f}%" if isinstance(pe_pct, (int, float)) else ""),
        ("有效分位(锚)", f"{eff_pct:.2f}%" if isinstance(eff_pct, (int, float)) else "—", f"锚={anchor}"),
        ("赔率评级", vc.get("odds_rating") or "—",
         f"评分 {vc.get('odds_score')}" if vc.get("odds_score") is not None else ""),
        ("框架胜率", f"{wr}%" if wr is not None else "—", v.get("win_rate_label") or ""),
        ("建议仓位", str(sug), f"想象力 {img} 级"),
    ]
    kpis = "".join(f'<div class="kpi"><div class="k">{_esc(k)}</div><div class="v">{_esc(val)}</div>'
                   f'<div class="s">{_esc(sub)}</div></div>' for k, val, sub in cells)

    cross_rows = ""
    for label, now_v, man_v, cs in [("PB", pb_now, vc.get("pb_manual"), vc.get("pb_cross")),
                                    ("PE", pe_now, vc.get("pe_manual"), vc.get("pe_cross"))]:
        if now_v is None:
            continue
        cross_rows += (f"<tr><td>{label}</td><td>{f2(now_v)}</td><td>{_esc(man_v)}</td>"
                       f"<td>{_esc(cs)}</td></tr>")

    badge = ('<span class="chip ok">终裁级 FINAL</span>' if final_ok
             else f'<span class="chip pend">草稿 DRAFT · {_esc(status)}</span>')

    # 🔴 MUST 级 warning 虽不在 blockers（引擎仍给 OK），但会把仓位打到 0 / 判"回避"，
    #    若不显式渲染，用户只看 KPI（赔率极佳）会被严重误导 → 强制置顶红框。
    warns = [w if isinstance(w, str) else json.dumps(w, ensure_ascii=False)
             for w in (v.get("warnings") or [])]
    mw = [w for w in warns if w.startswith("[MUST]")]
    warn_block = ""
    if mw:
        warn_block = ("<div class='note must'><b>🚨 硬约束警示（MUST）</b><ul style='margin:6px 0;padding-left:20px'>"
                      + "".join(f"<li>{_esc(w)}</li>" for w in mw) + "</ul></div>")
    soft = [w for w in warns if w not in mw]
    soft_block = ""
    if soft:
        soft_block = ("<div class='note soft'><b>提示（SHOULD/其他）</b><ul style='margin:6px 0;padding-left:20px'>"
                      + "".join(f"<li>{_esc(w)}</li>" for w in soft[:6]) + "</ul></div>")

    if final_ok:
        note_html = ('<div class="note fin">✅ <b>终裁级</b>：双源核验与 finalgate 均 PASS、blockers 为空，'
                     '本图可直接作为决策参考。</div>')
    else:
        bl = "；".join(_esc(b.get("msg") or b.get("name") or "blocker") for b in blockers) or _esc(status)
        note_html = (f'<div class="note dra">⚠️ <b>非终裁级</b>（status={_esc(status)}）：{bl}'
                     f'<br>本图仅作方向扫描，<b>不可作为买卖决策依据</b>。</div>')

    fwd_txt = f'{fwd.get("direction") or "—"} · {fwd.get("matrix_match") or "—"}（底部信号 {fwd.get("bottom_signals")} 个）'
    cats = v.get("catalysts") or []
    cat_html = "".join(
        f"<li>{_esc(c if isinstance(c, str) else json.dumps(c, ensure_ascii=False))}</li>"
        for c in cats[:6]) or "<li>—</li>"
    missing_html = f'<div class="warn">数据缺口：{_esc("；".join(missing))}</div>' if missing else ""

    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>{_esc(name)} · XYD 供需驱动裁决</title>
<style>
body{{font-family:'Segoe UI','Microsoft YaHei',sans-serif;background:#f0f2f5;margin:0;padding:24px;color:#222}}
.card{{max-width:840px;margin:0 auto;background:#fff;border-radius:10px;box-shadow:0 2px 10px rgba(0,0,0,.08);padding:22px 26px}}
.hd{{display:flex;justify-content:space-between;align-items:flex-start;border-bottom:2px solid #eee;padding-bottom:12px;margin-bottom:14px}}
.hd h2{{margin:0;font-size:20px}} .sub{{font-size:12px;color:#888;margin-top:4px;line-height:1.5}}
.chip{{display:inline-block;padding:4px 12px;border-radius:14px;font-size:12px;font-weight:bold;color:#fff}}
.chip.ok{{background:#2E7D32}} .chip.pend{{background:#C62828}}
.kpis{{display:flex;gap:12px;margin:14px 0 8px;flex-wrap:wrap}}
.kpi{{flex:1;min-width:118px;background:#f7f9fc;border:1px solid #eef1f5;border-radius:8px;padding:10px 12px}}
.kpi .k{{font-size:11px;color:#999}} .kpi .v{{font-size:18px;font-weight:bold;margin-top:2px}}
.kpi .s{{font-size:10px;color:#8a94a6;margin-top:2px}}
table{{border-collapse:collapse;width:100%;font-size:12px;margin:10px 0}}
th,td{{border:1px solid #e6e9ef;padding:6px 8px;text-align:left}}
th{{background:#f5f7fa;color:#555}}
.note{{font-size:12px;padding:9px 12px;margin:14px 0;border-radius:4px;line-height:1.6}}
.note.fin{{background:#E8F5E9;border-left:3px solid #2E7D32;color:#1B5E20}}
.note.dra{{background:#FFEBEE;border-left:3px solid #C62828;color:#7f1010}}
.note.must{{background:#FFEBEE;border-left:4px solid #B71C1C;color:#7f1010}}
.note.must li{{margin:4px 0}}
.note.soft{{background:#FFF8E1;border-left:3px solid #FB8C00;color:#7a5a00}}
.note.soft li{{margin:4px 0}}
.warn{{font-size:11px;color:#B71C1C;margin-top:8px}}
.mut{{font-size:11px;color:#888;margin-top:6px;line-height:1.6}}
svg{{width:100%;height:auto;display:block;margin:10px 0}}
</style></head><body><div class="card">
<div class="hd"><div><h2>{_esc(name)}</h2>
<div class="sub">{_esc(etf)} · {_esc(idx_name)}（{_esc(idx)}）<br>
数据截至 {_esc(v.get("as_of"))} · 估值锚 {_esc(anchor)} · engine {_esc(v.get("engine_version"))} · params {_esc(v.get("params_version"))}</div></div>
<div style="text-align:right;white-space:nowrap"><span class="chip" style="background:{chip_col}">{_esc(pos)}</span><br>{badge}</div></div>
<div class="kpis">{kpis}</div>
{svgs}{missing_html}
<table><tr><th>估值侧</th><th>now</th><th>手算兜底</th><th>双源校验</th></tr>{cross_rows}</table>
<div class="mut">前瞻：{_esc(fwd_txt)} · 赔率比 {_esc(cops)} · 锚 P50={_esc(odds.get("anchors", {}).get("p50"))}</div>
<div style="font-size:12px;margin-top:10px"><b>催化与风险：</b>
<ul style="margin:4px 0;padding-left:20px">{cat_html}</ul></div>
{warn_block}{soft_block}
{note_html}
<div class="mut">生成脚本 gen_etf_viz.py（动态读取 verdict，无硬编码结论）</div>
</div></body></html>"""


def main():
    ap = argparse.ArgumentParser(description="XYD-供需驱动 ETF 裁决可视化（动态读取 verdict）")
    ap.add_argument("-v", "--verdict", nargs="*", default=None, help="verdict_*.json 路径")
    ap.add_argument("-o", "--out", default=None, help="输出目录（默认与 verdict 同目录）")
    args = ap.parse_args()

    vps = args.verdict
    if not vps:
        vps = sorted(glob.glob(os.path.join(SKILL, "runs", "*", "verdict_*.json")))
    if not vps:
        print("[FAIL] no verdict found", file=sys.stderr)
        return 1

    etf_map = load_map()
    outs = []
    for vp in vps:
        if not os.path.exists(vp):
            print(f"[WARN] skip missing: {vp}", file=sys.stderr)
            continue
        html = build_html(vp, etf_map)
        base = os.path.splitext(os.path.basename(vp))[0] + "_viz.html"
        out = os.path.join(args.out, base) if args.out else os.path.join(os.path.dirname(vp), base)
        if args.out:
            os.makedirs(args.out, exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            f.write(html)
        outs.append(out)
        print(f"[OK] {os.path.basename(vp)} -> {out}")
    return 0 if outs else 1


if __name__ == "__main__":
    sys.exit(main())
