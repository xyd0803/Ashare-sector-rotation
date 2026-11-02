#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_no_secrets.py —— 提交前敏感信息自检（凭证 / 个人路径 / 不该入仓的目录）

为什么需要它
------------
本项目的全部数据源（妙想、iFinD、通达信、腾讯自选股）都依赖**你自己的账号与 API Key**。
密钥一旦进入 git 历史，即使后续删除也仍可被 `git log -p` 翻出，等于永久泄露。
因此把「检查」做成可执行脚本，而不是靠记忆。

用法
----
  python scripts/check_no_secrets.py            # 扫描「已跟踪 + 未忽略的新文件」（日常自检）
  python scripts/check_no_secrets.py --staged   # 只扫暂存区（pre-commit 钩子用）
  python scripts/check_no_secrets.py --quiet    # 只输出结论，不列出干净文件

分支感知（--private-mode）
--------------------------
私有备份分支（默认名以 ``local-backup`` 开头）上，``data/`` ``runs/`` 等目录与
个人绝对路径是**预期的**，检查会豁免这两类规则；
但**凭证规则与 mcp.json/.env/私钥路径黑名单永远生效**——私有备份同样可能是泄露源。
默认 auto 按当前分支判断，也可用 ``--private-mode on/off`` 强制。

退出码
------
  0  干净
  1  发现问题（含疑似凭证、个人绝对路径、不该入仓的目录）
  2  无法执行（不在 git 仓库内 / git 不可用）

设计原则：**fail-closed**。识别不了、跑不起来，一律按「不通过」处理，绝不静默放行。
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys

# ---------- 1. 不该被跟踪的路径 ----------
# 即便有人用 `git add -f` 强推，也要在这里拦下。
FORBIDDEN_PATH = [
    (re.compile(r"(^|/)mcp\.json$"), "本机 MCP 配置（含 em_api_key）"),
    (re.compile(r"(^|/)\.env(\.|$)"), "环境变量文件"),
    (re.compile(r"\.(key|pem|p12|pfx)$"), "私钥 / 证书"),
    (re.compile(r"(^|/)(secrets?|credentials?)(\.|/|$)"), "凭据文件"),
]

# 这些目录整个不该出现在版本库里
FORBIDDEN_DIR = [
    ("data", "数据目录（供应商分发权 + 含本机路径脚本）"),
    ("runs", "历史裁决（含具体标的买卖结论）"),
    ("archive", "历史归档"),
    ("drafts", "草稿"),
    ("logs", "日志"),
    (".evidence", "证据目录（含 MCP 原始返回 / 配额账本）"),
    (".rt_tmp", "运行时临时目录"),
    ("__pycache__", "Python 字节码缓存"),
]

