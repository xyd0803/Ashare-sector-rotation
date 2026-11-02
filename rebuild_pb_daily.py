#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rebuild_pb_daily.py —— 用 westock 日K点位 + 本地月频 PB 反推 BVPS → 重建 5 年日频 PB
================================================================================
适用：mx-ds-mcp 本周单元格配额用尽时，对缺口指数用本路径兜底。
原理：PB = 指数点位 / BVPS；BVPS 为慢变量（财报级更新），用本地月频 PB 反推
      BVPS_m = close_m / PB_m，线性插值到日频；PB_daily = close_daily / BVPS_d。
输入：
  · westock 落盘文件（tool-results/mcp-connector-proxy-westock-mcp_data_kline-*.txt）
  · data/index_monthly/{CODE}.json  （pb:{ym:v}, close:{ym:v} 月频）
  · data/raw/mx/{CODE}.json         （pb:[[date,val]...] 若 index_monthly 缺失）
输出：data/index_daily_pb/{CODE}.json（与 mx 直拉同构，source=rebuilt-westock+bvps）
"""
from __future__ import annotations
import argparse, glob, json, os, sys
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
OUT = os.path.join(DATA, "index_daily_pb")
MONTHLY = os.path.join(DATA, "index_monthly")
RAW_MX = os.path.join(DATA, "raw", "mx")
WST_GLOB = os.path.expanduser("~/.workbuddy/projects/*/*/tool-results/mcp-connector-proxy-westock-mcp_data_kline-*.txt")

# westock 代码 → 标准指数代码
W2CODE = {
    "sh000685": "000685.SH", "sh000688": "000688.SH", "sh000015": "000015.SH",
    "cs000813": "000813.CSI", "cs930632": "930632.CSI", "cs930707": "930707.CSI",
    "cs930709": "930709.CSI", "cs930713": "930713.CSI", "cs930722": "930722.CSI",
    "cs930915": "930915.CSI", "cs930955": "930955.CSI", "cs931066": "931066.CSI",
    "cs931238": "931238.CSI", "cs931994": "931994.CSI", "cs931866": "931866.CSI",
    "csH30199": "H30199.CSI", "csH30202": "H30202.CSI", "csH30269": "H30269.CSI",
    "sz980017": "980017.SZ", "sz980022": "980022.SZ", "sz980027": "980027.SZ",
    "sz980092": "980092.SZ", "sz399975": "399975.SZ", "sz399976": "399976.SZ",
    "sz399986": "399986.SZ", "sz399987": "399987.SZ", "sz399989": "399989.SZ",
    "sz399998": "399998.SZ", "sz980018": "980018.SZ", "sz987018": "987018.SZ",
    "hkHSTECH": "HSTECH.HI", "cs931151": "931151.CSI", "cs931719": "931719.CSI",
    "cs931637": "931637.CSI", "cs931160": "931160.CSI", "cs931743": "931743.CSI",
    "cs930851": "930851.CSI", "cs930901": "930901.CSI", "cs930598": "930598.CSI",
    "cs930641": "930641.CSI", "cs930915": "930915.CSI", "cs931752": "931752.CSI",
    "cs931643": "931643.CSI", "csH30184": "H30184.CSI", "csH30035": "H30035.CSI",
    "cs000932": "000932.SH", "cs931238": "931238.CSI", "sh950125": "950125.SH",
    "cs000510": "000510.CSI", "sh510880": "ETF", "sh518880": "ETF", "sh513530": "ETF",
    "sh513180": "ETF",
}


def q(p: float, vals):
    if not vals:
        return None
    s = sorted(vals)
    n = len(s)
    pos = p * (n - 1) / 100.0
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def pct_rank(v, vals):
    if not vals:
        return None
    return round(100.0 * sum(1 for x in vals if x <= v) / len(vals), 2)


def load_monthly_pb(code: str):
    """返回升序 [[date, pb, close]] 序列（优先 raw/mx 日频配对；其次 index_monthly 月频）。
    PB 合理性过滤：0.05 < pb < 40（拒绝点位污染源）。"""
    pts = []
    # 优先 raw/mx（若日频且有 close 配对）
    f2 = os.path.join(RAW_MX, f"{code}.json")
    if os.path.exists(f2):
        d = json.load(open(f2, encoding="utf-8"))
        pb = d.get("pb") or []
        cl = d.get("close") or []
        cmap = {c[0]: c[1] for c in cl}
        for p in pb:
            dt, pv = p[0], p[1]
            cv = cmap.get(dt, 0)
            if pv > 0 and cv > 0:
                pts.append([dt, pv, cv])
        if pts:
            vals = [p[1] for p in pts]
            if min(vals) > 0.05 and max(vals) < 40:
                return pts
            pts = []  # 污染源，回退 index_monthly
    # index_monthly（dict: {ym:v}）
    f1 = os.path.join(MONTHLY, f"{code}.json")
    if os.path.exists(f1):
        d = json.load(open(f1, encoding="utf-8"))
        pb_d, cl_d = d.get("pb", {}), d.get("close", {})
        if isinstance(pb_d, dict) and isinstance(cl_d, dict):
            for ym in sorted(pb_d):
                if pb_d[ym] in (None, "-", ""):
                    continue
                try:
                    pv = float(pb_d[ym])
                    cv = float(cl_d.get(ym, 0) or 0)
                except (TypeError, ValueError):
                    continue
                if pv <= 0 or cv <= 0:
                    continue
                dt = ym + "-28" if len(ym) == 7 else ym
                pts.append([dt, pv, cv])
            if pts:
                vals = [p[1] for p in pts]
                if min(vals) > 0.05 and max(vals) < 40:
                    return pts
                return []
    return []


def interp_bvps_daily(monthly_pts, daily_dates):
    """月频 BVPS 线性插值到日频。返回 dict date->bvps。"""
    bv = sorted((d, pb, cl) for d, pb, cl in monthly_pts)  # 升序
    bvps_pts = []
    prev = None
    for d, pb, cl in bv:
        if cl > 0 and pb > 0:
            bvps_pts.append([d, cl / pb])
    if not bvps_pts:
        return {}
    out = {}
    idx = 0
    for dd in daily_dates:
        while idx < len(bvps_pts) and bvps_pts[idx][0] <= dd:
            idx += 1
        if idx == 0:
            out[dd] = bvps_pts[0][1]
        elif idx >= len(bvps_pts):
            out[dd] = bvps_pts[-1][1]
        else:
            d0, v0 = bvps_pts[idx - 1]
            d1, v1 = bvps_pts[idx]
            try:
                t0 = date.fromisoformat(d0); t1 = date.fromisoformat(d1); td = date.fromisoformat(dd)
            except ValueError:
                out[dd] = v0
                continue
            span = (t1 - t0).days
            if span <= 0:
                out[dd] = v0
            else:
                frac = (td - t0).days / span
                out[dd] = v0 + (v1 - v0) * frac
    return out


def collect_westock_points() -> dict:
    """扫描全部 westock 落盘文件 → {code: [[date, close]...] 升序}。"""
    got = {}
    for f in sorted(set(glob.glob(WST_GLOB))):
        try:
            raw = open(f, encoding="utf-8").read()
            idx = raw.find("{")
            d = json.loads(raw[idx:])
        except Exception:
            continue
        data = d.get("data", {})
        items = data.get("data", []) if isinstance(data, dict) else []
        for item in items:
            sym = item.get("symbol", "")
            code = W2CODE.get(sym)
            if not code or code == "ETF":
                continue
            nodes = item.get("data", {}).get("nodes", []) if isinstance(item.get("data"), dict) else []
            pts = [(n["date"], float(n["last"])) for n in nodes
                   if n.get("last") not in (None, 0)]
            pts.sort(key=lambda x: x[0])
            if pts:
                got.setdefault(code, []).extend(pts)
    for code in got:
        seen = {}
        for d, v in got[code]:
            seen[d] = v
        got[code] = sorted(seen.items())
    return got


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", nargs="*", default=None,
                    help="只重建指定代码；缺省=全部 westock 有点位的指数")
    a = ap.parse_args()
    wpt = collect_westock_points()
    print(f"westock 点位覆盖 {len(wpt)} 只指数")
    os.makedirs(OUT, exist_ok=True)
    n_ok = 0
    for code in sorted(wpt):
        if a.codes and code not in a.codes:
            continue
        pts = wpt[code]
        # 已有 mx 直拉则跳过（mx 为准）
        fout = os.path.join(OUT, f"{code}.json")
        if os.path.exists(fout):
            prev = json.load(open(fout, encoding="utf-8"))
            if prev.get("source", "").startswith("mx-ds-mcp"):
                continue
        daily_dates = [d for d, _ in pts]
        mpb = load_monthly_pb(code)
        if not mpb:
            print(f"  [SKIP] {code}: 无本地月频 PB → 无法反推 BVPS")
            continue
        bvps = interp_bvps_daily(mpb, daily_dates)
        series = []
        for d, close in pts:
            b = bvps.get(d)
            if b and b > 0:
                series.append([d, round(close / b, 4)])
        if len(series) < 100:
            print(f"  [SKIP] {code}: 重建后仅 {len(series)} 点")
            continue
        vals = [v for _, v in series]
        now = vals[-1]
        rec = {
            "index_code": code, "index_name": code,
            "source": "rebuilt-westock+bvps(月频PB反推)",
            "as_of": series[-1][0],
            "n": len(series), "start": series[0][0], "end": series[-1][0],
            "pb": series,
            "p5": round(q(5, vals), 4), "p20": round(q(20, vals), 4),
            "p50": round(q(50, vals), 4), "p80": round(q(80, vals), 4),
            "p95": round(q(95, vals), 4),
            "now": round(now, 4), "pct_now": pct_rank(now, vals),
        }
        with open(fout, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, ensure_ascii=False)
        n_ok += 1
        print(f"  [OK ] {code}: n={len(series)} {series[0][0]}~{series[-1][0]}"
              f" now={now} 分位={rec['pct_now']}% P5={rec['p5']} P50={rec['p50']} P95={rec['p95']} (重建)")
    print(f"\n重建 {n_ok} 只完成 → {OUT}")


if __name__ == "__main__":
    sys.exit(main())
