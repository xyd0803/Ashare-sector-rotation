# 公开发布手册（内部）

> 这份文档是给仓库主人看的，不是给使用者看的。它记录的是「把仓库暴露到互联网」这一步的
> 边界与操作，以及**为什么这些边界不能让脚本替你猜**。

## 0. 先理解风险：历史一旦公开不可撤回

推送之后，即使立刻删除仓库，也可能已经被 fork、被搜索引擎缓存、被 GH Archive 抓取。
因此判断标准不是「现在的代码干不干净」，而是**「任何一次提交里有没有不该见人的东西」**。

泄露面只有三个，逐个堵：

| 泄露面 | 典型形态 | 谁能让它变红 |
|---|---|---|
| 提交身份 | 提交里带着真实邮箱，可反查到社交账号 | `check_public_ready.py` 身份检查 |
| 历史内容 | 密钥、个人绝对路径（含用户名） | `check_public_ready.py` 内容扫描 |
| 历史路径 | `data/`、`runs/` 等整目录被提交过 | `check_public_ready.py` 路径扫描 |

## 1. 两个分支的边界（最重要的一条）

| 分支 | 内容 | 可以推到哪里 |
|---|---|---|
| `main` | 代码、文档、测试、CI（108 个文件） | Gitee 私有仓 + GitHub 公开仓 |
| `local-backup` | `main` + `data/` `runs/` `archive/` `drafts/`（约 516 个文件） | **仅** Gitee 私有仓 |

`local-backup` 与 `main` **不共享历史**（孤儿分支），所以把 `main` 推公开时，
备份分支的任何内容都不可能被带出去。

`.githooks/pre-push` 会主动拦截「把 `local-backup` 推到 github.com」这件事，
所以即使误敲 `git push github --all` 也会被挡住。

## 2. 发布前三道闸（每次推公开仓之前都跑）

```bash
python scripts/check_no_secrets.py --quiet     # 工作区：这次改动干不干净
python scripts/check_public_ready.py --quiet   # 全历史：这个仓库能不能见人
python -m pytest -q                            # 代码本身还对不对
```

三条都要 exit 0。`check_public_ready.py` 覆盖六项：提交身份、历史凭证、
历史个人路径、历史禁入目录、单对象体积、离线 demo 是否仍能跑。

钩子已经自动化了其中两道：`pre-commit` 跑第一道，`pre-push` 跑第二道
（且只对非 `local-backup` 分支跑）。启用一次即可：

```bash
git config core.hooksPath .githooks
```

## 3. 首次推 GitHub

### 3.1 建仓

在 GitHub 新建**空仓库**，不要勾选自动生成 README / LICENSE / .gitignore——
否则首次 push 会因为历史无关而被拒。

### 3.2 身份：先决定用哪个邮箱

当前全部 11 条提交的邮箱是 `<gitee-noreply>@user.noreply.gitee.com`。
它是 Gitee 的 noreply 地址：**不可反查**，所以在 GitHub 上公开也不会泄露联系方式。

如果你想换成 GitHub 自己的 noreply（形如 `12345+用户名@users.noreply.github.com`，
可以在 GitHub → Settings → Emails 里查到），注意 `git config` **只影响新提交**，
已有历史要再改写一次：

```bash
FILTER_BRANCH_SQUELCH_WARNING=1 git filter-branch -f --env-filter '
export GIT_AUTHOR_EMAIL="你的GitHub noreply"
export GIT_COMMITTER_EMAIL="你的GitHub noreply"
' -- --branches
git update-ref -d refs/original/refs/heads/main 2>/dev/null
git reflog expire --expire=now --all && git gc --prune=now --quiet
git push --force-with-lease origin main
```

改写后务必重跑 `check_public_ready.py`：旧邮箱会以 `refs/original` 或 reflog
的形式残留，只有 `gc` 之后才真正消失。

### 3.3 推送

```bash
git remote add github https://github.com/<你的账号>/xyd-supply-demand-sector.git
git push github main          # 只推 main
```

**不要**用 `git push --all` 或 `git push --mirror`：前者会把 `local-backup`
一并送出（钩子会拦，但没必要走到那一步），后者会连远端跟踪分支一起镜像。

### 3.4 仓库设置（决定别人会不会点进来）

- **Description**：一句话说清「是什么 + 给谁用」，不要写「A quant framework」。
  例：`Auditable buy/sell verdicts for A-share sector ETFs — win rate, valuation
  percentile, position size, and the reasons it refused to be optimistic.`
- **Topics**：`quant` `a-share` `etf` `sector-rotation` `valuation` `investment-research`
- **Social preview**：上传一张 1280×640 的图（终端 demo 输出截图即可）
- **About → Releases**：打 `v1.1.0`，附一条 CHANGELOG 摘要

