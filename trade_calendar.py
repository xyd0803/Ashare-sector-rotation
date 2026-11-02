#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
trade_calendar.py —— 分市场交易日历（离线·机械·fail-honest）
================================================================================
背景（B-03，2026-10-25）：iFinD 指数估值接口返回的是**日历日**序列（含周末/
节假日顺延值，实测非交易日占比 22%），而估值缓存与分位计算必须建立在**交易日**
序列上。此前靠人工在合并前过滤 —— 属于「手抬不变量」：换个人/换一轮就会漏，
漏了不会报错，只会让 `pct_now`（→五档→EV）被节假日重复值静默污染。

**实证（2026-10-25 扫描存量缓存）**：850 个非交易日点分布在 15 个缓存里。其中
A 股 6 个（`+ifind_incr_calib` 引入，如 931151.CSI 的 2026-10-03/04…），另外
9 个是**港股/美股指数**（HSTECH.HI / SPX.GI 等 1301 点 vs A 股 1213 点）——
它们的「多余点」不是污染，而是**别的市场的交易日**。这条差异是本模块设计的
核心约束：**日历必须按市场分开，任何跨市场套用都会删掉真实数据。**

设计（四条硬要求）：
  1. **离线**：不依赖网络/MCP —— 日历由本地**已缓存的日频估值序列并集**推导
     （同一市场只有一套交易日，任一日频序列都是它的样本）。
  2. **分市场**：cn(.SH/.SZ/.CSI/.CNI) / hk(.HI/.HK) / us(.GI) 各自建历。
  3. **多数票**：某日期须被覆盖该月的**≥50% 序列**同时包含才算交易日。
     原因：污染源（iFinD 日历日合并）同样会给休市日投票，低阈值下「被污染的
     少数派」会把自己投成交易日（实测 2026-10-01~07 有 14/52 票）。多数票对
     真实交易日几乎无损（日频序列对真实交易日覆盖率接近 100%），对污染源有
     天然否决力。
  4. **未知不装懂**：市场样本不足（<3 个序列）/ 日期落在覆盖区间外 → 一律返回
     None 并**保留数据 + WARN**，绝不静默删点（删错=制造缺口，比不删更危险）。

用法：
    import trade_calendar as tc
    tc.market_of("SPX.GI")                 # → "us"
    tc.is_trading_day("2026-10-03", "cn")  # → False
    rep = tc.filter_dates(dates, "cn")     # {'kept','dropped','unknown',...}
    python trade_calendar.py --selfcheck
    python trade_calendar.py --rebuild
    python trade_calendar.py --audit       # 审计存量缓存里的非交易日点
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

DATA = HERE / "data"
PB_DIR = DATA / "index_daily_pb"
PE_DIR = DATA / "index_daily_pe"
CAL_PATH = DATA / "meta" / "trade_days.json"

# 市场 → 代码后缀（顺序即匹配优先级）
MARKETS: dict[str, tuple[str, ...]] = {
    "cn": (".SH", ".SZ", ".CSI", ".CNI"),
    "hk": (".HI", ".HK"),
    "us": (".GI"),
}
MIN_VOTE_RATIO = 0.5   # 多数票阈值
MIN_SERIES = 3         # 该市场样本 <3 个序列 → 日历不可信 → 不判休市
MAX_STALE_DAYS = 7     # 日历末值落后今天 >7 天 → 自动重建
SCHEMA = "trade-calendar/2"

# 合法缓存文件基名（排除 *_pb / *_pb_merged / *_pe 等临时/测试产物）
_CODE_RE = re.compile(r"^[0-9A-Z]{2,8}\.[A-Z]{2,3}$")


# ---------------------------------------------------------------------------
# 市场归类
# ---------------------------------------------------------------------------
def market_of(code: str) -> str | None:
    """指数代码 → 市场标识（cn/hk/us）；无法判定 → None（调用方须 fail-open）。"""
    c = str(code or "").upper()
    for mkt, suffixes in MARKETS.items():
        if any(c.endswith(s) for s in suffixes):
            return mkt
    return None


# ---------------------------------------------------------------------------
# 构建
# ---------------------------------------------------------------------------
def _series_files() -> list[tuple[str, Path]]:
    """[(code, path)]：只保留基名合法的缓存文件（排除临时/测试产物）。"""
    out: list[tuple[str, Path]] = []
    for d in (PB_DIR, PE_DIR):
        if not d.exists():
            continue
        for p in sorted(d.glob("*.json")):
            code = p.stem
            if _CODE_RE.match(code):
                out.append((code, p))
    return out


