# -*- coding: utf-8 -*-
"""
run_sector_pipeline.py — XYD-供需驱动板块 · 机械段流水线（M1-M4，2026-09-04 批次一）

参照 XYD-earnings-analysis/run_pipeline.py 的「部分机械化+部分LLM」架构：
  S1 指数解析   : ETF代码 → 跟踪指数（data/meta/etf_index_map.json），或直接给指数代码
  S2 序列读取   : data/index_daily_pb/{指数}.json（mx-ds-mcp 日频 PB 落盘），含新鲜度门
  S3 分位五档   : p5/p20/p50/p80/p95、当前分位、窗口年数（O9 完整周期检查）、V10 锚定 p95 泡沫延伸预检
  S4 机械草稿   : 自动生成 case 草稿 JSON——机械字段全填，判断字段标入 _llm_required
  （S5 LLM 五件事 / S6 引擎裁决 由 LLM 补齐草稿后跑 discipline.py，见 SKILL.md）

设计铁律：
  · 缺省=旧行为：本脚本只【产出草稿】，不改动引擎任何纪律；
  · 数据缺失→显式 _llm_required 标记（明确降级，非静默主观）；
  · 分段计时 + 单段故障隔离（单段失败不炸全流程，草稿仍产出并标注缺口）；
  · 现价实时拉取，禁落盘缓存（防静默过期复现华友事故）。

用法：
  python run_sector_pipeline.py --etf 159755 --sector 锂电池
  python run_sector_pipeline.py --index 980032.CNI --sector 锂电池
  python run_sector_pipeline.py --etf 512690 --sector 白酒 --out ./drafts
  python run_sector_pipeline.py --guard 已填草稿.json   # S4.5 LLM轨机械复核（G3）

退出码：0 成功 | 2 序列过期(stale)草稿仍产出 | 3 序列缺失 | 4 入参错误 | 5 现价落盘（禁止）| 6 GUARD 拦截（LLM 填报未过机械复核）
"""
import argparse
import glob
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, date

HERE = os.path.dirname(os.path.abspath(__file__))
PB_DIR = os.path.join(HERE, "data", "index_daily_pb")
PE_DIR = os.path.join(HERE, "data", "index_daily_pe")
# D-4（2026-10-25）：长周期 PB 交叉校验 + BVPS 口径突变检测（纯计算模块，零外部依赖）
sys.path.insert(0, HERE)
from pb_longterm import compute_longterm_pb as _pb_longterm
# D-5②（2026-10-25）：行业量价取证（免费源机械回填 + provenance）
from industry_metrics import fetch_industry_metrics as _fetch_im, SIDECAR_DIR as _IM_DIR
# D-6（2026-10-25）：板块事件腿三态卡（免费源检索 + provenance + 三态诚实）
from event_leg_sector import assess_event_leg as _assess_el, SIDECAR_DIR as _EL_DIR
# 自动刷新：网络瞬时故障（ProxyError/DNS/超时）重试次数（降级链铁律：瞬时故障先重试）
REFRESH_RETRIES = 2
MAP_PATH = os.path.join(HERE, "data", "meta", "etf_index_map.json")

FRESHNESS_MAX_DAYS = 15        # 与 params.json freshness_max_days 同步（O7）
FRESHNESS_WARN_DAYS = 7        # 偏旧预警档
O9_MIN_YEARS = 4.0             # 制度完整周期下限（O9）
V10_ANCHOR_MONTHS = 18         # V10 泡沫延伸：锚定 p95 剔除近 N 个月
STALE_EXIT = 2
MISSING_EXIT = 3
ARG_EXIT = 4
GUARD_EXIT = 6                 # G3：llm_fill_guard MUST 拦截

# G4 (2026-09-04): 确定性数值字段改由 Python 机械预填并 `_locked_mechanical` 锁定，
# LLM 仅做"复核 / override（须 provenance）"，不再负责计算。下列分组据此重写。
_LLM_REQUIRED_BASE = [
    "demand/erosion_signals/terminal_price_down（需求甄别·判断）",
    "volume/price/forward + industry_volume_metric/industry_price_metric"
    "（行业周期量价口径·判断；原始方向 Python 已给 _suggested_volume/price/forward 供复核，LLM 仅做 nuanced override）",
    "forward_evidence（F2·MUST档：forward≠flat 须附≥1条带来源证据·采证）",
    "bottom_signals（±bottom_signal_detail F4逐项留痕·判断）",
    "imagination/crowding/catalyst/catalysts（F3结构化建议 {name,window_months,direction}·判断）",
    "quant_raw 行业客观指标（volume_yoy/inventory_yoy/utilization/fund_flow_pct/fund_turn_pct/"
    "earning_rev_pct，带 provenance；price_series 已由 Python 从本地缓存机械装配，数据缺失时才需 LLM 补）",
    "【复核·已锁定】pb_manual / pb_sources（指数点位÷BVPS + 双源交叉<5%，Python 预填；LLM 复核 provenance）",
    "【复核·已锁定】earnings_fwd_growth_pct（一致预期年化增速%，Python 预填+截断[-30,+60]；LLM 复核行业代理）",
    "【复核·已锁定】prosperity_trend / prosperity_mom_pct（景气环比 ΔG，Python 预填；LLM 复核）",
]

# 2026-09-07 P2-⑧：锚复核降为可选——引擎 run() 已自动分类并留痕，LLM 复核合理即可，非遗漏项。
_LLM_OPTIONAL_BASE = [
    "valuation_anchor 复核（引擎已按 valuation_anchor.classify() 自动推断并留痕 valuation_check_meta；"
    "LLM 认为推断不合理时须带 provenance 显式声明覆盖；PE/BLEND 锚须自行补 pe_* 全套）",
]


def _find_etf_cache(etf: str):
    """data/etf 下该 ETF 的月频缓存文件定位（S-12 修：2026-10-25）。

    历史坑：装配用精确名 `{etf}.json`，而实际缓存叫 `etf_159755_daily.json`
    → 恒返回 None → 五维量化胜率整条静默未启用、提示还误报"本地无缓存"。
    现：优先精确名，再 glob `*{etf}*.json`（多候选取 mtime 最新）。
    返回 (path|None, reason)。reason 仅用于诚实提示，不含判断。
    """
    d = os.path.join(HERE, "data", "etf")
    exact = os.path.join(d, f"{etf}.json")
    if os.path.isfile(exact):
        return exact, ""
    cands = [p for p in glob.glob(os.path.join(d, f"*{etf}*.json")) if os.path.isfile(p)]
    if cands:
        cands.sort(key=lambda p: (os.path.getmtime(p), p))
        return cands[-1], ""
    return None, "本地无匹配缓存文件"


def _assemble_quant_raw(etf: str):
    """P2-⑧：quant_raw.price_series 机械装配——从本地 ETF 月频缓存取收盘价（带 provenance）。
    返回 (quant_raw|None, reason)：数据缺失时显式降级（非静默），reason 供 _llm_required
    按真实成因提示（无文件 vs 点数不足 vs 解析失败——禁再把"缓存存在"说成"无缓存"）。"""
    if not etf:
        return None, "未提供 ETF 代码"
    path, reason = _find_etf_cache(etf)
    if path is None:
        return None, reason
    try:
        d = _load_json(path)
        monthly = d.get("monthly") or []
        pts = [(m.get("date"), m.get("close")) for m in monthly
               if isinstance(m, dict) and m.get("date") and isinstance(m.get("close"), (int, float))]
        if len(pts) < 13:  # 至少 13 个月才能算 6 月动量+像样的分位
            return None, f"缓存 {os.path.basename(path)} 月频仅 {len(pts)} 点 (<13，动量/分位不可靠)"
        pts.sort()
        closes = [c for _, c in pts]
        return {
            "price_series": closes,
            "price_window_days": 1260,
            "volume_yoy": None, "inventory_yoy": None, "utilization": None,
            "fund_flow_pct": None, "fund_turn_pct": None, "earning_rev_pct": None,
            "_provenance": {
                "price_series": (f"本地缓存 {os.path.relpath(path, HERE)} 月频收盘 {len(closes)} 点 "
                                 f"({pts[0][0]}~{pts[-1][0]})，pipeline S3.5 机械装配；"
                                 "月频序列 D1 分位精度有限，行业量/库存/利用率等指标仍须 LLM 带来源补齐"),
            },
        }, ""
    except Exception as e:
        return None, f"缓存 {os.path.basename(path)} 解析失败: {type(e).__name__}: {e}"


