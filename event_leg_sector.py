#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
D-6（2026-10-25）· 板块事件腿三态卡（移植自 XYD-earnings-analysis 事件腿纪律）。

背景（审计定义 + XYD 全局事件腿铁律 2026-10-17）：出裁决前必须先去免费源检索事件腿，
禁未检索即宣称「没有事件腿」。「没去找」与「找了没有」必须可区分——这正是三态卡的本职：
  · retrieved（有表摘要） : 检索到 ≥1 条板块事件（政策/扩产/投产/减产/价格/中标…）→ 绿
  · empty   （无表 ⛔ 红标）: 已检索但源无事件数据 → 红标（板块事件真空，须显式标注，非静默略过）
  · corrupt （损坏 ⚠️ 灰标）: 查询异常/响应不可解析 → 灰标（数据损坏，须重查）

与 earnings 技能区别：earnings 渲染进十方块 HTML；本技能（供需板块）产草稿 JSON，事件腿
以草稿内 `event_leg` block 体现（带 state + summary + provenance），锁进 _locked_mechanical。
源：免费 妙想 NL（MX_APIKEY 本地直连，不耗付费配额）；后续可级联 westock/tdx 公告（免费源）。

用法：
  python event_leg_sector.py --sector 半导体 --index 980017.SZ
  （run_sector_pipeline 在生成草稿时若发现 data/event_leg/{index_code}.json 即自动读入）
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
SIDECAR_DIR = os.path.join(HERE, "data", "event_leg")
os.makedirs(SIDECAR_DIR, exist_ok=True)

MX_KEY = os.getenv("MX_APIKEY")
MX_BASE = "https://mkapi2.dfcfs.com/finskillshub/api/claw/query"

# 事件腿关键词（命中其一即视为板块事件）
EVENT_KEYWORDS = ["政策", "扩产", "投产", "减产", "价格", "中标", "签约", "产能",
                  "规划", "补贴", "限产", "出口", "进口", "审批", "批复", "上市"]


def _query_nl(q: str) -> str | None:
    if not MX_KEY:
        return None
    req = urllib.request.Request(
        MX_BASE, data=json.dumps({"toolQuery": q}).encode("utf-8"),
        headers={"Content-Type": "application/json", "apikey": MX_KEY}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        body = json.loads(r.read().decode("utf-8"))
    return _extract_text(body)


def _extract_text(body) -> str:
    if isinstance(body, str):
        return body
    if not isinstance(body, dict):
        return ""
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
    for n in nodes:
        if any(k in n for k in EVENT_KEYWORDS):
            return n
    return nodes[0] if nodes else ""


def extract_events(text: str, max_n: int = 6) -> list:
    """从 NL 文本抽取事件句（含事件关键词的句子），返回 list[str]。"""
    if not text:
        return []
    sents = re.split(r"[。\n；;]", text)
    hits = [s.strip() for s in sents if s.strip() and any(k in s for k in EVENT_KEYWORDS)]
    return hits[:max_n]


def assess_event_leg(sector: str, index_code: str, index_name: str = "") -> dict:
    """对单个板块检索事件腿 → 三态 card + side-car。

    返回 {state, summary(list), source, retrieved_at, raw_excerpt}。
    """
    rec = {
        "index_code": index_code, "sector": sector, "index_name": index_name,
        "source": "妙想NL(MX_APIKEY·免费源)", "retrieved_at": date.today().isoformat(),
    }
    if not MX_KEY:
        rec.update({"state": "corrupt", "summary": [],
                    "raw_excerpt": "", "_note": "MX_APIKEY 未配置→未检索"})
        _save(rec)
        return rec
    q = f"（{index_name or sector} {index_code}）最近 12 个月 行业 重大事件：政策/扩产/投产/减产/价格/中标/出口"
    text = None
    err = None
    for _ in range(2):
        try:
            text = _query_nl(q)
            break
        except Exception as e:  # pragma: no cover
            err = repr(e)
            time.sleep(1.0)
    if text is None:
        rec.update({"state": "corrupt", "summary": [],
                    "raw_excerpt": "", "_note": f"查询异常: {err or '空响应'}"})
    else:
        events = extract_events(text)
        if events:
            rec.update({"state": "retrieved", "summary": events, "raw_excerpt": text[:400]})
        else:
            rec.update({"state": "empty", "summary": [], "raw_excerpt": text[:200],
                        "_note": "已检索但源无板块事件数据（事件真空→红标，须显式标注）"})
    _save(rec)
    return rec


def _save(rec: dict):
    path = os.path.join(SIDECAR_DIR, f"{rec['index_code']}.json")
    json.dump(rec, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def main():
    ap = argparse.ArgumentParser(description="D-6 板块事件腿三态卡（免费源·provenance）")
    ap.add_argument("--sector", required=True)
    ap.add_argument("--index", required=True)
    ap.add_argument("--name", default="")
    args = ap.parse_args()
    rec = assess_event_leg(args.sector, args.index, args.name)
    sym = {"retrieved": "✅有表", "empty": "⛔无表(红标)", "corrupt": "⚠️损坏(灰标)"}.get(rec["state"], "?")
    print(f"[D-6] {args.sector} ({args.index}) → {sym}")
    for s in rec.get("summary", [])[:6]:
        print(f"  · {s}")
    if rec.get("_note"):
        print(f"  · {rec['_note']}")


if __name__ == "__main__":
    main()
