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

提交身份必须用 **GitHub 专属 noreply**，形如
`12345678+用户名@users.noreply.github.com`（GitHub → Settings → Emails →
勾选 *Keep my email addresses private* 后可见）。当前全部提交已统一为
`73050370+xyd0803@users.noreply.github.com`。

为什么不用别家的 noreply（如 Gitee 的）：地址本身确实**不可反查**，不会泄露联系方式，
但 GitHub 认不出这个域，提交不会关联到账号——作者显示为纯文本、贡献图不计，
同时会把公开的 GitHub 账号与那个账号关联起来。对「攒星标 / 当简历项目」这个目标
是纯负分。

注意 `git config user.email` **只影响新提交**，已有历史要再改写一次
（`--env-filter` 同时改 author 与 committer，缺一个就会残留）：

```bash
FILTER_BRANCH_SQUELCH_WARNING=1 git filter-branch -f --env-filter '
export GIT_AUTHOR_EMAIL="你的GitHub noreply"
export GIT_COMMITTER_EMAIL="你的GitHub noreply"
' -- --branches
# refs/original 会钉住旧对象，必须清干净，否则 gc 归零不了
git for-each-ref --format='%(refname)' refs/original | xargs -r -n1 git update-ref -d
git reflog expire --expire=now --all && git gc --prune=now --quiet
git push --force-with-lease origin main local-backup   # 私有备份仓也要跟着改
```

改写后务必重跑 `check_public_ready.py`：旧邮箱会以 `refs/original` 或 reflog
的形式残留，只有 `gc` 之后才真正消失。若之前推过私有备份仓（`origin`），
它的远端分支同样钉住旧对象，需 `git fetch --prune` 后再验一次
`git log --all --format='%ae' | sort -u`。

### 3.3 推送

```bash
git remote add github https://github.com/<你的账号>/Ashare-sector-rotation.git
git push github main          # 只推 main
```

**不要**用 `git push --all` 或 `git push --mirror`：前者会把 `local-backup`
一并送出（钩子会拦，但没必要走到那一步），后者会连远端跟踪分支一起镜像。

### 3.4 仓库设置（决定别人会不会点进来）

- **Description**：一句话说清「是什么 + 给谁用」，不要写「A quant framework」。
  本项目实际使用（面向中文投资者，GitHub 搜索结果只显示前 ~150 字符，务必把
  关键词放在最前）：

  > A 股行业、板块与 ETF 轮动的「纪律层」：供需格局 + 5 年估值分位 → 可逐条质询的
  > 胜率、象限、建议仓位、三档价格，以及它拒绝更乐观的全部理由（缺了哪个数据、
  > 降级损失几个百分点、参数指纹是什么）。不做预测，只回答「凭我确实知道的东西，
  > 多少仓位站得住」。多数轮动工具停在热力图，这里继续量化「它不知道多少」。
  > 零 API key、可离线跑 demo；另附 21 条踩坑登记——哪些思路已被证伪、别重走。
  > 行业轮动·估值分位·量化回测·ETF 轮动策略
- **Topics**：`quant` `a-share` `etf` `sector-rotation` `valuation` `investment-research`
- **Social preview**：上传一张 1280×640 的图（终端 demo 输出截图即可）
- **About → Releases**：打 `v1.1.0`，附一条 CHANGELOG 摘要

## 4. 仓库名

已定为 `Ashare-sector-rotation`（原名 `xyd-supply-demand-sector` 里的 `xyd`
对陌生人没有信息量）。改名后必须同步文档，否则访客照 README 操作会 404：

```bash
grep -rn "xyd-supply-demand-sector" README.md README.zh-CN.md \
     BACKLOG.md CHANGELOG.md PITFALLS_SDS.md    # 应无输出
```

只需改两份 README 的 title / `git clone` / `cd`（当前用 `<your-account>` 占位，
改的只是仓库名部分），以及本文档 3.3 的 remote 地址。GitHub 会自动重定向旧地址，
不会产生死链；但**文档里的硬编码旧名不会自动跳转**，必须手动改。

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
