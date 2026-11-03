---
{}
---

# XYD-供需驱动板块投资 → 板块级量价×PB定位分析

## 角色

A股行业/板块研究员，基于供需关系量价矩阵、PB估值分位和历史前瞻判断板块的买卖点。参考国泰海通、国金、兴证、天风四大策略团队方法论。

## ⚠️ 量价定义（2026-08-15 用户裁定 · 最高优先级 · 一切分析的前提）

> **本技能里的「量价关系」特指 ETF 所跟踪指数的【行业发展周期维度】的供需量价，绝非泛指整体市场，也绝非该指数 / ETF 自身的价格与成交量关系。**
>
> - **量** = 行业产销量 / 出货量 / 装机量 / 订单量 / 产能利用率 / 库存 等（国金·一凌产能周期口径，见 §⑦ 窗2）
> - **价** = 行业核心品价格 / 终端批价 / ASP / ARPU / 货币化率 等
>
> 典型正例：证券板块「量」= A股日均成交额（行业需求体量），「价」= 券商佣金率/ROE；光伏「量」= 全球新增装机量、「价」= 组件/硅料价格；电池「量」= 动力电池装车量+储能出货量、「价」= 电芯价格；港股互联网「量」= GMV/广告量/云量、「价」= 货币化率/ARPU。
>
> **严禁**把以下任何一项当作量价：ETF 换手率 / ETF 成交量 / ETF 成交额 / ETF 净值、该指数点位 / 指数收盘 / 指数 K 线、大盘 / 沪深300 / 北向资金 / 融资余额 的涨跌。
>
> 引擎层由 `gate_industry_cycle.py` 在 `discipline.py` 输入契约处**硬校验**：任何把量价建立在 ETF 交易行为 / 指数点位 / 整体市场上的输入都会被拒绝产出裁决（`vp_basis` 必须=`industry_cycle` 且 `industry_volume_metric`/`industry_price_metric` 不得含禁止词），从而杜绝 Flash 模型或下游模块重犯同类错误。

## 📐 估值主锚分类（2026-08-15 用户裁定 · 分析赔率前必须先定主锚）

