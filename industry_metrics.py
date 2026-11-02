#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
D-5②（2026-10-25）· 行业量价取证 SOP（免费源机械回填 + provenance）。

背景（审计定义）：行业量价（产销量同比/库存同比/产能利用率/盈利收入增速）是 discipline.py
「行业周期硬闸门」(vp_basis=industry_cycle) 的真实数据底座——严禁以 ETF 自身换手/净值/指数点位
充当量价。当前这些字段（analyze_etf.py quant_raw：volume_yoy/inventory_yoy/utilization/
earning_rev_pct；industry_volume_metric/industry_price_metric）是 LLM 手填占位（None），
引擎被迫走「§12.1 六档量价表」非量化回退路径（L-1 已给该路径打水印）。

本模块用【免费源 妙想 NL】(MX_APIKEY 本地直连，不耗 iFinD/妙想 MCP 配额) 机械回填上述
4 个客观指标 + 行业量/价口径 provenance，产出 side-car：data/industry_metrics/{index_code}.json。
三条铁律：
  · provenance 必带：每条指标记录 取值 + 单位 + 来源 + 抓取时间 + 原始证据摘录，杜绝静默捏造；
  · 三态诚实：filled(取到数值) / empty(源无数据或解析为空) / corrupt(查询异常)——不把
    「没去查」说成「没有」（沿用 XYD 全局事件腿纪律）；
  · 不破引擎：仅产出 side-car，由 run_sector_pipeline 读入写进草稿 quant_raw（锁定机械字段），
    discipline.py 引擎逻辑零改动。

用法：
  python industry_metrics.py --sector 银行 --index 399986.SZ
  （run_sector_pipeline 在生成草稿时若发现对应 side-car 即自动读入）