def _rows_of(p: Path) -> list[str]:
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    out = []
    for row in rec.get("pb") or []:
        try:
            d = str(row[0])
            _dt.date.fromisoformat(d)
        except (TypeError, ValueError, IndexError):
            continue
        out.append(d)
    return out


def build_market_calendar(market: str, verbose: bool = False) -> dict:
    """单市场日历：周末硬排除 + 多数票。样本不足 → available=False（不判休市）。"""
    counts: Counter = Counter()
    cover: Counter = Counter()       # 年月 → 覆盖该月的序列数
    n_files = 0
    for code, p in _series_files():
        if market_of(code) != market:
            continue
        days = _rows_of(p)
        if not days:
            continue
        n_files += 1
        uniq = set(days)
        for ym in {d[:7] for d in uniq}:
            cover[ym] += 1
        for d in uniq:
            counts[d] += 1
    days_out, suspect = [], []
    for d, c in counts.items():
        if _dt.date.fromisoformat(d).weekday() >= 5:
            suspect.append(d)                    # 周末点：永不算交易日
            continue
        need = max(1, math.ceil(cover[d[:7]] * MIN_VOTE_RATIO))
        if c >= need:
            days_out.append(d)
        else:
            suspect.append(d)                    # 有票未过半 → 休市日被写成有值
    days_out.sort()
    suspect.sort()
    available = bool(days_out) and n_files >= MIN_SERIES
    cal = {
        "market": market,
        "available": available,
        "n_series": n_files,
        "vote_ratio": MIN_VOTE_RATIO,
        "n": len(days_out),
        "start": days_out[0] if days_out else None,
        "end": days_out[-1] if days_out else None,
        "suspect_n": len(suspect),
        "suspect": suspect,
        "days": days_out,
    }
    if verbose:
        state = "可用" if available else f"样本不足({n_files}<{MIN_SERIES})·不判休市"
        print(f"  [交易日历·{market}] {n_files} 个序列 → {len(days_out)} 个交易日"
              f"（{cal['start']} ~ {cal['end']}）· {state}", file=sys.stderr)
        if suspect:
            print(f"  [交易日历·{market}] 否决 {len(suspect)} 个「有值但非交易日」可疑点"
                  f"：{suspect[:6]}", file=sys.stderr)
    return cal


def build_calendar(verbose: bool = False) -> dict:
    return {
        "schema": SCHEMA,
        "built_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "markets": {m: build_market_calendar(m, verbose=verbose) for m in MARKETS},
    }


def save_calendar(cal: dict) -> Path:
    CAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    CAL_PATH.write_text(json.dumps(cal, ensure_ascii=False), encoding="utf-8")
    return CAL_PATH


# ---------------------------------------------------------------------------
# 加载
# ---------------------------------------------------------------------------
_CACHE: dict | None = None


def _stale(cal: dict) -> bool:
    try:
        built = _dt.date.fromisoformat(str(cal.get("built_at"))[:10])
    except ValueError:
        return True
    if (_dt.date.today() - built).days > MAX_STALE_DAYS:
        return True
    ends = [(m.get("end") or "") for m in (cal.get("markets") or {}).values()
            if m.get("available")]
    if not ends:
        return True
    try:
        return (_dt.date.today() - max(_dt.date.fromisoformat(e) for e in ends)).days > MAX_STALE_DAYS
    except ValueError:
        return True


def load_calendar(rebuild_if_stale: bool = True) -> dict:
    """读日历缓存；缺失/过期自动重建。重建失败但有旧缓存 → 沿用 + WARN（不静默）。"""
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    cal = None
    if CAL_PATH.exists():
        try:
            cal = json.loads(CAL_PATH.read_text(encoding="utf-8"))
            if cal.get("schema") != SCHEMA:
                cal = None
        except Exception:
            cal = None
    if cal and (not rebuild_if_stale or not _stale(cal)):
        _CACHE = cal
        return cal
    try:
        fresh = build_calendar()
        save_calendar(fresh)
        _CACHE = fresh
        return fresh
    except Exception as e:
        if not cal:
            raise
        print(f"  [WARN] 交易日历重建失败（{type(e).__name__}: {e}），沿用旧日历",
              file=sys.stderr)
        _CACHE = cal
        return cal


# ---------------------------------------------------------------------------
# 判定 / 过滤
# ---------------------------------------------------------------------------
def _market_cal(market: str) -> dict | None:
    cal = load_calendar()
    return (cal.get("markets") or {}).get(market)


