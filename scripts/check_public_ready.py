#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_public_ready.py —— 公开前的“全历史”闸门

与 check_no_secrets.py 的分工
------------------------------
``check_no_secrets.py`` 看**工作区 / 暂存区**，回答“这一次提交干不干净”。
本脚本看**整个历史**，回答“这个仓库能不能见人”。

差别不是重复劳动：一个密钥在第 3 次提交里写入、第 9 次提交又删掉时，
工作区检查完全通过，但克隆者一句 ``git log -p`` 就能拿到它。
历史一旦公开就无法撤回（fork 与缓存会留存），所以这一层必须在推送前跑。

检查项
------
1. **提交身份**：作者/提交者邮箱必须是不可反查的 noreply 形式，或显式放行
2. **历史内容**：所有可达 blob 不得命中凭证规则与个人绝对路径规则
3. **历史路径**：任何一次提交都不得包含 data/ runs/ archive/ 等目录
4. **体积**：单个 blob 不超过 5MB（大对象会永久拖慢克隆）
5. **必备文件**：README（中英）/ LICENSE / requirements.txt / SKILL.md 均在 HEAD
6. **离线 demo**：剥离全部凭证环境变量后仍能跑通（--no-demo 可跳过）

用法
----
  python scripts/check_public_ready.py                 # 检查 HEAD 的全部历史
  python scripts/check_public_ready.py --ref main      # 指定 ref
  python scripts/check_public_ready.py --no-demo       # 跳过 demo 执行
  python scripts/check_public_ready.py --allow-email me@example.com

退出码
------
  0  可以公开
  1  发现问题（阻断）
  2  无法执行（不在 git 仓库内 / git 不可用 / ref 不存在）

