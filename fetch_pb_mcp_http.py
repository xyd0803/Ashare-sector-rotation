#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_pb_mcp_http.py —— 直连 mx-ds-mcp StreamableHttp 后端，批量拉取指数 5 年日频 PB 序列并落盘
=====================================================================================
背景：WorkBuddy 会话内调用 MCP 工具返回的新鲜数据不持久化到 tool-results（代理文件为陈旧批处理），
      导致 parse_mx_pb_daily.py 只能读到过期的 8-14 数据。本脚本直接 POST MCP 后端（key 取自 mcp.json），
      规避会话落盘限制，循环刷新多指数并写入 data/index_daily_pb/{CODE}.json。

用法：
  python fetch_pb_mcp_http.py --map idx_map.json        # idx_map: {CODE: 中文名}
  python fetch_pb_mcp_http.py --code 931151 --name 光伏产业
输出：data/index_daily_pb/{CODE}.json  （与 parse_mx_pb_daily.py 同 schema）
"""
from __future__ import annotations
import argparse, json, os, re, sys, time, datetime
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
OUT = os.path.join(DATA, "index_daily_pb")

MCP_URL = "https://mxapi.eastmoney.com/mxds/mcp"


def load_key():
    """读取 MCP 客户端配置里的 em_api_key。

    路径优先级：环境变量 XYD_MCP_JSON → 客户端的标准配置位置。
    两处都取不到时回退到环境变量 EM_API_KEY。
    """
    p = os.path.expanduser(
        os.environ.get("XYD_MCP_JSON") or "~/.workbuddy/mcp.json")
    if os.path.exists(p):
        try:
            d = json.load(open(p, encoding="utf-8"))
            s = d.get("mcpServers", {}).get("mx-ds-mcp", {})
            h = s.get("headers", {})
            k = h.get("em_api_key") or h.get("EM_API_KEY")
            if k:
                return k
        except Exception:
            pass
    return os.getenv("EM_API_KEY")


# ---------- 百分位（线性插值，与 parse_mx_pb_daily 一致）----------
def q(p, vals):
    if not vals:
        return None
    s = sorted(vals)
    n = len(s)
    pos = p * (n - 1) / 100.0
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return s[lo] + (s[hi] - s[lo]) * frac


def pct_rank(v, vals):
    if not vals:
        return None
    return round(100.0 * sum(1 for x in vals if x <= v) / len(vals), 2)


def window_5y(pts):
    end = pts[-1][0]
    end_d = datetime.date.fromisoformat(end)
    start_d = end_d - datetime.timedelta(days=365 * 5 + 2)
    return [p for p in pts if datetime.date.fromisoformat(p[0]) >= start_d]


class MCPClient:
    def __init__(self, url, api_key):
        self.url = url
        self.headers = {
            "em_api_key": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        self.sess = requests.Session()
        self.sess.headers.update(self.headers)
        self._id = 0

    def _rpc(self, method, params=None, is_notify=False):
        self._id += 1
        body = {"jsonrpc": "2.0", "method": method}
        if not is_notify:
            body["id"] = self._id
        if params is not None:
            body["params"] = params
        for attempt in range(3):
            try:
                r = self.sess.post(self.url, json=body, timeout=60)
                if r.status_code == 429:
                    wait = 5 * (attempt + 1)
                    print(f"  [429] 退避 {wait}s ...")
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                return self._parse_resp(r.text)
            except Exception as e:
                if attempt == 2:
                    raise
                time.sleep(2 * (attempt + 1))
        return None

    @staticmethod
    def _parse_resp(text):
        """解析 MCP 响应：可能是 SSE（text/event-stream）或纯 JSON。"""
        text = text.strip()
        if text.startswith("{"):
            return json.loads(text)
        # SSE: 多段 "event: ...\n data: {...}\n\n"
        best = None
        for block in re.split(r"\n\s*\n", text):
            for line in block.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    payload = line[len("data:"):].strip()
                    try:
                        obj = json.loads(payload)
                        if "result" in obj or "error" in obj:
                            best = obj
                    except Exception:
                        pass
        return best

    def initialize(self):
        self._rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "xy-fetch", "version": "1.0"},
        }, is_notify=False)
        self._rpc("notifications/initialized", is_notify=True)

    def call_tool(self, name, arguments):
        return self._rpc("tools/call", {"name": name, "arguments": arguments})


def build_record(code, label, columns, items):
    """columns: [指数名, 日期...]; items: [[指标名, 值...]]。"""
    dates = [str(c).replace("(日)", "") for c in columns[1:]]
    rec = None
    for it in items:
        name = it[0]
        if "市净率" not in str(name) and "PB" not in str(name).upper():
            continue
        vals_raw = it[1:]
        pts = []
        for dt, v in zip(dates, vals_raw):
            if v in (None, "-", ""):
                continue
            s = str(v).replace("倍", "").strip()
            try:
                f = float(s)
            except ValueError:
                continue
            pts.append([dt, f])
        if not pts:
            continue
        pts.sort(key=lambda x: x[0])
        end = pts[-1][0]
        pts = window_5y(pts)
        if len(pts) < 100:
            continue
        vals = [p[1] for p in pts]
        now = vals[-1]
        rec = {
            "index_code": code, "index_name": label,
            "source": "mx-ds-mcp-daily-pb",
            "as_of": end, "n": len(pts), "start": pts[0][0], "end": end,
            "pb": pts,
            "p5": round(q(5, vals), 4), "p20": round(q(20, vals), 4),
            "p50": round(q(50, vals), 4), "p80": round(q(80, vals), 4),
            "p95": round(q(95, vals), 4),
            "now": round(now, 4), "pct_now": pct_rank(now, vals),
        }
        break
    return rec


def fetch_one(client, code, name, end_date="2026-09-07"):
    query = f"{name}({code}) 从2021-08-02到{end_date}每个交易日的市净率PB"
    print(f"[MCP] {code} {name} -> {query}")
    resp = client.call_tool("mx_index_block_finance_data", {"query": query})
    if not resp:
        print(f"  [ERR] {code} 无响应")
        return None
    result = resp.get("result")
    if not result:
        print(f"  [ERR] {code} result 为空: {str(resp)[:120]}")
        return None
    # content[0].text 是 JSON 字符串
    text = None
    for c in result.get("content", []):
        if c.get("type") == "text":
            text = c.get("text")
            break
    if not text:
        print(f"  [ERR] {code} 无 text content")
        return None
    try:
        data = json.loads(text)
    except Exception as e:
        print(f"  [ERR] {code} JSON 解析失败: {e}")
        return None
    tables = data.get("data", [])
    if isinstance(tables, dict):
        tables = tables.get("data", [])
    for t in tables:
        cols = t.get("columns", [])
        items = t.get("items", [])
        if cols and items:
            rec = build_record(code, name, cols, items)
            if rec:
                return rec
    print(f"  [ERR] {code} 无有效 PB 序列（cols/items 缺失）")
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--map", help="JSON: {CODE: 中文名}")
    ap.add_argument("--code")
    ap.add_argument("--name")
    ap.add_argument("--end", default="2026-09-07")
    ap.add_argument("--sleep", type=float, default=1.0)
    a = ap.parse_args()

    key = load_key()
    if not key:
        print("ERROR: 找不到 em_api_key（mcp.json 或 EM_API_KEY）")
        sys.exit(1)
    client = MCPClient(MCP_URL, key)
    client.initialize()

    targets = {}
    if a.map:
        targets = json.load(open(a.map, encoding="utf-8"))
    elif a.code and a.name:
        targets = {a.code: a.name}

    os.makedirs(OUT, exist_ok=True)
    ok = 0
    for code, name in targets.items():
        try:
            rec = fetch_one(client, code, name, a.end)
        except Exception as e:
            print(f"  [EXC] {code}: {e}")
            rec = None
        if rec:
            with open(os.path.join(OUT, f"{code}.json"), "w", encoding="utf-8") as fh:
                json.dump(rec, fh, ensure_ascii=False)
            print(f"  [OK] {code} {name}: n={rec['n']} {rec['start']}~{rec['end']} "
                  f"now={rec['now']} 分位={rec['pct_now']}% P5={rec['p5']} P20={rec['p20']} "
                  f"P50={rec['p50']} P80={rec['p80']} P95={rec['p95']}")
            ok += 1
        time.sleep(a.sleep)
    print(f"\n完成：{ok}/{len(targets)} 只指数已落盘到 {OUT}")


if __name__ == "__main__":
    main()
