# Ashare-sector-rotation · 修补史与事故复盘（CHANGELOG）

> 2026-09-07 审议后自 SKILL.md 迁出（审议 RC2：文档与代码双真理、prose 数字漂移无机器锁）。

## 2026-10-25（晚）渲染层空值缺陷 S-21：卡片把「没算」显示成「算出来是 0」

> 触发：用户要求给 512070 / 159040 出标准终裁卡片。两张卡的 `p_star` 都是 null，
> `render_verdict_card.py` 直接把 Python `None` 拼进 HTML，卡片上出现
> 「W=None% · r=None%（σ=None）」「P* None」「安全边际 None%」。

- **隐蔽的一处（危害最大）**：`win_rate=None`（左侧布局通道·胜率待拐点）被 `int(round(None))` 兜成 0，
  EV 标题据此写 **「W=0%」**——159040 明明是 `status=OK` 的左侧等待布局，卡片却显示"胜率为零"，
  语义正好相反。这是「上游诚实标注了 None，渲染层把它抹平成 0」，与 D-8「降级必量化」同源。
- **引擎侧连带缺陷**：`discipline.py:1305` 把 `win is None` 与 `win <= 0` 合并为同一条 disclosure
  （「胜率=0（框架否决/价跌硬否决）」），导致 159040 卡片写着"框架否决"。已拆成两条独立文案，
  并**按新引擎重跑 159040 落盘**（未手改产物）。
- **修法（渲染层三态兜底）**：`p_star` 缺失时按 `status` / `left_side_channel` 分述原因——
  「框架否决（价跌硬否决·胜率=0）→ 不计算击球线」/「左侧布局通道（胜率待拐点·不入六档映射）→ P* 不适用」
  /「P* 未计算（缺胜率/波动输入）」；轴标签与安全边际一律 `—`，现价回落 `odds.now`；
  胜率环无数值显示 `—` 并带口径标签；**EV 区空档必须显示引擎 disclosure，禁静默空白**。
- **回归**：新增 `_test_card_null_20261025.py`（G1 真实否决卡 / G2 真实左侧卡 / G3 合成 null / G4 正常 P* 不回归 / G5 EV 披露不空白），
  挂入 `selfcheck_all`；`check_pitfall_closure` R1~R8 全绿（21 条坑）；run_all 六道全绿；
  回测 `NAV=1.3205 / maxDD=22.06%` **逐位未变**（引擎零漂移）。
- **通则**：**凡渲染层取值，先问"它为空时页面上显示什么"**；交付物里出现 `None` = 缺陷，不是排版细节。
> 约定：**进度叙事/事故复盘/参数版本史只进本文件**；SKILL.md 只留契约、入口与当前状态。
> 计数类数字（红队用例数/ETF 数/数据文件数）以 SKILL.md 的 AUTO:STATS 块（make_stats.py 生成）为准。

## 2026-10-25 元层治理：坑闭环从「自证」改为「他证」（S-20 / PITFALLS_REGISTRY / check_pitfall_closure）

> 触发：用户追问「为啥会新增坑，这样要不断填坑呀」。
> 逐条回代码核对后得出的结论**不是「坑太多」，而是「坑可以被假装关掉」**：
> 19 条坑的 `@preflight:` 全在 preflight 里显示 OK，但其中 6 条（S-13/14/15/17/18/19）性质完全不同、
> 却共用同一个检查项标签；S-07 的标签甚至自带「未装 pyflakes 则降级为提示」的豁免。
> 也就是说「6 条坑都闭环了」这句话**当时无人验证**。earnings 侧早在 2026-10-08 就有
> `PITFALLS_REGISTRY.json + check_pitfall_closure.py`，SDS 侧一直只有 md —— 这是本次补齐的结构缺口。

- **坑分类（把「坑数增长」拆成三种不同性质，避免一刀切）**
  - **A 施工型 8 条**（S-01/02/03/04/06/07/13/17）：新写/首跑时的一次性缺陷，有真实数据跑通即永久消失。
    数量随功能数增长，但**不会重生**。
  - **B 复发型 11 条**（S-05/08/09/10/11/12/14/15/16/18/20）：只要不机械化就无限重生
    （声明与实际不符 / 假接线 / 修入口不修存量 / 闭环靠自证）。**这 11 条已全部机械化**：
    preflight 检查项 9 条 + 注入测试 2 条，无一条只用文档。
  - **C 证明型元坑 1 条**（S-19）：不是产品缺陷，是「验证本身不充分」，靠影响面清单 + 强制全量回归治。
- **新增 `PITFALLS_REGISTRY.json`（20 条）**：结构化字段含 `class`（A/B/C）、`guard{type,check_id,proof,test,assertion}`、
  `blast_radius`（**gate**=数据写入/清洗闸门 / code / fixture / verify）、`stock_side` / `residual_ok`、`fix_location`；
  `_meta` 内含分类图例、R1~R8 规则正文、新增坑 SOP。
- **新增 `check_pitfall_closure.py`（R1~R8 双向校验）**，三条关键规则：
  - **R4 指错检查项**：preflight 守卫必须给 `proof`，且该串必须出现在**对应 check 函数源码**里。
    没有这条，「标签指向一个真实存在但根本不查这个坑的检查项」永远查不出来（S-14 的原型错误）。
  - **R5 垃圾桶闸**：单检查项承载 B 类坑 >3 → FAIL。一个 check 装 5 条坑时没人能说清它在验哪条，
    于是每条都"过了"但可能一条都没验。
  - **R6 影响面配对**：`blast_radius=gate` 必须给 `stock_side`（清存量落点）或 `residual_ok`（为何不需要），
    专治 S-18「修入口当修数据」。**第一版把 R6 套到所有 entry 类坑 → 11 条假阳性**（代码契约级坑本无存量概念），
    故按处置面重分 gate/code/fixture/verify，R6 只对 gate 生效 —— 校验器自己也不许产噪音。
- **md 侧标签诚实化**：S-13/14/16/17/19 的 `@preflight: 健康度与欠账闸`（一个并不查它们的检查项）
  改为 `@test: 文件::锚点`，指向真正覆盖它们的注入测试。`check_pitfall_coverage` 相应支持两类标签，
  且只认**行首**标签（正文里叙述性的 `@preflight:` 提及不算登记，否则 S-20 的正文会自触假阳性）。
- **补 S-16 唯一真缺口**：`_test_s16_batch_resilience_20261025.py` —— 四种连接器故障模式（抛异常 /
  返回 None / 返回非 dict / 空候选表）必须返回 `(None, 原因)` 而不抛，且混合故障批次必须跑完全部标的、
  每只都留失败记录、每只都真被查询过（T2d `BATCH_SURVIVES` 防「靠跳过假装完成」）+ main() 源码级
  `continue` 断言。此前 per-code 重试只改了代码、**没有任何测试证明它真的继续**。
- **校验器自身必须能被反驳**：`_test_closure_gate_20261025.py` 在临时 registry 副本上注入 6 类违规
  （proof 造假 / check_id 不存在 / 4 条 B 类挤一个 check / gate 撤 stock_side / 复发型守卫改 none /
  md↔registry 不一致 + 注入测试锚点造假），逐一断言规则**真会打红**。
  → 这是防「用一个永远 return 0 的检查器来证明闭环可靠」的自噬。
  实测 **8/8 通过**。
- **preflight 增两项**：`坑登记闭环`（MUST，跑 R1~R8）、`坑登记覆盖度`（支持 `@test:`；只认行首标签）。
  `REAL_CHECKS` 14 → 15 项；`selfcheck_all` 11 → 12 模块。
