#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ifind_fix_pb.py —— 用 iFinD 真实 PB 序列替换/补齐 index_daily_pb（禁止线性插入）
================================================================================
背景：rebuild_pb_daily.py 的 BVPS 线性插值重建对部分指数不可靠（930632 +58%、
931994 -15.5% 偏差），且 3 处 etf_index_map 映射错误（513310/159611/512890）导致
H30199/H30269/931790 重建到错误指数。本脚本用 iFinD index_data 返回的
【真实观测点】（近 19 天日频 + 月度采样，均为数据源直给值，非插值）重写这些
指数的五档参考线。

用法：python ifind_fix_pb.py --codes 930632.CSI,931994.CSI ...
      python ifind_fix_pb.py --all        # 处理预设清单
      python ifind_fix_pb.py --all --tag <批次标识>   # 配额归因（默认 sds_backfill）

配额纪律（2026-10-25 D-3）：本脚本经共享 call.py 取数，该 call() 内置 iFinD
预算硬闸 + **require_tag（无 tag 拒绝执行）**。取数前必须 set_attribution 归因。
"""
from __future__ import annotations
import argparse, json, os, re, sys, warnings
from datetime import datetime

warnings.filterwarnings("ignore")
def _ifind_call_dir():
    """定位提供 call() 的 iFinD 技能目录：环境变量优先，否则自动发现。

    不写死任何具体目录名。候选必须同时具备 call.py 且其中定义 set_attribution，
    避免误命中同名但无关的模块。
    """
    explicit = os.environ.get("XYD_IFIND_SKILL_DIR")
    if explicit:
        return explicit
    root = os.path.expanduser(
        os.environ.get("XYD_SKILLS_ROOT") or os.path.join("~", ".workbuddy", "skills"))
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return ""
    for name in names:
        p = os.path.join(root, name, "call.py")
        if not os.path.isfile(p):
            continue
        try:
            with open(p, encoding="utf-8", errors="ignore") as fh:
                if "def set_attribution" in fh.read():
                    return os.path.join(root, name)
        except OSError:
            continue
    return ""

_CALL_DIR = _ifind_call_dir()
if _CALL_DIR:
    sys.path.insert(0, _CALL_DIR)
try:
    from call import call, set_attribution
except ImportError as exc:
    raise SystemExit(
        "[ifind_fix_pb] 找不到 iFinD 的 call 模块。请设置 XYD_IFIND_SKILL_DIR "
        "指向提供 call.py 的技能目录。") from exc

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data", "index_daily_pb")

# 预设清单：偏差大的重建类 + 映射修正后 + 缺口污染类（iFinD 有真实 PB 的）
FIX_LIST = [
    "930632.CSI",   # 稀有金属 重建+58%偏差
    "931994.CSI",   # 电网设备 重建-15.5%偏差
    "H30199.CSI",   # 电力（映射修正后，原误作中韩半导体）
    "H30269.CSI",   # 红利低波（映射修正后，原误作电力）
    "931790.CSI",   # 中韩半导体（映射修正后，原 H30199）
    "980092.SZ",    # 自由现金流（污染，之前无干净源）
    "H30202.CSI",   # 软件（污染，之前无干净源）
    "HSHYLV.HK",    # 港股通高股息低波（缺口）
]
NAMES = {
    "930632.CSI": "稀有金属", "931994.CSI": "电网设备", "H30199.CSI": "电力指数",
    "H30269.CSI": "红利低波", "931790.CSI": "中韩半导体", "980092.SZ": "自由现金流",
    "H30202.CSI": "软件指数", "HSHYLV.HK": "港股通高股息低波",
}


def q(p, vals):
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


def fetch_ifind_pb(code: str, name: str):
    """iFinD 拉该指数近5年 PB 序列 → [[date, pb]...] 升序（真实值）。"""
    qs = f"{code} {name}指数2021年8月至2026年8月每个交易日的市净率PB全部数据"
    r = call("index", "index_data", {"query": qs})
    txt = r["data"]["result"]["content"][0]["text"]
    inner = json.loads(txt)
    data = json.loads(inner["data"])
    ans = data.get("answer", "")
    rows = re.findall(r"\|([0-9]{8})\|([0-9.]+)\|", ans)
    pts = []
    for d, v in rows:
        try:
            fv = float(v)
        except ValueError:
            continue
        dt = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        pts.append([dt, fv])
    pts.sort(key=lambda x: x[0])
    # 去重
    seen = {}
    for d, v in pts:
        seen[d] = v
    return sorted(seen.items())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", default=None, help="逗号分隔，覆盖 FIX_LIST")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--tag", default="sds_backfill",
                    help="iFinD 配额归因 tag（默认 sds_backfill；call.py require_tag 必填）")
    a = ap.parse_args()
    # 2026-10-25 D-3：配额归因（call.py 的 require_tag 会拒绝无 tag 调用）
    set_attribution("ifind_fix_pb", a.tag)
    print(f"[预算] 今日 iFinD 用量将记入 tag=#{a.tag}（日上限 80·超限 call() 自动 fail-closed）")
    codes = a.codes.split(",") if a.codes else (FIX_LIST if a.all else [])
    if not codes:
        print("用法: --all 或 --codes a,b,c")
        return 2
    os.makedirs(OUT, exist_ok=True)
    for code in codes:
        name = NAMES.get(code, code.split(".")[0])
        try:
            pts = fetch_ifind_pb(code, name)
        except Exception as e:
            print(f"[ERR] {code}: {e}")
            continue
        if len(pts) < 20:
            print(f"[SKIP] {code}: iFinD 仅 {len(pts)} 点")
            continue
        vals = [v for _, v in pts]
        now = vals[-1]
        rec = {
            "index_code": code, "index_name": name,
            "source": "ifind-real(近19天日频+月度采样,非插值)",
            "as_of": pts[-1][0], "n": len(pts),
            "start": pts[0][0], "end": pts[-1][0], "pb": pts,
            "p5": round(q(5, vals), 4), "p20": round(q(20, vals), 4),
            "p50": round(q(50, vals), 4), "p80": round(q(80, vals), 4),
            "p95": round(q(95, vals), 4),
            "now": round(now, 4), "pct_now": pct_rank(now, vals),
            "note": "iFinD 真实观测点重建（近19日频+月度），替代不可靠的 BVPS 插值重建",
        }
        with open(os.path.join(OUT, f"{code}.json"), "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False)
        print(f"[OK ] {code} {name}: n={len(pts)} {pts[0][0]}~{pts[-1][0]}"
              f" now={now} 分位={rec['pct_now']}% P5={rec['p5']} P50={rec['p50']} P95={rec['p95']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
