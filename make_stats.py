#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_stats.py —— SKILL.md 状态统计块生成器（2026-09-07 · 审议 RC2 根治）

把「红队用例数 / ETF 映射数 / PB·PE 数据文件数 / 模块清单」等易漂移的 prose 数字
改为脚本生成的 AUTO 块，嵌入 SKILL.md 的 <!-- AUTO:STATS:START/END --> 标记之间。
用法：
  python make_stats.py            # 重新生成并写回 SKILL.md
  python make_stats.py --check    # 校验当前块是否与实际一致（不一致 exit 1）
"""
import glob
import json
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.join(HERE, "SKILL.md")
START = "<!-- AUTO:STATS:START -->"
END = "<!-- AUTO:STATS:END -->"


def collect() -> dict:
    s = {}
    try:
        import redteam_sector
        s["redteam_cases"] = len(redteam_sector.CASES)
    except Exception as e:
        s["redteam_cases"] = f"ERR({e})"
    try:
        with io.open(os.path.join(HERE, "data", "meta", "etf_index_map.json"), encoding="utf-8") as f:
            s["etf_map"] = len(json.load(f))
    except Exception as e:
        s["etf_map"] = f"ERR({e})"
    s["pb_files"] = len(glob.glob(os.path.join(HERE, "data", "index_daily_pb", "*.json")))
    s["pe_files"] = len(glob.glob(os.path.join(HERE, "data", "index_daily_pe", "*.json")))
    try:
        with io.open(os.path.join(HERE, "data", "meta", "pb_daily_bands.json"), encoding="utf-8") as f:
            s["pb_registry"] = len(json.load(f))
    except Exception:
        s["pb_registry"] = "?"
    s["py_modules"] = len([p for p in glob.glob(os.path.join(HERE, "*.py"))
                           if not os.path.basename(p).startswith("_")])
    try:
        with io.open(os.path.join(HERE, "params.json"), encoding="utf-8") as f:
            s["params_version"] = json.load(f).get("version", "?")
    except Exception:
        s["params_version"] = "?"
    return s


def render(s: dict) -> str:
    L = [START, "## 当前状态（AUTO 统计 · 由 `make_stats.py` 生成，勿手改）", ""]
    L.append(f"- 红队对抗用例：**{s['redteam_cases']}**（`python redteam_sector.py`）")
    L.append(f"- ETF→指数映射：**{s['etf_map']}** 只（etf_index_map.json）；PB 日频序列文件 **{s['pb_files']}**（registry {s['pb_registry']}）；PE 日频序列文件 **{s['pe_files']}**（data/index_daily_pe/）")
    L.append(f"- params.json 版本：**{s['params_version']}**（指纹锁 params.lock）")
    L.append(f"- 顶层 Python 模块：**{s['py_modules']}** 个（`_`前缀临时件不计）")
    L.append(f"- 统一生成时间：见 git/文件时间戳（统计在 run_all Preflight 后随 `make_stats.py --check` 校验）")
    L.append(END, )
    return "\n".join(L) + "\n"


def main() -> int:
    check = "--check" in sys.argv
    s = collect()
    block = render(s)
    with io.open(SKILL, encoding="utf-8") as f:
        text = f.read()
    if START in text:
        pre = text.split(START)[0]
        post = text.split(END, 1)[1]
        new = pre + block + post
        if check:
            cur = text[text.index(START): text.index(END) + len(END)]
            gen = block[: block.rindex(END) + len(END)]
            if cur.strip() == gen.strip():
                print(f"make_stats --check: OK（红队{s['redteam_cases']}/ETF{s['etf_map']}/PB{s['pb_files']}/PE{s['pe_files']}）")
                return 0
            print("make_stats --check: FAIL —— SKILL.md 统计块与实际不一致，请运行 `python make_stats.py` 再生", file=sys.stderr)
            return 1
        with io.open(SKILL, "w", encoding="utf-8") as f:
            f.write(new)
        print(f"make_stats: 已更新 SKILL.md 统计块（红队{s['redteam_cases']}/ETF{s['etf_map']}/PB{s['pb_files']}/PE{s['pe_files']}）")
        return 0
    else:
        # 首次嵌入：插在「## 标准执行流程」之前
        anchor = "## 标准执行流程"
        if anchor not in text:
            print("SKILL.md 未找到锚点章节", file=sys.stderr)
            return 1
        new = text.replace(anchor, block + "\n" + anchor, 1)
        with io.open(SKILL, "w", encoding="utf-8") as f:
            f.write(new)
        print("make_stats: 已首次嵌入统计块")
        return 0


if __name__ == "__main__":
    sys.exit(main())