- **验证闭环**：`check_pitfall_closure` 全绿（20 条 / A8 B11 C1 / 守卫 preflight15 injection5）
  → 红队 8/8 → `make_stats --check` OK（红队77/ETF55/PB55/PE2）→ **`run_all` 六道全绿**
  → 回测 `signal_count=195` / NAV 1.3205 / maxDD 22.06% **逐位未变**（引擎零漂移）。

## 2026-10-25 第三批补漏：B-03 交易日机械过滤（存量清污）+ B-02 单入口 + B-10 覆盖率计量

> 触发：用户追问「剩余欠账是实战中才发现、还是之前漏掉的」。逐条回代码核实后**分类**：
> B-03 = **真漏项**（数据正确性，此前靠人工过滤 = 手抬不变量，已实际漏过一次）→ 本批补；
> B-02 = **真漏项但当前不改数值**（回测从不产 quant_raw → 模式恒 table，与 live 恰好一致，
> 但**回测闸门对量化路径完全失明**，报告里也查不到）→ 本批把结构统一 + 可见化；
> B-10 = **不是漏写的代码，是刻意的保守占位**（`map_validated=False`，校准 AUC≈0.50 无预测力，
> W_eff 向 50 收缩；此时硬抬覆盖率=把结论推向中性而无验证支撑）→ 本批只做**覆盖率计量**，
> 真实化留作有据的欠账（含解锁条件 AUC>0.55）。

- **B-03 交易日机械过滤（`trade_calendar.py` + `repair_nontrading.py` 新建）**
  - 实证污染：存量 11 个 A 股缓存含 **575 个非交易日点**；其中 6 个是**整段日历日序列**（如 930709.CSI 1301 点 vs 真实 1213 点，多 88 个节假日）。非交易日点按重复值计入 `pct_rank` → `pct_now` 偏，最大修正 **950125.SH 76.84%→79.13%**（足以改五档归属）。
  - 日历：离线从本地日频估值序列并集推导；**分市场** `cn / hk / us`（S-17：跨境三只多出的 88 点是**别的市场的交易日**，套用 A 股日历会把真实数据删掉）。判定=①非周末（确定性事实不投票）②覆盖该月序列 **≥50% 多数票**——低阈值会被污染源自投通过（2026-10-01~07 有 14/52 票，>2 票口径直接误判为交易日）。样本 <3 序列的市场 → `available=False` → 不判休市（保留 + WARN）。
  - 三处落盘口统一过滤：`fetch_pb_mxdata.parse_table`（妙想源实测部分标的返回日历日）/ `ingest_ifind_valuation`（`--trade-day-only` 默认开）/ `ingest_neodata_valuation`；记录 `calendar` / `calendar_market` / `nontrading_dropped` 便于追溯。
  - 纵深防御：`merge_series` 增周末 fail-closed（与市场无关的确定性校验，兜住「忘了过滤」）。
  - 存量清污：`repair_nontrading.py`（dry-run 默认；`--apply` 逐文件备份 `data/_backup_nontrading_20261025/`；重算走 `build_record` 单一实现 + 同步 PB bands；删除比例>20% 拒绝以防市场判错）。实际修复 12 个文件（11 PB + 1 PE），复核后存量非交易日点归 0。
  - **S-18 纪律**：闸门只防将来，**修了入口 ≠ 修了数据**，两者必须同时做。
  - **S-19 事故**：新闸门把 3 处静态夹具（手写日历日）打废 → 一律**改夹具不改闸门**（换成 2026-01-05/06/07、2099-01-05/06），并把「周末入参必须 MergeError」补成自检项，防「修夹具时把闸门修松」。
- **B-02 回测↔引擎单入口（`backtest.py`）**：`real_features` 改为调用 `quant_inputs.derive_quant_inputs`（与 live 同一函数），输入为 point-in-time 月频 close/volume（禁未来月）；报告新增 `win_rate_mode_counts` / `quant_derived_n_avg` / `quant_derived_n_max` / `quant_engine_note`，并打印一行（此前「走 table 还是 quant」完全不可见）。实测 **195/195 全 table、派生 2.53/8（max 3）**，与 live 语义一致；回测 signal_count 仍 195、NAV 1.3205、maxDD 22.06% **逐位未变**（引擎零漂移）。preflight 增硬断言：`real_features` 源码必须出现 quant_inputs 单入口，否则 FAIL。
- **B-10 覆盖率计量（`run_sector_pipeline.py`）**：草稿增 `_quant_coverage{derived_n,total,missing,win_rate_path_expected}`（**只计量、不造代理输入**）。实测 512800 = **2/8**（缺 q_vol_yoy/q_inv_yoy/q_util/q_fund_flow_pct/q_fund_turn_pct/q_earning_rev）→ 预期 table。**明确不做**：不为了让覆盖率好看而塞未经验证的代理特征（V4 校准已证 AUC≈0.50）。
- **BACKLOG 收敛（10 → 11 条）**：B-03 `已完成`；B-02 / B-10 转 `进行中`（结构/计量已落地，残差与解锁条件写清）；新增 **B-11 港股/美股日历样本不足**。
- **preflight `check_health_backlog` 扩容**：③ 交易日历可用性 + 关键判例（周末/国庆）+ **存量缓存非交易日点必须为 0**（MUST，否则跑 `repair_nontrading.py`）；④ B-02 单入口断言。
- **验证闭环**：`_test_b03_trade_day_20261025.py` **22 项全过**（日历判例/市场隔离/merge 周末拒绝/ingest 端到端清污+点数不足拒绝/parse_table 过滤/存量审计）→ `merge_valuation_gap` 自检 6/6 与 `_test_merge_regress` 7 组对账**逐位一致** → `make_stats --check` OK（红队77/ETF55/PB55/PE2）→ `preflight_etf.py` exit0（3 WARN）→ **`run_all.py` 六道闸门全绿** → 512800 端到端 draft 带 `_quant_coverage`（2/8）与 `_price_cross_check`（差 0.00%）、`_data_health=DEGRADED(3)`、退出码 0。

## 2026-10-25 第三批：L-4 欠账登记 / D-7 现价双源 / D-8 健康度四态（核心逻辑零改动）

- **背景**：三维审计末批（效率与欠账）。E-1~E-3 已在第二批落地，本批先复验仍 green（连接器矩阵/分级 lint/批量入口的实现标记与 preflight PASS 全在），再落 L-4/D-7/D-8 三项 P2。红线不变：**胜率公式/EV 公式/三道闸/否决规则一律不碰**——三项全部是「标注层 + 取证层」，引擎输入 schema 与判定逻辑逐位未动（回测 n=195 与上批基线一致）。
- **D-7 现价双源交叉（`run_sector_pipeline.py`）**：`fetch_price_now` 由单源（新浪 hq）改为**双源交叉**——原 `_fetch_price_sina` 抽出为独立探针，新增 `_fetch_price_tencent`（`qt.gtimg.cn`，免费无鉴权，字段[3]=当前价）。阈值 `PRICE_CROSS_WARN_PCT=0.5`：
  - 双源一致（差 ≤0.5%）→ note 标「双源交叉一致（差 x.xx%）」，`multi_source=True`；
  - 分歧 >0.5% → **保留主源新浪不静默改值**（S-14），落 `warn` 进 warnings，`diff_pct` 留痕；
  - 主源失败 → 腾讯**兜底**并显式标「单源」；双源皆失败 → `price_now=None`（EV 走相对=100，既有行为不变）。
  - 明细写草稿 `_price_cross_check{ok,primary,sources[],diff_pct,warn,multi_source}`（元数据，引擎不收）。
  - 选型说明：审计原文写「新浪 + westock data_quote（或 tdx）」，但 tdx/westock 属 **agent 侧 MCP，Python 子进程内不可调**；故取同为交易所快照口径的腾讯 HTTP 快照做机械交叉，tdx/westock 仍作人工兜底（preflight 连接器矩阵已提示选路）。实测 512800：新浪 0.849 / 腾讯 0.849，差 0.00%；现价段耗时 280~300ms（两次快照串行，已登记欠账 B-07）。
