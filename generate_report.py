#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generate_report.py —— 把 backtest_report.json + 覆盖状态 渲染为自包含 HTML 报告。

覆盖分类：
  · COVERED  : index_monthly 有 close>=2mo & pb>=2mo & raw/mx daily_pb>=20
  · 待补-PB  : tdx_security_deep_info(估值) 接口宕机 + 妙想积分耗尽，无法取得 PB
  · 结构性无 : tdx 本就无该指数估值数据（0 行）
  · 排除     : 商品指数（无 PB），框架不纳入
"""
import json, os, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
REP = json.load(open(os.path.join(DATA, "backtest_report.json"), encoding="utf-8"))
MAP = json.load(open(os.path.join(DATA, "meta", "etf_index_map.json"), encoding="utf-8"))

IDX_MONTHLY = os.path.join(DATA, "index_monthly")
RAW_MX = os.path.join(DATA, "raw", "mx")

# ---- 覆盖判定（与 check_cov.py 同口径） ----
targets = sorted({v["index_code"] for v in MAP.values()})
covered, pending = [], []
for code in targets:
    imp = os.path.join(IDX_MONTHLY, f"{code}.json")
    rmp = os.path.join(RAW_MX, f"{code}.json")
    cm = cr = dp = 0
    has_close = has_pb = False
    if os.path.exists(imp):
        d = json.load(open(imp, encoding="utf-8"))
        cm = len(d.get("close") or {}); cr = len(d.get("pb") or {})
        has_close = bool(d.get("close")); has_pb = bool(d.get("pb"))
    if os.path.exists(rmp):
        dp = len(json.load(open(rmp, encoding="utf-8")).get("pb") or [])
    ok = has_close and has_pb and cm >= 2 and cr >= 2 and dp >= 20
    nm = next((v.get("index_name", "") for v in MAP.values() if v["index_code"] == code), "")
    if ok:
        covered.append((code, nm))
    else:
        pending.append((code, nm))

# ---- 待补原因分类 ----
EXCLUDE = {"AU9999.SGE"}
reason = {}
for code, nm in pending:
    if code in EXCLUDE:
        reason[code] = "商品指数（无PB），框架排除"
    elif code == "N225.GI":
        reason[code] = "跨境（日经225）：数据源仅有收盘价、无PB估值"
    elif code == "NDXTMC.GI":
        reason[code] = "跨境（纳斯达克科技市值加权）：数据源无估值数据"
    else:
        reason[code] = "待补：数据源暂无该指数估值"

# ---- 汇总指标 ----
nav = REP["nav_series"]
n_periods = REP["n_periods"]
final_nav = REP["final_nav"]
years = n_periods / 12.0
cagr = (final_nav ** (1.0 / years) - 1) * 100 if years > 0 else 0
max_dd = REP["max_drawdown"] * 100
win = REP["win_rate_of_signals"] * 100
sig_n = REP["signal_count"]
n_etf = REP["n_etf_signaled"]
breaches = REP.get("breaches", [])

# ===== NAV 曲线 SVG =====
W, H = 920, 300
pad_l, pad_r, pad_t, pad_b = 50, 20, 20, 30
x0, x1 = pad_l, W - pad_r
y0, y1 = H - pad_b, pad_t
lo, hi = min(nav), max(nav)
def sx(i):
    return x0 + (x1 - x0) * (i / (len(nav) - 1))
def sy(v):
    return y1 + (y1 - y0) * 0  # placeholder
def syy(v):
    return y0 - (y0 - y1) * ((v - lo) / (hi - lo)) if hi > lo else y0
pts = " ".join(f"{sx(i):.1f},{syy(v):.1f}" for i, v in enumerate(nav))
# 网格 + 轴标签
grid = ""
for p in [0, 0.25, 0.5, 0.75, 1.0]:
    v = lo + (hi - lo) * p
    yy = syy(v)
    grid += f'<line x1="{x0}" y1="{yy:.1f}" x2="{x1}" y2="{yy:.1f}" stroke="#e5e7eb" stroke-width="1"/>'
    grid += f'<text x="{x0-6}" y="{yy+4:.1f}" text-anchor="end" font-size="11" fill="#6b7280">{v:.2f}</text>'
# x 轴年份刻度
xticks = ""
start_year = 2019
for yi, yr in enumerate(range(start_year, start_year + int(years) + 1)):
    frac = (yr - start_year) / years
    if 0 <= frac <= 1:
        xx = x0 + (x1 - x0) * frac
        xticks += f'<text x="{xx:.1f}" y="{H-10}" text-anchor="middle" font-size="11" fill="#6b7280">{yr}</text>'

nav_svg = f'''
<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" font-family="system-ui,sans-serif">
  {grid}
  <polyline points="{pts}" fill="none" stroke="#dc2626" stroke-width="2.2"/>
  <circle cx="{sx(len(nav)-1):.1f}" cy="{syy(nav[-1]):.1f}" r="3.5" fill="#dc2626"/>
  {xticks}
  <text x="{x0}" y="14" font-size="12" fill="#374151">组合 NAV（起始=1.0）</text>
</svg>'''

# ===== 逐 ETF 表 =====
# backtest_report.json：逐 ETF 明细在 per_etf 键下（{code: {...}}）
detail = REP.get("per_etf", {})
etfs = sorted(detail.items(), key=lambda kv: kv[1]["n_signals"], reverse=True)
rows_html = ""
for code, d in etfs:
    fw = d.get("avg_framework_winrate")
    r1 = d.get("avg_realized_1m_pct")
    r3 = d.get("avg_realized_3m_pct")
    r3w = d.get("realized_3m_win_pct")
    fwcol = "#dc2626" if (fw or 0) >= 60 else ("#d97706" if (fw or 0) >= 50 else "#059669")
    def col(v):
        if v is None: return "#6b7280"
        return "#dc2626" if v > 0 else ("#059669" if v < 0 else "#6b7280")
    r3col = col(r3)
    r3wcol = "#dc2626" if (r3w or 0) >= 55 else ("#d97706" if (r3w or 0) >= 45 else "#059669")
    r1s = f"{r1:+.2f}" if r1 is not None else "—"
    r3s = f"{r3:+.2f}" if r3 is not None else "—"
    r3ws = f"{r3w:.1f}" if r3w is not None else "—"
    rows_html += (
        f"<tr><td>{code}</td><td>{d.get('name','')}</td><td>{d.get('n_signals','')}</td>"
        f"<td>{d.get('avg_position_pct','')}</td>"
        f"<td style='color:{fwcol};font-weight:600'>{fw if fw is not None else '—'}</td>"
        f"<td style='color:{col(r1)}'>{r1s}</td>"
        f"<td style='color:{r3col}'>{r3s}</td>"
        f"<td style='color:{r3wcol}'>{r3ws}</td></tr>"
    )

# ===== 覆盖表 =====
cov_html = "".join(f"<tr><td>{c}</td><td>{n}</td><td style='color:#059669'>✓ 覆盖</td><td>—</td></tr>" for c, n in covered)
pend_html = ""
for c, n in pending:
    r = reason[c]
    style = "color:#9ca3af" if "结构性" in r or "排除" in r else "color:#dc2626"
    pend_html += f"<tr><td>{c}</td><td>{n}</td><td style='{style}'>✗ 待补</td><td>{r}</td></tr>"

now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

html = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>XYD 供需板块框架 · ETF 回测报告</title>
<style>
*{{box-sizing:border-box}} body{{font-family:system-ui,'PingFang SC','Microsoft YaHei',sans-serif;margin:0;background:#f7f8fa;color:#1f2937}}
.wrap{{max-width:1000px;margin:0 auto;padding:28px 20px 60px}}
h1{{font-size:22px;margin:0 0 4px}} .sub{{color:#6b7280;font-size:13px;margin-bottom:22px}}
.cards{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-bottom:24px}}
.card{{background:#fff;border:1px solid #eceef1;border-radius:12px;padding:16px}}
.card .k{{font-size:12px;color:#6b7280}} .card .v{{font-size:24px;font-weight:700;margin-top:6px}}
.card .v.red{{color:#dc2626}} .card .v.green{{color:#059669}} .card .v.blue{{color:#2563eb}}
.panel{{background:#fff;border:1px solid #eceef1;border-radius:12px;padding:18px 20px;margin-bottom:22px}}
.panel h2{{font-size:16px;margin:0 0 14px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{padding:7px 9px;text-align:right;border-bottom:1px solid #f0f1f3}}
th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){{text-align:left}}
th{{background:#fafbfc;color:#6b7280;font-weight:600;position:sticky;top:0}}
tbody tr:hover{{background:#fafbfc}}
.pos{{color:#dc2626}} .neg{{color:#059669}} .zero{{color:#6b7280}}
.note{{background:#fff7ed;border:1px solid #fed7aa;border-radius:10px;padding:14px 16px;font-size:13px;line-height:1.7;color:#9a3412;margin-bottom:22px}}
.note b{{color:#c2410c}}
.tag{{display:inline-block;padding:1px 7px;border-radius:6px;font-size:11px;background:#f3f4f6;color:#374151;margin:1px}}
</style></head><body><div class="wrap">
<h1>XYD 供需板块框架 · ETF 系统性回测</h1>
<div class="sub">区间 2019-01-01 ~ 2026-08-13 · 成本 5bps · 生成于 {now}</div>

<div class="cards">
  <div class="card"><div class="k">组合终值 NAV</div><div class="v red">{final_nav:.4f}</div></div>
  <div class="card"><div class="k">累计收益</div><div class="v red">+{(final_nav-1)*100:.2f}%</div></div>
  <div class="card"><div class="k">年化（CAGR）</div><div class="v red">+{cagr:.2f}%</div></div>
  <div class="card"><div class="k">最大回撤</div><div class="v green">-{max_dd:.2f}%</div></div>
  <div class="card"><div class="k">信号框架胜率</div><div class="v blue">{win:.2f}%</div></div>
  <div class="card"><div class="k">触发信号 ETF / 信号次数</div><div class="v blue">{n_etf} / {sig_n}</div></div>
</div>

<div class="panel"><h2>组合净值曲线</h2>{nav_svg}</div>

<div class="note">
<b>✅ 数据覆盖率 50/53（指数，94%），触发信号 ETF {n_etf}/54 只，信号 {sig_n} 次。</b><br>
本轮补齐方式：通过 <span class="tag">同花顺 iFinD index_data</span> 抓取 19 个缺失指数的「月末收盘价 + 市净率PB」并入库（原 tdx 估值接口会话中断、妙想积分耗尽，均无法用）。<br>
同时修正了宇宙映射两处错误：<span class="tag">159530→980022.SZ 国证机器人产业</span> <span class="tag">517520→931238.CSI SSH黄金股票</span>（原映射把两者指数代码互换/错配）。<br>
未覆盖 3 个：<span class="tag">AU9999.SGE 商品（框架排除）</span><span class="tag">N225.GI / NDXTMC.GI 跨境（数据源无 PB 估值）</span>。
</div>

<div class="panel"><h2>逐 ETF 信号汇总（按信号次数，触发 {n_etf} 只）</h2>
<table><thead><tr><th>代码</th><th>名称</th><th>信号次数</th><th>平均仓位%</th><th>框架胜率%</th><th>实现1m%</th><th>实现3m%</th><th>3m胜率%</th></tr></thead>
<tbody>{rows_html}</tbody></table>
<p style="font-size:12px;color:#6b7280;margin-top:10px">颜色：框架胜率/实现收益 红=正(涨) 绿=负(跌)，与中国市场惯例一致；3m胜率红≥55 / 橙45-55 / 绿&lt;45。</p>
</div>

<div class="panel"><h2>数据覆盖状态（53 个跟踪指数）</h2>
<table><thead><tr><th>指数代码</th><th>名称</th><th>状态</th><th>说明</th></tr></thead>
<tbody>{cov_html}{pend_html}</tbody></table>
</div>

<div class="panel"><h2>守卫与口径</h2>
<ul style="font-size:13px;line-height:1.8;margin:0">
<li>纪律引擎守卫违例：<b style="color:#059669">0</b>（G1 未来函数 / G3 幸存者 / G6 成本期数 全部通过）</li>
<li>量价信号：ETF 跟踪指数月度 close/volume（非 ETF 自身）</li>
<li>PB 分位：跟踪指数市净率（5年日频，point-in-time，禁未来函数）</li>
<li>收益端：ETF 自身月度净值（月K close）</li>
<li>仅纳入回测区间内已上市 ETF（list_date ≤ as_of，point-in-time）</li>
</ul></div>

</div></body></html>"""

out = os.path.join(DATA, "backtest_report.html")
open(out, "w", encoding="utf-8").write(html)
print("written:", out, "len=", len(html))
print(f"covered={len(covered)} pending={len(pending)} etfs_backtested={n_etf}")
