#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
analyze_etf.py —— 单只 ETF 分析固定编排入口（2026-09-07 · 审议 RC3 根治）

背景（技能审议 2026-09-07）：
  机械段（run_sector_pipeline）与裁决段（discipline）都坚不可摧，但中间的
  「LLM 填表 → FILLS 合并 → guard 复核 → 引擎」环节过去靠会话内即兴脚本承担，
  先后踩坑：FILLS 漏注入（胜率 70→45 假象）、`_`键剥离遗忘（ValueError 返工）。
  本入口把四步固化为一条命令，LLM 不再有机会跳步/漏步。

流程：
  S1-S4  run_sector_pipeline 子进程产出机械草稿（pb 分位五档锁定预填）
  FILL   读 --fills 文件，REQUIRED_FILL_KEYS 完整性预检（缺→exit 4，逐项点名）
  MERGE  剥 `_`键 → 合并 FILLS（锁定字段保护：LLM 不得改写 Python 预填值）
         → 自动落 `_fill_meta`（判断字段 provenance，P2-⑨）
  GUARD  llm_fill_guard 四道机械复核（MUST→exit 6）
  ENGINE discipline.evaluate → verdict 落盘 runs/<date>/verdict_<tag>.json

用法：
  python analyze_etf.py --etf 159755 --sector 锂电池 --fills fills_159755.json
  python analyze_etf.py --index 980032.CNI --sector 锂电池 --fills fills.json --as-of 2026-09-05

退出码：0 完成（含引擎 PENDING，verdict 已落盘）
        2 数据过期（pipeline 透传）  4 输入错误（FILLS 缺失/改锁定字段）
        6 guard 拦截               5 pipeline 失败