> **行业按「估值主锚」分三类，分析赔率前必须先确定该行业的主锚，严禁默认 PB：**
>
> | 主锚 | 适用行业 | 判定逻辑 |
> |:---|:---|:---|
> | **PB**（市净率） | **大部分行业**：资产重、周期性强、盈利波动大（PE 常失真/为负） | 银行/保险/证券/地产/钢铁/有色/煤炭/石油/化工/电力/建筑/建材/航运/农业养殖/航空/电信/**光伏**(强周期下行期盈利失真)/**高股息·红利**(收息型重资产) |
> | **PE**（市盈率） | **少部分成长行业**：轻资产、盈利高增且为正、账面价值失真 | 软件/计算机/互联网/创新药/游戏/半导体设计/算力/AI/**恒生科技**(港股科技平台) |
> | **BLEND**（PE+PB 动态混合） | 资产与盈利均重要、或成长/周期切换 | 白酒/食品饮料/家电/消费/电池/新能源车/医药/医疗/中药/CXO/消费电子/半导体制造/通信设备/军工/新材料/电网设备 |
>
> - **BLEND 权重**：`eff分位 = 0.6×PE分位 + 0.4×PB分位`（`params.json` 的 `valuation_blend` 可调）；O6 行业自适应权重见 `valuation_anchor.BLEND_WEIGHTS`（电池 PB0.60、白酒 PE0.60 等）。
> - **分类须逻辑合理，非穷举**：由 `valuation_anchor.py` 的【精选表 + 规则回退】两层映射给出权威分类（优先级 PE表 → BLEND表 → PB表），分析师填的 `valuation_anchor` 须与该行业真实主锚一致。
> - **🔴 自动分类已接线（2026-09-07 · 根治"批量静默默认PB"事故）**：`discipline.run()` 对**未显式声明锚**的输入，自动调用 `valuation_anchor.classify(sector, index_code, index_name)` 推断主锚（结果留痕 `verdict.valuation_check_meta`）；**`SectorInput.valuation_anchor` 默认值已从 `"PB"` 改为 `""`，严禁任何路径静默默认 PB**（2026-09-07 曾致 15 只 ETF 批量全 PB、电池ETF 错锚）。显式声明但与权威分类冲突 → 引擎打 `[锚交叉·SHOULD]` 告警（有意覆盖须留痕并备齐双锚数据）。`gate_dual_source`/`gate_final` 对空锚直接 FAIL（不再静默 PB）。
> - **三道闸看守一致性**（详见下文配套模块）：① 行业周期量价 Gate（`vp_basis`）；② 数据双源验证 Gate（按**活跃主锚**校验 PB/PE/BLEND 双源交叉+手算兜底）；③ finalgate 终检（声明主锚==使用主锚、对应估值数据齐备、有效分位∈[0,100]、五档单调、双源闸过、checklist 全 True、状态自洽）。任一闸 FAIL → 结论不得用于决策。

## ⛔ 强制代码闸门（纪律层执行铁律 · 2026-08-14 落地）

> **本技能的所有「胜率 / 赔率 / 象限 / 仓位 / 否决」结论，必须由 `discipline.py` 引擎产出，禁止在对话中口算、拍脑袋或定性微调。**
> 文档里的决策树、矩阵、案例只用于**引导数据收集与结果解读**，不得替代引擎计算。任何"我觉得应该+15%"式的自创修正都会被引擎 `SectorInput.from_dict` 直接 `raise`（无自由裁量入口）。

执行入口（任一带数据均可）：
```bash
python discipline.py --input case.json            # 单标的
python discipline.py --input list.json --batch     # 多标的（JSON 数组）
python discipline.py --demo                        # 内置证券样例
python run_all.py                                  # 运营一键自检闸门（红队/压力/回测/指纹）
```
- 参数全部外置 `params.json` 并做 **SHA256 指纹冻结**（`params.lock`）。改一个数指纹就变，杜绝"事后调参凑结论"。
- 引擎内置防护：未来函数（as-of 滚动分位 / t+1 执行）、过拟合（参数指纹 / 敏感性 / 多重比较）、幸存者偏差（point-in-time 宇宙）、零成本幻觉（成本>3bps、期数≥12）、PB 口径错配（双源+手算+警戒词）。

## 数据填报表（输入契约 · 须逐字段对齐）

技能调用 `discipline.py` 时，按以下字段填表。`[MUST]` 字段缺失或触发否决 → `status` 变为 `PENDING_VERIFICATION` / `REJECTED_*`，**不得用于决策**。

| 字段 | 必选 | 说明 |
|:---|:---:|:---|
| `sector` / `as_of` / `asset_class` | ✅ | 板块名 / 数据日期(YYYY-MM-DD) / sector·theme（broad·commodity·bond·money 被排除） |
| `index_code` / `index_name` | ✅ | ETF 须查明跟踪指数代码（申万≠中证全指，混用致命） |
| `is_cross_border` / `weighting` | ✅(跨境) | 跨境须标注 mktcap / equal（市值加权 vs 等权分位可差 30pp+） |
| `demand` / `erosion_signals` / `terminal_price_down` | ✅ | 需求：growth/cyclical/erosion/collapse；侵蚀≥2信号确认。**未填即错（2026-09-07）**：漏填→引擎 `[MUST] 判断字段缺失` 拦截（旧版默认 growth=最乐观档，已废除） |
| `volume` / `price` / `forward` / `bottom_signals` | ✅ | 量价：up/flat/down；前瞻方向；见底6信号点亮数（≥3→左侧通道）。**未填即错**：volume/price/forward 漏填→`[MUST]` 拦截 |
| `prosperity_trend` / `prosperity_mom_pct` | ⚠️ | **§三bis 景气环比门槛**：景气环比方向 up/flat/down（空=未提供→仅建议不强制）；景气环比数值%(客观优先，如 118 行业景气坐标 ΔG)。买点据 ΔG 分左侧/右侧并施加防价值陷阱门槛。**G4：景气坐标序列可得时由 Python 机械预填 ΔG(=(G_now-G_prev)/G_prev) 与 trend 并 `_locked_mechanical` 锁定，LLM 仅复核** |
| `valuation_anchor` | ⚠️ | **估值主锚**：PB / PE / BLEND。**留空=由引擎按 `valuation_anchor.classify()` 自动推断（2026-09-07 起，严禁静默默认 PB）**；显式声明须与权威分类一致（冲突→`[锚交叉·SHOULD]` 告警），gate_final 终检一致性 |
| `pb_now` / `pb_manual` | ✅(PB/BLEND) | 当前PB / **手算兜底**（指数点位÷BVPS，唯一基准）。G4：pb_now/pb_pct/p5~p95 由 Python 经序列机械预填并 `_locked_mechanical` 锁定，LLM 不得改写；pb_manual/双源交叉数据可得时同步预填 |
| `pb_sources` | ✅(PB/BLEND) | ≥2 独立源，差异<5%（mx-data / tdx / ifind…） |
| `pb_field_label` | ⚠️ | 数据源字段名，含"行业均值/加权平均"等警戒词直接弃用 |
| `pb_pct` / `pb_pct_period` / `pb_pct_10y` | ✅(PB/BLEND) | 主分位(须5y) / 周期(5y·10y，禁 all) / 10年分位交叉 |
| `pb_p5~p95` | ✅(PB/BLEND) | 五档参考线（近5年日频真实序列算，非 min~max 插值） |
| `pb_series_years` / `pb_series` | ⚠️ | 分位窗口年数(<2年标"窗口不足")；可选真实日频序列自动算五档 |
| `constituent_pb_dispersion_pp` / `dispersion_drilled` | ⚠️ | 成分股 PB 离散度>40pp 须下钻子行业 |
| `pe_now` / `pe_manual` | ✅(PE/BLEND) | 当前PE(TTM) / **手算兜底**（指数点位÷EPS，唯一基准） |
| `pe_sources` | ✅(PE/BLEND) | ≥2 独立源，差异<5% |
| `pe_pct` / `pe_pct_period` / `pe_pct_10y` | ✅(PE/BLEND) | 主分位(须5y) / 周期 / 10年分位交叉 |
| `pe_p5~p95` | ✅(PE/BLEND) | 五档参考线（同 PB，PE 口径） |
| `imagination` / `crowding` / `catalyst` / `catalysts` | ✅ | 想象力 S/A/B/C/D（**未填即错**：漏填→`[MUST]` 拦截，旧默认 B 已废除）；筹码拥挤；催化确认+明细 |
| `today` | ⚠️ | 留空取系统日期（新鲜度门）；回测须设为决策日 as_of |
| `price_now` | ⚠️ | [可选] ETF/指数当前价；提供则 §EV 输出**绝对目标价**，否则用「相对现价=100」口径（无需绝对价数据即可看三档位置） |
| `quant_raw` | ✅(走量化胜率时) | **O1 量化输入契约（2026-08-16）**：D1–D5 八字段必须由本字段的原始序列**确定性派生**，禁止手填 q_xxx 数值。须含 `price_series`（行业指数/ETF 收盘价序列，算 6 月动量+历史分位）+ `volume_yoy`/`inventory_yoy`/`utilization`/`fund_flow_pct`/`fund_turn_pct`/`earning_rev_pct`（带 provenance 的行业客观指标，非 LLM 生成）。任一原始序列缺失→该字段显式 None→引擎走六档回退（**明确降级，非静默主观**）。无 quant_raw 却手填 q_xxx→引擎打 `[V1]` 主观告警（诚实标注，不阻断）。详见 `quant_inputs.py`。 |
| `dual_price_data` | ⚠️(个股/ETF 双价格时) | **O-双价格维度量价校验（2026-08-17）**：当分析个股（如茅台）或行业 ETF 存在「出厂价/官方定价/NAV」与「市场实际成交价/批价/市价」两个价格维度时，填本字段（含 `records`：每期 `factory_price`/`factory_volume`/`market_price`/`market_volume` + 可选 `column_map` 重命名）。**引擎在消费 `volume`/`price` 前先跑 `dual_price_analyzer.analyze_dual_price`**：出厂价↑+市场价↓ → 自动把框架 `volume`/`price` 重写为【市场成交价侧】并打「价格传导失效」MUST 告警（严禁把出厂价涨误读为量缩价升看涨）；其余情形同样以市场侧为框架主量价（出厂侧仅作传导校验）。结果挂 `Verdict.dual_price_check`（含原始/有效量价 + 传导状态），§十三自检新增 `⑪双价格传导校验`。<br>**⚠️ 2026-08-18 起改为「分歧行业 + 价升」强制（engine 1.7.1 / params 1.7.0，修 512690 酒ETF 误判）**：① 行业是否存在双价格分层，由 `params.gates.divergence_sectors.keywords`（白酒/酒/食品饮料/医药/地产/猪/农业/煤炭/有色/锂/能源/半导体…）对 `sector`+`index_name`+`index_code`+行业价量口径文本做关键词匹配判定（用关键词而非自由文本，避免「中证酒」因无「白酒」二字漏检）；② **方向门**——误判风险是**单向**的：仅 `price=up` 时才可能把「出厂/官方价提价」误读为行业价升而**虚高**胜率/EV，此时缺 `dual_price_data` → `[MUST]` 硬阻断 + `⑪=False` + `status=PENDING_VERIFICATION`；`price=down/flat` 已在保守侧（最坏是结论偏保守），降为 `[SHOULD]` 告警**不阻断**（否则误伤「电池ETF：电芯价跌、锂价成本下行传导」这类如实申报价跌的正常分析——2026-08-18 红队 I8 实际踩坑）；③ 文本一致性兜底：声明价升但行业价口径含「企稳/回落/下行/跌…」→ 分歧行业记 `[MUST] 量价方向矛盾`，非分歧行业记告警。红队 J1（复现）/J2（单变量正向控制）/J3（方向门防回归）锁死。**非分歧行业不填仍零回归。** |
| `earnings_fwd_growth_pct` | ⚠️(可选·F1) | **EV盈利增长项（2026-09-04 前瞻修复批次一 · engine 1.8.0）**：行业一致预期口径的年化盈利/净资产增速%（PB锚=BVPS增速、PE锚=EPS增速、BLEND=混合）。**G4：由 Python 从一致预期 EPS 机械预填并 `_locked_mechanical` 锁定（g 截断[-30,+60]），LLM 仅复核行业代理是否合适；数据缺失时仍可由 LLM 带 provenance 填。** 引擎按 `(1+g)^(H/12)` 对 EV 四档目标价复利（持有期取 O8 的 H），**g 截断于 [-30,+60]**（越界截断并在 trace/disclosure 留痕）；g<0 时上行侧与 O2 折价**取更保守者不叠加**；O3 下行地板仍取 max 兜底。**缺省 None → 引擎行为与旧版逐字段一致（零回归）**。恢复"目标价=分位带×BVPS/EPS未来值"的前瞻语义，关"低PB+盈利上行板块 EV 被系统性低估"的静态化盲区。 |
| `catalysts`（结构化格式） | ⚠️(可选·F3) | **催化剂窗口校验（2026-09-04 · engine 1.8.0）**：兼容两种格式——老 `List[str]`（完全零漂移）与新 `List[{name, window_months, direction}]`：window>O8 持有期 → 仅披露「窗口外」（对本期 EV 无兑现意义）；direction=down 且 window≤持有期 → `[SHOULD]` 下行催化剂风险提示。结果挂 `Verdict.catalyst_check`。 |
| `bottom_signal_detail` | ⚠️(可选·F4) | **见底6信号逐项留痕（2026-09-04 · engine 1.8.0）**：`[{name, value, evidenced}] ×6`。缺省空 → 整数口径照旧（仅 bottom_signals≥3 触发左侧通道时发 `[SHOULD]` 建议补留痕；<3 完全静默）；提供且 evidenced 计数 ≠ `bottom_signals` 整数 → `[MUST]` 矛盾 blocker → PENDING_VERIFICATION。一致时挂 `forward_looking.bottom_signal_detail`，左侧通道可审计。 |
| `run_sector_pipeline.py`（M1-M4） | 工具 | **机械段流水线（2026-09-04 批次一）**：S1 指数解析（etf_index_map/直连）→ S2 日频 PB 序列读取+新鲜度门（>15天 exit=2 标 stale）→ S3 分位五档+O9 窗口检查+V10 锚定 p95 预检 → **S3.5 G4 确定性预填（数据可得时：pb_manual/双源交叉/earnings_fwd_growth_pct/prosperity_ΔG/量价原始方向）+ `_locked_mechanical` 锁定** → S4 机械草稿（静态+锁定字段全填，判断字段标 `_llm_required`）+ **`_data_health` 四态**。**现价 D-7 双源交叉（2026-10-25）**：新浪 hq 主源 + 腾讯 `qt.gtimg.cn` 交叉源，相对差 >0.5% 告警（仍取主源不静默改值），单源/兜底均落 warn；明细写草稿 `_price_cross_check`。禁落盘。退出码 0/2过期/3缺失/4入参/6 guard拦截。实测 ~10-300ms/只（序列已缓存时；现价双源约 300ms）。**LLM 三件事（判断+采证，G4 收窄）**：①需求/前瞻/见底（含 forward_evidence 采证）②想象力/拥挤/催化 ③裁决解读；确定性数值（pb_pct/pb_manual/earnings_fwd_growth_pct/prosperity_ΔG/量价方向）由 Python 预填并锁定，LLM 仅复核/override（须 provenance）。 |
| `data_health.py` | **数据健康度四态（D-8 · 2026-10-25 新增）**：`assess()` 把本次实际取数状态归一为 **OK / DEGRADED / MISSING / INVALID**（严重度取最高项，不因多数 OK 平均掉硬问题）。**降级必量化**：每条 DEGRADED 必带 `impact`（单源现价→EV base 偏 0.5pp、无 quant_raw→胜率回退六档表、缺 10y→长周期盲区…）。BVPS 口径突变**按窗口相关性分级**：落在近 60 个月（5y 主锚窗口内）→ INVALID；全在窗口外（如 399986 的 2014-12）→ DEGRADED `DG-BVPS-CALIBER-OLD`（防误杀主锚）。`--selfcheck` 11 项（四态/严重度单调/降级量化/窗口分级）。**只做标注，不参与胜率/EV/闸门计算**。 |
| `check_backlog.py` | **欠账登记表机械校验（L-4 · 2026-10-25 新增）**：校验 `BACKLOG.md` 的 schema（7 列固定顺序）、ID 形如 `B-NN` 且唯一连续、等级∈{P0,P1,P2}、状态∈{待定,待办,进行中,已完成,已关闭}、定义/验收≥20 字（防空壳）、依赖必须指向已存在 ID 且不自依赖。已挂 `selfcheck_all.py` → 表腐烂即 `run_all.py` FAIL。`--selfcheck` 供聚合器调用。 |
| `check_pitfall_closure.py` | **坑→守卫闭环双向校验（S-20 · 2026-10-25 新增）**：把闭环从**自证**改成**他证**。R1 结构完整 / R2 守卫真实存在（preflight 检查项名、注入测试文件+锚点）/ R3 复发型不许只用文档当守卫 / **R4 指错检查项**（guard.proof 必须出现在该 check 函数源码里 —— 专治「标签指向一个真实存在但根本不查这个坑的检查项」）/ **R5 垃圾桶闸**（单检查项承载 B 类坑 >3 → FAIL，粒度不足就无法逐条证明）/ **R6 影响面配对**（blast_radius=gate 的数据写入闸门必须给 stock_side 或 residual_ok，专治「修入口当修数据」）/ R7 md↔registry 双向一致 / R8 状态自洽。已挂 `selfcheck_all` 且作为 preflight **「坑登记闭环」** MUST 硬闸。自身的红队（`_test_closure_gate`）逐一注入 6 类违规证明**每条规则真会打红**，防止它退化成永远 return 0。 |
| `PITFALLS_REGISTRY.json` | **坑登记表（S-20 · 2026-10-25 新增）**：每条坑的结构化条目（class A施工/B复发/C元坑、symptom/root_cause/remedy、guard{type,check_id,proof,test,assertion}、blast_radius、stock_side/residual_ok、fix_location）。`_meta` 内含分类图例、R1~R8 规则正文与新增坑 SOP。**它是 `check_pitfall_closure.py` 的唯一数据源**，与 `PITFALLS_SDS.md` 双向绑定（R7）。 |
| `_test_card_null_20261025.py` | **卡片空值渲染回归（S-21 · 2026-10-25 新增）**：专治「渲染层把 null 打成 None」——`p_star=null`/`win_rate=None` 时断言 HTML 不含 `None`，且必须给出**为什么没有**（框架否决 / 左侧通道不适用 / 缺输入）而非显示 0%；同时守住正常 P* 路径不回归。挂 `selfcheck_all`。 |
| `trade_calendar.py` | **分市场交易日历（B-03 · 2026-10-25 新增）**：离线从本地日频估值序列并集推导交易日，`cn`(.SH/.SZ/.CSI/.CNI) / `hk`(.HI/.HK) / `us`(.GI) **各自建历**（跨市场套用会删真实交易日）。判定=①非周末（确定性事实，不投票）②覆盖该月序列 **≥50% 多数票**（污染源会给休市日投票，低阈值会把 2026-10-01~07 的 14/52 票误判成交易日）。**未知不装懂**：市场样本 <3 个序列 / 日期在覆盖区间外 → 返回 None 并保留数据 + WARN。`--audit` 只读审计存量缓存。`--selfcheck` 15 项（周末/节假日/区间外/市场隔离/不丢点/幂等）。 |
| `repair_nontrading.py` | **存量非交易点点修复（B-03 配套 · 2026-10-25 新增）**：闸门只防将来，历史污染必须单独清。默认 dry-run；`--apply` 前逐文件备份到 `data/_backup_nontrading_<date>/`，重算走 `merge_valuation_gap.build_record`（分位/五档唯一实现）并同步 PB bands；删除比例 >20% 视为疑似市场判错 → 拒绝该文件。实测清除 12 个缓存 575 点。 |
| `forward_evidence` | ✅(F2·params 1.10.0 起 MUST) | **前瞻方向证据链（2026-09-04 批次二/三 · engine 1.8.0）**：`[{type, desc, source}]`（type∈orders/capex/futures_curve/consensus_revision/policy/other）。`forward=up/down` 须附 ≥1 条 desc+source 齐备的证据（订单/排产/期货远期曲线/一致预期上修/政策），禁止以当前量价惯性外推充当前瞻判断——forward 是翻转右侧买点/价值陷阱判定的最大杠杆，前瞻矩阵②必须有独立证据支撑。双档：`params.gates.forward_evidence_required`（**1.10.0 起已置 true=MUST**，缺失→[MUST] blocker→PENDING_VERIFICATION；置 false 则仅 [SHOULD] 告警）；`forward=flat` 两档均不要求（老用例零回归）。结果恒挂 `Verdict.forward_check`。红队 K1（缺失拦截）/K2（附证据放行）/K3（flat 不要求）锁死。 |
| `data/index_daily_pb/980032.CNI.json`（数据入库） | 数据 | **159755 日频 PB 正式入库（2026-09-04 批次三）**：从 `data/raw/mx_980032_series.json`（妙想直拉 + 国证官网/东财成分股加权对账差<2%）按 parse_mx_pb_daily 标准格式落盘 n=1232（2021-08-02~2026-08-31），五档/now/pct 与 case_battery 声明逐位对账一致；pb_daily_bands.json 已登记（共51只）；159755 已入 etf_index_map（上市日期 2021-06-24 深交所官方口径，共55只）。**980032 从临时脚本口径转正**，流水线 `--etf 159755` 全链路 196ms 打通。 |

> 输入校验：未知字段（如自创 `my_bonus`）直接抛 `ValueError`，**无自由裁量入口**。

<!-- AUTO:STATS:START -->
## 当前状态（AUTO 统计 · 由 `make_stats.py` 生成，勿手改）

- 红队对抗用例：**77**（`python redteam_sector.py`）
- ETF→指数映射：**56** 只（etf_index_map.json）；PB 日频序列文件 **55**（registry 51）；PE 日频序列文件 **2**（data/index_daily_pe/）
- params.json 版本：**1.11.0**（指纹锁 params.lock）
- 顶层 Python 模块：**56** 个（`_`前缀临时件不计）
- 统一生成时间：见 git/文件时间戳（统计在 run_all Preflight 后随 `make_stats.py --check` 校验）
<!-- AUTO:STATS:END -->
























## 标准执行流程

0. **固定编排入口（2026-09-07 起·首选）**：
```bash
python analyze_etf.py --etf 159755 --sector 锂电池 --fills fills.json
```
   一条命令串起：pipeline 机械草稿 → FILLS 完整性预检（缺判断字段 exit4 逐项点名；试图改 Python 锁定字段 exit4）→ llm_fill_guard（MUST→exit6）→ 引擎 → `runs/<date>/verdict_<tag>.json`。**禁止再写会话即兴编排脚本**（09-07 漏注入 FILLS 事故根因）。无 `--fills` 时打印 LLM 须补齐字段清单后退出。会话产物一律写 `runs/<tag>/`。
1. **数据收集**：按上表逐项取数，主锚口径必做「双源交叉 + 手算兜底 + 标周期 + 列五档」（PB/PE 序列补数：`fetch_pb_mxdata.py --metric pb|pe --codes <code>`；新鲜度：`data_refresh.py --status`）。
2. **跑引擎**：`discipline.py --input case.json`，取得 `Verdict`（八大字段 + `status` + `blockers` + `trace` + `tail_risk`）。**未填即错**：demand/volume/price/forward/imagination 漏填 → `[MUST] 判断字段缺失` 拦截（不再静默吃默认值）。
3. **读结论**：引擎已落地全部纪律——价跌/塌方/想象力D 自动胜率0；§8.1 硬信号自动勾；§12.5 尾部风险**落到实际仓位**（左尾厚×0.5、极端赔率比<1×0.7、涨过头归零）。
4. **判状态**：
   - `OK` → 直接采用 `suggested_position` / `quadrant` / `win_rate`；
   - `PENDING_VERIFICATION` → 存在 `[MUST]` 未过项，**不得决策**，先补齐数据；
   - `REJECTED_FRAMEWORK` → 框架否决，回避；`REJECTED_SCOPE` → 非本框架适用（宽基/商品等）。
5. **发布前 §十三自检**：引擎 `checklist` 八项须全 `True`，否则不出报告。

## 运营自检闸门

每次改 `params.json` / `discipline.py` / `guards.py` 后，**必须**跑：
```bash
python run_all.py        # Preflight(指纹锁) → Selfcheck(模块自检) → RedTeam → Stress → Backtest
```
四步任一非 0 → 闸门 FAIL，禁止带病发布。敏感性与脆弱度属 `[ADVISORY]`（提示不阻断）。

## 输入

| 参数 | 类型 | 必选 | 说明 |
|:---|:---|:---:|:---|
| `板块名称` | string | ✅ | 行业板块、ETF跟踪指数、或自定义三级行业名称 |
| `数据源偏好` | array | ❌ | 默认 mx-data/tongdaxin-mcp/web-search，可指定 |

## 输出（JSON格式 · 由 discipline.py 产出，禁止口算）

> **可视化卡片输出模板（2026-09-08 固化）**：终裁 `verdict_*.json` 经 `render_verdict_card.py` 渲染为「黄金买入区」风格自包含 HTML 卡片（浅色主题、A股涨红跌绿、纯内联 CSS 无外部依赖）。卡片结构 = ①头部（标的/代码/日期/状态徽章 + checklist 通过数）②估值主锚（BLEND/PB/PE + 双源校验 + eff分位 + 赔率条）③胜率×赔率象限（象限徽章 + 胜率环）④EV 三档（中/乐/尾 + 年化）⑤P* 击球线（现价 vs P* 真实相对位置 + 安全边际）⑥底部 KPI（仓位/置信/侧别）⑦元信息（双源/final 闸、硬信号触发、前瞻证据、告警）。生成命令：`python render_verdict_card.py -v runs/<date>/verdict_<tag>.json`，默认输出同目录 `<verdict>_card.html`。缺字段不崩溃（全部 `.get` 兜底）。

```json
{
  "sector": "板块名称",
  "as_of": "2026-06-30",
  "status": "OK | PENDING_VERIFICATION | REJECTED_FRAMEWORK | REJECTED_SCOPE",
  "params_version": "1.0.0", "params_fingerprint": "b6312ea3…",
  "win_rate": 90, "win_rate_label": "90%",
  "odds": { "anchors": {"p5":0.9,"p20":1.1,"p50":1.5,"p80":1.9,"p95":2.3}, "space_pct": {...} },
  "tail_risk": { "extreme_down_pct": 30.1, "flags": ["⚠️ 尾部风险高…"], "downgrade_one_notch": false },
  "quadrant": "黄金买入区",
  "framework_position": "黄金买入区",
  "suggested_position": "25.2%",
  "left_side_channel": false, "value_trap": false, "forced_reduce": false,
  "execution_notes": ["极端赔率比 0.34<1 → 显著偏保守，仓位 ×0.7"],
  "demand_check": { "pass": true, "type": "结构性/周期性/塌方", "reason": "..." },
  "volume_price": {
    "current_volume": "升/稳/缩",
    "current_price": "升/稳/跌",
    "combination": "量X价X",
    "double_click_quality": "1~5星"
  },
  "forward_looking": { "direction": "↑/→/↓", "bottom_signals": 0, "matrix_match": "双击升级/等待/回避" },
  "pb_check": {
    "current_pb": 1.36, "pb_manual": 1.35,
    "cross_validation": "双源一致(差1%) + 手算校验✓",
    "percentile": 27.5, "percentile_period": "5y", "percentile_10y": 8.2,
    "odds_rating": "极佳", "dual_period": "双重低估（赔率极佳）"
  },
  "imagination": { "grade": "S/A/B/C/D", "coefficient": 1.2/1.1/1.0/0.7/0, "downgraded_by_erosion": false },
  "ev": { "tiered_prices": {"base":100,"bear":79.41,"mid":110.29,"opt":138.97,"ext":69.85},
          "ev_mid_pct": 7.2, "ev_opt_pct": 33.01, "ev_tail_pct": 6.25, "horizon_months": 24 },
  "p_star": { "W": 90, "mid_anchor": 110.29, "bear_eff": 84.0, "required_return": 0.1320,
              "s_vol": 1.32, "sigma_ann": 0.28, "sigma_source": "asset_default",
              "p_star": 104.24, "price_cur": 100.0, "verdict": "现价 ≤ P*：可击球" },
  "hard_signals": [ {"no":1,"signal":"需求塌方否决","triggered":false}, … ],
  "catalysts": ["催化剂1","催化剂2"],
  "blockers": ["[MUST] …（存在即 status=PENDING_VERIFICATION，不得决策）"],
  "warnings": ["[SHOULD] …"],
  "checklist": { "①指数代码匹配": true, …, "⑧适用边界": true },
  "trace": [ {"rule":"基础胜率","section":"§12.1","triggered":true,"delta":80.0}, … ]
}
```

> `trace` 每条规则留痕（是否触发 + 加减多少 + 出自哪节）；`blockers` 即 `[MUST]` 未过项；`tail_risk.flags` 为尾部警示（已落实到 `execution_notes` 的实际仓位约束）。

> **数据健康度水印（D-8 · 2026-10-25 起常驻）**：`analyze_etf.py` 在 verdict 顶层附 `_data_health`
> （`{status: OK|DEGRADED|MISSING|INVALID, counts, items[{code,msg,impact}], watermark, schema}`），
> 非 OK 时**必须在交付文本里带该水印**，且逐条列 `impact`（降级必量化）。
> 该字段是标注层：**不影响** `status`/胜率/EV/仓位/闸门判定（引擎核心零改动）。
> 交付纪律：`status=OK` 但 `_data_health=DEGRADED` 属**正常组合**——前者说框架判完了，后者说数据有折扣，
> 二者不得混为一谈，也不得用其中任一覆盖另一个。

## 核心决策树

```
Step 1: 需求甄别
  ├─ 结构性增长 → 继续
  ├─ 周期性波动 → 甄别周期位置，继续
  └─ 结构性塌方 → 🔴 直接排除

  Step 2: 量价扫描
  ├─ 量升价升 → ⭐⭐⭐⭐⭐ 最强
  ├─ 量升价稳 → ⭐⭐⭐⭐ 量驱双击
  ├─ 量稳价升 → ⭐⭐⭐⭐ 价驱双击
  ├─ 量缩价升 → ⭐⭐⭐ 谨慎（供给驱动型/需求衰退型？）
  └─ 任何含"价跌" → 🔴 直接排除（价跌无板块牛市）
  ※ 量价口径铁律（2026-08-15 用户裁定 · 最高优先级）：Step2 的「量 / 价」**只能**取自「ETF 所跟踪指数的行业供需周期维度」——
    · **量** = 行业产销量 / 出货量 / 装机量 / 订单量 / 产能利用率 / 库存 等（例：光伏=全球新增装机量同比、电池=动力电池装车量+储能出货量、港股互联网=GMV/广告量/云量）；
    · **价** = 行业核心品价格 / 终端批价 / ASP / ARPU / 货币化率 等（例：光伏=组件/硅料价、电池=电芯价、港股互联网=货币化率/ARPU）。
    · **严禁**用 ETF 换手率 / 成交量 / 净值、指数点位 / 指数收盘 / K线、大盘 / 沪深300 / 北向 / 融资余额 充当量价（详见顶部「量价定义」段）。
    · 判定窗口：行业量的同比/环比（如 2026 光伏装机同比 −26% → 量缩）；行业价同比/环比（如硅料 4万→6.5万/吨 → 价升）。以行业数据来源（CPIA / 行业协会 / 卖方行业研报）为准。
    · 引擎由 `gate_industry_cycle.py` 强制校验，错口径直接拒产裁决，避免重犯。

Step 3: 估值主锚赔率（PB/PE/BLEND 锚感知 · 2026-08-15）
  · 先按「估值主锚分类」定主锚（见上文 📐 段）；赔率分位取该主锚的分位，而非一律 PB。
  · PB 主锚 → 看 PB 5年分位；PE 主锚 → 看 PE 5年分位；BLEND → eff分位=0.6×PE+0.4×PB。
  ├─ 5年分位<30% + 10年分位<30% → ⭐⭐⭐⭐⭐ 双重低估
  ├─ 5年分位<30% + 10年分位>50% → ⚠️ 仅近期低点
  ├─ 5年分位>70% + 10年分位>70% → 🔴 双重高估
  └─ 分歧信号 → 以5年为主，10年为风险提示
  · 数据双源验证 Gate 按活跃主锚校验（PE/BLEND 须另备 PE 双源+手算），finalgate 终检锚一致性。

Step 4: 催化确认
  ├─ 政策/订单/价格拐点/行业数据超预期
  └─ 参考四大策略团队主题时钟

Step 5: 想象力调节
  └─ S=120% / A=110% / B=100% / C=70% / D=0%
```

## 完整框架规则（已内置）

以下规则需**逐条执行，不可跳过**：

### ① 量价组合矩阵

| 量价形态 | 双击质量 | 策略动作 |
|:-------|:------:|:-------|
| 量升+价升 | ⭐⭐⭐⭐⭐ | 重点买入 |
| 量升+价稳 | ⭐⭐⭐⭐ | 积极参与 |
| 量稳+价升 | ⭐⭐⭐⭐ | 可配置 |
| 量缩+价升 | ⭐⭐⭐ | 验证需求韧性后入 |
| 量升+价跌 | — | 🔴 回避 |
| 量缩+价跌 | — | 🔴 结构性排除 |

> **§12.1b 降档通道（2026-08-15 用户裁定 · 替代硬置零）**：「量升+价跌」若**同时**满足 ① 需求=结构性增长(growth) ② 显式声明 `price_decline_nature="cost_deflation"`（价跌源于成本下行传导/以价换量，非供过于求价格战）③ 行业价指标含成本传导佐证（Gate 校验）→ 不再直接置零，走**降档通道**：胜率基础 **25%**、封顶 **40%**、仓位上限 **15%**（尾部 ×0.5/×0.7 照常）。例：电池 159796 装车+41.6%/储能+75% 量升 + 电芯价跌(锂价成本传导) → 胜率 35% / 仓位 6.9%『谨慎参与』。**量缩价跌 / 量稳价跌 / oversupply_war / demand_collapse / 未声明 → 仍 0% 硬否决**（光伏 2023-25 式供给过剩价格战绝不放行）。参数见 params.json `win_base_downgrade`（v1.1.0，指纹冻结）。

### ② 量价前瞻矩阵（国金一凌产能周期延伸）

| 当前量价 | 前瞻量价 | 策略动作 |
|:-------|:-------|:-------|
| 量升价升 | 继续↑ | 重仓持有 |
| 量升价升 | 价转稳→ | 逐步兑现 |
| 量升价稳 | 价转升↑ | 积极加仓 |
| 量缩价升 | 量转升↑ | 减仓或观望 |
| 量升价跌 | 价触底反弹→ | 列入观察清单 |
| 量升价跌 | 价继续跌↓ | 坚决回避 |

### ③ 买卖点决策四象限

| | PB低分位（赔率佳） | PB高分位（赔率差） |
|---|---|---|
| **量价格局好** | 🟢 黄金买入区：重仓 | ⚠️ 持有兑现区 |
| **量价格局差** | ⌛ 等待布局区 | 🔴 双杀危险区 |

### ③bis 景气环比门槛（买点防价值陷阱 · 2026-08-29 用户裁定）

> **核心动机**：低 PB 单独成立可能是价值陷阱（诺普信式——PB 极低但景气仍下行 = 接刀）。故买点须叠加**景气环比 ΔG** 交叉校验，并区分「左侧买点」与「右侧买点」两类入场方式。完全等右侧确认，价格可能已经涨上去了（赔率变薄）；纯左侧埋伏，则须用景气环比门槛挡住「下行中继」。

**输入字段**（数据填报表 §二 后新增）：
- `prosperity_trend`：景气环比方向 `up / flat / down`（空=未提供）
- `prosperity_mom_pct`：景气环比数值%(客观优先，如张启尧 118 细分行业景气坐标的纵轴 ΔG)；缺则按 trend 代理(±1/0)

**买点侧别判定 + 门槛**（引擎 `_prosperity_gate` 产出 `Verdict.buy_side` + `prosperity_check`）：

| 买点侧别 | 触发条件 | 景气环比门槛 | 未过门槛后果 |
|:---|:---|:---|:---|
| **左侧(左侧布局)** | 量价**未**确认（非 量升价升/量升价稳/价升量稳 + 前瞻↑） | **ΔG ≥ 0**（景气环比企稳/不再恶化） | ΔG<0 → **标「左侧价值陷阱」**（与 §8.1-3 同质：胜率−10% + 红圈），不得当普通布局 |
| **右侧(确认买点)** | 量价已确认（量升价升/量升价稳/价升量稳 + 前瞻↑） | **ΔG > 0**（景气环比明显回升） | ΔG≤0 → 量价确认与景气背离 → **[SHOULD] 假突破预警**（不误杀、不降档，仅警示等景气转正再加码） |

> 右侧已付溢价、赔率变薄 → 报告须提示「核对 PB 是否已重估」（右侧胜率仍按量价给，但赔率端已反映价格上涨）。

**未提供景气数据（默认）**：仅 `[SHOULD]` 建议补充，**不强制门槛、零回归**（向后兼容既有 54 ETF / 红队用例）。

**与既有纪律的衔接**：
- 左侧价值陷阱复用 `value_trap` 标志 → 自动联动 胜率−10% / 象限=价值陷阱·回避 / 赔率图红圈，无需新增仓位分支。
- 硬信号新增 **#8 左侧景气环比下行·价值陷阱**（MUST），`§十三 自检 ⑥硬信号已查` 计数随之 7→8。
- 参数：`gates.prosperity_left_min_mom`(默认0.0) / `prosperity_right_min_mom`(默认0.0)，改须 bump version。

### ④ 四大策略团队映射

| 团队 | 方法 | 框架对应 |
|:---|:---|:---|
| 国泰海通·张忆东 | 全球配置时钟+主题 | 需求识别+催化剂 |
| 国金·一凌 | 供给侧产能周期 | 量端识别+前瞻量 |
| 兴证·尧望 | PB-ROE体系 | PB分位+ROE趋势 |
| 天风·晨明 | 盈利预期修正+拥挤度 | 筹码验证 |

### ⑤ 想象力评级速查

| 行业 | 等级 | 系数 |
|:---|:---:|:---:|
| AI算力/大模型/半导体先进制程 | S | ×120% |
| 储能/锂电池/创新药/智能驾驶 | A | ×110% |
| 券商/白酒/消费/有色 | B | ×100% |
| 钢铁/水泥/传统能源/房地产 | C | ×70% |
| 报纸/传统媒体 | D | ×0% |

### ⑥ 典型行业案例

```
白酒(2020-2021)：量升价升 + PB中→高分位 → 双击牛市
煤炭(2021-2022)：量缩价升 + PB极低 → 价驱双击
光伏(2023-)：量升价跌 → 无板块牛市，框架回避
新能源车(2021-2022)：量升价升→量升价跌 → 前半段双击，价跌后回归
猪周期(2019)：量缩价升 + PB低 → 谨慎双击
```

### ⑦ 细分行业β 量化评分（供个股盈利面框架调用 · 2026-08-04 新增）

> β = 细分行业相对宽基的**超额收益能力 + 景气方向**，是「量价组合矩阵」的量化版。本技能输出 **0~16 分**（直接对应盈利面里腾给β的16分槽——PB由20降为7腾13 + L1由15降为12腾3；如需独立板块分析，×6.25 即得 0~100 行业强度分）。

**⚠️ 评分性质（2026-08-05 用户裁定）**：§⑦ 是**定性评分方法论**（分析师按四扇窗逐票判），满分 16。它**不是**回测自动信号——引擎侧 β 仅作**常量占位（rank-neutral）**，绝不读缓存/聚类去「替代」回测里的打分或选股。

**预判四扇窗（全部 as-of 当时可见数据，禁止未来函数）**：
- **窗1 相对强度 RPS**：个股 hfq 收益 − 中证全指 hfq 收益，20/60/120日；RPS_60>0 且 RPS_120>0 = β上行（个股维度）。
- **窗1b 行业 RPS 确认**（行业维度）：细分行业指数相对宽基 20/60/120日超额，作行业加热确认；分析师凭行业认知归属细分行业即可（申万标签不可得时不影响定性判分）。
- **窗2 供需周期位置**（量价矩阵量端 · 国金一凌产能周期）：核心品价格同比/环比、Capex增速、产能利用率、库存四阶段、订单/出货量同比由负转正（D1代理=β启动）。
- **窗3 景气拐点**（行业版L4a）：行业一致预期净利润 上修家数−下修家数>0、成分股业绩预告向好比例。
- **窗4 资金β**（确认项非驱动）：北向/公募行业持仓环比、行业ETF净申购、融资余额行业分布——只作确认。

**0~16 分档**：

| 档 | 触发条件 | 分 | 对应量价矩阵 |
|:--|:--|:--:|:--|
| 强β | 窗2 周期上行 + 窗3 景气向上，且(窗1 RPS双正 或 窗4 资金流入确认) | 10~16 | 量价齐升·景气向上 |
| 中β | 窗2/窗3 仅一项满足，或周期中性但景气边际改善，或仅窗1价格强而基本面未确认 | 6~9 | 量或价单强 |
| 弱β | 窗2 周期下行 / 窗3 景气下修 / 无顺风；即便窗1短期强(题材脉冲)也不给高分 | 0~5 | 量价走弱/题材脉冲 |

**评分**：由分析师按四扇窗综合定档（基本面顺风主导，价格/资金仅确认），**不自动合成**；**护栏V**：个股120日裸收益>80% → 封顶「泡沫观察」不追。

**护栏（借框架铁律）**：① 速度型尖顶V（个股单季涨>80%）→ β封顶「泡沫观察」不追；② 反身性R（价格高位但PB/PE低位=利润峰值）→ 禁裸发强β；③ 题材A8（纯预期无基本面）→ 强β分降级（铁律⑱）。

> 调用约定：β 由分析师按本§⑦ 四扇窗逐票定性评定(0~16)后填入盈利面「细分行业β」16分槽；回测引擎侧仅作**常量占位**，**不自动查缓存替代**。β **只影响盈利面→W→仓位**，不参与 EV 赔率（EV 仍按 PB/PE 双锚）。

## 使用示例

### 例1：分析单个板块

```
用户: "@XYD-supply-demand-sector 分析证券板块当前处于什么位置"

→ 执行五步决策树：
   Step1 需求：资本市场改革 → 结构性增长 ✅
   Step2 量价：成交3万亿+79% + ROE9.2%创新高 → 量升价升 ⭐⭐⭐⭐⭐
   Step3 PB：1.357x/5.55%分位(5年) → 双重低估 ⭐⭐⭐⭐⭐
   Step4 催化：中报头部券商+50%+
   → 🟢 黄金买入区，仓位30%
```

### 例2：批量筛选ETF

```
用户: "@XYD-supply-demand-sector 全市场ETF中哪些在黄金买入区"

→ 对每个ETF：查其跟踪指数的PB + 判断板块量价组合 + 筛选PB≤10
→ 输出全部满足条件的ETF排名
```

### 例3：量价前瞻判断

```
用户: "@XYD-supply-demand-sector 最近哪些行业量/价有上调预期"

→ 查PMI新订单指数 + 卖�的排产/涨价研报 + 期货远期曲线
→ 用前瞻矩阵匹配策略动作
```

## 关联框架

- `个股盈利面框架` — 个股盈利面四层扫描（选股层，独立框架）
- `XYD-technical-analysis` — 个股技术面三层打分（择时层）
- `XYD-investment-system` — 投资总框架（调用总入口）

## 参考数据源

- **同花顺 iFinD MCP（`ifind-mcp`，2026-09-08 新增·重要源）**：`get_security_indicators` 为最核心指标查询工具（A股/基金/债券/指数/港股/美股/期货期权/板块），`get_stock_summary` 速览（≤5 只）、`search_securities` 智能选股/选基、`search_edb`/`fetch_edb` 宏观与行业经济指标、`search_news`/`search_notice` 资讯公告。
  - **一致预期首选源**：口径自带 **FY 年度标注**（根治妙想 `consensus_np_median` periods 只给数据日期、FY 归属未标注的老问题）。可直取 `预测归母净利润平均值`（`年度=2026`+`综合值周期=30天`）、`预测净利润/每股收益/营业收入(FY1~FY3)`、`预测市盈率（PE，FY1）`、**`评级机构家数`**、`预测净资产收益率(FY1~FY3)` 等 60+ 前瞻指标 → 直接供 **D4 景气预期边际（一致预期盈利上修占比）** 与窗3 景气拐点（上修家数−下修家数）使用。
  - 调用式：`get_security_indicators(market="A股"|"板块"|"指数", query="...")`；**单次 ≤5 个主体、≤5 个指标**，超量返回"数据过大"被截断，多票分批。
  - 边界：仅输出**日频**指标，不支持高频实时行情 / 证券筛选 / 非指标类（公告走 `search_notice`）。
  - 已有落地脚本 `ifind_fix_pb.py`：用 iFinD 真实观测点替换不可靠的日频 PB 插值重建值（2026-08-15 立）。
- mx-data / mx-search：东方财富妙想API（PB分位、行业利润增速）
- 通达信MCP：实时行情、K线、指数PB
- query-indicator：华泰金融数据
- WebSearch：卖方行业研报、涨价排产信息、宏观数据

> **降级链**：一致预期 iFinD → 妙想(mx-ds-mcp) → westock `data_consensus`；估值/行情 iFinD/妙想 → 通达信 → query-indicator。妙想 mkapi2 走代理**会间歇 502/网络异常**（表现"字段 0 个"而非真缺数，`error_kind=PROXY_OR_NETWORK`）→ 直接切 iFinD 或 westock，勿误判为标的问题。

---

## 配套代码模块（纪律层已代码化 · 2026-08-14）

| 文件 | 作用 |
|:---|:---|
| `discipline.py` | 纪律层唯一权威引擎（胜率/赔率/象限/仓位/否决/EV全由它产出；2026-08-15 锚感知：PB/PE/BLEND 分发；2026-08-15 §十二 五维量化胜率接入；2026-08-15 §EV 三档价+EV中乐尾部参照盈利面；2026-08-16 O2/O3 EV 基本面相位叠加+下行地板、O4 对称泡沫硬否决信号#7、O8 EV 持有期/年化、O9 制度周期断裂检测、O10 组合层 conviction cap） |
| `quant_winrate.py` | **五维量化胜率模块（§十二 · 2026-08-15 新增）**：把胜率从「六档查表」升级为「五维客观打分→确定性综合分 S→历史校准胜率 g(S)」，彻底杜绝 LLM 主观拍定。各维分数由固定阈值表算（非 AI 临时选），行业适配代理用固定映射表 `INDUSTRY_PROXY`；`python quant_winrate.py --selfcheck`，被 `discipline.py._win_rate` 调用（有量化指标输入走五维、无则回退六档表） |
| `sector_pstar.py` | **风险调整买点 P\*（§P\* · 2026-09-08 新增）**：`P*=[W·mid+(1−W)·bear_eff]/(1+r)`，r=r_base(W)×s_vol(σ_eff) 夹[3%,60%]，σ_eff=σ_ann·√(H/12)、s_vol=clamp(σ_eff/30%,0.25,2.0)；止损+摩擦走 bear_eff 不进 r（避免与 σ 重复计入）；σ 解析链 显式>序列派生(√252)>资产类别默认>缺(→s_vol=1.0 零回归)；胜率饱和 soft(softplus k=0.02)/hard 双通道；**只读不参与仓位与回测**。`python sector_pstar.py --selfcheck`（20 项：等价性216组/边界退化/单调性/幂等） |
| `quant_inputs.py` | **量化输入契约（O1 · 2026-08-16 新增）**：把 D1–D5 八字段从「手填/LLM 判」改为「从 `quant_raw` 原始序列确定性派生」。`derive_quant_inputs()` 按固定公式算 8 数（价格动量/分位由 price_series 算，量/库/利用率/资金/盈利修正由带 provenance 的客观指标透传），缺失→显式 None；`enforce_quant_contract()` 在 `discipline.run()` 开头钩入，派生并覆盖 q_xxx、无 quant_raw 却手填→打 `[V1]` 告警。关闭 V1 静默主观漏洞。`python quant_inputs.py --selfcheck` |
| `params.json` | 纪律层唯一参数源（SHA256 指纹冻结，改须 bump version；v1.2.0 新增 `valuation_blend` 权重） |
| `params.lock` | 参数冻结锁（指纹一致性校验，防事后调参） |
| `guards.py` | 量化防错守卫（未来函数/过拟合/幸存者偏差/多重比较/零成本/口径错配） |
| `valuation_anchor.py` | **估值主锚分类（2026-08-15 新增）**：规则+精选表把行业映射为 PB/PE/BLEND，返回权威分类（PE表→BLEND表→PB表 优先级）；`python valuation_anchor.py --selfcheck` 验证（含 O6 BLEND 权重自检），并被 `gate_final`/红队 I 组 引用。**O6 BLEND 自适应权重（2026-08-16）**：`BLEND_WEIGHTS` 按行业细分 PE/PB 权重（重资产成长 PB 偏高 0.55~0.65、轻资产消费 PE 偏高 0.55~0.65、制造中性 0.50/0.50），`blend_weights_for()` 命中专用权重、否则回退全局默认；被 `discipline._blend_weights` 调用替代全局 0.6/0.4，关 V6 |
| `gate_dual_source.py` | **数据双源验证 Gate（三道闸之② · 2026-08-15 新增）**：按活跃主锚校验 PB/PE/BLEND 双源交叉(≤5%)+手算兜底(≤20%)，BLEND 须双锚同时达标；`python gate_dual_source.py --selfcheck`，被 `discipline.py` 在 `run()` 内调用 |
| `gate_final.py` | **finalgate 终检（三道闸之③ · 2026-08-15 新增）**：端到端一致性（声明锚==使用锚、估值数据齐备、有效分位∈[0,100]、五档单调、双源闸过、checklist 全 True、状态自洽）；`python gate_final.py --selfcheck`，被 `discipline.py` 在 `run()` 内调用 |
| `redteam_sector.py` | 74 个红队对抗用例（坏输入能不能骗过框架；A–L 十二组，含 G 行业周期量价/H 降档通道/I 估值主锚分类/J 双价格方向门/K 前瞻证据链/L LLM轨机械复核） |
| `stress_test.py` | 压力检验（单调性/敏感性/2000组蒙特卡洛/幂等/指纹不变量） |
| `backtest.py` | 简洁回测骨架（point-in-time / t+1 / 成本 / 指纹；待 ETF 名单到位后填真实特征） |
| `calibrate_winrate.py` | **g(S) 校准骨架（O5 · 2026-08-16 新增）**：用 54 ETF 历史信号→持有期胜率回归拟合 `win_map`，替换 `quant_winrate` 的占位 g(S)。定义 `backtest_features.json` 数据契约（S + point-in-time win），提供 empirical 分桶 / logistic 两种拟合 + 校验（分桶校准误差+AUC+单调性），`--demo` 合成数据端到端自证；待 54 ETF 回测出数后 `--input` 产出 `proposed_win_map.json`（落地须人工审 + bump version + 更新指纹锁）。`python calibrate_winrate.py --selfcheck` |
| `run_all.py` | 一键运营自检闸门（串联：EtfPreflight→指纹锁→模块自检→红队→压力→回测） |
| `preflight_etf.py` | **🔴 运行前体检（2026-10-07 新增·发布闸第一步，日常也必须第一步跑）**：秒级·不联网·零积分，一次性检出 ①依赖 pandas ②fetch_one 签名含 metric ③CLI 参数契约 ④缓存 as_of 诚实性(必须==序列末值) ⑤硬编码结论字面量(AST 取真实常量·跳过 docstring/注释) ⑥SKILL 统计块 ⑦warnings 落盘契约 ⑧FILLS 含锁定字段。exit=0 可直跑 / exit=3 **禁止直跑**（先修完）<br>**立此脚本的依据**：两只 ETF 纯机器链路实测 3005ms，端到端却约 1h（放大 ~1200×）；当天撞到的 9 堵墙 **9/9 均可在运行前静态检出**，却全部靠"跑到一半才炸"暴露——每堵墙=1 轮完整往返。用法 `python preflight_etf.py [--codes 159755,515790] [--json]` |
| `PITFALLS_SDS.md` | **🔴 踩坑登记（preflight 的弹药库）**：撞墙必登记，且每条必须带 `@preflight:` 标签指向**真实存在**的检查项。preflight 的「坑登记覆盖度」项会校验标签是否兑现——**登记了坑却没有对应检查 = 未闭环 = MUST FAIL**。目的是防止新坑石沉大海、下个会话重撞一遍 |

### 🔴 防 LLM 反复撞墙的三层保障（2026-10-07 立）
1. **硬闸化（不是文档劝导）**：`analyze_etf.py` 与 `run_sector_pipeline.py` **两个入口都在代码路径上嵌入** `preflight_etf.enforce()`。
   无 `--skip-preflight` 逃生门。MUST → **exit 3 阻断**。体检**自身**异常才 fail-open（工具坏了不能反成阻断源）。
   防重入靠环境变量 `_XYD_PREFLIGHT_DONE=1`，其**读写只能在 `enforce()` 内各一次**——曾在包装函数里先验先置，
   导致 `enforce` 首行就 return，硬闸变成 100% 空转的死闸（红队注入测试抓出）。
2. **闭环化**：`PITFALLS_SDS.md` 登记坑 → 必须映射到真实检查项 → preflight 校验兑现。
   新坑若无可映射检查 = preflight 有盲区，**先补 `check_*` 再收工**。
3. **自验证**：红队注入测试证明 5/5 拦得住（含"绕过入口直跑 pipeline"）；闭环校验器自身也经 2/2 验证。 |
| `parse_mx_pb_daily.py` | **5 年日频 PB 五档（2026-08-15 新增）**：解析 mx-ds-mcp 落盘的日频 PB 宽表 → `data/index_daily_pb/{CODE}.json`（pb 全序列 + p5/p20/p50/p80/p95 + now + 当前分位）；`python parse_mx_pb_daily.py` 全量重解析 |
| `rebuild_pb_daily.py` | **日频 PB 兜底重建（2026-08-15 新增·2026-08-15 晚废弃插值）**：mx 配额尽时用 westock 日K点位 + 本地月频 PB 反推 BVPS 重建日频 PB。**⚠️ 2026-08-15 晚用户裁定：BVPS 线性插值重建不可靠（930632 偏差+58%/931994 −15.5%），禁止再用插值**——偏差大的已改用 `ifind_fix_pb.py` 真实值替换，本脚本仅保留点位采集用途 |
| `ifind_fix_pb.py` | **iFinD 真实 PB 替换/补齐（2026-08-15 新增）**：用同花顺 iFinD `index_data` 返回的【真实观测点】（近 19 天日频 + 月度采样，数据源直给非插值）替换不可靠重建值、补齐污染/缺口指数；`python ifind_fix_pb.py --all` 处理预设 8 只 |
| `build_daily.py` | **pb 污染 bug 已修（2026-08-15）**：① 退化分支逐行判定指标（原整 sheet 套同一 metric → 收盘点位被误写进 pb 字段，致 6 只指数污染）；② 落盘加 PB 合理性守卫（0.05~40，越界拒写） |
| `data/index_daily_pb/` | **54 只 ETF 跟踪指数 5 年日频 PB 五档（2026-08-15 建·晚修）**：mx-ds-mcp 直拉 28（权威）→ ifind 真实值替换/补齐 9（930632/931994/H30199/H30269/931790/980092/H30202/HSHYLV.HK/930722）→ westock 点位重建 9（核对偏差<15% 保留）→ raw/mx 转正/兜底 4。**覆盖 50/53**；3 个真无 PB 指标（AU9999 黄金现货/N225 日经/NDXTMC 纳指科技）。**etf_index_map 修正 3 处映射错误**：513310→931790.CSI（中韩半导体，原 H30199 实为电力）、159611→H30199.CSI（电力，原 H30269 实为红利低波）、512890→H30269.CSI（红利低波，原 930955 实为红利低波100）、513950→HSHYLV.HK。汇总见 `data/meta/pb_daily_bands.json`。**口径：5 年日频（2021-08-16 起），重建/补齐指数为 iFinD 真实采样点（近19日频+月度）** |
| `gate_industry_cycle.py` | **行业周期量价 Gate（三道闸之① · 2026-08-15 新增）**：硬校验所有量价分析必须建立在 ETF 跟踪指数的行业供需周期口径（vp_basis=industry_cycle + 合法行业量/价指标），拦截 ETF 换手率/指数点位/大盘 等错口径；被 `discipline.py` 在输入契约处调用拒绝错口径裁决 |
| `dual_price_analyzer.py` | **双价格维度量价分析（2026-08-16 新增 · 2026-08-17 挂入 discipline 输入契约）**：区分「出厂价/官方定价/NAV」与「市场实际成交价/批价/市价」两个价格维度，分别算量价趋势与共振/背离，交叉校验「出厂价↑+市场价↓→价格传导失效（严禁误判为量缩价升）」；`analyze_dual_price(data)` 接收任意个股/ETF 历史序列（含 column_map 重命名）输出结构化 JSON。**2026-08-17 已挂入 `discipline.py` 输入契约**：`SectorInput.dual_price_data` 字段提供后，引擎在消费 `volume`/`price` 前先跑该分析，出厂价↑+市场价↓ 时**自动把框架量价重写为市场成交价侧并打「价格传导失效」告警**，结果挂 `Verdict.dual_price_check` 且 §十三自检新增 `⑪双价格传导校验`。`python dual_price_analyzer.py --selfcheck` 内置茅台/ETF demo 自校验 |
| `analyze_etf.py` | **单只 ETF 固定编排入口（2026-09-07 审议 P0-②）**：pipeline 草稿→FILLS 完整性预检（缺判断字段 exit4；改 Python 锁定字段 exit4）→合并（剥 `_`键+锁定保护+`_fill_meta` provenance）→llm_fill_guard（MUST→exit6）→引擎→`runs/<date>/verdict_<tag>.json`。**禁止会话即兴编排脚本**；产物一律写 `runs/<tag>/`，历史产物在 `archive/` |
| `make_stats.py` / `selfcheck_all.py` | **文档统计自动化+自检聚合（2026-09-07 审议 P0-③/P1-⑤）**：make_stats 生成 SKILL.md 的 AUTO:STATS 块（红队用例数/ETF 映射数/PB·PE 文件数），`--check` 断言漂移（exit1）；selfcheck_all 聚合七模块 --selfcheck 并挂入 run_all 第二步 |
| `gen_etf_card.py` | **ETF 决策卡片生成（胜率+EV三档+五档目标价+§三bis 侧别，锚感知）**：159755 BLEND 重算版/515790 PB 版；数据源 `runs/2026-09-07/`；输出 viz_card_*.html（旧版·带 PENDING 脚注，保留作对照） |
| `render_verdict_card.py` | **★终裁可视化卡片生成器（2026-09-08 用户指定保留模板）**：读任意 `verdict_*.json` → 渲染「黄金买入区」风格自包含 HTML 卡片（浅色主题/涨红跌绿/纯内联CSS无外部依赖）。覆盖估值锚+双源校验、胜率环、EV三档、P*击球线、仓位/置信/侧别、硬信号/前瞻/告警。CLI：`python render_verdict_card.py -v verdict.json [-o out.html]`（可批量多份）。默认输出 `<verdict>_card.html`。**作为框架标准可视化输出模板之一** |
| `fetch_pb_mxdata.py`（--metric pb\|pe） | **双序列刷新器（2026-09-07 P1-⑥ 扩）**：`--metric pe` 拉 5 年日频 PE(TTM) 落 `data/index_daily_pe/{CODE}.json`（同 PB 结构，series 键仍为 "pb"+metric 字段；不入 bands registry）；PB 模式照旧同步 pb_daily_bands.json。⚠️2026-10-07 修：`fetch_one` 缺 metric 形参致刷新器 100% 崩溃（stale 无法自愈的根因）；as_of 改取真实序列末值（防虚报新鲜） |
| `ingest_ifind_valuation.py` | **iFinD 增量合并（2026-10-07·iFinD 升级为妙想同级别估值源）**：妙想刷新失败时，Agent 用 iFinD MCP `get_security_indicators(market=指数, query="<指数名>(<代码>) <缺口起>至<end> 每个交易日的市净率PB和市盈率PE(TTM)")`（一次调用同返 PB+PE 两列）→ 表格存文件 → `--table <md> --code <技能侧代码> `落盘。**只补缺口**：同日冲突保留原源并告警；缺口值按**重叠期中位比值**归一到原源口径（实测 PB 两源口径差 8.8~8.9%，PE 一致）；重叠<3 点 fail-closed 拒拼接；source 幂等标 `+ifind_incr(_calib)`。**节约纪律：妙想优先，iFinD 只补缺口窗口（几十点），不做每次强制双拉、不重拉全 5 年** |
| `data/index_daily_pe/` | **PE 日频序列库（2026-09-07 建）**：与 index_daily_pb 同构；BLEND/PE 锚 ETF 估值补数必备；新鲜度监控走 `data_refresh.py --status` |

*纪律层代码化由用户指令"将这套纪律层直接以代码形式同步写入技能模块，确保规则在技能执行时自动生效"落地；红队 57/57、压力硬不变量全过、回测管线贯通、参数指纹锁一致（v1.3.0）。*

---

## §十二 五维加权量化胜率（2026-08-15 用户裁定：胜率须由客观指标确定性算出，禁止 LLM 主观拍定）

### §12.0 设计原则
旧版胜率 = 「量价六档查表 + 白名单修正」是机械映射，但仍靠人工定档。新版改为：
**五维客观打分 → 确定性综合分 S → 历史校准胜率 g(S)**。整条链路零自由裁量：
任意相同输入永远得到相同 S、W，且每个分都可追溯到原始指标。

### §12.1 五维与权重（严格按用户给定）
| 维度 | 权重 | 客观指标（行业周期口径） | 分数来源 |
| --- | --- | --- | --- |
| D1 行业价格强度 | 42% | 行业核心品价格近6月涨跌幅% + 历史分位(0-100) | 动量0.6 + 分位0.4，固定阈值表 |
| D2 产业供需量能 | 27% | 产销量/出货/装机同比% + 库存同比%(负=去库利好) + 产能利用率% | 量0.45 + 库存0.30 + 利用率0.25 |
| D3 市场资金共识 | 13% | ETF 资金净流入近N月% + 成交额趋势%(正=放量) | 各 0.5，固定阈值表 |
| D4 景气预期边际 | 10% | 一致预期盈利上修占比% | 直接入分（0-100） |
| D5 趋势结构质量 | 8% | 均线多头 + 突破有效 − 量价背离 | 由价格序列自动算或显式给分 |

综合分 `S = Σ wᵢ·Dᵢ`（确定性线性组合）；胜率 `W = g(S)`，由 `params.json[win_quant.win_map]` 分段映射（当前占位：地板20% / 天花板80%，**待 54 ETF 历史回测校准**）。

**g(S) 形态（2026-09-08 凸映射扩展）**：`win_map_mode` = `convex`（默认，**段内凸** t^γ，γ=1.5）/ `linear`（旧分段线性，逐位一致）。
- 段内凸：段内取值 `y0+(y1−y0)·t^γ`，γ>1 时曲线位于弦**下方** → 除锚点外胜率一律 ≤ 线性值（更保守、不虚高）；锚点处 Δ=0；单调不减。
- ⚠️ **数学硬约束**：本锚点集各段弦斜率 0.60→0.867→0.80→0.667→0.667 非单调（S 型），「过锚点」与「全局凸」不可兼得——框架的"凸"= **段内凸**，节点处斜率由 γΔ/Δx 突降到 0，是**凹折点**。
- 实测：convex vs linear 最大偏差 **−2.22pp**（保守侧），经 `shrink=0.5` 后约 −1.1pp；`linear` 通道保留（`win_map_mode` 可切，逐位等于旧值，零回归）。

### §12.2 行业适配代理（规则化，非 AI 临时选）
不同行业用不同「量能代理指标集」喂给同一个 D2 公式，避免用错代理误判：
- **周期品**（煤炭/化工/有色/钢铁/石油/航运/工程机械）：重点看库存、开工率
- **科技**（半导体/电池/光伏/消费电子/通信设备）：重点看订单、出货量
- **消费**（白酒/食品/家电/畜牧/零售）：重点看终端动销、社零
- **金融/医药/成长**：看业务量/保费/院端量等（见 `quant_winrate.INDUSTRY_PROXY` 全表）

### §12.3 接入与向后兼容
`discipline.py._win_rate`：
- **输入含五维量化核心指标（D1~D4 八字段）** → 走 `compute_quant()` 作基础胜率；
- **未提供** → 回退旧「六档表」逻辑（54 ETF 回测/红队用例零回归，已验证）；
- **cost_deflation 降档通道**仍生效：五维算出 W 后约束到 [25%, 40%] 封顶，旧纪律不动；
- 白名单修正（前瞻/想象力/PB分位/拥挤/催化/价值陷阱）继续叠加于量化基础之上，全局 clamp 兜底。
- **O1 量化输入契约（2026-08-16 落地）**：八字段**须由 `quant_raw` 原始序列确定性派生**，引擎 `run()` 开头 `enforce_quant_contract()` 自动派生并覆盖 q_xxx（trace 可溯）；**无 quant_raw 却手填 q_xxx → 打 `[V1]` 主观告警**（诚实标注，不阻断），彻底关闭"胜率量化只保证聚合客观、上游仍可主观"的漏洞。详见 `quant_inputs.py`。

### §12.4 诚实局限（必读）
1. **g(S) 初值是占位**：地板20%/天花板80% 仅防虚高，真实映射须用 54 ETF 历史信号→持有期胜率回归拟合后替换（改动须 bump version）。
2. **D5 缺序列则退化为中性60**：若不提供价格序列/显式 D5 分，结构质量维(8%)不区分标的——真正部署须拉每只 ETF 的价格序列。
3. **白名单修正与 D3/D4 存在轻微概念重叠**（资金/预期既在五维内、又在白名单修正里），目前靠 clamp 约束，校准阶段可评估是否去重。
4. 模块只校验**字段一致性**不校验**数值真伪**：喂错数据（如把 ETF 换手率当行业量）会被 `gate_industry_cycle` 拦截，但喂"看起来合理但实为估算"的数（如 PE 占位）不会。

---

## §EV 三档价 + EV 中/乐/尾部（2026-08-15 用户裁定：参照个股盈利面框架）

### §EV.0 设计意图
板块层的「赔率」本质是**估值回归的空间**。给定估值主锚的 p5/p20/p50/p80/p95 分位带，在**基本面(BVPS/EPS)不变**假设下，估值回归到某分位带即对应一个目标价：

> **目标价 = 现价 × (分位带水平 / 当前锚值)**

由此可算出三档价与对应上行/下行空间，再用与个股盈利面技能**完全相同的 EV 公式**复算期望收益，使板块层结论与个股层同构、可横向比较。

### §EV.1 三档价映射（按主锚）

| 档位 | 锚分位带 | 含义 | 价格公式（base=现价，缺省=相对100） |
|:---|:---|:---|:---|
| 悲观价 | P20 | 估值回到低 20% 分位 | base × P20/now |
| 中性价 | P50 | 估值回到中位 | base × P50/now |
| 乐观价 | P80 | 估值回到高 20% 分位 | base × P80/now |
| 极端悲观价 | P5 | 估值回到最惨区 | base × P5/now |

- PB 主锚 → 用 PB 分位带；PE 主锚 → 用 PE 分位带；BLEND → 用 `eff=0.6×PE+0.4×PB` 的动态带（与 §估值主锚一致）。
- `price_now` 提供则输出**绝对价（元）**，否则输出**相对现价=100** 的目标档位（无需绝对价数据即可看三档位置）。

### §EV.2 EV 公式（与盈利面技能逐字一致）

```
u_mid%  = (中性价−现价)/现价 = space_pct[P50]      # 中性上行空间
u_opt%  = (乐观价−现价)/现价 = space_pct[P80]      # 乐观上行空间
d_bear% = max(0, (现价−悲观价)/现价)                # 悲观下行损失，恒≥0（无下行传 0）
d_ext%  = max(0, (现价−极端悲观价)/现价)            # 极端下行损失，恒≥0

EV中  = W × u_mid − (1−W) × d_bear      # 中性情形期望收益
EV乐  = W × u_opt − (1−W) × d_bear      # 乐观情形期望收益
EV尾  = W × u_mid − (1−W) × d_ext       # 极端下行情形期望收益（深止损正确压低 EV）
```

- `W` = 引擎胜率（五维量化或六档表，已含降档通道/白名单修正）。
- **三档单调保底**：乐观≥中性（u_opt≥u_mid）；若倒挂（u_opt<u_mid）则「乐观取中性保底」（2026-08-06 裁，与盈利面一致），避免发布倒挂三档，**且分位带本身单调（P80≥P50）→ 实际不会倒挂**，保底仅作防御。
- 框架否决（需求塌方/价跌硬否决/左侧通道）→ 胜率不可用 → **EV 不输出**（disclosure 说明）。

### §EV.2.1 O2/O3 基本面相位叠加 + 下行地板（2026-08-16 用户裁定 · 关 V2/V3）
原始 EV 假设「基本面(BVPS/EPS)不变」，与供需周期框架自相矛盾，且分位带无法表征跌破 p5 的基本面恶化（价值陷阱盲区）。故在 `discipline.py._ev` 内，**仅当基本面恶化时**叠加两层修正（参数见 `params.json.ev_fundamental_overlay`，保守上限、待 54 ETF 回测校准）：
- **O3 下行地板**：`d_bear = max(统计下行, 基本面地板)`。地板仅在恶化时生效：需求塌方→`collapse_downside_floor_pp`(30)；盈利下修→`clamp(|盈利修正%|×beta(0.5), 10, 40)`；仅前瞻↓→10；需求侵蚀→25。深度低估且基本面恶化时，EV 不再被 silent 记为 0 下行。
- **O2 上行折价**：盈利下行相位下 BVPS/EPS 将缩水、p50/p80 绝对价失真 → 对 u_mid/u_opt 做折价 `haircut`：盈利下修→`clamp(|盈利修正%|/100×beta, 0, 0.6)`；前瞻↓→0.30；需求侵蚀→0.15。盈利上修/平稳不折价。
- 输出新增 `fundamental_phase`（盈利平稳/盈利上修/盈利下修/前瞻下行/需求侵蚀/需求塌方）、`downside_floor_pct`、`upside_haircut_pct` 三字段，disclosure 同步披露。

### §EV.2.2 O8 EV 持有期/年化（2026-08-16 用户裁定 · 关 V7）
原始 EV 为 horizon-agnostic 点估计（回归至 p50 的百分比），3 个月与 3 年回归 EV 数值相同、跨板块不可比。故 `_ev` 新增**显式持有期 + 年化口径**（参数见 `params.json.ev_horizon`）：
- `horizon_months`：默认 `default_horizon_months=24`（≈供需周期半程），可由输入 `ev_horizon_months` 覆盖。
- 年化 = `(1 + ev/100)^(12 / horizon_months) − 1`，输出 `ev_mid_annualized_pct` / `ev_opt_annualized_pct` / `ev_tail_annualized_pct`。
- disclosure 显式标注「年化仅作跨板块可比口径，非收益保证」。
- `render_text` 新增 `[EV·O8] 持有期=24月 年化: 中/乐/尾（仅可比口径）` 摘要行。

### §EV.2.3 O9 制度周期断裂检测（2026-08-16 用户裁定 · 关 V12）
5y 窗口须含 ≥1 完整牛熊周期；否则应切 10y 口径。引擎新增 `_regime_check`（仅 SHOULD 级、不阻断）：
- 上游标记 `regime_break=True`（政策/指数重构/并购等结构性断裂）→ 切 10y；
- 或 `pb_pct_period="5y"` 且 `pb_series_years < regime_full_cycle_min_years`（默认 4 年）→ 窗口未含完整周期，切 10y。
- 10y 分位可用 → 有效分位(`_eff_pct`)切换至 10y + `[O9]` SHOULD 告警；10y 缺失 → 维持 5y 并降级告警（结论为该窗口条件下，可靠性降低）。
- 注：EV 三档价仍基于 5y 分位带（10y 完整带通常未提供），disclosure 会额外提示「跨周期结论以 10y 有效分位为主」。

### §EV.3 输出字段
`Verdict.ev`：`anchor` / `tiered_prices{base,bear,mid,opt,ext,unit}` / `u_mid_pct` / `u_opt_pct` / `d_bear_pct` / `d_ext_pct` / `ev_mid_pct` / `ev_opt_pct` / `ev_tail_pct` / `horizon_months` / `ev_mid_annualized_pct` / `ev_opt_annualized_pct` / `ev_tail_annualized_pct` / `fundamental_phase` / `downside_floor_pct` / `upside_haircut_pct` / `disclosure`。`render_text` 已带 `[EV]` / `[EV·O8]` / `[EV·O2/O3]` 摘要行。

### §十/§O10 组合层 conviction cap（2026-08-16 用户裁定 · 关 V9）
胜率(W)与仓位(pos)是**相互独立的两套杠杆**：W 影响 EV，pos 由「估值分位×想象力×量价×前瞻」决定，二者不相乘。故引入**风险预算** `conviction_score = pos × W/100`（跨板块可比的加权暴露度），写入 `Verdict.conviction_score`；`position` 新增 `conviction_cap=0.60`（组合层总上限）、`single_high_conviction_threshold=0.30`。当单板块 pos ≥ 30% → 发 `[O10]` SHOULD 提醒「跨板块 Σconviction 须 ≤ 60%」，防多板块高胜率高仓位叠加超配。`render_text` 新增 `[O10] 风险预算 conviction=…` 行。

### §EV.4 端到端示例（证券样例·PB 主锚）
现价锚 PB=1.36，分位带 P20=1.08 / P50=1.50 / P80=1.89 / P5=0.95，W=90%：
- 三档价(相对100)：悲观 79.41 / 中性 110.29 / 乐观 138.97 / 极端 69.85
- u_mid=+10.29%  u_opt=+38.97%  d_bear=20.59%  d_ext=30.15%
- **EV中=+7.2%  EV乐=+33.0%  EV尾=+6.3%**

> 注：本例 PB 分位偏低(27.5%)、但下行带宽（P5 比现在低 30%），故 EV尾被极端下行压低——与盈利面 EV 的「深止损压低 EV」机制完全一致，属正确行为。

---

## §P\* 风险调整买点（2026-09-08 · 与盈利面 `p_star.py` 公式同源、参数独立）

### §P\*.1 为什么是 P\* 而不是「给目标价打折」
低胜率票不能靠把三档价乘系数下调表达风险——EV 里 W 已经计过一次概率，再打折等于计两次，且会破坏三档价与分位带的同源性（终检会 FAIL）。正确做法：**锚不动，用要求回报率 r 反解「最高可付价格」**。

### §P\*.2 公式（模块 `sector_pstar.py`，参数段 `params.json[p_star]`）
```
P* = [W·mid + (1−W)·bear_eff] / (1 + r)
r      = clamp(r_base(W) × s_vol, r_min=3%, r_max=60%)
s_vol  = clamp(σ_eff / σ_ref, 0.25, 2.0)
σ_eff  = σ_ann · √(H / 12)          # H = 持有期(月)，缺省取 EV 持有期(O8)
bear_eff = max(bear, P·(1 − stop_pct − friction_pct))
```
- `r_base(W)`：W≥50→10%｜40~50→15%｜25~40→25%｜<25→40%（与盈利面同构）。
- **σ 越大 → r 越大 → P\* 越低**（高波动要求更高补偿）；ETF/宽基 σ 小 → r 自动更小（用户要求）。
- **止损/摩擦走 `bear_eff`，不进 r**：r 已由 σ 表达风险，止损再进 r 等于同一份下行风险计两次。
- **σ 解析链**：`p_star_sigma_ann` → 由 `quant_raw.price_series`/`q_prices` 派生(√252) → 资产类别默认表（etf .18 / broad .15 / sector .28 / theme .32 / commodity .25 / bond .05 / money .01）→ 缺失则 `s_vol=1.0`（**零回归**，r 不缩放）。

### §P\*.3 性质与边界（自检 20 项全绿，`sector_pstar.py --selfcheck`）
- **等价性**：`P* > 现价 ⟺ EV_eff > r`（216 组数值验证）→ P\* 是既有「要求回报率」判据的**价格化重述，不是新规则**，不与回测/交易逻辑冲突。
- **只读披露**：`Verdict.p_star` 不参与仓位（`_position`）、回测与硬否决；`win=None`（硬否决/左侧通道）或无 EV → `p_star=None`。
- **胜率饱和**：默认 `soft`（softplus，k=0.02），值域 `[w_floor,w_cap]=[0.01,0.99]`；常规区间（5~90%）偏移 <0.1pp，仅极端 W 平滑压缩；`hard` 通道保留（`sat_mode` 可切）。
- **边界退化**：σ=0 → s_vol 触下限 0.25，r 由 `r_min=3%` 兜底；低 W+极端 σ → r 触上限 60%；W 越界(−20/180)先硬夹再饱和、不崩；现价=0 → 只出 P\*、不产生除零结论；mid/bear 缺失 → 返回 None（不臆造）。
- **单调性**：W↑→P\*↑；σ↑→P\*↓；mid↑→P\*↑（自检逐点断言）。

### §P\*.4 与盈利面技能的关系
同一份公式、两套参数（个股 vs 板块/ETF 波动基准不同）：盈利面在另一独立框架的同名 `p_star.py`（σ 分支 2026-09-08 同步补上，σ 缺失→`s_vol=1.0` 零回归）；本框架在 `sector_pstar.py`。**改一处不影响另一处**，但公式若变更须两侧同步。

---

## 治理现状速览（契约级 · 详细修补史见 `CHANGELOG.md`）

> 本节只保留**仍生效的行为契约**；V1~V13/O1~O10/F1~F4/G1~G3 的落地过程、验证数据与事故复盘全部在 CHANGELOG.md。

- **胜率 g(S) 占位 + 置信治理（生效中）**：五维量化 win_map 为保守占位（floor20/cap80）；proxy 特征 walk-forward OOS AUC≈0.50 无样本外信号 → **硬闸不解除**：`win_quant.shrink=0.5` + `validated=false` → `W_eff=50+(W−50)×0.5`（confidence=LOW），纪律重心在 EV 三档+硬否决+分散。真实 D1-D5 接入且校准 AUC>0.55 方可解除。
- **V10 锚定 p95 泡沫延伸（生效中）**：`_build_bands` 另算「锚定 p95」=剔除近 `odds.anchor_exclusion_months=18` 月的序列 p95；`now/锚定p95 ≥ bubble_warn_ratio(1.3)` → 泡沫延伸警示（SHOULD，收紧 O4 豁免、显著降仓）、`≥ bubble_hard_ratio(1.6)` → 泡沫延伸硬否决（MUST，强制减仓/清仓）。需日频真实序列，否则回退 `p95×0.9`。V11/V13 待定。
- **国证指数数据配方（980032 类）**：妙想返回结构 `table.<指标ID>`（328664=PB、328773=PE）+ `headName` 存日期（`table.items` 恒空）；双源/手算兜底=国证官网 cnindex 成分股权重 + 东财 push2 成分股 PB/PE 加权（与妙想差<2%）；tdx 不覆盖国证指数；**159755→980032.CNI（不是 931719.CSI，那是 159796），勿混**。
- **数据新鲜度与产物纪律**：`data_refresh.py --status`（fresh≤7/warn≤15/stale>15）；O7 闸 >15 天 MUST 重取。会话产物只写 `runs/<tag>/`，历史叙事在 CHANGELOG.md。

---

*基于《供需驱动板块投资框架》转化（2026-07-06）；纪律层代码化 2026-08-14 起，O1~O10/F1~F4/G1~G3 治理项与版本史见 `CHANGELOG.md`；2026-09-07 端到端审议 + P0~P2 治理（未填即错/固定编排入口/文档统计自动化），详见 `技能审议_2026-09-07.md` 与 CHANGELOG。*