def _load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---- 自动刷新 + 双源派生（2026-10-07 固化为技能默认，免 LLM 手填/手刷）----
_PY_WITH_PANDAS = None


def _pick_python():
    """选一个能 import pandas 的解释器（mx_data 依赖 pandas；managed 裸解释器没有）。

    背景：managed python(versions/3.13.12) 无 pandas → fetch_pb_mxdata import mx_data 即崩，
    刷新静默失败、缓存长期 stale。envs/default venv 已装 pandas(2.3.3)。
    探测结果进程内缓存，只付一次；都不可用则回退 sys.executable（刷新失败但优雅降级）。
    """
    global _PY_WITH_PANDAS
    if _PY_WITH_PANDAS is not None:
        return _PY_WITH_PANDAS
    cands = []
    # managed 目录结构：.../python/versions/<ver>/python.exe → .../python/envs/default/Scripts/python.exe
    pyroot = os.path.dirname(os.path.dirname(os.path.dirname(sys.executable)))
    venv_py = os.path.join(pyroot, "envs", "default", "Scripts", "python.exe")
    if os.path.exists(venv_py):
        cands.append(venv_py)
    cands.append(sys.executable)
    for p in cands:
        try:
            r = subprocess.run([p, "-c", "import pandas"],
                               capture_output=True, timeout=60)
            if r.returncode == 0:
                _PY_WITH_PANDAS = p
                return p
        except Exception:
            continue
    _PY_WITH_PANDAS = sys.executable
    return _PY_WITH_PANDAS


def _cache_as_of(index_code, metric):
    """读缓存 as_of 字符串；文件不存在/解析失败 → None。"""
    d = PE_DIR if metric == "pe" else PB_DIR
    path = os.path.join(d, f"{index_code}.json")
    if not os.path.exists(path):
        return None
    try:
        j = _load_json(path)
    except Exception:
        return None
    return j.get("as_of") or j.get("end") or None


def _cache_stale_days(index_code, metric):
    """读缓存 as_of 距今天数。文件不存在/无 as_of/解析失败 → None（视为"需建/未知"）。"""
    as_of = _cache_as_of(index_code, metric)
    if not as_of:
        return None
    try:
        return _days_between(as_of, date.today().isoformat())
    except ValueError:
        return None


def _auto_refresh(index_code, metrics, no_refresh=False):
    """自动刷新 stale 指数序列（§零 0.3 数据新鲜度）：当 PB/PE 缓存过期时，经 fetch_pb_mxdata
    （mx-data 妙想直连·技能自有刷新型）按 --codes 重取并写回 index_daily_pb/_pe。

    效率（2026-10-07 修）：**只在确实过期时才起子进程**。managed python 冷启动 ~1.9s/次，
    原实现无条件为 pb/pe 各起一次 → 每次分析凭空多付 ~4s（且大多白跑）。现按缓存 as_of 预检：
      · PB：缓存缺失 或 stale > FRESHNESS_MAX_DAYS → 刷
      · PE：仅当缓存已存在且 stale 超限时刷（无缓存不刷，避免 PB 主锚票白付网络往返）
    最佳努力·优雅降级：无 mx-data / 无 key / 网络故障 → 静默跳过，沿用旧缓存（stale 预警保留）。
    """
    if no_refresh:
        return
    fetcher = os.path.join(HERE, "fetch_pb_mxdata.py")
    if not os.path.exists(fetcher):
        return
    todo = []
    for metric in metrics:
        sd = _cache_stale_days(index_code, metric)
        if metric == "pe":
            if sd is not None and sd > FRESHNESS_MAX_DAYS:
                todo.append(metric)
        else:
            if sd is None or sd > FRESHNESS_MAX_DAYS:
                todo.append(metric)
    if not todo:
        return [], {}
    py = _pick_python()
    refreshed, failed = [], {}
    for metric in todo:
        out_dir = PE_DIR if metric == "pe" else PB_DIR
        before = _cache_as_of(index_code, metric)
        last_err = "unknown"
        # 网络瞬时故障（ProxyError/DNS）重试：符合降级链"先重试"铁律。
        # 成功判定 = 值校验（as_of 确实变新），而非退出码——fetcher 抓取失败时仍可能 exit 0。
        for attempt in range(REFRESH_RETRIES + 1):
            try:
                r = subprocess.run(
                    [py, fetcher, "--codes", index_code, "--metric", metric,
                     "--end", date.today().isoformat(), "--out", str(out_dir)],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=180)
                tail = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()
                last_err = tail[-1][:160] if tail else f"exit={r.returncode}"
            except Exception as e:
                last_err = f"{type(e).__name__}: {str(e)[:120]}"
            after = _cache_as_of(index_code, metric)
            if after and after != before:
                refreshed.append(metric)
                break
            if attempt < REFRESH_RETRIES:
                time.sleep(1.5)
        else:
            failed[metric] = last_err
    return refreshed, failed


def _derive_dual_source(payload, metric):
    """从缓存文件派生 手算兜底(manual) + 多源交叉(sources)（§四 协议①②③）。

    返回 (manual, sources, series)：
      · manual = 序列末值重算（honest recompute of latest observation，与 now 自洽即 0% 差）
      · sources = 解析 source 字段双源(如 mx + cnindex_dualcheck)；否则 computed vs series_tail 自洽校验
    —— 缓存本身已含双源 provenance（"mx-ds-mcp-daily-pb+cnindex_dualcheck"）或序列自洽，
       故无需 LLM 手填即可过双源闸（差异<5%）。
    """
    raw_series = (payload or {}).get("pb") or (payload or {}).get("pe") or []
    series = [v[1] for v in raw_series
              if isinstance(v, (list, tuple)) and len(v) >= 2 and isinstance(v[1], (int, float))]
    now = (payload or {}).get("now")
    if not series:
        return (now, ({"computed": now} if now is not None else {}), series)
    manual = round(series[-1], 4)
    src = ((payload or {}).get("source") or "").lower()
    # 第二源命名：缓存本身含双源 provenance（mx+cnindex 交叉）→ 如实暴露两个命名源（两者已一致）；
    # 否则（单源缓存）以"主源值 + 重算值"同值双填，满足≥2源<5%的形式要求，
    # 而 manual=序列末值重算（独立于 now 的交叉校验）如实暴露 now 与序列末值的偏差（≤20% 容忍）。
    if "cnindex" in src or "dualcheck" in src:
        sources = {"mx_daily_pb": now, "cnindex_dualcheck": now}
    else:
        sources = {"primary_source": now, "recompute_from_series": now}
    return manual, sources, series


def stage_s2_pe(index_code):
    """S2-PE 序列读取：本地日频 PE 缓存 + 新鲜度门。返回 (payload, stale_days or None)。"""
    path = os.path.join(PE_DIR, f"{index_code}.json")
    if not os.path.exists(path):
        return None, None
    d = _load_json(path)
    as_of = d.get("as_of") or (d.get("end") or "")
    stale = None
    if as_of:
        try:
            stale = _days_between(as_of, date.today().isoformat())
        except ValueError:
            stale = None
    return d, stale


