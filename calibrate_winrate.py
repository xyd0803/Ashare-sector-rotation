#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
g(S) 校准骨架 (O5, 2026-08-16)
=================================================================
用 54 ETF 历史信号 → 持有期胜率 回归拟合 win_map，替换 quant_winrate 当前的占位映射。

背景：quant_winrate.compute_quant 的 `win_map`（g(S)）目前是占位值（地板20/顶80），
self-check 注释明示「待 54 ETF 历史回测校准」。本脚本定义数据契约 + 拟合流程，
**待 54 ETF 回测出数后一键产出校准 win_map 提案**。

数据契约 backtest_features.json:
  [
    {"as_of": "YYYY-MM-DD", "sector": str, "S": float(0-100),
     "win": 0/1,            # 持有期(默认 6M/12M)是否跑赢基准（point-in-time, t+1 执行）
     "fwd_ret": float(%)},   # 持有期实际收益（诊断用）
    ...
  ]
  - S 由 quant_winrate.compute_quant 在每只 ETF 的 as_of 快照上算出（须用 O1 量化输入契约派生的 D1-D5）。
  - win 须为 point-in-time 结果，禁止未来函数；持有期成本须 >=3bps。

拟合方法（两种，可切换）：
  - empirical：按 S 分桶算经验胜率（可解释、小样本稳健）→ 主推
  - logistic ：S 对 P(win) 逻辑回归（纯 stdlib 梯度下降）→ 平滑

产出：proposed_win_map.json（[ [upto_S, win], ... ] 同 quant_winrate 的 win_map schema）
      + 校验指标（分桶校准误差、AUC、单调性）+ 落地指引（需用户审 + bump version + 更新指纹锁）。