- **D-8 数据健康度四态（`data_health.py` 新建）**：移植盈利面技能 `_data_health` 口径，补齐本技能只有「新鲜度三档 + 零散 warnings」的缺口。
  - 四态 `OK / DEGRADED / MISSING / INVALID`，严重度取最高项（不因多数 OK 平均掉硬问题）；`--selfcheck` 11 项（四态/严重度单调/降级量化/窗口分级/过期硬限）。
  - **降级必量化**：每条 DEGRADED 必带 `impact`（单源现价→EV base 偏 0.5pp；无 quant_raw→胜率回退六档表；缺 10y→长周期盲区；行业量价未取证→量价打分无客观锚；事件腿未取证→催化剂盲区）。
  - 覆盖 `DG-PRICE-SINGLE / DG-PRICE-DIVERGE / DG-PB-SINGLE / DG-STALE-WARN / DG-QUANT-MISSING / DG-NO-10Y / DG-IM-UNRETRIEVED / DG-EVENT-UNRETRIEVED / DG-EVENT-CORRUPT / DG-BVPS-CALIBER-OLD` 与 `MS-SERIES / MS-PB-PCT / MS-PRICE / IV-STALE-HARD / IV-BVPS-CALIBER`。
  - 接线：`run_sector_pipeline._assess_data_health()`（fail-open 但有 `deferred` 标记，避免「未评估」冒充「评估通过」）→ 草稿 `_data_health`；`analyze_etf` 把水印打到 stdout 并附到 verdict 顶层 `_data_health{status,counts,items,watermark,schema:D8-four-state/1,applied_stage}`。**是标注层**：`status`/胜率/EV/仓位/闸门判定完全不受影响（实跑 512800：verdict status=OK 且 `_data_health=DEGRADED` —— 二者语义不同，不得互相覆盖）。
  - ⚠️ **口径自纠（S-13）**：初版 `bvps_caliber_mutation=True` 一律判 INVALID，实跑 512800 误杀——D-4 扫出的唯一突变是 **2014-12（142 个月前）**，主锚用的是近 5 年日频，该突变不在主锚窗口内。改为按 `caliber_events` 的 `_months_ago()` 与 **60 个月**切分：窗口内 → INVALID `IV-BVPS-CALIBER`；全在窗口外 → DEGRADED `DG-BVPS-CALIBER-OLD`（仅 10y 交叉口径打折）；只有 bool 无明细 → 维持 INVALID（fail-closed）。实跑后由 INVALID 正确降为 DEGRADED（3 项：口径突变窗口外 + 行业量价未取证 + 事件腿未取证）。
- **L-4 欠账登记（`BACKLOG.md` + `check_backlog.py` 新建）**：把散落在 CHANGELOG 叙事段、2026-08-16 评审的 V 系列、三维审计 E-4/E-5/L-5 里的未完结项收敛为**单一权威表**（7 列：ID/欠账/来源/等级/状态/定义与验收/依赖），首批登记 **10 条**：
  - **V11**（B-01，P2 待定）= 单标的 EV/仓位为毛利口径，交易成本只在 backtest 层扣，单标的输出未提示净额；**V13**（B-02，P1 待定）= 回测与引擎 quant 路径同源化——2026-08-16 已核对结论为「共同病根非逻辑漂移」（回测 `real_features` 只产占位特征、从未调 `quant_winrate.compute_quant`），真正同源化阻塞于真实 D1–D5 接入（B-10）。
  - 另含 B-03 iFinD 日历日序列机械过滤（P1 待办）、B-04 159755 月频缺口（S-12）、B-05 pyflakes benign 遗留、B-06 连接器矩阵无实战样例、B-07 E-4 现价并行、B-08 E-5 端到端口径、B-09 L-5 双价格保守侧常驻披露、B-10 O1 量化派生真实化。
  - **闸门化**：`check_backlog.py` 校验 schema（7 列固定顺序）/ID `B-NN` 唯一连续/等级状态枚举/定义≥20 字（防空壳）/依赖存在且不自依赖；挂 `selfcheck_all.py`（run_all 第 3 步）**且**新增 preflight `check_health_backlog`（入口硬闸，同时断言 D-7→D-8 接线：分歧必须产出 `DG-PRICE-DIVERGE`，否则水印丢失）。`REAL_CHECKS` 增「健康度与欠账闸」。
- **新增回归**：`_test_d7_d8_20261025.py`（15 项断言）——D-7 五场景（一致/分歧 1.00% 且主源不被覆盖/腾讯挂/新浪挂兜底/双挂）+ D-8 四态与降级量化 + **T10 真跑 pipeline** 断言草稿带 `_price_cross_check`（sina+tencent 双源）与 `_data_health`（非 deferred）。已实跑全过。
- **验证闭环**：`make_stats` 重建统计块（红队77/ETF55/PB55/PE1）→ `preflight_etf.py` exit0（3 WARN 不阻断：159755 月频缺口 / benign lint / 欠账 10 条 open）→ `run_all.py` **六道闸门全绿**（EtfPreflight·指纹锁·Selfcheck 10 模块+统计块·红队 77/77·Stress MC2000·Backtest）→ 回测 signal_count **195** 与上批基线逐位一致（引擎零漂移的结构性证明）→ 前两批注入测试（`_test_ifind_budget_gate` / `_test_merge_regress`）均 exit 0。端到端实跑 512800：`price_now=0.849`（双源差 0.00%）、`_data_health=DEGRADED(3)`、verdict `status=OK`、退出码 0；合成 smoke 产物（`.rt_tmp/d8test`）已清理。

## 2026-10-25 第二批：D-4 / D-5② / D-6 / E-1~E-3 落地 + 两个端到端断裂修复