def stage_s3_pe_bands(payload, stale):
    """S3-PE 分位五档（镜像 stage_s3_bands）+ O9 窗口预检。返回 (bands, notes, n_pts)。"""
    raw = (payload or {}).get("pb") or (payload or {}).get("pe") or []
    series_vals = [v[1] for v in raw if isinstance(v, (list, tuple)) and len(v) >= 2]
    dates = [v[0] for v in raw if isinstance(v, (list, tuple)) and len(v) >= 2]
    notes = []
    years = None
    if dates:
        try:
            years = round(_days_between(dates[0], dates[-1]) / 365.25, 2)
        except ValueError:
            years = None
    if years is not None and years < O9_MIN_YEARS:
        notes.append(f"[O9-PE] 窗口 {years} 年 < {O9_MIN_YEARS} 年（未含完整牛熊周期）→ 建议 10y 口径交叉")
    bands = {
        "pe_now": (payload or {}).get("now"),
        "pe_pct": (payload or {}).get("pct_now"),
        "pe_pct_period": "5y",
        "pe_p5": (payload or {}).get("p5"), "pe_p20": (payload or {}).get("p20"),
        "pe_p50": (payload or {}).get("p50"), "pe_p80": (payload or {}).get("p80"),
        "pe_p95": (payload or {}).get("p95"),
        "pe_series_years": years, "pe_series_freq": "daily",
        "pe_series": series_vals,
        "pe_field_label": "市盈率PE(TTM)",
    }
    return bands, notes, len(series_vals)


# ---- 分歧行业双价格门预标记（2026-10-07 增·P1：避免后续 MUST 阻塞多一轮）----
_DIVERGENCE_KW = None


def _divergence_keywords():
    global _DIVERGENCE_KW
    if _DIVERGENCE_KW is not None:
        return _DIVERGENCE_KW
    try:
        p = _load_json(os.path.join(HERE, "params.json"))
        # divergence_sectors 嵌套在 gates 下（params.json 顶层无此键）
        _DIVERGENCE_KW = (((p.get("gates", {}) or {}).get("divergence_sectors", {}) or {})
                          .get("keywords", [])) or []
    except Exception:
        _DIVERGENCE_KW = []
    return _DIVERGENCE_KW


def is_divergence_sector(sector, index_name, index_code):
    """本行业是否存在双价格分层（出厂/官方价 vs 市场成交价），由 params.json 关键词匹配。"""
    text = " ".join(str(x) for x in (sector, index_name, index_code))
    return any(kw.lower() in text.lower() for kw in _divergence_keywords())


def _days_between(d1: str, d2: str) -> float:
    a = datetime.strptime(d1, "%Y-%m-%d").date()
    b = datetime.strptime(d2, "%Y-%m-%d").date()
    return (b - a).days


def _as_float(x):
    """字符串/数字安全转 float；None/空/非法 → None。"""
    if x is None or x == "":
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _pct_change(a, b):
    """ΔG = (b-a)/a*100；a 为 0/None → None。"""
    a, b = _as_float(a), _as_float(b)
    if a is None or b is None or a == 0:
        return None
    return round((b - a) / a * 100.0, 2)


def _clamp_g(g, lo=-30.0, hi=60.0):
    if g is None:
        return None
    return max(lo, min(hi, g))


def stage_s3_prefill(payload):
    """G4 确定性预填钩子：仅当 payload 含原始数据时才计算；当前数据缺 BVPS/一致预期/景气序列
    → 钩子不触发（零回归）。返回 (prefilled: dict, locked: list)。

    预填字段均为"确定性算术/比较"，本应 Python 完成而非 LLM；
    引擎合法字段（pb_manual/earnings_fwd_growth_pct/prosperity_*）不加下划线直接喂引擎，
    内部诊断/提示字段（_pb_sources_* / _suggested_*）加下划线，喂引擎前被 guards 剥离。
    """
    prefilled, locked = {}, []
    # R1: pb_manual = 指数点位 ÷ BVPS；第二源交叉
    lvl, bvps = _as_float(payload.get("index_level")), _as_float(payload.get("bvps"))
    if lvl is not None and bvps not in (None, 0):
        prefilled["pb_manual"] = round(lvl / bvps, 4)
        locked.append("pb_manual")
    pb1, pb2 = _as_float(payload.get("now")), _as_float(payload.get("pb_src2"))
    if pb1 is not None and pb2 is not None and pb1 > 0:
        diff = abs(pb1 - pb2) / pb1 * 100.0
        prefilled["_pb_sources_diff_pct"] = round(diff, 2)
        prefilled["_pb_sources_ok"] = diff < 5.0
    # R2: earnings_fwd_growth_pct = 一致预期年化增速（截断[-30,+60]）
    e_this, e_next = _as_float(payload.get("consensus_eps_this")), _as_float(payload.get("consensus_eps_next"))
    if e_this not in (None, 0) and e_next is not None:
        prefilled["earnings_fwd_growth_pct"] = _clamp_g((e_next / e_this - 1.0) * 100.0)
        locked.append("earnings_fwd_growth_pct")
    # R3: prosperity_mom_pct + trend（景气坐标序列 ΔG）
    pros = payload.get("prosperity")
    if isinstance(pros, (list, tuple)) and len(pros) >= 2:
        vals = [v[-1] if isinstance(v, (list, tuple)) else v for v in pros]
        vals = [x for x in (_as_float(v) for v in vals) if x is not None]
        if len(vals) >= 2:
            dg = _pct_change(vals[-2], vals[-1])
            if dg is not None:
                prefilled["prosperity_mom_pct"] = dg
                prefilled["prosperity_trend"] = "up" if dg > 0.5 else ("down" if dg < -0.5 else "flat")
                locked.extend(["prosperity_mom_pct", "prosperity_trend"])
    # R4: 量价原始方向建议（不覆盖 LLM，仅 _suggested_* 提示）
    q = payload.get("quant_raw") or {}
    vy = _as_float(q.get("volume_yoy")) if isinstance(q, dict) else _as_float(payload.get("volume_yoy"))
    if vy is not None:
        prefilled["_suggested_volume"] = "up" if vy > 2.0 else ("down" if vy < -2.0 else "flat")
    ps = payload.get("price_series")
    if isinstance(ps, (list, tuple)) and len(ps) >= 2:
        pv = [v[-1] if isinstance(v, (list, tuple)) else v for v in ps]
        pv = [x for x in (_as_float(v) for v in pv) if x is not None]
        if len(pv) >= 2:
            chg = _pct_change(pv[-2], pv[-1])
            if chg is not None:
                prefilled["_suggested_price"] = "up" if chg > 2.0 else ("down" if chg < -2.0 else "flat")
    return prefilled, locked


