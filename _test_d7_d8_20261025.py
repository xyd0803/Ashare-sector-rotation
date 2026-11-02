#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
_test_d7_d8_20261025.py —— 第三批 D-7 / D-8 注入式回归（实现验证铁律）

覆盖：
  D-7 现价双源交叉
    T1 双源一致（差 0%）→ multi_source=True，无 warn
    T2 双源分歧 1%（>0.5%）→ warn 命中，仍取主源（新浪）
    T3 腾讯不可用 → 单源告警，price 仍可用
    T4 新浪不可用、腾讯可用 → 兜底并标单源
    T5 双源皆失败 → price=None，warn 命中（EV 走相对=100）
  D-8 健康度四态
    T6 OK 全绿无水印
    T7 单源现价 + 无 10y → DEGRADED 且每条降级有 impact
    T8 无序列 → MISSING
    T9 窗口内口径突变 → INVALID；窗口外 → DEGRADED（防误杀）
  T10 编排层真实路径：pipeline 草稿必须带 _price_cross_check 与 _data_health

用法：python _test_d7_d8_20261025.py   （exit 0=全过）
      python _test_d7_d8_20261025.py --with-integration
          （额外跑 T10 集成用例：真跑 pipeline，需新鲜本地 data/）
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import data_health as dh          # noqa: E402
import run_sector_pipeline as rsp  # noqa: E402

FAILS = []

# T10 是集成用例（真跑 pipeline，需新鲜本地数据）；默认跳过，见 main() 中说明。
RUN_INTEGRATION = "--with-integration" in sys.argv


def chk(cond, msg):
    print(f"  [{'PASS' if cond else 'FAIL'}] {msg}")
    if not cond:
        FAILS.append(msg)


def _patch(sina, tencent):
    """把两个私有取价函数替换为固定返回（不联网）。"""
    rsp._fetch_price_sina = lambda etf: sina
    rsp._fetch_price_tencent = lambda etf: tencent