- **背景**：第一批 P0（D-1/D-2/D-3/D-5①/L-1）后，继续落审计建议的第二批——数据层长周期与行业量价取证（D-4/D-5②）、事件腿三态卡（D-6）、效率层三项（E-1 连接器矩阵 / E-2 分级 lint / E-3 批量入口）。三处"只写字段、不改引擎逻辑"的轻量接线，外加两个本轮实测暴露并修复的端到端断裂。
- **D-4 长周期 PB 交叉校验 `pb_longterm.py`（新建）**：读 `data/index_monthly/{index}.json` 月频 PB+close，算 10y(全样本) 分位 `pb_pct_10y` + 窗口年数；BVPS 口径突变检测（同月 PB 跳 ≥15% 且价格变动 <5% → 疑似会计口径变更，分位可能失真）。接线 `run_sector_pipeline.py` S3 后：available 时写 `bands[pb_pct_10y/pb_series_years/bvps_caliber_mutation]`（引擎 O9 消费 10y 口径），口径突变作 SHOULD 告警（不阻断）。实测：399986.SZ 12.1y/145 月、pb_pct_10y=39.31%、捕获 2014-12 真实口径突变（PB 跳 30.2%/价格仅 0.0%）；980032.CNI 无月频文件 → unavailable 诚实落 WARN。
- **D-5② 行业量价取证 SOP `industry_metrics.py`（新建）**：`fetch_industry_metrics(sector, index_code, index_name)` 经妙想 NL 免费源取 4 指标（volume_yoy/inventory_yoy/utilization/earning_rev_pct，关键词解析 + 范围闸 [-100,300]），三态（filled/empty/corrupt）写 side-car `data/industry_metrics/{index}.json`（含 industry_volume_metric/industry_price_metric provenance）。接线 `_attach_industry_metrics`：side-car 存在时回填进 `quant_raw` + bands，缺则标注"LLM 须带 provenance 填或标未取到"。
- **D-6 板块事件腿三态卡 `event_leg_sector.py`（新建）**：`assess_event_leg(sector, index_code, index_name)` 经妙想 NL 取板块事件，三态诚实（retrieved 有表摘要 / empty 无表⛔红标 / corrupt 损坏⚠️灰标）写 side-car `data/event_leg/{index}.json`；沿用 XYD 全局事件腿铁律（"没去找"与"找了没有"必须可区分）。接线 `_attach_event_leg`：side-car 存在时植入 `draft["event_leg"]`（state/summary/source/retrieved_at/raw_excerpt）。
- **E-1 连接器就绪矩阵（preflight_etf.py）**：REAL_CHECKS 增「连接器就绪矩阵」，探针 `check_connector_matrix`（新浪行情/妙想直连 mkapi2/妙想·neodata MCP 宿主/ iFinD 账本 / tdx·westock agent 侧），源 down 只 WARN 不阻断，选路提示 + 审计落盘（≥3/5 即开跑）。4/4 可探面 UP 验证过。
- **E-2 分级 lint（preflight_etf.py）**：pyflakes 装 4.0.3；`check_lint_optional` 改分级——F821(未定义)/F823(未使用局部) → MUST，其余(未用导入等 benign) → WARN（全树 0 F821/F823）。
- **E-3 批量入口（run_sector_pipeline.py）**：`--etfs` 逗号分隔多只（如 512800,512880），一次进程平摊固定成本（体检/导入/连接），单只失败不杀整批；sector 缺省从 `etf_index_map` 的 index_name 推导；`try/except` 单只隔离 + 汇总表（指数/点数/as_of/新鲜度/PB分位/退出码）。
- **🔴 修复1（本批实测暴露）：条件锁定——仅实际填充才锁，消除 D-5②/D-6 字段误伤 LLM 合法手填**。初版把 `industry_volume_metric/industry_price_metric/volume_yoy/inventory_yoy/utilization/earning_rev_pct/event_leg` 无差别硬编码进 `_locked_mechanical`。但 `analyze_etf.py` 的合并 override 检测（`k in locked and k in fills and fills[k] != draft.get(k)`，第 218 行）对"锁了但草稿里没有（无 side-car，draft.get(k)=None）"的字段，会把 LLM 带 provenance 的合法手填判为 override → **exit 4 误杀**。修正：`_attach_industry_metrics` 返回实际填到的字段名 list、`_attach_event_leg` 的 rec 非 None 才锁、D-4 仅 `lt["available"]` 才锁、D-5② 仅实际填到值才锁；`locked_mechanical` 改为 `BASE_LOCKED + d4_lock + im_lock + el_lock + extra_locked` 在 attach 之后装配。实测：无 side-car 的 515220 → 这些字段不在锁表（LLM 可填）；有 side-car 的 512800 → 实际填到的字段入锁（inventory_yoy 因 side-car 为空正确不入锁）；篡改 `pb_pct` 等机器字段仍被入口早期拦截（exit 3）。
- **🔴 修复2（本批实测暴露）：analyze_etf 引擎输入过滤——草稿超集收敛为引擎严格子集**。D-4 写进 `bands` 的 `bvps_caliber_mutation`（bool 告警旗）与 D-6 的 `event_leg`（卡）是 LLM/人工研判用的客观元数据，但 `SectorInput` schema 不含二者，`discipline.evaluate → SectorInput.from_dict` 严格拒绝未知字段 → end-to-end 跑 515220 直接 `ValueError: 未知输入字段` 崩溃。该断裂此前从未跑过 end-to-end 故未暴露。修正：`analyze_etf.py` 调引擎前用 `discipline.SectorInput.__dataclass_fields__` 把 `cleaned` 过滤为引擎认得的字段集合（编排层负责草稿超集↔引擎子集的对账，不削弱引擎"禁止自创字段夹带主观修正"的严格契约）。修复后 515220/512800 均端到端出 verdict（status=OK，exit 0）。
- **验证闭环**：`make_stats --check` OK（红队77/ETF55/PB55/PE1）→ `preflight_etf.py` exit0（3 WARN 不阻断：159755 无月频缓存 / benign lint / 坑覆盖 info）→ `run_all.py` **六道闸门全绿**（EtfPreflight / Preflight 指纹 / Selfcheck 8 模块 / 红队 **77/77** / Stress MC2000 / Backtest）。端到端实测：512800（有 side-car）与 515220（无 side-car）各跑 analyze_etf 均 exit 0 出裁决；篡改机器字段被拦（exit 3）。
- **遗留 WARN（不阻断）**：159755 无月频缓存（S-12 覆盖缺口）；pyflakes benign 风格 86 条（未用导入/变量）；坑登记覆盖度 info（"连接器就绪矩阵"尚无实战样例映射，仅提示）。

## 2026-10-25 续 · stale 缓存全面刷新（财报周期闸 MUST 消除·全闸门全绿）

- **背景**：第一批收尾时 preflight 仅剩 1 项 MUST——财报周期闸 34/54 只 PB 缓存跨中报披露截止 2026-08-31 仍 stale（BVPS/EPS 锚点不含中报 → 分位系统性失真）。本批彻底清除。
- **源路由实测（三源能力边界，供后续复用）**：
  - **neodata（免费）**：覆盖 .SH/.SZ 主体指数；**CSI 系列（930xxx/931xxx/H30xxx/000xxx.CSI）与 950125.SH 无返回**；SPX.US 仅 PE 无 PB；不支持 .CNI（须 .SZ）。已存原始响应 `data/raw/neodata/nd_20260801_20261024_b1/b2.json`。
  - **妙想（免费·fetch_pb_mxdata）**：33/34 全覆盖（含全部 CSI 系列、SPX.GI PB、港股），唯 950125.SH（科创半导体材料设备）无数据。
  - **iFinD（配额）**：兜底 950125.SH，1 次调用（--tag sds_stale_refresh --charge 1，账本记 1 次）。
- **执行**：① 妙想整序列重建 33 只（22 只无覆盖 CSI 系列 + 11 只先经 neodata 合并验证后又以妙想同源自全量 supersede——neodata 合并暴露同日口径差 7~12% 的跨源标定证据，但终态取同源单口径序列，避免混合标定噪声）；② 950125.SH 经 iFinD 增量合并（as_of 2026-08-13→10-23，重叠期中位比值 1.0314 归一）；③ 顺带清掉 8 只 15 天新鲜度 stale（妙想）；④ 归档 2 只无后缀孤儿缓存（930709、HSTECH——活框架只认 .CSI/.HI 后缀键，仅 archive 旧稿引用；备份 `.rt_tmp/sds_bak/` + `archive/2026-10-25/_why_*.json` 留痕）。
- **fetch_pb_mxdata.py 健壮性修复（E-1 前瞻）**：`fetch_one` 遇妙想直连返回 None/非 dict 时原代码 `r.get` 直接 AttributeError **崩掉整个刷新循环**（一次瞬时故障=整批 codes 不处理）；改为 per-code 失败返回 + 一次 2s 重试（对齐文件头既有约定「偶发 0 返回=通道瞬时故障，稍候重试」）。实测：本批 12 只一批跑到第 11 只触发该崩溃，修复后同批 12/13 完成（仅 950125 无覆盖）。
- **iFinD 日历日口径防御（本批新发现·待登记）**：iFinD 指数估值返回**日历日序列**（周末/节假日顺延值，本次窗口 90 行中 20 行非交易日=22%）；直接合并会往交易日序列注入重复值污染分位。本次处置：按「新鲜缓存的交易日日期并集」机械过滤后再 ingest（不碰共享 ingest 语义）。既有 5 只 ifind 源缓存已含 6/1224 周末点（历史遗留，非本批引入）。
- **验证**：`run_all.py` **六道闸门全绿**（EtfPreflight 首次 exit=0 · 指纹锁 · Selfcheck · 红队 77/77 · Stress MC2000 · Backtest）。回测位移（数据驱动非逻辑回归）：signal_count 197→195、win_rate 0.8071→0.8154、final_nav 1.2651→1.3205（后期信号用上 10-23 新锚）。make_stats：红队77/ETF55/PB55/PE1。
- **遗留 WARN（3 项·不阻断）**：quant_raw 159755 无月频缓存（S-12 覆盖缺口，待补月频）；静态 lint（pyflakes 未装，既有）；预算闸今日已记 1 次。