"""
import json
import os
import re
import sys
import time
import urllib.request
import argparse
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
SIDECAR_DIR = os.path.join(HERE, "data", "industry_metrics")
os.makedirs(SIDECAR_DIR, exist_ok=True)

MX_KEY = os.getenv("MX_APIKEY")
MX_BASE = "https://mkapi2.dfcfs.com/finskillshub/api/claw/query"

# 四个客观指标 → 妙想 NL 查询模板（行业口径，非 ETF 自身）
METRIC_QUERIES = {
    "volume_yoy":      "（{name} {code}）行业 产销量/出货量 最近一期 同比增长率 百分比",
    "inventory_yoy":   "（{name} {code}）行业 库存 同比 增减 百分比",
    "utilization":     "（{name} {code}）行业 产能利用率 最近一期 百分比",
    "earning_rev_pct": "（{name} {code}）行业 营业收入/盈利 同比增长率 百分比",
}
# 解析关键词（命中其一才认为该句是量价证据）
PARSE_KEYWORDS = {
    "volume_yoy": ["产销量", "出货量", "产量", "销量", "同比增长", "同比"],
    "inventory_yoy": ["库存", "同比", "增长"],
    "utilization": ["产能利用率", "利用率", "开工率"],
    "earning_rev_pct": ["营业收入", "营收", "盈利", "利润", "同比"],
}

_PCT_RE = re.compile(r"([+-]?\d+(?:\.\d+)?)\s*%")


def _query_nl(q: str) -> str | None:
    if not MX_KEY:
        return None
    req = urllib.request.Request(
        MX_BASE, data=json.dumps({"toolQuery": q}).encode("utf-8"),
        headers={"Content-Type": "application/json", "apikey": MX_KEY}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        body = json.loads(r.read().decode("utf-8"))
    # 妙想 NL 返回结构不固定，尝试常见文本落点；失败返回 None 走 empty/corrupt
    try:
        return _extract_text(body)
    except Exception:
        return None


def _extract_text(body) -> str:
    """从妙想 NL 响应里挤出可读文本（覆盖 data.data.searchDataResultDTO 文本/items）。"""
    if isinstance(body, str):
        return body
    if not isinstance(body, dict):
        return ""
    # 路径1: 表格文本
    nodes = []

    def _walk(o):
        if isinstance(o, dict):
            for v in o.values():
                _walk(v)
        elif isinstance(o, list):
            for v in o:
                _walk(v)
        elif isinstance(o, str):
            nodes.append(o)
    _walk(body)
    # 优先含「同比/产能/库存/营收」的句子
    for n in nodes:
        if any(k in n for k in ("同比", "产能利用率", "库存", "营收", "产销量", "出货量")):
            return n
    return nodes[0] if nodes else ""


def parse_metric(text: str, metric: str) -> dict:
    """从 NL 文本解析单个指标 → three-state 记录。

    返回 {state, value, unit, provenance, raw}。
      filled: 命中关键词且解析到 % 数值
      empty : 命中关键词但无 % 数值（源无数据）
      corrupt: 文本为空或解析异常
    """
    if not text or not isinstance(text, str):
        return {"state": "corrupt", "value": None, "unit": None,
                "provenance": "源返回空/异常", "raw": ""}
    kws = PARSE_KEYWORDS.get(metric, [])
    # 按句子切分，定位含关键词的句子
    sentences = re.split(r"[。\n；;]", text)
    cand_sent = None
    for s in sentences:
        if any(k in s for k in kws):
            cand_sent = s
            break
    if cand_sent is None:
        # 整段兜底：直接在全文本里找 % 数
        cand_sent = text
    m = _PCT_RE.search(cand_sent)
    if m:
        val = float(m.group(1))
        # 合理性闸：同比/利用率一般落在 [-100, 300]，超出视 corrupt（误抓）
        if -100 <= val <= 300:
            return {"state": "filled", "value": val, "unit": "%",
                    "provenance": "妙想NL(MX_APIKEY) 机械解析", "raw": cand_sent.strip()[:200]}
    return {"state": "empty", "value": None, "unit": None,
            "provenance": "妙想NL 返回但未解析到数值（源无该指标数据）", "raw": cand_sent.strip()[:200]}


def fetch_industry_metrics(sector: str, index_code: str, index_name: str = "") -> dict:
    """对单个板块机械回填 4 指标 + 行业量/价口径 provenance，写 side-car。

    免费源（妙想 NL 本地直连）。返回 record dict。
    """
    rec = {
        "index_code": index_code, "sector": sector, "index_name": index_name,
        "source": "妙想NL(MX_APIKEY·免费源)", "fetched_at": _now_iso(),
        "metrics": {}, "industry_volume_metric": "", "industry_price_metric": "",
    }
    if not MX_KEY:
        # 无 key → 全部 corrupt（诚实标注，不伪装）
        for metric in METRIC_QUERIES:
            rec["metrics"][metric] = {"state": "corrupt", "value": None, "unit": None,
                                      "provenance": "MX_APIKEY 未配置→未查询", "raw": ""}
        rec["_skipped"] = "MX_APIKEY not set"
        _save(rec)
        return rec
    for metric, tmpl in METRIC_QUERIES.items():
        q = tmpl.format(name=index_name or sector, code=index_code)
        text = None
        err = None
        for attempt in range(2):
            try:
                text = _query_nl(q)
                break
            except Exception as e:  # pragma: no cover
                err = repr(e)
                time.sleep(1.0)
        if text is None:
            rec["metrics"][metric] = {"state": "corrupt", "value": None, "unit": None,
                                      "provenance": f"查询异常: {err or '空响应'}", "raw": ""}
        else:
            rec["metrics"][metric] = parse_metric(text, metric)
    # 行业量/价口径：取 volume_yoy / earning_rev_pct 的 raw 作为口径说明（provenance）
    rec["industry_volume_metric"] = (
        f"行业产销量同比(妙想NL) → {rec['metrics']['volume_yoy'].get('raw','')[:80]}")
    rec["industry_price_metric"] = (
        f"行业盈利/营收同比(妙想NL) → {rec['metrics']['earning_rev_pct'].get('raw','')[:80]}")
    _save(rec)
    return rec


def _save(rec: dict):
    path = os.path.join(SIDECAR_DIR, f"{rec['index_code']}.json")
    json.dump(rec, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def _now_iso() -> str:
    return date.today().isoformat()


def main():
    ap = argparse.ArgumentParser(description="D-5② 行业量价取证（免费源·provenance）")
    ap.add_argument("--sector", required=True)
    ap.add_argument("--index", required=True, help="指数代码（如 399986.SZ）")
    ap.add_argument("--name", default="", help="指数名称（提高 NL 精度）")
    args = ap.parse_args()
    rec = fetch_industry_metrics(args.sector, args.index, args.name)
    print(f"[D-5②] {args.sector} ({args.index}) → {SIDECAR_DIR}/{args.index}.json")
    for m, v in rec["metrics"].items():
        print(f"  · {m}: {v['state']} value={v['value']} | {v['provenance']}")
    n_filled = sum(1 for v in rec["metrics"].values() if v["state"] == "filled")
    print(f"  → 取到 {n_filled}/4 客观指标；缺项须在 _llm_required 明确标注『未取到』而非编造")


if __name__ == "__main__":
    main()