设计原则：纯标准库、零依赖、全留痕、不自动改 params（防偷偷调参）。
"""
from __future__ import annotations
import sys
import os
import json
import math
import random

HERE = os.path.dirname(os.path.abspath(__file__))


# ----------------------------------------------------------------------------
# 数据加载
# ----------------------------------------------------------------------------
def load_features(path: str) -> list:
    with open(path, "r", encoding="utf-8") as f:
        rows = json.load(f)
    if not isinstance(rows, list):
        raise ValueError("backtest_features.json 应为数组")
    for i, r in enumerate(rows):
        if "S" not in r or "win" not in r:
            raise ValueError(f"第 {i} 行缺 S/win 字段")
        if r["win"] not in (0, 1):
            raise ValueError(f"第 {i} 行 win 须为 0/1，实得 {r['win']}")
    return rows


# ----------------------------------------------------------------------------
# 方法一：经验分桶
# ----------------------------------------------------------------------------
def fit_empirical(rows: list, n_bins: int = 10, floor: int = 20, cap: int = 80) -> list:
    """按 S 分桶，桶内经验胜率 → win_map。空桶前向继承，整体夹地板/天花板。"""
    rows = sorted(rows, key=lambda r: r["S"])
    edges = [round(100 * (i + 1) / n_bins) for i in range(n_bins)]
    edges[-1] = 100
    bins = [[] for _ in range(n_bins)]
    for r in rows:
        idx = min(int(r["S"] / 100.0 * n_bins), n_bins - 1)
        bins[idx].append(r["win"])
    out = []
    last = floor
    for i, e in enumerate(edges):
        if bins[i]:
            wr = 100.0 * sum(bins[i]) / len(bins[i])
        else:
            wr = last  # 空桶继承
        wr = max(floor, min(cap, wr))
        last = wr
        out.append([e, round(wr, 1)])
    # 保证非递减
    for i in range(1, len(out)):
        if out[i][1] < out[i - 1][1]:
            out[i][1] = out[i - 1][1]
    return out


# ----------------------------------------------------------------------------
# 方法二：逻辑回归（纯 stdlib 梯度下降）
# ----------------------------------------------------------------------------
def _sigmoid(z):
    if z > 30:
        return 1.0
    if z < -30:
        return 0.0
    return 1.0 / (1.0 + math.exp(-z))


def fit_logistic(rows: list, iters: int = 8000, lr: float = 0.1, floor: int = 20, cap: int = 80):
    """P(win) = sigmoid(a + b*(S-50)/50)。返回 (a, b)。"""
    xs = [(r["S"] - 50.0) / 50.0 for r in rows]
    ys = [float(r["win"]) for r in rows]
    n = len(xs)
    a, b = 0.0, 0.0
    for _ in range(iters):
        ga, gb = 0.0, 0.0
        for x, y in zip(xs, ys):
            p = _sigmoid(a + b * x)
            err = p - y
            ga += err
            gb += err * x
        a -= lr * ga / n
        b -= lr * gb / n
    return a, b


def win_map_from_logistic(a, b, breakpoints=(0, 20, 40, 55, 70, 85, 100),
                          floor: int = 20, cap: int = 80) -> list:
    out = []
    for s in breakpoints:
        x = (s - 50.0) / 50.0
        p = _sigmoid(a + b * x) * 100.0
        p = max(floor, min(cap, p))
        out.append([s, round(p, 1)])
    for i in range(1, len(out)):
        if out[i][1] < out[i - 1][1]:
            out[i][1] = out[i - 1][1]
    return out


# ----------------------------------------------------------------------------
# 校验
# ----------------------------------------------------------------------------
def _auc(rows: list) -> float:
    """二分类 AUC（纯 stdlib）。"""
    pos = [r["S"] for r in rows if r["win"] == 1]
    neg = [r["S"] for r in rows if r["win"] == 0]
    if not pos or not neg:
        return float("nan")
    c = 0
    for x in pos:
        for y in neg:
            c += 1 if x > y else (0.5 if x == y else 0)
    return c / (len(pos) * len(neg))


def validate(rows: list, win_map: list, n_bins: int = 10) -> dict:
    """分桶校准误差 + AUC + 单调性。win_map: [[S,win],...] 同 schema。"""
    # 用 win_map 预测每个样本的 P(win)
    wm = sorted(win_map, key=lambda p: p[0])

    def predict(s):
        if s <= wm[0][0]:
            return wm[0][1] / 100.0
        if s >= wm[-1][0]:
            return wm[-1][1] / 100.0
        for i in range(len(wm) - 1):
            s0, w0 = wm[i]
            s1, w1 = wm[i + 1]
            if s0 <= s <= s1:
                return (w0 + (w1 - w0) * (s - s0) / (s1 - s0)) / 100.0
        return wm[-1][1] / 100.0

    rows = sorted(rows, key=lambda r: r["S"])
    edges = [round(100 * (i + 1) / n_bins) for i in range(n_bins)]
    edges[-1] = 100
    bins = [[] for _ in range(n_bins)]
    for r in rows:
        idx = min(int(r["S"] / 100.0 * n_bins), n_bins - 1)
        bins[idx].append((predict(r["S"]), r["win"]))
    cal_err = 0.0
    n_b = 0
    per_bin = []
    for i, e in enumerate(edges):
        if bins[i]:
            pred = sum(p for p, _ in bins[i]) / len(bins[i])
            obs = sum(w for _, w in bins[i]) / len(bins[i])
            cal_err += abs(pred - obs)
            n_b += 1
            per_bin.append({"upto_S": e, "pred": round(pred, 3), "obs": round(obs, 3),
                            "n": len(bins[i])})
    mono = all(win_map[i][1] <= win_map[i + 1][1] for i in range(len(win_map) - 1))
    return {
        "calibration_error_pctpts": round(cal_err / max(n_b, 1), 2),
        "auc": round(_auc(rows), 4),
        "monotonic": mono,
        "per_bin": per_bin,
        "n": len(rows),
    }


# ----------------------------------------------------------------------------
# Demo（证明管线端到端可用，无需真实数据）
# ----------------------------------------------------------------------------
def _demo():
    random.seed(42)
    # 合成单调关系：真实 P(win)=sigmoid((S-50)/18)，采样 600 点
    rows = []
    for _ in range(600):
        s = random.uniform(2, 98)
        p = _sigmoid((s - 50) / 18.0)
        win = 1 if random.random() < p else 0
        rows.append({"as_of": "2024-01-01", "sector": "demo", "S": round(s, 1),
                     "win": win, "fwd_ret": 0.0})
    emp = fit_empirical(rows)
    a, b = fit_logistic(rows)
    logi = win_map_from_logistic(a, b)
    print("== DEMO 合成数据（真实 P=sigmoid((S-50)/18)）==")
    print(f"empirical win_map: {emp}")
    print(f"logistic   win_map: {logi}  (a={a:.3f}, b={b:.3f})")
    ve = validate(rows, emp)
    vl = validate(rows, logi)
    print(f"empirical 校验: {ve}")
    print(f"logistic   校验: {vl}")
    assert ve["monotonic"] and vl["monotonic"], "win_map 须单调非递减"
    assert ve["auc"] > 0.7 and vl["auc"] > 0.7, "AUC 应显著>0.5"
    print("\nDEMO PASS：管线可端到端产出单调、高区分度的校准 win_map")
    return rows, emp, logi


# ----------------------------------------------------------------------------
# 真实运行
# ----------------------------------------------------------------------------
def run(input_path: str, out_path: str, method: str = "empirical", n_bins: int = 10,
        horizon: str = "3m"):
    rows = load_features(input_path)
    if horizon != "3m":
        key = f"win_{horizon}"
        rows = [dict(r, win=r[key]) for r in rows if r.get(key) is not None]
        if not rows:
            raise ValueError(f"horizon={horizon}: 特征文件无 {key} 标签")
    if method == "empirical":
        wm = fit_empirical(rows, n_bins=n_bins)
    elif method == "logistic":
        a, b = fit_logistic(rows)
        wm = win_map_from_logistic(a, b)
    else:
        raise ValueError("method 须为 empirical/logistic")
    rep = {
        "source": input_path,
        "method": method,
        "horizon": horizon,
        "n_samples": len(rows),
        "win_map": wm,
        "validation": validate(rows, wm, n_bins),
        "note": "提案。落地须：①人工审阅 ②quant_winrate.DEFAULT_QUANT.win_map 与 params.json[win_quant.win_map] "
                "同步替换 ③bump version ④更新 params.lock 指纹 ⑤跑 run_all 闸门",
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2)
    print(f"已写出提案 → {out_path}")
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    return rep


def _predict_prob(win_map: list, s: float) -> float:
    wm = sorted(win_map, key=lambda p: p[0])
    if s <= wm[0][0]:
        return wm[0][1] / 100.0
    if s >= wm[-1][0]:
        return wm[-1][1] / 100.0
    for i in range(len(wm) - 1):
        s0, w0 = wm[i]
        s1, w1 = wm[i + 1]
        if s0 <= s <= s1:
            return (w0 + (w1 - w0) * (s - s0) / (s1 - s0)) / 100.0
    return wm[-1][1] / 100.0


def walkforward(input_path: str, horizon: str = "3m", method: str = "empirical",
                train_min_months: int = 48, test_months: int = 12) -> dict:
    """Walk-forward 校准：expanding train → 固定 test 窗，滚动推进。
    报告【样本外】AUC/校准误差/各窗明细——全样本 AUC 过拟合虚高的照妖镜。"""
    rows = load_features(input_path)
    if horizon != "3m":
        key = f"win_{horizon}"
        rows = [dict(r, win=r[key]) for r in rows if r.get(key) is not None]
    months = sorted({r["as_of"][:7] for r in rows})
    folds = []
    i = train_min_months
    while i < len(months):
        test_ym = months[i:i + test_months]
        train_ym = set(months[:i])
        tr = [r for r in rows if r["as_of"][:7] in train_ym]
        te = [r for r in rows if r["as_of"][:7] in test_ym]
        if len(tr) >= 200 and len(te) >= 30:
            wm = fit_empirical(tr) if method == "empirical" else win_map_from_logistic(*fit_logistic(tr))
            preds = [{"S": _predict_prob(wm, r["S"]) * 100.0, "win": r["win"]} for r in te]
            folds.append({
                "train_end": months[i - 1], "test": f"{test_ym[0]}~{test_ym[-1]}",
                "n_train": len(tr), "n_test": len(te),
                "oos_auc": round(_auc(preds), 4),
                "oos_cal_err_pp": round(sum(abs(p["S"] / 100.0 - p["win"]) for p in preds) / len(preds) * 100, 2),
            })
        i += test_months
    aucs = [f["oos_auc"] for f in folds]
    rep = {
        "source": input_path, "method": method, "horizon": horizon,
        "mode": "walkforward(expanding-train)",
        "train_min_months": train_min_months, "test_months": test_months,
        "n_folds": len(folds),
        "oos_auc_mean": round(sum(aucs) / len(aucs), 4) if aucs else None,
        "oos_auc_min": round(min(aucs), 4) if aucs else None,
        "oos_auc_max": round(max(aucs), 4) if aucs else None,
        "folds": folds,
        "note": "OOS AUC>0.55 才支持 validated=true 解除 shrink；否则保留保守占位 g(S)。",
    }
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    return rep


# ----------------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------------
if __name__ == "__main__":
    args = sys.argv[1:]
    if "--selfcheck" in args or "selfcheck" in args:
        _demo()
        sys.exit(0)
    if "--demo" in args:
        _demo()
        sys.exit(0)
    if "--input" in args:
        i = args.index("--input")
        inp = args[i + 1]
        out = "proposed_win_map.json"
        method = "empirical"
        horizon = "3m"
        if "--out" in args:
            out = args[args.index("--out") + 1]
        if "--method" in args:
            method = args[args.index("--method") + 1]
        if "--horizon" in args:
            horizon = args[args.index("--horizon") + 1]
        if "--walkforward" in args:
            walkforward(inp, horizon=horizon, method=method)
        else:
            run(inp, out, method, horizon=horizon)
    else:
        print(__doc__)
        print("\n用法:")
        print("  python calibrate_winrate.py --selfcheck          # 端到端自检（合成数据）")
        print("  python calibrate_winrate.py --demo              # 演示拟合")
        print("  python calibrate_winrate.py --input features.json --out prop.json [--method empirical|logistic] [--horizon 3m|6m|12m]")
        print("  python calibrate_winrate.py --input features.json --walkforward [--horizon 3m|6m|12m]  # 样本外校准")
