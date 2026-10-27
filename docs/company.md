# 公司事件新库、加减分账本与 Web 评分列实施方案

本文件保存用户确认的设计和验收要求，不维护实施进度。唯一活动台账为[工程实施](3_工程实施.md)，实施阶段273。评分、架构和下载运行合同分别以[评分逻辑](1_评分逻辑.md)、[工程设计](2_工程设计.md)、[下载训练](4_下载训练.md)为权威，行为切换时同步更新。

## 1. 目标与边界

- 新库固定为`data/company/company_facts.sqlite3`。旧`evidence.sqlite3`停用，不导入、不迁移、不兼容读取、不作为失败回退；旧文件留在磁盘，不自动删除。
- 推荐发布表每股一行，保存财务及已核实事件。公告正文、原始证据和断点只供下载及按需解释使用，不进入推荐启动读取范围。
- download负责采集、正文核验、事件提取和原子发布；recommendation负责有效期、过滤、评分、动作与排名；HTTP/Web只展示只读用例结果。
- 不生成`data/doc/`人工审核文档。待核实原因保存在数据库、日志及Web中。
- 加分和扣分显示在现有“评分”列，详情复用股票抽屉，不新增重复分数列。
- 新规则参数是待研究验证的候选，工程正确不代表收益资格；研究及生产启用遵守评分权威第12节。现有V1正式权限、V5工程Shadow、Long不评分及历史不可变合同保持。
- 候选开关为`config/scoring.json`的`company_scoring.enabled`，默认false；公司事件仍下载并核验，硬风险过滤生效，账本明确显示额外加减分未启用。生产启用与真实新库下载不属于本次代码交付的自动部署动作。

## 2. 每股发布表

`published.code`为主键。删除包含全部公告及正文的大`payload`，字段如下：

| 字段 | 类型与空值 | 含义 |
| --- | --- | --- |
| code | TEXT，主键 | 股票代码 |
| financial_report_date | TEXT，可空 | 最新已披露报告期 |
| financial_disclosed_at | TEXT，可空 | 财报披露时间 |
| financial_state | TEXT，必填 | complete/partial/unavailable |
| basic_eps | REAL，可空 | 基本每股收益 |
| book_value_per_share | REAL，可空 | 每股净资产 |
| revenue_growth_pct | REAL，可空 | 营收同比增长率 |
| net_profit_growth_pct | REAL，可空 | 归母净利润同比增长率 |
| core_profit_growth_pct | REAL，可空 | 扣非净利润同比增长率 |
| roe_pct | REAL，可空 | ROE |
| parent_net_profit | REAL，可空 | 归母净利润 |
| core_net_profit | REAL，可空 | 扣非净利润 |
| pledge_ratio_pct | REAL，可空 | 最新质押比例 |
| pledge_as_of | TEXT，可空 | 质押数据日期 |
| pledge_state | TEXT，必填 | complete/partial/unavailable |
| unlock_schedule_json | TEXT，必填 | 解禁日期和比例数组 |
| unlock_checked_at | TEXT，可空 | 解禁检查时间 |
| unlock_coverage_end | TEXT，可空 | 已核验前视范围终点 |
| unlock_state | TEXT，必填 | complete/partial/unavailable |
| risk_events_json | TEXT，必填 | 已核实公告风险数组 |
| positive_events_json | TEXT，必填 | 正面事件数组 |
| review_reasons_json | TEXT，必填 | 待核实原因数组 |
| pending_document_count | INTEGER，非负 | 待核验正文数量 |
| announcement_coverage_start | TEXT，可空 | 公告覆盖起点 |
| announcement_coverage_end | TEXT，可空 | 公告覆盖截止 |
| announcement_state | TEXT，必填 | complete/partial/unavailable |
| checked_at | TEXT，必填 | 实际检查时间 |
| published_at | TEXT，必填 | 本行发布时间 |

百分比使用百分点，0.3表示0.3%。未知数值为NULL，数组为空时为`[]`；空风险数组必须结合覆盖状态解释，不能自动当作无风险。时间戳含Asia/Shanghai时区，纯日期使用YYYY-MM-DD。全局publication保存发布、结构与提取规则身份。分数随推荐结果保存，不在公司库写死。

### 2.1 risk_events_json