def main() -> int:
    print("=" * 70)
    print("D-7 现价双源交叉")
    print("=" * 70)

    _patch((1.000, "新浪实时"), (1.000, "腾讯实时"))
    px, note, cross = rsp.fetch_price_now("512800")
    chk(px == 1.0 and cross["multi_source"] and cross["warn"] is None
        and cross["diff_pct"] == 0.0, "T1 双源一致 → multi_source=True 且无告警")

    _patch((1.000, "新浪实时"), (1.010, "腾讯实时"))
    px, note, cross = rsp.fetch_price_now("512800")
    chk(px == 1.0 and cross["multi_source"] and cross["warn"]
        and abs(cross["diff_pct"] - 1.0) < 0.01 and "1.00%" in cross["warn"],
        "T2 分歧 1.00% > 0.5% → 告警且仍取主源新浪")
    chk(px == 1.0, "T2b 分歧时主源不被第二源覆盖（避免静默改值）")

    _patch((1.000, "新浪实时"), (None, "拉取失败(TimeoutError)"))
    px, note, cross = rsp.fetch_price_now("512800")
    chk(px == 1.0 and not cross["multi_source"] and cross["warn"]
        and "单源" in cross["warn"], "T3 腾讯不可用 → 单源告警，主源仍可用")

    _patch((None, "现价=0（盘前/未开盘）"), (1.000, "腾讯实时"))
    px, note, cross = rsp.fetch_price_now("512800")
    chk(px == 1.0 and cross["primary"] == "tencent" and not cross["multi_source"]
        and "兜底" in note, "T4 新浪不可用 → 腾讯兜底并标单源")

    _patch((None, "拉取失败"), (None, "拉取失败"))
    px, note, cross = rsp.fetch_price_now("512800")
    chk(px is None and cross["ok"] is False and cross["warn"],
        "T5 双源皆失败 → price=None 且告警（EV 走相对=100）")

    print("=" * 70)
    print("D-8 数据健康度四态")
    print("=" * 70)
    good_cross = {"ok": True, "multi_source": True, "primary": "sina",
                  "diff_pct": 0.0, "warn": None}
    ok = dh.assess(index_code="X", series_points=1200, freshness_days=1, pb_pct=42.0,
                   pb_sources={"a": 1, "b": 1}, price_cross=good_cross,
                   has_quant_raw=True, pb_pct_10y=40.0,
                   industry_metrics_present=True, event_leg_state="retrieved")
    chk(ok["status"] == "OK" and ok["watermark"] is None, "T6 全绿 → OK 且无水印")

    dg = dh.assess(index_code="X", series_points=1200, freshness_days=3, pb_pct=42.0,
                   pb_sources={"a": 1, "b": 1},
                   price_cross={"ok": True, "multi_source": False, "primary": "sina",
                                "diff_pct": None, "warn": "单源"},
                   has_quant_raw=True, pb_pct_10y=None,
                   industry_metrics_present=True, event_leg_state="retrieved")
    chk(dg["status"] == "DEGRADED" and dg["watermark"], "T7 单源+无10y → DEGRADED 带水印")
    chk(all(it["impact"] for it in dg["items"] if it["level"] == "DEGRADED"),
        "T7b 降级必量化：DEGRADED 每条都有 impact")

    ms = dh.assess(index_code="X", series_points=0, freshness_days=1, pb_pct=None,
                   pb_sources={}, price_cross=None, has_quant_raw=False)
    chk(ms["status"] == "MISSING", "T8 无序列 → MISSING")

    near = dh.assess(index_code="X", as_of="2026-10-25", series_points=1200,
                     freshness_days=1, pb_pct=50, pb_sources={"a": 1, "b": 1},
                     price_cross=good_cross, has_quant_raw=True, pb_pct_10y=40,
                     bvps_caliber_mutation=True,
                     caliber_events=[{"month": "2025-05", "pb_jump_pct": 20,
                                      "price_jump_pct": 0}],
                     industry_metrics_present=True, event_leg_state="retrieved")
    old = dh.assess(index_code="X", as_of="2026-10-25", series_points=1200,
                    freshness_days=1, pb_pct=50, pb_sources={"a": 1, "b": 1},
                    price_cross=good_cross, has_quant_raw=True, pb_pct_10y=40,
                    bvps_caliber_mutation=True,
                    caliber_events=[{"month": "2014-12", "pb_jump_pct": 30,
                                     "price_jump_pct": 0}],
                    industry_metrics_present=True, event_leg_state="retrieved")
    chk(near["status"] == "INVALID" and old["status"] == "DEGRADED",
        "T9 窗口内口径突变→INVALID / 窗口外→DEGRADED（防误杀）")

    print("=" * 70)
    print("T10 编排层真实路径（真跑 pipeline）")
    print("=" * 70)
    # T10 是集成用例：它真跑 run_sector_pipeline.py，需要本地派生数据且数据必须新鲜
    # （过期的 data/ 会被新鲜度闸门拒绝，返回 3 —— 那是数据状态，不是代码缺陷）。
    # 因此默认不跑，保持套件在任何环境结果一致；本地开发用 --with-integration 开启。
    if not RUN_INTEGRATION:
        print("  [SKIP] T10 为集成用例（真跑 pipeline + 需新鲜本地数据）；"
              "加 --with-integration 启用")
    elif not os.path.isdir(os.path.join(HERE, "data", "index_monthly")):
        chk(False, "T10 已要求集成模式，但 data/index_monthly 不存在")
    else:
        out = tempfile.mkdtemp(prefix="d7d8_")
        r = subprocess.run([sys.executable, "run_sector_pipeline.py", "--etf", "512800",
                            "--sector", "银行", "--out", out],
                           cwd=HERE, capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
        drafts = [f for f in os.listdir(out) if f.startswith("_draft_512800_")]
        ok_run = r.returncode == 0 and drafts
        chk(ok_run, f"T10a pipeline 退出码 {r.returncode} 且有草稿")
        if ok_run:
            d = json.load(open(os.path.join(out, drafts[0]), encoding="utf-8"))
            pc = d.get("_price_cross_check") or {}
            h = d.get("_data_health") or {}
            chk(pc.get("sources") and pc.get("ok") is True,
                f"T10b 草稿带双源明细（源={[s['name'] for s in pc.get('sources', [])]}）")
            chk(h.get("status") in dh.STATES and h.get("schema") == "D8-four-state/1",
                f"T10c 草稿带四态健康度（status={h.get('status')}）")
            chk(h.get("deferred") is False, "T10d 健康度非 deferred（真实评估而非未评估）")

    print("-" * 70)
    if FAILS:
        print(f"❌ 失败 {len(FAILS)} 项：")
        for f in FAILS:
            print("   -", f)
        return 1
    print("✅ D-7/D-8 注入式回归全通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