## 2026-10-25 第一批 P0 落地（D-1/D-2/D-3/D-5①/L-1 · 核心逻辑零改动）

- **背景**：三维审计（数据层 55% / 逻辑层 90% / 效率层 85%）后执行第一批 P0 五项。红线：discipline.py 胜率公式/EV 公式/三道闸/否决规则一律不碰。
- **D-2 泛化非破坏性合并器 `merge_valuation_gap.py`（新建）**：`ingest_ifind_valuation._rebuild` 内嵌算法收敛为唯一实现，所有 ingest_*_valuation 共用。语义逐位保留：同日冲突保留原源值、缺口按重叠期中位比值归一到原源口径、重叠 <3 fail-closed 拒拼、幂等 source 标注（`+<provider>_incr(_calib)` 不累加）、5y 窗口裁剪、<100 点拒绝覆盖、as_of 恒取真实序列末值。`has_old` 参数区分「缓存存在但序列空」（走校准分支拒拼）vs「新缓存」（直落），匹配原语义。回归 `_test_merge_regress_20261025.py` 嵌入重构前 verbatim 参照，**7/7 逐位对账一致**。
- **D-3 iFinD 预算闸（三路径补齐）**：① `sds_ifind_budget.py`（新建·适配器）委托共享的 `ifind_budget` 模块（**单一账本**：配额按账号计不按项目计，账本路径由使用者的运行环境决定，cap 由本地配置给出），共享模块不可用时启内嵌等价闸（同账本同 cap）；② `ifind_fetch.py`/`ifind_fix_pb.py` 增 `--tag` + `set_attribution`（call.py 内置 require_tag，无归因一跑即拒）；③ `ingest_ifind_valuation.py` 经 `--charge/--no-charge` 给 MCP 直调路径记账；④ `preflight_etf.py` 新增 REAL_CHECKS「iFinD 预算闸」（归因接线核验 + 耗尽且无 --no-refresh 且无逃生门 → MUST exit3 + ≥80% cap → WARN；检查自身异常 fail-open）。注入测试 `_test_ifind_budget_gate_20261025.py` 6 组 10 断言全过（耗尽 MUST/本地优先放行/逃生门放行/归因接线/记账闭环/防递归）。
  - ⚠️ **事故教训（已登记 PITFALLS S-11）**：适配器初版命名 `ifind_budget.py` 与共享模块同名 → `import ifind_budget as _ib` 绑定半初始化自身 → `today_count()` 无限递归 RecursionError。改名 `sds_ifind_budget.py` 并立命名纪律。教训：跨技能委托共享模块时，本地包装器**不得与共享模块同名**。
- **D-1 neodata 估值合并 `ingest_neodata_valuation.py`（新建）**：免费源 neodata index_valuation（PB(LF)/PE(TTM) 日频 T+1）接入。JSON/markdown 双解析（「--」容错）；`_base_code()` 主体代码匹配（980032.CNI↔980032.SZ 同指数，neodata 不支持 .CNI 后缀）；经 merge_valuation_gap 合并（provider=neodata，幂等后缀）；无覆盖显式 SKIP exit0（覆盖缺口≠数据错误）；免费源不记账。端到端 6 场景验证：fail-closed exit2 / 无覆盖 exit0 / 缺文件 exit4 / 000015 真实合并 45 点（同日 0 改动、新增点=neodata/1.0172、now 诚实、bands 同步、source 正确带 _calib）/ 新缓存 55<100 正确拒绝 / markdown 兜底。**000015 PB 缓存 stale→fresh（as_of 2026-08-14→2026-10-23，pct 93.0→99.26），全局 stale 48→47**。
- **D-5① quant_raw 装配修复（run_sector_pipeline.py）**：`_find_etf_cache` 优先精确名再 glob `*{etf}*.json`（多候选取 mtime 最新）；`_assemble_quant_raw` 改返回 `(quant_raw|None, reason)`，reason 区分「本地无匹配缓存文件 / 月频仅 N 点 (<13) / 解析失败」三类成因，调用处按真实成因生成 `_llm_required` 文案；preflight 新增「quant_raw 装配匹配」检查（缓存文件在但装配找不到 → MUST；map 内 ETF 无缓存 → WARN 覆盖缺口显式化）。54/54 可装配 + 159755 缺口正确 WARN。
  - ⚠️ **自纠教训（已登记 PITFALLS S-12）**：初版根因误判「文件名不匹配」；目录复核发现 `etf_159755_daily.json` 在 data/raw/ 而非 data/etf/，data/etf 本就无 159755 缓存（54/55 覆盖缺口），提示文案其实准确。教训：**叙事性归因比没有归因更危险**——写 PITFALLS 前必须复核目录事实。
- **L-1 胜率模式水印（discipline.py）**：`Verdict` 增 `win_rate_mode` 纯附加字段（quant/table/downgrade_quant/downgrade_table/channel/rejected/na），`_win_rate` 每个返回点设模式；文本报告增「胜率模式」行，table 回退带「⚠️ 非量化路径」水印。四路径验证：quant=73 / table=85 / rejected / na 全对。ENGINE_VERSION 保持 1.8.0（纯附加无行为变更）。
- **验证闭环**：make_stats 重建 SKILL 统计块（红队77/ETF55/PB57/PE1）→ run_all --quick：Preflight 指纹锁 PASS、Selfcheck 全绿 → 红队 **77/77 PASS** → Stress(MC 2000) 硬不变量全过（ADVISORY: S2）→ Backtest exit0（基线 n_periods=89 / signal_count=197 / win_rate_of_signals=0.8071 / final_nav=1.2651，L-1 纯附加 + D-2 逐位对账 → 零漂移结构性保证）。
- **遗留 MUST（既有数据状况，非本批引入）**：财报周期闸 35/54 只 PB 缓存跨中报披露截止 2026-08-31 仍 stale。根治路径 = D-1 的持续使用（neodata 批量刷新，`ingest_neodata_valuation.py --json <拉取文件> --code <指数>`，codes≤10/次）。

## 2026-09-08 终裁可视化卡片模板固化（render_verdict_card.py）

- **需求**：用户指定将 159755 电池ETF 终裁卡片（黄金买入区风格、浅色主题、A股涨红跌绿）固化为**机械化输出模板**，供后续任意 `verdict_*.json` 复用。
- **落地**：新增 `render_verdict_card.py`——读 verdict JSON → 渲染自包含 HTML 卡片（纯内联 CSS、无外部依赖、缺字段 `.get` 兜底不崩溃）。CLI：`python render_verdict_card.py -v verdict.json [-o out.html]`（支持批量多份）。卡片结构 = 头部状态徽章 + 估值主锚(双源校验/eff分位/赔率条) + 胜率环×象限 + EV三档(中乐尾/年化) + P*击球线(真实相对位置/安全边际) + 底部KPI(仓位/置信/侧别) + 元信息(双源/final闸/硬信号/前瞻/告警)。
- **验证**：159755 重渲染关键数值与终裁逐位对齐（PB19.22%/PE13.77%/eff17.04%/赔率82.96/胜率58%/仓位20.2%/EV中19.62%乐69.0%尾14.02%/P*0.932可击球）；极简 verdict 缺字段不崩溃。
- **登记**：SKILL.md 脚本表 + 输出节「可视化卡片输出模板」段已补；旧 `gen_etf_card.py` 保留作对照（带 PENDING 脚注）。