# ---------- 2a. 凭证特征（任何分支都不得命中，私有备份同样不行） ----------
CRED_RULES = [
    # 变量名像密钥，且赋了 12 位以上的字面量（排除值为变量名/表达式的情况）
    (re.compile(
        r"""(?i)\b(api[_-]?key|apikey|em_api_key|mx_apikey|access[_-]?key|accesskey|
             secret[_-]?key|client[_-]?secret|private[_-]?key|token|password|passwd|
             webhook)\b\s*[:=]\s*["']([A-Za-z0-9_\-\.\+\/]{12,})["']""",
        re.X),
     "疑似硬编码密钥（变量名 + 字面量取值）"),

    # 各类平台 Key 的固定前缀
    (re.compile(r"\bsk-[A-Za-z0-9]{16,}"), "疑似 OpenAI 风格密钥 sk-..."),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "疑似 AWS Access Key"),
    (re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"), "疑似 GitHub Token"),
    (re.compile(r"\bglpat-[A-Za-z0-9\-_]{20,}"), "疑似 GitLab Token"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"), "疑似 Slack Token"),

    # JWT（MCP / 网关类鉴权常见）
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\."), "疑似 JWT"),
]

# ---------- 2b. 个人绝对路径（会被用户名带进公开历史） ----------
# 私有备份分支上豁免：本机路径在 data/、runs/ 里不可避免，且该分支不外发。
# 注：字面量拆成两段拼接，避免本脚本自身的源码被这几条规则误命中（自指误报）。
PII_RULES = [
    (re.compile(r"[A-Za-z]:[\\/]+Use" r"rs[\\/]+[^\\/\s\"']+"), "Windows 个人绝对路径（含用户名）"),
    (re.compile(r"(?<![\w])/Us" r"ers/[^/\s\"']+"), "macOS 个人绝对路径（含用户名）"),
    (re.compile(r"(?<![\w])/ho" r"me/[^/\s\"']+"), "Linux 个人绝对路径（含用户名）"),
]

# 私有备份分支：这些目录在这里是「合法内容」，不是误入仓。
# 注意 FORBIDDEN_PATH（mcp.json / .env / 私钥）**不在**豁免之列。
PRIVATE_BRANCH_PREFIX = "local-backup"
FORBIDDEN_DIR_EXEMPT_ON_PRIVATE = {"data", "runs", "archive", "drafts", "logs"}

# 命中这些词的行视为已知安全（参数指纹是设计的一部分，不是密钥）
SAFE_LINE_HINTS = ("params_fingerprint", "fingerprint", "sha256", "sha1", "md5")

MAX_BYTES = 4 * 1024 * 1024   # 超过 4MB 的文件跳过（数据文件本就不该入仓）


def _run_git(args):
    """执行 git 命令，返回 (成功?, stdout)。"""
    try:
        proc = subprocess.run(["git"] + args, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
    except (OSError, FileNotFoundError):
        return False, ""
    if proc.returncode != 0:
        return False, proc.stderr.strip()
    return True, proc.stdout


def _list_targets(staged):
    """返回待检查的相对路径列表（正斜杠）。

    用 `-z` 取文件名：默认输出会把中文等非 ASCII 文件名转义成 `"\\346\\240\\241..."`，
    既读不到文件、也会让路径黑名单漏判。`-z` 输出原始字节，NUL 分隔，不做任何转义。
    """
    if staged:
        ok, out = _run_git(["diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR"])
    else:
        # -c 已跟踪 / -o 未跟踪 / --exclude-standard 遵守 .gitignore
        ok, out = _run_git(["ls-files", "-co", "--exclude-standard", "-z"])
    if not ok:
        return None, out
    return [p.replace("\\", "/") for p in out.split("\0") if p.strip()], ""


def _check_path(path, private=False):
    """路径级规则：返回问题描述列表。

    private=True（私有备份分支）时豁免「不该入仓的目录」，
    但 mcp.json / .env / 私钥这类路径黑名单始终生效。
    """
    problems = []
    for pattern, why in FORBIDDEN_PATH:
        if pattern.search(path):
            problems.append(f"路径命中黑名单：{why}")
    parts = path.split("/")
    for bad_dir, why in FORBIDDEN_DIR:
        if bad_dir in parts[:-1]:
            if private and bad_dir in FORBIDDEN_DIR_EXEMPT_ON_PRIVATE:
                continue
            problems.append(f"位于不该入仓的目录 {bad_dir}/：{why}")
            break
    return problems


def _mask(text):
    """把疑似密钥打码，避免自检输出本身二次泄密。"""
    if len(text) <= 8:
        return text[:2] + "*" * max(1, len(text) - 2)
    return text[:4] + "*" * min(12, len(text) - 4)


def _check_content(path, private=False):
    """内容级规则：返回 [(行号, 说明, 打码片段)]。

    凭证规则恒定生效；个人绝对路径规则在私有备份分支豁免
    （data/ 与 runs/ 里出现本机路径不可避免，且该分支不外发）。
    """
    hits = []
    with open(path, "rb") as fh:
        raw = fh.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES or b"\x00" in raw[:4096]:
        return hits                      # 过大或二进制，跳过
    rules = CRED_RULES if private else CRED_RULES + PII_RULES
    text = raw.decode("utf-8", errors="replace")
    for lineno, line in enumerate(text.splitlines(), start=1):
        if any(hint in line for hint in SAFE_LINE_HINTS):
            continue
        for pattern, why in rules:
            match = pattern.search(line)
            if match:
                snippet = match.group(0)
                hits.append((lineno, why, _mask(snippet)))
                break                    # 一行只报一次，避免刷屏
    return hits


def _current_branch():
    """当前分支名。未提交的新分支（unborn）也能取到，`rev-parse` 则取不到。"""
    ok, out = _run_git(["symbolic-ref", "--short", "HEAD"])
    return out.strip() if ok else ""


def main():
    ap = argparse.ArgumentParser(description="提交前敏感信息自检（fail-closed）")
    ap.add_argument("--staged", action="store_true", help="只检查暂存区（pre-commit 用）")
    ap.add_argument("--quiet", action="store_true", help="只输出结论")
    ap.add_argument("--private-mode", choices=("auto", "on", "off"), default="auto",
                    help="私有备份分支模式：豁免个人路径与禁入目录，凭证规则仍强制（默认 auto=按分支判断）")
    args = ap.parse_args()

    ok, _ = _run_git(["rev-parse", "--is-inside-work-tree"])
    if not ok:
        print("[FAIL] 当前目录不是 git 仓库，无法确定检查范围。请先在仓库根目录执行。", file=sys.stderr)
        return 2

    branch = _current_branch()
    if args.private_mode == "on":
        private = True
    elif args.private_mode == "off":
        private = False
    else:
        private = branch.startswith(PRIVATE_BRANCH_PREFIX)

    targets, err = _list_targets(args.staged)
    if targets is None:
        print(f"[FAIL] 无法列出待检查文件：{err}", file=sys.stderr)
        return 2

    if not args.quiet:
        scope = "暂存区" if args.staged else "已跟踪 + 未忽略的新文件"
        print(f"[check] 检查范围：{scope}，共 {len(targets)} 个文件")
        if private:
            print(f"[check] 分支 {branch or '(unborn)'}：私有备份模式 —— "
                  f"个人路径与 data/runs 等目录豁免，凭证规则仍然强制")

    findings = []          # [(path, [问题...])]
    for path in targets:
        problems = _check_path(path, private=private)
        try:
            for lineno, why, snippet in _check_content(path, private=private):
                problems.append(f"L{lineno} {why} → {snippet}")
        except (OSError, PermissionError) as exc:
            problems.append(f"无法读取（按不通过处理）：{exc}")
        if problems:
            findings.append((path, problems))

    print()
    if not findings:
        suffix = "（私有备份模式）" if private else ""
        print(f"[PASS] {len(targets)} 个文件全部通过：无凭证字面量、无个人绝对路径、无禁入目录。{suffix}")
        return 0

    print(f"[FAIL] {len(findings)} 个文件存在问题，已阻断：")
    for path, problems in findings:
        print(f"  {path}")
        for item in problems:
            print(f"    - {item}")
    print()
    print("处理方式：① 若确为凭证 → 移出仓库并改用环境变量（见 docs/DATA_SOURCES.md）；")
    print("          ② 若只是本地路径 → 改为 os.path.expanduser('~/...') 或相对路径；")
    print("          ③ 若整个文件都不该入仓 → 加进 .gitignore（必要时 git rm --cached）。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
