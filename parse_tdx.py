#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
parse_tdx.py —— 把 tdx `tdx_security_deep_info` (f9_index_historical_pe_pb) 落地 TXT 文件
解析为回测契约文件：

  data/index_monthly/{index_code}.json   {index_code,index_name,etf_code,
                                           pb:{ym:v}, close:{ym:v}}   (volume 留空)
  data/raw/mx/{index_code}.json          {index_code,index_name,source,
                                           pb:[[date,pb],...], close:[[date,close],...]}

关键改进（支持日频切块合并）：
  · 多文件按 index_code 累加（同一指数可能有 日频2019-2021 / 日频2023-2026 / 月频2019-2026 多块）
  · 同一交易日(date)若被多块覆盖，优先保留「行数更多」的那块的值（日频块 727 行 > 月频块 92 行，
    从而日频覆盖的月份用日频，其余月份用月频兜底）
  · 仅解析命中 etf_index_map 的指数；个股盈利文件因不在宇宙内被自动跳过

用法：
  python parse_tdx.py --all-projects             # 扫描所有 ~/.workbuddy/projects 下 tdx_security_deep_info 文件
  python parse_tdx.py --src DIR1 DIR2 --file F1  # 指定目录/文件
"""
import argparse, json, os, re, glob

HERE = os.path.dirname(os.path.abspath(__file__))
MAP = os.path.join(HERE, "data", "meta", "etf_index_map.json")
IDX_MONTHLY = os.path.join(HERE, "data", "index_monthly")
RAW_MX = os.path.join(HERE, "data", "raw", "mx")
os.makedirs(IDX_MONTHLY, exist_ok=True)
os.makedirs(RAW_MX, exist_ok=True)


def load_map():
    mp = json.load(open(MAP, encoding="utf-8"))
    by_base = {}
    for etf_code, info in mp.items():
        ic = info.get("index_code", "")
        base = re.split(r"[.\s]", ic)[0]
        by_base[base] = (etf_code, info)
    return mp, by_base


def base_of_query(hdr):
    """从查询头提取指数代码（兼容 名称在前/代码在前 两种语序）。

    例：
      查询 CS稀金属指数 930632 从2019年1月1日 ...  -> 930632
      查询 399975.SZ 证券公司 从2019年1月1日 ...      -> 399975
    """
    if not hdr:
        return None
    # 先匹配带后缀的代码，如 000685.SH / 399975.SZ
    m = re.search(r"(\d{4,6}\.[A-Za-z]{2,4})", hdr)
    if m:
        return re.split(r"[.\s]", m.group(1))[0]
    # 退化：裸 6 位代码，如 930632
    m = re.search(r"(\d{4,6})", hdr)
    if m:
        return m.group(1)
    return None


def parse_file_into(path, by_base, acc):
    """把单个 tdx 落地文件解析进累加器 acc[base] = {etf_code,info,pb:{date:(v,rows)},close:{date:(v,rows)}}"""
    txt = open(path, encoding="utf-8", errors="replace").read()
    hdr = ""
    for line in txt.splitlines():
        if line.startswith("查询:"):
            hdr = line
            break
    base = base_of_query(hdr)
    if not base or base not in by_base:
        return False
    etf_code, info = by_base[base]
    m = re.search(r"详细结果:\s*(\{.*\})\s*$", txt, re.S)
    if not m:
        return False
    try:
        obj = json.loads(m.group(1))
    except Exception:
        return False
    rss = (obj.get("data", {}) or {}).get("result_sets", []) or []
    rows = None
    for rs in rss:
        if rs.get("id") == "valuation_rows":
            rows = rs.get("rows")
            break
    if not rows:
        return False
    nrows = len(rows)
    slot = acc.setdefault(base, {"etf_code": etf_code, "info": info,
                                 "pb": {}, "close": {}})
    cnt = 0
    for r in rows:
        d = r.get("日期")
        if not d:
            continue
        c = r.get("收盘价")
        pbv = r.get("市净率PB(MRQ)")
        if pbv is not None:
            v = round(float(pbv), 4)
            cur = slot["pb"].get(d)
            if cur is None or nrows > cur[1]:
                slot["pb"][d] = (v, nrows)
            cnt += 1
        elif d in slot["pb"]:
            pass
        if c is not None:
            v = round(float(c), 4)
            cur = slot["close"].get(d)
            if cur is None or nrows > cur[1]:
                slot["close"][d] = (v, nrows)
            cnt += 1
    return cnt > 0


def _collect(src_dirs, explicit_files, all_projects):
    files = list(explicit_files)
    dirs = list(src_dirs)
    if all_projects:
        dirs.append(os.path.expanduser("~/.workbuddy/projects"))
    def _want(fn):
        # 兼容两种落地命名：旧会话 tdx_security_deep_info-* 与本会话 chatcmpl-tool-*
        return fn.endswith(".txt") and ("tdx_security_deep_info" in fn or "chatcmpl-tool" in fn)
    for d in dirs:
        if os.path.isfile(d) and _want(d):
            files.append(d)
            continue
        for root, _, fns in os.walk(d):
            for fn in fns:
                if _want(fn):
                    files.append(os.path.join(root, fn))
    seen = set()
    return [f for f in files if not (f in seen or seen.add(f))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", nargs="*", default=[], help="dirs containing deep_info txt")
    ap.add_argument("--file", nargs="*", default=[], help="explicit txt files")
    ap.add_argument("--all-projects", action="store_true")
    ap.add_argument("--only-missing", action="store_true",
                    help="只处理当前 index_monthly 仍缺失 close+pb 的指数")
    a = ap.parse_args()
    mp, by_base = load_map()
    files = _collect(a.src, a.file, a.all_projects)
    print(f"扫描到 tdx_security_deep_info 文件 {len(files)} 个; 宇宙 base={len(by_base)}")

    acc = {}
    for f in files:
        try:
            parse_file_into(f, by_base, acc)
        except Exception as e:
            print("  ERR", os.path.basename(f), repr(e))

    # 写出（与既有文件 MERGE，避免覆盖 mx 已物化的更完整数据）
    written = []
    for base, slot in acc.items():
        etf_code, info = slot["etf_code"], slot["info"]
        idx_code = info["index_code"]
        idx_name = info.get("index_name", "")
        pb_d = {d: v for d, (v, _) in slot["pb"].items()}
        close_d = {d: v for d, (v, _) in slot["close"].items()}
        if not pb_d or not close_d:
            continue
        # 月度重采样（取该月最大交易日的值 = 月末）
        pb_by_ym, close_by_ym = {}, {}
        for d, v in pb_d.items():
            pb_by_ym.setdefault(d[:7], []).append((d, v))
        for d, v in close_d.items():
            close_by_ym.setdefault(d[:7], []).append((d, v))

        # ---- index_monthly 合并 ----
        imp = os.path.join(IDX_MONTHLY, f"{idx_code}.json")
        rec = {"index_code": idx_code, "index_name": idx_name, "etf_code": etf_code,
               "pb": {}, "close": {}, "volume": {}}
        if os.path.exists(imp):
            try:
                old = json.load(open(imp, encoding="utf-8"))
                rec["index_name"] = old.get("index_name", idx_name)
                rec["etf_code"] = old.get("etf_code", etf_code)
                rec["pb"] = dict(old.get("pb") or {})
                rec["close"] = dict(old.get("close") or {})
                rec["volume"] = dict(old.get("volume") or {})
            except Exception:
                pass
        for ym, lst in pb_by_ym.items():
            rec["pb"][ym] = sorted(lst)[-1][1]
        for ym, lst in close_by_ym.items():
            rec["close"][ym] = sorted(lst)[-1][1]
        with open(imp, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, ensure_ascii=False, indent=1)

        # ---- raw/mx 合并（按交易日 union，新值优先）----
        rmp = os.path.join(RAW_MX, f"{idx_code}.json")
        raw = {"index_code": idx_code, "index_name": idx_name,
               "source": "tdx-f9-daily", "pb": [], "close": []}
        if os.path.exists(rmp):
            try:
                old = json.load(open(rmp, encoding="utf-8"))
                raw["index_name"] = old.get("index_name", idx_name)
                raw["source"] = old.get("source", "tdx-f9-daily")
                raw["pb"] = list(old.get("pb") or [])
                raw["close"] = list(old.get("close") or [])
            except Exception:
                pass
        pbd = {d: v for d, v in raw["pb"]}
        cd = {d: v for d, v in raw["close"]}
        pbd.update(pb_d)
        cd.update(close_d)
        raw["pb"] = sorted(pbd.items())
        raw["close"] = sorted(cd.items())
        with open(rmp, "w", encoding="utf-8") as fh:
            json.dump(raw, fh, ensure_ascii=False)
        written.append((etf_code, idx_code, idx_name, len(pbd), len(cd),
                        len(rec["pb"]), len(rec["close"])))
        print(f"  OK {etf_code:8} {idx_code:12} {idx_name[:12]:12} "
              f"日频pb={len(pbd)} 日频close={len(cd)} 月度pb={len(rec['pb'])}")

    # 覆盖报告
    have = {w[1] for w in written}
    miss_codes = [(c, v["index_code"], v.get("index_name", ""))
                  for c, v in mp.items() if v["index_code"] not in have]
    print(f"\n本次写出 {len(written)} 个指数; 宇宙仍缺失 close+pb: {len(miss_codes)}")
    for c, ic, nm in miss_codes:
        print(f"   MISSING {c:8} {ic:12} {nm}")


if __name__ == "__main__":
    main()