仅保存已核实公告事件；财务亏损、质押、解禁由结构化字段派生，不重复写成公告事件。

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| event_id | string | 稳定事件身份，同案进展保持不变 |
| risk_type | enum | 下表风险类型 |
| risk_level | integer | 1–4，不保存0级事件 |
| subject_type | enum | 公司、控股股东、实控人、持股5%以上股东 |
| severity_basis | enum | 正式警示、普通处罚、严重处罚、硬风险事实 |
| case_id | string/null | 可靠官方案号 |
| first_disclosed_at | datetime | 首次正式披露时间 |
| last_disclosed_at | datetime | 最近相关进展时间 |
| effective_at | datetime | 生效时间 |
| status | enum | active/resolved/cancelled |
| resolved_at | datetime/null | 完成、解除或撤销时间 |
| expires_at | datetime/null | 明确失效时间；持续事件可空 |
| summary | string | 简短事实说明，不含正文 |
| evidence_ids | string[] | 初始、最新、解除等必要证据定位 |
| reduction_plan | object/null | 减持计划专用信息 |

| risk_type | 中文 | 等级 |
| --- | --- | --- |
| regulatory_warning | 正式监管警示 | 1 |
| administrative_penalty | 行政处罚 | 普通2，正式认定严重3 |
| disciplinary_action | 纪律处分 | 普通2，正式认定严重3 |
| financial_fraud | 财务造假 | 4 |
| official_investigation | 正式调查 | 4 |
| major_illegal | 重大违法 | 4 |
| fund_occupation | 资金占用 | 4 |
| illegal_guarantee | 违规担保 | 4 |
| forced_delisting | 强制退市 | 4 |
| major_shareholder_reduction | 主要股东减持 | 4 |

reduction_plan固定包含planned_start_date、planned_end_date、planned_ratio_pct、completion_disclosed_at，均允许未知为空。

- 警示、普通及严重处罚、造假或重大违法正式决定：首次正式披露后365天，正式撤销可提前终止。
- 调查、资金占用、违规担保、强制退市等已知持续事项：可靠同案解除前保留。
- 减持：完成、终止或届满后再保留90天；resolved不代表立即无效。
- 普通进展不延长首次披露窗口；无法关联同案的解除公告不能清除旧风险。
- 疑似事件进入待核实原因，不通过降低等级放行。

### 2.2 positive_events_json

首版仅装载回购、重要股东增持和经营合同相关事件，不装载全部普通新闻。

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| event_id | string | 稳定计划或合同身份 |
| event_type | enum | share_repurchase/shareholder_increase/operating_contract |
| subject_type | enum | 公司、控股股东、实控人、持股5%以上股东 |
| status | enum | planned/implementing/completed/cancelled |
| first_disclosed_at | datetime | 首次披露时间 |
| last_disclosed_at | datetime | 最近进展时间 |
| cancelled_at | datetime/null | 取消或失效时间 |
| positive_level | integer | 0–3，0表示未达到加分门槛 |
| summary | string | 简短事实说明 |
| evidence_ids | string[] | 必要证据定位 |
| measurements | typed object | 对应事件的数值对象 |
| level_milestones | object[] | 首次达到每级的证据时间 |
| not_eligible_reason | enum/null | 未实施、数值不足、口径不明等 |

measurements按事件采用真实类型：

- 回购：actual_ratio_pct、purpose；用途为cancellation/employee_plan/incentive/other/unknown。
- 增持：actual_ratio_pct。
- 合同：contract_amount_cny、reference_annual_revenue_cny、reference_report_date、reference_disclosed_at、amount_to_revenue_pct、binding_confirmed、amount_basis（comparable/unknown）。未知数值允许为空。

level_milestones每项包含level、qualified_at、measurement_value_pct、evidence_id。普通进展不刷新已到期等级；真实达到更高等级时新增里程碑。无法确认同案或新增实施数量时，不重复授予加分。

### 2.3 待核实原因

review_reasons_json每项为scope（risk/positive/financial/pledge/unlock）、reason_code、可空evidence_id、detected_at、summary。风险待核实影响风险覆盖；普通利好待核实只使该利好不加分，不额外生成硬风险。

## 3. 过滤与评分

### 3.1 先检查资格

以下命中即排除：最新归母或扣非净利润小于0；营收增长≤−10%；归母或扣非增长≤−20%；最新完整质押比例≥10%；未来180天累计解禁≥1%；有效4级公告风险。未知或覆盖不足限制正式可执行资格，不能当作0风险。硬风险不允许被利好抵消。

### 3.2 财务与利好奖励

