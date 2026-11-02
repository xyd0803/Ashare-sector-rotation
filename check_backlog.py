#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_backlog.py —— 欠账登记表机械校验（L-4 · 2026-10-25 第三批）

背景（三维审计 · 逻辑层 L-4）：
  「V11/V13 待定」这句话在 `技能审议_2026-08-16.md` 里躺了两个月，每轮分析者都要
  重新推演一遍它们是什么、卡在哪。根因不是遗忘，而是**欠账没有可机械查询的载体**：
  它们散在 CHANGELOG 的叙事段落里，人读得懂，机器读不到。
  `BACKLOG.md` 是载体，本脚本是**闸门**——避免「表建好之后慢慢烂掉」：
  ID 重复、状态写成「等有空」、依赖指向不存在的 ID，都必须当场 FAIL。

校验项（任一不过 → exit 1）：
  1. BACKLOG.md 存在且含 `<!-- BACKLOG:START/END -->` 标记
  2. 表头列齐全且顺序固定
  3. 每行 7 列（管道符转义不处理——本表契约为纯文本单元格）
  4. ID 形如 `B-NN`、全局唯一、连续（从 B-01 起无空洞）
  5. 等级 ∈ {P0,P1,P2}；状态 ∈ {待定,待办,进行中,已完成,已关闭}
  6. 定义/验收 ≥ 20 字（防空壳条目）
  7. 依赖为 `—` 或已存在的 ID（且不自依赖）

用法：
  python check_backlog.py                # 校验并打印统计（open/closed 分档）
  python check_backlog.py --selfcheck    # 供 selfcheck_all.py 调用（同一校验，静默细节）
"""
import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "BACKLOG.md")
START, END = "<!-- BACKLOG:START -->", "<!-- BACKLOG:END -->"
COLS = ["ID", "欠账", "来源", "等级", "状态", "定义 / 验收标准", "依赖"]
LEVELS = {"P0", "P1", "P2"}
STATUSES = {"待定", "待办", "进行中", "已完成", "已关闭"}
OPEN_STATUSES = {"待定", "待办", "进行中"}
MIN_DEF_LEN = 20


def _cells(line: str) -> list:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def parse(md_text: str) -> tuple[list[dict], list[str]]:
    """解析 BACKLOG.md → (rows, errors)。errors 非空即表结构破损。"""
    errs = []
    if START not in md_text or END not in md_text:
        return [], [f"缺少 {START} / {END} 标记"]
    block = md_text.split(START, 1)[1].split(END, 1)[0]
    lines = [ln for ln in block.splitlines() if ln.strip().startswith("|")]
    if len(lines) < 2:
        return [], ["表体不足（需表头 + 分隔行 + ≥1 数据行）"]
    header = _cells(lines[0])
    if header != COLS:
        errs.append(f"表头不符：got {header} / want {COLS}")
    rows = []
    # lines[1] 是 |----| 分隔行
    for ln in lines[2:]:
        cs = _cells(ln)
        if len(cs) != len(COLS):
            errs.append(f"列数 {len(cs)}≠{len(COLS)}：{ln[:60]}")
            continue
        rows.append(dict(zip(COLS, cs)))
    return rows, errs


def validate(rows: list[dict], errs: list[str]) -> list[str]:
    errs = list(errs)
    ids = [r["ID"] for r in rows]
    seen = set()
    for r in rows:
        rid = r["ID"]
        if not re.fullmatch(r"B-\d{2}", rid or ""):
            errs.append(f"ID 非法：{rid!r}（须 B-NN）")
        if rid in seen:
            errs.append(f"ID 重复：{rid}")
        seen.add(rid)
        if r["等级"] not in LEVELS:
            errs.append(f"{rid} 等级非法：{r['等级']!r}")
        if r["状态"] not in STATUSES:
            errs.append(f"{rid} 状态非法：{r['状态']!r}")
        if len(r["定义 / 验收标准"]) < MIN_DEF_LEN:
            errs.append(f"{rid} 定义/验收过短（{len(r['定义 / 验收标准'])} < {MIN_DEF_LEN} 字）→ 空壳条目")
        if not r["欠账"]:
            errs.append(f"{rid} 欠账名称为空")
        if not r["来源"]:
            errs.append(f"{rid} 来源为空（须可追溯文档/章节）")
    # 连续性
    nums = sorted(int(i.split("-")[1]) for i in seen
                  if re.fullmatch(r"B-\d{2}", i or ""))
    if nums and nums != list(range(1, len(nums) + 1)):
        errs.append(f"ID 不连续（应 1..{len(nums)}）：{nums}")
    # 依赖存在性
    for r in rows:
        dep = r["依赖"]
        if dep in ("—", "-", "", "无"):
            continue
        for d in [x.strip() for x in re.split(r"[,，/、]", dep) if x.strip()]:
            if d == r["ID"]:
                errs.append(f"{r['ID']} 自依赖")
            elif d not in seen:
                errs.append(f"{r['ID']} 依赖不存在的 {d}")
    return errs


def report(rows: list[dict]) -> None:
    n_open = sum(1 for r in rows if r["状态"] in OPEN_STATUSES)
    n_done = len(rows) - n_open
    print(f"  欠账总数 {len(rows)}（open={n_open} / closed={n_done}）")
    for lv in ("P0", "P1", "P2"):
        sub = [r for r in rows if r["等级"] == lv]
        if not sub:
            continue
        o = sum(1 for r in sub if r["状态"] in OPEN_STATUSES)
        print(f"    {lv}: {len(sub)} 条（open {o}）")
    for r in rows:
        if r["状态"] in OPEN_STATUSES:
            print(f"      · {r['ID']} [{r['等级']}/{r['状态']}] {r['欠账']}")


def main() -> int:
    ap = argparse.ArgumentParser(description="欠账登记表机械校验（L-4）")
    ap.add_argument("--selfcheck", action="store_true", help="selfcheck_all 调用模式")
    a = ap.parse_args()
    if not os.path.exists(PATH):
        print(f"❌ BACKLOG.md 不存在：{PATH}", file=sys.stderr)
        return 1
    with open(PATH, encoding="utf-8") as f:
        rows, errs = parse(f.read())
    errs = validate(rows, errs)
    if errs:
        print(f"❌ BACKLOG 校验失败 {len(errs)} 项：", file=sys.stderr)
        for e in errs:
            print(f"   - {e}", file=sys.stderr)
        return 1
    report(rows)
    print("  ✅ BACKLOG 表结构与枚举校验通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
