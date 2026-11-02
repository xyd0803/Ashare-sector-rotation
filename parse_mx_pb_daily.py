#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
parse_mx_pb_daily.py —— 解析 mx-ds-mcp 落盘的 5 年日频 PB 文件 → 汇总 JSON
================================================================================
输入：tool-results 下 mcp-connector-proxy-mx-ds-mcp_*.txt（自动落盘）
输出：data/index_daily_pb/{CODE}.json
        {index_code, index_name, source:"mx-ds-mcp-daily-pb",
         as_of:"2026-08-14", n, start, end,
         pb:[[date, value]...] 升序,   # 5年日频真实PB序列
         p5,p20,p50,p80,p95, now, pct_now}
用法：python parse_mx_pb_daily.py [文件...]   # 不传参则扫描全部落盘文件
"""
from __future__ import annotations
import argparse, glob, json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
OUT = os.path.join(DATA, "index_daily_pb")
SRC_GLOB = [
    os.path.expanduser("~/.workbuddy/projects/*/*/tool-results/mcp-connector-proxy-mx-ds-mcp_*.txt"),
]


def parse_fname(name: str):
    """从 '光伏产业(931151.CSI)(指数)' 提取 (代码, 名称)。"""
    m = re.search(r"\(([A-Za-z0-9\.]+)\)\s*\(指数\)\s*$", name)
    if not m:
        return None, name
    code = m.group(1)
    label = name[: name.rfind("(")]
    return code, label


def q(p: float, vals):
    """分位数（线性插值，与 numpy.percentile 默认一致）。"""
    if not vals:
        return None
    s = sorted(vals)
    n = len(s)
    pos = p * (n - 1) / 100.0
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return s[lo] + (s[hi] - s[lo]) * frac


def pct_rank(v, vals):
    """当前值 v 在序列中的历史分位 = 序列中 ≤v 的比例 ×100。"""
    if not vals:
        return None
    return round(100.0 * sum(1 for x in vals if x <= v) / len(vals), 2)


def window_5y(pts, end):
    """截取最近 5 年窗口（以数据末端为基准），pts 须升序。"""
    import datetime
    end_d = datetime.date.fromisoformat(end)
    start_d = end_d - datetime.timedelta(days=365 * 5 + 2)
    return [p for p in pts if datetime.date.fromisoformat(p[0]) >= start_d]


def parse_file(path: str) -> dict:
    raw = open(path, encoding="utf-8").read()
    idx = raw.find("{")
    if idx < 0:
        return {}
    d = json.loads(raw[idx:])
    out = {}
    for t in d.get("data", []):
        cols = t.get("columns", [])
        dates = [str(c).replace("(日)", "") for c in cols[1:]]
        for it in t.get("items", []):
            code, label = parse_fname(it[0])
            if not code:
                continue
            vals_raw = it[1:]
            pts = []
            for dt, v in zip(dates, vals_raw):
                if v in (None, "-", ""):
                    continue
                s = str(v).replace("倍", "").strip()
                try:
                    f = float(s)
                except ValueError:
                    continue
                pts.append([dt, f])
            if not pts:
                continue
            # mx 返回降序（最新在前）→ 转升序
            pts.sort(key=lambda x: x[0])
            # 统一 5 年窗口
            end = pts[-1][0]
            pts = window_5y(pts, end)
            if len(pts) < 100:
                continue
            vals = [p[1] for p in pts]
            now = vals[-1]
            pct_now = pct_rank(now, vals)
            out[code] = {
                "index_code": code, "index_name": label,
                "source": "mx-ds-mcp-daily-pb",
                "as_of": end,
                "n": len(pts), "start": pts[0][0], "end": end,
                "pb": pts,
                "p5": round(q(5, vals), 4), "p20": round(q(20, vals), 4),
                "p50": round(q(50, vals), 4), "p80": round(q(80, vals), 4),
                "p95": round(q(95, vals), 4),
                "now": round(now, 4), "pct_now": pct_now,
            }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*")
    a = ap.parse_args()
    files = a.files
    if not files:
        files = []
        for g in SRC_GLOB:
            files += glob.glob(g)
        files = sorted(set(files))
    os.makedirs(OUT, exist_ok=True)
    merged = {}
    for f in files:
        try:
            got = parse_file(f)
        except Exception as e:
            print(f"[ERR] {os.path.basename(f)}: {e}")
            continue
        for code, rec in got.items():
            merged[code] = rec
        print(f"[OK ] {os.path.basename(f)}: {len(got)} 指数")
    for code, rec in merged.items():
        with open(os.path.join(OUT, f"{code}.json"), "w", encoding="utf-8") as fh:
            json.dump(rec, fh, ensure_ascii=False)
    print(f"\n共 {len(merged)} 只指数已落盘到 {OUT}")
    for code, rec in sorted(merged.items()):
        print(f"  {code} {rec['index_name']}: n={rec['n']} {rec['start']}~{rec['end']}"
              f" now={rec['now']} 分位={rec['pct_now']}%"
              f" P5={rec['p5']} P20={rec['p20']} P50={rec['p50']} P80={rec['p80']} P95={rec['p95']}")


if __name__ == "__main__":
    sys.exit(main())
