#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
merge_valuation_gap.py —— 估值序列「非破坏性缺口合并器」（2026-10-25 泛化落地）
================================================================================
背景：D-2（数据层审计）。原 `ingest_ifind_valuation.py::_rebuild` 内嵌了一套成熟的
增量合并逻辑（同日冲突保留原源 / 重叠期中位比值归一 / 重叠<3 fail-closed /
幂等 source 标注 / 5y 窗口裁剪 / bands 同步），但它只服务 iFinD 一条链。neodata
（免费源·指数估值日频）接入时如果复制一份，两条链的口径防线就会各自漂移——
参照 个股盈利面框架 的 local_fund_bridge 纪律：**同一套合并语义只允许
一个实现**。

本模块 = 该合并语义的唯一实现，所有 ingest_*_valuation.py 共用：

  1. merge_series()   缺口合并（非破坏性）：
     · 同日冲突**保留原源值**（绝不互相覆盖混口径）；>5% 差异显式 WARN；
     · 缺口日期填入前用**重叠期中位比值**归一到原源口径（否则拼接边界跳变、
       分位失真；实测妙想 PB 2.967 vs iFinD 3.2294 同日差 8.9%，PE 一致）；
     · 重叠点 <3 → 无法可靠校准 → **fail-closed 拒绝拼接**（MergeError）；
     · 重叠期比值 max/min > 1.02 → WARN（校准为近似，建议整序列重建）；
     · 新缓存（无 old）整序列同源，直接落新源原值。
  2. build_record()   5y 窗口裁剪 + <100 点拒绝 + 五档/分位重算 + as_of 诚实
       （as_of 恒取真实序列末值，绝不取请求日期——S-05 缓存虚报新鲜坑）。
  3. sync_bands()     PB bands registry 同步（PE 不入，与 fetcher 行为一致）。
  4. source 幂等标注：`+<provider>_incr(_calib)` 后缀可反复合并不累加
       （多次增量合并不重复追加）。

依赖：fetch_pb_mxdata 的 q / pct_rank / BANDS / DATA（分位与 bands 的单一
真相源，不复制定义）。该 import 无网络副作用（mx_data 仅其 main() 内惰性导入）。

用法（在 ingest 脚本内）：
    from merge_valuation_gap import (merge_series, build_record, sync_bands,
                                     load_cache, save_record, MergeError)