## 2026-09-08 凸映射 g(S) + 风险调整买点 P\*（engine 1.8.0 · params 1.10.0→**1.11.0**）

- **需求**：基于盈利面框架扩展两项——①分数→胜率**凸映射**；②**风险调整买点 P\***（纳入波动/止损/回撤，ETF 波动小则 r 默认更小）。先出方案（6 决策点）→ 用户「均用推荐」后落地。
- **决策点取值（用户采纳推荐）**：A 默认 `convex`（linear 通道保留、逐位等于旧值）· B 饱和 `soft`（softplus k=0.02，hard 通道保留）· C 锚点**沿用供需** `win_map`（不换盈利面锚点，避免跨框架口径混用）· D `σ_ref=30% / s_vol∈[0.25,2.0] / r∈[3%,60%]` · E r 为**持有期总回报**（σ 同步按 √(H/12) 换算到持有期尺度，非年化）· F P\* **不参与仓位/回测**（只读披露）。
- **① g(S) 段内凸（`quant_winrate.g_score`）**：`W=y0+(y1−y0)·t^γ`，γ=1.5；曲线位于弦下方 → 除锚点外胜率一律 ≤ 线性（保守不虚高），锚点 Δ=0、单调不减。⚠️ **数学硬约束**：锚点集各段弦斜率 0.60→0.867→0.80→0.667→0.667 非单调（S 型），「过锚点」与「全局凸」不可兼得 → 框架的"凸"= **段内凸**，节点是凹折点（斜率由 γΔ/Δx 突降到 0）。实测 convex vs linear 最大偏差 **−2.22pp**（shrink×0.5 后约 −1.1pp）；`win_map_mode=linear` 与旧 `_lerp` 逐位一致（零回归通道）。
- **② 新模块 `sector_pstar.py`**：`P*=[W·mid+(1−W)·bear_eff]/(1+r)`；`r=clamp(r_base(W)×s_vol,3%,60%)`；`s_vol=clamp(σ_eff/σ_ref,0.25,2.0)`；`σ_eff=σ_ann·√(H/12)`（H 缺省=EV 持有期 O8=24）。**止损+摩擦走 `bear_eff=max(bear,P(1−stop−fric))`，不进 r**（r 已由 σ 表达风险，再计一次=重复计入）。σ 解析链：显式 → 序列派生(√252) → 资产类别默认表 → 缺失 `s_vol=1.0`（零回归）。
- **集成**：`SectorInput` 增 `p_star_sigma_ann/p_star_horizon_months/p_star_stop_pct/p_star_friction_pct/win_map_mode`；`Verdict.p_star`（只读，None=未产出）；`_p_star()` 全程 try/except，模块缺失/异常只留 trace 不阻断；硬否决(`win=0`，EV 仅 disclosure)与左侧通道(`win=None`) → `p_star=None`。`_win_rate` 的 `compute_quant` 改传 `self._quant_params(inp)`（支持 mode 覆盖）。
- **验证**：`sector_pstar --selfcheck` 20 项（等价性 216 组 `P*>P ⟺ EV_eff>r`、σ=0/极端/缺失、W=0/100/越界、现价=0、mid 缺失、单调性×3、软饱和、幂等、序列派生）；`quant_winrate --selfcheck` 增 7 项凸映射用例；新增 `_check_pstar_integration.py`（引擎级 15 项：σ 解析链/ETF vs sector/绝对价/止损不改 r/硬否决与左侧通道/convex-linear 差异/锚点与单调）。**`run_all.py` 五步全绿**（Preflight 指纹 d390ff51 / Selfcheck 8 模块 / 红队 / 压力 / 回测）。
- **盈利面同步**：另一独立框架的同名 `p_star.py` 补**同构但参数独立**的 σ 分支（`SIGMA_CFG`，σ_ref=30%/H_ref=12）；σ 缺失 → `s_vol=1.0` → r 与首版逐位一致（`test_p_star_regression.py` 5/5 零回归 + 终检 exit0）。两框架**公式同源、参数独立**，改一处不影响另一处。
- **文档**：SKILL.md 新增 §P\* 全章 + §12.1 凸映射段 + 脚本清单 `sector_pstar.py` + 输出契约示例；params.json 增 `p_star` 段与 `win_quant.win_map_mode/gamma`，version→1.11.0，params.lock 重生成。

## 2026-09-07 端到端审议 + P0~P2 治理落地（engine 1.9.0 · params 1.10.0 未动 · 指纹不变）

- **内部评审**：五维度评估 + 低级错误五根因 RC1~RC5。
- **P0-① 未填即错（根治 RC1）**：`SectorInput` 判断字段默认值 demand(growth)/volume/price/forward(flat)/imagination(B) 全部改 `""`；`run()` 顶部新增校验——缺失 → `[MUST] 判断字段缺失` blocker（PENDING_VERIFICATION、仓位归零），内部仅映射保守值（cyclical/flat/B）防 crash。红队新增 M 组（M1 全缺 / M2 单缺 demand 对照）。
- **P0-② 固定编排入口 `analyze_etf.py`（根治 RC3）**：pipeline 草稿→FILLS 完整性预检（缺项 exit4 逐项点名）→合并（`_`键剥离+锁定字段保护+`_fill_meta` provenance）→llm_fill_guard（MUST→exit6）→引擎→`runs/<date>/verdict_<tag>.json`。FILLS 缺 forward_evidence/vp_basis 曾实测拦截两例，均为预期行为。
- **P0-③ SKILL.md 瘦身 + `make_stats.py`（根治 RC2）**：本文件迁出叙事；SKILL.md 嵌 AUTO:STATS 块，`make_stats.py --check` 断言文档数字与实际一致。
- **P1-④ 产物归档**（根治 RC4）：会话产物只写 `runs/<tag>/`；错锚 verdict_summary 等旧产物移 `archive/2026-09-07/`。
- **P1-⑤ run_all 增挂七模块 selfcheck**；**P1-⑥ `fetch_pb_mxdata.py --metric pe` 双序列刷新器** + PE 基建/卡片层文档补齐。
- **P2-⑦ demo 附 forward_evidence 展示完整 OK 路径**；**⑧ 锚复核降级为可选 + quant_raw price_series 本地缓存机械装配**；**⑨ 判断字段 provenance**：analyze_etf 自动落 `_fill_meta`，guard 缺 provenance → SHOULD。

## 以下为迁出的历史叙事（原文保留 · 2026-08-16 ~ 2026-09-04）

## 已知漏洞与待办（2026-08-16 评审）

> 2026-08-16 对技能做程序实现/分析流程/逻辑设计三维度审议，结论：**工程完成度最高、纪律闭环最硬，但本质是"统计分位+规则映射"引擎**，核心薄弱点在三处，已排优先级。