## 4. 改名（可选）

仓库名里的 `xyd` 对陌生人没有信息量。若改成 `ashare-sector-rotation` 一类：

```bash
grep -rn "xyd-supply-demand-sector" README.md README.zh-CN.md
```

只需改两份 README 的 `git clone` 与 `cd` 两行（当前已是 `<your-account>` 占位，
改的只是仓库名部分）。GitHub 会自动重定向旧地址，不会产生死链。

## 5. 日常同步

```bash
# 1) 本地跑闸
python scripts/check_public_ready.py --quiet && python -m pytest -q

# 2) 先推私有仓（有备份，心里有底）
git push origin main
python scripts/backup_private_data.py --push

# 3) 再推公开仓
git push github main
```

顺序刻意是「私有 → 公开」：万一公开那步出问题，代码至少已经在私有仓里。

## 6. 本地技能不受影响的五条铁律

这个目录**同时是活动技能目录和 git 仓库**（`~/.workbuddy/skills/XYD-supply-demand-sector`）。
git 只是在旁边多了一个 `.git/`，技能加载只读 `SKILL.md`，所以**加版本控制本身不影响技能运行**。
真正会伤到技能的是下面五件事：

1. **不要在这个目录切分支。**
   `git checkout local-backup` 会把 516 个备份文件倒进工作区，技能目录瞬间变成另一副样子。
   备份走独立 worktree（`~/.workbuddy/skills/.XYD-supply-demand-sector-local-backup`），
   要用就 `git worktree add ./restore local-backup` 到别的路径去。

2. **永远不要在这里执行 `git clean -xdf`。**
   `data/` `runs/` `archive/` `drafts/` `logs/` 都是 **ignored 的未跟踪文件**：
   `git pull`、`git checkout main`、`git reset --hard` 都不会碰它们，
   但 `git clean -xdf` 会把它们**全部删除**，且不进回收站。

3. **`git pull` 只改被跟踪的代码。**
   如果哪天担心，pull 之前先 `python scripts/backup_private_data.py` 存一份快照。

4. **合并外部 PR 要绕一道。**
   技能目录就是工作目录，直接把别人的 PR 合进来 = 直接改你正在用的技能。
   正确顺序：在临时克隆里合、跑 `pytest` + `--selfcheck`、确认无误再 `git push origin main`。

5. **技能被复制/改名后仍然可用。**
   `SKILL.md` 在仓库根，所有运行时代码用相对路径 + `HERE`，没有绝对路径依赖，
   所以 `git clone` 到任意目录名都能当技能用（已用干净克隆验证过）。

## 7. CI 与 history gate

GitHub Actions 默认是 **浅克隆**（`fetch-depth: 1`），历史闸门会因此看不到旧提交、
把「历史里有密钥」误判成干净。所以 `ci.yml` 里 gate 那个 job 显式设了
`fetch-depth: 0`——这是很容易被忽略、但会让整道闸失去意义的一行。

该 job 只在 `push` 时运行，不在 `pull_request` 时运行：外部贡献者的提交邮箱不受你控制，
对 PR 跑身份检查只会制造噪声。

Gitee 侧没有 Actions。要在 Gitee 上也有 CI 需要开通 Gitee Go 并单独写流水线；
私有仓自用可以不做，代码质量由本地 `pytest` + 推送钩子保证。

## 8. 失败处置表

| 症状 | 原因 | 处置 |
|---|---|---|
| `[提交身份] <某真实邮箱>` | 历史里有真实邮箱 | 按 3.2 的 filter-branch 改写，再 `gc` |
| `[历史内容] file.py：疑似硬编码密钥` | 密钥进过历史 | 改写历史 + **吊销该密钥**（改写不等于没泄露） |
| `[历史路径] data/x.json` | 曾 `git add -f` 强推 | `git filter-branch --index-filter 'git rm --cached --ignore-unmatch data/x.json'` |
| `[体积] xxx 有 8.0MB` | 大对象进过历史 | `git filter-repo --strip-blobs-bigger-than 1M`（需先 `pip install git-filter-repo`） |
| `[离线 demo] 未通过` | 代码引入了网络/数据依赖 | 修代码，不要放宽检查 |
| 推送被 pre-push 拦下 | 目标 ref 没过闸门 | 先看输出，修完再推；`--no-verify` 仅在完全知情时使用 |

> 一条硬规矩：**任何密钥一旦进过历史，就当它已经泄露**。改写历史只是让仓库干净，
> 不等于密钥安全——必须去对应平台吊销并换新。
