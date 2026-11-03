#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_pb_mxdata.py —— 经 mx-data 直连 mkapi2 拉取指数 5 年日频 PB/PE 序列并落盘
================================================================================
（2026-09-07 P1-⑥ 扩为双序列刷新器：--metric pb|pe，供 PB/PE/BLEND 锚 ETF 补数）
输入：
  --map    data/meta/etf_index_map.json 或单独的 idx_map_refresh.json
           （code -> name 映射；只处理 --codes 指定的子集）
  --codes  逗号分隔的指数代码子集（如 399976.SZ,931151.CSI,...）；缺省全量
  --metric pb（缺省，市净率PB）| pe（市盈率PE(TTM)）
  --end    序列末端日期（默认今天）；查询窗口固定 5 年
输出：
  --metric pb → data/index_daily_pb/{CODE}.json（+ pb_daily_bands.json 同步）
  --metric pe → data/index_daily_pe/{CODE}.json（与 PB 文件同构：series 键仍为 "pb"，
                另加 "metric":"pe" 字段；bands registry 不含 PE，PE 刷新状态走
                data_refresh --status 的 index_daily_pe 扫描）
依赖：mx-data 技能（同目录 mx_data.py），key 自动从 MCP 客户端配置的
      em_api_key 取（可用环境变量 XYD_MCP_JSON 指定配置文件位置）。
注意：妙想直连偶发 0 返回（间歇故障，连 PB 对照组也 0）→ 判通道问题，稍候重试，
      勿误判为标的无数据。
