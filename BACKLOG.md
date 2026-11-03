# Ashare-sector-rotation · 欠账登记（BACKLOG）

> 建立于 2026-10-25（三维审计 L-4）。
> **存在理由**：本技能的历史欠账散落在 `CHANGELOG.md` 的「待定 / 待登记 / 遗留 WARN」、
> 2026-08-16 评审记录的 V 系列、以及三维审计的 E-4/E-5/L-5 里——**没有任何一处可被机械查询**。
> 结果是「V11/V13 待定」这句话在文档里躺了两个月，每轮都有人重新推演一遍。
> 本表把它收敛为**单一权威欠账清单**：一条欠账一行，有 ID、等级、状态、可验收定义与依赖。
>
> 纪律：
> 1. 新增欠账必须先登记本表，再在 `CHANGELOG.md` 记当轮动作（CHANGELOG 记**历史**，本表记**未完结**）。
> 2. 关闭欠账：把状态改 `已完成` 并在「定义/验收」末尾追加 `→ 落地见 CHANGELOG <章节>`，**不删行**（保留追溯）。
> 3. 本表由 `check_backlog.py --selfcheck` 机械校验（schema/唯一性/状态枚举/依赖存在性），
>    已挂入 `selfcheck_all.py` → 表结构破损会使 `run_all.py` FAIL。
> 4. 等级：P1=影响正确性/可复现，P2=效率与诚实性改进。