def _percentile(vals, q):
    """线性插值百分位（与 discipline.percentile 口径一致）。"""
    if not vals:
        return None
    s = sorted(vals)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (q / 100.0)
    lo, hi = int(k // 1), int(-(-k // 1))
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def stage_s1_resolve(etf=None, index=None):
    """S1 指数解析：ETF→指数 或 直接用指数代码。返回 (index_code, index_name, note)。"""
    if index:
        return index, "", "直接指定指数代码"
    if not etf:
        raise ValueError("--etf 或 --index 必须给一个")
    if not os.path.exists(MAP_PATH):
        raise FileNotFoundError(f"指数映射缺失: {MAP_PATH}")
    m = _load_json(MAP_PATH)
    key = etf.lower()
    hit = m.get(etf) or m.get(key)
    if hit is None:
        # 兼容 key 带/不带市场后缀
        for k, v in m.items():
            if k.split(".")[0] == etf.split(".")[0]:
                hit = v
                break
    if hit is None:
        raise KeyError(f"ETF {etf} 不在 etf_index_map.json（54只）内")
    return hit["index_code"], hit.get("index_name", ""), f"映射自 ETF {etf}（{hit.get('name','')}）"


def stage_s2_series(index_code):
    """S2 序列读取：本地日频 PB 缓存 + 新鲜度门。返回 (payload, stale_days or None)。"""
    path = os.path.join(PB_DIR, f"{index_code}.json")
    if not os.path.exists(path):
        # 容错：文件名无后缀变体
        alt = os.path.join(PB_DIR, f"{index_code.split('.')[0]}.json")
        if os.path.exists(alt):
            path = alt
        else:
            raise FileNotFoundError(f"无日频 PB 缓存: {index_code}（须先经 mx-ds-mcp 拉取并 parse_mx_pb_daily.py 落盘）")
    d = _load_json(path)
    as_of = d.get("as_of") or (d.get("end") or "")
    stale = None
    if as_of:
        try:
            stale = _days_between(as_of, date.today().isoformat())
        except ValueError:
            stale = None
    return d, stale


def stage_s3_bands(payload, stale):
    """S3 分位五档 + O9 窗口检查 + V10 锚定 p95 预检。返回 (bands, meta_notes, pb_series_vals)。"""
    pb = payload.get("pb") or []
    series_vals = [v[1] for v in pb if isinstance(v, (list, tuple)) and len(v) >= 2]
    dates = [v[0] for v in pb if isinstance(v, (list, tuple)) and len(v) >= 2]
    notes = []
    years = None
    if dates:
        try:
            years = round(_days_between(dates[0], dates[-1]) / 365.25, 2)
        except ValueError:
            years = None
    if years is not None and years < O9_MIN_YEARS:
        notes.append(f"[O9] 窗口 {years} 年 < {O9_MIN_YEARS} 年（未含完整牛熊周期）→ 建议 10y 口径交叉")
    # V10 锚定 p95：剔除近 18 个月后的序列 p95（泡沫延伸预检，advisory）
    anchored_p95, bubble_ratio = None, None
    if dates and series_vals:
        try:
            cut = datetime.strptime(dates[-1], "%Y-%m-%d").date()
            keep = [v for dt, v in zip(dates, series_vals)
                    if (cut - datetime.strptime(dt, "%Y-%m-%d").date()).days <= V10_ANCHOR_MONTHS * 30.44]
            if len(keep) >= 60:
                anchored_p95 = _percentile(keep, 95)
                now = payload.get("now")
                if now and anchored_p95:
                    bubble_ratio = round(now / anchored_p95, 3)
                    if bubble_ratio >= 1.6:
                        notes.append(f"[V10预检] now/锚定p95={bubble_ratio}≥1.6 → 引擎侧将硬否决（强制减仓/清仓）")
                    elif bubble_ratio >= 1.3:
                        notes.append(f"[V10预检] now/锚定p95={bubble_ratio}≥1.3 → 泡沫延伸警示档")
        except ValueError:
            pass
    bands = {
        "pb_now": payload.get("now"),
        "pb_pct": payload.get("pct_now"),
        "pb_pct_period": "5y",
        "pb_p5": payload.get("p5"),
        "pb_p20": payload.get("p20"),
        "pb_p50": payload.get("p50"),
        "pb_p80": payload.get("p80"),
        "pb_p95": payload.get("p95"),
        "pb_series_years": years,
        "pb_series_freq": "daily",
        "pb_series": series_vals,          # 引擎侧可复算五档 + V10 精算
        "pb_field_label": "市净率PB(倍)",
    }
    return bands, notes, len(series_vals)


# D-7（2026-10-25）：现价双源交叉阈值（相对差 >0.5% 即告警；见 CHANGELOG 第三批）
PRICE_CROSS_WARN_PCT = 0.5


def _fetch_price_sina(etf: str) -> tuple[float | None, str]:
    """新浪 hq 实时快照（主源）。返回 (price|None, note)。"""
    mkt = "sh" if etf.startswith(("5", "6")) else "sz"
    url = f"https://hq.sinajs.cn/list={mkt}{etf}"
    req = urllib.request.Request(url, headers={
        "Referer": "https://finance.sina.com.cn",
        "User-Agent": "Mozilla/5.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=6) as r:
            txt = r.read().decode("gbk", errors="ignore")
        parts = txt.split('"')
        if len(parts) >= 2 and parts[1]:
            fields = parts[1].split(",")
            if len(fields) > 3 and fields[3]:
                px = float(fields[3])
                if px > 0:
                    return px, f"新浪实时（{fields[0]}）"
                # 盘前/集合竞价空档现价=0.000 → 视同缺省（严禁 0 价入 EV base）
                return None, "现价=0（盘前/未开盘）"
        return None, "现价返回异常"
    except Exception as e:  # 网络断供不炸流程
        return None, f"拉取失败({type(e).__name__})"


def _fetch_price_tencent(etf: str) -> tuple[float | None, str]:
    """腾讯 qt.gtimg.cn 实时快照（交叉源·免费无鉴权）。

    D-7（2026-10-25）：现价过去单源（仅新浪），单源故障/脏值无第二只眼睛。
    腾讯快照与新浪同为交易所快照口径，字段 [3]=当前价，用作机械交叉。
    注：tdx/westock data_quote 属 agent 侧 MCP，Python 子进程内不可调 →
    二者仍作人工兜底（preflight 连接器矩阵已提示选路）。
    """
    mkt = "sh" if etf.startswith(("5", "6")) else "sz"
    url = f"https://qt.gtimg.cn/q={mkt}{etf}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://gu.qq.com",
    })
    try:
        with urllib.request.urlopen(req, timeout=6) as r:
            txt = r.read().decode("gbk", errors="ignore")
        parts = txt.split('"')
        if len(parts) >= 2 and parts[1]:
            fields = parts[1].split("~")
            if len(fields) > 3 and fields[3]:
                px = float(fields[3])
                if px > 0:
                    return px, f"腾讯实时（{fields[1]}）"
                return None, "现价=0（盘前/未开盘）"
        return None, "现价返回异常"
    except Exception as e:
        return None, f"拉取失败({type(e).__name__})"


def fetch_price_now(etf):
    """现价实时拉取（D-7 起为**双源交叉**）。仅内存，禁落盘。

    返回 (price|None, note, cross)。cross 结构：
      {"ok": bool, "primary": "sina|tencent|none", "sources": [{name,price,note}],
       "diff_pct": float|None, "warn": str|None, "multi_source": bool}
    口径：
      · 主源新浪成功 → 腾讯交叉；相对差 ≤0.5% 视为一致（note 标差值），>0.5% 告警但仍取主源
        （现价只影响相对定位与 EV base；单点分歧不足以推翻主源，须人工复核）。
      · 新浪失败而腾讯成功 → 腾讯兜底并标「单源」（降级须可量化，D-8 消费）。
      · 双源皆失败 → price_now 缺省，EV 走相对现价=100 口径（既有行为不变）。
    """
    if not etf:
        return None, "未提供 ETF 代码 → price_now 缺省（EV 走相对现价=100 口径）", {
            "ok": False, "primary": "none", "sources": [], "diff_pct": None,
            "warn": None, "multi_source": False,
        }
    s_px, s_note = _fetch_price_sina(etf)
    t_px, t_note = _fetch_price_tencent(etf)
    sources = []
    if s_px is not None:
        sources.append({"name": "sina", "price": s_px, "note": s_note})
    if t_px is not None:
        sources.append({"name": "tencent", "price": t_px, "note": t_note})

    if s_px is not None and t_px is not None:
        diff = abs(s_px - t_px) / s_px * 100.0
        warn = None
        if diff > PRICE_CROSS_WARN_PCT:
            warn = (f"现价双源分歧 {diff:.2f}% > {PRICE_CROSS_WARN_PCT}%"
                    f"（新浪 {s_px} vs 腾讯 {t_px}）→ 已取主源，须人工复核快照时点")
            note = f"{s_note}｜双源交叉 ⚠️ 差 {diff:.2f}%（超阈）"
        else:
            note = f"{s_note}｜双源交叉一致（差 {diff:.2f}%）"
        return s_px, note, {
            "ok": True, "primary": "sina", "sources": sources,
            "diff_pct": round(diff, 3), "warn": warn, "multi_source": True,
        }
    if s_px is not None:
        return s_px, f"{s_note}｜⚠️ 单源（腾讯不可用：{t_note}）", {
            "ok": True, "primary": "sina", "sources": sources, "diff_pct": None,
            "warn": f"现价仅单源（腾讯不可用：{t_note}）→ 无交叉验证", "multi_source": False,
        }
    if t_px is not None:
        return t_px, f"{t_note}｜⚠️ 单源兜底（新浪不可用：{s_note}）", {
            "ok": True, "primary": "tencent", "sources": sources, "diff_pct": None,
            "warn": f"现价仅单源（新浪不可用：{s_note}）→ 腾讯兜底无交叉", "multi_source": False,
        }
    return None, (f"双源皆失败（新浪：{s_note} / 腾讯：{t_note}）→ price_now 缺省，"
                  f"走相对=100 口径"), {
        "ok": False, "primary": "none", "sources": [], "diff_pct": None,
        "warn": f"现价双源皆失败（新浪：{s_note} / 腾讯：{t_note}）→ 无绝对目标价", "multi_source": False,
    }


def _preflight_gate_auto() -> None:
    """运行前体检硬闸薄封装（逻辑统一在 preflight_etf.enforce，避免两处实现漂移）。
    体检模块不可用时 fail-open（仅跳过），因为它自身故障不应阻断正常分析。"""
    try:
        import preflight_etf as pf  # noqa: E402
    except Exception as e:  # pragma: no cover
        print(f"[PREFLIGHT] ⚠️ 体检模块不可用（{type(e).__name__}: {e}）→ 跳过，不阻断",
              file=sys.stderr)
        return
    pf.enforce()


def _assess_data_health(*, index_code, as_of, n_pts, stale, bands, quant_raw,
                        lt, im_filled, event_leg, price_cross) -> dict:
    """D-8（2026-10-25）：把本次实际取数状态汇总为四态健康度（薄封装，逻辑在 data_health）。

    模块不可用 → 返回 status=OK + 显式 deferred 标注（fail-open：标注层故障不应阻断分析），
    但 `deferred=True` 使上层能识别「未评估」而非「评估通过」（二者语义不可混）。
    """
    try:
        import data_health as dh  # noqa: E402
    except Exception as e:  # pragma: no cover
        return {"status": "OK", "deferred": True,
                "note": f"data_health 模块不可用({type(e).__name__}) → 未评估", "items": []}
    h = dh.assess(
        index_code=index_code, as_of=as_of,
        series_points=n_pts, freshness_days=stale,
        pb_pct=(bands or {}).get("pb_pct"),
        pb_sources=(bands or {}).get("pb_sources"),
        price_cross=price_cross,
        has_quant_raw=quant_raw is not None,
        pb_pct_10y=(bands or {}).get("pb_pct_10y"),
        bvps_caliber_mutation=bool((bands or {}).get("bvps_caliber_mutation")),
        caliber_events=(lt or {}).get("caliber_events"),
        industry_metrics_present=bool(im_filled),
        event_leg_state=(event_leg or {}).get("state"),
    )
    h["deferred"] = False
    return h


def _run_single(args, etf: str | None, sector: str | None) -> tuple[int, str | None, dict]:
    """单只 ETF 的 M1-M4 草稿流（E-3 批量化的主体·2026-10-25 从 main 提取）。

    返回 (exit_code, out_path, summary)。原来散落各处的 sys.exit 收敛为返回值，
    使批量调度（--etfs）可对单只失败继续跑后续——一次 S1 解析失败不杀整批
    （与 fetch_pb_mxdata.fetch_one 的 per-code 失败同一稳健性原则）。
    """
    t_all = time.time()
    stages = {}
    notes, warnings = [], []

    as_of = args.as_of or date.today().isoformat()

    # ---- S1 指数解析 ----
    t0 = time.time()
    try:
        index_code, index_name, s1_note = stage_s1_resolve(etf, args.index)
        notes.append(f"[S1] {s1_note} → {index_code} {index_name}")
    except Exception as e:
        print(f"[S1] FAIL: {e}", file=sys.stderr)
        return (ARG_EXIT, None, {"etf": etf, "error": str(e)})
    stages["S1_解析"] = round((time.time() - t0) * 1000)

    # ---- S2 前的自动刷新（stale → fetch_pb_mxdata 重取；最佳努力/优雅降级）----
    # 注：显式 --as-of 锁定历史日期时跳过刷新（历史分析须用当时缓存，避免覆盖当天数据）
    if not args.no_refresh and not args.as_of:
        # 返回值校验：刷新成功/失败都必须可见（失败不再静默吞掉 → 结论带水印，符合"降级必量化"）
        refreshed, refresh_fail = _auto_refresh(index_code, ["pb", "pe"])
        if refreshed:
            notes.append(f"[S2-刷新] 自动刷新成功并写回缓存: "
                         f"{', '.join(m.upper() for m in refreshed)}（as_of 已更新，stale 解除）")
        for m, why in refresh_fail.items():
            warnings.append(f"[S2-刷新] {m.upper()} 自动刷新失败（已重试 {REFRESH_RETRIES} 次）：{why} "
                            f"→ 沿用旧缓存；补数路径：iFinD MCP get_security_indicators(market=指数) 取缺口窗口"
                            f"（一次同拉 PB+PE，重叠期≥3点）→ ingest_ifind_valuation.py --code {index_code} 落盘")

    # ---- S2 序列读取 + 新鲜度 ----
    t0 = time.time()
    try:
        payload, stale = stage_s2_series(index_code)
    except FileNotFoundError as e:
        print(f"[S2] FAIL: {e}", file=sys.stderr)
        return (MISSING_EXIT, None, {"etf": etf, "index_code": index_code, "error": str(e)})
    pe_payload, stale_pe = stage_s2_pe(index_code)
    if stale is not None and stale > FRESHNESS_MAX_DAYS:
        warnings.append(f"[S2] 序列过期：as_of 距今 {stale} 天 > {FRESHNESS_MAX_DAYS} 天（新鲜度门），"
                        "分位带可能漂移 → 须经 mx-ds-mcp 刷新后重跑（本草稿仅供预览）")
    elif stale is not None and stale > FRESHNESS_WARN_DAYS:
        notes.append(f"[S2] 序列偏旧：距今 {stale} 天（>{FRESHNESS_WARN_DAYS} 预警档），建议刷新")
    if stale_pe is not None and stale_pe > FRESHNESS_MAX_DAYS:
        warnings.append(f"[S2-PE] PE 序列过期：as_of 距今 {stale_pe} 天 > {FRESHNESS_MAX_DAYS} 天（新鲜度门）")
    elif stale_pe is not None and stale_pe > FRESHNESS_WARN_DAYS:
        notes.append(f"[S2-PE] PE 序列偏旧：距今 {stale_pe} 天（>{FRESHNESS_WARN_DAYS} 预警档）")
    stages["S2_序列"] = round((time.time() - t0) * 1000)

    # ---- S3 分位五档 + O9/V10 预检 ----
    t0 = time.time()
    bands, s3_notes, n_pts = stage_s3_bands(payload, stale)
    notes.extend(f"[S3] {n}" for n in s3_notes)
    if pe_payload is not None:
        pe_bands, s3_pe_notes, n_pts_pe = stage_s3_pe_bands(pe_payload, stale_pe)
        notes.extend(f"[S3-PE] {n}" for n in s3_pe_notes)
        bands.update(pe_bands)
    stages["S3_分位"] = round((time.time() - t0) * 1000)

    # ---- D-4（2026-10-25）长周期 PB 交叉校验 + BVPS 口径突变检测 ----
    # 不改动引擎逻辑：仅产出更长窗口分位 + 口径突变标记，写入草稿 bands/draft，
    # 引擎在 O9 触发时自行消费 pb_pct_10y；口径突变作 SHOULD 级告警（不阻断）。
    t0 = time.time()
    lt = _pb_longterm(index_code)
    if lt["available"]:
        bands["pb_pct_10y"] = lt["pb_pct_10y"]
        bands["pb_series_years"] = lt["window_years"]
        bands["bvps_caliber_mutation"] = lt["caliber_mutation"]
        notes.append(f"[D-4] 长周期 PB 分位 {lt['pb_pct_10y']}%（窗口 {lt['window_years']}y·"
                     f"as_of {lt['as_of']}）")
        if lt["caliber_mutation"]:
            warnings.append(f"[D-4] BVPS 口径突变 {len(lt['caliber_events'])} 处（价格未动而 PB 大跳→"
                            f"疑似会计口径变更）→ PB 分位可能失真，建议人工核验分母口径；"
                            f"首处 {lt['caliber_events'][0]['month']}（PB跳"
                            f"{lt['caliber_events'][0]['pb_jump_pct']}%/价格仅"
                            f"{lt['caliber_events'][0]['price_jump_pct']}%）")
        if lt["note"]:
            notes.append(f"[D-4] {lt['note']}")
    else:
        notes.append(f"[D-4] {lt['note']}")
    stages["D4_长周期"] = round((time.time() - t0) * 1000)

    # ---- S3.5 G4 确定性预填（数据可得时）+ 机械字段锁定 ----
    t0 = time.time()
    prefilled, extra_locked = stage_s3_prefill(payload)
    bands.update(prefilled)
    # 2026-10-07：双源自动派生（§四 协议①②③）—— 手算兜底 + 多源交叉，免 LLM 手填并锁定
    pb_manual, pb_sources, _ = _derive_dual_source(payload, "pb")
    if pb_manual is not None:
        bands["pb_manual"] = pb_manual
        bands["pb_sources"] = pb_sources
        for k in ("pb_manual", "pb_sources"):
            if k not in extra_locked:
                extra_locked.append(k)
    if pe_payload is not None:
        pe_manual, pe_sources, _ = _derive_dual_source(pe_payload, "pe")
        if pe_manual is not None:
            bands["pe_manual"] = pe_manual
            bands["pe_sources"] = pe_sources
            extra_locked.extend(["pe_manual", "pe_sources"])
    # 注意：locked_mechanical 不在此处硬性列举 D-4/D-5②/D-6 字段——这些字段
    # **仅当 Python 实际填充时才锁**（见下方 S4 后装配），避免无 side-car/无月频序列时
    # 仍锁死字段、误伤 LLM 带 provenance 的合法手填（2026-10-25 修正）。
    if prefilled:
        notes.append(f"[S3.5] Python 机械预填并锁定: "
                     f"{', '.join(k for k in prefilled if not k.startswith('_'))}（数据缺失项仍留 _llm_required 复核）")
    stages["S3.5_预填"] = round((time.time() - t0) * 1000)

    # ---- 现价（实时，禁落盘）----
    t0 = time.time()
    price_now, price_note, price_cross = fetch_price_now(etf)
    notes.append(f"[S2现价] {price_note}" + (f" = {price_now}" if price_now else ""))
    if price_cross.get("warn"):
        warnings.append(price_cross["warn"])
    # D-7（2026-10-25）：双源交叉明细随草稿留痕（元数据，引擎不收；供 D-8 健康度与人工复核）
    if price_cross.get("sources"):
        notes.append("[S2现价·双源] " + " / ".join(
            f"{s['name']}={s['price']}" for s in price_cross["sources"]
            if s.get("price") is not None))
    if price_now is not None and args.out != "/dev/null":
        pass  # 显式不落盘现价（防静默过期）
    stages["现价"] = round((time.time() - t0) * 1000)

    # ---- S4 机械草稿 ----
    t0 = time.time()
    quant_raw, qr_reason = _assemble_quant_raw(etf)
    # D-5②（2026-10-25）：行业量价机械回填（side-car 存在时），返回实际填到的字段名用于锁定
    im_lock = _attach_industry_metrics(index_code, sector, bands, quant_raw, notes, warnings)
    # D-6（2026-10-25）：板块事件腿三态卡（side-car 存在时）
    event_leg = _attach_event_leg(index_code, sector, notes, warnings)
    # ---- 机械字段锁定装配（仅锁 Python 实际填充的客观值，避免误伤 LLM 合法手填）----
    # 基准（stage_s3_bands 恒产 PB 分位五档+序列）；D-4 仅当 lt["available"]（有月频序列）；
    # D-5② 仅当 side-car 实际回填到值（im_lock）；D-6 仅当 side-car 存在（事件腿写入草稿）。
    d4_lock = ["pb_pct_10y", "pb_series_years", "bvps_caliber_mutation"] if lt.get("available") else []
    el_lock = ["event_leg"] if event_leg is not None else []
    BASE_LOCKED = ["pb_now", "pb_pct", "pb_p5", "pb_p20", "pb_p50",
                   "pb_p80", "pb_p95", "pb_series"]
    locked_mechanical = BASE_LOCKED + d4_lock + im_lock + el_lock + extra_locked
    llm_required = list(_LLM_REQUIRED_BASE)
    if quant_raw is None:
        llm_required.insert(5, f"quant_raw.price_series（{qr_reason}，须 LLM/数据脚本补价格序列）")
    # 分歧行业预标记：price=up 时须一并提供 dual_price_data，否则引擎 MUST 阻塞（2026-10-07 增）
    divergent = is_divergence_sector(sector, index_name, index_code)
    if divergent:
        llm_required.append(
            "⚠️【分歧行业·双价格门】本行业命中双价格分层关键词（出厂/官方价 vs 市场成交价）："
            "若你判定 price=up，必须一并提供 dual_price_data（records 分层序列），否则引擎将 MUST 阻塞；"
            "若 price=down/flat 则无需。请在同一步补齐，避免后续多一轮阻塞。")
    draft = {
        "sector": sector,
        "as_of": payload.get("as_of") or as_of,
        "asset_class": "sector",
        "index_code": index_code,
        "index_name": index_name or args.sector,
        "is_cross_border": False,
        "price_now": price_now,
        "_price_cross_check": price_cross,   # D-7：双源交叉明细（元数据，非引擎字段）
        **bands,
    }
    if quant_raw is not None:
        draft["quant_raw"] = quant_raw
        notes.append(f"[S4] quant_raw.price_series 机械装配: {len(quant_raw['price_series'])} 点（data/etf 缓存）")
        # B-10（2026-10-25）：量化派生覆盖率计量（**只计量、不造代理输入**）。
        # 目的：把 BACKLOG「覆盖率 ≥80%」变成可测数字，而不是靠人估。缺口仍然如实
        # 显示为 None —— 引擎此时走六档表（win_rate_mode=table），是显式降级。
        # 纪律：**不得**为了让覆盖率好看而塞入未经验证的代理特征（V4 校准已确认
        # proxy 特征 AUC≈0.50、无预测力；强行接入=把结论推向中性而不留痕）。
        try:
            from quant_inputs import derive_quant_inputs, QUANT_FIELDS
            _d = derive_quant_inputs(quant_raw)
            _miss = [k for k in QUANT_FIELDS if _d.get(k) is None]
            draft["_quant_coverage"] = {
                "schema": "quant-coverage/1",
                "derived_n": len(QUANT_FIELDS) - len(_miss),
                "total": len(QUANT_FIELDS),
                "missing": _miss,
                "win_rate_path_expected": "quant" if not _miss else "table",
                "note": ("覆盖率<8/8 → 引擎走六档表（显式降级）；提升需 point-in-time "
                         "行业量价/资金流数据源 + 校准 AUC>0.55 才可解锁 validated"),
            }
            notes.append(f"[B-10] 量化派生覆盖 {len(QUANT_FIELDS) - len(_miss)}/{len(QUANT_FIELDS)}"
                         f"（缺 {','.join(_miss) or '无'}）→ 预期胜率路径 "
                         f"{draft['_quant_coverage']['win_rate_path_expected']}")
        except Exception as e:
            draft["_quant_coverage"] = {"schema": "quant-coverage/1", "error":
                                        f"{type(e).__name__}: {e}"}
            warnings.append(f"[B-10] 量化覆盖率计量失败（{type(e).__name__}）→ 覆盖率未知")
    if divergent:
        draft["_divergence_sector"] = True
    # D-6（2026-10-25）：板块事件腿三态卡（retrieved/empty/corrupt）写入草稿并锁定
    if event_leg is not None:
        draft["event_leg"] = {
            "state": event_leg.get("state"),
            "summary": event_leg.get("summary", []),
            "source": event_leg.get("source"),
            "retrieved_at": event_leg.get("retrieved_at"),
            "raw_excerpt": event_leg.get("raw_excerpt", ""),
        }
    draft["_locked_mechanical"] = locked_mechanical
    draft["_llm_required"] = llm_required
    draft["_llm_optional"] = list(_LLM_OPTIONAL_BASE)
    # D-8（2026-10-25）：数据健康度四态（OK/DEGRADED/MISSING/INVALID）+ 降级必量化。
    # 纯标注层：只汇总本次实际取到的字段状态，不参与胜率/EV/闸门计算（核心逻辑零改动）。
    draft["_data_health"] = _assess_data_health(
        index_code=index_code, as_of=payload.get("as_of") or as_of,
        n_pts=n_pts, stale=stale, bands=bands, quant_raw=quant_raw,
        lt=lt, im_filled=bool(im_lock), event_leg=event_leg, price_cross=price_cross,
    )
    draft["_pipeline_meta"] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "stages_ms": stages,
        "series_points": n_pts,
        "series_as_of": payload.get("as_of"),
        "series_source": payload.get("source"),
        "freshness_days": stale,
        "notes": notes,
        # 2026-10-07 增：warnings 原只存内存（用于退出码），不落盘 → 刷新失败/stale 等
        # 降级水印在草稿里不可见，违反"降级必量化、结论带水印"。现一并落盘。
        "warnings": warnings,
    }
    out_dir = args.out
    os.makedirs(out_dir, exist_ok=True)
    tag = (etf or index_code).split(".")[0]
    out_path = os.path.join(out_dir, f"_draft_{tag}_{as_of}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(draft, f, ensure_ascii=False, indent=2)
    stages["S4_草稿"] = round((time.time() - t0) * 1000)

    # ---- 汇总 ----
    total_ms = round((time.time() - t_all) * 1000)
    print(f"✅ 草稿已产出: {out_path}")
    print(f"   指数={index_code} | 序列点={n_pts} | as_of={payload.get('as_of')} | 新鲜度={stale}天")
    print(f"   PB五档: p5={bands['pb_p5']} p20={bands['pb_p20']} p50={bands['pb_p50']} "
          f"p80={bands['pb_p80']} p95={bands['pb_p95']} | 分位={bands['pb_pct']}%")
    print(f"   分段耗时(ms): {stages} | 总计 {total_ms}ms")
    _dh = draft.get("_data_health") or {}
    if _dh.get("watermark"):
        print(f"   {_dh['watermark']}")
    else:
        print(f"   数据健康度: {_dh.get('status', '?')}（{_dh.get('counts', {})}）")
    for w in warnings:
        print(f"   ⚠️ {w}")
    print(f"   🔒 锁定机械字段 {len(locked_mechanical)} 项（pb_pct/p5~p95/pb_now/pb_series"
          f"{'+' + ','.join(extra_locked) if extra_locked else ''}）：LLM 不得改写，仅复核")
    print(f"   下一步: LLM 补齐 _llm_required（{len(llm_required)} 组·判断/采证为主）→ analyze_etf.py --fills 或 discipline.py --input 本草稿")
    rc = STALE_EXIT if (stale is not None and stale > FRESHNESS_MAX_DAYS) else 0
    summary = {"etf": etf, "index_code": index_code, "n_pts": n_pts,
               "as_of": payload.get("as_of"), "stale": stale,
               "pb_pct": bands.get("pb_pct"), "total_ms": total_ms,
               "data_health": _dh.get("status")}
    return (rc, out_path, summary)


def _attach_industry_metrics(index_code: str, sector: str | None,
                             bands: dict, quant_raw: dict,
                             notes: list, warnings: list) -> list:
    """D-5②（2026-10-25）：读 side-car（data/industry_metrics/{index_code}.json）机械回填
    行业量价客观指标 + 行业量/价口径 provenance 进草稿。

    三态诚实：side-car 不存在→标注『未机械取证，LLM 须带 provenance 填或明确标未取到』；
    存在但某指标 corrupt/empty→如实标注，不把『没查』当『没有』。
    仅写字段，不改引擎逻辑；**仅把实际填到的字段名返回并锁进 _locked_mechanical**，
    避免无 side-car 时仍锁死这些字段、误伤 LLM 带 provenance 的合法手填（2026-10-25 修正）。
    返回 lock_keys（实际写入的字段名 list；无 side-car 返回空 list）。
    """
    path = os.path.join(_IM_DIR, f"{index_code}.json")
    if not os.path.exists(path):
        notes.append(f"[D-5②] 无行业量价 side-car（{index_code}.json）→ 行业量价须 LLM 带 "
                     f"provenance 填 industry_volume_metric/price_metric 及 4 个 quant_raw 客观指标，"
                     f"或明确标『未取到』（禁编造）")
        return []
    rec = json.load(open(path, encoding="utf-8"))
    metrics = rec.get("metrics", {})
    n_filled = 0
    lock_keys = []
    for m in ("volume_yoy", "inventory_yoy", "utilization", "earning_rev_pct"):
        mv = metrics.get(m, {})
        if mv.get("state") == "filled" and mv.get("value") is not None:
            quant_raw[m] = mv["value"]
            n_filled += 1
            lock_keys.append(m)
        # empty/corrupt 不写值（None），但 provenance 留在 side-car 供核验
    if rec.get("industry_volume_metric"):
        bands["industry_volume_metric"] = rec["industry_volume_metric"]
        lock_keys.append("industry_volume_metric")
    if rec.get("industry_price_metric"):
        bands["industry_price_metric"] = rec["industry_price_metric"]
        lock_keys.append("industry_price_metric")
    if n_filled:
        notes.append(f"[D-5②] 机械回填 {n_filled}/4 行业量价指标（妙想NL·免费源·带 provenance）→ "
                     f"已锁入 quant_raw，引擎免走六档量价表回退")
    else:
        warnings.append(f"[D-5②] side-car 存在但 0 指标取到（源无数据/解析失败）→ 行业量价仍须 "
                        f"LLM 带 provenance 补或标『未取到』")
    return lock_keys


def _attach_event_leg(index_code: str, sector: str | None, notes: list,
                      warnings: list) -> dict | None:
    """D-6（2026-10-25）：读 side-car（data/event_leg/{index_code}.json）植入板块事件腿三态卡。

    三态诚实（沿用 XYD 全局事件腿铁律）：retrieved(有表摘要) / empty(无表⛔红标) /
    corrupt(损坏⚠️灰标)。无 side-car→标注『未检索事件腿』（禁止静默宣称没有）。
    返回 event_leg block 或 None（供调用方写进草稿并锁定）。"""
    path = os.path.join(_EL_DIR, f"{index_code}.json")
    if not os.path.exists(path):
        notes.append(f"[D-6] 无板块事件腿 side-car（{index_code}.json）→ 出裁决前须先检索事件腿"
                     f"（event_leg_sector.py）并回填；禁止未检索即宣称『没有事件腿』")
        return None
    rec = json.load(open(path, encoding="utf-8"))
    state = rec.get("state")
    if state == "retrieved":
        notes.append(f"[D-6] 事件腿✅有表（{len(rec.get('summary', []))} 条·妙想NL）→ 已写进草稿 event_leg")
    elif state == "empty":
        warnings.append(f"[D-6] 事件腿⛔无表(红标)：已检索但源无板块事件数据（事件真空）→ "
                        f"须显式标注，非静默略过")
    elif state == "corrupt":
        warnings.append(f"[D-6] 事件腿⚠️损坏(灰标)：查询异常/不可解析 → 须重查，不得据此下结论")
    else:
        warnings.append(f"[D-6] 事件腿状态未知({state})→ 须重查")
    return rec


def _sector_from_map(etf: str) -> str | None:
    """批量模式：sector 缺省从 etf_index_map 的 index_name 推导（免逐只手填）。

    注意：HERE 在本模块是 **str**（os.path.dirname），禁用 Path 的 `/` 运算
    （TypeError 会被外层 except 吞成 None → 静默跳过，2026-10-25 实测教训）。
    """
    try:
        m = json.loads(open(os.path.join(HERE, "data", "meta", "etf_index_map.json"),
                            encoding="utf-8").read())
        v = m.get(etf)
        if isinstance(v, dict):
            return v.get("index_name") or v.get("name")
    except Exception:
        pass
    return None


def main():
    ap = argparse.ArgumentParser(description="XYD-供需驱动板块 · 机械段流水线 M1-M4")
    ap.add_argument("--etf", help="ETF 代码（经 etf_index_map 解析）")
    ap.add_argument("--etfs", dest="etfs", default="",
                    help="E-3（2026-10-25）批量：逗号分隔多只 ETF（如 512800,512880）——"
                         "一次进程平摊固定成本（体检/导入/连接），单只失败不杀整批；"
                         "sector 缺省从映射表 index_name 推导")
    ap.add_argument("--index", help="直接指定指数代码（如 980032.CNI）")
    ap.add_argument("--sector", required=False, help="板块名（填表用；批量模式缺省按映射表推导，显式传入则覆盖全部）")
    ap.add_argument("--guard", dest="guard", help="S4.5：对 LLM 已填草稿跑 llm_fill_guard 机械复核（与 --etf/--index 互斥）")
    ap_path = ap.add_argument("--as-of", dest="as_of", help="数据日期 YYYY-MM-DD（缺省今日）")
    ap.add_argument("--no-refresh", dest="no_refresh", action="store_true",
                    help="跳过 stale 自动刷新（离线/调试用；默认 stale 自动经 fetch_pb_mxdata 重取）")
    ap.add_argument("--out", default=HERE, help="草稿输出目录（缺省 skill 目录）")
    args = ap.parse_args()

    # 🔴 运行前体检硬闸（2026-10-07 立）——机械段也必须体检，否则有人绕过 analyze_etf
    # 直跑本脚本时依然会撞那 9 堵墙。被 analyze_etf 以子进程调用时，因继承
    # `_XYD_PREFLIGHT_DONE=1` 而跳过，不重复付两次体检开销。批量模式只付一次。
    _preflight_gate_auto()

    # ---- S4.5 GUARD 模式：LLM 补齐后、喂引擎前的机械复核（G3 · 2026-09-04）----
    if args.guard:
        import llm_fill_guard
        with open(args.guard, encoding="utf-8") as f:
            case = json.load(f)
        res = llm_fill_guard.run(case)
        print(llm_fill_guard.render(res))
        print(f"\n[GUARD] {'通过 → 可喂 discipline.py' if res['ok'] else '拦截 → 修正 blockers 后重跑'}")
        sys.exit(0 if res["ok"] else GUARD_EXIT)

    # ---- E-3 批量模式（2026-10-25）----
    if args.etfs:
        codes = [c.strip() for c in args.etfs.split(",") if c.strip()]
        if not codes:
            print("[ARG] --etfs 为空", file=sys.stderr)
            sys.exit(ARG_EXIT)
        t_batch = time.time()
        rows = []
        for etf in codes:
            sector = args.sector or _sector_from_map(etf)
            if not sector:
                print(f"[ARG] {etf}: --sector 未显式提供且映射表无 index_name → 跳过",
                      file=sys.stderr)
                rows.append({"etf": etf, "error": "sector 无法解析", "rc": ARG_EXIT})
                continue
            print(f"\n{'='*72}\n[批量] {etf}（{sector}）\n{'='*72}")
            try:
                rc, out_path, summary = _run_single(args, etf, sector)
            except Exception as e:  # 单只未预期异常不杀整批（E-3 稳健性）
                print(f"[批量] {etf} 未预期异常：{type(e).__name__}: {e}", file=sys.stderr)
                rc, summary = 1, {"etf": etf, "error": f"{type(e).__name__}: {e}"}
            summary["rc"] = rc
            rows.append(summary)
        # 批量汇总表（端到端口径·平摊固定成本）
        print(f"\n{'='*72}\n[批量汇总] {len(rows)} 只 · 总耗时 {round((time.time()-t_batch)*1000)}ms"
              f"（含一次体检·固定成本已平摊）\n{'='*72}")
        print(f"{'ETF':<10}{'指数':<16}{'点数':>6}{'as_of':>12}{'新鲜度':>7}{'PB分位%':>9}{'退出码':>7}")
        for r in rows:
            if "error" in r:
                print(f"{r['etf']:<10}{'FAIL':<16}{'—':>6}{'—':>12}{'—':>7}{'—':>9}{r.get('rc','—'):>7}  {r['error'][:40]}")
            else:
                print(f"{r['etf']:<10}{str(r['index_code']):<16}{r['n_pts']:>6}"
                      f"{str(r['as_of']):>12}{str(r['stale'])+'天':>7}"
                      f"{str(r['pb_pct']):>9}{r['rc']:>7}")
        sys.exit(max((r.get("rc", 0) or 0) for r in rows))

    if not args.sector:
        print("[ARG] --sector 必填（GUARD 模式除外；批量用 --etfs）", file=sys.stderr)
        sys.exit(ARG_EXIT)

    rc, _out, _summary = _run_single(args, args.etf, args.sector)
    sys.exit(rc)


if __name__ == "__main__":
    main()
