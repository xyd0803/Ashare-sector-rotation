#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
repair_nontrading.py —— 清除估值缓存中的非交易日点（B-03 存量修复）
================================================================================
为什么需要它：交易日过滤闸门（trade_calendar + ingest/parse 侧过滤）只能防
**将来**的污染，历史上已落盘的污染点必须单独清除，否则 `pct_now` 与五档
（p5/p20/p50/p80/p95）会一直带着偏差 —— 闸门修好了 ≠ 数据干净了。

实测（2026-10-25）：11 个 A 股缓存含 575 个非交易日点，其中 6 个是**整段周末/
节假日日历日序列**（如 930709.CSI 1301 点 vs 真实 1213 点，多 88 个节假日），
其余是 iFinD 增量合并引入的少数点。

安全纪律：
  1. 默认 --dry-run（只报告不改）；--apply 才落盘。
  2. 落盘前**逐文件备份**到 `data/_backup_nontrading_<YYYYMMDD>/<metric>/`。
  3. 重算走 `merge_valuation_gap.build_record`（分位/五档唯一实现），不另写公式。
  4. 该市场日历不可用（样本不足，如 hk/us）→ **跳过并不改**（不拿别市场日历删点）。
  5. 删除数 >20% → 视为异常（可能选错市场日历）→ 拒绝该文件并报错，需人工核。

用法：
    python repair_nontrading.py                 # dry-run 报告
    python repair_nontrading.py --apply         # 执行修复（含备份）
    python repair_nontrading.py --apply --code 931151.CSI ...   # 只修指定标的
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import trade_calendar as tc                      # noqa: E402
from merge_valuation_gap import build_record, save_record  # noqa: E402

DATA = HERE / "data"
MAX_DROP_RATIO = 0.20    # 单文件删除比例上限（超过=疑似市场判错，拒绝执行）


def _load(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def scan(codes: set[str] | None = None) -> list[dict]:
    """只读扫描：返回需要修复的文件清单（含新记录预览）。"""
    tc.load_calendar()
    out = []
    for metric, d in (("pb", DATA / "index_daily_pb"), ("pe", DATA / "index_daily_pe")):
        if not d.exists():
            continue
        for p in sorted(d.glob("*.json")):
            code = p.stem
            if not tc._CODE_RE.match(code):
                continue        # 临时/测试产物
            if codes and code not in codes:
                continue
            rec = _load(p)
            if not rec:
                continue
            series = {}
            for row in rec.get("pb") or []:
                try:
                    series[str(row[0])] = float(row[1])
                except (TypeError, ValueError, IndexError):
                    continue
            if not series:
                continue
            mkt = tc.market_of(code)
            if mkt is None:
                out.append({"metric": metric, "code": code, "path": p,
                            "skip": "市场无法判定", "n": len(series)})
                continue
            rep = tc.filter_dates(list(series), mkt)
            if not rep["available"]:
                out.append({"metric": metric, "code": code, "path": p,
                            "skip": f"{mkt} 日历不可用（样本不足）", "n": len(series)})
                continue
            if not rep["dropped"]:
                continue
            clean = {k: series[k] for k in rep["kept"]}
            item = {"metric": metric, "code": code, "path": p, "market": mkt,
                    "n": len(series), "n_drop": len(rep["dropped"]),
                    "sample": rep["dropped"][:5], "unknown_n": len(rep["unknown"]),
                    "old": rec, "series": clean}
            if len(rep["dropped"]) / len(series) > MAX_DROP_RATIO:
                item["skip"] = (f"删除比例 {len(rep['dropped'])/len(series):.1%} "
                                f">{MAX_DROP_RATIO:.0%} → 疑似市场判错，拒绝执行")
            out.append(item)
    return out


def apply_one(item: dict, backup_root: Path) -> tuple[bool, str]:
    p: Path = item["path"]
    old = item["old"]
    try:
        rec = build_record(item["code"], old.get("index_name") or item["code"],
                           item["series"], old.get("source") or "",
                           metric=item["metric"], old=None)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    # 保留原 source 的语义（不做 incr 标注，因为是清洗而非合并）
    rec["source"] = str(old.get("source") or "")
    rec["nontrading_dropped"] = item["n_drop"]
    rec["nontrading_cleaned_at"] = _dt.date.today().isoformat()
    rec["calendar"] = tc.SCHEMA
    if old.get("index_name"):
        rec["index_name"] = old["index_name"]
    if item["metric"] == "pe":
        rec["metric"] = "pe"
    backup_root.mkdir(parents=True, exist_ok=True)
    tgt = backup_root / item["metric"]
    tgt.mkdir(parents=True, exist_ok=True)
    shutil.copy2(p, tgt / p.name)
    save_record(p.parent, item["code"], rec)
    # PB 同步 bands registry
    if item["metric"] == "pb":
        try:
            from merge_valuation_gap import sync_bands
            sync_bands(item["code"], rec)
        except Exception as e:
            return True, f"已修但 bands 同步失败（{type(e).__name__}）"
    return True, (f"n {item['n']}→{rec['n']} 分位 {old.get('pct_now')}%→{rec['pct_now']}% "
                  f"as_of {old.get('as_of')}→{rec['as_of']}")


def main() -> int:
    ap = argparse.ArgumentParser(description="清除估值缓存中的非交易日点（B-03 存量修复）")
    ap.add_argument("--apply", action="store_true", help="真正落盘（默认 dry-run）")
    ap.add_argument("--code", nargs="*", default=[], help="只处理指定指数代码")
    ap.add_argument("--backup-dir", default="", help="备份根目录（缺省 data/_backup_nontrading_<today>）")
    a = ap.parse_args()

    items = scan(set(a.code) if a.code else None)
    todo = [x for x in items if not x.get("skip")]
    skipped = [x for x in items if x.get("skip")]
    print(f"[扫描] 需修复 {len(todo)} 个文件；跳过 {len(skipped)} 个")
    for s in skipped:
        print(f"  [SKIP] {s['code']:<14} {s['metric']}  {s.get('skip')}")
    for it in todo:
        print(f"  [TODO] {it['code']:<14} {it['metric']}  "
              f"{it['n']} 点 → 剔除 {it['n_drop']} 个非交易日 {it['sample']}"
              + (f"（未知 {it['unknown_n']}）" if it["unknown_n"] else ""))
    if not todo:
        print("[OK ] 无需要修复的缓存（全部已是交易日序列）")
        return 0
    if not a.apply:
        print("\n[dry-run] 未改动任何文件。确认后加 --apply 执行（会自动备份）。")
        return 0

    backup_root = Path(a.backup_dir) if a.backup_dir else (
        DATA / f"_backup_nontrading_{_dt.date.today():%Y%m%d}")
    ok = fail = 0
    for it in todo:
        good, msg = apply_one(it, backup_root)
        print(f"  [{'OK  ' if good else 'FAIL'}] {it['code']:<14} {it['metric']}  {msg}")
        ok += good
        fail += (not good)
    print(f"\n[完成] 修复 {ok} 个，失败 {fail} 个；备份于 {backup_root}")
    return 0 if fail == 0 else 3


if __name__ == "__main__":
    sys.exit(main())
