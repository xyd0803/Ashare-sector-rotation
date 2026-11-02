#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XYD 供需板块框架 —— 量化防错守卫 (Guards)
========================================
专治量化研究里最常见、也最致命的六类错误。纪律层（discipline.py）与回测
（backtest.py）都必须经过本模块，任何绕过都视为违规。

  G1 未来函数 / 前视偏差 (Look-ahead)
      - as_of 之后的任何数据点禁止进入决策
      - PB 分位必须 as-of 滚动计算，禁止用全样本分位给历史时点打标
      - 信号 t 生成 → t+1 执行（回测强制）
  G2 过拟合 (Overfitting)
      - 参数指纹冻结：params.json 改一个数就换指纹，留痕可查
      - 自由参数计数 vs 最小样本量门（默认 10×参数数）
      - 参数敏感性：±20% 扰动的结论翻转率 >30% 判「脆弱/疑似过拟合」
      - 无自由裁量入口：修正项白名单 key 之外一律 raise
  G3 幸存者偏差 (Survivorship)
      - ETF 名单须 point-in-time：成立日 ≤ as_of，退市/清算标的保留但标记
  G4 数据口径错配 (Field mismatch)
      - PB 字段警戒词扫描（"行业均值/加权平均"冒充所选指数 —— 已两次踩坑）
      - 分位周期必须标注，全历史分位禁止决策
  G5 多重比较 (Data snooping)
      - 试验次数登记 + Bonferroni 提示 + 参数网格搜索告警
  G6 成本幻觉 (Zero-cost illusion)
      - 回测成本 bps 必须 > 0，且样本期数 ≥ 12