### 高严重（治本项）
- **V1 胜率量化仅保证"聚合客观"**：五维量化要求 D1–D5 八字段齐全才走五维，缺一即回退旧六档定性表。✅ **2026-08-16 已落地 O1 量化输入契约**（`quant_inputs.py` + `discipline.run()` 钩子 `enforce_quant_contract`）：八字段改由 `quant_raw` 原始序列确定性派生，手填 q_xxx 无 quant_raw → 打 `[V1]` 告警。漏洞关闭。
- **V2 EV 下行被分位带结构性低估**：`d_bear = -p20 if p20<0 else 0.0`（`discipline.py:819`），深度低估时 d_bear=0，价值陷阱盲区 → ✅ **2026-08-16 已落地 O3 基本面下行地板**（`ev_fundamental_overlay.downside_floor_*` + `_ev` 叠加），基本面恶化时 d_bear 取统计下行与地板之大者，价值陷阱盲区关闭。
- **V3 EV 假设"基本面不变"与周期框架矛盾**：锂/煤/EPS 可腰斩板块 p50 绝对价失真，无盈利相位叠加 → ✅ **2026-08-16 已落地 O2 EV 盈利相位叠加**（`ev_fundamental_overlay.earnings_decline_haircut_beta` + `_ev` 上行折价），盈利下行相位下 u_mid/u_opt 按盈利下修幅度折价，disclosure 显式标注「基本面不变假设失效」。
- **V4 五维权重 42/27/13/10/8 与 g(S) 均为占位值**（注释自陈"待 54 ETF 回测校准"）→ **O5 校准骨架已搭（2026-08-16 `calibrate_winrate.py`）**，54 ETF 回测跑通后确认 proxy 特征 AUC≈0.50 无信号→保留保守占位 g(S)（floor20/cap80）。**✅ 2026-09-01 已落地 V4 置信治理**：`win_quant.shrink=0.5` + `validated=false` → 未经验证时 `W_eff=50+(W-50)*shrink`（向基准50%收缩）并标 `confidence=LOW`，纪律重心转 EV三档+硬否决+分散（落实2026-08-16校准报告§五.3）；真实 D1-D5 接入且 `calibrate_winrate` 验证 AUC>0.55 后，置 `validated=true`+`shrink=1.0` 解除收缩。`quant_winrate.compute_quant` 输出新增 `win_rate_eff/confidence/map_validated`，`discipline._win_rate` 基胜率改用 `win_rate_eff` 驱动 EV/仓位。

### 中严重
- **V5 无对称泡沫硬否决**：买点有价跌/塌方否决，过热(pb>95%)仅 SHOULD 鱼尾 → ✅ **2026-08-16 已落地 O4 对称泡沫硬否决**：`pb_pct_bands.bubble_pct_max=95` + `_hard_signals` 新增信号#7「泡沫硬否决」MUST——有效分位>95% 且前瞻未↑ → 强制减仓/清仓（对称于价跌硬否决，`forced_reduce`→仓位归零）。前瞻↑的过热板块仍走鱼尾 SHOULD（不硬否决），与框架原意一致。
- **V6 BLEND 全局 0.6/0.4 非行业自适应** → ✅ **2026-08-16 已落地 O6 BLEND 自适应权重**（`valuation_anchor.BLEND_WEIGHTS` 按行业细分 + `discipline._blend_weights` 解析替代全局 0.6/0.4）：重资产成长(电池/半导体制造/封测/军工…)PB 权重 0.55~0.65、轻资产消费(白酒/家电/CXO/游戏…)PE 权重 0.55~0.65、制造中性(电子/通信/医药/医疗器械…)0.50/0.50；未命中回退默认。直接关掉「PB 极廉但 PE 极贵被平均掩盖」的分裂盲区。
- **V7 EV 无时间维度** → ✅ **2026-08-16 已落地 O8 持有期/年化**：`params.json.ev_horizon`（默认 24 月，输入可覆盖）+ `_ev` 新增 `horizon_months` 与 `ev_*_annualized_pct`；年化 `(1+ev)^(12/H)−1` 仅作跨板块可比口径、非收益保证，disclosure 显式标注。EV horizon-agnostic 漏洞关闭。
- **V8 freshness_max_days=60 偏松** → ✅ **2026-08-16 已落地 O7 新鲜度收紧**：`freshness_max_days` 60→**15**（约 10 交易日），新增 `freshness_warn_days=7` 偏旧预警档（warning 非 blocker，提示分位带可能漂移）；过期仍 MUST blocker 强制重取。PB 分位/动量为快变量，60 天足以错过整段行情的漏洞关闭。

### 低严重/设计性
- ✅ **V9 W 与仓位为独立杠杆（原无文档）** → **2026-08-16 已落地 O10 组合层 conviction cap**：`Verdict.conviction_score=pos×W/100` 作跨板块可比风险预算；`position.conviction_cap=0.60` / `single_high_conviction_threshold=0.30`，单板块 pos≥30% 发 `[O10]` SHOULD 提醒聚合约束；SKILL.md §十/§O10 文档化 W 与 pos 独立性。文档缺口关闭（V10 见下，已解决）。
- ✅ **V10 p95 泡沫上沿偏低（泡沫涨过头触发偏晚，如 2021 新能车）** → **2026-09-01 已落地 V10 泡沫延伸检测**：`odds.anchor_exclusion_months=18` + `bubble_warn_ratio=1.3` + `bubble_hard_ratio=1.6`；`_build_bands` 计算「锚定 p95」= 剔除近 18 个月后的序列 p95（不随两年慢牛滚动滞后），`now/锚定p95≥1.3`→泡沫延伸警示(SHOULD，收紧O4前瞻↑豁免、显著降仓)、`≥1.6`→泡沫延伸硬否决(MUST，强制减仓/清仓、关闭O4前瞻↑豁免)。实测：通信设备式慢牛（前瞻↑）原本靠 O4 豁免不下车，现锚定 p95 比>1.6 即强制减仓/清仓。`_core_odds`/`_odds(BLEND)`/`_hard_signals`/`_win_rate` 全链路接入；需 `pb_series`/`pe_series` 日频真实序列方生效，否则回退原有 `p95×0.9` 触发。V11/V13 仍待定（非本轮）。

### 落地顺序（截至 2026-08-16）
1. ✅ **O1 量化输入契约（已落地）**；⏳ **O5 校准骨架（已搭，待回测数据）**；✅ **O2/O3 EV 基本面叠加+下行地板（已落地 2026-08-16）**。
2. ✅ **O4 对称泡沫硬否决（已落地）**；✅ **O6 BLEND 自适应权重（已落地）**；✅ **O7 新鲜度收紧 60→15 + 预警档（已落地）**。
3. ✅ **O8 EV 持有期/年化（已落地）**；✅ **O9 制度周期断裂检测（已落地）**；✅ **O10 组合层 conviction cap（已落地）**。

### 2026-08-31 补丁与数据源经验（电池ETF 159755 实战）
- ✅ **双价格窗口自适应（漏洞修复）**：`dual_price_analyzer` 的 `DEFAULT_RECENT=20/BASE=60` 按日频设计，**月频/稀疏序列(n<20)恒判 flat 且 chg=null**，分层判读整体失效（159755 实测 6 个月频点全 flat）。已修复：`n<recent` 时自动缩窗 `recent=n//2(≥2)、base=n−recent`，`cross.window_mode` 记录 `auto_sparse/insufficient/default`；回归=内置茅台 demo 结论不变 + 基线 case 零漂移（W=77/仓位15.8% 不变）。
- ✅ **双价格方向闸实战验证（保守行为正确）**：压力测试（factory=挂牌 0.30→0.423 up，market=假设大宗持平 0.36 flat）→ 引擎判「市场平稳·官方单动」divergence=True，**量价以市场侧为准**；若只看挂牌价声明 price=up 而 `industry_price_trend` 口径与大宗价矛盾 → MUST blocker → `status=PENDING_VERIFICATION`、仓位归零。**教训：分歧行业的价格方向必须以市场成交价侧填写，挂牌价单边看多会被闸门拦死。**
- **980032.CNI（国证新能电池）数据配方**：① MX 妙想 API 返回结构为 `table.<指标ID>`（如 328664=PB、328773=PE）+ `headName` 存日期，`table.items` 恒空——解析脚本 `_fetch_980032_series.py`；② 双源/手算兜底=国证官网 `cnindex.com.cn/sample-detail/detail?indexcode=980032` 拉成分股权重 + 东财 push2 ulist 批量取成分股 PB/PE 加权调和（指数定义式，与 MX 差<2%）；③ tdx 不覆盖国证指数，指数点位/ETF 行情走 westock `data_quote`/东财 push2；④ 159755→980032.CNI（**不是** 931719.CSI，那是 159796），勿混。