```text
成长分 = clamp(50 + 2 × 可用营收/归母/扣非增长率平均值, 0, 100)
财务综合分 F = 质量分×50% + 价值分×30% + 成长分×20%
Tomorrow财务规则奖励 = 2 × clamp((F−50)/50, 0, 1)
```

质量与价值沿用原配置。D25财务组件在主板/科创板约占29.41%、创业板约占14.29%，其财报增长已计入基础分，不再额外奖励。Tomorrow必要财务输入或时点不合格时不授予奖励。

| 利好 | 低级+1 | 中级+2 | 高级+3 |
| --- | --- | --- | --- |
| 已实际回购且明确用于注销 | 比例≥0.1% | ≥0.3% | ≥0.5% |
| 合格重要股东实际增持 | 比例≥0.1% | ≥0.3% | ≥0.5% |
| 已生效约束性经营合同 | 占当时已披露年度营收≥5% | ≥10% | ≥20% |

尚未实施、主体不合格、缺少数值、币种或税口径不明不加分。计划、预告、意向协议、分红送转、补贴、概念和媒体评级首版不自动加分。

Tomorrow利好有效至达到等级披露日后的第5个交易日收盘；D25为第20个交易日收盘。未来披露不能使用，普通进展不刷新期限，取消/失去依据即停止。交易日历不足不能虚构期限。

同利好组取最高，不同组相加；Tomorrow财务和事件奖励合计最多2分，D25事件奖励最多5分。有效公司监管扣分或风险覆盖不足时，不授予额外公司奖励；D25原财务组件不重复改变。Tomorrow财务奖励优先分配，其余按事件分降序、类型、身份稳定排序。

### 3.3 风险扣分

公司1/2/3级分别扣3/5/8，4级排除。公司监管组取最高；现有量价规则及组组合保持。拥挤5、价量背离4、高波动3、短期过热3、日内反转4、流动性收缩3、趋势破坏3。阈值只从统一配置读取。

删除旧公司负面公告5/15映射、标题直接加减分及被硬门覆盖的公司软扣分。同一事实先去重，再组内归并和跨组求和。

### 3.4 精确账本

`min(25,总扣分)`表示最多扣25分，不是最低25分。规则值、上限后的值与实际分数变化分别保留。

| 字段 | 含义 |
| --- | --- |
| weighted_base_score | 原基础分，已扣质量缺失，0–100 |
| requested_bonus | 符合资格的财务和利好规则奖励 |
| capped_bonus | 策略上限后的奖励 |
| applied_bonus | 考虑100分上限后的实际加分 |
| base_score | 加奖励后的基础分 |
| raw_risk_penalty | 去重、组归并后的总扣分 |
| local_risk_penalty | 25分上限后的扣分 |
| applied_risk_penalty | 考虑0分下限后的实际减分 |
| local_score | 最终本地分 |

```text
capped_bonus = min(requested_bonus, strategy_bonus_cap)
applied_bonus = min(capped_bonus, 100 − weighted_base_score)
base_score = weighted_base_score + applied_bonus
local_risk_penalty = min(raw_risk_penalty, 25)
applied_risk_penalty = min(local_risk_penalty, base_score)
local_score_unrounded = base_score − applied_risk_penalty
local_score = ROUND_HALF_UP(local_score_unrounded, 2)
```

输入必须有限且符合范围；负扣分、NaN不得静默当成无风险。先限制奖励再扣风险，不允许隐藏的超100奖励抵消风险。计算不逐项提前舍入。展示舍入差额只解释两位小数的显示误差，不改变计算。

| 原基础 | 规则奖励 | 奖励上限 | 归并风险 | 实际加分 | 实际减分 | 最终 |
| --- | --- | --- | --- | --- | --- | --- |
| 80 | 0 | 5 | 0 | 0 | 0 | 80 |
| 80 | 3 | 5 | 4 | 3 | 4 | 79 |
| 80 | 6 | 2 | 4 | 2 | 4 | 78 |
| 99 | 3 | 5 | 8 | 1 | 8 | 92 |
| 80 | 0 | 5 | 30 | 0 | 25 | 55 |
| 8 | 0 | 5 | 12 | 0 | 8 | 0 |

### 3.5 决策合同

V2/V3按local_score排名；V1保留独立模型排名，账本影响本地动作门；V5保持工程Shadow；Long不评分。DeepSeek只收紧动作，不改数值。模型净效用、可靠度、下行保护、集中度、15:00 first-wins及历史不可变保持。

## 4. 更新、日志及Web

### 4.1 每日更新与实时使用