"""
from __future__ import annotations

import hashlib
import json
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass


# =============================================================================
# 异常
# =============================================================================
class GuardError(Exception):
    """守卫拒绝：必须中止，不允许降级绕过。"""


class LookaheadError(GuardError):
    """未来函数。"""


class OverfitGuardError(GuardError):
    """过拟合防线。"""


class SurvivorshipError(GuardError):
    """幸存者偏差。"""


# =============================================================================
# G2-a 参数指纹冻结
# =============================================================================
def params_fingerprint(params: Dict[str, Any]) -> str:
    """对参数做规范化 SHA256。任何数值改动 → 指纹变化 → 报告里可追溯。"""
    clean = _strip_comments(params)
    blob = json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _strip_comments(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _strip_comments(v) for k, v in obj.items()
                if not (isinstance(k, str) and k.startswith("_"))}
    if isinstance(obj, list):
        return [_strip_comments(x) for x in obj]
    return obj


def assert_params_frozen(params: Dict[str, Any], expected: Optional[str],
                         strict: bool = False) -> Tuple[bool, str]:
    """与 params.lock 比对。strict=True 时不一致直接 raise。"""
    fp = params_fingerprint(params)
    if expected is None:
        return False, fp
    ok = (fp == expected)
    if not ok and strict:
        raise OverfitGuardError(
            f"参数指纹不一致：期望 {expected[:12]}… 实际 {fp[:12]}…\n"
            f"→ params.json 被改动但未 bump version / 未更新 params.lock。"
            f"这是防『事后调参凑结论』的核心防线，禁止跳过。")
    return ok, fp


# =============================================================================
# G1 未来函数
# =============================================================================
def _d(x: str):
    return datetime.strptime(x[:10], "%Y-%m-%d").date()


def assert_no_lookahead(as_of: str, records: Sequence[Tuple[str, str]]) -> None:
    """records = [(字段名, 该数据的真实可得日期)]，任一晚于 as_of 即拒绝。"""
    bad = []
    for name, ts in records:
        try:
            if _d(ts) > _d(as_of):
                bad.append(f"{name}@{ts}")
        except Exception:
            bad.append(f"{name}@日期非法({ts})")
    if bad:
        raise LookaheadError(
            f"未来函数：as_of={as_of} 却引用了 {bad} —— 决策时点不可得的数据禁止入模。")


def asof_percentile(series: Sequence[Tuple[str, float]], as_of: str,
                    window_years: float = 5.0, min_points: int = 250
                    ) -> Optional[Dict[str, Any]]:
    """
    as-of 滚动分位（G1 核心）：只用 as_of 及之前、且落在 window_years 窗口内的点。
    绝不使用全样本分位为历史时点打标（这是最隐蔽的未来函数）。
    返回 {pct, p5, p20, p50, p80, p95, n, window_years, freq_hint}
    """
    a = _d(as_of)
    cut = a.replace(year=a.year - int(window_years)) if window_years >= 1 else a
    pts = [v for ts, v in series if _d(ts) <= a and _d(ts) >= cut]
    if len(pts) < min_points:
        return None
    s = sorted(pts)
    cur = [v for ts, v in series if _d(ts) <= a][-1]

    def q(p: float) -> float:
        k = (len(s) - 1) * p / 100.0
        lo, hi = int(math.floor(k)), int(math.ceil(k))
        return s[lo] if lo == hi else s[lo] + (s[hi] - s[lo]) * (k - lo)

    return {"pct": round(100.0 * sum(1 for v in s if v <= cur) / len(s), 2),
            "p5": round(q(5), 4), "p20": round(q(20), 4), "p50": round(q(50), 4),
            "p80": round(q(80), 4), "p95": round(q(95), 4),
            "n": len(s), "window_years": window_years, "current": cur}


def assert_t_plus_1(signal_date: str, exec_date: str) -> None:
    """信号 t 生成、t+1 执行。同日成交 = 用收盘信号做收盘价交易 = 未来函数。"""
    if _d(exec_date) <= _d(signal_date):
        raise LookaheadError(
            f"执行日 {exec_date} 未晚于信号日 {signal_date} → 同日/回溯成交属未来函数。")


# =============================================================================
# G2-b 自由参数 / 样本量 / 敏感性
# =============================================================================
@dataclass
class OverfitGuard:
    free_params: int
    min_samples_per_param: int = 10
    trials: int = 0
    trial_log: List[str] = field(default_factory=list)

    @property
    def min_samples(self) -> int:
        return self.free_params * self.min_samples_per_param

    def check_sample_size(self, n_samples: int, strict: bool = False) -> Dict[str, Any]:
        ok = n_samples >= self.min_samples
        msg = (f"样本 {n_samples} ≥ 门槛 {self.min_samples}（{self.free_params}参数×"
               f"{self.min_samples_per_param}）" if ok else
               f"样本 {n_samples} < 门槛 {self.min_samples} → 结论只能标『启发式未校准』，"
               f"禁止宣称『回测验证有效』")
        if not ok and strict:
            raise OverfitGuardError(msg)
        return {"ok": ok, "msg": msg, "n": n_samples, "required": self.min_samples}

    def record_trial(self, desc: str) -> None:
        self.trials += 1
        self.trial_log.append(desc)

    def multiple_testing_report(self, alpha: float = 0.05,
                                warn_trials: int = 5) -> Dict[str, Any]:
        adj = alpha / max(1, self.trials)
        return {
            "trials": self.trials,
            "alpha": alpha,
            "bonferroni_alpha": round(adj, 5),
            "warn": self.trials >= warn_trials,
            "msg": (f"已试 {self.trials} 组参数 → 显著性门槛应收紧至 {adj:.4f}；"
                    f"多重比较下『最优参数』极易是噪声。"
                    if self.trials >= warn_trials else
                    f"试验 {self.trials} 组，多重比较风险可控。"),
        }


def sensitivity_flip_rate(cases: Iterable[Dict[str, Any]], evaluator,
                          perturb_pct: float = 20.0,
                          key: str = "quadrant") -> Dict[str, Any]:
    """
    参数敏感性：把 params 中的数值阈值同时上/下扰动 perturb_pct%，
    看结论（默认象限）翻转比例。翻转率高 = 结论骑在阈值上 = 脆弱/疑似过拟合。
    evaluator(case, scale) -> 结论值
    """
    flips = total = 0
    detail = []
    for c in cases:
        base = evaluator(c, 1.0)
        for scale in (1 - perturb_pct / 100.0, 1 + perturb_pct / 100.0):
            got = evaluator(c, scale)
            total += 1
            if got != base:
                flips += 1
                detail.append({"case": c.get("sector", "?"), "scale": scale,
                               "base": base, "perturbed": got})
    rate = (flips / total) if total else 0.0
    return {"flip_rate": round(rate, 4), "flips": flips, "total": total,
            "detail": detail[:20]}


def scale_numeric_thresholds(params: Dict[str, Any], scale: float,
                            paths: Sequence[Sequence[str]]) -> Dict[str, Any]:
    """按给定路径缩放阈值，生成扰动参数副本（不改原对象）。"""
    out = json.loads(json.dumps(params))
    for path in paths:
        node = out
        for k in path[:-1]:
            node = node[k]
        v = node[path[-1]]
        if isinstance(v, (int, float)):
            node[path[-1]] = v * scale
    return out


# =============================================================================
# G3 幸存者偏差
# =============================================================================
def filter_pit_universe(universe: Sequence[Dict[str, Any]], as_of: str,
                        strict: bool = True) -> List[Dict[str, Any]]:
    """
    point-in-time 宇宙：只保留 as_of 当日已上市且未退市的标的。
    universe item: {code, name, listed_date, delisted_date?}
    缺 listed_date 时 strict=True 直接拒绝（否则等于默认"一直存在"= 幸存者偏差）。
    """
    a = _d(as_of)
    out = []
    for it in universe:
        ld = it.get("listed_date")
        if not ld:
            if strict:
                raise SurvivorshipError(
                    f"{it.get('code')} 缺 listed_date → 无法做 point-in-time 筛选，"
                    f"直接使用会引入幸存者偏差（今天存在的 ETF 才被回测）。")
            continue
        if _d(ld) > a:
            continue
        dd = it.get("delisted_date")
        if dd and _d(dd) <= a:
            continue
        out.append(it)
    return out


# =============================================================================
# G4 口径错配
# =============================================================================
def scan_field_keywords(label: str, forbidden: Sequence[str]) -> List[str]:
    return [k for k in forbidden if k and k in (label or "")]


def check_percentile_period(period: str, allowed: Sequence[str],
                            forbidden: Sequence[str]) -> Tuple[bool, str]:
    p = (period or "").lower()
    if p in [x.lower() for x in forbidden]:
        return False, f"全历史/成立以来分位禁止用于决策（period={period}）"
    if p not in [x.lower() for x in allowed]:
        return False, f"分位周期未标注或非法（period={period!r}），须 5y/10y"
    return True, f"period={period} 合规"


# =============================================================================
# G6 成本 / 样本期
# =============================================================================
def check_backtest_realism(cost_bps: float, n_periods: int,
                           min_cost_bps: float = 3.0, min_periods: int = 12,
                           strict: bool = True) -> Dict[str, Any]:
    problems = []
    if cost_bps < min_cost_bps:
        problems.append(f"成本 {cost_bps}bps < {min_cost_bps}bps：零成本回测会系统性高估收益")
    if n_periods < min_periods:
        problems.append(f"样本仅 {n_periods} 期 < {min_periods} 期：不足以支撑任何统计结论")
    if problems and strict:
        raise GuardError("回测真实性守卫拒绝：" + "；".join(problems))
    return {"ok": not problems, "problems": problems}


# =============================================================================
# 汇总自检
# =============================================================================
def guard_report(params: Dict[str, Any], lock_fp: Optional[str] = None) -> Dict[str, Any]:
    g = params.get("guards", {})
    og = OverfitGuard(free_params=g.get("free_param_count", 9),
                      min_samples_per_param=g.get("min_samples_per_param", 10))
    frozen, fp = assert_params_frozen(params, lock_fp)
    return {
        "params_version": params.get("version"),
        "fingerprint": fp,
        "frozen_match": frozen,
        "free_params": og.free_params,
        "min_samples_required": og.min_samples,
        "forbidden_field_keywords": g.get("forbidden_field_keywords", []),
        "sensitivity_perturb_pct": g.get("sensitivity_perturb_pct"),
        "fragile_flip_rate": g.get("sensitivity_flip_rate_fragile"),
        "backtest_min_cost_bps": g.get("backtest_min_cost_bps"),
        "backtest_min_periods": g.get("backtest_min_periods"),
    }


if __name__ == "__main__":
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "params.json"), "r", encoding="utf-8") as f:
        P = json.load(f)
    lock_path = os.path.join(here, "params.lock")
    lock = None
    if os.path.exists(lock_path):
        with open(lock_path, "r", encoding="utf-8") as f:
            lock = json.load(f).get("fingerprint")
    rep = guard_report(P, lock)
    print("== guards 自检 ==")
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    if lock is None:
        with open(lock_path, "w", encoding="utf-8") as f:
            json.dump({"fingerprint": rep["fingerprint"],
                       "params_version": rep["params_version"],
                       "note": "参数冻结锁。改 params.json 后须 bump version 并重新生成本文件。"},
                      f, ensure_ascii=False, indent=2)
        print(f"\n[INIT] 已生成 params.lock：{rep['fingerprint'][:16]}…")
    else:
        print(f"\n参数冻结校验：{'PASS 一致' if rep['frozen_match'] else 'FAIL 不一致（须说明改动理由）'}")