"""
import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import discipline  # noqa: E402
import llm_fill_guard  # noqa: E402

# LLM 必须补齐的判断字段（未填即错 2026-09-07：引擎侧同样 MUST 拦截，此处前置快速反馈）
REQUIRED_FILL_KEYS = ["demand", "volume", "price", "forward", "imagination", "vp_basis"]
PIPELINE = os.path.join(HERE, "run_sector_pipeline.py")


def _latest_draft(out_dir: str, tag: str) -> str:
    cands = sorted(glob.glob(os.path.join(out_dir, f"_draft_{tag}_*.json")),
                   key=os.path.getmtime)
    if not cands:
        raise FileNotFoundError(f"--out {out_dir} 下未找到 _draft_{tag}_*.json")
    return cands[-1]


def run_pipeline(args) -> str:
    cmd = [sys.executable, PIPELINE, "--sector", args.sector, "--out", args.out]
    if args.etf:
        cmd += ["--etf", args.etf]
    else:
        cmd += ["--index", args.index]
    if args.as_of:
        cmd += ["--as-of", args.as_of]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    tail = (r.stdout or "") + (r.stderr or "")
    # 2026-10-07 修（P0 撞墙根因）：管线 exit=2 = 序列偏旧(stale) 但草稿仍正常产出，
    # 原代码把 exit 2 当致命失败 exit 5 中止——而 48/57 指数序列已 stale，几乎每次跑都撞墙。
    # 仅 3/4/5/6（缺序列/入参错/内部失败/guard 拦截）为致命；2 带警告继续。
    if r.returncode not in (0, 2):
        print(tail[-1500:], file=sys.stderr)
        sys.exit(5)
    if r.returncode == 2:
        print("[PIPELINE] ⚠️ 序列偏旧(stale)：草稿已产出但分位带可能漂移，建议 "
              "data_refresh.py --refresh 刷新后重跑；本次继续以现有草稿裁决。", file=sys.stderr)
    tag = (args.etf or args.index).split(".")[0]
    return _latest_draft(args.out, tag)


def _emit_fills_template(args, draft) -> str:
    """写出"待填判断字段"起步模板（2026-10-07 增·P1）。

    消除 LLM 手搓 FILLS JSON 的字段名错误 → analyze_etf 缺字段 exit4 重跑的多轮摩擦。
    - 仅含判断字段（vp_basis 已默认填好，它是 gate_industry_cycle 唯一合法值）；
    - 锁定的机械字段(pb_pct/p5~p95…)由草稿带入，模板不重复；
    - 若管线已机械装配 quant_raw.price_series，原样保留，避免合并时被覆盖。
    """
    tag = (args.etf or args.index).split(".")[0]
    as_of = draft.get("as_of") or date.today().isoformat()
    tpl = {
        "_comment": (
            f"填写下列判断字段（机械字段已由草稿锁定，勿重复填）后运行："
            f"python analyze_etf.py --etf {args.etf or ''} --sector {args.sector} "
            f"--fills 本文件 --out {args.out}"),
        "vp_basis": "industry_cycle",   # gate_industry_cycle 唯一合法值，默认填好
        "demand": None, "volume": None, "price": None, "forward": None, "imagination": None,
        "industry_volume_metric": None, "industry_price_metric": None,
        "forward_evidence": [],          # forward≠flat 时必须≥1条 {type,desc,source}
        "bottom_signals": 0, "bottom_signal_detail": [],
        "crowding": None, "catalyst": None, "catalysts": [],
        "dual_price_data": None,         # 分歧行业且 price=up 时 MUST
        "quant_raw": {"volume_yoy": None, "inventory_yoy": None, "utilization": None,
                      "fund_flow_pct": None, "fund_turn_pct": None, "earning_rev_pct": None},
        "_fill_meta": {},                # 每个判断字段附判断依据（行业数据/研报来源）
    }
    # 保留管线已装配的价格序列（合并时 fills 覆盖 draft，故原样带出）
    src_qr = draft.get("quant_raw") or {}
    if src_qr:
        # D-5②（2026-10-25）：机械回填的行业量价客观指标（妙想NL）须原样保留，
        # 不因模板默认 None 而被抹掉（LLM 不得静默改写客观值）；其余非指标键照旧带出。
        for k, v in src_qr.items():
            if k not in ("volume_yoy", "inventory_yoy", "utilization",
                         "fund_flow_pct", "fund_turn_pct", "earning_rev_pct") or v is not None:
                tpl["quant_raw"][k] = v
        tpl["quant_raw"]["_note"] = ("price_series 已由管线机械装配(data/etf 缓存)，"
                                     "其余 5 个行业客观指标须带 provenance 补")
    path = os.path.join(args.out, f"fills_{tag}_{as_of}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(tpl, f, ensure_ascii=False, indent=2)
    return path


def _preflight_gate(fills_path: str | None = None) -> None:
    """🔴 运行前体检硬闸（2026-10-07 立）——**确保任何 LLM 进门先体检，不是靠自觉**。

    背景：两只 ETF 纯机器链路实测 3005ms，端到端却约 1h（放大 ~1200×）。当天撞的 9 堵墙
    **9/9 均可在运行前静态检出**。若体检只写在 SKILL.md 里（劝导），下一个会话照样会绕过去
    重新撞一遍——所以必须做成**代码路径上的硬闸**，而非文档里的建议。

    设计：
    - 走 `preflight_etf.enforce()` 内联执行（不起子进程），耗时 ~2s，相对 1h 可忽略。
    - 检出 MUST → **exit 3 阻断**（fail-closed）。**不提供 --skip-preflight 逃生门**。
    - 体检**自身异常** → 仅警告放行（fail-open）：工具坏了不能反过来成为新的阻断源。
    ⚠️ 注意：防重入标志 `_XYD_PREFLIGHT_DONE` 的**读与写必须只在 preflight_etf.enforce()
    内部各一次**。若在此先置 1 再调 enforce，enforce 首行检查就会为“真”而直接 return，
    使本闸变成 100% 空转的死闸（2026-10-07 红队注入测试抓出的自锁 bug）。
    """
    try:
        sys.path.insert(0, HERE)
        import preflight_etf as pf  # noqa: E402
    except Exception as e:  # pragma: no cover
        print(f"[PREFLIGHT] ⚠️ 体检模块不可用（{type(e).__name__}: {e}）→ 跳过，不阻断分析",
              file=sys.stderr)
        return

    # FILLS 冲突：优先精确检查本次指定文件
    extra_msgs: list[str] = []
    if fills_path and os.path.exists(fills_path):
        try:
            with open(fills_path, encoding="utf-8") as f:
                j = json.load(f)
            bad = sorted(pf.LOCKED_FIELDS & set(j))
            if bad:
                extra_msgs.append(
                    f"本次 --fills {os.path.basename(fills_path)} 含机械锁定字段 {bad} "
                    f"→ 合并必被 llm_fill_guard 拦（exit 4）。请清空这些键（草稿会自动派生）")
        except Exception as e:
            extra_msgs.append(f"FILLS 读取失败: {e}")
    pf.enforce(extra_msgs=extra_msgs)


def main() -> int:
    ap = argparse.ArgumentParser(description="XYD 供需板块 · 单只 ETF 固定编排入口")
    ap.add_argument("--etf", help="ETF 代码（经 etf_index_map 解析）")
    ap.add_argument("--index", help="直接指定指数代码（如 980032.CNI）")
    ap.add_argument("--sector", required=True, help="板块名")
    ap.add_argument("--fills", help="LLM 已填判断字段 JSON（缺省=仅出草稿并打印缺项清单）")
    ap.add_argument("--as-of", dest="as_of", help="数据日期 YYYY-MM-DD")
    ap.add_argument("--out", default=os.path.join(HERE, "runs", date.today().isoformat()),
                    help="产物目录（缺省 runs/<今日>/）")
    args = ap.parse_args()
    if not args.etf and not args.index:
        ap.error("--etf 或 --index 必须给一个")
    os.makedirs(args.out, exist_ok=True)

    # 🔴 运行前体检硬闸：必须在任何实质动作之前（2026-10-07 · 防 LLM 反复撞墙）
    _preflight_gate(args.fills)

    # ---- S1-S4 机械草稿 ----
    draft_path = run_pipeline(args)
    with open(draft_path, encoding="utf-8") as f:
        draft = json.load(f)
    tag = (args.etf or args.index).split(".")[0]
    print(f"[S1-S4] 草稿: {os.path.relpath(draft_path, HERE)}  锁定字段 {len(draft.get('_locked_mechanical', []))} 项")

    if not args.fills:
        tpl_path = _emit_fills_template(args, draft)
        print(f"[FILL] ✅ 已自动生成 FILLS 起步模板: {os.path.relpath(tpl_path, HERE)}")
        print("       填好判断字段后运行：")
        print(f"       python analyze_etf.py --etf {args.etf or ''} --sector {args.sector} "
              f"--fills {os.path.relpath(tpl_path, HERE)} --out {args.out}")
        print("[FILL] 须补齐的判断字段（未填即错，引擎亦会 MUST 拦截）：")
        for g in draft.get("_llm_required", []):
            print("  -", g)
        return 0

    # ---- FILL 完整性预检 ----
    try:
        with open(args.fills, encoding="utf-8") as f:
            fills = json.load(f)
    except Exception as e:
        print(f"[FILL] ❌ FILLS 文件不可读/非法 JSON: {args.fills}（{e}；exit 4）", file=sys.stderr)
        return 4
    if not isinstance(fills, dict):
        print("[FILL] ❌ FILLS 须为 JSON 对象（字段->值）；exit 4", file=sys.stderr)
        return 4
    missing = [k for k in REQUIRED_FILL_KEYS
               if fills.get(k) in (None, "", []) and draft.get(k) in (None, "")]
    if missing:
        print(f"[FILL] ❌ FILLS 缺少必填判断字段: {missing}（exit 4；未填即错，引擎将 MUST 拦截）", file=sys.stderr)
        return 4
    if str(fills.get("forward") or draft.get("forward") or "flat") != "flat" \
            and not (fills.get("forward_evidence") or draft.get("forward_evidence")):
        print("[FILL] ❌ forward≠flat 但 FILLS 无 forward_evidence（F2 MUST；exit 4）", file=sys.stderr)
        return 4

    # ---- MERGE：剥 `_`键 + 锁定字段保护 + provenance ----
    locked = set(draft.get("_locked_mechanical") or [])
    override = [k for k in locked if k in fills and fills[k] != draft.get(k)]
    if override:
        print(f"[MERGE] ❌ FILLS 试图改写 Python 锁定机械字段: {override}（G4 纪律，LLM 不得改写；exit 4）",
              file=sys.stderr)
        return 4
    cleaned = {k: v for k, v in draft.items() if not str(k).startswith("_")}
    fill_meta = dict(fills.get("_fill_meta") or {})
    for k, v in fills.items():
        if str(k).startswith("_"):
            continue
        if k in locked:
            continue  # 锁定字段以 Python 预填为准
        cleaned[k] = v
        fill_meta.setdefault(k, f"fills-file:{os.path.basename(args.fills)}")
    cleaned["_fill_meta"] = fill_meta  # P2-⑨：判断字段 provenance 随案留痕

    # ---- GUARD ----
    res = llm_fill_guard.run(cleaned)
    print(llm_fill_guard.render(res))
    if not res.get("ok", False):
        print("[GUARD] ❌ 机械复核存在 MUST 未过项（exit 6），结论不得进入引擎决策。",
              file=sys.stderr)
        return 6

    # ---- ENGINE ----
    # 草稿是「超集」（含 D-4/D-6 等客观元数据字段 bvps_caliber_mutation/event_leg，
    # 供 LLM/人工研判用）；引擎 SectorInput 是「严格子集」契约（from_dict 拒绝未知字段，
    # 防主观篡改）。编排层在此把草稿收敛为引擎认得的字段集合，避免客观元数据字段被引擎拒收
    # （2026-10-25 修正：D-4/D-6 落地后遗留的端到端断裂，此前未跑过 end-to-end 故未暴露）。
    _eng_allowed = set(getattr(discipline.SectorInput, "__dataclass_fields__", {}))
    engine_input = {k: x for k, x in cleaned.items()
                    if not str(k).startswith("_") and k in _eng_allowed}
    v = discipline.evaluate(engine_input)
    # D-8（2026-10-25）：数据健康度四态水印——取自草稿（编排层已按本次实际取数评估）。
    # 只做标注：附加到 verdict 顶层 + 文本摘要，不参与胜率/EV/闸门计算。
    _dh = draft.get("_data_health") or {}
    if _dh:
        _dh = dict(_dh)
        _dh["applied_stage"] = "analyze_etf"
        v["_data_health"] = _dh
    verdict_path = os.path.join(args.out, f"verdict_{tag}.json")
    with open(verdict_path, "w", encoding="utf-8") as f:
        json.dump(v, f, ensure_ascii=False, indent=1)
    if _dh.get("watermark"):
        print("\n" + _dh["watermark"])
        for it in _dh.get("items", []):
            if it.get("level") == _dh.get("status"):
                print(f"   · [{it['code']}] {it['msg']}")
                if it.get("impact"):
                    print(f"     影响：{it['impact']}")
    print(discipline.render_text(v))
    print(f"\n[ENGINE] verdict 已落盘: {os.path.relpath(verdict_path, HERE)}  status={v['status']}")
    if v["status"] != "OK":
        print("[ENGINE] ⚠ 非 OK 状态（PENDING/REJECTED）：结论不得用于决策，blockers 见上。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