"""
from __future__ import annotations
import argparse, json, os, re, sys, time, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
OUT = DATA / "index_daily_pb"
BANDS = DATA / "meta" / "pb_daily_bands.json"

# 把 mx-data 技能目录加入 sys.path 以便 import mx_data
MXDATA_DIR = HERE.parent / "mx-data"
if str(MXDATA_DIR) not in sys.path:
    sys.path.insert(0, str(MXDATA_DIR))


def _load_key():
    p = Path(os.environ.get("XYD_MCP_JSON")
             or (Path.home() / ".workbuddy" / "mcp.json"))
    mcp = json.loads(p.read_text(encoding="utf-8"))
    for v in mcp.get("mcpServers", {}).values():
        k = v.get("headers", {}).get("em_api_key")
        if k:
            return k
    raise RuntimeError("mcp.json 中未找到 em_api_key")


def q(p, vals):
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
    if not vals:
        return None
    return round(100.0 * sum(1 for x in vals if x <= v) / len(vals), 2)


def parse_table(dto: dict, code: str, name: str, end_override: str, metric: str = "pb"):
    """从 mx-data 返回的 dto['table'] 解析 估值日频序列（PB 或 PE，单位同为「倍」）。"""
    t = dto.get("table") or {}
    head = t.get("headName") or []
    if not head:
        return None
    # 找 PB 数据列：非 headName、且为与 head 等长的字符串列表（含"倍"）
    pb_key = None
    for k, v in t.items():
        if k == "headName":
            continue
        if isinstance(v, list) and len(v) == len(head) and v and isinstance(v[0], str) and "倍" in v[0]:
            pb_key = k
            break
    if pb_key is None:
        return None
    raw_dates = [str(d).replace("(日)", "").strip() for d in head]
    raw_vals = t[pb_key]
    pts = []
    for dt, vv in zip(raw_dates, raw_vals):
        if vv in (None, "-", "", "None"):
            continue
        s = str(vv).replace("倍", "").strip()
        try:
            f = float(s)
        except ValueError:
            continue
        pts.append([dt, f])
    if len(pts) < 100:
        return None
    # headName 降序（最新在前）→ 转升序
    pts.sort(key=lambda x: x[0])
    # 5 年窗口
    end_d = datetime.date.fromisoformat(pts[-1][0])
    start_d = end_d - datetime.timedelta(days=365 * 5 + 2)
    pts = [p for p in pts if datetime.date.fromisoformat(p[0]) >= start_d]
    # B-03（2026-10-25）：交易日过滤。源端 nl 查询写的是「每个交易日」，但实测部分标的
    # 返回的是**日历日**（如 930709.CSI 1301 点 vs 真实 1213 点，多 88 个节假日顺延值）——
    # 非交易日点按重复值计入 pct_rank → 分位/五档失真。统一走 trade_calendar 分市场过滤。
    # 日历构建失败 → 抛 CalendarUnavailable（fail-closed，宁可拒绝落盘也不写未校验序列）。
    import trade_calendar as _tc
    pts, _cal_rep = _tc.filter_pairs(pts, code, verbose=True)
    if _cal_rep.get("reason"):
        print(f"  [WARN] {code} 交易日过滤：{_cal_rep['reason']}", file=sys.stderr)
    if len(pts) < 100:
        return None
    vals = [p[1] for p in pts]
    now = vals[-1]
    end = pts[-1][0]
    return {
        "index_code": code, "index_name": name,
        "source": "mx-ds-mcp-daily-pe" if metric == "pe" else "mx-ds-mcp-daily-pb",
        # 2026-10-07 修（诚实性）：as_of 原取 end_override（=请求的 --end，常为"今天"），
        # 但真实序列末值可能更早（如请求 10-07 而数据只到 09-30）→ 新鲜度门被虚报为 0 天，
        # 掩盖真实缺口。as_of 一律取真实末值 end，查询范围仍由 end_override 控制。
        "as_of": end,
        "n": len(pts), "start": pts[0][0], "end": end,
        "pb": pts,
        "p5": round(q(5, vals), 4), "p20": round(q(20, vals), 4),
        "p50": round(q(50, vals), 4), "p80": round(q(80, vals), 4),
        "p95": round(q(95, vals), 4),
        "now": round(now, 4), "pct_now": pct_rank(now, vals),
        "calendar": _tc.SCHEMA,
        "calendar_market": _cal_rep.get("market"),
        "nontrading_dropped": len(_cal_rep.get("dropped") or []),
    }


def fetch_one(mx, code: str, name: str, end_override: str, delay: float = 1.0, metric: str = "pb"):
    # 2026-10-07 修：main() 一直以 metric= 调用，但本函数未声明该形参 → TypeError，
    # 导致刷新器（PB/PE 全路径）自 PE 改造后 100% 崩溃、缓存长期 stale 而无法自愈。
    label = "市盈率PE(TTM)" if metric == "pe" else "市净率PB"
    qtext = f"{name}({code}) 从2021-08-02到{end_override}每个交易日的{label}"
    try:
        r = mx.query(qtext)
    except Exception as e:
        return None, f"query-err:{type(e).__name__} {str(e)[:120]}"
    # 2026-10-25 修（E-1 健壮性）：妙想直连偶发 None/非 dict 响应（通道瞬时故障），
    # 原代码直接 r.get → AttributeError 崩掉**整个循环**（后续 codes 全部不处理，
    # 一次瞬时故障=整批白跑）。改为 per-code 失败返回，循环继续；并按文件头注释
    # 的既有约定（"偶发 0 返回=通道瞬时故障，稍候重试"）做一次 2s 重试。
    if not isinstance(r, dict):
        time.sleep(2.0)
        try:
            r = mx.query(qtext)
        except Exception as e:
            return None, f"query-err(retry):{type(e).__name__} {str(e)[:120]}"
        if not isinstance(r, dict):
            return None, f"empty/invalid response x2: {type(r).__name__}"
    data = r.get("data", {}).get("data", {})
    dt = data.get("searchDataResultDTO", {})
    lst = dt.get("dataTableDTOList", [])
    if not lst:
        return None, "empty dataTableDTOList"
    rec = parse_table(lst[0], code, name, end_override, metric)
    if rec is None:
        return None, f"parse failed (no {label} column / <100 pts)"
    return rec, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--map", default=str(DATA / "meta" / "etf_index_map.json"))
    ap.add_argument("--codes", default="", help="逗号分隔子集；缺省全量")
    ap.add_argument("--metric", choices=["pb", "pe"], default="pb", help="序列指标：pb=市净率（缺省）/ pe=市盈率PE(TTM)")
    ap.add_argument("--end", default=datetime.date.today().isoformat())
    ap.add_argument("--delay", type=float, default=1.2)
    ap.add_argument("--out", default="", help="输出目录；缺省 data/index_daily_pb 或 data/index_daily_pe")
    a = ap.parse_args()

    default_out = DATA / ("index_daily_pe" if a.metric == "pe" else "index_daily_pb")
    out_dir = Path(a.out) if a.out else default_out

    import importlib
    mxmod = importlib.import_module("mx_data")
    os.environ["MX_APIKEY"] = _load_key()
    mx = mxmod.MXData()

    m = json.loads(Path(a.map).read_text(encoding="utf-8"))
    if isinstance(m, list):  # 兼容 list 型
        m = {x.get("code"): x for x in m}
    # idx_map_refresh.json 是 code->name；etf_index_map.json 是 etf->{index_code,index_name,...}
    want = set(c.strip() for c in a.codes.split(",") if c.strip())
    todo = {}
    for k, v in m.items():
        if isinstance(v, dict) and "index_code" in v:
            idx = v["index_code"]
            if want and idx not in want:
                continue
            todo[idx] = v.get("index_name", "")
        else:
            # code -> name 直接映射
            if want and k not in want:
                continue
            todo[k] = v
    print(f"待刷新指数: {len(todo)} 只")

    out_dir.mkdir(parents=True, exist_ok=True)
    bands = json.loads(BANDS.read_text(encoding="utf-8")) if (BANDS.exists() and a.metric == "pb") else {}
    ok = 0
    for code, name in todo.items():
        rec, err = fetch_one(mx, code, name, a.end, a.delay, metric=a.metric)
        if rec is None:
            print(f"  [FAIL] {code} {name}: {err}")
            continue
        (out_dir / f"{code}.json").write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        if a.metric != "pb":
            print(f"  [OK ] {code} {name}: n={rec['n']} {rec['start']}~{rec['end']} "
                  f"now={rec['now']} 分位={rec['pct_now']}% P5={rec['p5']} P95={rec['p95']}（PE，不入 bands registry）")
            ok += 1
            if a.delay:
                time.sleep(a.delay)
            continue
        # 同步 bands（仅 PB）
        b = bands.get(code, {})
        b.update({
            "end": rec["end"], "n": rec["n"],
            "pct": rec["pct_now"], "now": rec["now"],
            "p5": rec["p5"], "p95": rec["p95"],
            "p20": rec["p20"], "p50": rec["p50"], "p80": rec["p80"],
        })
        bands[code] = b
        print(f"  [OK ] {code} {name}: n={rec['n']} {rec['start']}~{rec['end']} "
              f"now={rec['now']} 分位={rec['pct_now']}% P5={rec['p5']} P95={rec['p95']}")
        ok += 1
        if a.delay:
            time.sleep(a.delay)
    if a.metric == "pb":
        BANDS.write_text(json.dumps(bands, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n完成: {ok}/{len(todo)} 只已落盘 + bands 同步")
    else:
        print(f"\n完成: {ok}/{len(todo)} 只 PE 序列已落盘（{out_dir}）")


if __name__ == "__main__":
    sys.exit(main())