依次更新结构化事实、新日期/最近7天修订、历史积压与到期重试；无积压时每日一个旧月修订。合并升级/解除/撤销/到期，更新受影响股票并原子发布。已完成断点不被重建，修订待核验撤下旧利好，已确认风险保留并标待核实。失败不能清空风险或刷新原披露时间。

推荐正常重启一次共享预读；盘中仅从内存检查有效期及年龄，结合当前价格形成估值及量价分。不读正文、不逐股查库、不联网抓公司资料。页面展示手动资料覆盖截止。

### 4.2 逐股日志

```text
sync company | 公告增量下载 | 最近处理公告目录 600519 贵州茅台 | 本轮该股页记录 86 篇 | 正文分析未开始 | 本轮涉及公告 1200/4401 股（覆盖核验中）
sync company | 公告正文分析 | 已尝试 120/500 篇 | 已核验 118 篇 | 当前 600519 贵州茅台 | 公告 86 篇 | 已分析 3 篇 | 待分析 2 篇 | 事件待核验
sync company | 发布公司投影 | 已分析股票 1201/4401 股（等待原子发布） | 当前 600519 贵州茅台 | 公告 86 篇 | 已分析 5 篇 | 待分析 0 篇 | 风险 0 条 | 利好 1 条 | 待核实 0 项 | 资料完整
```

股票总数固定，收到公告的股票按代码去重；下载阶段公告数为本轮接收数量，发布分析阶段为本股一年窗口的本地总数。逐页接收不等于本股已完整；正文计数是文本提取，不等于已确认事件。已处理、完整、部分、未发布分开，失败原因另列。批量发布前不显示发布成功。有进展每秒最多一条，无进展独立5秒心跳；纯复用批量汇总。默认隐藏证券组、子查询页码。最终汇总处理、完整/部分、更新/复用、待核验、正负事件及发布状态；新增或修订统一计入变更行，不把页数当股票进度。下载日志只展示事实，策略实际分以推荐为准。

### 4.3 现有评分列与详情

现有评分单元格展示最终分、原基础分、实际加分、实际扣分、复核状态，不增重复分数列。键盘或点击打开现有抽屉，显示财务贡献/奖励、利好事件、组归并、策略上限、100分限幅、风险合计、25分封顶、0分限幅和舍入解释。

D25财务贡献注明已包含于原基础分；V1模型排名分与本地动作分分开。评分账本、GET/SSE和冻结来自同一后端类型对象，前端不重算。旧历史缺明细时说明未保存，不重算旧分。

风险展示明确区分扣分、硬排除、动作收紧、待核实。完整且无有效风险显示“未发现已确认风险”；覆盖不足不显示成风险0。利好展示已加分、未达门槛、未实施、过期、撤销、因风险停用等原因。硬排除在既有过滤诊断展示，不塞回推荐名单；选席后合法veto回避不补位。证据标题和链接通过推荐只读用例按需取得，不加载全量正文或联网。

## 5. 实施与验收

原子切换新库、事件模型、下载核验与发布、全部推荐消费者、评分身份/冻结、API和Web；删除旧入口、大包、启动正文核验和重复加减分，不留双读双写或第二实现。所有权、消费者和删除路径登记在阶段273。

必测：两类JSON完整字段/枚举/空值/codec，主体和否定语义，同案合并/升级/撤销/解除，风险持续与减持90天，利好门槛及里程碑，财报时点/亏损/成长/缺失，Tomorrow奖励和D25不重复，未知和硬风险优先，同组归并、策略上限、100/25/0边界、六个算例及舍入，取消/锁/断点/原子发布，每日新增/修订，GET/SSE/Web账本和历史不变，V1/V5/Long/DeepSeek合同。

性能目标：正文预读0、每轮公司SQL及网络0；多档共享、512行一批；读取字节量较旧结构减少至少80%；同机同输入5轮交替中位预读耗时减少至少70%，热缓存全市场不超过3秒。独立报告首次读取、SQL、解码、RSS和总耗时；不截断事件达标。保留容量、取消和30秒deadline。诊断复用scripts既有框架，在仓库外隔离目录演练，不导入活动旧库。

文档、配置和代码同批更新；按风险运行定向测试、Ruff、mypy及桌面浏览器验收。完整diff Review后按AGENTS规范单批提交推送。研究先固定参数，再按评分第12节比较原策略、新风险、利好、Tomorrow财务奖励；缺点时公告不伪造回测。真实数据切换和生产启用按既有独立授权流程执行，不把工程完成写成收益通过。