设计原则与 check_no_secrets.py 一致：**fail-closed**。跑不起来就按不通过处理。
"""
from __future__ import annotations

import argparse
import io
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import check_no_secrets as guard  # noqa: E402  (复用同一份规则，避免两套标准漂移)

MAX_BLOB_BYTES = 5 * 1024 * 1024

# 不可反查的邮箱：平台提供的 noreply 地址与 CI 机器人。
# 关键不是“看起来像假的”，而是“别人无法从中推出你的真实联系方式”。
ALLOWED_EMAIL_PATTERNS = [
    re.compile(r"@users\.noreply\.github\.com$", re.I),
    re.compile(r"@user\.noreply\.gitee\.com$", re.I),
    re.compile(r"^noreply@", re.I),
    re.compile(r"^actions@github\.com$", re.I),
    re.compile(r"^41898282\+github-actions\[bot\]@users\.noreply\.github\.com$", re.I),
    re.compile(r"^49699333\+dependabot\[bot\]@users\.noreply\.github\.com$", re.I),
]

REQUIRED_FILES = [
    "README.md",
    "README.zh-CN.md",
    "LICENSE",
    "requirements.txt",
    "SKILL.md",
    ".gitignore",
]

# 仓库名的唯一规范写法：首字母大写。
# 小写形态拆成两段拼接——否则本脚本会命中自己的规则（自指误报，
# 与个人路径检查踩过的是同一个坑）。
CANONICAL_NAME = "Ashare-sector-rotation"
_LOWER_NAME = "ashare-sector-" "rotation"


def git(args, stdin=None):
    """执行 git，返回 (成功?, stdout_bytes, stderr_text)。"""
    try:
        proc = subprocess.run(["git"] + args, input=stdin, capture_output=True)
    except (OSError, FileNotFoundError) as exc:
        return False, b"", str(exc)
    if proc.returncode != 0:
        return False, proc.stdout, proc.stderr.decode("utf-8", "replace").strip()
    return True, proc.stdout, ""


def resolve_ref(ref):
    ok, out, err = git(["rev-parse", "--verify", ref + "^{commit}"])
    if not ok:
        return None, err
    return out.decode().strip(), ""


def commits_of(sha):
    """返回 [(sha, 作者名, 作者邮箱, 提交者名, 提交者邮箱)]。"""
    ok, out, err = git(["log", "--format=%H%x00%an%x00%ae%x00%cn%x00%ce", sha])
    if not ok:
        return None, err
    rows = []
    for line in out.decode("utf-8", "replace").splitlines():
        parts = line.split("\0")
        if len(parts) == 5:
            rows.append(tuple(parts))
    return rows, ""


def object_paths(sha):
    """oid -> 路径。`rev-list --objects` 对 commit/tree 不给路径，这里只保留有的。"""
    ok, out, err = git(["rev-list", "--objects", sha])
    if not ok:
        return None, err
    mapping = {}
    for line in out.decode("utf-8", "replace").splitlines():
        parts = line.split(" ", 1)
        if len(parts) == 2:
            mapping.setdefault(parts[0], parts[1])
    return mapping, ""


def iter_blobs(oids):
    """流式读取 blob，逐条 yield (oid, bytes)。

    用单次 `git cat-file --batch` 而不是逐个调用：几百个对象时后者要几十秒，
    而这里只起一个进程。非 blob 对象（commit/tree）也会被读出后丢弃。
    """
    if not oids:
        return
    payload = ("\n".join(oids) + "\n").encode()
    proc = subprocess.run(["git", "cat-file", "--batch"], input=payload, capture_output=True)
    stream = io.BytesIO(proc.stdout)
    while True:
        header = stream.readline()
        if not header:
            return
        parts = header.rstrip(b"\n").split()
        if len(parts) < 3:          # "<sha> missing" 之类
            continue
        oid, otype = parts[0].decode(), parts[1].decode()
        size = int(parts[2])
        data = stream.read(size)
        stream.read(1)              # 每条记录后跟一个换行
        if otype == "blob":
            yield oid, data


def scan_history(sha, quiet):
    """历史内容与路径扫描，返回问题列表。"""
    problems = []

    mapping, err = object_paths(sha)
    if mapping is None:
        return None, f"无法枚举历史对象：{err}"

    # --- 3. 历史路径 ---
    bad_paths = {}
    for oid, path in mapping.items():
        hits = guard._check_path(path)
        if hits:
            bad_paths.setdefault(path, hits)

    for path, hits in sorted(bad_paths.items())[:20]:
        problems.append(f"[历史路径] {path}：{hits[0]}")

    # --- 2. 历史内容 ---
    oids = list(mapping)
    rules = guard.CRED_RULES + guard.PII_RULES
    content_hits = {}
    scanned = skipped = 0

    for oid, data in iter_blobs(oids):
        if len(data) > guard.MAX_BYTES or b"\x00" in data[:4096]:
            skipped += 1
            continue
        scanned += 1
        text = data.decode("utf-8", "replace")
        path = mapping.get(oid, oid[:8])
        for lineno, line in enumerate(text.splitlines(), start=1):
            if any(hint in line for hint in guard.SAFE_LINE_HINTS):
                continue
            for pattern, why in rules:
                match = pattern.search(line)
                if match:
                    content_hits.setdefault(path, []).append(
                        f"L{lineno} {why} → {guard._mask(match.group(0))}")
                    break

    for path, hits in sorted(content_hits.items()):
        problems.append(f"[历史内容] {path}：{hits[0]}" +
                        (f"（另有 {len(hits) - 1} 处）" if len(hits) > 1 else ""))

    if not quiet:
        print(f"[scan] 历史 blob {scanned} 个已扫描，{skipped} 个因二进制/超限跳过")

    return problems, ""


def check_identities(commits, allow_emails):
    """身份检查：邮箱必须不可反查。

    返回 {邮箱（名字）: {sha, ...}}。按 sha 去重，因为一次提交会同时贡献
    作者与提交者两个身份 —— 否则「1 次提交」会被报成「2 次提交」。
    """
    bad = {}
    allowed = {e.lower() for e in allow_emails}
    for sha, an, ae, cn, ce in commits:
        for name, email in ((an, ae), (cn, ce)):
            low = email.strip().lower()
            if low in allowed:
                continue
            if any(p.search(email.strip()) for p in ALLOWED_EMAIL_PATTERNS):
                continue
            bad.setdefault(f"{email}（{name}）", set()).add(sha)
    return bad


def check_large_blobs(mapping):
    """体积检查：单个 blob 超过阈值会永久拖慢每一次克隆。"""
    ok, out, _ = git(["cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"],
                     stdin=("\n".join(mapping) + "\n").encode())
    if not ok:
        return []
    big = []
    for line in out.decode("utf-8", "replace").splitlines():
        parts = line.split()
        if len(parts) != 3 or parts[1] != "blob":
            continue
        size = int(parts[2])
        if size > MAX_BLOB_BYTES:
            big.append((mapping.get(parts[0], parts[0][:8]), size))
    return sorted(big, key=lambda x: -x[1])


def check_required_files(sha):
    ok, out, err = git(["ls-tree", "-r", "--name-only", "-z", sha])
    if not ok:
        return None, err
    present = {p.replace("\\", "/") for p in out.decode("utf-8", "replace").split("\0") if p}
    return [f for f in REQUIRED_FILES if f not in present], ""


def check_naming(sha):
    """仓库名必须在全库统一为大写开头的规范写法。

    GitHub 对 URL 大小写不敏感、旧链接自动 301，所以小写写法不会 404——正因如此
    它很容易被放过，最终表现为仓库页、README、文档三处显示不一致。这个检查把
    「不会报错」变成「会变红」。
    """
    ok, out, err = git(["grep", "-nI", "--no-color", "-e", _LOWER_NAME, sha])
    if not ok:
        if err.strip():                     # 退出码 1 = 无命中（通过）；>1 = 出错
            return [f"[命名] git grep 执行失败：{err.strip()[:120]}"]
        return []
    hits = []
    for line in out.decode("utf-8", "replace").splitlines():
        parts = line.split(":", 3)          # <ref>:<path>:<lineno>:<text>
        if len(parts) == 4:
            hits.append(f"[命名] {parts[1]}:{parts[2]} 用了小写仓库名，"
                        f"应统一为 {CANONICAL_NAME}")
    return hits


def run_demo():
    """剥离凭证后跑离线 demo：README 的核心承诺，必须由脚本验证而非声称。"""
    env = {k: v for k, v in os.environ.items()
           if not k.upper().endswith(("APIKEY", "API_KEY", "TOKEN", "SECRET"))}
    try:
        proc = subprocess.run([sys.executable, "discipline.py", "--demo"],
                              cwd=ROOT, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=env, timeout=180)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if proc.returncode != 0:
        tail = (proc.stdout + proc.stderr).strip().splitlines()[-3:]
        return False, f"退出码 {proc.returncode}：{' / '.join(tail)}"
    if "params=" not in proc.stdout:
        return False, "输出里没有 params 指纹，可能跑的不是 demo"
    return True, ""


def main():
    ap = argparse.ArgumentParser(description="公开前的全历史闸门（fail-closed）")
    ap.add_argument("--ref", default="HEAD", help="要检查的 ref（默认 HEAD）")
    ap.add_argument("--quiet", action="store_true", help="只输出结论")
    ap.add_argument("--no-demo", action="store_true", help="跳过离线 demo 检查")
    ap.add_argument("--allow-email", action="append", default=[],
                    help="额外放行的邮箱（可重复），用于已知可信的机器人账号")
    args = ap.parse_args()

    ok, _, _ = git(["rev-parse", "--is-inside-work-tree"])
    if not ok:
        print("[FAIL] 当前目录不是 git 仓库。", file=sys.stderr)
        return 2

    sha, err = resolve_ref(args.ref)
    if sha is None:
        print(f"[FAIL] 找不到 ref {args.ref}：{err}", file=sys.stderr)
        return 2

    commits, err = commits_of(sha)
    if commits is None:
        print(f"[FAIL] 无法读取提交历史：{err}", file=sys.stderr)
        return 2

    problems = []

    # --- 1. 身份 ---
    bad_ids = check_identities(commits, args.allow_email)
    for who, shas in bad_ids.items():
        problems.append(f"[提交身份] {who} 出现在 {len(shas)} 次提交（如 {sorted(shas)[0][:7]}）")

    # --- 2 & 3. 历史内容与路径 ---
    hist_problems, err = scan_history(sha, args.quiet)
    if hist_problems is None:
        print(f"[FAIL] {err}", file=sys.stderr)
        return 2
    problems.extend(hist_problems)

    # --- 4. 体积 ---
    mapping, _ = object_paths(sha)
    for path, size in check_large_blobs(mapping or {}):
        problems.append(f"[体积] {path} 有 {size / 1048576.0:.1f}MB，超过 "
                        f"{MAX_BLOB_BYTES // 1048576}MB 上限")

    # --- 5. 必备文件 ---
    missing, err = check_required_files(sha)
    if missing is None:
        print(f"[FAIL] 无法读取文件树：{err}", file=sys.stderr)
        return 2
    for name in missing:
        problems.append(f"[必备文件] 缺少 {name}")

    # --- 6. 离线 demo ---
    if not args.no_demo:
        demo_ok, why = run_demo()
        if not demo_ok:
            problems.append(f"[离线 demo] discipline.py --demo 未通过：{why}")

    # --- 7. 命名一致性 ---
    problems.extend(check_naming(sha))

    print()
    if not problems:
        print(f"[PASS] {len(commits)} 次提交可公开：身份已匿名化、历史无凭证与个人路径、"
              f"无禁入目录、无超大对象、必备文件齐全、仓库名统一、离线 demo 通过。")
        return 0

    print(f"[FAIL] 该 ref 还不能公开，共 {len(problems)} 项问题：")
    for item in problems:
        print(f"  - {item}")
    print()
    print("处理方式：")
    print("  身份类 → git filter-branch --env-filter 改写后 gc，再 force-push")
    print("  内容类 → 当前提交修掉不够，必须改写历史（见 docs/PUBLISHING.md）")
    print("  路径类 → 确认 .gitignore 生效后再重新提交；已入历史的需 filter-branch --index-filter")
    print("  体积类 → 大对象需 git filter-repo --strip-blobs-bigger-than")
    print(f"  命名类 → 改成 {CANONICAL_NAME}（首字母大写），三处都要改：README、文档、remote")
    return 1


if __name__ == "__main__":
    sys.exit(main())
