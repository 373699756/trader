# 开发工作计划

本文件是开发任务的拆分、依赖、状态和交付记录，不定义产品行为、评分公式或软件运行契约。

- 荐股流程、过滤、评分、风险、融合、动作、排名和展示规则：见
  [recommendation-strategy.md](recommendation-strategy.md)。
- 产品范围、架构、生命周期、数据服务、API、Web、运维和发布契约：见
  [software-business-design.md](software-business-design.md)。
- 依赖、构建和入口：见根目录 `pyproject.toml`。
- 批次纪律、Review、测试分级、提交和推送：见根目录 `AGENTS.md`。

## 1. 当前基线

基线提交：`edeae5f`，上游为 `origin/feature/tomorrow-v2`。

评分模块化计划项 1–9 已完成并已推送。已完成内容包括公共评分内核、通用模型评分路由、V1/V2/V3
profile 目录、V3 训练/在线特征合同、消费者迁移、旧链删除、权威文档同步和最终质量门禁。
历史下载、checkpoint、manifest、分片和运行数据不属于评分重构提交。

评分档位的当前能力矩阵：

| 档位 | Today | Tomorrow | D25 |
|---|---|---|---|
| V1 | 规则评分 | V1 模型 | 规则评分 |
| V2 | 规则评分 | V2 模型 | 规则评分 |
| V3 | 规则评分 | V3 Tomorrow 模型 | 规则评分 |

V3 当前只装配 Tomorrow 头。Today/D25 训练头、V3 三头组合和 V3 生产启用不是已完成事项，必须另立
数据、研究和人工授权批次。

## 2. 评分模块化交付记录

1. **计划项 1：批次闭合**。审查稳定命名迁移，确认工作树、上游和文件范围。
2. **计划项 2：契约先行**。固定 profile、策略头、JSON codec、生产/研究隔离、hash、冻结和融合边界。
3. **计划项 3：公共领域内核**。提取残差化、成本、净效用、分数映射、置信度和稳定排序。
4. **计划项 4：通用应用路由**。由 `ModelScoringRouter` 按策略头编排，Today/D25 明确走规则评分。
5. **计划项 5：V1 profile**。迁移 codec、线性 predictor、模型身份和状态证据，保持固定预测不变。
6. **计划项 6：V2 profile**。迁移 P2 工件 codec/predictor，研究类型不再穿透生产应用端口。
7. **计划项 7：V3 profile**。拆分 locator、codec、predictor、组合器，统一训练与在线残差化，当前只装配
   Tomorrow 单头并在缺失/篡改时失败关闭。
8. **计划项 8：消费者迁移**。bootstrap、settings、CLI、performance、状态聚合、研究消费者和包资源测试
   统一使用 profile 工厂，删除旧链和 facade。
9. **计划项 9：文档收尾**。本文件承接开发计划；策略文档只保留策略规则，软件设计只保留系统契约。

历史研究实现身份和效率门禁也归档于本工作计划：`score_current_baseline_consistency_audit`（失败状态
`baseline_identity_inconsistent`）、`ScoreTomorrowPointInTimeFeatures`、`ScoreTomorrowShadowModels`、
`ShadowModelArtifactStore` 和 `score_tomorrow_cost_aware_selection_report` 均为隔离研究任务，不能成为在线生产依赖。
研究特征实现不接入 `bootstrap.py`、HTTP、调度、活动运行库、正式决策或 DeepSeek。
评分热路径验证按每个完成评分 epoch、每个被评估候选、每次正式 current/frozen 决策和每个实际 DeepSeek 候选
计数，并要求相同输入的候选、分数、风险、动作、排名和决策 hash 完全一致，100 tick 分配增长不超过 20%。

## 3. 历史评分研究路线

所有研究只消费历史 point-in-time 数据，固定 `production_authority=false`；线上 outcome 仍只用于正式推荐历史、运行监控和回退告警。研究终态只有 `historical_data_insufficient`、`historical_rejected` 或
`historical_validated`，不自动改配置、重训、晋级、激活或回退。
不得定时、在线或无人授权地自动训练/调参、自动晋级、自动激活或自动回退。

不得恢复未来日 collector，不得把既有 139 日窗口重新命名为独立盲测；线上 outcome 仍只用于正式推荐历史、运行监控和回退告警。用户显式调用第 15.1.20 节的 `train-tomorrow` 时，命令也只能按已封存输入确定性执行，
不能绕过前置 blocker。

### 3.1 已封存章节