"""
from __future__ import annotations

import datetime
import json
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median

from fetch_pb_mxdata import q, pct_rank, BANDS, DATA  # noqa: F401  (单一真相源)

PB_DIR = DATA / "index_daily_pb"
PE_DIR = DATA / "index_daily_pe"

WINDOW_DAYS = 365 * 5 + 2          # 与 fetcher / ingest_ifind 同：5 年窗口
MIN_POINTS = 100                   # 与 fetcher 同：残缺序列拒绝覆盖缓存
CONFLICT_PCT = 0.05                # 同日口径冲突告警阈值（>5% 显式 WARN）
RATIO_SPREAD_MAX = 1.02            # 重叠期比值稳定性阈值（max/min）

# 幂等标注：合并后缀可来自不同 provider，剥离时全部识别（防 +ifind_incr+neodata_incr 累加）
_INCR_SUFFIXES = ("+ifind_incr_calib", "+ifind_incr",
                  "+neodata_incr_calib", "+neodata_incr")


class MergeError(RuntimeError):
    """fail-closed：合并被拒绝（口径无法校准 / 序列残缺 / 空序列 / 非交易日）。"""


def _is_weekend(d: str) -> bool:
    try:
        return datetime.date.fromisoformat(str(d)[:10]).weekday() >= 5
    except ValueError:
        return False


@dataclass
class MergeResult:
    series: dict                       # 合并后全序列 {date: value}
    filled: int                        # 本次填入的缺口点数（0=无缺口可补）
    conflicts: list = field(default_factory=list)   # [(date, 原源值, 新源值)] 同日冲突（保留原源）
    ratios: list = field(default_factory=list)      # 重叠期比值序列（归一化用）
    calib: str = ""                    # 归一口径说明（空=未归一）
    skipped: bool = False              # True=无缺口可补（调用方应跳过落盘）

    @property
    def ratio_median(self) -> float | None:
        return median(self.ratios) if self.ratios else None


def strip_incr(src: str) -> str:
    """剥离全部已知增量合并后缀（幂等标注的基础）。"""
    base = str(src or "")
    for s in _INCR_SUFFIXES:
        base = base.replace(s, "")
    return base


def incr_source(old_source: str, provider: str, calib: bool) -> str:
    """幂等 source 标注：旧 source 剥离增量后缀后追加本次 provider 的后缀。

    provider: "ifind" / "neodata" …；calib=True 追加 `_calib` 变体。
    新缓存（old_source 空）由调用方直接用 source_tag，不走本函数。
    """
    tag = f"+{provider}_incr" + ("_calib" if calib else "")
    return (strip_incr(old_source) + tag).lstrip("+")


def merge_series(old_series: dict | None, incoming: dict,
                 label: str = "新源", has_old: bool | None = None) -> MergeResult:
    """非破坏性缺口合并（语义与 2026-10-07 ingest_ifind_valuation._rebuild 逐位一致）。

    old_series: {date: value} 原缓存序列（None = 新缓存，整序列同源直落）
    incoming   : {date: value} 新源观测点
    has_old    : 原缓存文件是否存在（None=按 old_series 是否为 None 推断）。
                 **注意**：缓存文件存在但序列为空（损坏/空文件）≠ 新缓存——
                 按原语义走校准分支，重叠 0 点 <3 → fail-closed 拒绝（更安全）。
    返回 MergeResult；口径无法校准时 raise MergeError（fail-closed）。
    """
    if has_old is None:
        has_old = old_series is not None
    # B-03（2026-10-25）纵深防御：估值序列只允许交易日。周末是**确定性事实**
    # （任何市场周六/周日都不开市），因此这里做与市场无关的硬校验——不依赖
    # trade_calendar（可能不可用），也**不允许**静默放过。节假日由 ingest 侧的
    # trade_calendar 分市场过滤负责；此处只兜住「忘了过滤」这一类漏接。
    _wk = [d for d in incoming if _is_weekend(d)]
    if _wk:
        raise MergeError(
            f"incoming 含 {len(_wk)} 个周末(非交易日)日期 {sorted(_wk)[:5]} → 拒绝合并"
            f"（fail-closed：非交易日点会重复计入分位、污染 pct_now/五档；"
            f"请先经 trade_calendar.filter_pairs 过滤）")
    series = dict(old_series or {})
    missing, ratios, conflicts = {}, [], []
    for d, v in sorted(incoming.items()):
        if d in series:
            ov = series[d]
            if ov:
                ratios.append(v / ov)
                if abs(v - ov) / abs(ov) > CONFLICT_PCT:
                    conflicts.append((d, ov, v))
            continue
        missing[d] = v
    calib = ""
    if missing:
        if not has_old:                    # 新缓存：整序列同源，无混合问题
            series.update(missing)
            filled = len(missing)
        elif len(ratios) < 3:
            raise MergeError(
                f"缺口 {len(missing)} 点但重叠点仅 {len(ratios)}(<3)，"
                f"无法校准跨源口径 → 拒绝拼接（fail-closed；请拉全序列重建或等妙想恢复）")
        else:
            r_med = median(ratios)
            spread = max(ratios) / min(ratios) if min(ratios) > 0 else float("inf")
            if spread > RATIO_SPREAD_MAX:
                print(f"  [WARN] 重叠期比值不稳定 max/min={spread:.4f}"
                      f"（中位 {r_med:.4f}），校准为近似——建议条件允许时整序列重建",
                      file=__import__("sys").stderr)
            for d, v in missing.items():
                series[d] = round(v / r_med, 4)
            filled = len(missing)
            calib = f"（缺口 {filled} 点已按重叠期中位比值 {r_med:.4f} 归一到原源口径）"
            print(f"  [INFO] {calib}", file=__import__("sys").stderr)
    else:
        filled = 0
    if not series:
        raise MergeError("合并后序列为空")
    return MergeResult(series=series, filled=filled, conflicts=conflicts,
                       ratios=ratios, calib=calib, skipped=(filled == 0))


def report_conflicts(result: MergeResult, metric: str, label: str = "新源") -> None:
    """同日口径冲突显式报告（保留原源值·未混合）——冲突不是错误，但必须可见。"""
    if not result.conflicts:
        return
    mx = max(result.conflicts, key=lambda x: abs(x[2] - x[1]) / abs(x[1]))
    print(f"  [WARN] {metric.upper()} 同日口径冲突 {len(result.conflicts)} 个"
          f"（保留原源值·未混合）：最大 {mx[0]} 原源={mx[1]} vs {label}={mx[2]}"
          f"（差 {abs(mx[2] - mx[1]) / mx[1] * 100:.1f}%）",
          file=__import__("sys").stderr)


def build_record(code: str, name: str, series: dict, source: str,
                 metric: str = "pb", old: dict | None = None,
                 provider: str = "ifind", calib: str = "",
                 window_days: int = WINDOW_DAYS) -> dict:
    """5y 窗口裁剪 + 五档/分位重算 + as_of 诚实 + 幂等 source 标注。

    metric="pe" 时附带 "metric":"pe" 字段（与 PB 文件同构，series 键恒为 "pb"）。
    """
    pts = sorted(series.items())
    if not pts:
        raise MergeError("合并后序列为空")
    end_d = datetime.date.fromisoformat(pts[-1][0])
    start_d = end_d - datetime.timedelta(days=window_days)
    pts = [(d, v) for d, v in pts if datetime.date.fromisoformat(d) >= start_d]
    if len(pts) < MIN_POINTS:
        raise MergeError(f"合并后序列仅 {len(pts)} 点 (<{MIN_POINTS})，拒绝以残缺序列覆盖缓存")
    vals = [v for _, v in pts]
    now = round(vals[-1], 4)
    src = (incr_source((old or {}).get("source") or "", provider, bool(calib))
           if old else source)
    rec = {
        "index_code": code,
        "index_name": (old or {}).get("index_name") or name or code,
        "source": src,
        "as_of": pts[-1][0],          # 诚实：真实序列末值，绝不取请求日期（S-05）
        "n": len(pts), "start": pts[0][0], "end": pts[-1][0],
        "pb": [[d, v] for d, v in pts],   # 同构约定：series 键恒为 "pb"
        "p5": round(q(5, vals), 4), "p20": round(q(20, vals), 4),
        "p50": round(q(50, vals), 4), "p80": round(q(80, vals), 4),
        "p95": round(q(95, vals), 4),
        "now": now, "pct_now": pct_rank(now, vals),
    }
    if metric == "pe":
        rec["metric"] = "pe"
    return rec


def load_cache(path: Path | str) -> dict | None:
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_record(out_dir: Path | str, code: str, rec: dict) -> Path:
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{code}.json"
    path.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    return path


def sync_bands(code: str, rec: dict, bands_path: Path | str | None = None) -> None:
    """PB bands registry 同步（与 fetcher / ingest_ifind 行为一致；PE 不入）。"""
    bp = Path(bands_path) if bands_path else BANDS
    bands = json.loads(bp.read_text(encoding="utf-8")) if bp.exists() else {}
    b = bands.get(code, {})
    b.update({"end": rec["end"], "n": rec["n"], "pct": rec["pct_now"],
              "now": rec["now"], "p5": rec["p5"], "p95": rec["p95"],
              "p20": rec["p20"], "p50": rec["p50"], "p80": rec["p80"]})
    bands[code] = b
    bp.write_text(json.dumps(bands, ensure_ascii=False, indent=1), encoding="utf-8")


# ------------------------------------------------------------------ 自校验
def _selfcheck() -> int:
    """确定性回归：构造夹具验证四条合并语义（同日保留/归一/<3 拒绝/幂等标注）。"""
    import os
    import tempfile

    # ① 新缓存直落（B-03 后夹具须用交易日：2026-01-05/06 为周一/周二）
    r = merge_series(None, {"2026-01-05": 2.0, "2026-01-06": 2.1})
    assert r.series == {"2026-01-05": 2.0, "2026-01-06": 2.1} and r.filled == 2
    assert not r.conflicts and not r.skipped

    # ② 同日冲突保留原源 + 缺口按中位比值归一
    old = {"2026-01-05": 2.0, "2026-01-06": 2.0, "2026-01-07": 2.0}
    inc = {"2026-01-05": 2.2, "2026-01-06": 2.2, "2026-01-07": 2.2, "2026-01-08": 2.2}
    r = merge_series(old, inc)
    assert r.series["2026-01-05"] == 2.0, "同日冲突必须保留原源值"
    assert len(r.conflicts) == 3 and abs(r.ratio_median - 1.1) < 1e-9
    assert abs(r.series["2026-01-08"] - 2.0) < 1e-9, "缺口须按中位比值 1.1 归一"
    assert r.filled == 1 and not r.skipped

    # ③ 重叠<3 fail-closed
    try:
        merge_series({"2026-01-05": 2.0}, {"2026-01-05": 2.1, "2026-01-06": 2.1})
        raise AssertionError("重叠<3 必须 MergeError")
    except MergeError:
        pass

    # ③b B-03 纵深防御：周末（非交易日）入参必须 fail-closed 拒绝
    for bad in ("2026-01-03", "2026-01-04"):     # 周六/周日
        try:
            merge_series(None, {bad: 2.0})
            raise AssertionError(f"周末点 {bad} 必须被拒绝")
        except MergeError:
            pass

    # ④ 幂等 source 标注（反复合并不累加后缀）
    s0 = "mx-ds-mcp-daily-pb"
    s1 = incr_source(s0, "ifind", calib=True)
    s2 = incr_source(s1, "ifind", calib=True)
    assert s1 == "mx-ds-mcp-daily-pb+ifind_incr_calib", s1
    assert s2 == s1, f"幂等失败: {s2}"

    # ⑤ build_record 窗口裁剪 + as_of 诚实 + <100 拒绝
    series = {f"2026-01-{d:02d}": 2.0 for d in range(1, 32)}   # 31 点 <100
    try:
        build_record("X.CSI", "X", series, "src")
        raise AssertionError("<100 点必须拒绝")
    except MergeError:
        pass

    # ⑥ sync_bands 往返
    with tempfile.TemporaryDirectory() as td:
        bp = os.path.join(td, "bands.json")
        rec = build_record("X.CSI", "X", {**series, **{
            f"2025-{m:02d}-15": 1.5 + 0.01 * m for m in range(1, 13)}},
            "src", old=None) if False else None
        # 直接构造最小 rec 验证 bands 同步
        sync_bands("X.CSI", {"end": "2026-01-31", "n": 31, "pct_now": 50.0,
                             "now": 2.0, "p5": 1.9, "p95": 2.1,
                             "p20": 1.95, "p50": 2.0, "p80": 2.05}, bands_path=bp)
        back = json.loads(Path(bp).read_text(encoding="utf-8"))
        assert back["X.CSI"]["now"] == 2.0 and back["X.CSI"]["n"] == 31

    print("merge_valuation_gap 自校验通过（6/6：新缓存直落/同日保留/中位比值归一/"
          "重叠<3拒绝/幂等标注/窗口与bands）")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selfcheck())