def calendar_status() -> dict:
    """各市场日历可用性与覆盖区间（供 preflight / 审计展示）。"""
    load_calendar()
    assert _CACHE is not None
    return {m: {"available": c.get("available"), "n": c.get("n"),
                "start": c.get("start"), "end": c.get("end"),
                "n_series": c.get("n_series"), "suspect_n": c.get("suspect_n")}
            for m, c in (_CACHE.get("markets") or {}).items()}


def is_trading_day(d: str, market: str = "cn") -> bool | None:
    """True=交易日 / False=非交易日 / None=无法判定（市场样本不足或覆盖区间外）。

    **None 时调用方必须保留数据并 WARN** —— 不装懂。
    """
    d = str(d)[:10]
    try:
        _dt.date.fromisoformat(d)
    except ValueError:
        return None
    mc = _market_cal(market)
    if not mc or not mc.get("available"):
        return None
    if d in set(mc["days"]):
        return True
    if mc["start"] <= d <= mc["end"]:
        return False
    return None


def filter_dates(dates, market: str = "cn", verbose: bool = False) -> dict:
    """按市场交易日过滤日期集合。

    返回 {'kept','dropped','unknown','market','available','range'}
    · dropped = 明确非交易日 → 调用方应剔除；
    · unknown = 无法判定 → **保留**，调用方须 WARN；
    · available=False（市场样本不足）→ 全部 keep，调用方须显式提示未过滤。
    """
    mc = _market_cal(market) or {}
    kept, dropped, unknown = [], [], []
    for d in dates:
        r = is_trading_day(d, market)
        if r is True:
            kept.append(d)
        elif r is False:
            dropped.append(d)
        else:
            unknown.append(d)
            kept.append(d)
    if verbose and (dropped or unknown):
        msg = f"  [交易日过滤·{market}] 剔除 {len(dropped)} 个非交易日"
        if dropped:
            msg += f"（示例 {dropped[:5]}）"
        if unknown:
            msg += f"；{len(unknown)} 个无法判定（区间外/样本不足）→ 保留待核"
        print(msg, file=sys.stderr)
    if verbose and not mc.get("available"):
        print(f"  [WARN] {market} 交易日历样本不足（{mc.get('n_series')} 个序列）→ "
              f"**未做交易日过滤**，非交易日点可能残留，须人工核", file=sys.stderr)
    return {"kept": sorted(kept), "dropped": sorted(dropped),
            "unknown": sorted(unknown), "market": market,
            "available": bool(mc.get("available")),
            "range": (mc.get("start"), mc.get("end"))}


def filter_series(series: dict, market: str = "cn",
                  verbose: bool = False) -> tuple[dict, dict]:
    """过滤 {date: value} 序列（估值缓存合并前用）。返回 (clean, report)。"""
    rep = filter_dates(list(series), market, verbose=verbose)
    return {d: series[d] for d in rep["kept"]}, rep


def filter_pairs(pairs, code: str, *, verbose: bool = False):
    """[[date, value], ...] + 指数代码 → (clean_pairs, report)。**落盘前的统一入口**。

    策略（fail-honest，不静默）：
      · 市场可判定且日历可用 → 按该市场交易日过滤；
      · 市场无法判定 / 该市场日历样本不足 → **原样返回** + report['reason']，
        调用方须把 reason 写进产出（WARN），不得声称「已校验」；
      · 日历构建失败（load_calendar 抛错）→ 直接向上抛 CalendarUnavailable
        （fail-closed：宁可拒绝落盘，也不写入未校验序列）。
    """
    mkt = market_of(code)
    if mkt is None:
        return pairs, {"market": None, "available": False, "dropped": [],
                       "unknown": [], "kept": [p[0] for p in pairs],
                       "reason": "市场无法判定（代码后缀未识别）→ 未过滤"}
    mc = _market_cal(mkt) or {}
    if not mc.get("available"):
        return pairs, {"market": mkt, "available": False, "dropped": [],
                       "unknown": [], "kept": [p[0] for p in pairs],
                       "reason": f"{mkt} 交易日历样本不足（{mc.get('n_series')} 个序列）→ 未过滤"}
    rep = filter_dates([p[0] for p in pairs], mkt, verbose=verbose)
    keep = set(rep["kept"])
    clean = [p for p in pairs if p[0] in keep]
    rep["reason"] = "" if not rep["dropped"] else f"剔除 {len(rep['dropped'])} 个非交易日"
    return clean, rep