15.1.21–15.1.34 的基线审计、热链等价效率、H1 能力、标签/切分、残差账本、过滤消融、透明候选、时序确认、
Today/Tomorrow/D25 独立留出和跨策略结论均已形成不可变的数据不足或历史拒绝终态。三策略锚点固定为 Today 11:20、
Tomorrow 14:50、D25 14:50；候选确认使用 Holm 多重检验，最终留出至少 200 个交易日。不得恢复未来日 collector，
不得把既有 139 日窗口重新命名为独立盲测，也不得覆盖旧报告或把线上结果反推为全候选训练样本。

既有研究身份包括 `score_r6_historical_legacy`、`score_r6_daily_trend`、`score_r6_daily_stability`、
`score_tomorrow_historical_p2`、`tomorrow_v1_v2_h0_holdout_report_v2` 和
`tomorrow_v2_historical_risk_probability_v1`；它们只作不可变审计，不形成新的运行任务。

### 3.2 依赖状态

| 章节 | 状态 | 依赖/退出条件 |
|---|---|---|
| 15.1.35 Tomorrow V3 单一行业模型 | `blocked_by_15_1_38` | 2000 日线 manifest 合格且历史行业/资格 `effective_at` 审计通过 |
| 15.1.36 V3 条件式生产适配 | `blocked_by_15_1_35` | 15.1.35 日线代理通过；生产启用仍需用户授权 |
| 15.1.37 四路实施边界 | `control_only` | 只维护所有权、依赖和交付边界 |
| 15.1.38 BaoStock 2000 日归档 | `pending` | 当前唯一可执行章节；供应商登录/历史请求恢复后开始 |

每次“继续”只交付表中唯一 `pending` 的完整章节；当前计划中唯一下一执行章节是 15.1.38。
`blocked_*` 不得被强行改成可执行状态。

### 3.3 V3 训练与验证（15.1.35–15.1.36）

V3 是新的唯一 Tomorrow 模型，C3 只表示其离线训练阶段，不产生独立 profile，不做投票或 stacking，不读取 V1/V2/C3 运行时预测。训练切分使用下载数据库实际可用的共同完整交易日（只使用下载数据库中实际存在的记录），先保留最新 200 日；模型为 Ridge/LightGBM 50/50。
缺历史、行业或资格事实时返回 `historical_data_insufficient`，不生成伪模型。

老 V2 predictor、bundle、hash、配置语义、历史和冻结记录全部封存且不修改；共享文档、CLI 和生产接缝由集成
批次统一审查，不在训练批次中复制实现。

训练、确认和终端留出必须按交易日不重叠切分；最终留出至少 200 个交易日。`point_in_time_parity=false`、
`automatic_model_update=false` 和 `production_authority=false` 在研究阶段固定不变。

唯一公开训练命令是 `./run.sh train-tomorrow`；归档入口为 `./run.sh download_history`。一次命令形成一个由输入 manifest 和 hash 派生的 `run_id`，
产物位于 `data/train/tomorrow-v3/<run_id>/`，至少包含 `model.json`、`report.json` 和 `evidence/`。主程序启动时读取最新 `model.json`
并经过 codec 校验，不得自动 promotion、切换 profile 或回退旧模型。

### 3.4 BaoStock 2000 日归档（15.1.38）

目标是截至 `2026-08-31` 最近 2000 个交易所开市日，每只股票最多 2000 个代码-日期逻辑记录；新股、退市股、
停牌股和来源不足按真实有效区间保存，不补造数据。raw/qfq 必须在同一行 `(code, trade_date)` 保存，前复权、未复权
字段不能拆成两条记录。股票应有交易日从上市日起算，不能把新上市股票伪造成缺少 2000 日。`--sessions` 接受 1–2000 且默认 2000，
只有 `--sessions 2000` 可以生成正式 manifest。

归档身份为 `score_baostock_daily_core_v2`，默认目录为
`data/history/baostock-daily/sessions-2000/`，分片文件位于 `shards/<board>-<code-prefix>.sqlite3`。分片按板块与股票代码前四位存储，每个分库最多 100 只股票，单个
分库损坏时只重新下载该分库（单个分库损坏，只重新下载该分库）；最终只登记 `catalog.sqlite3`、分片和
`manifest.json`，不生成合并总库。所有归档内容均带内容 hash，供恢复和审计复核。

覆盖门禁要求全体和逐板应有代码-日期单元覆盖率均不低于 95%，至少 95% 的全窗口老股各自达到 95% 完整率，
并保留最新 200 日、失败代码、停牌证据和全部 hash。BaoStock 不能证明历史 11:20/14:50 点时、行业或资格
`effective_at`，所以日线合格不能直接打开 Today/Tomorrow/D25 留出或生产权限。

四路所有权固定如下；Codex A 独占数据内容语义，Codex B 不实现下载、覆盖审计或切分，Codex C 不定义或重切数据集，
Codex D 不决定覆盖是否通过：

