#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ifind_fetch.py —— 用同花顺 iFinD index_data 补抓缺失指数的「月末收盘价 + 市净率PB」，
写入回测契约文件（与 parse_tdx 输出同构）：
  data/index_monthly/{idx_code}.json   {index_code,index_name,etf_code,pb:{ym:v},close:{ym:v},volume:{}}
  data/raw/mx/{idx_code}.json          {index_code,index_name,source:"ifind-index-data",pb:[[date,pb],...],close:[[date,close],...]}

用法：
  python ifind_fetch.py            # 抓所有 check_cov 判缺失的指数
  python ifind_fetch.py --codes 931066 931151   # 只抓指定
  python ifind_fetch.py --tag <批次标识>         # 配额归因（默认 sds_backfill）

配额纪律（2026-10-25 D-3）：本脚本经共享 call.py 取数，该 call() 内置 iFinD
预算硬闸 + **require_tag（无 tag 拒绝执行）**。取数前必须 set_attribution 归因
（此前从未设置 → 一跑即被 fail-closed 拒，或被迫设全局 env 掩盖归因缺失）。
"""
import json, os, re, sys, time, warnings
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
        "[ifind_fetch] 找不到 iFinD 的 call 模块。请设置 XYD_IFIND_SKILL_DIR "
        "指向提供 call.py 的技能目录。") from exc

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
MAPF = os.path.join(DATA, "meta", "etf_index_map.json")
IDX_MONTHLY = os.path.join(DATA, "index_monthly")
RAW_MX = os.path.join(DATA, "raw", "mx")
os.makedirs(IDX_MONTHLY, exist_ok=True)
os.makedirs(RAW_MX, exist_ok=True)

END = "2026年8月14日"
START = "2019年1月1日"


def load_map():
    mp = json.load(open(MAPF, encoding="utf-8"))
    return mp


def covered_codes():
    """与 check_cov 同口径：返回已覆盖的 index_code 集合"""
    targets = sorted({v["index_code"] for v in load_map().values()})
    cov = set()
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
        if has_close and has_pb and cm >= 2 and cr >= 2 and dp >= 20:
            cov.add(code)
    return cov


def fetch_pb_close(query):
    """调用 ifind index_data，返回 [(date_YYYY-MM-DD, close, pb), ...] 或 None"""
    r = call("index", "index_data", {"query": query})
    try:
        text = r["data"]["result"]["content"][0]["text"]
    except Exception:
        return None
    try:
        obj = json.loads(text)
        inner = json.loads(obj["data"])
        answer = inner.get("answer", "")
    except Exception:
        return None
    if not answer or "没有" in answer or "无数据" in answer:
        return None
    rows = []
    # |证券代码|证券简称|日期|月收盘价|市净率（PB）|
    for line in answer.splitlines():
        m = re.match(r"\|\s*([0-9A-Za-z.]+)\s*\|\s*([^|]+?)\s*\|\s*(\d{8})\s*\|\s*([\d.]+)\s*\|\s*([\d.]+)\s*\|", line.strip())
        if not m:
            continue
        code, name, d8, close, pb = m.group(1), m.group(2).strip(), m.group(3), m.group(4), m.group(5)
        d = f"{d8[:4]}-{d8[4:6]}-{d8[6:8]}"
        rows.append((d, float(close), float(pb)))
    return rows or None


def write_out(idx_code, idx_name, etf_code, rows):
    """rows: [(date, close, pb)] 升序写入 index_monthly + raw/mx（与既有 merge）"""
    # ---- index_monthly ----
    imp = os.path.join(IDX_MONTHLY, f"{idx_code}.json")
    rec = {"index_code": idx_code, "index_name": idx_name, "etf_code": etf_code,
           "pb": {}, "close": {}, "volume": {}}
    if os.path.exists(imp):
        try:
            old = json.load(open(imp, encoding="utf-8"))
            for k in ("index_name", "etf_code", "pb", "close", "volume"):
                rec[k] = old.get(k, rec[k])
        except Exception:
            pass
    pb_by_ym, close_by_ym = {}, {}
    for d, c, p in rows:
        pb_by_ym.setdefault(d[:7], []).append((d, p))
        close_by_ym.setdefault(d[:7], []).append((d, c))
    for ym, lst in pb_by_ym.items():
        rec["pb"][ym] = sorted(lst)[-1][1]
    for ym, lst in close_by_ym.items():
        rec["close"][ym] = sorted(lst)[-1][1]
    with open(imp, "w", encoding="utf-8") as fh:
        json.dump(rec, fh, ensure_ascii=False, indent=1)

    # ---- raw/mx ----
    rmp = os.path.join(RAW_MX, f"{idx_code}.json")
    raw = {"index_code": idx_code, "index_name": idx_name,
           "source": "ifind-index-data", "pb": [], "close": []}
    if os.path.exists(rmp):
        try:
            old = json.load(open(rmp, encoding="utf-8"))
            raw["index_name"] = old.get("index_name", idx_name)
            raw["pb"] = list(old.get("pb") or [])
            raw["close"] = list(old.get("close") or [])
        except Exception:
            pass
    pbd = {d: v for d, v in raw["pb"]}
    cd = {d: v for d, v in raw["close"]}
    for d, c, p in rows:
        pbd[d] = p
        cd[d] = c
    raw["pb"] = sorted(pbd.items())
    raw["close"] = sorted(cd.items())
    with open(rmp, "w", encoding="utf-8") as fh:
        json.dump(raw, fh, ensure_ascii=False)
    return len(raw["pb"]), len(raw["close"]), len(rec["pb"])


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", nargs="*", default=[], help="只抓指定 index_code")
    ap.add_argument("--sleep", type=float, default=0.6, help="每次调用间隔秒（并发限流）")
    ap.add_argument("--tag", default="sds_backfill",
                    help="iFinD 配额归因 tag（默认 sds_backfill；call.py require_tag 必填）")
    a = ap.parse_args()
    # 2026-10-25 D-3：配额归因（call.py 的 require_tag 会拒绝无 tag 调用）
    set_attribution("ifind_fetch", a.tag)
    print(f"[预算] 今日 iFinD 用量将记入 tag=#{a.tag}（日上限 80·超限 call() 自动 fail-closed）")

    mp = load_map()
    if a.codes:
        targets = [re.split(r"[.\s]", c)[0] for c in a.codes]
    else:
        targets = sorted({v["index_code"] for v in mp.values()} - covered_codes())
        targets = [re.split(r"[.\s]", t)[0] for t in targets if t != "AU9999.SGE"]  # 商品排除

    # code -> (etf_code, info)
    by_code = {}
    for etf_code, info in mp.items():
        ic = info["index_code"]
        base = re.split(r"[.\s]", ic)[0]
        by_code.setdefault(base, (etf_code, info))

    print(f"待抓指数 {len(targets)} 个: {targets}")
    ok, fail = [], []
    for i, base in enumerate(targets):
        etf_code, info = by_code.get(base, (None, {"index_name": base}))
        idx_code = info["index_code"]
        idx_name = info.get("index_name", base)
        query = f"{idx_name}指数 {base} 从{START} 到 {END} 每月末的收盘价和市净率PB"
        try:
            rows = fetch_pb_close(query)
        except Exception as e:
            rows = None
            print(f"  [{i+1}/{len(targets)}] {base:10} ERR {type(e).__name__}: {e}")
        if rows:
            n1, n2, n3 = write_out(idx_code, idx_name, etf_code, rows)
            ok.append((base, idx_name, n1, n2, n3))
            print(f"  [{i+1}/{len(targets)}] {base:10} OK  rows={len(rows):4} raw_pb={n1} raw_close={n2} mo_pb={n3}")
        else:
            fail.append((base, idx_name))
            print(f"  [{i+1}/{len(targets)}] {base:10} FAIL(无数据)")
        time.sleep(a.sleep)

    print("\n==== 结果 ====")
    print(f"成功 {len(ok)} / {len(targets)}")
    for b, n, n1, n2, n3 in ok:
        print(f"  OK {b:10} {n} raw_pb={n1} mo_pb={n3}")
    if fail:
        print(f"失败/无数据 {len(fail)}:")
        for b, n in fail:
            print(f"  FAIL {b:10} {n}")


if __name__ == "__main__":
    main()