### 2026-09-04 前瞻修复批次一 + V10 实现修复（engine 1.8.0 · params 未动 · 指纹不变）
- **F1/F3/F4 落地**（见输入表三行新增）；F1 缺省零回归已证（case_battery/3etf 与改前基线逐键一致，除 engine_version）。
- **V10 三处实现 bug 修复（红队 46→65/65 复活的关键）**：V10（2026-09-01）把辅助锚 `anchored_p95` 塞进 anchors 字典后未同步三处消费端——① `_missing` 五档完整性校验把 None 的 anchored_p95 当缺失 MUST 拦截（违背 SKILL.md「无序列则回退」设计，所有无序列/短历史输入一律被错误打成待核验）；② checklist ⑤ `all(anchors非None)` 被同一 None 永久打假、⑥ `len(hard)==8` 未随 V10h/V10w 增补到 10 条；③ BLEND 锚混合对 None 键直接 `wp*None` TypeError（J1 crash）。修复口径：anchored_p95 从 §12.5 必填与 checklist ⑤ 剔除（消费端本就判 None 回退）、⑥ 改清单子集校验、BLEND 混合跳过 None 键。**副作用披露**：此前被错误拦截的用例（短历史指数如动漫游戏 930901、沪深300非银 H30035）解锁后按框架自身公式给出仓位（7.6%/39.6%），属恢复设计本意而非放水——红队 65 用例含 I8 三真实案例全数背书。
- 验证留档：`_test_batch1_2026-09-04.py`（32/32）+ `_baseline_*_2026-09-04.json`（改前快照）+ `run_all.py` 四步全 PASS。

### 2026-09-04 前瞻修复批次二/三 + 数据入库（engine 1.8.0 · params 1.9.0→1.10.0 · 指纹重锁 c992458d…）
- **F2 前瞻方向证据链（双档）**：批次二先落 SHOULD（`forward_evidence_required=false`，缺证据仅 [SHOULD] 告警不阻断）；批次三翻 MUST（params 1.10.0 置 true + params.lock SHA256 重锁）——`forward=up/down` 无 ≥1 条 desc+source 齐备证据 → [MUST] blocker「前瞻方向缺独立证据（F2）·禁止以当前量价惯性外推」→ `status=PENDING_VERIFICATION`、仓位归零；`forward=flat` 两档均豁免（老用例零回归）。双向约束：forward=down 同样要求证据（红队 K 组锁死 K1 缺失拦截 / K2 附证据放行 / K3 flat 豁免）。红队 65→68 用例。
- **数据入库三件**：① `data/index_daily_pb/980032.CNI.json`（159755 日频 PB 从临时脚本口径转正，n=1232，2021-08-02~2026-08-31，五档/now/pct 与 case_battery 声明逐位对账一致）；② `pb_daily_bands.json` 登记（51 只）；③ `etf_index_map` 补 159755→980032.CNI（55 只，上市日期深交所官方口径）。
- **真实用例补证据链**：case_battery_159755（电池产业创新联盟订单 + 一致预期上修）、3etf 三案例（中证酒/沪深300非银/动漫游戏）各附带来源 forward_evidence——MUST 档下四只真实用例无一误伤（无 F2 blocker）。
- 验证留档：`_test_batch23_2026-09-04.py`（17/17：SHOULD 档 4 + MUST 档 5 + 数据一致性 4 + 真实用例健康度 4）+ 红队 68/68 + `run_all.py` 四步全 PASS（Preflight 指纹锁 / RedTeam / Stress / Backtest）。零新问题确认。

### 2026-09-04 harness 三缺口补全（G3→G2→G1 · 红队 68→74 · params/engine 未动 · 指纹不变）
- **G3 LLM轨机械复核层（`llm_fill_guard.py` · 新增）**：LLM 补齐 `_llm_required` 后、喂引擎前过四道确定性检查——①枚举/范围（volume/price/forward/demand/imagination/prosperity_trend；crowding 兼容引擎契约 bool）②**bands 反算对账（最强）**：pb_pct/pe_pct 声明值 vs pb_series+now 反算（rank_pct 同口径），偏差>3pp→MUST 拦截（抄错/编数必现形，159755 实测差 0.0pp）③语义矛盾（imagination≥A 但 demand∈erosion/collapse、demand=growth 但侵蚀≥2、景气 mom/trend 不成对、F4 计数矛盾、F2 证据缺 desc/source→MUST；terminal_price_down vs demand、F3 缺字段→SHOULD）④F2 方向一致性（词表+`±N%` 正则，forward=up 但证据全下行语义→SHOULD）。入口：`llm_fill_guard.py --input/--selfcheck` 或 `run_sector_pipeline.py --guard case.json`（exit 6=拦截）。**过程教训**：自查抓到 crowding 契约误判（引擎实为 bool）与词表漏词（"+8%/预增58%"未命中），均已修复——真实 5 用例零 MUST 零 warning。红队 L1–L6。
- **G2 数据统一刷新器（`data_refresh.py` · 新增）**：`--status`（fresh≤7/warn≤15/stale>15 三档新鲜度）｜`--rebuild`（离线一致性复算：与 parse_mx_pb_daily 逐字同口径重算五档+pct_now，守卫=PB∈[0.05,40]/五档单调/now=末点/五档自洽；`--fix-bands` 用**文件自身序列复算**修复污染——非插值；`--write` 同步 registry 并回填 as_of schema）｜`--refresh`（三级源链：①mx-ds-mcp 会话内补拉②ifind_fix_pb 子进程③点位重建提示，--dry-run 默认）。**自查战果**：抓到 3 只存量五档污染文件（000688.SH 五档递减又升/000813/950125，与自身序列矛盾，历史脚本事故），已同口径复算修复+registry 回写，复核 0 硬失败；n<100 短窗（4 只历史兜底）降为信息性；中间产物 `*_pb/_pe` 文件过滤出扫描（MKT 后缀 regex {2,4}）。当前 50/51 stale（数据止 2026-08-14/15）——刷新须 API 配额，按"按需"原则未自动全量，走 `--refresh` 或会话内妙想补拉。
- **G1 真实回测闭环（诚实结论：proxy 特征无样本外信号 → 保留保守 g(S)）**：①特征重建 `calib_features.py --end 2026-08-31` → 2547 条（2019-01~2026-04，2026-04 为 3m win 标签自然边界=收益数据前沿 2026-07）；②三档全样本校准（calibrate_winrate 新增 `--horizon`）：AUC 3m=0.529/6m=0.534/12m=0.496，均<0.55；③**walk-forward OOS（新增 `--walkforward`，expanding-train 48 月→test 12 月×4 折）：OOS AUC 3m=0.5008/6m=0.4982 ≈ 纯随机** → **硬闸生效：validated=false、shrink=0.5 不解除**（不放水）；④backtest.py 新增**满仓等权基准对照**：策略 NAV 1.314 vs 基准 3.241（超额−59%）——框架是"买点择时+仓位纪律"引擎，现金拖累使其在单边牛市必然跑输满仓基准，对照意义=检验选择 alpha（与 O5 校准 AUC≈0.5 互证），报告已附口径 note 防误读。breaches=0。
- **端到端验证（一次性全链路）**：`--etf 159755` 202ms 全链路（S1映射→S2新鲜度→S3五档逐位→现价→S4草稿）→ `--guard` PASS → `discipline.py` 最终裁决完整（胜率74%/黄金买入区/仓位15.8%/EV三档/O8年化/尾部降档/自检全过）；stale 边界 512690 exit=2 仍产出草稿+过期警告；`run_all.py` 四步全 PASS（红队 74/74）。退出码语义 0/2/3/4/6 全验证。
