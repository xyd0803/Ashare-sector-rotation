#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
_test_b03_trade_day_20261025.py —— B-03「交易日机械过滤」注入式回归
================================================================================
验证的不是「函数能跑」，而是**闸门真的会拦**（对齐纪律：加完保护机制必须做注入测试）：
  T1 日历判定：周末/节假日 → 非交易日；区间外 → 未知（不装懂）
  T2 市场隔离：港股/美股日历样本不足时**不得**套用 A 股日历删点（否则删真实交易日）
  T3 merge_series 纵深防御：周末入参 → MergeError（fail-closed）
  T4 ingest_ifind_valuation 端到端：含周末/节假日的 iFinD 表 → 落盘序列 0 非交易日，
     且记录 nontrading_dropped；数据点不足则拒绝落盘
  T5 fetch_pb_mxdata.parse_table：源端给日历日 → 落盘记录不含周末
  T6 存量审计：现有缓存无（A 股）非交易日点

用法：python _test_b03_trade_day_20261025.py
      python _test_b03_trade_day_20261025.py --require-calendar
          （日历为空时报错而非跳过；本地有 data/index_daily_pb 时用）
"""
from __future__ import annotations

import datetime as dt
import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import trade_calendar as tc                       # noqa: E402
from merge_valuation_gap import MergeError, merge_series  # noqa: E402

PB = HERE / "data" / "index_daily_pb"
BANDS = HERE / "data" / "meta" / "pb_daily_bands.json"
TMP = HERE / ".rt_tmp" / "b03"
TEST_CODE = "T9999.CSI"          # 合成标的（测试后必删）

# 日历为空时默认跳过（仓库不含数据）；本地开发用 --require-calendar 强制要求。
REQUIRE_CALENDAR = "--require-calendar" in sys.argv

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def _md_table(rows: list[tuple[str, float]]) -> str:
    """构造 iFinD 风格 markdown 表（证券代码|证券简称|日期|市净率（PB）|市盈率（PE，TTM））。"""
    out = ["|证券代码|证券简称|日期|市净率（PB）|市盈率（PE，TTM）|", "|---|---|---|---|---|"]
    for d, v in rows:
        out.append(f"|{TEST_CODE}|测试指数|{d}|{v}|{v * 10}|")
    return "\n".join(out) + "\n"


def main() -> int:
    cal = tc.load_calendar()
    cn = (cal.get("markets") or {}).get("cn") or {}
    days = cn["days"]

    # 交易日历由 data/index_daily_pb 的多序列投票推导（见 trade_calendar.py）。
    # 公开仓库按设计不含任何数据，因此干净克隆里日历为空 —— 此时无法判定交易日。
    # 按本项目的 fail-closed 原则，缺日历**不能**退化成「只有周末休市」：
    # 那会把国庆、春节当成交易日，比直接拒答更危险。
    # 故此处显式跳过，并用 --require-calendar 让本地开发强制要求日历可用。
    if not days:
        if REQUIRE_CALENDAR:
            print("  [FAIL] 要求日历可用，但 cn 日历为空（先构建 data/index_daily_pb）")
            return 1
        print("== 0. 前置：cn 日历为空 ==")
        print("  [SKIP] 交易日历需由本地 data/index_daily_pb 投票推导；仓库不含数据。")
        print("         本地验证请加 --require-calendar。")
        print("\n跳过 B-03（0 项判定）：日历不可用，无法评估。")
        return 0

    print(f"== 0. 前置：cn 日历 {cn['n']} 个交易日（{cn['start']}~{cn['end']}）==")
    check("cn 日历可用", bool(cn.get("available")))

    # ---------------- T1 日历判定 ----------------
    print("== T1 日历判定 ==")
    check("周六 → 非交易日", tc.is_trading_day("2026-10-03", "cn") is False)
    check("国庆节假日 → 非交易日", tc.is_trading_day("2026-10-01", "cn") is False)
    check("真实交易日 → 交易日", tc.is_trading_day(days[-1], "cn") is True)
    check("区间外 → None（不装懂）", tc.is_trading_day("2001-01-05", "cn") is None)

    # ---------------- T2 市场隔离 ----------------
    print("== T2 市场隔离（不得跨市场套用日历）==")
    hk_pairs = [["2026-10-03", 1.0], ["2026-10-05", 1.1]]
    kept, rep = tc.filter_pairs(hk_pairs, "HSTECH.HI")
    check("hk 日历不可用 → 原样保留（不删港股点）", len(kept) == 2 and not rep["available"],
          rep["reason"])
    kept2, rep2 = tc.filter_pairs([["2026-10-01", 1.0]], "SPX.GI")
    check("us 日历不可用 → 原样保留", len(kept2) == 1 and not rep2["available"], rep2["reason"])
    kept3, rep3 = tc.filter_pairs([["2026-10-01", 1.0], [days[-1], 1.0]], "931151.CSI")
    check("cn 可判定 → 剔除节假日、保留交易日",
          [p[0] for p in kept3] == [days[-1]] and rep3["dropped"] == ["2026-10-01"])

    # ---------------- T3 merge_series 纵深防御 ----------------
    print("== T3 merge_series 周末 fail-closed ==")
    for bad in ("2026-10-03", "2026-10-04"):
        try:
            merge_series(None, {bad: 2.0})
            check(f"周末 {bad} 被拒绝", False)
        except MergeError as e:
            check(f"周末 {bad} 被拒绝", "周末" in str(e) or "非交易日" in str(e))
    try:
        r = merge_series(None, {days[-1]: 2.0, days[-2]: 2.1})
        check("交易日入参正常合并", r.filled == 2)
    except MergeError as e:
        check("交易日入参正常合并", False, str(e))

    # ---------------- T4 ingest 端到端 ----------------
    print("== T4 ingest_ifind_valuation 端到端（含周末/节假日表）==")
    TMP.mkdir(parents=True, exist_ok=True)
    bands_backup = BANDS.read_text(encoding="utf-8") if BANDS.exists() else ""
    try:
        # 取最近 200 个真实交易日 + 注入 5 个非交易日（周末/国庆）
        real = days[-200:]
        dirty = real + ["2026-10-01", "2026-10-03", "2026-10-04", "2026-09-12", "2026-10-17"]
        dirty.sort()
        rows = [(d, round(2.0 + 0.001 * i, 4)) for i, d in enumerate(dirty)]
        tbl = TMP / "ifind_dirty.md"
        tbl.write_text(_md_table(rows), encoding="utf-8")
        out = subprocess.run([sys.executable, "ingest_ifind_valuation.py",
                              "--table", str(tbl), "--code", TEST_CODE,
                              "--name", "测试指数", "--no-charge"],
                             cwd=HERE, capture_output=True, text=True, encoding="utf-8")
        ok = out.returncode == 0
        check("ingest 退出码 0", ok, (out.stderr or out.stdout)[-200:] if not ok else "")
        wrote = PB / f"{TEST_CODE}.json"
        if wrote.exists():
            rec = json.loads(wrote.read_text(encoding="utf-8"))
            dates = [d for d, _ in rec["pb"]]
            bad = [d for d in dates if tc.is_trading_day(d, "cn") is False]
            check("落盘序列 0 非交易日点", not bad, f"残留 {bad[:5]}")
            check("nontrading_dropped 已记录", rec.get("nontrading_dropped") == 5,
                  f"记录值={rec.get('nontrading_dropped')}")
            check("calendar 版本已标注", rec.get("calendar") == tc.SCHEMA, str(rec.get("calendar")))
            check("as_of 为交易日", tc.is_trading_day(rec["as_of"], "cn") is True, rec["as_of"])
            check("n 只含交易日", rec["n"] == len(real), f"{rec['n']} vs {len(real)}")
        else:
            check("落盘文件存在", False, "未生成缓存")
        # 数据点不足 → 拒绝落盘
        few = TMP / "ifind_few.md"
        few.write_text(_md_table([(d, 2.0) for d in days[-30:]]), encoding="utf-8")
        out2 = subprocess.run([sys.executable, "ingest_ifind_valuation.py",
                               "--table", str(few), "--code", "T9998.CSI",
                               "--name", "测试指数2", "--no-charge"],
                              cwd=HERE, capture_output=True, text=True, encoding="utf-8")
        check("点数不足 → fail-closed 拒绝落盘",
              out2.returncode != 0 and not (PB / "T9998.CSI.json").exists(),
              f"rc={out2.returncode}")
    finally:
        for f in (PB / f"{TEST_CODE}.json", PB / "T9998.CSI.json"):
            if f.exists():
                f.unlink()
        if bands_backup:
            BANDS.write_text(bands_backup, encoding="utf-8")

    # ---------------- T5 parse_table 过滤 ----------------
    print("== T5 fetch_pb_mxdata.parse_table（日历日源）==")
    import fetch_pb_mxdata as fpm
    head = [f"{d}(日)" for d in ["2026-10-03", "2026-10-01", days[-1], days[-2]]]
    dto = {"table": {"headName": head, "市净率PB": ["2.0倍", "2.1倍", "2.2倍", "2.3倍"]}}
    rec = fpm.parse_table(dto, TEST_CODE, "测试指数", days[-1], "pb")
    check("parse_table 参数不足返回 None（<100 点）", rec is None)
    big_head = [f"{d}(日)" for d in days[-150:]] + ["2026-10-03(日)", "2026-10-01(日)"]
    big_vals = [f"{2.0 + 0.001 * i}倍" for i in range(len(big_head))]
    dto2 = {"table": {"headName": big_head, "市净率PB": big_vals}}
    rec2 = fpm.parse_table(dto2, TEST_CODE, "测试指数", days[-1], "pb")
    if rec2:
        bad = [d for d, _ in rec2["pb"] if tc.is_trading_day(d, "cn") is False]
        check("parse_table 输出 0 非交易日", not bad, f"残留 {bad[:5]}")
        check("parse_table 记录 calendar/nontrading_dropped",
              rec2.get("calendar") == tc.SCHEMA and rec2.get("nontrading_dropped") == 2,
              f"cal={rec2.get('calendar')} drop={rec2.get('nontrading_dropped')}")
    else:
        check("parse_table 正常返回记录", False)

    # ---------------- T6 存量审计 ----------------
    print("== T6 存量缓存审计（A 股应清零）==")
    bad_all = tc.audit_caches()
    check("存量缓存 0 个非交易日点", not bad_all,
          "；".join(f"{k}:{v['n_dropped']}" for k, v in list(bad_all.items())[:5]))

    shutil.rmtree(TMP, ignore_errors=True)
    print("\n" + (f"✅ B-03 回归全过（{len(PASS)} 项）" if not FAIL
                  else f"❌ 失败 {len(FAIL)} 项: {FAIL}"))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