# ---------------------------------------------------------------------------
# 审计：存量缓存里的非交易日点
# ---------------------------------------------------------------------------
def audit_caches() -> dict:
    """扫描全部估值缓存，报告非交易日点（分市场）。只读，不改数据。"""
    load_calendar()
    bad: dict[str, dict] = {}
    for code, p in _series_files():
        mkt = market_of(code)
        days = _rows_of(p)
        if not days:
            continue
        rep = filter_dates(days, mkt or "cn")
        if rep["dropped"]:
            bad[code] = {"market": mkt, "n": len(days),
                         "n_dropped": len(rep["dropped"]),
                         "sample": rep["dropped"][:6],
                         "unknown_n": len(rep["unknown"])}
    return bad


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------
def _selfcheck() -> bool:
    ok = True

    def chk(cond, msg):
        nonlocal ok
        if not cond:
            ok = False
            print(f"  [FAIL] {msg}")
        else:
            print(f"  [ok  ] {msg}")

    chk(market_of("931151.CSI") == "cn", "931151.CSI → cn")
    chk(market_of("HSTECH.HI") == "hk", "HSTECH.HI → hk")
    chk(market_of("SPX.GI") == "us", "SPX.GI → us")
    chk(market_of("BOGUS") is None, "无法判定市场 → None（fail-open）")

    st = calendar_status()
    chk(st.get("cn", {}).get("available") is True,
        f"cn 日历可用（{st.get('cn', {}).get('n')} 个交易日，"
        f"{st.get('cn', {}).get('start')}~{st.get('cn', {}).get('end')}）")
    chk((st.get("cn", {}).get("n") or 0) > 1000, "cn 日历规模 >1000（≈5 年 × 244）")

    # 周末 & 节假日（被污染少数派写值）必须判 False
    chk(is_trading_day("2026-10-03", "cn") is False, "周六 2026-10-03 → A股非交易日")
    chk(is_trading_day("2026-10-01", "cn") is False, "国庆 2026-10-01（14/52 污染票）→ 非交易日")
    chk(is_trading_day("2026-10-05", "cn") is False, "国庆 2026-10-05（周一休市）→ 非交易日")
    # 区间外 → None
    chk(is_trading_day("2001-01-05", "cn") is None, "覆盖区间外(2001) → None，不判休市")
    # 市场样本不足 → None（绝不套用别市场日历）
    if not st.get("hk", {}).get("available"):
        chk(is_trading_day("2026-10-03", "hk") is None,
            "hk 样本不足 → None（不套用 A 股日历删港股真实交易日）")

    # 过滤：不丢点 + 剔除非交易日 + 幂等
    cn_start, cn_end = st["cn"]["start"], st["cn"]["end"]
    rep = filter_dates([cn_start, "2026-10-03", cn_end, "2001-01-05"], "cn")
    chk(rep["dropped"] == ["2026-10-03"], "非交易日被剔除")
    chk("2001-01-05" in rep["kept"], "未知日期保留（不静默删真实数据）")
    chk(set(rep["kept"]) | set(rep["dropped"]) == {cn_start, cn_end, "2026-10-03", "2001-01-05"},
        "不丢点：kept∪dropped 覆盖全部输入")
    rep2 = filter_dates(rep["kept"], "cn")
    chk(rep2["kept"] == rep["kept"] and not rep2["dropped"], "二次过滤幂等")

    print("trade_calendar selfcheck: " + ("OK" if ok else "FAILED"))
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="分市场交易日历（离线多数票版）")
    ap.add_argument("--selfcheck", action="store_true")
    ap.add_argument("--rebuild", action="store_true", help="强制重建日历缓存")
    ap.add_argument("--check", metavar="DATE", default="", help="判定某日是否交易日")
    ap.add_argument("--market", default="cn", choices=list(MARKETS))
    ap.add_argument("--stats", action="store_true", help="打印各市场日历元信息")
    ap.add_argument("--audit", action="store_true", help="审计存量缓存中的非交易日点（只读）")
    a = ap.parse_args()
    if a.rebuild:
        save_calendar(build_calendar(verbose=True))
        print(f"  → {CAL_PATH}")
    if a.check:
        r = is_trading_day(a.check, a.market)
        print(f"  {a.check}@{a.market} → "
              + {True: "交易日", False: "非交易日", None: "未知（样本不足/区间外）"}[r])
    if a.stats:
        print(json.dumps(calendar_status(), ensure_ascii=False, indent=1))
    if a.audit:
        bad = audit_caches()
        tot = sum(v["n_dropped"] for v in bad.values())
        print(f"  非交易日点：{tot} 个，分布在 {len(bad)} 个缓存")
        for code, v in sorted(bad.items(), key=lambda kv: -kv[1]["n_dropped"]):
            print(f"   {code:<14} {v['market']}  {v['n_dropped']:>3}/{v['n']:<5} {v['sample']}")
    if a.selfcheck:
        return 0 if _selfcheck() else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