| Owner | 负责内容 | 禁止事项 |
|---|---|---|
| Codex A | gateway、raw/qfq 同行归一化、日历/股票池、SQLite 分片、checkpoint、合并、覆盖审计、manifest/hash 和历史事实能力 | 不训练模型、不读取收益、不改生产接缝 |
| Codex B | 只消费冻结 manifest，校验六 Alpha 字段、单位、键和 hash；父数据合格后训练 Ridge/LightGBM 50/50 | 不下载、不切分留出、不做 stacking、不读旧 V2 分数 |
| Codex C | 留出隔离、日线代理留出、独立 14:50 留出、影子比较和收益/风险报告 | 不重切数据、不调参、不改训练 schema、不激活生产 |
| Codex D | `[research]` 依赖、CLI、锁/超时/取消/恢复、单一 `train-tomorrow` 编排、状态投影和生产接缝 | 不裁决覆盖、不决定收益通过、不在授权前 promotion |

实施波次：

1. 契约波：冻结 schema、hash、失败关闭和 `production_authority=false`，尚不联网。
2. 实现波：完成 SDK adapter、下载、checkpoint、审计、manifest、单股/小批幂等和中断恢复。
3. 全量波：执行 2000 日全 A 股下载，形成逐股、逐板、全体覆盖和资源证据。
4. 研究交接波：A 发布只读 manifest/事实 port，B 才能确认输入兼容，C 保持终端留出关闭，D 汇总状态。

运行上限：固定最多 1 个进程（固定最多 1 个 SDK 子进程）；单次供应商调用墙钟上限 60 秒；最多重试 2 次；每次查询至少间隔 2 秒；
取消宽限 10 秒；启动至少 25GiB 可用空间，低于 2GiB 在当前 checkpoint 后安全停止。供应商 `10001011` 映射为
`supplier_query_failed_blacklisted` 并立即停止整次运行。续传先读取 checkpoint 索引和冻结上下文，不解码
`daily_cells.payload_json`；日线完成与行业事实 checkpoint 分离，行业不足时保留日线并标记
`historical_industry_incomplete`。`baostock_runtime_progress` 的阶段包括 `preflight`、`supplier_login`、
`trading_calendar`、`security_universe`、`database_initializing`、`worker_starting`、`downloading`、`merging`；
进度固定投影 `sessions`、`universe_count`、`checkpointed_codes`、`remaining_codes`、`completed_codes`、
`failed_codes`、`expected_records`、`downloaded_records`、`active_workers`、`source`、`current_code`、
`rate_limit_cooldown_seconds`、`last_failure_reason`、`elapsed_seconds`、`checkpoint_database_pattern`、
`partition_database_pattern`、`catalog_database`、`manifest_path` 和 `checkpoint_loading`。60 秒只约束单次供应商调用，不约束包含多次正常调用的完整阶段或单股任务。

当前状态：供应商登录/历史请求仍被拒绝，尚无合格全量 manifest；不得把 fixture 或入口成功当作真实覆盖，
不得启动 V3 训练、留出或生产晋级。备用 Tencent/Eastmoney/Tushare 探针未形成可替代的正式归档，不能混入
BaoStock checkpoint 或 manifest。

Codex B 波次 1 状态：已完成。`tomorrow_v3_input_compatibility_v1` 只读校验冻结输入描述的
`score_baostock_daily_core_v2`、2000 日身份、`(code, trade_date)` 键、raw/qfq 同行、六个 Alpha 字段、单位和
父 manifest hash；固定 `training_started=false`、`terminal_holdout_opened=false`、`production_authority=false`、
`automatic_model_update=false`。15.1.38 整节仍为 `pending`，不能把 fixture 结果解释成真实覆盖。

Codex C 工程契约已完成：`baostock_holdout_isolation_contract` 只验证新旧留出身份隔离和最新 200 日边界，
保持 `score_tomorrow_historical_candidate`、`tomorrow_v3_point_in_time_holdout`、`terminal_holdout_opened=false`、
`point_in_time_parity=false`，
不打开留出、不读取收益、不改变生产权限。

## 4. 交付与验证

每个批次先更新契约和失败测试，再实现；子任务只运行直接相关的定向测试、Ruff、mypy 和 diff 检查，整个大任务
收尾时才运行一次完整门禁。高风险批次的完整命令组为：

```bash
make format-check
make lint
make type-check
make test
make package
```

文档职责调整只需文档契约、链接/格式检查和 `git diff --check`；若修改机器契约，追加受影响契约测试。
每次“继续”只交付下一个完整未完成章节。提交前确认暂存区只包含本批文件，使用一个 Conventional Commit，推送并核对 `HEAD == @{upstream}`。