<!-- BACKLOG:START -->
| ID | 欠账 | 来源 | 等级 | 状态 | 定义 / 验收标准 | 依赖 |
|----|------|------|------|------|----------------|------|
| B-01 | V11 单标的 EV/仓位未计交易成本 | 2026-08-16 评审 · V11 | P2 | 待定 | 现状：EV 与建议仓位均为毛利口径，交易成本只在 backtest 层扣。验收：单标的 verdict 增净额披露（`ev_net_mid_pct` 与仓位净额），或在 disclosure 显式标注「毛利口径，未扣双边成本+冲击成本」，二者取一即关。 | — |
| B-02 | V13 回测与引擎 quant 路径同源化 | 2026-08-16 评审 · V13 | P1 | 进行中 | 2026-08-16 已核对：回测 `real_features` 仅产占位特征、从未调用 quant 派生 → 属「共同病根」非逻辑漂移。**已落地（2026-10-25 第三批）**：回测改为调用 `quant_inputs.derive_quant_inputs` 同一入口，并把 `win_rate_mode_counts` / `quant_derived_n_avg` 写入回测报告（此前完全不可见）；preflight 增「回测必须走单入口」硬断言。实测回测 195/195 全 table、派生 2.53/8，与 live 一致。**残差**：`A/B 可复现同一信号集`需 point-in-time 行业量价/资金流（依赖 B-10）；未达成前不得声称回测覆盖了 quant 路径。→ 落地见 CHANGELOG 2026-10-25 第三批 | B-10 |
| B-03 | iFinD 指数估值日历日序列未机械过滤 | CHANGELOG 2026-10-25 续 | P1 | 已完成 | **已落地（2026-10-25 第三批）**：新增 `trade_calendar.py`（分市场 cn/hk/us · 离线多数票 · 周末硬排除 · 区间外返回 None 不装懂）；`fetch_pb_mxdata.parse_table` / `ingest_ifind_valuation`（`--trade-day-only` 默认开）/ `ingest_neodata_valuation` 三处落盘口统一过滤；`merge_series` 增周末 fail-closed 纵深防御。存量修复 `repair_nontrading.py` 清除 12 个缓存共 575 个非交易日点（最大分位修正 950125.SH 76.84%→79.13%），备份留存。回归 `_test_b03_trade_day_20261025.py` 22 项全过（含注入）。→ 落地见 CHANGELOG 2026-10-25 第三批 | — |
| B-04 | 159755 月频缓存缺失（S-12 覆盖缺口） | CHANGELOG 2026-10-25 | P2 | 待办 | 现状：`data/index_monthly/` 无 159755 → D-4 长周期不可用、preflight 常驻 1 WARN。验收：补齐月频 PB 序列（≥120 月）使 `pb_longterm` available，或在该指数上显式声明 `terminal_na` 式豁免并落盘成因。 | — |
| B-05 | pyflakes benign 遗留（未用导入/变量） | 第二批 E-2 | P2 | 待办 | 现状：全树 0 F821/F823（硬错已清零），余 benign 风格项约 90 条仅 WARN。验收：`preflight_etf` 静态 lint 的 WARN 计数 ≤ 20（或明确长期挂账并在 SKILL 声明不修）。 | — |
| B-06 | 连接器就绪矩阵无实战样例映射 | 第二批 E-1 | P2 | 待办 | 现状：`check_connector_matrix` 只探活，坑登记表（PITFALLS）未回填「矩阵告警 → 实战样例」。验收：每类探针告警至少 1 条真实样例映射进 `preflight_etf` 提示文案。 | — |
| B-07 | E-4 现价双源串行拉取未并行 | 三维审计 · E-4 | P2 | 待定 | 现状：D-7 落地后新浪 + 腾讯两次快照**串行**（各 6s 超时上限）；正常时约 100~300ms。验收：两源并发（threading/futures）且单只现价段 P95 ≤ 现基线一半，双源语义与告警不变。 | — |
| B-08 | E-5 端到端耗时未按端到端口径计量 | 三维审计 · E-5 | P2 | 待定 | 现状：`_pipeline_meta.stages_ms` 只覆盖管线内部，缺「请求→交付」端到端口径。验收：verdict 落 `_pipeline_meta.wall_ms` 且 SKILL 明示「端到端口径」为唯一对外耗时口径。 | — |
| B-09 | L-5 双价格保守侧盲点未常驻披露 | 三维审计 · L-5 | P2 | 待定 | 现状：分歧行业双价格（出厂价 vs 市场价）仅在 `disclosure` 偶发提示。验收：命中双价格分层关键词时，verdict 恒带「保守侧盲点」常驻段落（说明下行档可能低估），不依赖 LLM 自觉。 | — |
| B-10 | O1 D1–D5 量化派生 helper 真实化 | 2026-08-16 评审 · O1 | P1 | 进行中 | **已落地（2026-10-25 第三批）**：管线增 `_quant_coverage` 计量（derived_n/8 + 缺失字段 + 预期胜率路径），把覆盖率从估算变成可测数字；实测 512800 = 2/8（缺 q_vol_yoy / q_inv_yoy / q_util / q_fund_flow_pct / q_fund_turn_pct / q_earning_rev）→ 预期 table。**残差不做（有据）**：`win_quant.map_validated=False`，校准 AUC≈0.50 无预测力、W_eff 向 50 收缩；此时用代理特征把覆盖率做高 = 把结论推向中性且无验证支撑。验收：接入 point-in-time 行业量价/库存/产能利用率/资金流真实数据源 → 覆盖率 ≥80% → 跑 `calibrate_winrate` 且 AUC>0.55 → 方可置 validated。→ 落地见 CHANGELOG 2026-10-25 第三批 | — |
| B-11 | 港股/美股交易日历样本不足 | 第三批 B-03 衍生 | P2 | 待办 | 现状：本地仅 3 个跨境指数缓存（HSHYLV.HK / HSTECH.HI / SPX.GI），低于 MIN_SERIES=3 → 日历 `available=False` → 这 3 只**不做交易日过滤**（宁可不过滤也不套用 A 股日历删真实交易日）。验收：跨境缓存 ≥3 只/市场，或引入离线交易所日历文件，使 hk/us 日历可用且 `_test_b03` 增两市场判例。 | — |
| B-12 | 新增 ETF 映射必触发「统计块过期」阻断 → 每只新标的固定多一轮往返 | 2026-10-25 实跑 159040 | P2 | 待办 | 现状：`etf_index_map` 每加一只就会使 SKILL.md 的 AUTO:STATS 计数过期，`preflight_etf` 判 MUST → 整批阻断，必须手工跑 `make_stats.py` 再重跑管线（实测多 1 轮 + 约 4s，且新标的首次分析 100% 命中）。闸门本身正确（防统计块说谎），但**可预见的必现摩擦不应靠人记**。验收：管线检测到映射表相对上次统计块有增删时，自动重算统计块并留痕（或 preflight 报出「一键修复命令」且允许 `--fix` 自愈），使首次分析不再多一轮。 | — |
<!-- BACKLOG:END -->

