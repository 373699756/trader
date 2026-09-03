# Changelog

All notable changes to this project are documented here.

## Unreleased

### Added

- 用户要求区分历史下载与训练结果目录，并明确历史下载自动落盘。本批将 `download_history` 的默认目录改为
  `trader/data/history/`，该目录及其 SQLite/WAL/manifest 被 Git 忽略；训练编排器改为把 Tomorrow 研究工件写入
  可提交的 `trader/data/train/`。同时移除历史下载目录必须位于仓库外的旧限制，保留显式 `--runtime-dir` 覆盖能力。
  Verification: BaoStock CLI、运行请求和计划契约定向测试通过，受影响文件 Ruff 通过，`git diff --check` 通过。
  Residual Risks: 真实 BaoStock 登录/全量覆盖仍需在安装 `[research]` 依赖且服务端可用时验证。

- 用户本轮要求依据策略文档执行 Codex B 未完成任务。B 的波次 1 输入兼容契约及既有数据不足收口均已完成；
  由于 Codex A 尚无真实 2000 日合格 manifest，本轮未启动 V3 训练、候选、收益或终端留出。同步校正文档中的
  最新实测状态：`download_history --sessions 1` 返回 `supplier_login_transport_failed`，
  `--sessions 2000` 在外部 I/O 前因 `disk_below_30gb` 阻塞；15.1.38 继续为 `pending`，15.1.35 继续为
  `blocked_by_15_1_38`，生产权限和自动更新保持关闭。`Regression-Key: codex-b-blocked-by-baostock-v3-v2`。

- 用户要求先修复 Codex D 运行诊断和 BaoStock 登录阻塞。本批将统一 `research` profile 适配到现有
  `v2_research_readiness_v9`，只读投影 `baostock_history`、Tomorrow V3 blockers 和
  `production_authority=false`，不新增 `research-status` 状态源或读取 V1/V2 预测。
  `Regression-Key: codex-d-research-v9-baostock-gate-v1`。

- 用户确认旧 H0 历史研究链（`research-history`、`research-screen`/`screen-history`、固定回测和六阶段筛选）不再作为执行入口，统一使用 `download_history` 下载 BaoStock 历史日线，再由 `train-tomorrow` 执行 Tomorrow 训练；本批补齐入口、脚本、文档和负向契约测试。

- 用户本轮要求继续执行推荐策略第 15 节 Codex B 未完成任务。复核确认 15.1.38 的 Codex B 波次 1 输入兼容
  契约已完成，但后续 V3 训练仍必须等待 Codex A 的真实 2000 日合格 manifest；同步修正文档，将 D 的执行模型
  明确为受控独立子进程监督。2026-09-02 显式探针：`--sessions 1` 返回
  `supplier_login_failed_unboundlocalerror`；`--sessions 2000` 在外部 I/O 前因可用空间低于 30GB 返回
  `resource_blocked/disk_below_30gb`（约 8.85GiB 可用），未创建全量运行目录或研究工件。15.1.38 仍为
  `pending`，15.1.35 仍为 `blocked_by_15_1_38`；本批没有训练、收益、终端留出或生产权限变化。
  `Regression-Key: codex-b-baostock-blocker-evidence-v1`。

- 用户要求继续执行推荐策略第 15.1.38 节 Codex D 未完成任务。本批接入 `[research]` 可选 BaoStock 依赖，新增
  `research-baostock-history --runtime-dir <仓库外绝对路径> --sessions 1..2000` 显式入口及 `research-status`
  状态投影；实现每股独立 WAL SQLite 分片、逐行 `next()/get_row_data()` 查询、最多两个子进程、60 秒墙钟、
  最多两次重试、锁、取消、断点恢复、确定性合并和 SHA-256 manifest。基础 Web、启动、`check`、bootstrap、
  `train-tomorrow` 均不导入或隐式下载 BaoStock。`Regression-Key: codex-d-baostock-history-integration-v1`。

- 用户依据荐股策略要求执行 Codex B 未完成任务。本批完成第 15.1.38 节 B 波次 1 的只读输入消费契约：
  新增 `tomorrow_v3_input_compatibility_v1`，以类型化 port 核对 BaoStock v2 冻结描述的 2000 日身份、
  `(code, trade_date)` 主键、raw/qfq 同行、必需字段及单位、逐行 SHA-256 和父 manifest hash，并固定六个
  Alpha 名称/单位。报告只产生 `compatible|incompatible` 及有界原因，不读取价格行、覆盖率或收益，不切分、
  不训练、不打开留出、不授予生产权限。`Regression-Key: tomorrow-v3-input-compatibility-v1`。

- 用户要求执行荐股策略第 15 节 Codex C 未完成任务。本批新增纯领域
  `baostock_holdout_isolation_contract`：只消费未来由 Codex A 封存的类型化 manifest 元数据，逐项验证最新
  200 日未被训练、确认或日线代理消费，拒绝 BaoStock 日线声明 14:50 点时一致，并固定新
  `tomorrow_v3_point_in_time_holdout_v1` 与已完成第 15.1.32 节旧留出的身份和父 hash 隔离。审计不读取
  行情、收益或模型，不定义切分且始终保持留出关闭和无生产权限。
  `Regression-Key: codex-c-baostock-holdout-isolation-v1`。

- `baostock-2000-v3-roadmap-consistency-v1`：新增未完成章节状态表、唯一 `pending` 执行项、BaoStock/V3
  非重叠所有权契约，以及 2000 条逻辑记录、raw/qfq 同行、全体/逐板/老股覆盖、逐股失败、SDK 子进程资源
  上限和新旧终端留出隔离的机器断言。文档任务不实现 SDK、CLI、SQLite 或生产 profile。

- 用户要求把 BaoStock 作为历史数据下载源集成计划写入荐股权威策略，并按 Codex A/B/C/D 分工。本批新增
  第 15.1.38 节，固定 `score_baostock_daily_core_v1`、截至 2026-08-31 的最近 1500 个交易所开市日、
  前复权/未复权共同日线、仓库外分片 SQLite/checkpoint/合并 manifest 及逐股 SHA-256；覆盖分母改为股票
  从上市日至退市前的应有代码-日期单元，避免要求新股伪造上市前行或通过排除失败股票制造覆盖。A 负责
  SDK/下载/归档，B 负责数据质量与 95% 覆盖审计，C 负责切分/200 日保留/V3 readiness，D 负责依赖、显式
  CLI、分片编排、合并和最终集成。明确 BaoStock 日线不能证明历史 11:20/14:50 或完整 `effective_at`，
  不得据此开启三策略终端留出或获得生产权限。`Regression-Key: baostock-1500-daily-roadmap-v1`。

- 用户要求合并 `recommendation-strategy.md` 与行业 LightGBM 计划。本批将 Tomorrow 新方案统一为 V3 单一
  申万一级行业 Ridge + LightGBM 50/50 离线模型：C3 仅为训练阶段，不再创建 V3 stacking 或读取 V1/V2
  运行时预测；老 V2 predictor、bundle、hash、历史和冻结记录保持封存不变。策略文档补齐 3,000 日数据门禁、
  点时行业生效、标签/embargo、LightGBM 固定参数、单次离线批次、唯一模型工件、14:50 代理差异、人工启用、
  失败关闭和四路无干扰任务边界。验证：`git diff --check` 通过；pytest 未执行，因本机 `.venv` Python 解释器路径失效。
  剩余风险：V3 代码、配置、wheel 和生产接入仍需独立高风险批次，当前不改变活动 profile。

- 用户再次要求执行推荐策略第 15 节 Codex B 未完成任务。本批补齐第 15.1.35 节 V1/V2/C3 原始预测联合的
  数据不足收口：新增 `tomorrow_joint_insufficient_terminal_v1` 与应用封存入口，并将联合终态 hash 绑定到
  Codex B 批次。父工件不足时不读取日期或收益，不生成原始预测、联合模型、Holm 统计或终端留出；完整父
  工件出现后才允许另立真实联合研究批次。`Regression-Key: codex-b-tomorrow-joint-insufficient-v1`。

- 用户要求继续执行推荐策略第 15 节 Codex B 未完成任务。本批新增
  `seal_codex_b_insufficient_batch` 与 `CodexBTerminalArtifactStore`：读取已封存的 Codex A completion，按
  Today/Tomorrow/D25 继承 completion、capability、标签和残差账本 hash 及失败原因，封存
  `historical_codex_b_insufficient_batch_v1`。由于父工件为 `historical_data_insufficient`，不生成过滤人口、
  透明候选、Holm、收益、模型或终端留出；联合报告仅保存可审计的终态 hash。`Regression-Key:
  codex-b-historical-data-insufficient-closure-v1`。

- 用户要求执行推荐策略第 15 节 Codex C 未完成任务。本批新增仓库外的
  `scripts/codex_c_terminal_holdout.py`：校验 Codex A capability、标签批次和 terminal index 的父 hash，
  对 Today、Tomorrow、D25 分别封存继承父 hash/失败原因的 `historical_data_insufficient` 终态，再封存跨策略
  只读结论。真实执行未读取终端日期、未生成收益或模型、未打开留出，也未修改生产资源；工件二次执行幂等。
  `Regression-Key: codex-c-terminal-failure-closure-v1`。

- 用户要求继续执行推荐策略第 15 节 Codex A 的耗时未完成任务。本批先完成真实 H1 来源能力审计，而不是
  无条件启动全市场下载：腾讯前复权日线返回 640 行、最早 2024-01-09，无法达到至少 1000 个共同交易日；
  东方财富历史分钟端点独立探测失败，免费来源仍不能证明 11:20/14:50 历史锚点和历史有效证券状态。
  三策略、标签预注册、残差账本和 Tomorrow C3 均已封存 `historical_data_insufficient` 终态；未生成
  OOF/model、未开启终端留出，也未取得生产或自动更新权限。`Regression-Key: codex-a-h1-capability-audit-v2`。

- 用户要求继续执行第 15 节 Codex D 未完成任务，并将 A 项目的可执行 H1 前置能力接入 Tomorrow 训练入口。
  `train-tomorrow` 与 `research-status` 现在通过同一只读类型化 port 检查 Codex A 的元数据/标签预注册；
  H1 覆盖不足时在资源探针和 handoff 读取前直接报告 `tomorrow_*` 数据 blocker，绑定预注册批次 SHA-256，
  不写空工件、不伪造资源测量、不创建 V3 或生产权限。`Regression-Key: tomorrow-codex-a-prerequisite-gate-v1`。

- 用户要求继续执行第 15 节 Codex B 未完成任务。本批先执行 H1 点时归档只读审计：`python -m
  trader.entrypoints.h1_point_in_time --runtime-dir .runtime/v2`。Today、Tomorrow、D25 均明确返回
  `historical_data_insufficient`，覆盖率和共同交易日均为 0，且 `terminal_holdout_opened=false`、
  `production_authority=false`。由于本机没有可证明 11:20/14:50 点时字段的来源归档，15.1.28–15.1.30
  继续保持阻塞，未生成候选、收益或生产工件。

- 用户要求继续执行第 15 节 Codex B 未完成任务。本轮复核确认 Codex B 的过滤消融、透明候选、Holm 确认和
  Tomorrow 原始预测联合工程代码已经封存；但 Codex A 的 H1 真实归档尚未初始化，15.1.26 标签/切分与
  15.1.27 成熟残差账本没有可消费父工件，因此 15.1.28–15.1.30 保持未运行，避免伪造历史 point-in-time
  收益证据。策略文档同步记录该阻塞状态；未修改生产配置、V1/V2/C3 工件或终端留出。

- 用户要求执行推荐策略第 15 节 Codex A 未完成任务。现状确认 H1 覆盖与标签预注册已有骨架，但
  `historical_prediction_residual_ledger_v1` owner 完全缺失，Tomorrow C3 只有可手工构造的数据类，尚无
  H1 日线特征/成熟标签接线、固定五候选训练、5 折 OOF 或可执行 Ridge/LightGBM adapter。本批新增两阶段
  H1 adapter：先只读截至 D 的前复权 1/3/5 日收益与 20/40/60 日 skip-5 市场/板块/成交额残差动量并封存
  feature hash，再独立读取 D+1 生成同人口等权基准和 20/50/100bp 标签；未来行篡改不改变既有特征，过滤
  证据不完整和 D+1 停牌行失败关闭；复用此前已封存、可拒绝冲突和篡改的三策略标签预注册 JSON 工件。
  新增固定参数、最多 2 线程的 Ridge/浅层 LightGBM、50/50 集成、
  强收缩板块残差和极强收缩个股残差五候选，全部修正只拟合此前折 OOF 残差，最新 200 日点时保留段不参与
  开发或冻结。新增预测/结果物理分表、追加写 SQLite 账本，精确绑定策略/日期/锚点/代码/horizon/父 split/
  模型 hash，保留 `label_pending` 与 `not_modeled`，按交易日及市场/板块/流动性/波动/Top6 分层汇总误差。
  所有工件固定 `production_authority=false`、`automatic_model_update=false`，不接入生产、DeepSeek、联合器
  或终端留出。`Regression-Key: codex-a-h1-residual-c3-v1`。

- 用户要求执行推荐策略第 15 节 Codex D 未完成任务。新增 `train-tomorrow` 唯一公开入口及
  `tomorrow_research_artifact_graph_v1`：先封存 100 只/120 日资源探针，再按父 SHA-256 连续推进开发、
  确认、日线代理留出和 14:50 点时留出；输入/探针 hash 派生不可由用户指定的 `run_id`。独立 run 目录
  使用内容寻址图、进程锁、compare-and-set 和隐藏原子检查点恢复；同内容幂等、异内容/篡改/环/错误 owner
  失败关闭，终态 `report.json` 显式绑定工件图、资源、失败原因和 Parquet 分区，存在候选时才封存经 hash
  校验的 `model.json`。`research-status` 升级为 `v2_research_readiness_v8`，加法公开 run、下一阶段和生产
  阻塞；V3 仍未注册，默认 V1、V2 工件、生产评分和自动更新边界不变。
  `Regression-Key: tomorrow-research-orchestration-artifact-graph-v1`。

- 用户确认进一步精简 Tomorrow V3 历史训练契约：目标命令改为语义明确且当前尚未实现的
  `./run.sh train-tomorrow`，一次调用连续执行全部可用训练/确认/两级留出并支持原子断点续跑；结果目录
  对用户只公开合并数据/运行/验证信息的 `report.json` 和唯一模型结果 `model.json`，全量特征、标签及
  OOF 证据改为内部 `evidence/` 日期分区 Parquet。主程序不读取研究目录，只有两级留出通过和再次授权后，
  独立发布批次才把 `model.json` 转换为 wheel 模型资源。
  `Regression-Key: tomorrow-v3-single-command-two-artifact-contract-v1`。

- 用户要求执行推荐策略第 15 节 Codex C 未完成任务。新增研究隔离的 Today 11:20、Tomorrow 14:50 和 D25
  14:50 终端留出回放契约，以及只读跨策略结论服务。回放按交易日聚合完整候选人口，固定比较 20/50/100bp
  成本、local-only 基准配对增量、移动区块 bootstrap、严重亏损、换手、Rank IC、Q5-Q1、容量和集中度；
  D25 强制成对保留 T+2 至 T+5 收益。父候选拒绝/数据不足时不打开最终留出，点时一致性或 horizon 缺失失败关闭，
  合法空仓日保留在交易日分母；所有报告和跨策略汇总绑定 SHA-256、固定策略顺序且
  `production_authority=false`，不创建生产资源、不修改 H0/P2/V1/V2 或 DeepSeek 行为。新增三策略
  `report.json` 不可变封存适配器，采用显式字段白名单、内容哈希校验、同内容幂等、异内容冲突和策略/模式
  篡改拒绝；修复指标聚合辅助路径缺失导致的终端回放运行时错误。
  `Regression-Key: codex-c-terminal-holdout-v1`。

- 用户要求执行推荐策略第 15.1.25 节，并明确所有荐股评分/训练只能消费历史 point-in-time 数据。新增独立
  `score_h1_point_in_time_v1` 类型化规范、Today 11:20 与 Tomorrow/D25 14:50 锚点记录、参数化来源能力探针
  port、有界 1600 交易日下载/断点续传服务，以及独立 `score-h1-point-in-time` SQLite 归档。股票池、逐股
  记录、交易日、字段覆盖和来源响应均绑定 SHA-256 manifest；同内容幂等、异内容/未来行/非前复权/时区和
  点时冲突失败关闭。三策略只读覆盖审计分别返回 `coverage_ready` 或 `historical_data_insufficient`，并固定
  `terminal_holdout_opened=false`、`production_authority=false`，不生成候选、不计算收益、不修改 H0 或生产缓存。
  新增显式 `python -m trader.entrypoints.h1_point_in_time --runtime-dir <path>` 入口；统一 CLI 注册留给 Codex D，
  普通 `check`、Web 和生产启动不会隐式运行。
  `Regression-Key: h1-point-in-time-archive-coverage-v1`。

- 用户要求执行荐股策略第 15.1 节 Codex B 未完成任务。新增研究隔离的
  `historical_filter_recall_ablation_report_v1`、透明有限候选和
  `historical_candidate_confirmation_report_v1` 类型化领域契约及应用编排：一级永久资格/安全 veto
  固定为控制组，二级规则只允许预注册 leave-one-rule-out 与双规则交互；候选上限固定为每策略 8 个并
  保留 local-only 控制组；确认段按日期对齐、5 日区块 bootstrap 和完整 Holm family 一次评价。所有
  研究对象都绑定 SHA-256、20/50bp 指标、终端留出未开启和 `production_authority=false`，不修改生产
  配置或 V1/V2/C3 工件 schema。`Regression-Key: codex-b-historical-filter-confirmation-v1`。

- 用户要求综合此前关于历史日线训练、V1/V2/C3 联合、收益目标和主程序交互的讨论，把第 15 章全部未完成
  工作拆成 Codex A/B/C/D 四条可独立并行路线，而不是只安排 C3 与 V3。荐股权威文档新增第 15.1.37 节：
  A 独占 H1/标签/残差账本与 C3，B 独占过滤/透明候选/确认与 V3 联合器，C 独占 Today/Tomorrow/D25
  终端留出及跨策略结论，D 独占工件状态机、共享集成、单一训练命令和条件式生产适配；四个波次允许基于
  类型化 port/fixture 并行编码，但真实数据、确认和留出严格等待父工件 SHA-256。同步把 Tomorrow 新训练
  的目标入口最终收敛为无阶段参数、当前尚未实现的 `./run.sh train-tomorrow`，一次调用连续完成完整可用
  训练链且不包含自动 promotion。修正上游拒绝/数据不足导致终端章节永久阻塞、终态语义和容量/状态分层不一致；V1/V2 工程
  继续冻结，V3 仍无生产权限，过滤证据不完整股票继续禁止进入模型推理。
  `Regression-Key: four-lane-tomorrow-research-roadmap-v1`。

- 为使四路后续能基于真实类型契约并行，新增隔离的 Tomorrow C3 日期保留/时序切分、成熟标签裁剪、
  Ridge/LightGBM/固定集成工件、强收缩分层/个股残差约束和严格 JSON/SHA-256 codec；新增 V1/V2/C3
  原始成本后净超额的严格共同交集、非负 simplex 权重、固定正则候选及收益/风险门禁选择核心。当前能力
  只提供纯研究逻辑与 fixture 测试，没有 H1 下载/接线、真实训练执行、阶段编排、完整联合工件、终端留出
  或生产装配，始终 `production_authority=false`。

- 用户要求继续完成推荐策略第 15.1.24 节，并明确荐股评分/训练不得依赖未来数据。新增只读
  `scoring_hot_path_efficiency_baseline_v1` 类型化基线报告及 `trader-cli research-scoring-hot-path-baseline`，
  按 Today/Tomorrow/D25 与阶段记录 `ScoringInputEpoch`、变化码脏集收缩、实际股票/因子重算、延迟
  P50/P95/最大值、外部请求、缓存命中、SQLite 事务/字节、latest-wins 替换、冻结前完成率，以及 epoch、
  被评估候选、正式 current/frozen 决策和实际 DeepSeek 候选四类独立成本分母。原因是原有运行 telemetry
  分散在输入适配器、latest-wins 和 `LatencyWaterfall`，无法审计重算成本和等价性；修改只消费已存在的
  历史/当前输入，不读取未来收益、不改变候选、评分、风险、融合、预算、冻结或降级语义。报告封存相同
  输入、乱序、缓存冷热、部分来源失败、latest-wins 替换和冻结边界的决策 hash 等价结果，并证明脏集
  收缩；合法空推荐仍保留真实候选分母。`Regression-Key: scoring-hot-path-efficiency-baseline-v1`。

- 用户依据 `recommendation-strategy.md` 执行第 15.1.38 节 Codex A 未完成任务。本批新增
  `score_baostock_daily_core_v2` 类型化数据面：BaoStock 逐行 gateway、2000 日交易日历/上市退市有效股票池、
  raw/qfq 同 `(code, trade_date)` 逻辑单元、WAL SQLite 分片/checkpoint、确定性合并、全体/逐板/逐股覆盖审计、
  来源版本及 SHA-256 manifest、历史行业/资格/硬过滤/风险事实 `effective_at` 能力工件，以及固定标签和唯一
  V3 切分工件。A 的冻结 store 实现 B 的只读输入描述 port；所有产物保持留出关闭、无点时一致和无生产权限。
  `Regression-Key: baostock-daily-core-v2-data-plane-v1`。

### Fixed

- `baostock-positional-query-compat-v1`：历史查询现在按 `/tmp/baostock_download_1500.py` 的调用形状传递
  `start_date/end_date` 位置参数，避免 SDK 兼容实现只接受旧式位置参数时在查询阶段失败；并继续保留单 worker
  和黑名单快速失败策略。该修复不改变 2000 日、raw/qfq 同键和 manifest 资格门禁。

- `baostock-anonymous-request-rate-v1`：复核 `/tmp` 分片产物发现供应商登录成功后，历史查询在并发分片下集中
  返回黑名单错误；BaoStock runtime 默认 worker 从 2 收敛为 1，匹配旧脚本单进程请求形状，并对登录黑名单
  失败停止无效重试，避免继续放大供应商封禁。显式传入 2 个 worker 仍受原有上限约束。

- `codex-b-blocked-by-baostock-v3-v2`：修正 BaoStock gateway 定向测试的私有导入排序，使当前分支的 Ruff
  门禁可重复通过；不改变测试语义或供应商调用边界。

- `baostock-anonymous-login-v2`：按用户指定的 `/tmp/baostock_download_1500.py` 收敛 BaoStock 登录实现，
  runtime 统一直接调用 SDK 原生无参数 `login()`，移除用户名、密码和 API key 分支；查询继续使用逐行
  `next()/get_row_data()`。该实现不绕过 BaoStock 服务端账号/IP 黑名单。

- `codex-d-research-v9-baostock-gate-v1`：诊断脚本不再按已退役的 v5 `active_research` 结构解析 v9 状态；现按
  公开 v9 投影白名单输出 BaoStock 状态、V3 输入/生产 blockers，并在任一 blocker 存在时失败关闭。
  BaoStock 登录读取显式环境凭据/API key，服务端黑名单、网络拒绝、超时、SDK 传输异常和黑名单响应分别映射为
  有界错误码；不记录凭据、错误文本或供应商载荷。

- `codex-d-baostock-history-integration-v1`：最终并发 Review 完成 BaoStock 快照/切分校验的缩进与格式整理，并将
  显式 CLI 分派的已审查复杂度标注纳入门禁；严格重构债务基线恢复为零，业务校验语义不变。

- `codex-c-baostock-holdout-isolation-v1`：第 15.1.38 节此前只有人工可读的 Codex C 隔离要求，无法机器证明
  A 的最新 200 日是否泄漏给三个日线阶段，也无法拒绝复用旧留出身份/hash 或伪称 14:50 点时一致。现以
  不可变输入、受控 blocker、严格 SHA-256 和确定性审计 hash 固化该边界；第 15.1.38 整节仍为 `pending`，
  不把局部契约完成误报为 A/B/D 工程或真实 2000 日下载完成。

- 用户把 BaoStock 下载上限更新为每股最多 2000 条，并要求按 Review 建议消除计划矛盾。确认原因是原
  1500 日计划没有同步 V3 的 3000/1600 日目标、1000 日不可满足的四段切分、15.1.37/15.1.38 重复所有权、
  已完成 15.1.32 被未来 V3 重开、越权启用与标准发布前置条件冲突，以及软件设计仍描述旧 V1/V2/C3
  stacking。现统一为独立 BaoStock v2 身份、V3 至少 1250 个完整有效日、先隔离 200 日新点时留出、A 数据/
  B 训练/C 验证/D 集成，并把研究验证、人工授权和生产接入拆为独立批次。
  `Regression-Key: baostock-2000-v3-roadmap-consistency-v1`。

- 用户要求先拉取远端并解决冲突。本批将本地 `feat(research): add isolated 640-day close proxy`
  重放到最新上游时确认，该独立 H0 Ridge 代理已被新的 Tomorrow V3 单一行业
  Ridge/LightGBM 50/50 权威契约整体取代，因此冲突解决为保留上游唯一 `train-tomorrow`
  入口并跳过过时提交；同步修正远端提交遗留的旧四路/发布措辞契约断言。

- `codex-a-h1-capability-audit-v2`：能力探针改为逐来源失败隔离，供应商 TLS/连接/载荷失败不再丢弃其它
  来源的成功证据；`score_h1_source_capability_audit_v2` 以类型化 `probe_failures` 进入不可变 hash，
  继续传递到三策略残差和 C3 数据不足原因。执行脚本先直连、失败时使用同一 `requests` 栈的系统代理
  会话，统一参数、请求头、超时和异常分类，不依赖外部 `curl` 或泄露供应商载荷。

- `tomorrow-codex-a-prerequisite-gate-v1`：修复研究状态入口把 Tomorrow 工件图冲突错误投影为
  `h1_archive_invalid` 的边界归因问题；H1 归档冲突和 Tomorrow 图/检查点冲突现在分别保留各自 blocker，
  且 H1 前置仍在读取 Tomorrow handoff 前失败关闭。

- `codex-a-h1-residual-c3-v1`：Review 修正三项会削弱研究证据的问题：MAE/ATR20 改为有符号不利波动并按
  T+1 `<= -1.5`、D25 `<= -2.5` 校验严重亏损标签；账本只有全部已建模预测均连接成熟 outcome 时才返回
  `residuals_ready`；H1 D/D+1 连接由逐行扫描改为身份索引，避免全市场 1600 日规模退化为平方复杂度。
  SQLite decoder 同时增加字段白名单、类型解析、重建 hash 和未知字段/篡改失败关闭。

- `tomorrow-research-orchestration-artifact-graph-v1`：最终 Review 将 Tomorrow 工件提交中的 CAS/活动 run
  校验、终态模型验证、Parquet 证据封存和 checkpoint/终态发布拆为同一进程锁内的独立职责，并将 H1
  manifest 的 SQLite 快照读取、universe/record/history hash 审计与报告组装分离；保持工件 schema、
  内容 hash、写入顺序、失败关闭和生产隔离语义不变，严格复杂度债务恢复为零。

- `codex-b-historical-filter-confirmation-v1`：修复过滤消融报告生成规则贡献时引用未定义局部变量导致的
  运行时失败；恢复以固定控制人口计算基线指标，并保留禁用规则变体相对控制组的 I/O、评分行和 DeepSeek
  请求节省量。同步收窄确认器可空均值的类型并公开 `CandidateConfirmationPlan`，避免严格 mypy 门禁失败。
  `Regression-Key: codex-b-historical-filter-confirmation-v1-runtime-fix`。

- `baostock-daily-core-v2-data-plane-v1`：修复供应商边界把 BaoStock `pctChg`/`turn` 百分数误当 ratio 的单位
  风险，现统一除以 100 并校验返回代码和复权标记；本地 SQLite 内容冲突不再被误分类为供应商失败。
  checkpoint/batch hash、损坏 context、孤立或冲突行、汇总计数/比例和零应有日期人口均失败关闭，停牌只有
  raw/qfq 两侧明确标记时才算取得，未知缺行不再推断为停牌。

### Verification

- `baostock-positional-query-compat-v1`：严格位置参数 SDK 桩及 BaoStock gateway/runtime 定向测试通过，Ruff、
  格式检查和 mypy 通过。2026-09-03 重新运行旧 `/tmp` 脚本（单股、1 日）仍返回供应商黑名单 `10001011`，因此
  当前无法声称真实 1800/2000 日下载或训练输入已恢复。

- `baostock-anonymous-request-rate-v1`：BaoStock runtime/application 定向测试、Ruff、格式检查和 mypy 通过；
  `/tmp` 分片数据库的脱敏错误统计显示历史请求阶段曾出现 `history:10001011`，当前匿名 `sessions=1` 探针仍
  在登录阶段返回 `supplier_login_failed_blacklisted`，因此未执行小批或 `sessions=2000`，供应商封禁解除前无法
  证明真实下载成功。

- `baostock-anonymous-login-v2`：BaoStock gateway/runtime 及诊断定向测试通过，受影响文件 Ruff
  check/format-check 通过。真实网络下旧 `/tmp` 下载脚本与修复后的 `download_history --sessions 1` 均返回
  `supplier_login_failed_blacklisted`（供应商错误码 10001011 的脱敏映射），证明当前阻塞是供应商匿名账号/IP
  状态而非登录参数或逐行查询实现；按计划未执行小批重试或 `--sessions 2000`。

- `codex-d-research-v9-baostock-gate-v1`：v9 诊断/登录定向测试 23 项通过，受影响文件 Ruff format/check
  和 BaoStock runtime mypy 通过；真实网络权限下先执行 `--sessions 1`，BaoStock 返回受控
  `supplier_login_failed_blacklisted`。由于 1 日门槛未通过，按计划未执行小批或 `--sessions 2000`，未生成
  全量 manifest、覆盖率、停牌证据或历史 `effective_at` 能力，不读取 V1/V2 预测、不做 stacking、不自动 promotion。

- 本批完整验证通过：`make format-check`、`make lint`、`make type-check`、`make test`（全量测试 100%）和 `make package`；另通过 `bash -n run.sh`、受影响 CLI/文档契约及 `git diff --check`。未执行浏览器门禁，因本批不改变活动 Web 行为。

- `codex-b-baostock-blocker-evidence-v1`：`--sessions 1` 真实 BaoStock 探针返回
  `supplier_login_failed_unboundlocalerror`；`--sessions 2000` 在任何外部 I/O 前返回
  `resource_blocked/disk_below_30gb`，`df` 显示 `/tmp` 文件系统约 8.85GiB 可用。14 项 B/运行时/文档契约
  定向测试通过；`make format-check`、`make lint`、`make type-check` 和 `git diff --check` 通过。本批仅修改
  文档与交付记录，未运行全量测试、打包或浏览器门禁（不适用）。

- `codex-d-baostock-history-integration-v1`：BaoStock 逐行归一化、raw/qfq 同键合并、WAL 分片、请求参数/路径/
  上限、CLI 显式入口和普通 CLI 懒加载契约定向测试通过；受影响文件 Ruff、mypy、`bash -n run.sh` 通过。
  使用 `.venv` 中 BaoStock 0.9.30 执行一次 `--sessions 1` 真实小批探针，供应商登录返回
  `unboundlocalerror` 类连接失败，CLI 输出有界 `failed` 状态且未生成数据行。`make format-check`、`make lint`、
  `make type-check`、`make test`、`make package`、`bash -n run.sh` 和 `git diff --check` 通过；仓库外以
  `pip --no-deps --target` 安装 wheel 后运行同一命令返回有界 `dependency_unavailable`。因此全量 2000 日下载、
  95% 覆盖和历史 `effective_at` 能力仍未通过，浏览器门禁不适用，未授予生产权限。

- `tomorrow-v3-input-compatibility-v1`：领域与应用定向测试覆盖完整 fixture、字段缺失、单位错误、身份/截止/
  主键/布局/hash 不一致、非冻结输入、生产权限拒绝、额外字段向前兼容和 port 单次只读调用；文档契约确认
  B 波次完成但 15.1.38 整节及 15.1.35 仍保持阻塞。领域、应用、BaoStock/V3 文档及架构契约通过；
  受影响文件 Ruff、format、mypy 和 `git diff --check` 通过。真实 BaoStock port/manifest 尚不存在，因此未
  声称真实输入兼容或运行训练；本批未改依赖、入口、生产评分或 Web，全量、wheel 和浏览器门禁不适用。

- `codex-c-baostock-holdout-isolation-v1`：领域与 BaoStock 计划契约测试 23 项通过；连同 Tomorrow V3
  文档及 V2 架构契约共 48 项通过。受影响的领域/测试文件 Ruff、format 和领域模块 mypy 通过；完整 diff
  Review 与 `git diff --check` 通过。本批不接供应商、运行时、Web、依赖、构建或生产入口，因此真实下载、
  `make test`、wheel 和浏览器验收不适用。

- `baostock-2000-v3-roadmap-consistency-v1`：完整 `tests/contract` 168 项通过；受影响的两个文档契约测试文件
  `ruff check` 与 `ruff format --check` 通过；活动权威文档的旧 1500 日命令、3000 日目标、`rolling_1500`、
  旧联合/stacking 和过时阻塞状态扫描无残留，完整 diff Review 与 `git diff --check` 通过。本批只修改 Markdown
  和文档契约测试，不修改依赖、入口或运行代码，因此 `make test`、`make package`、仓库外 wheel、真实下载、
  供应商和浏览器门禁不适用；这些门禁由第 15.1.38 节实现批次执行。

- `baostock-daily-core-v2-data-plane-v1`：19 项 Codex A 定向测试通过，覆盖 2000 上限、同行归一化、停牌空值、
  IPO/退市分母、200 日保留、失败续传、WAL/幂等/冲突/篡改、确定性合并、manifest/hash、历史事实不足、
  唯一切分和 B 只读描述 port；全部 research unit、相关 component、BaoStock/Tomorrow V3 文档契约及 V2 架构
  契约扩大回归通过。13 个本批文件 Ruff format/check、6 个源文件 mypy 和本批 `git diff --check` 通过。
  本批不改依赖、CLI、进程、生产评分或 Web，故完整构建、wheel 和浏览器门禁不适用于 A 独占提交。

- `baostock-1500-daily-roadmap-v1`：新增文档契约测试固定数据身份、1500 日、截止日、双复权口径、四路
  所有权、新股应有交易日分母、点时否定边界和无生产权限；本批新增及相邻 V3 文档契约 6 项、测试文件
  Ruff/format、文档 diff Review 与 `git diff --check` 通过。本批仅封存计划，未修改依赖/CLI/运行代码，
  未启动全量下载，故全量 Python、wheel、浏览器和真实覆盖门禁不适用；真实下载与 95% 覆盖仍须在
  第 15.1.38 节波次三留证。

- `tomorrow-v3-pull-conflict-resolution-v1`：`git pull --rebase` 已将分支同步到最新上游；冲突
  文件无遗留 marker，过时的独立代理入口/实现/测试未被重新引入。Tomorrow V3 文档契约
  4 项、`./run.sh help` 和 `git diff --check` 通过；本批仅修正同步冲突及文档契约测试，
  不改活动模型、配置、运行时评分或 Web，因此全量、wheel 和浏览器门禁不适用。

- `codex-b-tomorrow-joint-insufficient-v1`：联合器数据不足终态、固定 V1/V2/C3 父 profile 顺序、预测/模型/
  Holm 未建模约束、B 批次 hash 绑定定向测试通过；受影响领域/应用/测试 Ruff、mypy、format、全量 pytest、
  `make format-check`、`make lint`、`make type-check`、`make package` 和 `git diff --check` 通过。未执行
  真实训练或收益验证，因为 Codex A H1/C3 父工件仍为 `historical_data_insufficient`。

- `codex-b-historical-data-insufficient-closure-v1`：Codex B 三策略终态、父 hash 继承、空研究结果约束、
  联合 hash、不可变 JSON 工件同内容幂等、异内容冲突和篡改失败关闭定向测试 8 项通过；受影响源文件
  Ruff、format、mypy 和 `git diff --check` 通过。使用真实 Codex A 数据不足 completion 的离线输入生成终态，
  未读取历史日期或网络，`historical_data_insufficient`、`terminal_holdout_not_opened` 和
  `production_authority=false` 保持不变。真实收益确认、Holm、终端留出和浏览器验收因父工件不足或未改 Web
  而不适用。

- `codex-c-terminal-failure-closure-v1`：C 执行脚本父工件校验、三策略失败关闭、跨策略结论、不可变报告写入和
  二次幂等单元/组件测试 11 项通过；受影响 C 领域/应用/基础设施/脚本 Ruff、format、mypy 和
  `git diff --check` 通过。使用仓库外 A 工件真实执行两次均返回退出码 1，Today/Tomorrow/D25 与跨策略均为
  `historical_data_insufficient`，`terminal_holdout_opened=false`，父子 hash 一致；未运行真实收益回放和浏览器
  验收，因为父工件不足且本批未修改 Web 或生产运行行为。后续 Review 修复 C 测试 import 排序后，`make lint`
  和同一组 9 项定向测试再次通过。

- `codex-a-h1-capability-audit-v2`：领域、应用、基础设施、脚本、入口、架构和路线契约定向测试 126 项通过，
  覆盖逐来源失败、其它来源证据保留、v2 codec/hash、失败原因下游传递、代理隔离、参数类型、仓库外工件
  和无 OOF/model 终态；受影响 Python 文件 Ruff、format、mypy 及 `git diff --check` 通过。真实 `sources` 诊断在非沙箱网络下为
  `degraded`（证券主表、历史源和腾讯报价通过，Tushare 因缺 token 降级）；真实 H1 capability 命令按预期
  连续两次退出 1 并在仓库外生成 capability、标签预注册和 Codex A 终态三个不可变 JSON，全部父子 hash
  二次执行一致；腾讯成功返回 640 行，东方财富记录 `eastmoney_historical_minute_probe_failed`。
  `make format-check`（541 个文件）、`make lint`（含零严格复杂度债务）、`make type-check`（321 个源文件）、
  `make test` 和 `make package` 均通过。浏览器门禁不适用：本批没有改 Web、冻结或活动评分行为。

- `tomorrow-codex-a-prerequisite-gate-v1`：新增的 H1/Tomorrow 冲突归因回归与既有
  `research-status`/`train-tomorrow` 前置测试通过；受影响 CLI/契约文件 Ruff format/check 通过。
  随后重新运行 `make format-check`、`make lint`、`make type-check`、`make test` 和 `make package`，全部通过；
  安全空运行目录冒烟稳定返回 `status=blocked`、`next_stage=resource_probe` 及三个有界 Tomorrow H1 blocker，
  `production_authority=false`。本批未修改 Web 资源，三档桌面浏览器验收不适用；未运行真实来源下载、训练、
  两级留出或 V3 发布，H1 覆盖仍是外部未验证风险。

- `codex-a-h1-residual-c3-v1`：CodexA H1/标签/账本/C3 相关 unit/component/infra 测试 46 项通过；架构、历史路线、
  Tomorrow V3 文档和评分计划契约 33 项通过。13 个受影响文件 Ruff format/lint、7 个源文件 mypy 和
  `git diff --check` 通过。固定 LightGBM 模型重复拟合、模型文本重载、五候选 OOF、未来行隔离、过滤证据
  失败关闭、追加幂等/冲突/篡改、signed MAE 和部分标签不得提前 ready 均有回归断言。仓库外空 H1 目录
  只读审计明确 Today/Tomorrow/D25 均为 `historical_data_insufficient`、0 覆盖、0 共同交易日且留出未开启；
  `research-status` 因本机未提供 `TRADER_CONFIG` 无法核对真实运行目录。完整 `make`/wheel/浏览器门禁不适用：
  本批只新增生产隔离离线研究模块，未修改组合根、依赖、入口、配置、Web、冻结或包资源。

- `tomorrow-research-orchestration-artifact-graph-v1`：Codex D 应用/基础设施/CLI/文档定向测试 57 项通过；
  覆盖五阶段单次连续推进、缺父工件阻塞、owner/父 hash/无环校验、资源超限、同 run 恢复、不同输入新 run、
  模型与证据篡改、终态 `publishable`/人工授权分离、`run.sh`/PowerShell 薄转发和 V1/V2 隔离。并行 A/B/C
  研究单元及架构契约 57 项通过。`make format-check`、`make lint`（含零严格复杂度债务）、
  `make type-check`（316 个源文件）、`make test` 和 `make package` 均通过；`git diff --check` 通过。
  仓库外安装 wheel 后，`trader` 导入、`trader-cli --help`、`validate-config`、模板/CSS/JavaScript/图标资源
  和带已锁定依赖搜索路径的 `pip check` 均通过。`./run.sh help` 公开唯一 `train-tomorrow` 入口；无 handoff
  冒烟按预期非零退出并报告 `resource_probe_handoff_missing`、`run_id=null`、
  `production_authority=false`、`automatic_model_update=false`。三档桌面浏览器验收不适用：本批未修改 Web
  资源或生产运行行为，也未执行真实 H1 下载、训练、确认、两级留出、收益验证或 V3 发布。

- `tomorrow-v3-single-command-two-artifact-contract-v1`：两份权威文档的相关定向契约 26 项通过，覆盖
  单一目标命令、两个对外工件、内部 Parquet 证据、V1/V2/C3 原始预测联合、独立生产授权和 V3 profile
  未激活；两个受影响契约文件的 Ruff format/check（`--no-cache`）通过，`git diff --check` 通过。
  本批是文档与契约更新，未执行真实历史下载、训练、收益验证、模型发布或主程序 V3 装配。

- `codex-c-terminal-holdout-v1`：终端留出领域、三策略适配器、跨策略汇总和不可变 JSON 封存测试 9 项通过；
  受影响文件 Ruff 和 mypy 通过；点时 parity、父状态失败关闭、D25 四 horizon、稳定 hash、生产隔离、同内容
  幂等、异内容冲突、策略不匹配、未知字段和哈希篡改均有回归断言。架构与历史路线契约测试通过，
  `git diff --check` 通过。未运行真实 H1 下载、候选确认、终端收益回放或生产服务验收，因父工件和授权尚未
  提供；报告代码已保持 `historical_data_insufficient`/`historical_rejected` 失败关闭语义，真实数据证据仍待
  后续波次。

- 15.1.25 定向验证：H1 规范、能力探针、下载服务和独立 SQLite manifest 单元测试 8 项通过；覆盖同身份
  幂等、断点续传、策略隔离、未来/迟到/无时区/非 qfq 拒绝和数据库列篡改检测；受影响文件 Ruff 和 mypy
  通过，独立只读审计命令通过。空归档三策略均明确返回 `historical_data_insufficient`，终端留出未开启。
  未运行真实供应商能力探针、批量下载或收益/标签读取，因本批只封存 H1 覆盖边界；真实来源可用性仍需在
  有授权环境执行显式命令后确认。

- `codex-b-historical-filter-confirmation-v1`：Codex B 新增领域/应用单元测试 3 项通过；受影响 6 个
  Python 源文件 Ruff 与 mypy 通过；既有 Tomorrow 联合器定向测试通过；`git diff --check` 通过。
  完整架构契约未能运行，因为工作树中用户已有的 `src/trader/domain/research/terminal_holdout.py` 含未闭合
  三引号，AST 解析在进入本批模块前失败；真实 H1/收益确认、终端留出和生产服务验收不属于本批可验证范围。
  剩余风险：需先由所属 Codex C 修复该语法错误并在父工件封存后运行真实确认；本批不会自动晋级候选。

- `codex-b-historical-filter-confirmation-v1-runtime-fix`：Codex B 领域/应用定向测试 30 项通过；8 个受影响
  Python 文件 Ruff 和 mypy 通过。回归覆盖固定人口消融、资源节省量、日期对齐、共同 Holm、V1/V2/C3
  原始预测联合和生产隔离。策略文档契约通过；架构全局门禁被工作树中未纳入本批的
  `historical_residual_ledger.py` 反向导入违规阻断。未运行真实 H1 下载、收益确认、终端留出或生产服务验收，
  因父工件尚未封存；研究终态仍保持 `historical_candidate_ready`/`historical_rejected`/`historical_data_insufficient`，
  不授予生产权限。

- `four-lane-tomorrow-research-roadmap-v1`：文档契约及 C3/V3 隔离核心定向测试 31 项通过；V2 架构契约
  20 项通过；受影响 11 个 Python 文件的 Ruff lint/format 和 5 个源文件 mypy 通过；生产组合根、入口、
  Web、配置和 `run.sh` 均无新研究模块引用，`git diff --check` 通过。pytest 仅因工作区只读无法写
  `.pytest_cache`，测试本身全部通过；本批没有运行真实历史下载、训练、留出或生产服务验收。

- 15.1.24 定向验证：基线值对象单元/契约测试、受影响 Ruff、mypy、离线 `trader.entrypoints.performance`
  workload（5500 行全市场、360 候选、100 tick、无网络）通过；报告终态 `passed`，相对回归预算 5%、
  100 tick 分配增长预算 20% 和绝对资源预算均通过。最终 `make format-check`、`make lint`、
  `make type-check`、`make test`、`make package`、`make performance-check` 全部通过；浏览器和真实供应商
  运行证据不属于本批未改动的 Web/来源边界，仍由发布门禁覆盖。

- 用户要求继续推荐策略第 15 节未完成任务，并核对现有基线身份是否与生产结论一致。新增只读
  \`score_current_baseline_consistency_audit_v1\`：按来源内容 SHA-256 逐项核对活动 V1/V2 模型、
  有效策略配置、P2 历史结论、人工授权基线和两份权威文档；审计值对象固定
  \`production_authority=false\`，冲突返回 \`baseline_identity_inconsistent\`，未启动运行时明确返回
  \`live_identity_unverified\`，不会修改配置、模型、冻结、决策或收益数据。新增
  \`trader-cli research-baseline-audit\` 显式只读入口。原因是此前只有分散的状态投影，缺少在 H1
  下载和读取新收益前验证“当前活动身份”和“历史拒绝结论”是否属于同一基线的可审计 owner。
  \`Regression-Key: score-current-baseline-consistency-audit-v1\`。

- 用户要求把此前关于历史评分训练的讨论总结写入独立文档。新增 `docs/trade.md`，归纳 Tomorrow
  `daily_close_proxy` 的训练人口、硬过滤失败关闭、统一 Ridge/浅层 LightGBM 候选、60%/20%/20%
  时序切分、5 折 expanding walk-forward、成本后 local-only 收益门禁、DeepSeek 训练隔离及未来人工
  生产接入边界；文档明确自身不是权威策略，不能把收盘代理冒充第 15.1.32 节要求的 14:50 点时证据。
  `Regression-Key: tomorrow-daily-close-training-proposal-v1`。

- 用户要求依据荐股策略权威文档的未完成条目，合并整条荐股链更合理、更高效和更科学的计划。未完成
  路线新增四个同级、可独立交付的所有者：现有 V1/V2、P2 报告、人工授权与状态投影的基线身份审计；
  过滤瀑布与候选召回消融；组合净效用约束选择；评分热链决策等价与资源效率门禁。新路线还把
  DeepSeek 的预计信息增益调用分配限定为隔离研究挑战者，不允许借性能优化直接改变生产复核顺序。
  `Regression-Key: recommendation-chain-scientific-roadmap-v2`。

- 用户要求实时捕获 Web 长期显示的板块/公司风险降级、Tomorrow 0 分空仓和“最近错误 14 项”。公开状态
  新增加法的 `company_research` 白名单投影，统一 Web 诊断升级为
  `web_recommendation_health_v4`，可同时观察公司研究批次聚合、冻结快照降级，并把“零只已评分却声称
  `no_positive_net_utility`”按未冻结错误/冻结受控降级报告。`Regression-Key: web-stale-frozen-degradation-truth-v1`。

- 用户要求把硬过滤拆成两级，并完全执行“已知历史亏损、历史 ST、假账及其它权威永久负面事实的股票
  不再查询或下载逐股数据”的计划。新增类型化 `IssuerEligibilityRegistry`、追加写
  `issuer_eligibility_registry_v1` SQLite 事实库和点时解析规则；正式年度财报亏损、历史 ST/退市警示、
  权威确认的财务造假/重大违法/资金占用/违规担保/强制退市及人工永久名单可形成不可逆一级资格事实。
  新增只读 `trader-cli eligibility-list --as-of` 审计入口，以及不泄露股票身份的一级原因/完整性状态。
  `Regression-Key: two-level-permanent-issuer-eligibility-v1`。

- 用户要求把此前“历史评分优化”与 DeepSeek、更高收益目标、预测—实际误差反馈和自动调节合并成一份
  Codex 可逐节执行的权威计划。荐股策略路线现为第 15.1.21–15.1.36 节，新增全候选残差账本、历史
  DeepSeek 点时 facts 与增量消融、自适应收益/成本/严重亏损/不确定性净效用、三策略独立终端留出、
  Champion/Challenger、分级自动调节、内容寻址晋级、`next_start` 激活和回退边界。
  `Regression-Key: historical-adaptive-deepseek-scoring-roadmap-v1`。

- 用户要求基于现有优化空间制定可由 Codex 逐节执行的详细历史评分计划。现状确认当前 H0 仅有
  368/139 个训练/验证交易日，同一 139 日窗口已被多项研究观察，且公开研究组合没有 Today、D25
  各自的端到端历史留出所有者。荐股权威文档新增第 15.1.21–15.1.29 节：按独立批次依次交付 H1
  最多 1600 日点时归档、三策略标签与 60%/20%/20% 切分、嵌套 walk-forward/Holm 控制、Today、
  Tomorrow 新挑战者、D25、严重亏损概率/市场状态和跨策略终态；既有 139 日不得冒充新盲测，所有
  工件固定无生产权限且禁止恢复未来日 collector。软件业务设计只记录隔离装配和生产授权边界，避免
  形成第二套策略定义。`Regression-Key: historical-score-optimization-roadmap-v1`。

- 用户要求继续闭合此前未完成的评分验证任务，并纠正所有评分策略都应只使用历史 point-in-time 数据。
  新增 `tomorrow_v2_historical_risk_probability_v1` 完整离线链：独立类型化历史数据集、H0 验证段内有序
  60 日训练/20 日 Platt 校准/40 日检验及双边各 1 日 embargo、训练期常数概率 Brier 基线、ECE 0.05
  门禁、合法空仓日组合评价、不可变模型/报告值对象和原子防篡改仓储。数据不足只返回
  `historical_data_insufficient` 且不封存伪模型，所有终态固定 `production_authority=false`。
  `Regression-Key: historical-only-score-validation-v1`。

- 用户继续执行 `docs/plan.md` 批次 10，要求删除迁移期痕迹并完成发布级验收。最终架构契约现在验证目标
  功能包、旧路径退役、无环依赖、无兼容 shim 和计划文件退役；权威设计明确最终目录为唯一活动边界。
  `Regression-Key: functional-package-final-cutover-v1`。

- 用户继续执行 `docs/plan.md` 批次 9，要求研究、结算和入口收拢且普通生产排障不加载离线实现。
  新增 `application/research`、`application/outcomes` 的窄所有权包、结算端口和 profile 证据端口；生产共享
  的 Tomorrow P2 不可变模型工件落在 `application/ports/tomorrow_model.py`，保持原 schema、验证和 SHA-256
  identity。`Regression-Key: functional-package-research-outcomes-v1`。

- 用户继续执行 `docs/plan.md` 的下一完整未完成章节，要求把 HTTP/API/SSE 与页面资源形成专业、可审查的
  包边界。新增 `web/api` 局部所有权说明、迁移清单和架构契约，固定该包唯一拥有请求校验、显式 JSON
  投影、SSE 编码与注入的只读 Web 服务协议，并禁止反向导入基础设施、入口或组合根。
  `Regression-Key: functional-package-web-api-v1`。

- 用户要求整个工程移除含义模糊的旧英文术语，并为不同职责采用专业名称。新增 tracked 仓库契约，
  大小写不敏感检查所有 Git 路径与可解码内容，防止模块、符号、测试、文档、仓库技能或历史记录再次
  引入该词；测试自身通过分段构造检查目标，不形成自我豁免。
  `Regression-Key: precise-runtime-resource-state-naming-v1`。

- 用户追问上个详细计划及重新 Review 的问题是否全部修复，并指出“每天推荐 0 只却要求先等 20 日数据”
  不能产生有效分析证据。确认根因是既有 outcome 只结算活动档位已入选股票，未保存未入选候选或另一
  profile 的同输入预测，固定等待天数既不能补齐选择偏差，也不该阻塞当天评分。新增
  `tomorrow_v1_v2_paired_forward_v1`：异步消费同一 `TomorrowNativeInput`，V1/V2 都对全部共同可评分
  候选生成预测、成本、分数、排名和选择状态；0 只正式推荐仍保存配对。正式记录只按
  `input_versions.native` 精确绑定，T+1 后按市场等权基准、20bp 和 MAE/ATR 结算，并在达到功效后封存
  两层人工审查报告，全链始终无生产和自动切换权限。
  `Regression-Key: tomorrow-v1-v2-all-candidate-profit-evidence-v1`。

- 用户质疑“统一前置 20 日历史、长期 0 只推荐却没有分析数据”的策略合理性，并明确所有评分计划都应
  服务于提高荐股收益。权威策略新增唯一收益目标：对应持有期内可重复、成本后、相对可投资 A 股基准的
  风险约束净超额；推荐数量、页面非空、分数或工程完成均不得冒充收益。计划复核进一步删除固定未来
  20 日作为 V1/V2 历史留出、比较器实现或配对采集的启动条件，改由首个标签可见前冻结的统计功效规范
  决定最终生产切换样本量。状态 API 新增活动策略/profile 的 `history_required_sessions`，Web 与
  `web_recommendation_health_v3` 可解释逐股资格口径。
  `Regression-Key: per-stock-history-eligibility-profit-evidence-v1`。

- 用户再次反馈推荐漏斗仍为 `360 → 0 → 0`。可复用 Web 诊断子报告升级为
  `web_recommendation_health_v2`，加法输出完整漏斗阶段，以及各最多 32 项的人口过滤、候选过滤、
  候选瞬态、候选可选告警和供应原因聚合计数，不包含股票身份或逐股数据；该证据用于区分页面覆盖、
  历史门槛、人口横截面和候选评分四类不同断点。
  `Regression-Key: population-candidate-dual-watermark-funnel-v1`。

- 用户问题：推荐漏斗再次显示 `360 → 0 → 0`、过滤 89、观察草稿 0、最高分不可用，并追问交付
  Skill 为何没有阻止复发。新增共享不可变 `DecisionCoverage`，由同一个纯函数从
  `ScoredDecision` 生成 GET 与 SSE 的候选、已评估、过滤、正式、观察覆盖；新增浏览器失败先行
  回归，固定复现旧 coverage `360/0/89` 被新 SSE 结果 `360/229/89` 替换后的最终漏斗。
  `trader-delivery` 仍是仓库交付工作流，不是 Web、行情或调度器的运行时 hook。
  `Regression-Key: sse-replacement-coverage-regression-v1`。

- 用户问题：九条 `run.sh` 运维/研究命令需要手工记忆顺序，V1/V2 又只能改 JSON，日常运行容易漏阶段或
  留下配置写入。新增 `check`、`research-history`、`research-screen` 三个公开组合命令和统一
  `--profile v1|v2` 进程参数；默认 V1，V2 必须显式选择。Python CLI 统一执行组合顺序并输出
  `trader_command_group_v1` 分段退出码汇总，Linux/macOS 与 PowerShell 入口只做薄转发。
  `Regression-Key: run-command-groups-profile-selection-v1`。

- 用户问题：P1/P2 的评分差异、哪套更可能挣钱及两套方案尚未完成什么此前没有一处可审计答案。两份
  权威文档现在统一区分“生产档位 V1/V2”与不可变“历史研究身份 P1/P2”，补充因子、模型、成本、
  分歧和证据差异表，并明确当前不能证明任何一套可重复取得未来收益：V2 的平均成本后净增量证据强于
  V1，但其严重亏损、换手和 Q5-Q1 门禁失败；V1 的同口径留出也未通过。原先记录的 V1/V2/共同待办
  已由本批全候选配对链闭合，真实前向证据不阻塞或改变当前评分，也不得自动切换档位。
  `Regression-Key: tomorrow-v1-v2-profile-naming-and-evidence-roadmap-v1`。
- 用户问题：权威文档只写“P1 五个候选尚未选出唯一工件”，没有解释为何工程代码已经存在却不能直接与
  P2 切换。确认原因是 `score_tomorrow_shadow_p1_v1` 只定义五候选逐日研究与门禁，固定 2027 年窗口尚未
  发生，也不产出全局推理工件；H0 缺少原 P1 所需的历史时点行业、市值、流动性和盘中输入。文档现在
  明确区分未完成的原 P1 与人工日线 proxy。新增包内 `p1_manual_residual_momentum_v1` 线性工件，从 H0
  训练段 1,765,685 行流式拟合并绑定规范、manifest、feature contract 与 SHA-256
  `89f21552c2cd3f2addb16fa6db28f4a515991429ec287725e8c1434ee14cd1b4`；新增可重复、显式输出路径的
  `scripts/package_tomorrow_p1_model.py`，不访问网络或输出股票明细。
  `Regression-Key: tomorrow-p1-p2-configurable-profile-v1`。
- 针对用户明确要求“把现在存在但没接入的最新模型直接切换为当前评分，交易日后台自行采集后续证据”，
  新增 Tomorrow 生产模型端口、类型化横截面评分服务与 wheel 内置模型资源。启动只接纳
  `daily_reconstructible_ensemble_v1` 且校验 SHA-256
  `27034e52813f1776e2ed218c1c397f481b244fb852b01be08ddc21249d887da5`；状态公开人工授权、不可变历史
  拒绝原因、T+1 自动结算模式、禁止自动更新和逐股亏损概率未建模。新增 1/3/5 日收益、20/40/60 日
  skip-5 动量、20 日平均成交额与 Amihud 输入，以及允许负预测值的不可变模型诊断对象。
  `Regression-Key: tomorrow-p2-manual-production-activation-v1`。
- 针对用户选择暂缓密钥配置、修复 688981 历史为空并要求继续解释未来评分晋级，统一运行诊断新增
  `--history-source composite|tencent|eastmoney`。历史、来源、live 和 full profile 可从同一公开入口
  拆分生产组合路由与单一供应商，仍只输出聚合行数、错误计数和延迟，不泄露股票价格或外部载荷。
  `Regression-Key: tencent-qfq-equivalent-day-history-v1`。
- 针对用户要求把荐股评分 Review 扫描问题先写入权威文档再修复，新增 V4 六类结构化风险到本地规则的
  领域归一化契约、`deepseek_v4_local_rules_2026_08` 有类型策略版本、决策 epoch 同批选择限制值对象，
  以及覆盖风险映射、配置漂移、veto、零权重维度和 epoch 自校验的回归门禁。
  `Regression-Key: scoring-policy-integrity-review-v1`。
- 针对用户要求将 `review.md`、`fenshu.md` 归并后删除，扩展文档单一真相源契约：两份旧问题/评分计划
  文件必须不存在，证券主数据来源职责、P2 终态及未来新候选入口必须由两份权威文档直接定义。
  `Regression-Key: authoritative-doc-retirement-review-score-plan-v1`。
- 针对用户要求继续完成 `docs/fenshu.md` 下一整节 P2-1，新增显式
  `research-tomorrow-p2-screen` 离线执行链：只读复用 H0 qfq 归档，按同日市场/板块/成交额暴露残差化
  六个 Alpha 字段，使用训练段内部时间留出完成固定 ridge/单线程 LightGBM 的唯一一次训练，再在正式
  验证段评价固定候选。新增类型化逐股证据、模型工件、终态报告、原子幂等防篡改仓储，以及
  `v2_research_readiness_v4.tomorrow_p2` 只读状态投影。
  `Regression-Key: score-tomorrow-historical-p2-screen-v1`。
- 针对用户要求继续完成 `docs/review.md` 的 Web 解释优化，`V2SupplyFunnel` 新增达到观察线/正式线的
  精确计数；桌面验收脚本加法支持系统缺少 geckodriver 时使用无头 Chrome DevTools，继续由同一参数化
  入口捕获三档分辨率和本地零外网夹具证据。`Regression-Key: web-recommendation-state-explanation-v1`。
- 针对用户要求先解决 `docs/review.md` 中基础资料长期停在 120/360、并追问“已有四五个股票数据接口为何
  仍缺数据”，新增沪深交易所官方证券主数据适配器：独立采集上交所主板/科创板和深交所 A 股代码、
  板块、交易所与上市日期，只接纳代码唯一、两所齐全、受支持三板不少于 4000 条且上市日期完整的
  原子快照。新增不可变来源健康状态以及统一诊断 `security-master` profile，报告只输出沪深/板块行数、
  完整率和有界延迟，不泄露股票明细或外部载荷。
  `Regression-Key: security-master-official-exchange-fallback-v1`。
- 针对用户要求继续 `docs/fenshu.md` 下一个完整未完成章节，新增不可变
  `score_tomorrow_historical_p2_v1` 与类型化 `score_tomorrow_historical_p2_report_v1`。P2-0 只读绑定
  H0 规范，冻结字段准入矩阵、唯一日线可重建线性/LightGBM 集合、固定模型随机种子与单线程、稳定
  Top6/合法空池、20/50/100bp、300 配对、区块 bootstrap、尾部风险、换手和集中度门禁；P0/P1 证据
  明确排除，报告只能决定是否可另立前向预注册，始终没有生产权限。
  `Regression-Key: score-tomorrow-historical-p2-contract-v1`。
- 针对用户要求避免 Web 推荐漏斗同类故障再次被误判，新增 `trader-delivery` Skill 专用事故手册，固定
  宿主网络可达性、逐阶段刷新、时区归一化、漏斗语义、冻结窗口和当前 release 重启六个检查点。手册
  归档本次已确认的多层根因与诊断陷阱：沙箱 `connection_failed` 不能证明服务未运行，行情/特征发布
  成功不能证明 `V2RefreshOutcome` 已构造，混合 UTC/上海时间的 `max` 会保留胜出对象原时区，
  `refresh:value_error` 只是定位线索，`candidate_quotes_pending` 与
  `security_master_coverage_incomplete`/合法末级 0 必须分开解释，Today 冻结控制也不能误作数据丢失。
  新契约确保 Skill 入口持续路由该手册且关键检查点不会被静默删除。
  `Regression-Key: recommendation-funnel-incident-playbook-v1`。
- 针对 `docs/fenshu.md` 批次 5，新增隔离的 `score_tomorrow_shadow_p1_v1` 预注册与前向证据链：冻结
  residual reversal、residual momentum、session decomposition、cost/risk adjusted 与 constrained
  ensemble 五挑战者、40+20 精确窗口、线性/LightGBM 50%/50% 集合、300/100 同日同股配对下限、
  20/50/100bp、3/5/10 日区块、10,000 次 bootstrap 与固定五成员 Holm 家族。日历确认、逐日证据和
  历史/前向/合并报告均原子防篡改封存；终态报告绑定日历确认、精确逐日证据 manifest，并分别保存
  三档成本的全部区块结果，前向证据和报告必须绑定已通过并已封存的历史门禁哈希。
  `Regression-Key: score-tomorrow-shadow-preregistration-v1`。
- 针对 `docs/fenshu.md` 批次 4，新增研究专用 `score_tomorrow_cost_aware_selection_v1`：完整消费批次 3 影子预测，对 Tomorrow/D25、expanding/rolling_252、linear/LightGBM 每折逐股形成成本后净效用、门槛、入选排名和跳过原因，并由独立工件库按选择规范/父报告原子防篡改封存。`Regression-Key: score-tomorrow-cost-aware-selection-v1`。
- 针对 `docs/fenshu.md` 批次 3，新增研究专用 `score_tomorrow_shadow_models_v1`：在同一 Tomorrow/D25 点时特征、结算标签和 20bp 成本上运行正则化线性控制组与真实浅层 LightGBM，按 expanding/rolling_252、1/25 日 embargo 生成 `score_tomorrow_shadow_report_v1` 完整逐日逐股预测，并由独立工件库原子防篡改封存。`Regression-Key: score-tomorrow-shadow-models-v1`。
- 针对 `docs/fenshu.md` 批次 2，新增研究专用 `score_tomorrow_point_in_time_features_v1`：固定计算短期残差反转、中期残差动量、隔夜、日内和尾盘五类特征，以 R2 input hash 与类型化上下文 hash 双重绑定结果，并保留逐字段 missing mask。`Regression-Key: score-tomorrow-point-in-time-features-v1`。
- 针对 `docs/fenshu.md` 批次 0 的点时证据缺口，新增 `v2_committed_research_audit_v2`：local 观察一次保存完整股票池的板块/行业、历史 ST、上市/退市身份、结构化公司风险、外部风险事实、来源时间和输入时间，hybrid 只引用人口哈希。新增按策略、交易日及默认 14:50 截止的类型化 SQLite 读取。`Regression-Key: score-point-in-time-population-v1`。
- 针对用户要求把 `scripts/` 中能合并的诊断一次执行并删除合并后的无用脚本，新增内部
  `scripts.runtime_diagnostics` 包。Web、历史、腾讯、Tushare 和 Firefox 探针按职责保留独立实现，统一
  复用标准输出 JSON 边界；历史与腾讯探针进一步共用 nearest-rank 延迟统计。统一 CLI 新增
  `web/history/tencent/tushare/browser/performance` 精确 profile，既可一次 `live/full` 扫描多边界，也可
  在定位后只复测一个边界。`Regression-Key: diagnostic-wrapper-consolidation-v1`。
- 针对用户要求继续 `fenshu.md`“批次 1：建立原生因子诊断层”，新增离线
  `score_native_factor_diagnostics_v1` 与不可变 `score_factor_diagnostic_report_v1`。报告绑定 R2/R3 父哈希、
  研究规范和类型化市值/流动性维度，原生输出每日 IC/Rank IC、ICIR、五分组 20/50/100bp 净超额与
  单调性、覆盖/缺失、1/3/5 日衰减与换手、板块/行业/市值/流动性分层、严重亏损、MAE/ATR20、Q5 正贡献
  集中度，以及生产 Top120 剪枝前后的 oracle recall。`Regression-Key: score-native-factor-diagnostics-v1`。
- 针对用户要求把 `scripts/` 中可组合的现场检查收敛为一次执行并按既有计划生成防回归 Skill，新增
  `scripts/diagnose_runtime.py` 与 `trader-runtime-diagnostics-v1` 脱敏聚合契约。`runtime`、`sources`、
  默认 `live` 和显式 `full` 四档 profile 分别覆盖运行 Web、真实供应商、两者组合及 Firefox/离线性能，
  单项失败后仍继续扫描其他边界。新增仓库级 `$trader-delivery` Skill、影响矩阵、运行诊断路由和交付
  证据清单，并通过项目元数据允许隐式触发。`Regression-Key: delivery-diagnostics-orchestration`。
- 新增文档单一真相源契约：直接验证 `docs/V2.md`、`docs/implementation-plan.md` 和
  `docs/start_stop.md` 不再存在，并要求 V2-only 边界、日常运维命令、正式发布状态、失败的
  `score_p0_v2` 证据及下一项原生评分因子诊断 Gate 由两份权威文档承接。
- 针对用户确认 Tushare 已达到 120 积分、每日可调用 8000 次，新增只读
  `scripts/sample_tushare_daily.py`。脚本复用生产配置和 Token 边界，逐证券实测行数、延迟、raw 标签、
  120 分权限及进程内调用计数，不输出 Token、价格或完整外部响应。
- Tushare 类型健康状态及 `/api/v2/status.market_data.sources.tushare` 加法公开 120 积分、
  `unadjusted_daily`、50 次/分钟、8000 次/日、进程内当分钟/当日尝试、进程内估算余量和本地限流次数；
  `process_*` 命名明确区分供应商跨进程总账。
- 针对用户要求按评分收益优化计划执行证据链批次，新增不可变 `ScoreResearchCoverage` 与窗口覆盖值对象；
  纯领域评估按固定计划日、带时区上海时钟和 14:50 截止计算已记录、已错过、最大可达天数、下一计划日、
  完成和可恢复状态，不把截止前的当天或未来日期提前判为失败。
- committed trace 新增只读、类型化的逐交易日首个观测时间探针；活动研究覆盖只接纳 14:50 或更早的
  committed observation，迟到事件仍保留在不可变档案，但不能让已经失败的固定日期恢复资格。
- `research-status` 公开契约升级为 `v2_research_readiness_v3`，活动历史/前向窗口加法公开
  `missed_trade_dates`、`maximum_attainable_trade_dates`、`next_planned_trade_date`、`complete` 和
  `recoverable`，继续保持只读、无网络、无评分和不创建运行文件。
- 针对用户再次反馈 `history warmup batch exceeded its deadline` 并要求直接运行脚本抓现场数据，新增
  `scripts/sample_history_sources.py`：按生产 worker 波次实测腾讯主源/东方财富回退、可用行数与请求延迟，
  可在显式仓库外目录对同一批真实 K 线比较逐条和批量 SQLite 事务；既有 Web 健康脚本同时记录 warmup
  覆盖、计划、完成、失败与在途轨迹，并在失败计数增长时给出确定性发现。
- `/api/v2/status.market_data` 加法公开历史预热退避数、唯一失败数、timeout 累计、在途年龄、批次 deadline
  和最后来源；Web adapter 继续只投影有界聚合，不泄露股票代码、供应商错误文本或原始载荷。
- 针对用户现场出现 `history warmup batch exceeded its deadline`，新增不可变
  `HistoryWarmupPolicy` 及其纯构建函数，统一约束预热批大小、实际历史 worker 数、单来源 timeout 与
  batch deadline；组合根不再自行拼接含队列波次数的魔法公式。
- 针对用户要求重新扫描并优化“数据采集 → 评分推荐 → Web 展示”全链路实时性，新增类型化
  `V2RefreshOutcome` 与跨策略共享的不可变评分输入 epoch：每次行情、候选、研究和分钟尾巴刷新
  都携带数据版本、变化代码、完成时间和降级事实，调度器只对真实变化触发评分；状态 API 新增有界
  `market_changes` 与端到端 `latency_waterfall` 聚合，不公开股票代码、关联 ID 或原始样本。
- 决策 SSE 新增 `patch_schema_version=2` 的有界完整替换投影。统一索引在决策和初始报价 overlay 原子
  提交时生成与 GET 一致的 projection ETag，事件只复制入选 TopK 的渲染字段；浏览器可直接应用推荐
  patch，只有游标、schema 或身份不一致时才回退完整 GET。
- 针对全工程 Review 发现行情 health 在内部字典、状态对象和公开 JSON 之间反复转换，新增不可变
  `MarketGatewayHealthStatus`、`MarketSourceHealthStatus`、`SecurityMasterHealthStatus` 与
  `TushareHealthStatus`。网关、预热和来源协调只读取类型字段，只有最终 `MarketDataHealth` adapter
  按公开字段白名单投影 JSON；架构 AST 同时约束 `health()` 与 `status()`，防止内部状态退回
  `dict`/`Mapping` 根值。
- 新增 `V2RuntimeIssueRegistry`、独立 DeepSeek/冻结 adapter 模块和浏览器
  `dashboard_stream.js`。运行时问题去重/恢复、模型/冻结资源、SSE 游标/退避/断线轮询分别拥有清晰
  生命周期，结构契约禁止这些职责重新并回调度器、行情输入 adapter 或页面控制器。

- 针对用户要求重新 Review 全工程并按计划修复，新增正式记录基础设施 codec 与架构 AST 契约：领域、
  应用层只持有当前 schema 的 `CommittedDecisionRecord` 类型对象，JSON 编解码、字段白名单、版本和哈希
  校验统一归属 `infra/persistence/decision_record_codec.py`；契约禁止领域/应用重新引入持久化 decoder。

- 针对用户要求继续优化“行情采集 → 过滤/评分 → 推荐发布 → Web 展示”实时链路，新增
  `tencent_topk` 紧急来源 lane、`eastmoney_intraday` 分钟尾巴 lane 和三个短线策略独立
  DeepSeek hybrid latest-wins lane；`/api/v2/status.scheduler.hybrid_lanes` 加法公开其有界运行状态。
- 冷启动全市场与候选采集完成时立即形成不含股票身份的
  `candidate_quotes_pending`/`scoring_pending` 输入质量快照，使推荐漏斗能区分“采集中、待评分”与
  已确认的业务零值。

- 针对用户将活动源码单文件上限调整为 1200 行，根 `AGENTS.md`、权威架构文档和 AST 机器契约统一采用
  1200 行硬上限；接近上限仍按职责、耦合和可测试性 Review，不再以 500/800 行机械拆分制造聚合模块。
- 新增 tomorrow/d25 的 14:49:20 策略级检查点调度、窗口内失败重试，以及 `/api/v2/status.scheduler`
  的 cadence、控制执行器和冻结计数投影，固定时点是否 pending/inflight/retry/completed/missed 可直接观察。

- 针对用户反馈 Web 荐股数据异常、推荐漏斗经常显示 0，新增只读、参数化的
  `scripts/check_web_recommendation_health.py`。脚本连续采样 status 与三个短线 current，输出不含股票
  身份的 `web_recommendation_health_v1` JSON，定位候选、特征、证券身份、历史、评分持续归零或回退，
  input quality 消失、release/schema 和 status/current 身份不一致；实际生产归零根因仍待真实交易窗口
  采样确认，本批不把尚未复现的原因写成事实。

- 针对 Review 指出的 35 秒 Web 保留窗口不能替代实时采集 cadence，新增活动组合根装配的
  `CadencePlanner`、按物理任务隔离的 latest-wins lane 和 Tomorrow 午后分钟 tail 任务；全市场、
  候选、TopK、tail、评分、long、冻结与收盘恢复分别按真实最近到期时间推进，任务状态进入 status。
- 新增统一决策/正式记录 schema v2，固化不可变 anchor quote、setup、downside、研究覆盖、复核终态和
  `selection_diagnostics`；活动 codec 只接受当前 schema，旧 v1 数据只能随完整旧 release 离线保留。
- 新增禁止外网且直接调用活动生产函数的 `performance-check` CLI、Linux/Windows 启动脚本入口、
  `scripts/run_production_performance.py` 与 Makefile 门禁，固定覆盖 5500 行双源全市场、360 候选、
  三策略评分、overlay CAS、API/ETag/status、SSE、100 tick RSS、环境身份和 5% 相对回归。

- 针对用户要求把“内部用对象、JSON 只在边界使用”的统一规则写入代理指令，根 `AGENTS.md` 新增
  “JSON 与类型对象边界”强制章节，明确类型状态根、动态键 `Mapping`、输入解析、输出白名单投影、
  禁止对象自序列化方法、公开 schema 演进和 AST 门禁豁免条件。

- 针对用户指出内部状态在 JSON 字段与对象字段之间反复转换，新增活动源码序列化边界 AST 契约：除显式
  DeepSeek 可观测性端口/投影外，内部 `status()` 必须返回真实类型，且应用层不得再定义 `as_dict()`、
  `to_status()` 或 `to_json()`。线程池、来源 lane/registry、cadence、延迟瀑布和缓存现在都有不可变
  状态根对象；DeepSeek 缓存也返回类型状态。来源、数据集等动态键仍以
  `Mapping[Key, StatusValue]` 表达，不伪装成固定 JSON schema。

- 针对用户反馈观察池再次为空、Web 数据新鲜度/行情覆盖/推荐漏斗卡片状态反复消失，新增不可变
  `V2InputQualityStatus`、供应漏斗和行情摘要应用层状态值，以及 overlay 发布/失败计数。状态端口现在
  是调度器装配的必需契约，HTTP 只执行显式脱敏 JSON 投影，不能因对象缺少可选方法而静默返回空状态。
- 扩展既有 Firefox 桌面 runner，真实等待浏览器从冷启动状态迁移后断言“352 / 360”、行情与身份缺失
  构成、“360 → 65 → 0”、过滤/观察草稿/最高分和腾讯行情来源，并继续覆盖三档桌面分辨率。

- 针对用户要求按实际“采集到 Web”刷新结果留出稳定余量，运行配置 schema 10 新增
  `api.web_snapshot_retention_seconds=35`。生产组合根将该值注入根页面，浏览器诊断同时公开实际读取的
  毫秒值，配置缺失或非正数会在启动校验阶段拒绝，页面不再自行维护生产保留周期。
- 扩展既有 `scripts/measure_web_refresh_interval.py`，新增 `--runtime-config` 并在 v2 报告中同时输出
  配置保留窗口、DOM 最大刷新间隔、剩余余量和覆盖结论，后续可直接复测而无需另写临时脚本。

- 针对用户要求保留本轮有用实测、避免后续重新编写临时脚本，新增参数化
  `scripts/measure_web_refresh_interval.py` 和 `scripts/sample_tencent_quotes.py`：前者使用活动生产
  调度、统一索引、SSE、Flask 与 Firefox 测量刷新到 DOM 的真实墙钟间隔，后者重复采样腾讯公开
  行情并区分请求延迟、接收时间和来源版本是否变化；两者默认只向标准输出写结构化 JSON。
- 新增可复用诊断脚本契约，检查两个脚本均有稳定 `--help`/`--output` CLI、不得依赖测试夹具，并确保
  协作规则持续要求把可复用诊断固化到 `scripts/`。

- 针对全仓 Review 发现“生产只装配统一调度器，但活动树仍保留仅由测试引用的 Today/Tomorrow
  策略专属运行时”这一结构漂移，新增生产可达性架构门禁：应用层运行时模块必须由唯一组合根
  `bootstrap.py` 显式装配；退休文件恢复、权威文档重新引用旧运行时或漏接 overlay SSE 均会失败。
- 新增统一 `V2DecisionBuilderPort.refreshed_overlay()` 边界和冻结后报价回归，覆盖 afternoon lane
  复用同批统一行情更新 Today 正式入选股票、CAS 并发冲突、正式决策身份不可变及 overlay SSE 发布。

- 针对用户看到 `./run.sh [serve|validate-config|research-*...]` 后无法判断“一堆参数干什么”的问题，
  新增可执行帮助契约：`./run.sh help` 与 PowerShell 帮助按日常命令、显式离线研究和高级配置分组，
  每个子命令说明是否启动服务、是否只读、是否下载或封存研究工件，并明确日常启动无需任何参数。

- 针对用户质疑“身份缺失为何非要 Tushare”新增免费证券身份闭环回归与可观察状态：
  `/api/v2/status.market_data.security_master` 只公开内存身份总数、上市日期/交易日龄完整数、免费来源
  标识、持久化调度错误数和 `tushare_required=false`；行情覆盖卡在身份尚未补齐时明确显示
  “免费行情+交易日历补齐中”，不再把缺少外部 token 当作身份缺口原因。

- 针对用户现场看到“行情缺失 0 · 身份缺失 302”却无法判断含义的问题，新增报价抢跑迟到回归和
  身份缺失构成展示契约。回归覆盖新浪先返回、东方财富晚于报价截止时间完成、当前报价不被迟到
  结果覆盖、完整证券身份进入独立身份仓并由独立 `reference` lane 写入 SQLite，且不新增第二次
  物理请求。

- 针对用户现场看到“明天观察池：本轮未形成观察草稿，请查看运行状态”，新增午间冷启动恢复回归和
  观察池四态契约。测试覆盖只恢复缺失的 tomorrow/d25/long、Today 永不午间追补、DeepSeek 调用为 0、
  同日 current/空草稿成功后不重复、活动 lane 不追加重复请求，以及刷新未形成输出时可在下一 tick 重试。
- 新增 `V2DecisionBuilderPort.has_local_draft()` 类型化只读边界，使调度器能区分“没有产物”和“正式
  发布门禁未过但本地草稿已经形成”，不再把草稿状态藏在输入适配器内部。

- 针对用户反馈观察池长期停在“正在生成观察草稿”且修改反复引入新问题，新增进程内 release 握手：
  `v2_status_v2.release` 同时公开已加载的 DecisionView schema 与 Web asset revision，浏览器再对每份
  current/history 响应执行第二层 schema 校验。当前真实服务的三个 current 仍为
  `v2_decision_view_v1`，而工作树已是 v2，现被稳定识别为旧后端与新静态资源混版。
- 新增失败关闭的浏览器 release contract 模块和回归：缺少模块、status v1、DecisionView v1、决策
  schema 不同或资源 revision 不同均进入受控 `release_contract_mismatch`，不再伪装成行情采集、评分
  或观察草稿生成。
- Web 应用现在在 `create_app()` 阶段把模板和全部打包静态资源固化为进程内只读 release 快照；旧进程
  不再从修改中的工作树热读新 JavaScript/CSS/HTML。未知资源保持 404，已知资源保留内容 ETag、
  `no-cache`/`nosniff`，且应用工厂仍无线程、网络、数据库和文件写入副作用。

- 针对用户再次反馈观察池消失、Web 数据新鲜度/行情覆盖/推荐漏斗等卡片无状态，新增独立的
  `UnifiedDecisionDraftIndex` 和 `v2_decision_view_v2.draft` 只读边界。评分已经完成但输入质量尚未
  达到正式发布门槛时，只保存最新同日观察草稿；正式 `items`、`UnifiedDecisionIndex`、SSE、冻结、
  历史、归档和收益结算均不接纳该草稿。
- 新增真实冷启动迁移浏览器回归：初始服务只有 360 条候选行情且没有 `input_quality`/草稿，第二次
  15 秒状态轮询发布草稿并自动重读 current；Firefox 必须从“采集中”迁移到两行按分数排序的观察池，
  不再用预先填满的静态夹具跳过故障窗口。

- 针对用户反馈 Web 的数据新鲜度、行情覆盖和推荐漏斗在短线 `not_ready` 时全部失去状态，新增失败
  先行的应用聚合、HTTP 脱敏、JavaScript 摘要和 Firefox 真实浏览器回归；覆盖空 current 与仍在推进的
  360 只候选、行情/身份缺口、评分/过滤/观察草稿、最高分及预算每日上限同时存在的场景。

- 针对用户确认“免费全市场证券主数据实际只持久化候选子集”的剩余断点，新增失败先行回归：共享
  输入必须同时传递有界候选代码和本轮完整全市场身份代码；参考加载器需保存候选外证券身份；
  SQLite 批量接口需用单个写事务完成多股主数据，并在任一同刻冲突时整体回滚。
- 新增 `/api/v2/status.deepseek` 安全投影契约，只允许公开启用、已配置、最近物理调用数和受控零调用
  原因；测试注入的密钥和外部载荷必须被丢弃。

- 针对用户反馈 `./run.sh` 无法启动，新增“物理损坏 SQLite 数据平面”失败先行回归：覆盖基础设施端口
  将驱动异常转换为受控不可用，以及组合根在数据平面不可恢复时仍按既有 fail-open 契约继续启动。

- 针对用户反馈近 20 日推荐与观察池供给稀少、DeepSeek 长期未参与，新增不泄露股票身份的
  `scheduler.input_quality.*.supply_funnel`、`supply_reason_counts` 和 `primary_blocker`。诊断逐层
  区分请求候选、候选特征、证券主数据、有效历史、过滤 disposition、完整评分、DeepSeek 可审集合、
  可执行/观察动作与最终两池；DeepSeek 状态回归同时锁定启用、配置、物理调用数和零调用原因。

- 针对用户反馈“近 20 日有效推荐稀少、观察池寥寥无几且观察标的亏损”，新增 Score-P1 生产输入
  覆盖门禁和只读聚合诊断。状态 API 的 `scheduler.input_quality` 按策略公开请求候选、定向特征、
  证券身份和历史摘要的数量/比例及受控降级原因，不公开股票代码，也不在 HTTP 请求内抓取、评分
  或调用 DeepSeek。

- 针对用户要求预注册“排名持续性/分数平滑/换手惩罚”并保留收益与回撤改善，新增隔离的
  `score_r6_daily_stability_v1`。固定绑定已封存 R6D 报告与候选，三类机制使用 26 个有限候选，
  训练/复用诊断分别重置跨日状态，继续使用相同 Top6、单板 4 只、20bp 和 5 日标签口径。

- 新增 `research-r6-stability-screen`、`score_r6_daily_stability_report_v1` 追加式防篡改报告和
  `research-status.score_r6_stability` 摘要；父报告/候选缺失、篡改或身份不符时 fail-closed，长计算
  开始前向 stderr 输出反馈，且不启动生产、网络、Web、DeepSeek、冻结或后台线程。

- 针对用户要求继续基于历史日线优化趋势/收益评分，新增隔离的 `score_r6_daily_trend_v1`：从既有
  H0 前复权归档点时重建 60→5 日残差动量、趋势效率、20 日下行稳定、60 日回撤恢复和流动性，
  预注册 48 个候选并统一回放真实 Top6、单板最多 4 只、20bp 成本及未来 5 日标签。

- 新增 `research-r6-daily-screen`、`score_r6_daily_trend_report_v1` 追加式防篡改报告和
  `research-status.score_r6_daily` 摘要。离线命令在首次长计算前输出进度，缺少覆盖时不创建研究目录，
  已封存报告可幂等读取；篡改或同身份不同内容 fail-closed。

- 针对用户要求“观察池高分排在上面”，新增查询层排名恢复回归和 Firefox 两行观察池桌面断言；fixture
  特意让内部代码顺序与评分顺序相反，并要求 Tomorrow/D25 页面均显示 `74.00` 在 `72.00` 之前。

- 针对用户反馈“DeepSeek 最近没有使用”并要求不影响现有 Web 和荐股实时性，新增 V2 类型化公司研究
  意图、独立生命周期运行时和 `company_research` 内存状态。状态可观察新输出/周期研究提交、批次、
  冷却、退避、短路与重评分计数，但不公开股票集合、外部载荷或密钥。

- 用户要求继续 `docs/implementation-plan.md` 的下一完整未完成章节；本批完成 Score-R7 人工晋级档案
  工程能力。根因是 Score-R6 只封存历史/前向资格制品，尚无可供人工逐项审查且能从原始逐日证据
  复算的不可变档案边界。新增 `score_r7_promotion_dossier_v1`、显式
  `research-r7-dossier --research-identity ...`、`runtime_dir/score-r7` 追加式存储和只读状态摘要。

- R7 档案固定绑定 R6 历史报告、前向 spec、20 个逐日 manifest、最终报告、交易日历、规则、配置/
  策略、数据 schema、活动策略、融合、引擎、统计程序与冻结候选哈希；同时列出三板六组件权重、
  动作门槛、风险扣分、十项逐门禁实际值/阈值、失败日、样本、两项消融、集中度、剩余风险，以及
  20/50/100bp × 3/5/10 日九组确定性非循环区块敏感性。

- 用户要求继续 `docs/implementation-plan.md` 的下一完整未完成章节；本批完成 Score-R6 历史参数筛选与
  新前向 Gate 的预注册能力。新增固定候选网格、训练期唯一择优、验证期冻结复核、全局与三板块权重
  选择、低样本板块回退、真实生产六因子权重映射，以及不可变历史报告、20 日前向逐日证据与最终报告。

- 新增 `research-r6-screen` 离线入口和 `research-status.score_r6` 真实制品状态：只有 H0 覆盖率达标才
  能封存筛选结果；前向规范绑定 H0/R6 报告、候选、交易日历、规则、配置、策略、融合和 schema 哈希，
  并以 100 组同股配对及固定 10,000 次五日分块 bootstrap 分别评价 local 和 hybrid 晋级资格。

- 用户要求继续 `docs/implementation-plan.md` 的未完成任务；启动审计发现上一批 Score-H0 已在工作树
  标记完成但尚未 Review、提交或推送，因此本批只闭合该整节，不把下一节 Score-R6 混入同一提交。
  新增固定 `score_h0_v1` 规范、最多 5 worker 的有界/可断点历史下载、独立 SQLite 归档、只读状态、
  `ohlcv_cross_section_v1` 训练/验证诊断，以及 `research-history-download` / `research-backtest` 入口。

- H0 报告现在绑定规范、股票池、逐股历史内容、聚合归档、固定训练/验证日期、20bp 成本、实现版本与
  报告 SHA-256；生成诊断前逐行复算归档哈希，内容篡改、同键冲突或完成集合不一致均显式拒绝。

- 用户确认修复后的 `./run.sh serve` 已运行并要求继续未完成计划。由于旧 `score_p0_v1` 缺失的是已
  过去窗口的点时输入、运行服务不能解除该 Gate，本批在任何新窗口收益可见前预注册独立
  `score_p0_v2`：固定 2026-08-21 至 2026-10-23 的 40 个历史计划交易日、2026-10-26 至
  2026-11-20 的 20 个前向计划交易日、`20260820` bootstrap 主种子及规范 spec SHA-256。

- 新增研究身份全链回归：R2 extraction、R3 baseline、R4 五挑战者、R5 bootstrap/统计报告/final
  report/forward binding 必须携带同一 identity/spec hash；新身份使用显式 v2 schema 和独立前向
  目录，旧 v1 日期、随机流和不可变证据不能混入。

- 针对用户已经运行 `run.sh` 一整天却仍被告知“数据采集不够”，新增只读 `research-status` 运维入口：
  同时报告 committed observation 分区/legacy 计数、日期覆盖、20GB 载荷容量、固定研究窗口、R6
  阻塞原因，以及 outcome 基准/完整结果计数；缺少数据库时不创建目录、文件或网络请求。

- 新增正式冻结推荐的 SQLite outcome 证据仓储与盘后结算接线，按正式记录、股票和 horizon 保存
  T+1/T+2/T+3/T+5 收益、全市场等权基准、20bp 净超额、MAE/ATR20 和结构化质量状态；相同业务
  结果跨重启幂等，不同内容冲突，临时观察/结算时间不改变不可变业务身份。

- 针对用户要求在控制台点击地址直接打开 Web，新增浏览器 URL 格式化与 IPv4/IPv6 入口回归；IPv6
  自动使用方括号，避免输出无法解析的地址。

- 针对用户要求 Web 启动前显示访问地址，新增入口生命周期回归，锁定监听端口成功绑定后、Web 服务
  线程启动前只输出一次浏览器访问提示。

- 针对用户反馈 Tomorrow/D25 观察池“最新价、今日涨跌、成交/换手、总市值”全部为空，新增评分项
  同批不可变报价锚点、完整 `DecisionQuote` 领域值及其规范哈希/正式记录兼容序列化；新增决策与
  初始完整 overlay 的单临界区 CAS 发布，以及领域、投影、查询、HTTP、调度和桌面回归。

- 针对用户要求数据新鲜度使用 HMS 表示，新增单一纯时长格式器和边界回归，覆盖负数/非法值、秒、
  分钟、小时及超过 24 小时的年龄；Firefox 三档验收同时读取实际卡片文本并要求匹配 HMS。

- 针对用户要求去除“候选覆盖”与“推荐漏斗”的重复信息，新增前端行情完整性诊断：按当前策略完整
  展示名单统计具备有效价格、涨跌幅、来源和来源时间的股票，并独立给出行情缺失与名称/行业身份
  缺失数量；新增纯函数、摘要渲染、历史报价 overlay 同步刷新和三档桌面回归。

- 针对用户要求“修复必须选择长期最优方向，不要用最小运行链规避必要重构”新增协作策略契约测试，
  固定根因分析、目标架构、跨模块或全仓重构许可，以及实现范围与风险验证范围相互独立的规则。

- 针对用户反馈“最近错误显示 6 项、观察池为空且股票名称和行情列大量为 `—`”新增共享参考数据
  调度、免费证券主数据持久化、生产交易日历上市交易日投影、午后 Today 调度和预期冻结拒绝回归；
  Firefox 桌面门禁新增 Long 股票名称、行业、价格、涨跌幅、成交额、换手率、市值、来源和时间均
  不得为空的真实行断言。

- 针对用户要求“系统健康合并到最近错误，并以高效、漂亮、便于定位的布局展示”，新增最多 20 条的
  进程内结构化错误历史：按策略/原因合并重复失败，保存严重度、阶段、首次与最近发生时间、次数、
  活动/已恢复状态和恢复时间；状态 API 只返回脱敏受控字段。页面新增“查看全部”错误抽屉，可区分
  活动与已恢复记录，并复制诊断代码；Firefox 禁止剪贴板时自动选中代码供手工复制。

- 针对用户反馈“重启后三个评分策略观察池同时消失、列表只有代码而名称显示 `—`”新增真实刷新时间
  推进、三策略共同构建、名称/行业身份往返、正式记录 current-schema 往返、current HTTP 与运行版本状态回归。
  状态 API 现在公开脱敏 scheduler 摘要，可直接区分刷新失败、决策失败和仍在运行的旧代码。

- 针对用户报告的“今早/明日长期 `not_ready`、2–5 日只有空观察池且候选数异常”新增生产输入并发、
  三板限额、刷新失败保留、状态诊断、覆盖去重和正式记录往返回归。测试锁定同一观察点三条评分策略
  只执行一次全市场与候选报价刷新，且任一过滤原因重叠不会再放大候选/拒绝统计。

- 用户要求继续 `docs/implementation-plan.md` 的下一个完整未完成章节。本批完成 Score-R5 工程能力：
  新增 `score_r5_statistical_gate_v1` 固定统计器，按五个 R4 变体分别形成 local-only、hybrid 相对
  production 及 hybrid 相对 local-only 报告；配对移动区块 bootstrap 固定主种子 `20260811`、
  3/5/10 日非循环区块和每项 10,000 次，20bp、5 日单侧 p 值始终保留五成员 Holm 家族。

- 新增历史与最终门禁，统一从 R4 同日同股结算行派生三档成本、严重回撤及配对区间、候选召回、
  删除最佳月份/板块、正向贡献单股/前五集中度、五分组、Rank IC 和高低分组回撤差。历史不是恰好
  40 日、配对不足 300 条或任一证据门禁失败时以结构化原因终止，不得进入前向。

- 新增固定 2026-11-02 至 2026-11-27 二十日的前向 collector、`score_r5_forward_day_v1` 不可变
  JSON 仓储和 `score_r5_final_report_v1` 封存器。记录绑定历史门禁、变体/参数、数据、规则、配置/
  策略、融合、统计和报告身份；同键同内容重放幂等，不同内容或篡改冲突，失败日和 `no_decision`
  不得被盈利日替换。最终分别保留历史身份、前向报告和 40+20 合并门禁，并重跑固定 Holm 家族。

- 用户要求继续 `docs/implementation-plan.md` 的下一个完整未完成章节。本批完成 Score-R4：新增
  `score_r4_preregistered_parameters_v1` 不可变机器 manifest、五个独立挑战者版本及生产纯领域
  评估适配端口。连续入场 11 条线性端点、主板/成长板高热带和弱收盘/尾盘/回撤阈值均在收益比较前
  固定；coverage shrink、active-set 候选扩展和四项合并只形成离线研究 override。

- 新增 production/local-only/hybrid 同日同股配对 manifest。每行绑定同一 R2 active-set、R3 baseline
  和 `CostSettlementBasis`，未选中侧权重为 0、入选侧等权；hybrid 只接受 R2 已记录的结构化 facts，
  没有 facts 时强制为 local control copy。报告绑定参数、输入、日级和五变体规范 SHA-256。

- 用户要求继续 `docs/implementation-plan.md` 的下一个完整未完成章节。本批完成 Score-R3：新增
  `HistoricalBaselineReplayEvaluator` 隔离端口和 `score_r3_baseline_report_v1` 离线回放器，显式消费
  R2 不可变日证据及生产纯领域回放结果，不复制过滤、评分、风险或选择公式，也不接入组合根、HTTP、
  Web、冻结或 DeepSeek。

- 新增 production baseline 与 active-set oracle 双排名校验，以及 20/50/100bp 日组合净超额、平均
  MAE/ATR20、严重回撤率、候选召回率、最终组件字段覆盖率、板块/行业集中度、五分组 20bp 净超额和
  平均日内 Spearman Rank IC。报告 JSON 使用规范 SHA-256 不可变封存，相同内容重放幂等，不同内容、
  schema/hash 篡改或既有身份冲突均拒绝。

- 用户要求继续 `docs/implementation-plan.md` 的下一个完整未完成章节。本批完成 Score-R2：新增
  `score_r2_historical_v1` 离线两阶段提取器、研究专用纯领域覆盖率收缩/候选与最终分乐观上界、
  每板生产 Top120 起始集和约束感知 active-set。提取结果逐日保存真实覆盖身份、正式池/观察池
  裁剪证明、完整字段、三板结算与规范 SHA-256，不读取供应商、不调用 DeepSeek，也不接入生产
  组合根、HTTP、冻结或 Web。

- 新增 `score_r2_partition_v1` Polars 不可变 Parquet 分区：按日拆分身份、三板覆盖、硬拒绝聚合、
  紧凑/完整/评估候选、日线、分钟线、共享复权窗口、结算和证明；每个文件及日级/顶层 manifest
  均可复算 SHA-256，相同内容重放幂等，不同内容、文件篡改或 manifest 篡改显式冲突。

- 新增短线 current/观察池与 Today 封口回归：Today、Tomorrow、D25 均接受统一 API 的
  `view=current`，Tomorrow/D25 在 `midday` 可渲染 `observe`，Today 午间、冻结、历史和盘后继续
  关闭观察池；Today 调度线程在 `11:20:00` 秒内带微秒延迟仍冻结不晚于边界的最新 current，
  `11:20:01` 后仍永久禁止追补。

- 桌面 Firefox 发布 runner 新增真实形态的 `ready + observe` fixture，三档布局验收前分别切换
  Tomorrow 与 D25，强制检查观察池未隐藏、摘要计数和带股票代码的观察行，避免只验 Long 而漏掉
  短线观察池回归。

- 新增代码 `603083` 风险组件同观测时刻重复提交回归，覆盖 `penalty` 在内的八个组件均保持
  SQLite recent 首次提交结果，后续冲突不覆盖、不丢失且不误报为持久化失败。

- 新增盘中当日日 K 历史持久化回归，覆盖 `observed_at` 早于当日 15:00 时仍可写入代码
  `301717`，并同时锁定上一交易日记录继续使用 15:00 来源时间。

- Score-R1-Migrate 新增 V2 committed observation 研究链：Today、Tomorrow、D25 成功提交后携带
  同批 `v2_committed_research_audit_v1`，独立 SQLite 保存硬拒绝聚合、硬过滤通过候选、板块/行业、
  候选组件、缺失掩码、覆盖率、板块可靠度、生产 Top120 及 production_local/research_shadow 配对。

- 新增生产 `V2MarketDataAdapter` 的临时无效空集/合法业务空集回归，以及 Web 首屏策略选择和
  Long `view=current` 身份回归；覆盖行情过期不得发布、ST 等业务过滤允许合法空、当日有条目
  策略优先展示和短线 `live/official` 与 Long `current` 的隔离校验。

- 新增 15:00 后冷启动与已有同日 current 两条调度回归，以及历史预热不得抢占候选历史请求的并发
  回归；测试分别锁定 Tomorrow/D25 收盘兜底、Long 当前投影、Today 禁止追补和共享 history lane
  的 latest-wins 所有权。

- V2-E11 新增可重复执行的 Firefox 桌面发布 runner，精确校准并验收 1280x720、1440x900、
  1920x1080 三档视口，检查根页面可见性、纵向顺序、页面级横向溢出、Long 侧栏/表格重叠、
  三个长期分类、股票行和浏览器异常，并输出脱敏 JSON 与截图证据。

- V2-E10 完成旧生产链物理删除：唯一组合根现在直接装配 V2 数据适配器、调度器、统一决策记录、
  committed event observer、冻结协调器和只读 API/SSE；旧 Pipeline、RecommendationSnapshot、
  publisher/query/replay、snapshot 仓储/迁移器、shadow/cutover 接点及旧 Web/API/SSE 资源不再位于
  活动树。研究侧只接收 V2 committed event，不再读取旧 snapshot/baseline。

- Long 页面保留固定名单界面，不再被通用荐股表替代：三个顶层 Tab 固定为“卡脖子行业”“高成长赛道”
  和“低价潜力股”，行业分组与股票顺序继续来自长期名单，V2 current 只覆盖行情字段，不展示评分
  或荐股动作。

- V2-E9 收敛唯一入口与运行命名空间：默认 `config/v2/runtime.json` 现在只使用
  `.runtime/v2`，server lock、初始化、恢复和关闭均以配置中的 V2 runtime 为根；新增入口契约
  覆盖配置、CLI、启动脚本和进程锁边界。

- V2-E8 新增统一 `UnifiedDecisionQueries`、`UnifiedDecisionEventStream` 和只读 Web 外壳。today、
  tomorrow、d25、long 现在共同使用 decisions current/history/dates、status 与 events；正式日期
  由 V2 仓储倒序有界读取，current/history 支持 ETag，SSE 使用跨策略单调序列、有界历史、
  有界客户端队列、显式游标恢复、重同步和慢客户端隔离。

- 新增权威文档一致性负向契约，锁定“当前迁移状态”与“最终发布契约”的区分、Today/Tomorrow/D25
  唯一冻结边界、V2 决策类型，以及迁移流水账和已退役术语不得重新进入两份权威文档。

- V2-E7 新增独立 `LongV2Runtime` 与 `LongRefreshRequest`。固定池定向行情现在由单 worker、单
  latest-wins lane 直接生成统一 `LongProjection` current；投影按 `long_watchlist.json` 顺序
  携带完整名单、唯一分组、价格、涨跌幅、成交额、换手率、总市值、来源、来源时间及
  `live/retained/missing` 状态，schema 升级为 `v2_long_projection_v2`，运行状态固定公开
  `score_status=not_applicable`。

- V2-E6 新增 `D25NativeInput`、策略独占的 D25 latest-wins runtime、14:49:20 检查点、14:50
  正式冻结、15:00 `close_fallback` 和只读 `UnifiedScoredDecisionQueries` 实例。D25 复用统一
  纯领域选择/融合核心与正式记录仓储，但 current、sequence、observer、错误状态、事件、
  冻结唯一键和查询均按策略隔离。

- V2-E5 新增 `TodayNativeInput`、`TodayV2Runtime`、11:20 精确冻结协调器和正式记录报价
  overlay。Today 现在直接复用统一纯领域选择/融合核心生成 local/hybrid `ScoredDecision`，
  使用独立 latest-wins worker 与 observer，并与 Tomorrow 共享按策略隔离的统一索引、仓储和
  DeepSeek 预算链。

- 评分研究权威契约新增候选覆盖率收缩和乐观上界公式、约束感知 active-set 召回证明、五个
  挑战者的隔离行为、同日同股组合贡献、非循环移动区块 bootstrap、Holm step-down、固定
  前向封存状态、第二轮权重收缩和 `PromotionDossier` 人工晋级边界。

### Changed

- 公开启动语义收敛为无参数启动 Web，删除 `serve`/`app` 别名；`run.sh` 与 `run.ps1` 仅公开 `check`、`download_history` 和 `train-tomorrow`。旧研究执行器从 CLI 分派和 bootstrap 组合根移除，`research-status` 继续只读展示不可变的历史审计字段。
- 将 BaoStock 活动模块的通用 JSON 编解码辅助提取到 `baostock_daily_codec.py`，主模块降至 1200 行以内，数据 schema、hash 校验和 SQLite 行为保持不变。

- BaoStock 权威计划从未产出正式工件的 1500 日 v1 方案升级为 `score_baostock_daily_core_v2`：截至
  2026-08-31 的最近 2000 个交易所开市日内，每只股票最多保存 2000 个 `(code, trade_date)` 逻辑记录，
  未复权和前复权字段同行保存；新股、退市股和来源不足股票只保存真实区间。旧 H1 v1 的 1600 日能力审计
  及数据不足终态保持不可变。BaoStock 规划为 `[research]` optional extra，普通 Web wheel 和启动链不导入或
  隐式调用 SDK。

- `codex-a-h1-capability-audit-v2`：能力脚本默认先使用 `trust_env=false` 的有界 HTTP session，连接失败时
  再以相同请求语义回退到系统代理会话；请求参数只接受字符串或字符串元组，供应商原始价格载荷不进入
  输出或工件。能力 artifact 与脚本投影 schema 均升级到 v2，以显式公开 `probe_failures`，旧 v1 工件不再
  被新 decoder 静默接受。

- `tomorrow-daily-close-training-proposal-v1` 只新增非权威研究说明，不改写第 15.1.21–15.1.34 节现行
  路线、活动 V1/V2、硬过滤、风险、固定 68/32 融合、动作、Top6、14:50 冻结、API 或 Web。文档把
  “历史收盘训练”和“未来生产接入”拆为两个独立授权批次，生产接入前必须先解决与 14:50 点时契约的
  差异，禁止由研究报告自动创建 `v3` 或修改配置。

- 用户要求按“收益大、难度低、被依赖者优先”重新 Review 第 15.1.21–15.1.40 节，并删除不可靠、难以
  完成的荐股策略自动优化。确认旧路线把历史 DeepSeek 盈利回测、模型族搜索、组合优化、概率模型、
  自动训练/晋级/激活集中在末段，实施和证伪成本显著高于本地个人研究看板的预期收益；仅靠点时证据也
  无法排除当前大模型训练语料已知后续结果。权威路线现收敛为第 15.1.21–15.1.34 节：基线身份审计后
  立即执行决策 hash 等价的热链提速，再按分策略 H1、标签、全候选账本、顺序无关过滤消融、每策略最多
  8 个透明候选、统一 Holm 家族和三策略独立留出推进。H1 的 Today 数据不足不再阻塞 Tomorrow/D25；
  合法空推荐的效率成本也改按评分 epoch、候选、正式决策和 DeepSeek 应用分别统计。
  `Regression-Key: historical-score-roadmap-priority-pruning-v1`。

- 评分研究权威路线由第 15.1.21–15.1.36 节重排为第 15.1.21–15.1.40 节；总序 15.1.21 明确标记为已
  封存、15.1.22 保持已完成，并保留既有 H0/R6/P2 身份及未变的 H1 计划身份，不覆盖历史报告。每次
  “继续”仍只执行下一个完整未完成同级章节，顺序固定为
  基线一致性、H1、预注册、全候选残差、过滤/召回、DeepSeek、收益/成本/风险、净效用、组合选择、
  嵌套时序、三策略终端留出、风险稳健性、等价性能、自动挑战者、受控晋级和最终生产授权。软件业务
  设计同步明确研究隔离与第 15.1.37 节生产性能所有权。`Regression-Key: recommendation-chain-scientific-roadmap-v2`。

- Web 已登记 `strategy_history_coverage_partial`、`structured_risk_unavailable`、
  `cross_source_deviation` 和 `history_data_degraded` 的精确中文说明，不再把这些已知事实合并为“部分数据
  暂不可用”。错误详情标题分别显示活动问题与已恢复记录，避免把冻结快照降级、当前运行错误和已恢复
  历史简单相加成“当前错误 14 项”。公开状态 schema 升级为 `v2_status_v13`，静态握手升级为
  `release-contract-2026-09-01-v14`。统一 Web 诊断把外部 JSON 解析独立为类型化契约模块，完整保留公司
  研究协调器的运行、排队、退避、短路、预算、周期提交与重评分聚合，同时保持单文件低于 1200 行。

- 两份权威文档已把硬过滤固定为“一级永久资格过滤 -> 二级动态硬过滤”，并在历史自适应路线中插入已完成
  的第 15.1.22 节；原 H1 及后续章节顺延至 15.1.23–15.1.36。全市场批量结果先登记一级事实再发布，
  历史预热、候选定向行情、逐股公司研究、参考数据、Long 和分钟行情均在提交前裁剪已知排除代码；
  DeepSeek 只能看到过滤后的候选。正式年度财报端点有界读取最多 500 行并保存点时可见历史及覆盖状态，
  研究缓存保持旧载荷兼容；点时资格查询按事实一次构建排除集合，复杂度为 O(事实数+候选数)，不会随名单
  扩大退化为逐股扫描全部事实。状态公开契约升级为 `v2_status_v12`，静态握手升级为
  `release-contract-2026-09-01-v13`。

- 原第 15.1.21–15.1.29 节未完成路线不再只做一次性历史调权，而是由单一 15.1.21–15.1.36 顺序承接
  H1、标签、残差、DeepSeek、模型、选择、三策略留出、风险、自动研究和最终生产授权。自动调节只允许
  历史离线批量运行：20 个新增成熟日期只能触发挑战者，L1 至少需要 60 个新增不重叠日期，L2 每次需要
  新的独立 200 日未开启终端留出；盘中热切换、同日在线学习、自动 profile 切换和无门禁晋级仍被禁止。
- 软件业务设计同步把路线索引更新为第 15.1.21–15.1.36 节，并明确第 15.1.23–15.1.34 节保持研究隔离，
  第 15.1.35 节注册表默认不接入组合根，第 15.1.36 节必须取得明确生产接入指令并执行完整高风险门禁。
  当前运行继续 `automatic_model_update=false`，本批没有改变评分、DeepSeek 请求、配置、API 或 Web。

- 确认根因不只是文档仍保留跨年计划，而是 R5/R6/P1 的未来日 collector、运行期 V1/V2 全候选配对、
  outcome 补充结算和 R7 晋级档案仍在生产组合根、调度 observer、SQLite 与状态 API 中形成第二条评分验证
  状态源。评分验证现统一只读封存 H0 历史归档；线上 T+1 只结算正式冻结推荐并用于运行监控，不进入训练、
  校准、门禁、自动调参或切换。Score-R6 历史唯一验证另立 `score_r6_historical_v2`/
  `score_r6_historical_report_v2`，不把旧 v1 的 `forward_required` 工件重新解释为新报告。
- `research-screen` 新增 V2 历史风险验证第六阶段，`research-status` 升至
  `v2_research_readiness_v7` 并只读投影模型/报告终态。删除运行期比较字段后 `/api/v2/status` 升至
  `v2_status_v11`，静态资源握手升至 `release-contract-2026-09-01-v12`；内部类型新增字段不会绕过显式
  Web 投影。生产评分公式、DeepSeek 168 次预算、动作阈值、冻结记录及人工 V1/V2 授权不变。

- 完成最终功能包切换：删除权威设计中的迁移台账和 `docs/plan.md` 一次性计划，保留 Changelog 与历史报告
  作为交付证据；最终运行、研究、Web、入口和基础设施目录直接由架构契约与窄入口测试守护。未改变公开
  API、评分、融合、冻结、持久化 schema、配置或运行时业务行为。
  `Regression-Key: functional-package-final-cutover-v1`。

- 确认根因是 CLI、bootstrap 和 research 包初始化在普通导入时聚合离线筛选、回放和模型训练；CLI 导入
  58 个、server 导入 49 个研究模块。研究用例现在只在显式 `research-*` 命令函数内加载，bootstrap 的离线
  服务依赖移入构建函数，server 只保留权威后台证据消费者；研究包初始化不再执行聚合导入。普通生产依赖图
  因此不加载离线训练实现，公开 CLI 参数、命令、退出码、研究状态 JSON、结算行为和工件 hash 保持不变。
  `Regression-Key: functional-package-research-outcomes-v1`。

- 确认根因是路由、serializer、SSE 响应和 Web 服务协议仍平铺在 `web` 根目录，且 blueprint 组合另有一个
  根级 facade，API 所有权与模板/静态资源展示边界混杂。本批将四个 API 模块统一迁移到 `web/api`，
  blueprint 创建收为包内私有实现，`register_routes()` 成为唯一注册入口；`web/app.py` 继续只负责应用
  创建、release 快照和一次注册调用。URL、schema、ETag、SSE cursor、四策略视图、release identity、
  评分、冻结、供应商、DeepSeek 和持久化行为保持不变。
  `Regression-Key: functional-package-web-api-v1`。

- 确认根因是同一个笼统名称同时指代 latest-wins 单运行/单等待队列、进程资源启停编排和 cadence 固定
  调度点状态，职责边界不清。队列模块统一为 `latest_wins.py`，进程资源模块统一为
  `resource_orchestration.py`，组合根使用 `ApplicationResources` 与 `_application_resources()`；调度点统一为
  `SchedulePointStatus`、`SchedulePointState.status` 和 `schedule_point_status()`。公开 schedule point JSON
  仅返回 `status`，status schema 升至 `v2_status_v10`，静态资源握手升至
  `release-contract-2026-08-31-v11`，权威设计、迁移计划与交付技能同步更新。

- 用户继续执行功能拆包计划，要求完成运行时、调度与生命周期包迁移。确认根因是 cadence、schedule、
  worker、source lane、latest-wins、shutdown、runtime status 和市场输入组装仍平铺在 `application` 根目录，
  生产组合根、infra 适配器、诊断与测试广泛依赖旧路径；本批将 11 个调度/生命周期模块迁移到
  `application/runtime`，将 `v2_input_runtime.py` 迁移到 `application/market_data`，并为两包增加局部
  所有权导航。共享 `cache.py` 保持根级应用契约，未复制或隐藏外部客户端构造。三策略 scoring/hybrid lane、
  数据 lane、latest-wins、single-flight、冻结重试、30 秒共享停止 deadline 和状态/API/SSE schema 保持不变。
  `Regression-Key: functional-package-runtime-market-data-v1`。
  Verification：cadence/schedule/workers/input/resource/runtime 定向测试、调度集成、E3/bootstrap/app-factory、
  功能包和架构契约通过；`make format-check`、`make lint`、`make type-check`、`make test`、`make package`、
  `make performance-check` 和 `git diff --check` 通过。性能门禁 `passed`、网络调用 0、内存增长 0%。沙箱外
  `diagnose_runtime.py --profile live` 中交易所主数据 5212 条、历史 3/3、Tencent 3 条通过；Tushare 缺 token
  降级，本地 Web 服务未运行而无法采样 status/current。
  Residual Risks：未对当前 release 执行上午热运行、午间冷启动、11:20、14:50、15:00 后及正式记录命中/
  收盘恢复的真实五时段矩阵，也未取得运行中 Web/API/SSE/浏览器一致性现场证据；这些门禁不能由离线测试
  冒充，后续可用服务窗口或最终 release 批次需补齐。供应商现场实测消耗仅为诊断请求，无业务写入。

- 用户继续执行功能拆包计划，要求完成应用层推荐与决策包迁移。确认根因是推荐评分/冻结用例和统一决策索引、
  查询、事件、SSE、overlay 能力仍平铺在 `application` 根目录，导致 Web、bootstrap、runtime、persistence 和
  测试依赖旧路径；本批将九个推荐用例迁移到 `application/recommendation`，九个决策模块迁移到
  `application/decisions`，并新增 `DecisionIndexPort` 解耦冻结协调器与决策索引实现。旧路径物理删除，Long
  projection 仍独立，DecisionView、ETag、projection version、正式/观察分池、冻结投影和合法空集行为保持不变。
  `Regression-Key: functional-package-application-recommendation-decisions-v1`。
  Verification：批次定向决策/冻结/投影测试、E2/E4/E5/E6/E7 contract、功能包边界和架构契约通过；
  `make format-check`、`make lint`、`make type-check`、`make test`、`make package` 和 `git diff --check` 通过，
  wheel 清单包含两个新应用子包，活动源码、测试和脚本中的旧导入路径搜索结果为 0。
  真实 `scripts/diagnose_runtime.py --profile full --output -` 与浏览器现场诊断未执行，本批未改变供应商 I/O、
  冻结时序或 Web schema，现场延迟和外部服务可用性仍为未验证风险。
  Residual Risks：后续批次仍需维持新应用边界并完成 runtime、Web、研究和最终切换；既有供应商预热长尾和
  overlay 性能长尾不属于本批范围。

- 用户继续执行功能拆包计划，要求完成推荐领域的过滤、评分、风险融合与选择包迁移。确认根因是推荐域
  八个模块仍平铺在同一目录，且 `scored_fusion.py` 运行时反向依赖 `scored_selection.py`，阶段所有权和
  依赖方向难以审查；本批将过滤迁移到 `domain/recommendation/filtering`，板内评分迁移到 `scoring`，
  风险与融合迁移到 `risk_fusion`，稳定排名与选择迁移到 `selection`，并把跨阶段不可变选择结果类型
  收拢到共享 `models.py`。旧路径物理删除，评分入口、九组权重、50 分/30% 候选门槛、本地风险单次扣除、
  固定 68/32 融合（验收向量 83.40）、78/73 动作门槛、TopK 和板块/行业集中度行为保持不变。
  `Regression-Key: functional-package-recommendation-stages-v1`。
  Verification：推荐领域定向测试、推荐包边界契约、`make format-check`、`make lint`、`make type-check`、
  `make test`、`make package` 和 `make performance-check` 全部通过；性能门禁网络调用为 0，评分热路径
  在预算内。真实 `scripts/diagnose_runtime.py --profile sources --output -` 未执行，本批未改变供应商 I/O，
  现场供应商延迟和降级行为仍为未验证风险。
  Residual Risks：后续应用层推荐/决策包迁移仍需保持共享模型边界，Web 与运行时下游将在后续批次切换。

- 用户继续执行功能拆包计划，要求完成行情历史、参考数据与服务编排包迁移。确认根因是历史缓存/预热、
  交易日历/证券主数据和 gateway/service 协调模块仍平铺在 `infra/market_data`，导致 worker、lane、缓存
  与参考数据所有权边界不清；本批将历史能力迁移到 `infra/market_data/history`，参考数据迁移到
  `infra/market_data/references`，服务编排迁移到 `infra/market_data/service`，并将门面改为
  `service/facade.py`。所有生产、测试和诊断导入已切换，旧路径物理删除；历史预热独占调度、reference/
  Tushare/实时/历史/emergency lane 隔离、deadline 分段、缓存身份、持久化次数、失败保留最近有效快照和
  状态/API 行为保持不变。`Regression-Key: functional-package-history-reference-service-boundaries-v1`。
  Verification：定向历史/参考/服务/lane/调度与架构契约测试通过；`make format-check`、`make lint`、
  `make type-check`、`make test` 和 `make package` 全部通过，wheel 清单确认包含三个新子包；真实
  `scripts/diagnose_runtime.py --profile sources --output -` 未执行，供应商现场延迟和降级仍为未验证风险。
  Residual Risks：未在真实外部供应商环境验证本批运行时诊断，后续批次仍需维持新包边界并覆盖下游应用/Web 迁移。

- 用户继续执行功能拆包计划，要求隔离行情供应商 I/O 与规范化职责。确认根因是 provider、解析/合并、
  特征和字段质量模块平铺在 `infra/market_data`，供应商故障与字段问题难以按边界定位；本批将供应商适配器
  迁移到 `infra/market_data/providers`，将规范化、合并、字段质量、缓存身份和特征物化迁移到
  `infra/market_data/normalization`。所有生产、诊断脚本和测试导入已切换，旧同级路径物理删除；来源回退、
  单位/复权、时区、merge epoch、缓存身份和状态投影行为保持不变。新增 provider/normalization 反向依赖
  契约。`Regression-Key: functional-package-market-data-boundaries-v1`。
  Verification：通过行情规范化、合并、路由、字段质量、供应商、网关、Tushare、架构和全部 contract 测试，
  以及 `make format-check`、Ruff、mypy、全量测试、`make package`、仓库外 wheel/CLI 资源验收。
  `scripts/diagnose_runtime.py --profile sources` 未执行真实供应商请求，避免在重构批次消耗配额；现场供应商
  延迟与降级行为保留为未验证风险。

- 用户继续执行功能拆包计划，要求完成配置与组合包迁移。确认现状是配置实现平铺在
  `infra/settings*.py`，导致组合根、入口、DeepSeek 和测试直接依赖多个内部路径；本批将八个配置模块
  迁移到 `trader.infra.settings`（`models`、`parser`、`credentials`、`market_policy`、
  `factor_validation`、`strategy_validation`、`runtime`、`loading`），并由包入口显式导出稳定加载函数和
  公共类型。所有生产、脚本和测试导入已切换，旧路径物理删除；配置 schema、默认值、凭据优先级、有效
  哈希和 `create_app()` 无副作用行为保持不变。`Regression-Key: functional-package-settings-cutover-v1`。
  Verification：通过配置单元、bootstrap、入口、应用工厂、架构及全部 contract 测试，并通过
  `make format-check`、Ruff、mypy、全量测试、`make package` 与仓库外 wheel/CLI 资源验收。
  Residual Risks：后续行情、运行时和 Web 包仍依赖当前旧目录，须按计划后续批次继续迁移；本批未改变这些
  能力的运行语义。

- 用户要求按 `docs/plan.md` 开始功能拆包重构。确认根因是现有分层方向正确但 application、行情基础设施和
  Web 能力仍平铺，缺少可审计的目标包所有权与迁移顺序；本批冻结 `domain`、`application`、`infra`、`web`
  层内功能包布局，并登记批次 2-9 的显式旧模块到目标包台账。新增功能包边界契约，验证目标包声明、
  每个迁移源的唯一批次/目标、既有旧链退役、允许依赖方向和无循环导入；未移动生产模块、未改变运行行为、
  API、策略、冻结或发行形态。`Regression-Key: functional-package-migration-boundaries-v1`。
  Verification：定向通过 `tests/contract/test_v2_architecture.py`、
  `tests/contract/test_authoritative_document_consistency.py` 和
  `tests/contract/test_functional_package_boundaries.py`；Ruff/mypy、全量测试、打包和浏览器门禁不适用，
  因本批仅修改架构契约、计划和文档。Residual Risks：后续目录迁移尚未执行，运行包边界和 wheel 外安装
  仍需在对应批次验证。

- V1/V2 比较不再使用任意 20 日前置条件。只读 H0 同口径留出固定 139 日、687,321 条配对及报告 hash
  `47e2b9bfd4d404521f8251e2e51c491aa96c1bc0d8423dea95e63320daa6e3bf`；V1 的 20bp 平均日净增量
  `-0.250480%`、bootstrap 下界 `-0.813048%`，没有晋级收益证据。以 H0 日级 V2−V1 标准差
  `3.831660%`、50bp 最小经济差异、双侧 5% 和 80% 功效预注册 522 个独立日；每个有效日须完成全部
  共同候选结算且至少含 300 条 `complete` 配对；这只
  约束未来生产切换，评分、全候选配对与首个 T+1 标签从当前即可运行。
- 组合根仍只有配置选中的一个 profile 进入活动决策链；两套封存评分器只进入异步研究消费者，不调用
  DeepSeek、不改动作/冻结/配置。`research-screen` 新增 V1/V2 H0 留出成为第五阶段，研究状态升级为
  `v2_research_readiness_v5`；公开状态升级为 `v2_status_v9`，加法投影后台维护的配对证据内存快照。
  显式 `research-status` 才只读检查 SQLite，HTTP 请求不访问数据库。

- 历史就绪从“候选至少 99% 具备统一 20 日历史，否则整批不发布”改为逐股、逐策略/profile 资格：
  Today 与当前启发式 D25 使用登记的 20 日摘要，Tomorrow V1/V2 明确要求 61 个 qfq session 及模型
  字段，公开历史合格计数也必须同时满足所选 profile 字段而非只数 session。覆盖率只作为健康指标；
  已有合法分数必须继续发布，历史不足股票显式跳过，全部不合格才保持
  `transient_invalid_empty`。公开 status schema 升至 `v2_status_v8`，静态握手升至
  `release-contract-2026-08-31-v10`。

- Today、Tomorrow、D25 的共享原生评分现在分别使用全市场发现批次水位与最终候选评分水位：
  `preselect_max_age_seconds` 只审计人口批次，`score_max_age_seconds` 只审计候选原始行情。慢历史、
  研究或分钟增强不再把已完整接纳的同一人口批次倒算为过期；候选过期、99% 历史覆盖、硬过滤、
  风险、DeepSeek、动作、融合、TopK 和冻结契约均未放宽。

- decision/overlay 行级 patch schema 从 v3 升至 v4，decision 完整替换必须携带完整 coverage，静态资源
  握手升至 `release-contract-2026-08-31-v9`。浏览器只在 coverage 六项均为非负整数、计数关系合法且
  `selected_count` 与完整 upserts 一致时应用 patch，否则按既有协议请求 current 重同步。
- Python 3.10 目标 mypy 对第三方 NumPy 2.5（仅支持 Python 3.12+）stub 的 `type` 语句无法解析；沿用
  LightGBM/Polars 的第三方边界策略跳过 NumPy stub 展开，并以仓库内最小不透明 stub 隔离第三方语法；
  项目自身的真实类型与公共边界仍完整检查，不把活动源码目标版本从 3.10 偷换为当前解释器版本。

- Tomorrow 默认生产档位由 V2 改为用户指定的 V1；`./run.sh`、`serve`、`check` 及研究组合均接受统一
  `--profile`。启动覆盖不写回 `strategy.json`，但参与有效策略 SHA-256、模型装配、性能身份和新决策
  模型身份；纯 H0/R6/P2 历史阶段继续绑定不可变研究规范，不受活动档位改名或重算。
- `run.sh`/`run.ps1` 公开表面收敛为 `serve`、`check`、`research-history`、`research-screen` 和参数化
  R7 档案。底层九个 `trader-cli` 阶段继续保留供契约测试、自动化和故障定位；普通门禁非零不会截断
  同组合后续阶段，操作性异常仍立即失败关闭。

- Tomorrow 活动评分命名从 P1/P2 迁移为 V1/V2：策略 schema 升至 15，
  `tomorrow_scoring_profile` 只接受 `v1|v2` 且默认 `v2`；类型化端口、组合根装配、状态/API 和测试使用
  同一枚举，旧 `p1|p2` 配置失败关闭。V1 生产模型身份改为 `v1_manual_residual_momentum_v1`，内容 hash
  改为 `4291ea514c233a14ab6f9262e72ea541d1e9a794e73d02f10f8220509f6f502b`；V2 的不可变 P2 历史来源工件
  及 `p2_*` 特征字段保持原名和原 hash。公开 status schema/静态资源握手同步升为
  `v2_status_v7` / `release-contract-2026-08-30-v8`，既有冻结记录不回写。
- 策略配置升级到 schema 14，新增唯一 `tomorrow_scoring_profile=p1|p2`，默认保持 `p2`；配置参与策略
  SHA-256，切换后必须重启。组合根和离线性能入口只装配所选包内工件，P1/P2 共用同一类型化评分服务，
  不并行打分、不热切换、不回退旧 Tomorrow 分；模型 ID/hash 继续进入决策身份，既有冻结记录不可覆盖。
  状态/API 新增 `profile_id`，P1 显式报告 `historical_unavailable` 和 proxy 原因。评分组件改为 profile
  中性的 `model_net_utility_rank/model_confidence`，Web 文案由“P2信号分”改为“模型信号分”。Status
  schema 与静态资源握手同步提升为 `v2_status_v6` / `release-contract-2026-08-30-v7`。
- Tomorrow 本地 `base_score` 现由封存 P2 ridge/LightGBM 50%/50% 集合的成本后净效用横截面分产生；
  Today、D25、Long、结构化本地风险、固定 68/32 融合、78/73 门槛、Top6/行业/板块约束和 14:50
  冻结保持不变。20 个未来交易日不再阻塞当前模型使用，只由既有 15:00 后 outcome 结算器持续积累
  T+1 净超额、MAE/ATR20 与严重回撤证据，且不会静默调权、重训、回切或覆盖冻结结果。
- Decision/Status/SSE patch 公开契约分别提升为 `v2_decision_view_v3`、`v2_status_v5` 和
  `patch_schema_version=3`，静态资源握手提升为 `release-contract-2026-08-30-v6`。Tomorrow Web 新增
  P2 信号分、预测超额、估算成本、预测成本后净超额、模型分歧和评分版本；原始模型诊断不再伪装成
  0–100 评分分量，旧正式记录缺少该可选对象时仍可只读恢复。GET 与 SSE 替换事件均携带同一
  `input_versions.score_model`，浏览器不会把策略标签误当成模型版本。
- 活动策略标签提升为 `strategy_review30_top6_observe6_riskmap_2026_08`，风险映射版本进入策略内容哈希，
  防止旧缓存或旧决策身份复用新映射语义；内部决策 epoch schema 提升到 `decision_epoch_v2`，内容哈希
  新增正式/观察容量、单行业上限和单板比例。固定 68/32 融合公式、78/73 门槛、API、Web 与冻结不变。
- 旧问题记录中的数据源解释、Web 指标展示前置条件和当前运行边界已按产品职责归入软件业务设计；旧评分
  计划中的六类建模问题、P2 失败终态和新候选准入规则已按策略职责归入荐股策略文档。根因确认不是
  权威契约缺少主体技术内容，而是一次性记录仍留在活动文档树且策略现状摘要未随 P2 终止更新；本批
  采用语义去重归位，不把历史流水原样拼接为第二套定义。
- P2-1 使用真实 `.runtime/v2` H0 归档复核 4,904/5,006 只完整历史（97.9624%），形成 368 个训练日、
  139 个验证日和 678,370 条验证同日同股配对。候选的 20/50/100bp 平均净增量与 20bp bootstrap 下界
  虽为正，但严重亏损率 15.9472% 高于 H0 历史代理 8.2734%、换手增加 56.2350 个百分点、Q5-Q1 为负，
  因而按预注册门槛不可变封存为 `historical_rejected`。P2-2/P2-3 已取消；没有改评分、78/73 门槛、
  DeepSeek、68/32 融合、冻结、活动配置、API 或 Web。
- 推荐主区现在按冻结错过、采集中、数据门禁阻断、评分完成空池四种状态给出确定性结论；评分完成空池
  显示最高最终分、距正式线、两档达线数量及最多三项聚合原因。公开状态 schema 升为 `v2_status_v4`，
  静态资源握手升为 `release-contract-2026-08-30-v5`；活动评分、78/73 门槛、融合、风险和冻结均未修改。
- 证券主数据从“等待评分批次、实际依赖东方财富富身份分页”改为“启动恢复后立即在独立 `exchange`
  lane 刷新，候选发现缺口时幂等续刷”；有限 HTTP 重试、24 小时成功 TTL、300 秒失败退避和最近有效
  快照共同独立于 20 秒实时报价 deadline。完整快照按字段无损合并、派生上市交易日龄并批量持久化；
  全市场规范合并只消费本轮真实报价代码对应的参考资料，不再让全量主数据生成无价格的行情行。
- `/api/v2/status.market_data.sources.exchange` 加法公开启用状态、计划/成功/失败/超时、延迟、快照行数、
  上市日期行数和 timeout；`openpyxl` 作为解析深交所官方 XLSX 的直接运行依赖。活动评分、78/73 门槛、
  68/32 融合、风险扣分、候选公式和冻结规则均未修改。
- P2 加速路线不再把无法由 H0 日线重建的 14:50 生产决策称为历史生产基线；历史 comparator 固定为
  `score_h0_ohlcv_cross_section_v1`，只用于决定唯一候选是否值得进入真实前向影子。历史 ST/行业、
  14:50 尾部、披露时点、公司风险和 DeepSeek facts 缺少真实生效/接收时间时全部失败关闭；实际生产
  同日同股比较、真实行业约束和约 20 个精确官方交易日仍留给后续 P2-2 预注册。
- `docs/fenshu.md` 将 P2-0 标记为已完成、P2-1 标记为下一个未完成整节。P1 的 2027 年身份、日期、失败
  状态和工件继续只读；本批没有读取/生成历史筛选结果、创建 P2 前向身份、修改活动评分、78/73 门槛、
  固定 68/32 融合、冻结、DeepSeek、配置、CLI、API 或 Web。
- 用户要求不要等到 2027 年才使用新评分，并明确采用“历史验证 + 约 20 个交易日前向影子”。确认根因
  是已冻结 P1 主动选择了 2027 年 40+20 未来窗口且依赖尚未发布的年度日历，这是不可改写的研究身份
  设计选择，不是历史行情无法参与评分研究。`docs/fenshu.md` 现将 P1 定位为只读审计记录，不再作为
  最快生效路线，并给出计划中的 `score_tomorrow_historical_p2_v1` 历史身份：复用 `score_h0_v1`
  归档、建立字段准入矩阵、只冻结一个可一致重建的 Tomorrow local-only 候选；通过后再创建
  `score_tomorrow_shadow_p2_v1`，立即运行约 20 个精确预注册交易日的真实影子，满足历史 300/前向
  100 配对及完整收益/风险门禁后再由独立高风险批次切换生产。
  P2 首版明确排除无法历史还原的 ST/行业、14:50 尾部、披露时点风险和 DeepSeek Alpha，防止用当前值
  回填或为赶进度制造训练/生产偏移；本批只优化非权威执行计划，不修改 P1 机器身份、活动评分、78/73
  门槛、68/32 融合、正式推荐或 Web 行为。
  `Regression-Key: score-p2-accelerated-promotion-plan-v1`。
- 首页第二张摘要卡从容易被误解为推荐就绪度的“行情覆盖”改为“数据可用性”。短线 current 未就绪时
  直接读取同交易日 `supply_funnel.security_master`、`history` 与行情 summary，主行显示基础资料
  完整数/候选数，副行只显示行情覆盖与有效历史数；采集阶段显示“准备中”，已发布短线、历史和 Long
  仍按当前页面完整名单显示行情可用数。状态 API、供应漏斗和证券主数据门禁没有改 schema 或双写状态。
- 用户要求继续 `docs/fenshu.md` 下一个完整未完成章节。批次 6“人工晋级”经只读资格审计从含糊的
  “未开始”改为 `[等待前置条件]`：当前日期早于 2027-06-14 首个历史计划日，上交所官方休市安排页
  尚未发布 2027 年年度文件，本地与 Git 没有对应日历证明、40+20 逐日证据或三类终态报告，因此不存在
  可供人工确认的 `promotion_eligible`。本批按失败关闭契约不修改活动配置、策略/引擎/融合版本、
  78/73 门槛或固定 68/32 融合；只有未来真实证据全部通过后才能另立高风险生产晋级批次。
  `Regression-Key: tomorrow-shadow-manual-promotion-readiness-v1`。
- 批次 5 没有把新五挑战者名称塞入旧 `Score-R5`：旧链绑定的是另一组不可变挑战者，复用其身份会污染
  既有档案。新链独立拥有规范与证据命名空间，只把无家族含义的配对移动区块 bootstrap、确定性种子和
  Holm step-down 收敛为通用纯领域统计实现；旧 R5 继续得到相同随机流和结果。生产候选、评分、风险、
  68/32 融合、DeepSeek 预算、冻结、API 和 Web 均未改变。
- 本轮已按 `AGENTS.md` 显式加载并执行仓库级 `trader-delivery` skill；此前未触发属于交付流程遗漏，
  不是 Web、行情或运行时故障，也不需要修改产品触发逻辑。用户同时反馈的 Web 推荐漏斗异常保持独立
  待诊断批次，本提交不把尚无运行实证的原因写成事实。
- `docs/fenshu.md` 批次 4 标记为工程能力完成；Tomorrow 固定不继承跨日 incumbent，D25 固定新进入 `20bp`、维持 `0bp`，选择保持 Top6、单行业最多 2、按最终池计算的单板最多 60% 及合法空池。报告仍固定 `status=exploratory`、`production_authority=false`，未接入生产组合根或 Web。
- `pyproject.toml` 增加 LightGBM 4.7 与 NumPy 2 的正式运行依赖；LightGBM 只存在于研究 infra 训练适配器，未接入生产组合根。`docs/fenshu.md` 批次 3 标记为工程能力完成，报告仍固定 `status=exploratory`、`production_authority=false`。
- `docs/fenshu.md` 批次 2 标记为工程能力完成；特征工程只供后续预注册影子模型消费，固定 `production_authority=false`，不修改活动生产候选、评分、融合、风险、冻结、DeepSeek 或 Web。
- 新研究事件写入升级为 `v2_research_committed_event_v2`；既有事件/审计 v1 保留显式只读 codec 和原始内容哈希验证，不迁移、不补写。`fenshu.md` 对应点时人口子项已完成，新研究身份仍等待后续评分规格冻结和标签可见前预注册。
- `scripts/diagnose_runtime.py --profile research` 复用现有 `trader-cli research-status` 的
  `v2_research_readiness_v3` 权威投影，只汇总活动研究身份、历史/前向窗口、最大可达日期和 blocker；
  不再由独立脚本重复读取 SQLite 和计算覆盖状态，避免计划外旧日期被误投影为 `score_p0_v2` 活动记录。
  2026-08-28 只读状态确认该身份已错过 4 个计划日、最大只能达到 36/40，固定为
  `historical_collection_failed`；缺失日的运行级直接原因因历史日志不存在仍待验证。
  `Regression-Key: score-p0-readiness-v1`。
- 根因确认：上一批虽增加统一编排器，但六个顶层专项脚本仍各自拥有 CLI 与落盘逻辑，导致入口、参数和
  维护边界重复，后续代理仍可能绕过统一报告。本批把 `scripts/diagnose_runtime.py` 收口为唯一公开诊断
  CLI，内部探针只向父进程输出一个 JSON；`make performance-check` 直接复用正式
  `trader.entrypoints.performance`，浏览器和供应商专项门禁均经统一 profile 路由。该重构不改变行情、
  评分、DeepSeek、冻结、API 或 Web 业务行为，荐股策略文档无需变化。
- 原生因子诊断复用 R2 的点时组件和 `CostSettlementBasis`、R3 的 active-set oracle，不扩写已冻结 R3
  schema，也不建立第二套行情、评分或结算链。20bp 主标签、Pearson/Spearman 最小 5 对、非年化 ICIR、
  Q1-Q5、三档成本、1/3/5 日滞后、`MAE/ATR20 <= -1.5` 和集中度分母现由荐股策略文档冻结为唯一口径；
  缺失继续为 `null`，报告固定 `production_authority=false`。
- 现有 Web 漏斗、历史源、腾讯、Tushare、Firefox 和生产性能脚本继续作为各自边界的唯一实现；统一入口
  只通过有界子进程编排、状态归一化和字段白名单汇总，不复制供应商解析或业务判定，也不进入生产调度。
  `Makefile`、README、权威运维契约和 `AGENTS.md` 统一引导修改任务先加载 `$trader-delivery`、按影响矩阵
  选测试与实证，并优先用组合 profile 定位首个故障边界及下游影响。
- 用户要求把三份并行文档中已经完成但尚未进入权威文档的内容完成合并后删除来源文件。核对确认
  V2-only 产品边界、无兼容原则、唯一 API、冻结规则和大部分评分研究状态已经归入权威文档；实际缺口是
  日常安装/启动/只读检查命令仍只在独立运行手册中，文档治理和机器契约仍把三份文件当活动输入，且删除
  实施计划会遗失尚未完成的正式发布与原生因子诊断 Gate。本批将运维命令和剩余工程状态统一归入
  `software-business-design.md`，把诊断标签、指标和生产隔离边界归入 `recommendation-strategy.md`，
  不改变任何运行时、配置或生产策略行为。
- 120 分 Tushare 从“代码声明支持但生产提前返回”改为参考 lane 中真实执行固定 `000001` 的低频
  raw 日线能力审计并复用 6 小时缓存；`daily` 改为符合官方参数的逐证券请求，滚动 60 秒与上海自然日
  进程门禁在 50/8000 次前失败关闭。raw 观测仍不进入历史特征、评分、冻结或推荐。
- `score_p0_v2` 不再因窗口尚未结束而笼统显示 `historical_collecting`：已经过去且缺少 committed
  evidence 的固定日期会使根状态进入 `historical_collection_failed`，R2-R5 继续 fail-closed。新研究
  身份推迟到因子、标签、模型、成本和挑战者规范冻结后再预注册，不创建含义未定的占位身份。
- 历史预热生产策略现在把 20 秒批次预算显式拆为 18 秒供应商路由和 2 秒校验/提交余量；腾讯一次加
  东方财富三个 host 的最坏四次串行尝试使单次 HTTP timeout 从配置上限 12 秒截断到 4.5 秒，避免合法
  回退路径自身超过批次 deadline。每股最多 61 条最近历史记录改为一次原子批量事务，不再逐日重复连接、
  初始化 schema 和提交 SQLite。
- 生产历史预热单批由固定 30 只改为 `min(30, history_workers)`；当前 5 个历史 worker 因而每批 5 只，
  预热自身不再预先堆积第二个 worker 波次。20 秒 deadline、逐股即时提交、稳定轮转和真实慢尾退避
  保持不变，没有通过盲目延长 deadline 掩盖供应商故障。
- 全市场、候选与 TopK 刷新直接复用供应商返回的同批 `FeatureSnapshot`，评分输入按完整特征身份缓存并
  在构建前后复核 epoch；候选特征包含已缓存结构化研究，行情缓存预计算代码索引和横截面映射，消除
  候选/TopK 二次读取、三策略重复构建及每轮重复字典构造。数值标准化增加常见数字/字符串快速路径。
- 腾讯定向行情按每片最多 120 只、最多 3 片并发复用统一有界执行器；任一分片失败仍整体显式降级，
  不发布静默残缺快照。历史预热保持首次入队顺序避免排名重排饥饿，并在单股票完成时立即提交内存与
  持久化结果，不再等待整批最慢请求。
- 同策略评分和 DeepSeek hybrid 在数据准备、本地评分及发布前执行 latest-wins 检查，短发布动作与新
  offer 线性化；被更新输入取代的旧周期不再继续发布、研究或融合。Web status schema 升级为
  `v2_status_v3`，静态资源 revision 升级为 `release-contract-2026-08-26-v3`，权威架构、策略与运维
  文档同步固化输入驱动评分、失败降级、直推 SSE 和延迟瀑布契约。
- 用户要求把扫描出的六类问题全部优化。根因复核确认：类型化状态仍在行情 health 链泄漏为 JSON 字典；
  正式记录哈希与持久化各维护一套字段投影且研究审计依赖 `__dict__`；运行时、SSE 和 6586 行行情组件
  测试按历史增长聚合；实施计划仍混入已完成波次；测试树保留无人引用的旧流水线证据；少量测试、资源
  握手和校验文本仍使用旧版本名。本批采用统一目标架构整改，不保留双表示或兼容别名。
- 正式记录身份哈希和持久化 codec 统一复用领域层
  `committed_record_identity_payload()` 的单一显式字段材料；研究审计改为逐字段投影候选与决策，不再
  通过对象反射决定审计线格式；列式批次身份哈希也改为显式列举字段。内部字段增加不会自动改变
  持久化、审计、批次身份或 Web schema。
- 将原 6586 行行情组件测试按 vendor、gateway、lane、history、reference、Tushare、feature、intraday、
  research、service 十个真实行为域拆分，共享工厂进入非测试 support 模块；156 条测试断言保持不变，
  每个行为文件均低于 1200 行，并增加结构契约限制重新形成单文件聚合套件。
- 权威产品/策略文档统一表述为“V2-only 工程与发布门禁已验收、当前仍属 Unreleased”；实施计划只保留
  正式 0.2.0 发布与 `score_p0_v2`/Score-R6-R7 外部证据两个未闭合 Gate，不再复制已完成施工波次、
  会话分工和迁移时间线。正式 release 仍须用户另行发起独立批次。

- 用户可观察诉求是避免同一业务在 JSON 字段和对象字段、Tomorrow 专属名和三策略公共实现之间反复迁移。
  根因是正式记录 codec 位于领域层，且 Today/Tomorrow/D25 公共选择、质量、融合、投影、冻结仍以
  `tomorrow_*` 命名并保留 Today 包装入口。现在三策略统一复用 `scored_*` 类型模块和单一投影入口，
  配置 schema 升至 13；公开 JSON 仍只由持久化/Web/状态 adapter 显式投影。
- D25 市场状态配置缩减为仍参与审计的 60/40 breadth 边界；性能门禁改为调用活动
  `score_board_strategy()`，不再通过退休评分器测量一条生产不会执行的路径。

- TopK 腾讯报价不再与 360 只普通候选共享一个 running/pending 槽，且 2 秒任务 deadline 会截断实际
  HTTP timeout；全市场与 Tomorrow 分钟尾巴也不再共享东方财富 pending 槽。物理任务预算从 worker
  实际开始时刻计算，排队时间不再提前消费采集预算。
- 输入驱动评分在最小间隔内保留一个 latest-pending 请求，到期只提交最新一轮；三策略普通评分输入
  复用一份不可变候选特征 batch，Tomorrow 只额外构造一份分钟尾巴变体。local 发布后模型复核异步
  升级，慢 DeepSeek 不再占住下一轮本地评分，迟到 hybrid 继续由统一索引 CAS/序列规则拒绝。
- SSE 断线恢复从固定 15 秒等待改为立即 status/current 对账、3 秒临时轮询和 1/2/4/8/15 秒指数重连；
  overlay 事件只替换匹配代码的推荐/观察行并更新摘要，不再重建表头和整张表。

- 本地评分改为只在候选报价、市场新闻或个股风险成功完成后触发；`score` cadence 仅作为合并重复触发的
  最短间隔，不再由调度 tick 独立排队。14:48 后 DeepSeek 保持关闭，但 tomorrow/d25 纯本地评分以
  1 秒最短间隔持续到 14:50，运行配置身份升级为 `runtime_v38_input_driven_realtime_2026_08_25`。
- SSE 的 `schema_version` 统一保持字符串事件 envelope `v2_event_v1`，行级报价 patch 独立使用
  `patch_schema_version=2`；内部类型对象与外部 JSON 不再复用一个字段表达两种类型或两层版本。

- 荐股健康采样在 HTTP JSON 边界立即解析为不可变类型快照，分析与报告投影不保留逐股 `items`；持续
  异常窗口按可评分阶段和事件序列切分，进程重启、冻结/非评分阶段及带合法空诊断的零推荐不再误报；
  单次 status/current projection 差异按并发读取竞态记为 warning，其余身份冲突仍为 error。

- 决策 CAS 成功后现在直接写入内存 SSE，研究 observer 只消费审计副本；overlay 事件携带行级报价和
  与 current ETag 一致的 `projection_version`。浏览器按策略、交易日和事件类型过滤，decision 才整表
  GET，overlay 原地 patch；status 对账可在事件丢失后恢复当前投影。
- DeepSeek 预算账本在 reserve、完成、失败、批次终态及启动恢复时原子换入不可变内存快照；Reviewer
  和顶层 status 共享同一上海交易日快照，HTTP status 不再执行预算 SQLite 汇总。
- 冻结后的行情更新改为独立 TopK 定向批次：Today 11:20 后、Tomorrow/D25 14:50 后和已有正式记录的
  15:00 后运行期只更新入选代码报价，不重新评分；冷启动缺正式记录时才执行一次收盘全市场恢复。

- 后续自动化代理修改状态接口时，必须先按根 `AGENTS.md` 选择类型对象或显式 JSON 边界，并同步契约测试；
  不能再把局部字典/对象互换当作独立重构，也不能以兼容名义保留双表示或扩大隐式 fallback。

- 进程内控制与诊断统一读取对象字段；市场健康适配器在最终可观测性边界显式投影来源 lane、缓存和
  延迟 JSON，保持现有 `/api/v2/status` 字段、枚举和值格式不变。缓存身份直接由规范 dataclass 编码器
  生成相同 canonical JSON，不再先复制为临时字典；配置/供应商/持久化/schema 载荷仍按 JSON 边界处理。

- 报价 overlay 的观察时刻改为本轮调度请求、入选特征本地观察时刻与报价本机接收时刻的最晚值；
  供应商 `source_time` 仍只作为受校验的数据事实，不能用未来声明时间推进本地时钟。组合根状态投影
  拆入独立模块，`bootstrap.py` 回到架构行数门禁以内；评分公式、阈值、排序和冻结身份均未改变。

- Web 未冻结 current 快照的内存保留窗口由硬编码 30 秒改为配置驱动的 35 秒；该值按本轮生产调度、
  SSE、HTTP 与真实 Firefox 链路实测约 30 秒加 5 秒余量确定。策略 cadence、行情数据年龄和
  stale/degraded 门槛保持不变，避免把 35 秒误用为采集 TTL 后将物理刷新进一步拖慢。
- runtime 配置身份由 `runtime_v36_v2_only_release_2026_08_12` 升级为
  `runtime_v37_web_retention_margin_2026_08_24`，Web release 身份同步升级到 v16，确保旧常驻进程与
  新页面不会静默混用不同保留窗口。

- `AGENTS.md` 现在要求首次完成的可复用诊断、性能、数据源或浏览器实测参数化进入 `scripts/`，以后
  优先复用或扩展现有脚本，禁止在 `/tmp`、heredoc 或临时工具调用中重复重写；Makefile 的 Ruff
  源范围同步覆盖两个新 Python 脚本。

- Today、Tomorrow、D25 的活动调度现全部收敛到 `V2SchedulerRuntime` 与 `V2MarketDataAdapter`；
  11:20 后不重跑 Today 评分，而由其它评分 lane 已完成的统一行情批次只更新同日 scored current/formal
  的可变报价 overlay。缺少新报价时继续保留已有 overlay 或正式报价锚点，Web 查询不现场抓行情。
- 权威产品/架构契约同步为当前 V2-only 组合根、统一策略 lane 和真实服务验收要求；协作约束中已失效的
  P6 名称改为当前 V2 current/旧发布链描述，评分、融合、门槛、冻结时点及外部数据配置均未改变。

- Linux/macOS/WSL 与 PowerShell 启动入口现在先验证子命令，再进行虚拟环境创建、依赖安装和入口
  调度；无参数与 `serve` 继续等价启动看板，原有研究命令及其参数透传语义保持不变。

- 免费全市场行情产生的板块、交易所和上市日期现在由组合根直接接入独立 `reference` latest-wins
  lane；富身份响应即使晚于实时报价截止时间，也会在规范化和字段合并后异步批量写入 SQLite，
  无需等待下一评分周期。上市交易日龄继续仅由本地生产交易日历派生，Tushare 保留为可选估值、
  财务、日历和历史增强，不再是证券身份覆盖或系统就绪条件。

- 全市场来源协调器现在把“低延迟实时报价”和“稳定证券身份”分成两个接纳结果：完整物理响应先
  规范化并把板块、交易所、上市日期晋升到网关身份仓，再单独执行报价截止门；迟到结果仍不能进入
  当前报价、评分或冻结。行情覆盖卡在身份缺失非零时同时显示“上市日期/交易日龄”构成，Web 资源
  revision 提升到 v14，防止旧页面与新脚本混用。

- MIDDAY 调度现在使用独立 `midday_recovery` 周期：只在同日产物缺失且策略 lane 空闲时提交
  tomorrow、d25 和 long；所有午间恢复强制 `allow_review=false`。Long 刷新只有被其 latest-wins
  worker 接纳后才记作 handoff，提交拒绝会进入既有受控刷新失败与重试链。
- 观察池展示由 open/生成中/不可用扩展为显式空草稿状态：已有草稿但 0 只达到 `observe` 条件时显示
  “本轮无股票达到观察条件”，计数为 0；只有 lane 活动时显示“正在生成”，lane 空闲且无草稿时显示
  “本轮尚无可用观察草稿”。Web asset revision 升为 v13，阻止旧进程与新资源混版。

- 观察池的“正在生成”现在必须同时有活动时段和该策略调度 lane 的 `running`/`pending` 证据；lane 已
  空闲且草稿仍为空时显示“本轮未形成观察草稿，请查看运行状态”。页面遇到 release 不一致时隐藏
  无效观察池、阻止旧数据解释，并明确提示正常停止旧服务后重新运行 `./run.sh serve`。

- 短线 `not_ready` 首次评分阶段的五张摘要卡现在优先使用同交易日 `scheduler.input_quality`，该状态
  尚未形成时回退到脱敏 `market_data` 候选缓存、年龄和最新候选来源：覆盖显示真实样本，身份明确
  “待评分”，漏斗显示“采集中/待计算”，运行中的冻结卡显示“采集中”。Web 在观察窗口内随 15 秒
  状态轮询自动重读 current，草稿出现后原位渲染观察表，正式推荐仍保持未就绪。
- `/api/v2/status.market_data` 加法公开聚合字段 `candidate_quote_latest_source`；只返回最新候选报价的
  来源名称，不公开股票代码或逐股事实。`DecisionView` schema 从 v1 升为 v2，并以独立 `draft` 对象
  表达未正式发布的观察项。

- `scheduler.input_quality.*` 新增不含股票身份的 `summary`，统一携带候选行情完整数、行情/证券身份
  缺失数、最新来源/源时间和最高最终分；`/api/v2/status.market_data` 只投影内存行情年龄、来源计数、
  熔断、延迟和历史预热聚合，主动丢弃逐股缺失键、外部错误文本与载荷。短线 current 未发布时摘要卡
  消费这套状态，正式推荐仍为 0，观察明确显示为“观察草稿”。

- 参考数据调度现在显式区分两类范围：估值、财务、研究与历史增强继续只消费固定候选池，免费
  `board/exchange/listing_date` 身份持久化消费本轮规范全市场代码集；两者合并为同一个 Tushare lane
  请求身份，避免新增来源 lane 竞争。数据平面新增类型化证券主数据批量写端口，既有单条/正式记录
  写入复用同一准备、冲突与提交实现。

- V2 数据平面仓储现在在初始化、恢复、读和写的完整连接作用域统一捕获 `sqlite3.DatabaseError`，并映射
  为 `DataPlaneUnavailableError`；锁定仍保留专用受控原因，其它物理损坏不再把 SQLite 驱动异常泄漏到
  进程生命周期。该变化不自动删除、覆盖或伪装修复损坏数据，也不改变正常数据库 schema/提交语义。

- 候选历史异步补齐改为 `HistoryWarmup` 唯一所有者：证券主数据/交易日历参考刷新不再向 history
  lane 提交整批候选的第二条任务，候选更新仍以最多 30 只批次、三板覆盖和逐股退避推进，不与实时
  Web 请求绑定。

- 东方财富发行人公告从表面 `page_size=10000` 改为按真实 100 条单页上限最多 50 页有界遍历，使用
  稳定公告身份去重并原子缓存聚合载荷；完整旧基线到期后只需首页即可做确定性增量合并，减少持续
  刷新的请求量。生产候选、可靠度、风险、73/78 动作和 TopK 阈值均未改变。

- Today、Tomorrow、D25 的统一 V2 local/hybrid 决策现按策略身份在融合后、动作和两个选择池之前
  执行既有活动下行保护。只有原本达到执行门槛的候选会被降为观察；`local_score`、固定 68/32
  融合、动作门槛、正式 TopK、观察池容量、DeepSeek 候选边界和冻结时间保持不变。

- R6D 日级收益、严重亏损、换手、波动、召回、集中度及板块指标累计、基础趋势分、proxy 分和受约束
  Top6 选择已提炼为共享纯函数，R6D 原行为与报告身份不变，R6S 不再复制第二套评价口径。R6S 训练
  固定要求换手至少下降 3 个百分点、净超额最多回落 0.10 个百分点、严重亏损率最多增加 1 个百分点；
  已观察验证段只标记为 `reused_observed_validation_window`，不能被描述为新盲测。

- 真实 H0 诊断冻结候选为 `previous_score_weight=0.5`、`entrant_turnover_penalty=2.0`、持续性加分 0；
  换手从父控制组 `47.34%` 降至 `31.52%`，严重亏损率从 `13.91%` 降至 `11.87%`，日净超额标准差
  从 `4.386` 降至 `4.157`。但净超额从 `+0.153%` 降至 `+0.006%`，oracle Top6 召回也下降，故真实
  报告为 `historical_rejected`，活动评分、Web 实时性及 DeepSeek 策略均未改变。

- 趋势候选只允许在固定训练段择优，且训练收益不得低于 R6 v1 proxy、严重亏损不得增加；冻结候选只在
  2026-01-01 至 2026-07-31 验证段评价一次。真实报告中候选验证净超额由基线 `-0.396%` 提升到
  `+0.153%`，严重亏损率由 `17.39%` 降到 `13.91%`，但换手由 `39.61%` 升到 `47.34%`，超过
  `+5` 个百分点门槛，故报告按契约为 `historical_rejected`，未改变活动评分或荐股。

- 统一短线 current 与历史只读决策投影顺序：API 按不可变决策已有连续 `rank` 输出入选项，Web 继续
  只做正式/观察分池筛选，不复制评分公式或自行重排。正式池和观察池因此都保持最终分、本地分、代码
  的生产稳定排序；评分、入池身份、冻结记录、overlay、SSE 和刷新节奏不变。

- 三条评分策略现在先原子发布本地结果，再把新进入输出和既有候选按 `stock_risk` 的 120–300 秒
  配置节奏交给独立单协调 worker；每批最多 4 股、40 秒预算且使用独立公司研究端点池。研究变化只
  重算同交易日已有 current，首次 DeepSeek 复核等待对应研究批次，失败则释放屏障并继续保留本地结果。

- `research-status` 新增 R7 不可变档案摘要；Linux/Windows 启动脚本公开显式 R7 命令。计划与两份
  权威契约更新为 Score-R7 工程能力已完成、当前无剩余工程章节，但真实前向证据、正式档案实例和
  生产发布尚未发生。档案固定为 `manual_review_status=pending` 与
  `production_change_authorized=false`，不改变活动评分、风险、融合、冻结、DeepSeek、Web 或配置。

- 总计划和两份权威契约推进到 Score-R6 已完成、Score-R7 为下一完整章节。`research-status` 不再用
  固定 false 代替 R6 事实，而是只从哈希可复算的历史/前向不可变制品推导状态；local 通过而 hybrid
  未通过时仅产生 `local_only_eligible`，不会把 DeepSeek 路径误报为已验证或自动改动活动配置。

- 总计划和两份权威契约推进到 Score-H0 已完成、Score-R6 为下一完整章节。历史参数筛选可在 H0 固定
  归档覆盖达标后开始，不再等待 `score_p0_v2` 的未来 40 日；生产晋级仍必须冻结候选并取得新的真实
  前向证据，回顾性结果固定 `promotion_authority=false`，活动策略、融合、风险、冻结与 Web 均不变。

- 单股下载完成门槛收紧为至少 66 根有序、唯一、截止日内前复权日线，分别覆盖 61 根特征输入和 5 根
  未来标签；下载任务只保留最多 worker 数量的在途 future，失败仅保存脱敏类别，重跑复用冻结股票池
  并只补失败或缺失证券。

- `research-status` 升级为 `v2_research_readiness_v2`，同时报告活动 `score_p0_v2` 的历史/前向计划
  日期数、实际 observation 日期数、spec hash 和旧 `score_p0_v1` 的终止状态。活动窗口不足 40 日
  时状态为 `historical_collecting`；即使分区齐全也只进入显式离线评价，不自动运行 R2-R5 或开放 R6。

- Score-R2 不再由模块内日期常量决定窗口，而是消费纯领域不可变 research spec；旧 v1 保持原主窗口
  和最近前序替换规则，新 v2 只尝试预注册的 40 个未来交易日。R3-R5 的父报告、bootstrap 随机流、
  forward collector 和 final sealer 均拒绝跨身份或跨 spec hash 组合。

- committed observation 新写入从已满的 64MiB 单库切换为按交易日 SQLite 分区；已有 legacy 单库
  只读参与查询、去重和容量统计且不做破坏性迁移。单日分区与 20GB 归档分别限流，达到上限显式
  拒绝研究载荷但继续保留正式推荐；盘后 settlement 由“仅 15:00 秒点”改为同日成功键去重，失败和
  15:00 后冷启动可重试。

- `trader-server` 现在在监听端口成功绑定后立即向标准输出刷新实际浏览器地址；提示保留用户已有的
  “浏览器登录地址”中文标签，并将裸 `host:port` 改为带 `http://` scheme 的完整 URL。默认输出
  `浏览器登录地址->http://127.0.0.1:5000`，自定义 host/port 时显示实际配置且支持终端识别为超链接。

- Tomorrow、D25 和 Today 的评分投影现在从形成该项的同批 `MarketQuote` 固化价格、涨跌幅、成交额、
  换手率、总市值、来源、来源时间和版本；local 升级 hybrid 时决策身份与匹配新父版本的完整 overlay
  原子换版。只读查询优先展示匹配 overlay，否则回退到决策锚点，历史与冻结记录不再依赖现场行情。

- “数据新鲜度”从中文整分钟/秒改为紧凑 HMS：小于一分钟显示 `59s`，小于一小时显示
  `5m 12s`，一小时及以上显示 `27h 2m 26s`；小时不按自然日折算，秒级刷新精度得到保留。

- 首页第二张摘要卡由“候选覆盖”改为“行情覆盖”；候选、已评分、正式推荐、过滤、观察与最高评分
  统一归入“推荐漏斗”，Long 明确显示“不适用”。“快照状态”标题后固定显示交易日
  `YYYY-MM-DD`，正文中的发布时间统一只显示上海时区 `HH:mm:ss`，避免同一区域重复日期。

- 修复方案现在必须综合正确性、架构一致性、维护成本、可测试性、降级、性能和兼容成本择优，不得
  以最小 diff、文件数、改动行数或最短运行链为目标。有明确整体收益时允许在同批完成跨模块或
  全仓工程重构，并同步闭合接口、数据流、迁移或兼容、旧链清理、测试、文档和验收。

- 三条短线策略现在只在一个共享输入批次内调度一次候选证券参考刷新；11:20 后只继续计算
  Tomorrow/D25，Today 保留正式结果的报价 overlay，不再创建必然被封口索引拒绝的评分周期。
  免费全市场行情中的板块、交易所和上市日期按候选持久化，上市交易日数统一复用生产交易日历，
  重启恢复不再依赖 2000 积分 Tushare 权限。

- 顶部状态区改为固定等高双栏：左侧“快照状态”，右侧“最近错误”，系统健康以正常/降级/错误徽标
  合并到右栏，不再单独占用摘要卡。摘要收敛为数据新鲜度、候选覆盖、推荐漏斗、模型预算和冻结状态
  五张等宽卡；Header 运行条只保留运行、市场阶段、推送和评分时刻，减少重复信息并稳定首屏纵向位置。

- scored 原生输入的决策时刻现在取调度请求与同批本地观测/接收完成时间的最大值，避免网络请求期间
  新鲜候选被误判为未来数据；供应商来源时间仍不能推进本地时钟。新决策从同批 quote 固化名称和行业，
  current、冻结历史与 HTTP 只读复用，不执行现场网络补名；`/api/v2/status` 新增 `runtime_version`
  和 scheduler 摘要；本批 current-only codec 收口后，新 release 不再读取缺少显示字段的旧正式记录。

- Today、Tomorrow、D25 同一调度观察点现在 single-flight 共享全市场与候选报价批次，本地输入只读
  已有历史、结构化研究和分钟尾部缓存，不再由三条策略 lane 分别同步抓取历史与公司研究；
  `candidate_pool_size=120` 按每板应用，三板请求上限恢复为 360。状态 API 新增调度 lane、失败/发布
  计数和按策略结构化活动错误；成功发布会清除对应策略活动错误。

- 新生成的 `ScoredDecision` 保存去重后的 population/rejected 覆盖计数，current 与正式历史查询直接
  使用精确计数；当前 schema 缺少字段时 fail-closed，不再兼容推导。过滤原因计数继续作为可重叠
  原因分布，不再被误当成股票数相加。

- 总计划与权威研究状态推进为 Score-R5 工程能力已完成、Score-R6 为下一章节。当前真实 R2/R4 证据
  仍不足 40 个有效历史日，统计结果只能是 `exploratory`/`historical_rejected`；真实前向窗口尚未
  开始，不存在 `promotion_eligible` 版本。活动 50 分/30%/每板 Top120、风险、68/32 融合、冻结、
  Web、DeepSeek 预算和生产配置均未改变。

- 总计划与权威研究状态推进为 Score-R4 已完成、Score-R5 为下一章节。R4 少于 40 个有效日时仍只
  输出 `exploratory` 配对能力，不运行 bootstrap、Holm、前向 collector 或晋级；活动生产策略、
  50 分/30%/每板 Top120、硬过滤、风险、冻结、Web 和固定融合行为均保持不变。

- 总计划与权威研究状态推进为 Score-R3 已完成、Score-R4 为下一章节。R3 只有在 R2 恰好提供 40 个
  有效日时才标记 `replayed`；当前活动运行库历史点时覆盖不足时仍可生成确定性的 `exploratory`
  报告，但不得据此宣称取得 40 日收益证据、通过历史门禁或具备晋级资格，生产策略保持不变。

- 总计划与权威研究状态推进为 Score-R2 已完成、Score-R3 为下一章节。固定主窗口最多接纳 40 个
  真实有效日；主窗口失败只从最近前序实际交易日向 `2026-05-18` 补足。当前活动运行库不含该
  预注册历史窗口的完整点时 epoch，因此真实运行只能形成 `exploratory` 覆盖结果，不能伪造
  40 日证据、收益结论或晋级状态。

- V2 调度器现在以内存状态公开最后一次调度判定的真实上海交易阶段，未启动时为 `closed`，运行后为
  `today_observe/today_main/today_late/midday/afternoon/...`；状态 HTTP 只读该值，不查询交易日历、
  网络、文件或数据库。Web 静态资源 revision 提升为 `snapshot-identity-2026-08-13-v6`，确保浏览器
  不继续复用旧的快照身份脚本。

- 研究风险组件持久化现在显式区分数据平面同时间冲突、数据平面不可用和未知异常：安全的
  first-wins 冲突只记 debug，未知异常的 warning 增加脱敏异常类型，不记录外部载荷。

- 历史特征持久化现在将按交易日推定的收盘来源时间限制在实际观测时刻以内；盘中当日日 K 使用
  当前带时区观测时间，已收盘历史日仍保持 15:00 来源时间，不改变历史特征、候选或评分公式。

- 用户要求继续 `docs/implementation-plan.md` 未完成任务。核对后发现计划顶部误写“下一章节
  Score-R2”，但正文和权威设计仍明确 Score-R1-Migrate 未完成；本批先闭合该依赖章节，并把下一
  研究章节统一修正为 Score-R2，不提前实现相邻章节。

- 根页面首次打开时按 Today、Tomorrow、D25、Long 顺序选择第一个当日 `ready` 且有条目的
  current；没有有条目策略时选择第一个 ready，全部未就绪时回到 Today。该自动选择只执行一次，
  用户手动切换后不被 15 秒状态刷新覆盖。静态资源版本提升为 v5，避免浏览器继续使用已经实际加载过
  的错误 v4 缓存。

- 15:00 后的 V2 调度现在只恢复当日缺失的 Tomorrow/D25 正式记录与 Long 当前投影：已有同日
  current 优先直接固化，否则按收盘行情本地补算并创建不可覆盖的 `close_fallback`；Today 继续按
  11:20 冻结边界保持 `not_ready`，收盘恢复不调用 DeepSeek。命中全市场行情缓存时仍会恢复被
  候选请求礼让的后台历史预热，但预热完成回调在 history lane 已有 pending 请求时不再自我续批。

- 用户反馈“界面上没有数据”，并要求继续最后一个工程章节。现状确认包含两层原因：运行时未就绪时
  scored 策略可以合法返回 `not_ready`，而 Long 固定名单此前又被错误绑定到 current API 成功；
  因此 API 暂不可用时连卡脖子等固定股票身份也被隐藏。本批让 Long 打包名单先于实时接口显示，
  current 只覆盖真实行情；同时把 V2 配置提升到唯一 schema 9，删除无效旧执行模式字段，计划状态
  更新为 V2-E0 至 E11 全部完成。

- 用户连续反馈“long 界面变成荐股、需要恢复卡脖子三个 Tab”。根因是统一 Web 根页面切换期间把
  Long payload 当成了 scored decision 展示；本批通过 V2 current payload 归一化与固定名单展示层
  恢复三分类、行业侧栏和长期行情表，同时删除旧兼容 Web 路由，避免再次通过旧接口分流。

- 用户反馈 Long 页面被显示成荐股表，要求恢复之前固定的卡脖子行业、高成长赛道和低价潜力股股票界面。
  原因已确认：V2-E7 保留了 Long 固定名单和无评分 `LongProjection`，但 V2-E8 重写统一根页面时把
  Long 一并降成通用决策表，丢失了原有三分类、左侧行业分组和长期股票行情表；本批没有改动 Long
  运行时、固定名单或荐股接口。现在 Long 单独恢复三分类和左右分栏，固定名单保持配置顺序和完整席位，
  V2 current 仅覆盖价格、涨跌幅、成交额、换手率、市值、行情来源与时间，Long 页面不再显示评分、动作、
  推荐原因或荐股漏斗。

### Fixed

- 修正规划层虽已有 H1、模型和留出门禁，却没有独立章节证明“当前基线是否一致、每条动态规则是否值得
  硬阻断、候选剪枝漏掉多少可执行正收益股票、单股排序是否形成最优受约束组合、性能优化是否保持决策
  hash 等价”的缺口。原因已确认是这些目标散落在既有规则或指标中，没有可由一次“继续”完整闭合的
  owner、工件身份和完成条件；本批只修正权威路线，不宣称对应能力已经实现或收益已经提高。
  `Regression-Key: recommendation-chain-scientific-roadmap-v2`。

- 实时复现确认当前冻结 Tomorrow 记录在 12:41 生成，早于 12:44 的零分解释修复；它的
  `coverage.evaluated_count=0`、复核候选 0、最高分 0，却携带 `no_positive_net_utility`，随后在 14:50
  按不可覆盖规则冻结。读取端现在对该矛盾失败关闭：不篡改正式记录、不声称“评分已完成”，明确说明未
  形成可评分候选并保持空仓等待下一交易日。正常且至少有一只已评分候选的负净效用空仓仍使用固定成本
  说明；同一矛盾若出现在未冻结 current，则明确等待重新评分而不误称“冻结记录”。两者均不放宽成本、
  历史、风险、73/78、68/32 或 DeepSeek 门禁。

- 修复原单级 `hard_filter()` 必须等候选行情、历史和研究特征形成后才执行，导致本应永久排除的股票仍会
  消耗历史下载、公司研究、候选行情和分钟行情请求的问题。根因确认是历史预热和各逐股适配器没有共享的
  发行人资格端口，而不是评分阈值本身。现在已知永久事实先于逐股 I/O 生效；新研究本批发现永久事实后，
  会在读取价格历史和进入评分前再次裁剪。注册表持久化失败只记录受控降级并保留最近有效内存事实，
  不阻塞本地推荐和只读 Web。

- 修正旧路线无法回答“如何根据预测收益与实际收益差距自动校准”且没有验证 DeepSeek 独立增量价值的
  计划缺口，同时消除策略文档旧 15.1.21–15.1.29 索引与软件设计路线摘要之间形成第二套状态的风险。
  新契约要求残差账本覆盖全部合格候选而非仅 Top6，未建模/未校准字段保持类型状态和 `null`，合法最高
  0 分继续表示成本和风险后的现金选择，不得为追求页面非空放宽门槛。Review 进一步固定预测与 outcome
  为按父 hash 连接的两类不可变记录，并禁止六类 DeepSeek 风险事实进入正向残差校准器，避免成熟标签
  原地改写预测或同一风险重复影响模型分与 penalty/veto。

- 用户反馈“明日数据异常，最高评分 0 分”。现场先确认 Tomorrow 有 57 只完成评分而非评分链空缺，安全
  重启后仍为 0；再只读使用截至前一交易日的本地历史重建 V1 全横截面，可重建 2,781 只，毛预测超额
  最高约 0.3076%，扣固定 20–40bp 成本后最高净效用约 -0.0612%，正净效用为 0 只。因此 0 分是
  “净效用不大于 0 映射为 0”的合法空仓结果，缺陷是原状态误写成普通门槛不足/无复核候选。Tomorrow
  现在使用 `no_positive_net_utility` 类型化空结果，状态以同一原因作为首要阻断，页面明确说明成本后
  净超额未转正和保持空仓；未调整模型、成本、73/78 门槛、评分或冻结，历史覆盖降级仍单独展示。
  `Regression-Key: tomorrow-zero-score-cost-aware-cash-v1`。

- 用户反馈 `run.sh` 不能正常启动。现场复现确认服务并未启动失败：10:52 已有真实
  `trader-server` 持有 `.runtime/v2/server.lock`，根页面与状态接口均可访问；第二实例被内核文件锁
  正确拒绝，直接删除锁会破坏单实例边界。锁冲突现在继续返回非零，但同时输出实际浏览器 URL，并明确
  指引在原启动终端按 Ctrl+C 正常停止后重试、禁止删除锁；Linux/macOS 与 PowerShell 帮助也同步把
  `research-screen` 更正为实际六阶段。`Regression-Key: active-server-lock-startup-guidance-v1`。

- 修复离线 V1/V2 留出命令在删除运行期比较规范后仍反向引用其历史报告 hash 的残留；固定 hash 现在由
  holdout 所有者维护。修复初版历史风险数据直接扩展 P2 冻结行、以及新 R6 语义复用旧 v1 路径会改变或
  误读不可变证据的问题：风险数据改用独立值对象，P2 行/报告身份不变，旧 R6 v1 目录只作审计且不会被
  当前状态读取。

- 修复生产模型适配器为了读取 Tomorrow P2 工件而导入离线筛选模块的问题。工件值对象迁移到生产中立端口，
  消除 server 启动时连带加载历史筛选、回放模型、P2 规范等七个离线模块；离线 trainer、artifact store 和
  研究测试改用同一类型，不保留复制类或兼容转发。生产推理仍校验原候选身份、schema、参数有限性和内容
  hash，错误仍 fail closed。

- 修复计划中直接执行 `node --test tests/js/test_dashboard_state.js` 时未提供显式资源路径便无法读取
  dashboard 脚本的问题；测试现在默认解析仓库内正式静态资源，同时保留包装器传入 wheel/临时资源路径
  的能力。生产 JavaScript、路由响应和浏览器行为未改变。

- 修复运行时代码、测试、功能包迁移清单、Web release handshake 与诊断 fixture 对旧模块/字段名称的耦合；
  wheel 现在只打包职责明确的两个新运行时模块，应用工厂仍保持无线程、无网络、无数据库和无文件写入
  副作用，调度、冻结、评分、DeepSeek、持久化及供应商行为均未改变。

- 修复“正式推荐为 0 就没有可评价数据”的取样缺口：比较器以两套 profile 的共同可评分集合为总体，
  不以最终入选交集为总体；正式绑定 manifest、全部标签和报告不可变，同日其它临时输入在绑定后清理，
  避免事后挑样本和长期无界占盘。修复同一 native 输入的 local/hybrid 来源身份差异造成 manifest 冲突；
  缺 ATR 的候选仍保留预测，并在结算时显式标为数据不足，而不是令整批比较失败。研究 SQLite 初始化
  失败改为状态降级和 observer 错误，不再阻断生产启动、活动评分或冻结。最终 Review 进一步禁止未正式
  绑定或不属于正式 manifest 的标签写入，改用显式字段白名单序列化，并规定只有全 manifest 已结算且
  至少含 300 条 `complete` 配对的横截面才计独立日，避免分批结算提前触发终态。
- 新增 V2 严重亏损概率 challenger 的标签、特征、60/20/40 日训练/校准/独立检验、1 日 embargo、
  Brier/ECE 和单次终止预注册；当前证据未到，不拟合、不回填，Web 继续明确
  `loss_probability_status=not_modeled`。

- 修复少数候选历史不足把其余已具备评分资格的股票一起阻断、并让“0只”混淆数据未就绪与真实低收益/
  风险空集的问题。选择器现在在本地评分前按实际 session 要求跳过单股；输入质量只让候选行情或证券
  身份的批次完整性继续 fail closed，不再让历史比例覆盖已有分数。Tomorrow 模型的 61 日真实输入要求
  也不再被通用 20 日指标掩盖。活动模型字段不完整的股票在板内候选限额前排除，不再占用名额并挤掉
  模型可评分股票。

- 修复全市场发现已完成、候选定向报价也有效时，选择器仍以更晚候选 `evaluated_at` 对约 5571 条人口
  重做 20/30 秒新鲜度判断的问题。现场人口因候选增强耗时被 Today/D25 全部标成 `stale_quote`，板内
  横截面为空，导致 342 个硬过滤允许的候选没有进入本地评分并显示 `360 → 0 → 0`；现在人口按自身
  完整批次水位审计，候选仍按最终水位审计。此前 Skill 已正确执行交付流程，但 Skill 不是运行时 hook，
  上一批修复的是 SSE coverage 陈旧，本批确认并修复的是独立的更上游评分断点。首次真实重启又发现
  人口水位 `max()` 保留胜出 UTC 对象导致类型边界以 `decision:value_error` 拒绝；原生输入现同时规范
  market/candidate 全部业务时间到 `Asia/Shanghai` 后再计算水位。

- 修复 decision SSE 完整替换把 payload 强制标为 `ready`，却沿用上一轮 GET 的旧 `coverage`；摘要因而
  停止读取同交易日 input-quality，并把已恢复到 229 个完整评分的 Tomorrow 继续画成
  `360 → 0 → 0`。现在 GET、publisher、serializer 和浏览器 replacement 共用同一 coverage 事实，SSE
  原子替换旧计数后显示 `360 → 229 → 0`，不需要为每个正常 patch 追加完整 GET。
- 完整门禁首次运行发现 mypy 2.3 在 Python 3.14 环境尚未检查项目源码就因 NumPy 2.5 stub 的 Python
  3.12 `type` 语法与目标 Python 3.10 冲突；现在第三方 NumPy 实现按显式 override 隔离，恢复 3.10
  目标下对 `src/trader` 的全量类型检查。

- 修复评分档位只能通过持久修改策略配置切换、无法针对单次启动安全覆盖的问题；配置 loader、唯一组合根、
  服务入口和离线性能门禁现共同消费类型化档位覆盖，配置原文件保持不变。实现 Review 还阻止了纯
  `research-status`/历史阶段被迫读取活动策略配置，保持其既有只读与最小依赖边界。

- 修复活动配置、公开状态和文档把生产档位与研究阶段 P1/P2 混为同一身份的问题；V1 的历史不可用原因
  改用不冒充研究版本的稳定代码，Web 继续展示中性的“模型信号分”。文档同时纠正“活动档位单边 T+1
  结算可直接比较两模型”的潜在误解：该数据只覆盖活动档位已入选股票，存在选择偏差。
- 修复 Tomorrow 生产评分只能硬编码加载 P2、无法按配置选择模型的问题；模型输入由固定六维改为工件声明
  的严格特征集合，P1 只取得三项残差动量，P2 仍取得原六项特征。任一 profile、资源、schema、特征宽度
  或完整 hash 不匹配均失败关闭，避免把五候选研究族、错误宽度或被篡改资源静默送入生产评分。
- 修复最终 Review 发现的两项运行实测缺口：Tomorrow 详情页“评分版本”此前读取 GET 中不存在的
  `strategy_version`，SSE 替换也未传递模型输入身份；现在两条读取路径都使用
  `input_versions.score_model`。无头 Chrome 退出后偶发异步残留 `Default` 目录使三档验收误报失败，现以
  有界 1 秒重试清理临时 profile，并有直接回归覆盖，业务浏览器进程和生产目录不受影响。
- 修复真实封存模型可能给出负预测超额时，原始百分比被误塞入只允许 0–100 的 `score_components`、
  导致决策构造失败的问题；预测值现进入带真实数值范围的 `DecisionModelDiagnostics`，本地评分分量仍
  保持 0–100 强校验，GET 与 SSE 均经白名单投影。
- 修复旧 `history_summary` 可在缺少 P2 新字段和 3/40 日锚点时被缓存 TTL 误判为完整的问题；恢复时若
  已有 61 根 qfq 原始 K 线便自动重建摘要。模型横截面同时复用正式硬过滤并排除不支持板块，缺少任一
  模型输入固定为 `production_model_features_missing`，不回退旧 Tomorrow 分。
- 修复模型缺失分支错误复用上一候选临时对象的选择审计问题，并把 LightGBM 在线推理固定为单线程；
  活动 `performance-check` 现在实际加载同一 hash 绑定模型和 360 行 P2 输入，不再只测旧启发式路径。
- 修复腾讯对 688981 等“窗口内无需复权”股票返回 `day` 时被旧解析器直接丢弃的问题。根因是生产请求
  已显式要求 qfq，但旧实现只接受响应键 `qfqday`；当东方财富三个历史主机同时连接失败时，组合链便
  从腾讯 HTTP 成功错误退化为 0 行。现在只有逐行公司行动元数据为空且两个调整标志均为零时才接纳
  `day` 为 qfq 等价序列；元数据缺失、公司行动存在或任一调整标志非零仍失败关闭，一般 raw 日线绝不
  冒充 qfq。修复不改变历史来源优先级、候选公式、评分、78/73 门槛、68/32 融合或冻结。
  `Regression-Key: tencent-qfq-equivalent-day-history-v1`。
- 修复本次评分一致性扫描确认的五项代码/策略漂移：V4 schema 的减持、解禁、质押、诉讼和业绩风险此前
  因原始代码与本地规则名不一致而静默丢弃；模型复核会覆盖已有本地 veto；零权重行业政策会错误增加
  已知维度数；配置仅校验权重合计而未锁定固定向量、0.50/2 覆盖门和观察余量 5；决策 epoch 使用宽于
  本轮策略的 10/8、6/5 常量复核选择结果。现在六类风险的全部三级严重度均失败关闭地映射到注册规则，
  veto 只作 OR 合并，零权重维度完全排除，启动校验逐项锁定固定参数，epoch 按自身同批策略限制自校验。
- 修正荐股策略第 15.1 节仍声称 P2 的 H0 覆盖、候选封存和前向证据“尚未完成”的过期状态；现在明确
  P2-1 已 `historical_rejected`、P2-2/P2-3 已取消且不是后续“继续”任务，避免旧计划文件把已终止路线
  误导为可恢复任务。活动评分、78/73 门槛、68/32 融合及 Web 展示行为均未改变。
- 修复状态卡虽已显示基础资料 120/360，主推荐区仍只说“快照尚未发布”，以及评分完成空池无法读取生产
  聚合原因的问题。根因是页面未把 `scheduler.input_quality` 接入主结论，且旧代码读取公开响应中不存在
  的诊断字典；现在统一消费类型化漏斗与原因计数，Today 冻结错过优先于当前盘后 pending，普通页面仍
  不展示上市日期/交易日龄缺失明细。
- 修复 Tomorrow/D25 真实漏斗虽有 360/360 行情、证券主数据却长期固定为 120/360 的首个数据门禁断点。
  根因不是“完全没有多源行情”，而是来源字段能力不对称：新浪/腾讯主要提供价格，120 积分 Tushare
  无 `stock_basic` 上市日期权限，东方财富 `f26` 成为免费活动路径的单点；该分页又与报价对冲共享
  deadline，断连或迟到时价格可由新浪发布而身份无法补齐。官方交易所通道现把报价可用性与证券身份
  可用性拆成两个独立生命周期，失败整批拒绝且不覆盖上次有效资料。
- 全量门禁复核修复两项测试证据问题，不改变生产实现：历史诊断路径拒绝测试改为基于真实仓库根目录，
  不再依赖另一台机器的硬编码绝对路径；迟到免费证券身份持久化测试现在先保证 hedge 来源真实在途，
  再分别等待持久化调度与写入完成，避免高负载下用密集 SQLite 读轮询争抢后台 writer 后误报失败。
- 修复用户看到“行情覆盖 360 / 360”同时又看到“身份缺失 240（上市日期 240）”却无法获得有效决策
  信息的问题。根因确认在 Web 信息分组而非行情或漏斗计数：`360 / 360` 只证明报价完整，旧副行却混入
  技术身份缺口及上市日期/交易日龄构成。新卡使用直接阶段计数展示“基础资料 120 / 360”和
  “行情 360 / 360 · 历史有效 78”，首页不再展示上市日期缺失数量，旁边推荐漏斗继续独立显示候选、
  完整评分和正式推荐。`Regression-Key: web-data-readiness-semantics-v1`。
- 针对用户反馈 Web 展示数据和推荐漏斗异常，修复全市场/候选刷新完成时间在 UTC 报价接收时刻较晚时
  未投影回 `Asia/Shanghai` 的根因。此前行情与特征已经成功发布，但 `V2RefreshOutcome` 在构造阶段抛出
  `refresh:value_error`，使 `close_quotes` 持续重试、Tomorrow/D25 收盘补算从未提交，页面只能忠实显示
  `360 → 采集中 → 0`。现在应用层先按绝对时刻选取最晚完成值，再统一转换为上海时区；不修改前端、
  不伪造漏斗计数，也不放宽类型/冻结契约。`Regression-Key: refresh-completed-at-shanghai-v1`。
- 本批完整严格 lint 发现批次 5 新增的影子门禁模块仍有六参数私有函数；校验逻辑已归还给持有规范与
  日历证明的 `PreregisteredShadowGate`，消除参数债且不改变研究报告、哈希或生产数据流。

- 批次 5 最终 Review 修复三项未被首轮测试覆盖的证据缺口：门禁报告不再只保存可能碰巧相同的聚合值，
  而是绑定日历确认和精确逐日证据 manifest；50/100bp 不再只有文档声明而缺少报告统计；全期合法空组合
  不再因零暴露抛异常，而是形成包含固定五成员 Holm 家族的结构化拒绝报告。采集中报告不能提前占用
  终态工件键，前向 collector、门禁与工件库共同拒绝日历或历史资格错配。
- 消除影子选择继续叠加趋势、稳定性等重复 Alpha、忽略个股成本或用单一进出门槛制造 D25 抖动的研究风险：候选效用字段固定为毛预期超额与同股估计成本，严重亏损概率和分歧只作同效用排序/审计；D25 状态按模型和窗口隔离，Tomorrow 拒绝跨期状态；约束不足时不放宽门槛补满 Top6。
- 消除 Tomorrow/D25 影子模型随机拆分、相邻标签泄漏、跨 horizon 特征漂移、训练外统计量污染和两模型数据口径漂移风险：结算显式绑定 horizon/固定观察 lag，同交易日两 horizon 必须绑定同一特征 hash，按时间顺序执行固定 embargo，标签必须在预测日前可见，标准化仅拟合核心训练段，线性与 LightGBM 共用完全相同的矩阵、标签、成本、验证和校准行；单类严重亏损校准使用 Laplace 平滑，不制造 0/1 概率。报告拒绝非固定 spec、重复折叠/预测和非法板块；工件冲突域包含训练窗口，不同窗口不互相覆盖，写入前先核对投影哈希；JSON 白名单投影只属于最终 infra 边界。
- 消除 Tomorrow 研究特征从任意 JSON payload 猜行业、财务或公告字段的未来数据风险：行业必须绑定不晚于截止的生效/接收时间，财务和公告只按 `published_at/received_at` 接纳，历史报告期不能替代披露日期；14:20 精确锚点、其他时段锚点、历史或残差控制不足时保持 `null`，不退用更早分钟或伪造 0。
- 修复未来研究窗口依赖当前股票池重建历史总体造成的幸存者偏差风险；硬拒绝股票只保留总体/资格证明所需的有界事实，不进入评分或收益轨迹。迟于 14:50 的事件或输入不能被点时读取选中，不能恢复已错过计划日。
- 修复诊断脚本“已有统一入口但仍需记忆并维护多个顶层命令”的重复交付问题：同一 `full` 命令现在按
  子进程隔离连续定位运行 Web、三类供应商、浏览器和离线性能，单项失败仍不会掩盖后续检查；输出文件
  只由统一入口校验为仓库外绝对路径并一次写入，内部模块不再各自创建报告文件。
- 修复评分研究只有 R3 最终分 Rank IC 和组合汇总、无法逐原生组件回答“相关性是否稳定、成本后是否单调、
  是否集中于少数股票/分层、候选剪枝损失多少 oracle”的证据缺口。根因是此前 R2/R3 已封存所需点时证据，
  但没有绑定两级父身份的独立因子诊断模型和持久化边界；本批新增父哈希、逐日身份和逐股维度全集校验，
  错配、缺行、文件篡改及同身份不同内容均失败关闭。
- 修复既有诊断分散在六个入口、一次失败容易打断人工排查且修改计划没有固定下游影响路由的问题。确认的
  流程根因是专项脚本虽已可复用，但缺少统一编排契约和仓库级计划执行说明；本批没有把 Changelog 中
  历次 Web 空数据、历史预热或实时性缺陷的旧根因误当成当前根因。组合报告现在明确区分
  `passed/degraded/failed`，并以脱敏子检查与稳定 finding code 支撑后续独立修复批次。
- 修复权威文档仍反向引用非权威概览、实施计划和启动停止手册，导致“唯一权威”与实际活动输入不一致；
  后续未完成的产品/发布/工程 Gate 直接维护在软件业务设计第 14 节，评分研究 Gate 直接维护在荐股策略
  第 15.1 节，已完成施工证据只保留在 Changelog 和报告。
- 修复 120 分 Tushare 分支虽声明支持 `daily_history`，但参考刷新只检查 qfq 后直接返回、真实 Token
  始终零调用的问题；同时修复把多个代码以逗号拼入单个 `ts_code` 的无效请求形状、空结果被记为成功，
  以及非成功供应商数字码全部退化为不可诊断 `sdk_error` 的问题。数字码仅保留类别，不泄露消息或载荷。
- 修复 `research-status` 只比较记录数、无法识别不可替换计划日已经错过的问题。实际不可变证据确认
  2026-08-24、2026-08-25、2026-08-26 没有 committed event 或 V2 正式决策，P0v2 最大只能达到 37/40；状态现在
  明确给出 `score_p0_v2_historical_planned_dates_missed`，禁止把失败身份误读为仍可完成。缺失日为何
  未形成正式决策因没有对应运行日志仍待验证，本批没有把数据平面隔离记录误写为确定因果；同时修复
  14:50 后迟到事件仅凭日期就能把失败窗口重新变成可恢复的问题。
- 修复真实历史源在 1 秒左右已经返回后，预热批次仍因逐条历史写盘反复争用 SQLite、跨过 20 秒 deadline
  并输出同名告警的问题；同时修复配置允许腾讯加三个东方财富 host 依次各等待 12 秒、与 20 秒批次上限
  自相矛盾的问题。已完成股票继续立即进入内存和持久化缓存，空响应/真正慢尾只影响对应股票并按原退避。
- 修复历史预热把 30 只股票提交给仅 5 个 worker、却把六波队列等待压进 20 秒 batch deadline，导致
  尚未获得执行槽的股票也被记为超时失败并进入 60-900 秒退避的问题。修复前现场只读状态曾显示候选
  universe 360、已计划 270、完成 143、失败 97；新策略消除了预热自身产生的第二至第六波排队误判。
- 修复未变化的候选/研究数据仍重复触发三策略评分、同策略旧周期可在新输入到达后迟到发布、TopK
  刷新后再次读取候选特征，以及推荐 SSE 总是触发完整 current GET 的端到端延迟浪费。浏览器实测中
  发现并修复决策 patch 错把 projection ETag 当作 snapshot identity、导致后续 overlay 请求 resync
  的身份错误；现在 decision version 与 projection version 各自承担稳定职责。
- 修复历史并发加载必须等待整批尾部才让快速结果可见，以及候选排名反复变化会让队尾股票长期无法
  预热的问题；研究新闻的类型化结果继续保留原 deadline 异常语义。Review 同时将本机 `.token_key`
  权限从过宽模式收紧为 `0600`，未读取或改写凭据内容。
- 修复测试 SQLite 直接绑定 `datetime` 在 Python 3.14 触发弃用警告，测试数据现在显式写入 ISO-8601；
  同步把活动设置校验文案、Web 包说明、测试文件名、性能 fixture 身份和静态资源握手身份改为当前 V2
  语义，避免程序、测试和文档继续把历史 v15/v16/v17 波次误作活动 release。
- 修复权威文档仍把版本号 `0.2.0` 当成已正式发布、而全部变更实际仍位于 `Unreleased` 的状态矛盾；
  `pyproject.toml` 的包版本保持不变，只有完成独立发布批次、版本归档、提交推送和同提交 tag 后才可
  声称正式发布。

- 修正文档与程序不一致：架构文档补齐第五个 `research` 领域包、当前正式记录 schema-only 策略和
  `scored_*` 公共模块边界；README 不再把只做能力审计的 120 积分 Tushare `daily` 描述成历史主源；
  策略文档不再声称旧 D25 双乘评分仍用于回放。
- 修正 DeepSeek parser 同时接受旧 schema、模型目录仍保留已退役模型以及 provider 仍接受
  `deepseek_http` 别名的问题；活动复核只接受 V4 schema、`deepseek-v4-flash/pro` 和 `http` provider。

- 修复 Web 推荐漏斗把运行时 `selected_executable` 无条件显示为 0、首次输入质量尚未完成时把未知阶段
  展示成 `0 → 0 → 0` 的问题；已有同日完整质量快照仍优先保留，下一轮临时 pending 不会覆盖它。
- 修复普通候选请求可覆盖 TopK pending、全市场可阻塞分钟尾巴、节流窗口内输入评分永久丢失，以及
  同步 DeepSeek 等待造成更新行情无法及时形成新 local 推荐的实时链路缺口。
- 修复 SSE 短断线最长约 15 秒才重新对账，以及每个 overlay 报价事件触发完整 DOM 重绘造成的额外
  patch-to-paint 和布局抖动风险。

- 修复权威文档要求输入完成后评分、14:49:20 检查点和 14:50 前最新本地稿，但生产仍周期评分、检查点
  协调器无调用者且最终窗口没有 `score` cadence 的三处断链；现在输入未完成不会提前构建，检查点在新
  current 形成后按 tomorrow/d25 独立重试，14:50 冻结仍以 CAS 拒绝迟到决策。
- 修复 DeepSeek SQLite 尚未初始化或被独占锁定时，内存空预算快照丢失按阶段零值结构的问题；初始
  `by_stage`、target/limit/remaining/target_met 现在与初始化后的同日零用量摘要一致。
- 修复 overlay SSE 用数字 `schema_version=2` 覆盖字符串事件版本、前端被迫依赖冲突类型的问题；同时
  补齐调度 status 夹具，并修复刚完成的荐股健康脚本遗漏 `zip(strict=...)` 和格式导致全仓 lint 失败。

- 修复此前只能人工查看单次 Web 卡片、无法区分“合法零推荐”和“上游漏斗异常”的运维检测缺口；连接
  失败、字段缺失、计数越界、上游阶段连续为零及非零回退现在均给出稳定原因码并以退出码 1 阻断。

- 修复生产 `submit_due()` 仍按整策略周期运行并最多等待 30 秒、导致 TopK/候选/全市场物理数据年龄预算
  无法满足的问题；现在返回计划器真实下一到期时间，周期在途只保留最新请求并按各自 deadline 降级。
- 修复 Tomorrow 评分读取分钟 tail、但生产没有调用 `refresh_intraday_tail()` 的断链；13:00 后按 5 秒、
  14:20-14:48 按 3 秒刷新，失败继续使用最近有效 tail。
- 修复冻结/恢复后的正式决策无法独立进行 TopK overlay、15:00 后已有正式记录仍可能依赖全市场批次的
  问题；定向报价现在形成独立不可变特征批次并直接执行 overlay CAS，名单、排名、分数和哈希不变。
- 修复所有策略 SSE 都触发当前策略 GET、status 的 `decision_version` 与前端 `snapshot_id` 对账失配，
  以及研究 observer 满队列可吞掉 Web decision 事件的问题。
- 修复一次 status 最多重复两次预算聚合、UTC/上海日期不一致及 SQLite 锁定可能令 status 失败的问题。
- 修复 Web 把可变 live quote 当成 anchor，并丢失 setup、downside、复核终态、研究覆盖和正式选择诊断的问题。
- 最终 Review 修复性能入口顶层导入 POSIX `resource` 导致 Windows 连普通 CLI 都无法加载的问题；RSS
  采样现在按平台延迟加载，Linux/macOS 统一为 KiB，Windows 使用进程峰值 Working Set。

- 修复统一 JSON/对象规则虽然已存在于软件架构文档和机器契约、但根代理指令没有直接写明，导致后续
  协作者可能只看到局部调用方式而再次选择相反表示的问题；现在代理开始任务即可读到唯一选择标准。

- 修复状态表示没有全仓边界规则、导致同类字段在字典下标和对象属性之间反复迁移的问题。根因是应用
  对象自行承担线格式转换且部分运行控制直接读取 JSON 形状；现在进程内状态、动态键集合和外部 JSON
  三类职责有唯一规则及机器门禁，后续新增状态字段不会自动改变公开 JSON schema。

- 修复 overlay 用调度请求发起时间而不是网络完成时间作为版本时钟，导致同批成功新报价被误判为未来
  数据的问题；同时修复 overlay 错误只会累积、后续成功刷新无法恢复健康状态的问题。预期 CAS 竞争
  不污染错误，交易日/代码范围/事件发布等真实失败仍保持可观察。
- 修复 `/api/v2/status.scheduler.input_quality` 依赖 `getattr(..., {})` 的隐藏降级：接口缺失过去会让
  行情覆盖、身份缺失构成、推荐漏斗和观察草稿卡片同时消失且不报装配错误；现在应用端口、运行状态、
  组合根投影和浏览器卡片形成一条强类型链，并有契约、集成和真实 DOM 回归锁定。

- 修复 Web 缓存保留期恰好等于实测刷新周期、没有任何抖动余量的问题：此前生产链一次 DOM 刷新可达
  30.002 秒，而浏览器在 30.000 秒即淘汰未冻结快照，策略切换或短暂请求失败时可能失去最近同日有效
  展示；现在 35 秒窗口覆盖刷新尾部，并保留约 5 秒安全余量。

- 修复旧 `TodayV2Runtime` 虽未被生产组合根装配，却独占“冻结后继续更新报价 overlay”行为，导致删除
  影子实现会让 Web 的 Today 价格停在 11:20。该行为现由生产统一调度器执行，正式名单、分数、动作、
  排名和哈希保持冻结；预期 CAS 竞争不污染最近错误，异常发布失败仍以脱敏 overlay 阶段错误可观察。

- 修复启动脚本把不同风险和副作用的独立子命令压成一行“用法”，导致它们看起来像必须填写的启动
  参数；同时修复拼错命令仍先准备 Python 环境、甚至可能触发依赖安装后才报错的问题。未知命令现在
  快速返回简短可执行指引，不倾倒研究命令和环境变量。

- 修复证券主数据以整条 observation 替换导致的反复身份缺失：同源较新稀疏响应过去会删除已保存的
  上市日期，进而同时制造 `missing_listing_date` 与 `missing_listing_age_sessions`。现在按优先级选择
  规范版本并按字段无损合并，较新非空值仍可纠正旧值，字段缺席不能删除旧的板块、交易所或上市
  日期；交易日历变化后会统一重算已有身份的交易日龄与涨跌停规则。

- 修复上一批虽然把全市场代码传给证券主数据持久化，却仍只保存报价抢跑胜出来源的问题：现场
  360 只 Tomorrow 候选行情覆盖完整，但证券身份仅覆盖 58 只；302 只中 237 只缺上市日期、65 只
  缺上市交易日龄。东方财富富身份响应此前作为 `hedge_inflight` 输家或报价迟到结果被丢弃，导致
  持久化长期停留在 851 只。现在任何已完整返回的富身份全市场响应都会供后续批次持久化，同时
  保持身份 100% 门禁，不用代码前缀猜上市日期或放宽推荐阈值。

- 修复两项共同造成“明天观察池没有数据”的已验证问题：`decision_at()` 在 11:20-13:00 正确暂停常规
  评分，但 V2 scheduler 没有实现权威契约要求的缺失快照一次性本地恢复；现场进入午后后 Tomorrow
  实际已形成草稿但 `draft.items=[]`，前端仍把它误报为“未形成观察草稿”。现在午间缺失会恢复一次，
  合法空草稿也按真实筛选结果展示，不降低门槛或补造股票。

- 修复源码更新后 Flask 仍由 10:45 启动的旧 Python 进程提供 v1 current、却从工作树读取新 JavaScript
  所造成的无限“正在生成”假状态。根因不是 lane 仍在计算：现场状态显示 Today/Tomorrow/D25 lane
  各完成 58 轮、零失败且均已空闲；旧 API 从结构上不可能返回新增 `draft` 字段。

- 修复上一批只覆盖“评分已结束且已有 `input_quality`”的静态状态，遗漏真实冷启动窗口的问题：此前
  首轮评分运行时 `input_quality={}`，前端既不读取已有 `market_data`，也不会在后台评分结束后重读
  current，因此数据新鲜度、行情覆盖和推荐漏斗为空，观察池继续消失。现在两个阶段都有明确状态，
  且质量门禁拒绝正式发布时不再丢弃同批已计算出的观察股票。
- 修复 `not_ready` 渲染在正式空表后提前返回、导致独立观察区永远不写行，以及详情抽屉只在正式
  `items` 查找股票的问题；草稿行现可排序、点击并查看相同的行情/评分/风险字段。

- 修复统一 V2 Web 把“已发布决策”误作所有摘要卡唯一状态源的问题：覆盖门禁令 current 正确返回空
  `not_ready` 时，页面不再把实际候选/评分进度伪装成 `0 → 0 → 0`，也不再把行情年龄和覆盖显示为空。
  同时修复 DeepSeek 账本公开 `planned_limit` 而卡片只读取 `limit` 导致每日上限为 `—`，以及同一市场
  阶段的 15 秒状态刷新不重绘 `not_ready` 摘要的问题。

- 修复此前 Score-P2 实现与权威契约不一致的问题：虽然网关已从免费全市场响应生成完整证券身份，
  应用层仍只把 120 只候选传给持久化入口，导致重启证据长期停留在逐步轮转规模。现在每轮完整
  全市场身份在单个 SQLite 事务中幂等写入，候选外股票不再因未进入当轮评分池而永久缺少主数据。
- 修复组合根已有 DeepSeek 缺钥诊断、但 V2 HTTP 状态投影丢弃整个 `deepseek` 字段的问题；现在
  物理调用为 0 时可直接区分 `api_key_missing`、无合格候选、缓存命中、预算/截止和禁用等受控原因。

- 修复 `.runtime/v2/v2-data/v2-data.sqlite3` 物理损坏时 `./run.sh` 在 Web 绑定前以
  `database disk image is malformed` 退出的问题。已确认直接根因是活动 V2 数据平面整库损坏；导致
  文件损坏的更早外部事件待验证，不能仅凭现有文件归因于断电、磁盘或某次进程退出。修复后持久化
  适配器受控降级，现有只读 Web 和本地内存推荐链可继续启动；本机损坏库已改名保留并由 schema v3
  新库接管活动路径。

- 修复历史批次中一个慢尾触发截止后回滚同批成功股票的问题。截止前已完成且通过 qfq/schema 校验
  的单股结果现在先写入缓存和数据平面，批次仍以 timeout 终态释放身份，仅未覆盖代码进入退避重试。

- 修复公告接口实际只返回首页 100 条却长期把绝大多数证券标为公司风险历史不可核验的问题。分页
  失败、超过上限、未来或畸形行继续 fail-closed，不能被误判为完整历史或解除观察门。

- 修复“评分线程已处理数百只但状态只能看到最终空池”的诊断盲区；现在可直接区分候选特征缺失、
  证券身份缺失、历史不足、业务过滤、无完整评分、无可审候选、低于动作线和集中度约束，避免以
  降低门槛来掩盖供给质量问题。

- 生产审计确认近期并非“正式推荐很多但普遍亏损”：现有 V2 正式记录的可执行项为 0，页面少量股票
  来自明确不可执行的观察池；同时收益仓储没有可执行推荐结算样本，不能据此宣称评分收益已改善。
  根因之一是输入质量实现只检查“至少一只算出分数”，没有执行权威契约要求的完整批次覆盖门禁。
- 修复候选定向响应缺页、证券身份不完整或历史覆盖不足仍可能发布部分评分集的问题。候选特征和
  证券身份现在要求 100% 覆盖，`history_days >= 20` 且 20 日成交额摘要有效的历史覆盖要求至少
  99%；不足统一 `not_ready`、保留最近有效 current，不进入 DeepSeek 或冻结。
- 修复已有活动下行保护只存在于通用排名链、未接入统一 V2 决策的问题。趋势破位、低稳定尾部、
  risk-off 弱收盘、ATR 反转或保护输入缺失现在都不能被高分或 DeepSeek 复核覆盖；动作原因使用
  可持久化且可由现有 Web 中文映射解释的结构化 `downside_guard:...`。

- 修复 R6D 唯一失败项缺少可执行稳定性研究边界的问题：换手不再通过事后放宽父门槛处理，而是由
  预注册候选显式权衡；硬过滤失败股票不能靠历史分数或持续性加分续留，首日没有伪造的新入选惩罚，
  父制品/新制品读取异常均结构化拒绝。

- 修复历史趋势研究的两项可观察性缺口：长回放开始前现在立即向 stderr 输出进度；只读状态从封存的
  候选参数复算候选哈希，不再因 JSON 有意省略派生字段而显示空身份。报告内容及哈希未被重写。

- 最终 Review 修复趋势效率路径分母多包含 `t-60` 涨跌幅的问题，使分母严格对应
  `close(t-60)→close(t-5)` 的 `t-59…t-5` 55 个收益段；新增首日异常收益回归。修复前报告已移至
  `/tmp` 保留备查，未作为有效证据或写入 Git；修复后使用完全相同的预注册参数重新封存。

- 修复观察池按股票代码而非评分展示的问题。根因是领域选择已正确生成高分优先排名，但
  `ScoredDecision` 为规范哈希按代码保存内部条目，查询层直接投影该内部顺序；现在只读查询按已有
  `rank` 恢复展示次序，最多仅排序 12 个已入选项，不增加行情抓取、评分或 DeepSeek 调用。

- 根因确认：V2-E10 删除旧 Pipeline 后，生产组合根仍构造研究读取器，却没有实例化或调用既有
  `ResearchCoordinator`；结构化风险长期缺失令候选按 fail-closed 规则保持 observe-only，因而不会
  进入 DeepSeek 复核。现已恢复 V2 原生异步意图、结果重评分及多策略独立首轮屏障，不放宽风险门槛，
  也不会用 DeepSeek 自由文本替代结构化风险事实。

- R7 证据加载现在重构并校验完整 R6 spec、逐日同股记录、报告及冻结候选，随后重新执行 R6 门禁；
  缺失、额外日期、内容篡改、父哈希/候选不一致、非 `promotion_eligible`、路径型 identity 或复算哈希
  不一致均 fail-closed。local-only 档案明确把未通过的 hybrid 门禁标为非拟发布范围，不能误报模型增益。

- 修复 Score-R6 只有计划描述、没有可执行且防泄漏的选择/评价链路的问题：缺失公司、DeepSeek、ST、
  行业或盘中风险事实不再被当作零风险证据；训练与验证严格隔离，小样本板块不得独立过拟合，最终
  前向报告必须绑定全部 20 个逐日哈希，且 hybrid Gate 失败不能借 local 结果获得 hybrid 晋级资格。

- 修复短历史只含一根日线也会被标记 `complete`、令 `research-status` 过早开放 Score-R6 筛选的问题；
  现在短响应不写完成内容并保留 `invalid_history`。同时修复腾讯缺少 `qfqday` 时把原始 `day` 错标为
  前复权的问题，严格交给同语义前复权回退源或失败，不再把未复权数据混入 H0。

- 修复原 H0 报告只携带 spec hash、没有股票池/逐股内容/实现/报告哈希且 CLI 无法序列化训练与验证
  `date` 的问题；空归档回测现在保持只读、输出 ISO 日期和可复算哈希，并以
  `insufficient_coverage` 返回，不创建运行目录。

- 修复 H0 已允许历史筛选后，`research-status` 仍同时返回 R5 晋级阻塞并与
  `score_r6_executable=true` 自相矛盾的问题；现在筛选就绪、`score_p0_v2` 离线评价进度和生产前向
  晋级分别报告，历史筛选达标不会被未来点时窗口错误关闭，生产晋级仍保持 false。

- 修复运行证据连续性恢复后仍只有旧 `score_p0_v1_historical_point_in_time_missing` 永久阻塞、后续
  observation 虽持续增长却没有可执行研究身份的问题。新窗口从首个观察日前固定，避免继续等待不可
  恢复的旧 Gate，也避免把 8 月 20 日预注册前的部分日数据、后来补抓数据或失败日冒充完整样本。

- 最终 Review 修复旧 v1 报告兼容性：新增的 identity/spec 字段只参与 v2 canonical hash 和载荷，
  v1 继续生成原 schema/hash；R3 解码缺少新字段的已落盘报告时恢复冻结的 v1 默认身份，避免旧证据
  因模型扩展变成不可读。

- 根因确认：旧研究库载荷已达 `67,106,829 / 67,108,864` 字节，异步 observer 捕获容量异常后只在
  内存累加，生产推荐仍正常运行，因此“服务跑了一整天”和“完整研究证据继续增长”同时出现分歧。
  现在 observer 队列、完成/拒绝/消费者失败计数及当前错误进入 `/api/v2/status`，未恢复的消费者
  错误会把健康状态标为 `degraded`，后续成功写入才清除当前错误。

- 修复生产组合根实际注入 `V2NoopSettlement`，导致调度器显示结算成功但没有任何 outcome/基准落库
  的问题；改为真实服务从正式记录派生目标、读取复权历史并不可变结算，完成/失败计数进入 scheduler
  状态。非有限锚点或 ATR20 现在落为 `insufficient_data`，不会产生 NaN 收益或回撤证据。

- 修复控制台仅显示 `127.0.0.1:5000` 或带中文前缀的裸地址、终端无法稳定识别为可点击 Web 链接的
  问题；实际监听 host 和 port 仍由统一运行配置决定，不新增浏览器启动副作用。

- 修复通过 `run.sh`、`run.ps1`、`run.bat` 或直接执行 `trader-server` 启动成功后，终端没有明确访问
  地址、使用者仍需从配置推断端口的问题。绑定失败或运行时未启动时不会输出不可访问的地址。

- 修复评分输入已有完整腾讯行情，但 `_decision_item` 只复制名称/行业、窄 overlay 又只保存价格/涨跌、
  查询层把成交额/换手率/总市值硬编码为 `None`，最终令观察池四列显示 `—` 的跨层丢失。新发布决策
  不再暴露“身份已可见、行情尚未挂接”的窗口，行情刷新失败仍保留上一份有效快照。

- 修复 `1593 分` 容易被理解成评分且分钟显示丢弃剩余秒数的问题。数据年龄和快照元信息现在复用
  同一个 HMS 字符串，不新增第二套计时状态，也不改变行情来源时间或陈旧判定。

- 修复“候选覆盖”和“推荐漏斗”重复展示候选/已评估数据、Long 用后端局部覆盖数而不是 224 只完整
  固定名单作分母，以及历史报价 overlay 更新表格后行情覆盖卡未同步的问题。行情状态现从统一决策
  载荷保留到展示模型，固定名单无行情时显式标记 `missing`，不会把占位来源误计为可用行情。

- `AGENTS.md` 原文没有要求“最小运行链修复”，但“只修改需要的边界”“最小充分验证集”和“最小
  测试”缺少更高优先级的方案选择原则，容易让代理把测试成本控制误读成补丁式实现要求。现已明确
  禁止以“先跑起来”代替完整修复，并要求用可审查证据说明根因、目标架构、方案取舍和完成条件。

- 实机数据平面确认 `security_master_recent` 为 0，当前 Tushare 仅 120 积分且无证券主数据/交易日历
  权限；原运行链又未调度参考刷新，也未保存东方财富全市场行情已携带的身份字段，导致重启或免费
  主源短暂失败后板块、上市日期和上市交易日数同时缺失，短线候选在 0.85 板块可靠度门槛前被过滤，
  Web 只能显示空观察池。现在免费身份会幂等写入并在启动时恢复，生产日历补齐上市交易日数；
  `freeze_sealed`/`freeze_closed` 仅计为预期发布拒绝，不再虚增“最近错误”。名称和 Long 行情字段
  保持从统一决策/API 到桌面列表完整渲染，正式筛选、风险和评分门槛均未放宽。

- 修复现有界面只读取 `last_error`、但运行组合根主要维护 `last_error_code`，导致系统已降级却无法在
  主界面定位原因的问题。现在成功发布只恢复刷新/构建/复核/发布阶段错误，冻结或结算成功只恢复各自
  阶段，避免一次普通发布误把冻结失败标成已恢复；当前策略快照的降级原因也会与运行错误去重合并。

- 修复重启加载共享输入实现后 Today、Tomorrow、D25 观察池全部消失的问题。生产隔离复现确认：请求
  于刷新开始时定时，而候选报价的本地观测/接收时间在网络完成后晚数秒，旧构建仍使用请求时刻，因而
  三策略共同触发 `scored native input cannot contain future features` 并被降级成笼统 `valueerror`。
  现在决策使用可信本地完成时间，保留对伪造未来供应商时间的拒绝，并把该类异常映射为结构化
  `future_input_time`。同时修复 scored 投影丢弃 quote 名称/行业、查询层硬编码空字符串造成名称显示
  `—`；历史缺字段记录不改写，运行服务必须正常重启才能加载修复。

- 修复用户当前运行中 Today/Tomorrow 长时间无快照、D25 虽 ready 但观察池为空且显示
  `candidate_count=26125` 的问题。根因确认是三条独立策略每轮重复执行全市场、候选历史和结构化研究
  慢 I/O，实际只有 D25 偶发完成；同时查询层把一只股票可命中的多个过滤原因直接求和，造成覆盖数
  重复。现在共享快速行情输入、本地先发布且慢研究留在独立后台，刷新失败直接保留最近有效决策，
  不再继续制造误导性的 `decision_unavailable`；覆盖统计按去重股票数返回。

- 修复 R4 之后只有配对 manifest、尚无可执行统计晋级边界的问题：此前无法证明固定随机流、Holm
  多重检验、集中度/稳定性失败终止，也没有可恢复且冲突安全的固定前向记录。R5 现在对缺日、短区块、
  缺失 p 值、样本不足、版本漂移、计划日失败和持久化篡改全部 fail closed，并禁止缩小检验族或顺延日期。

- 修复 Score-R3 后仍只有基线指标、没有可执行五挑战者隔离回放和 local/hybrid 同股配对边界的问题。
  R4 现在拒绝未记录 facts 冒充 hybrid、生产基线身份漂移、不连续或不稳定 Top6、板块/行业超限、
  observe-only 入选、硬热度拒绝身份泄漏，以及无 active-set loaded 证明的 Top120 外候选扩展。

- 修复 Score-R2 之后缺少可执行基线报告边界的问题：此前只有点时提取、结算依据和 active-set 证明，
  无法以固定口径形成日级/汇总指标或验证重复运行哈希；现在通过显式生产回放端口、严格 Top6/集中度/
  Top120 身份校验和不可变报告仓储闭合该缺口，同时继续拒绝伪造缺失历史证据。

- 修复 Score-R2 实施前仅有两阶段端口 schema、没有可执行历史提取、上界保护或不可变分区的问题。
  新边界明确校验 summary/full-field 输入哈希一致、评估分不得超过点时乐观上界、生产 Top120 必须
  满足候选 50 分和核心缺失不超过 30%，并拒绝未来数据、日线/分钟同键异内容、复权窗口重复、三板
  结算缺口及已确认风险被乐观上界抹除。

- 用户报告“推荐快照身份不匹配 / 推荐快照读取失败”，并指出今早、明日、2–5日均无荐股且页面下方
  观察池消失。实机确认 Tomorrow 与 D25 后端 current 各有 2 条 `observe`，但前端仍只接受旧内部
  `live/official` 身份而拒绝统一 API 的合法 `view=current`；即使绕过身份错误，status 又固定返回
  技术标签 `phase=v2`，导致观察池时段判断继续隐藏两组数据。现在三种短线 current 均通过身份校验，
  调度器公开真实阶段，未冻结盘中 current 的观察项恢复到独立观察池；正式推荐计数仍只统计
  `executable`，不会把观察项冒充荐股。

- 修复 Today 已有可冻结草稿却在 11:20 丢失的问题。2026-08-13 运行研究库证明 Today 在
  09:50-11:11 已成功提交 9 个 current，但冻结协调器只接受微秒恰为零的 `11:20:00.000000`，而
  调度器把整个 `11:20:00.xxx` 秒识别为冻结点，真实线程延迟因此被误判为 `missed_freeze` 并丢弃草稿。
  两侧现在按同一调度秒对齐，同时封口决策仍必须满足 `observed_at <= 11:20`。

- 用户报告 `research risk component persistence failed for component=penalty code=603083`。运行库确认
  该代码同一观测时刻的八个风险组件（含 penalty）最终均已健康提交；根因是同一代码、组件和
  `observed_at` 的不同研究版本重复写入时，SQLite recent 契约正确保留先提交记录并抛出
  `DataPlaneConflictError`，研究辅助层却把这个安全的 first-wins 结果误报为持久化失败。现在该冲突
  被单独识别，真实不可用仍显式降级，其他异常仍保留可诊断告警。

- 用户报告 `history persistence failed for 301717`。根因已确认：盘中获取当日日 K 时，持久化层
  无条件把来源时间推定为当日 15:00，导致 `observed_at < source_time` 并被数据平面不变量拒绝。
  现在来源时间不会晚于真实观测时间，当日历史记录可正常进入 SQLite 数据平面，失败仍按既有
  降级策略隔离且不阻塞内存历史与本地推荐。

- 修复原 committed event 研究 trace 仅驻留内存、重启后丢失且未保留 R1 候选审计语义的问题；现在
  规范载荷和内层审计使用独立 SHA-256，启动逐行校验并隔离损坏记录，数据库初始化/写入失败由有界
  observer 隔离，不回滚正式决策、冻结、API/SSE 或增加 DeepSeek 请求。

- 用户再次反馈“Web 上又没数据”。实机确认 Long API 始终有 224 条，但前端 current 身份校验只
  接受短线 `live/official`，把 Long 合法的 `view=current` 拒绝后永久回退到无价格静态名单；同时
  首页固定打开 Today，而当日 Today 按 11:20 规则为 `not_ready`。现在 Long current 可进入缓存和
  渲染，首屏自动落到实际有数据的策略。另修复生产调度适配器绕过 `input_quality.publishable`、把
  stale/history 未就绪空集发布并冻结为 ready 的缺口；15:00 精确点不再让收盘恢复与尚在完成的
  14:50 正式冻结并发竞争，下一调度 tick 才允许缺失策略执行收盘恢复。

- 用户反馈“Web 上又没数据”。实机复现确认不是展示层问题：调度器在交易日 `AFTER_CLOSE` 阶段既不
  评分也不刷新 Long，导致 15:00 后启动时 Today/Tomorrow/Long 均为 `not_ready`；补上收盘恢复后又
  发现历史预热完成回调会立即续批，并在共享 latest-wins history lane 中 supersede 已排队的候选历史
  请求，使本地补算以 `SourceRequestSupersededError` 失败。本批补齐收盘恢复端口与调度，并让后台
  预热礼让业务请求；实机重启后 Long 恢复 224 条固定名单行情，Tomorrow 创建同日
  `close_fallback`，已有 D25 正式记录保持不变，Today 未被违规追补。

### Removed

- 退役 `research-history`、`research-screen`/`screen-history`、`research-baostock-history` 及其旧下载、回测、R6/P2 筛选和 holdout 分派；不保留兼容别名，避免与 BaoStock 下载及 Tomorrow 训练形成重复流程。

- 从活动权威计划移除 V3 的 3000 日扩容目标、`rolling_1500` 窗口比较、V1/V2/C3 原始预测联合/stacking
  路线，以及复用已完成 15.1.32 留出的描述；旧联合器实现及数据不足工件仅作为历史审计保留，本批不删除
  运行代码或既有工件。

- `codex-a-h1-capability-audit-v2`：移除“任一免费来源失败就放弃整份能力审计”的共同成功前提；未删除
  H1 归档、既有研究工件、生产资源或活动 V1/V2 行为。

- 删除已经只剩迁移索引、且与荐股权威文档形成重复入口的 `docs/trade.md`，同步删除其专属契约测试；
  V3 训练、工件和主程序交接只由 `docs/recommendation-strategy.md` 第 15.1.35–15.1.37 节定义。

- 从未完成路线物理删除历史 DeepSeek 盈利增量回测、自动模型族/参数搜索、净效用自动校准、组合黑盒
  优化、独立逐股严重亏损概率模型、定时/在线/无人授权重训、Champion/Challenger 注册表、自动晋级、启动激活和自动
  回退章节，不保留隐藏 TODO 或未来自动接入入口。历史终端留出只产生
  `production_authority=false` 结论；任何候选即使通过，也必须由用户另立单一策略、单一候选的高风险
  人工生产变更批次。本批不删除或改变现有生产 DeepSeek、固定 68/32 融合、风险、阈值、冻结、配置、
  活动代码或既有历史工件。`Regression-Key: historical-score-roadmap-priority-pruning-v1`。

- 移除第 15.1.23–15.1.36 旧未完成编号与“模型完成后直接进入留出/自动化”的路线歧义；没有删除或修改
  任何生产代码、活动策略、冻结记录、历史研究工件、模型、配置、API 或 Web 行为。
  `Regression-Key: recommendation-chain-scientific-roadmap-v2`。

- 已排除代码不再进入非冻结的历史预热、候选/Long 定向行情、市场新闻、公司风险、参考数据和分钟行情
  请求，也不再占用评分或 DeepSeek 复核名额；原动态规则中的配置黑名单、ST/退市和永久结构化风险仍
  作为防御性二次校验保留。未删除既有历史缓存或冻结记录，避免破坏审计与收益结算。

- 移除未完成路线中“模型候选选择后直接进入三策略留出、缺少残差/DeepSeek 增量/自动生命周期”的旧
  章节结构，以及把所有自动晋级一概排除而无法表达受控离线自适应的笼统措辞；不删除任何活动代码、
  历史工件、生产档位、风险规则、冻结记录、API、Web 或运行数据。

- 删除 R5 未来日采集、P1 预注册未来影子、R6 未来窗口、R7 晋级档案、运行期 V1/V2 全候选比较与补充
  outcome 结算的领域、应用、仓储、CLI 和脚本入口；同步删除其旧业务测试，不保留兼容 shim、双写、隐藏
  fallback 或活动状态字段。`score_p0_v1`、`score_p0_v2` 与 P2 既有失败事实仍按固定身份只读归档。

- 退役 `docs/plan.md` 及其中的迁移账本、批次状态和临时完成清单；不再把一次性施工计划作为活动输入，
  也未保留旧目录、兼容别名、重定向或双实现。

- 物理删除 application 根级研究/结算模块、旧 profile 端口和对应根级测试路径；删除 research 包的聚合
  `__all__`/重导出和所有旧导入路径。未保留兼容模块、弃用窗口、双读双写、动态 fallback 或旧生产实现。

- 物理删除 `web` 根级 `routes.py`、`routes_v2.py`、`route_services.py`、`decision_serializers.py` 和
  `decision_sse.py` 旧路径；未保留转发模块、兼容别名、重复 blueprint 注册、双 serializer 或隐藏
  fallback。

- 物理移除两个旧运行时模块文件名、对应单元测试文件名、旧类/方法/字段符号以及 schedule point 旧 JSON
  字段；未保留兼容模块、别名、双 JSON 字段、反射 fallback 或历史文本例外，Git 历史不改写。

- 移除 `DataPlaneCoverage` 和生产输入质量中的 99% 候选历史整批否决权，以及浏览器“至少 357/360”
  固定文案；不删除 20/40/60 日因子、61 日模型输入、流动性硬过滤、风险、动作、TopK 或冻结门槛。

- 移除共享选择器把单一候选评分水位隐式同时用于人口预选的行为；不删除任何行情来源、历史回退、
  风险规则、评分公式、门槛、预算、冻结记录或旧 release 数据。

- 移除 decision SSE 的独立 `filtered_count` 线字段及浏览器内同名双表示；过滤数只来自完整 coverage 的
  `rejected_count`，不再让单个新字段与整组旧 coverage 混合成不可能的漏斗。

- 删除 `run.sh`/`run.ps1` 对九个底层阶段的逐项公开映射，避免用户在 Shell 层自行拼接顺序；未删除
  底层 CLI 实现、不可变研究身份或既有研究工件。

- 删除活动生产命名中的 `p1|p2` 配置别名以及 P1 命名的 V1 打包脚本、训练模块、测试和包内资源路径；
  不删除或改名不可变 P1/P2 研究规范、封存报告、历史特征字段和既有 Changelog 证据。
- 移除 Tomorrow 生产评分对旧人工权重本地分的隐式 fallback；模型输入不足的股票显式不评分。没有删除
  Today/D25 仍在使用的启发式实现，也没有改写 P2 历史拒绝报告或创建被禁止的 P2-2 前向身份。
- 删除已完成语义归位的 `docs/review.md` 与 `docs/fenshu.md`；其历史用户诉求、运行实证和逐批交付结果
  继续由本 Changelog 追溯，不在权威文档保留版本流水，也不保留兼容副本或重定向文件。
- 移除 `supply_funnel` 通过 `asdict()` 自动扩散公开 JSON 字段的投影方式，改为状态 adapter 显式字段
  白名单；移除 Web 对生产响应中不存在的 `blocked_reason_counts`/`selection_skip_reason_counts` 作为
  聚合解释来源的依赖，不保留双表示或隐藏 fallback。
- 移除证券主数据刷新必须等到交易日评分批次、并受东方财富/新浪实时报价 20 秒 deadline 间接约束的
  隐含单点；未删除或替换任何活动报价源，Tushare 仍仅为可选增强。
- 本批未删除 H0、P0、P1、活动评分或生产发布链；P2-0 只新增隔离研究契约。构建生成的 `build/`、
  `dist/` 与 egg-info 仍是忽略产物，不进入本任务提交。
- 删除首页的上市日期/交易日龄缺失构成、免费补齐过程文案及旧 `quoteCoverage*` DOM/渲染 helper；
  后端 `security_identity_missing_count`、原因计数、证券主数据补齐与 100% 业务门禁继续保留，诊断和
  API 可观察性不因首页简化而丢失。
- 删除已由统一入口覆盖且不再承担独立职责的六个顶层包装脚本：
  `check_web_recommendation_health.py`、`measure_web_refresh_interval.py`、`sample_history_sources.py`、
  `sample_tencent_quotes.py`、`sample_tushare_daily.py` 和 `run_production_performance.py`。保留
  `check_refactor_quality.py`、`generate_long_watchlist_asset.py` 与 `test.sh`，因为它们分别承担架构门禁、
  资产生成和测试启动职责，不能与运行诊断合并。历史 Changelog 中的旧文件名仍作为交付审计记录保留。
- 本批没有删除或替换 R2/R3、生产评分、固定 68/32 融合、78/73 门槛、冻结、DeepSeek、API 或 Web；
  新诊断服务和报告存储未接入组合根、HTTP、调度或活动运行目录，避免离线证据取得隐式生产权限。
- 本批未删除或合并掉任何专项脚本，也未新增诊断到生产请求、调度、评分或冻结链；统一报告不转发逐股
  代码、价格、Token、供应商原始载荷和子进程 stderr，避免“合并脚本”形成第二套业务实现或敏感留档。
- 删除已经完成归并的 `docs/V2.md`、`docs/implementation-plan.md` 和 `docs/start_stop.md`；不保留重定向、
  摘要副本或兼容读取，避免再次形成与两份权威文档竞争的产品、计划和运维定义。
- 移除 `v2_research_readiness_v2` 及其“窗口结束前一律 collecting”的模糊投影；没有删除、补写或迁移
  任何 P0v1/P0v2 研究分区、正式决策、冻结记录、活动评分或历史运行数据。
- 删除测试树中已经无人引用且只记录旧 Pipeline B/C 交接、v15-v17 性能波次的 19 份报告、baseline
  与 manifest fixture；删除四个带旧波次名的测试路径并以当前职责名替换。`docs/reports/` 与
  `CHANGELOG.md` 中的历史引用作为审计记录保留，不参与活动测试、打包或运行。
- 没有删除 R2-R5 离线研究能力：权威策略仍把它们定义为 `score_p0_v2` 与未来 Score-R6/R7 外部证据
  的前置能力，且生产组合根、HTTP、冻结和 DeepSeek 请求链均不可达。当前保留是经可达性与契约复核
  后的明确边界，不是遗留生产实现。

- 删除未被生产组合根调用的 Tomorrow 读一次并融合旧用例及其测试、Today V2 投影别名层、三套旧通用
  today/tomorrow/d25 评分器与权重配置、D25 `not_overheated`/双 multiplier 因子、正式记录 legacy
  decoder、DeepSeek 旧 schema/模型兼容分支，以及引用仓库外 `/tmp` 截图的过时 `design-qa.md`。

- 移除无生产调用者的 `PipelineTask.HYBRID_READY`，并从周期任务集合移除 `SCORE`；评分配置仍保留为
  输入驱动节流策略，不新增兼容事件、双调度链或隐藏 fallback。

- 本批未删除或放宽候选、过滤、评分、风险、TopK、冻结和行情链；诊断报告明确不输出股票代码、逐股
  行情或供应商载荷，也不会因正式推荐/观察数单独为 0 而判错。

- 移除固定 30 秒 supervisor tick 对实时采集周期的事实限制、研究 observer 对 Web SSE 发布的所有权、
  status 请求时的 SQLite 预算读取，以及正式记录已存在时 15:00 后不必要的全市场恢复；未移除或放宽
  评分、过滤、融合、风险、TopK、冻结边界、DeepSeek 168 次预算或 35 秒 Web 展示保留窗口。

- 从代理实施规则中消除“内部状态可按局部方便选择 JSON 字典或对象”的解释空间；本批只修改文档，
  没有删除运行代码、公开 JSON 字段或兼容能力。

- 移除 `CacheIdentity.as_dict()`、`SchedulePointState.to_json()`、`TomorrowInputQuality.to_status()`，
  以及线程池、来源 lane、cadence、缓存和延迟状态的内部字典下标读取；没有增加兼容字典、双实现或
  反射 fallback。

- 移除 overlay publisher 的默认 no-op、调度器的 `submit_tick` 兼容方法及可选 `observe_clock` 反射 fallback、
  运行状态对可选 `input_quality_status` 的空字典 fallback，以及已无生产调用者的
  `legacy_v14_hard_filter`。所有必需能力必须在组合根显式装配，不再用“能启动但无数据”的默认值隐藏缺线。

- 移除 `dashboard.js` 中独立的 `CACHE_MAX_AGE_MS=30000` 生产真相源；浏览器仅消费服务端从当前
  runtime 配置渲染的窗口，无配置的无服务应用以 0 秒失败关闭，不偷偷回退到另一套 35 秒默认值。

- 删除生产不可达且与统一 V2 链重复的 Today/Tomorrow 策略专属 runtime、旧 trading-session 编排、
  旧领域冻结状态机、旧冻结端口、旧候选投影器和旧 columnar provider 适配器，并删除只验证这些退休
  实现的专用测试；统一调度、当前冻结协调器、统一市场数据服务和其生产回归继续保留。

- 移除启动帮助中的单行管道式命令墙和未知命令时的整页帮助输出；未删除任何 CLI、研究能力、参数
  透传、默认启动、Windows 委托入口或环境变量。

- 移除免费证券主数据持久化对 `tushare` lane 和后续评分参考刷新周期的隐式依赖；`tushare` 刷新
  不再重复负责写入免费行情身份。

- 移除“实时报价抢跑输家等同于证券身份无效”的隐含耦合；未移除来源超时、行情截止、证券身份
  100% 覆盖门、Tomorrow 固定 78 分门槛或冻结不可覆盖约束。

- 移除把“草稿存在但观察条目为 0”和“lane 空闲且从未形成草稿”合并为同一文案的前端隐式状态；
  未移除或放宽评分、过滤、风险、融合、排名、观察余量、正式动作阈值、冻结边界或 DeepSeek 预算。

- 移除浏览器验收中预先注入完成态 `input_quality` 和正式观察 current 的捷径；本批没有删除正式推荐、
  历史、阈值、策略入口或 Long 固定名单。

- 移除免费证券身份“仅按候选代码持久化”的隐含范围和逐股独立 SQLite 连接/事务路径；未移除
  候选池上限、历史 30 只批次、公告完整性门、DeepSeek 证据门或 Tomorrow 固定 78 分动作阈值。

- 本批未删除任何版本控制内业务链、正式推荐、研究库或用户文件；只把已由只读 `quick_check` 确认损坏的
  活动数据平面库移出活动文件名并保留可恢复副本，避免新进程继续写入不可信整库。

- 删除 `ReferenceLoader.schedule_reference_data()` 中绕过 `HistoryWarmup` 的全候选历史异步提交路径，
  不再保留两套历史调度状态源。

- 移除“有任意已评分候选即可把覆盖不足批次视为 ready”的失效判定；未删除或替换现有 Web 页面、
  评分权重、阈值、冻结记录、DeepSeek 预算或用户运行数据。

- 本批未删除或替换活动评分、推荐、冻结、Web、DeepSeek、运行数据库或 R6D 不可变报告；未增加
  双生产策略、隐藏 fallback 或运行时自动调参路径。

- 本批未删除或替换生产评分、DeepSeek、冻结、API、Web、正式推荐及运行数据库；未通过换手门槛的
  历史候选没有接入生产，也没有保留活动双实现或隐藏 fallback。

- 本批未删除推荐、观察项、历史记录、运行数据或 Web 资源，也未移除任何评分、过滤、风险、融合、
  TopK、冻结或 DeepSeek 预算约束。

- 移除“公司研究适配器已存在但生产调度无意图入口”的断链状态；未删除或改写运行数据库、正式记录、
  用户截图、Web 资源、决策 API/SSE、评分/融合规则、冻结边界或 DeepSeek 168 次原子预算。

- 移除实施计划中 Score-R7 的未完成标记和已经过时的“下一研究章节”指针；没有删除或替换任何活动
  生产策略，也没有加入自动调权、自动晋级、自动回退或运行时双实现。

- 删除 `research-status` 中无论证据如何都返回 R6 不可晋级的硬编码占位状态，以及 R6 必须隐式等待
  `score_p0_v2` 40+20 窗口的错误耦合；没有删除或改写任何现有研究证据、生产评分、冻结记录或运行库。

- 删除腾讯历史适配器把未复权 `day` 响应伪装成 `qfq` 的隐式回退；生产统一历史链仍保留腾讯前复权
  主源和东方财富前复权回退，不删除任何现有运行数据、快照或用户未跟踪截图。

- 移除替代评价对前序日期回填、失败日换日和旧窗口随机种子的隐式复用能力；未删除、迁移或改写旧
  `score_p0_v1` 研究证据，生产评分、风险、融合、冻结、DeepSeek 预算、API 和 Web 行为均未改变。

- 删除与权威“后台结果结算”契约冲突的 `V2NoopSettlement` 生产实现；未恢复旧生产链、旧 Web
  envelope 或 HTTP 写入入口，也未删除或改写现有运行数据库。

- 删除仅能承载价格和涨跌幅的 `OverlayQuote` 重复领域类型；评分锚点与 overlay 统一使用完整
  `DecisionQuote`，没有新增第二套报价状态源或 Web 请求时抓行情链路。

- 删除“数据新鲜度”数值中的中文“分/秒”单位和整分钟截断；行情时间 `HH:mm:ss`、冻结时间及其它
  中文业务说明保持不变。

- 删除首页独立“候选覆盖”摘要及快照正文中的重复年月日；没有删除候选/过滤统计、股票名单、行情
  字段或推荐业务数据，候选流水仍完整保留在唯一的“推荐漏斗”中。

- 删除协作规则中的“最小充分验证集”和“运行与风险相称的最小测试”措辞，统一改为“风险匹配的
  充分验证”；本次没有削弱高风险完整门禁、固定架构边界或不可破坏的业务约束。

- 本批未删除运行数据、正式推荐或风险规则，也未通过伪造名称/行情、降低板块可靠度、放宽过滤门槛
  或盘后追补 Today 来制造观察项；用户当前运行进程未被擅自停止或重启。

- 删除独立“系统健康/降级状态”摘要卡，以及 Header 中重复的数据年龄、模型预算和冻结信息；主界面
  不再直接展示原始技术原因码，受控代码只保留在错误详情中。

- 本批未删除或替换生产实现；R5 不接入组合根、HTTP、Web、统一决策索引、正式冻结或活动配置，不持有
  模型客户端，也不新增 DeepSeek 物理请求、运行线程或供应商数据读取。未伪造 40 日历史或未来 20 日
  真实证据，未提前执行 Score-R6 调权、门槛研究或生产晋级。

- 本批未删除或替换生产实现；R4 不持有模型客户端、不增加 DeepSeek HTTP、不写活动配置、统一决策
  索引、冻结、正式历史、普通 API/Web 或运行数据库，也未提前实现 R5 统计和前向影子。

- 本批未删除或替换生产实现；R3 研究层不创建第二套行情、评分、冻结、Web、DeepSeek 请求链或活动配置，
  也不移除 R2 失败日和覆盖身份来美化报告。

- 本批未抓取、回填或提交任何真实运行数据库、行情快照、研究分区或收益标签；未复制生产评分公式，
  未新增数据框依赖、DeepSeek 请求、后台线程、运行配置、普通 API/Web 字段或自动晋级行为。

- 本批未删除或改写推荐、观察项、历史或运行数据库；未放宽评分、过滤、风险、融合、Top6 或冻结
  门槛，也未恢复观察池的持久化。冻结、`close_fallback`、历史和盘后仍不展示观察池。

- 本批未删除或改写已有风险证据、SQLite schema、风险映射、处罚规则或评分结果；不通过吞掉未知异常
  来消除日志。

- 本批未删除历史数据、SQLite schema、缓存条目或旧记录；不对修复前失败的未写入记录进行伪造回填。

- 删除 `application/v2_research_trace.py` 的进程内 trace 实现和 `tomorrow_trace` 命名；研究采集不再
  依赖旧 snapshot baseline、tomorrow shadow runtime 或旧运行库，也不提供历史回填兼容路径。

- 本批未删除 API、策略、历史记录或固定名单；保留四策略入口、冻结不可覆盖约束和 Long 全量
  224 只配置身份，不以删记录方式改写当天已经固化的正式空结果。

- 删除失效且依赖已退役 fixture/旧 DOM 的 Chrome runner、runtime schema 5-8 默认补齐路径，以及
  已无任何运行消费者的 `decision_execution_mode=versioned_dag` 配置表象；当前 release 只接受
  schema 9，不读取或迁移旧配置。

- 删除旧双链测试、旧性能入口与只服务旧链的 CLI/配置接点；V2 测试改为验证旧资源不可达、V2-only
  路由和固定 Long Tab；同时删除未被 V2 组合根引用的旧事件队列、板块评分协调器、snapshot schema/
  migration、outcome settlement port、性能脚本和测试工厂，避免退役模块继续进入源码或测试树。

### Verification

- 本批 15.1.23：定向契约与单元测试通过（基线审计一致、冲突、运行身份不可得、重复 hash、CLI
  只读投影）；Ruff 和 mypy 对受影响 Python 文件通过；绝对配置运行
  \`trader-cli research-baseline-audit\` 输出 10 项带来源 SHA-256 的声明并返回
  \`live_identity_unverified\`。未运行全量门禁（待本批最终高风险验证阶段执行）。

- `tomorrow-daily-close-training-proposal-v1`：新增契约先在 `docs/trade.md` 缺失时以 2 项失败证明边界，
  文档完成后定向 2 项通过；完整 `tests/contract` 共 153 项通过，覆盖非权威定位、收盘代理限制、硬过滤
  失败关闭、DeepSeek 训练隔离、时序切分、统一模型和人工生产授权。新增契约文件的 Ruff format/check、
  Ruff lint 与 `git diff --check` 通过。`make test`、`make package`、仓库外 wheel、真实供应商和三档浏览器
  验收不适用：本批只新增 Markdown 总结、Changelog 和读取该文档的契约断言，不改变活动代码、配置、
  构建、入口、行情、评分、冻结、API 或 Web。

- `historical-score-roadmap-priority-pruning-v1`：先更新路线机器契约并确认旧文档按预期失败；完成两份权威
  文档收敛后，`tests/contract` 全部 151 项通过，覆盖 15.1.21/22 完成状态、15.1.23–15.1.34 新顺序、
  两层过滤先于 H1、三策略独立点时覆盖、有限候选上限、Holm、独立留出、自动优化删除和人工生产变更
  边界。两个受影响契约测试文件的 Ruff format/check、Ruff lint 与 `git diff --check` 通过。
  `make test`、`make package`、仓库外 wheel 和三档浏览器验收不适用：本批只修改 Markdown 和读取这些
  文档的契约断言，不改变活动代码、配置、构建、入口、API、Web 或运行行为。

- `recommendation-chain-scientific-roadmap-v2`：更新路线契约后，21 个读取两份权威文档的 contract 测试文件
  共 89 项通过，覆盖 15.1.21/22 已完成状态、15.1.23–15.1.40 顺序、两层过滤前置关系、点时/留出纪律、
  基线一致性、过滤召回、组合净效用、等价性能和生产授权边界；目标测试在文档更新前按预期失败。
  受影响测试文件的 Ruff format/check 与 `git diff --check` 通过。`make test`、`make package`、仓库外 wheel
  和三档浏览器验收不适用：本批只改权威 Markdown、Changelog 和文档契约断言，不修改运行代码、构建、
  入口、资源、API 或 Web 行为。

- `web-stale-frozen-degradation-truth-v1`：宿主初始确认没有活动 `trader-server`，随后从基线
  `f00bb17147a0ab87eb89ce3a2bc43a1369e4cae6` 通过 `./run.sh` 正常启动并加载
  `v2_status_v12`/`release-contract-2026-09-01-v13`；修复前 live 诊断连续 6 轮读取成功，官方证券名录
  5214/5214、腾讯报价和 Tushare 日线通过，历史预热由 307/360 推进到 343/360。失败优先回归确认旧
  Web 不接受 v13 握手，状态 API 不公开公司研究，诊断漏报零评分语义矛盾；实现后 Web/诊断/API/架构
  定向 64 项与 Node 页面状态契约通过。`make format-check`、`make lint`、`make type-check`、`make test`
  和 `make package` 全部通过；重启后实机确认 `v2_status_v13`、
  `release-contract-2026-09-01-v14`、公司研究完整白名单及冻结 Tomorrow 原身份。统一 `full` 诊断中 Web
  按预期以 4 项 warning 降级，官方证券名录 5214/5214、历史、腾讯、Tushare 和 Firefox 子项通过；与真实
  服务并发的首次离线性能子项因 overlay p95 102.803ms 超出 100ms 门限而失败，正常停止服务后同一隔离
  门禁通过（p95 95.825ms），未改动无关 overlay 热路径。三档 Firefox 验收均无浏览器错误、页面级横向
  溢出或布局重叠；仓库外 wheel 安装后可导入 0.2.0、执行 `trader-cli --help` 并读取模板、JavaScript、
  CSS 与 SVG 资源。`git diff --check` 通过。

- `two-level-permanent-issuer-eligibility-v1`：先新增契约和回归并确认因缺少资格领域/持久化模块而失败；
  实现后定向运行一级点时规则、SQLite 幂等/冲突/篡改、CLI、财报历史、历史预热、候选/Long/研究/
  分钟请求裁剪、Bootstrap、Web/API 与历史路线契约通过；评分融合、DeepSeek 预算、冻结恢复、SSE
  游标/慢客户端、哈希一致性、架构 AST 和 app factory 专项共 186 项通过。`make format-check`、
  `make lint`、`make type-check`、`make test` 和 `make package` 最终全部通过，276 个源文件无类型错误、
  严格重构债为零；Lint 首轮发现 5 处导入排序和 2 处新增复杂度债，均修复后重跑通过，没有扩大债务
  基线。打包首轮只因沙箱禁止隔离环境下载 setuptools 失败，获准联网后原命令成功构建 sdist/wheel。
  仓库外安装 wheel 后包导入、`trader-cli`、模板/CSS/JS/SVG 和 `v2_status_v12`/静态资源身份通过。
  离线生产性能门禁通过，网络调用为 0、status API P95 为 1.204ms；浏览器刷新门禁通过，patch-to-paint
  P95 为 27ms。Firefox 三档 1280x720、1440x900、1920x1080 验收通过，无白屏、重叠、页面横向溢出
  或浏览器错误，静态请求均使用 `release-contract-2026-09-01-v13`。只读资格 CLI 对当前尚无新库的运行
  目录返回空 manifest 且完整性通过；本批未为验证调用行情供应商或 DeepSeek。

- 历史自适应 DeepSeek 评分路线按低风险文档/机器契约批次验证：先运行 6 个直接相关契约文件通过
  26 项，再运行完整 `.venv/bin/pytest -q tests/contract` 通过 149 项；受影响契约测试文件的 Ruff 与
  format check 通过，`git diff --check` 通过。未运行 `make format-check`、`make lint`、
  `make type-check`、`make test`、`make package`、仓库外 wheel、真实 DeepSeek 和三档浏览器门禁：本批
  只更新待执行策略路线、软件设计交叉索引和文档断言，没有修改活动 Python、配置、入口、API/SSE、
  Web、包资源、模型或运行行为。

- Tomorrow 0 分解释修复执行失败优先回归：新增 Python 用例先证明 10bp 毛预测在固定成本后全为 0 时
  仍被误标为 `score_below_observation_floor`，JS 用例先证明页面误报为距正式线 78 分；修复后两项定向
  回归、评分/输入状态/决策身份/Web API 定向 59 项和权威文档契约通过。`make format-check`、
  `make lint`（严格重构债为零）、`make type-check`、`make test`、`make package` 全部通过；仓库外安装
  wheel 后包、入口、资源和新页面原因通过。三档 Firefox 桌面验收通过，无白屏、重叠、横向溢出或
  浏览器错误。现场对照以同一 `run.sh` 安全重启且结果仍为 0；只读历史重建仅使用 2026-09-01 之前的
  本地 qfq 日线，2,781 只中正净效用为 0，未请求或采集未来评价数据。修复后真实 current API 返回
  `maximum_final_score=0.0`、`empty_reason=no_positive_net_utility`，状态 API 同步以该原因为首要阻断，
  并继续公开 72/297 的 61 日历史覆盖。

- `run.sh` 活动实例提示修复按入口/生命周期高风险门禁验证：失败优先回归先证明旧帮助仍写五阶段且
  锁冲突只输出原始异常；修复后入口与进程锁定向测试 4 项通过。`make format-check`、`make lint`
  （严格重构债为零）、`make type-check`、`make test` 与 `make package` 通过；仓库外安装 wheel 后包导入、
  `trader-cli`/`trader-server --help` 及模板、CSS、JavaScript、图标资源读取通过。真实运行先确认旧 PID、
  根页面 HTTP 200 和有效锁，再等 Today 于 11:20 正式冻结后发送 SIGTERM；修复后 `./run.sh` 输出
  `http://127.0.0.1:5000`、根页面返回 200，第二实例返回非零并显示同一地址、安全停止方式和禁止删锁。
  运行诊断连续 3 次 API 采样成功、无错误，但因采样期历史预热失败计数从 1 增至 2 如实标记 degraded。

- 历史评分优化规划（低风险文档/机器契约）：
  `.venv/bin/pytest -q tests/contract/test_historical_score_optimization_roadmap.py
  tests/contract/test_historical_only_score_validation.py tests/contract/test_score_plan_contract.py
  tests/contract/test_score_research_detailed_strategy_contract.py tests/contract/test_v2_only_product_contract.py`
  通过（19 项）；新增契约文件的 Ruff 与 format check 通过，`git diff --check` 通过。未运行
  `make format-check`、`make lint`、`make type-check`、
  `make test`、`make package`、仓库外 wheel 和浏览器门禁：本批只新增待执行研究契约与文档断言，未修改
  Python 活动实现、入口、依赖、包资源、API/SSE 或 Web 行为。

- 历史唯一验证、R6/P2 不可变身份、风险模型/报告封存、H0 数据集、CLI 组合、研究状态、生产组合根退役、
  API schema/资源握手和旧路径负向契约定向通过；完整 `tests/contract` 与 `make test` 通过。
  `make format-check`、`make lint`（严格重构债为零）、`make type-check` 和 `make package` 通过；打包首次因
  沙箱禁止隔离环境下载 setuptools 失败，经授权联网后重跑成功。`git diff --check` 通过。三档桌面浏览器
  不适用：本批未改页面布局或交互，Web 变化仅为删除状态字段并同步 schema/静态握手，已由 API 与 JS
  契约覆盖；真实供应商/DeepSeek 不适用，因为新链只读本地 H0 归档且生产 I/O 行为未变。

- 最终架构/文档契约、功能包边界、JSON 序列化边界、`create_app()` 无副作用、融合固定向量、预算并发、
  latest-wins/停止、冻结恢复、SSE 和 Web 资源回归通过；`docs/plan.md` 不存在，迁移账本不存在，所有旧
  路径和兼容导入扫描为 0。完整 `make format-check`、`make lint`、`make type-check`、`make test`、
  `make package` 和 `make performance-check` 均通过。
- 三档桌面 Chrome 验收通过（1280x720、1440x900、1920x1080，无白屏、页面级横向溢出、Long 区域重叠或
  浏览器错误）。统一 `scripts/diagnose_runtime.py --profile full --output -` 如实返回 `failed`：生产性能
  子检查通过；Web endpoint、交易所证券主数据、腾讯行情和浏览器刷新为连接/环境失败，历史源为空响应
  降级，Tushare 因缺少 token 降级。该结果不作为真实供应商或 Firefox 发布级通过证据。

- `tests/unit/application/research`、`tests/unit/domain/research`、`tests/unit/infra/research`、结算、trace、
  score-plan、功能包、架构和 E9 入口契约全部通过；CLI 导入研究模块数为 0，server 仅加载允许的后台证据
  消费者。`make format-check`、`make lint`（严格重构债务 0）、`make type-check`（288 个源码文件）、
  `make test` 和 `make package` 全部通过，`git diff --check` 通过。
- 仓库外 wheel 安装后实际从临时 `site-packages` 导入 `trader`，新 research/outcomes 模块可导入，旧模块
  均不可发现；`trader-cli --help`、绝对配置路径 `validate-config`、只读 `research-status`、模板/CSS/JS/
  两个 SVG 资源和 `pip check` 通过。统一 `scripts/diagnose_runtime.py --profile research --output -`
  完成并只输出有界聚合报告；报告按既有 `score_p0_v2_historical_planned_dates_missed` 状态为 `failed`，
  未将该运行库状态误报为重构通过。

- Batch 8 定向 Web/架构/应用回归通过：E8、app-factory、decision query/stream、DeepSeek Web 组件、
  功能包和架构契约全部通过，`node --test tests/js/test_dashboard_state.js` 直接命令通过；
  `make format-check`、`make lint`（严格重构债务 0）、`make type-check`（285 个源文件）、`make test`
  （pytest 100%）和 `make package` 全部通过。
- 最终 wheel 在仓库外临时 target 安装并从安装路径导入 `trader.web.api` 四个模块，实际执行
  `trader-cli --help` 和绝对路径 `validate-config`，模板、CSS、JavaScript 与两个 SVG 资源均可读。
  Chrome headless 的 1280x720、1440x900、1920x1080 三档桌面验收 `passed=true`：尺寸精确、无白屏、
  页面级横向溢出、Long 区域重叠或浏览器错误，API/SSE、观察池、错误抽屉和四策略视图正常，外网调用 0。

- 本批定向 cadence/latest-wins/runtime/schedule/workers、调度集成、bootstrap/app-factory、架构、功能包、
  E3/E8 Web 与诊断契约共 154 项通过；`make format-check`、`make lint`（严格重构债务 0）、
  `make type-check`（285 个源文件）、`make test`（pytest 100%）和 `make package` 全部通过。
- `git ls-files` 路径扫描与 `git grep -in` 内容扫描均为 0；仓库外临时 target 从最终 wheel 导入
  `LatestWinsWorker`、`ApplicationResources` 和 `trader`，实际执行 `trader-cli --help`，并读取模板、CSS、
  JavaScript 与 SVG。Chrome headless 三档桌面验收 `passed=true`：1280x720、1440x900、1920x1080 均无
  白屏、页面横向溢出、Long 区域重叠或浏览器错误，v11 资源全部命中，外部网络调用为 0。

- `tomorrow-v1-v2-all-candidate-profit-evidence-v1` 回归覆盖：两档都选 0 时仍保存全部共同可评分候选；同一
  native 的 local/hybrid 重放幂等；正式绑定只接受精确 `input_versions.native` 并清理同日临时输入；
  缺 ATR 只让该候选 T+1 标为 `insufficient_data`；未完成全候选结算或少于 300 条完整配对的横截面不计
  独立日；候选层与组合层报告、只读空库状态、研究库初始化
  失败开放、`create_app()` 零数据库副作用、status v9 白名单和 research-screen 五阶段均通过。
- 真实 `.runtime/v2` H0 留出命令完成 139 日、687,321 条同口径配对，复算报告 hash
  `47e2b9bfd4d404521f8251e2e51c491aa96c1bc0d8423dea95e63320daa6e3bf`、验证证据 hash
  `a38062efe934c15e38081bf095a3bc606235c62bd14fdcc613ee886c4335b793` 与日级 V2−V1 标准差
  `3.831660%`；`research-status` 只读公开 readiness v5、holdout 终态和配对 collecting 状态。
- 高风险完整门禁 `make format-check`、`make lint`（严格重构债为零）、`make type-check`（270 个源码
  文件）、`make test` 和 `make package` 最终通过。首次全量测试暴露诊断脚本仍只接纳 readiness v4，
  升级为 v5 后定向及全量复跑通过；首次隔离构建因沙箱网络限制失败，获准联网后成功生成 sdist/wheel。
- 全新仓库外 `/tmp` 环境安装最终 wheel 及声明依赖后，包从安装目录导入，`trader-cli --help`、
  `validate-config`、`trader-server --help` 和 `pip check` 通过，模板、CSS、JavaScript、两个 SVG 与两套
  Tomorrow 模型资源均可读。V1/V2 离线性能门禁均通过、零网络调用、内存增长 0%；status API P95
  分别为 1.757ms/1.026ms。Firefox 刷新门禁 DOM P95 1.051s、patch-to-paint P95 13ms；三档桌面
  1280x720、1440x900、1920x1080 均无白屏、横向溢出、Long 重叠或浏览器错误，外网请求为 0。
- 当前分支服务在正常权限下重启后，统一 runtime 诊断连续 3 轮 status/current 采样通过、无错误或告警；
  status v9 明确投影配对库 `initialized=true`、`collecting`、`0/522`、最少 300 条完整横截面、
  `production_authority=false` 和 `automatic_profile_switch=false`。服务随后以 Ctrl+C 完成有界停止。

- `per-stock-history-eligibility-profit-evidence-v1` 失败先行：旧实现对 2 只候选中 1 只具备历史、1 只仅
  19 日的批次返回 `not_ready`，并拒绝 50% 历史覆盖的 `DailyFeaturePack`；实现后仅合格股票评分并
  发布。负向回归证明全部候选历史不足仍为 `transient_invalid_empty`，全为 ST 的合法过滤空集仍是
  publishable `business_empty`。Tomorrow 模型回归固定 61 日要求，60 日股票只跳过自身；另一个失败
  先行回归证明模型字段缺失的高候选分股票不再先占用板内名额、把字段完整股票挤成 0 分。
- 定向数据平面、原生投影、生产 adapter、选择器、Web/API、诊断、Skill 事故规则及 JS 状态契约已通过；
  `make format-check`、`make lint`（严格重构债务为零）、`make type-check`（261 个源文件）、全量
  `make test` 和获准联网后的 `make package` 最终全部通过；固定融合 `83.40`、预算并发、冻结恢复、
  SSE 游标/慢客户端、架构与 `create_app()` 副作用契约均包含在全量回归中。
- 离线生产性能门禁通过：5500 行全市场、360 候选、三策略评分 P95 24.735ms，零外网调用、RSS 增长
  0%。最终 wheel 从仓库外目标安装并实际导入，`trader-cli --help` 及模板、CSS、JavaScript、SVG
  资源可读。Firefox SSE 诊断 8 个 DOM 样本 P95 1.051 秒、9 个 patch-to-paint 样本 P95 8ms；
  三档桌面报告在 1280x720、1440x900、1920x1080 均无白屏、横向溢出或浏览器错误，外网请求为 0。
- 真实新进程在 15:03 对 `web_recommendation_health_v3` 连续 4 轮采样全部通过。Tomorrow 明确公开
  `history_required_sessions=61`，在全局历史覆盖 342/360、活动 profile 合格 284/360 时仍完成 215 只
  评分；正式 0 只的 `empty_reason=score_below_observation_floor`，215 只均为 `below_score_threshold`，
  证明此时是策略/动作门槛空集而非无分析数据或全局历史覆盖阻塞。D25 同一进程也在历史 182/360 时
  保留 140 只完整评分。测试服务随后以一次 Ctrl+C 完成有界停止。

- `population-candidate-dual-watermark-funnel-v1` 失败先行：同一组 100 只候选在全市场批次完成后
  延迟 85 秒进入评分，修复前人口 100/100 因 `stale_quote` 被拒且完整评分为 0；修复后 Today、
  Tomorrow、D25 均得到 100 个本地分。负向回归继续断言候选本身过期时 100/100 保持
  `stale_quote`、完整评分为 0。Web 诊断字段失败先行回归也先以缺失人口原因计数失败，随后通过。
- 首次加载双水位修复的真实进程中，候选任务已完成 360 条，但 UTC 人口水位稳定触发三次
  `decision:value_error`；新增混合 UTC/上海时区失败先行后，三策略同一回归均恢复 100 个本地分，
  并断言原生人口时间在边界内统一为 `Asia/Shanghai`。
- 修复前真实 V1 服务状态：人口 5571 条，Today/D25 的 `population_rejected_count=5571` 且
  `stale_quote=5571`，候选仍有 342 个 `observe_only`；Tomorrow 在另一轮仅 34 条人口幸存并得到
  164 个本地分。该六阶段证据确认首个断点是人口水位，不是页面占位、DeepSeek、最终 TopK 或
  `OBSERVE_ONLY` 本身。定向 projection、选择器、input-quality、运行 adapter、诊断和架构回归共
  98 项通过。
- 最终 `make format-check`、`make lint`（零严格重构债）、`make type-check`（261 个源码文件）、
  `make test` 和 `make package` 通过；完整测试首轮仅有一个既有 scheduler 线程退出时序断言偶发失败，
  单项复跑及之后三轮完整测试均通过。五时段调度/冻结、83.40 融合、预算/流、冻结与架构专项 145 项
  通过。隔离构建首次因沙箱禁止下载 setuptools 失败，获准网络后最终构建通过。
- 仓库外安装最终 wheel 后，`trader-cli --help` 可执行，模板、CSS、JavaScript 和两项 SVG 资源均可读。
  隔离 Firefox 刷新门禁通过：DOM P95 1.049 秒、patch-to-paint P95 6ms、decision replacement 1 次；
  1280×720、1440×900、1920×1080 三档均无白屏、横向溢出、重叠或浏览器错误。
- 最终 V1 真实进程在午间恢复 Tomorrow `360 → 46 → 0`、D25 `360 → 48 → 0`；两者人口过滤原因不再
  含 `stale_quote`，recent errors 不含 `decision:value_error`。Today 在 11:20 后按冻结契约保持不补算；
  当轮候选历史仅 73/360，Tomorrow/D25 均明确保持 `history_coverage_incomplete`。

- `sse-replacement-coverage-regression-v1` 失败先行：实现前两项 Python 事件/API 契约均因实际
  `patch_schema_version=3` 失败，JS 端没有可原子替换 coverage 的 helper；实现后对应事件、API 和
  浏览器状态回归通过，并断言 GET/SSE coverage 完全一致、旧 `360/0/89` 被替换为 `360/229/89`。
- 修复前主机网络现场：统一 `runtime` 6 次样本均可达当前 V1 服务，Today/Tomorrow/D25 的
  `full_scored` 分别为 118/229/237，而页面仍报告 0；统一 `live` 五项探针全部通过，证券主数据
  5212/5212、历史供应商 6/6 可用、腾讯行情与 Tushare 正常。隔离历史持久化对比为批量 183 条/3 次
  事务/37.2ms，排除了供应商、持久化和评分本身作为页面旧 0 的根因。
- `make format-check`、`make lint`（含零严格重构债务）、`make type-check`（261 个源码文件）、
  `make test` 和 `make package` 全部通过；架构/无副作用、固定融合 83.40、SSE 游标与慢客户端、冻结及
  本批 GET/SSE coverage 专项共 98 项通过。仓库外 Python 3.14 环境实际从最终 wheel 路径导入包、
  执行 `trader-cli --help` 并读取模板、CSS、JavaScript 和两项 SVG 图标资源。
- 重启真实 V1 服务后，统一 runtime 契约可达且 release v9 资源全部返回 200；隔离 Firefox 直接应用
  decision replacement 1 次、零 resync，patch-to-paint P95 16ms。桌面 1280x720、1440x900、
  1920x1080 三档均无白屏、横向溢出、重叠或浏览器错误，场景漏斗由采集中正确更新为
  `360 → 56 → 0`。统一 full 的证券主数据、历史源、腾讯、Tushare 和浏览器 5/7 项通过；运行状态与
  离线性能两项未通过及其证据保留在 Residual Risks，未写成全量通过。

- 本批契约优先回归已覆盖默认 V1、显式 V2、配置不写回、有效策略身份变化、组合根单实例模型装配、
  三组阶段顺序、非零结果后继续、workers 定向转发、Shell 默认/显式档位、非法档位安装前拒绝及
  PowerShell 命令对称。`make format-check`、`make lint`（含零严格重构债）、`make type-check`（260 个
  源码文件）、`make test`（1,231 项）和 `make package` 全部通过；首次 lint 发现新增导入排序和 CLI
  复杂度/参数债，已通过导入归位、类型化选项对象及调度 helper 修复，复跑通过。最终重建包首次因受限
  环境不能访问本机依赖代理而失败，在宿主网络权限下用同一命令复跑成功。
- 默认 V1 与显式 V2 分别真实执行 `./run.sh check`，三段退出码均为 0、零网络性能门禁通过；有效策略
  SHA 分别为 `0136b6f7...91c6` 与 `9f6f2f75...e2fb`，`quote_to_draft` P95 分别为 1085.561ms 与
  1121.406ms。隔离临时运行目录真实执行 `research-screen`，四个空归档门禁均返回 1 但全部执行并形成
  完整汇总，未写当前研究工件；`research-history` 的 workers 转发、两段顺序和非零延续由直接契约覆盖，
  未为本次命令表面变更重复访问供应商或修改现有 4,904/5,006 H0 归档。
- 真实服务先后以默认 V1 和显式 V2 启动：状态分别返回固定 V1/V2 模型 ID 与不同有效策略 SHA，V2
  运行时磁盘配置仍为 V1，证明覆盖不写回；最终服务已恢复 V1。wheel 在仓库外临时目录强制安装后从
  隔离路径导入，聚合 CLI/服务 `--profile`、V2 `validate-config`、两份模型资源及模板/CSS/JavaScript/SVG
  均可读。无 Web schema、资源或布局变化，三档桌面截图门禁不适用；运行状态 API 已作专项实测。

- `tomorrow-v1-v2-profile-naming-and-evidence-roadmap-v1`：失败先行回归证明旧 `p1|p2` 仍被接受、状态仍
  返回旧 profile、旧 V1 工件身份及 v6/v7 发布握手；实现后配置、工件、评分、组合根、API/Web 和文档
  定向测试全部通过。高风险命令组 `make format-check`、`make lint`、`make type-check`（260 个源码文件）、
  `make test`（pytest 100%）和 `make package` 全部通过；全量测试首次发现一处旧文档断言仍要求 P2 活动
  名称，修复并定向复测后第二次全量通过。
- 构建 wheel 后在仓库外 `pip --no-deps --target` 安装，能分别加载 V1
  `4291ea51...f502b` 与 V2 `27034e52...887da5`，读取模板/静态资源、无副作用创建 App 并执行安装包
  `validate-config`。使用 H0 的 1,765,685 个训练行真实执行 `scripts/package_tomorrow_v1_model.py`，重建
  文件与包内资源逐字节相同。
- 默认 V2 与临时 V1 配置分别真实执行 `./run.sh performance-check`，均为零网络调用、零失败、零 RSS
  增长，`quote_to_draft` p95 分别为 1100.174ms / 1107.404ms。分别真实重启 `./run.sh serve` 后，状态
  返回 `profile_id=v2` / `profile_id=v1`、各自固定模型 ID/hash、正确历史终态及
  `v2_status_v7` / `release-contract-2026-08-30-v8`；最终恢复默认 V2，统一 Web 诊断连续 3 轮通过。
- 无头 Chrome 三档桌面发布验收 `passed=true`；1280x720、1440x900、1920x1080 均无浏览器错误、页面级
  横向溢出、Long 重叠或 release 资源错配。首次在受限沙箱内访问已授权本机服务得到
  `connection_failed`，按环境边界在宿主权限下复测通过，不作为产品失败。
- `tomorrow-p1-p2-configurable-profile-v1`：定向配置、P1/P2 包内推理、评分/决策身份、状态/API、Web 文案、
  流式训练和权威文档契约通过；高风险完整命令组 `make format-check`、`make lint`、`make type-check`
  （260 个源码文件）、`make test`（pytest 100%）和 `make package` 全部通过。构建 wheel 后在仓库外
  `pip --no-deps --target` 安装，已从安装目录加载 P1/P2 固定 hash、模板和静态资源，并执行
  `trader-cli validate-config` 成功。
- 使用 H0 归档的 1,765,685 个训练行再次执行 `scripts/package_tomorrow_p1_model.py`，得到与包内资源
  逐字节相同的 SHA-256 `a72713cb723d3dc1f7ba8f4bdc6d68f5cd7e67504227f84939523299b7945780`，
  内容身份仍为 `89f21552...14cd1b4`。默认 P2 与临时 P1 配置分别真实执行
  `./run.sh performance-check`，两者均零网络调用、零失败、零 RSS 增长；P2/P1 的
  `quote_to_draft` p95 分别为 1100.609ms / 1118.616ms，均在既有预算内。
- 停止旧进程后分别以 P2、P1 配置真实执行 `./run.sh serve` 并重启：P2 状态返回
  `profile_id=p2`、`daily_reconstructible_ensemble_v1` 和 hash `27034e52...887da5`；P1 状态返回
  `profile_id=p1`、`p1_manual_residual_momentum_v1` 和 hash `89f21552...14cd1b4`，策略内容 hash 也由
  `46166ba1...` 变为 `e7a44b59...`。P1 Web 健康诊断连续 3 轮通过；两者均为
  `v2_status_v6` / `release-contract-2026-08-30-v7`，证明配置只在重启后生效且未回退另一工件。
- 真实 P2 `full` 有界诊断中 Web、交易所证券主数据、qfq 历史、腾讯行情和生产性能 5 项通过；交易所
  资料为 5212/5212，Tushare 因未配置 token 受控降级。统一 `browser_refresh` 探针因本机缺少 Firefox/
  geckodriver 不能执行；仓库既有 Chrome 三档桌面发布门禁另行实跑 `passed=true`，1280x720、
  1440x900、1920x1080 均无浏览器错误、页面级横向溢出或 Long 区域重叠，静态资源全部使用 v7 revision。
  最终恢复默认 P2 并在 5000 端口保持服务运行；`live` 诊断 4 项通过、0 项失败，仅 Tushare token 缺失
  受控降级，Web 连续 3 轮、交易所 5212/5212、三只 qfq 历史和腾讯行情均通过。
- 失败先行回归先复现 GET/SSE 模型版本不一致和详情页不展示工件身份；修复后 Python API/SSE 12 项及
  JavaScript 页面状态契约通过，Tomorrow 详情使用
  `daily_reconstructible_ensemble_v1:<sha256>`，不再显示普通策略标签。
- 本批定向回归覆盖模型 hash/确定性推理、负预测诊断、硬过滤横截面、旧历史摘要重建、Decision 正式记录
  round-trip、组合根/状态/API/SSE/Web schema 与权威文档契约；相关 125 项组合测试全部通过。
- 真实执行 `./run.sh performance-check` 通过：5500 行全市场、360 候选的 P2 活动生产投影
  `board_ready_to_draft` p95 为 228.045ms，`quote_to_draft` p95 为 1089.000ms，零网络调用、零门禁失败，
  峰值 RSS 300556KiB，低于 384MiB 预算。
- 真实停止旧进程并用 `./run.sh serve` 重启后，`/api/v2/status` 返回新策略 hash
  `strategy_sha256_d0f61e6a981811f36abe`、准确模型 ID/hash、`manual_user_override`、
  `automatic_t1_outcome_settlement`、`automatic_model_update=false` 和 `not_modeled`。统一 live 诊断的
  Web、交易所证券主数据、三板 qfq 历史和腾讯行情 4 项通过；证券主数据 5212/5212 完整，三只代表股票
  历史均取得 61 根可用记录。
- 最终高风险命令组在当前完整 diff 上通过：`make format-check`（434 个文件）、`make lint`（严格重构债务
  为零）、`make type-check`（259 个源码文件）、`make test`（pytest 100%）和 `make package`。仓库外安装
  新 wheel 后从安装路径导入，hash 绑定 P2 工件、模板、CSS、JavaScript、SVG 及 CLI 配置校验均通过。
  无头 Chrome 最终报告 `passed=true`，1280x720、1440x900、1920x1080 均无浏览器错误、页面级横向
  溢出或 Long 区域重叠；临时 profile 清理重试没有再误报。
- 最终 `./run.sh serve` 已从最新工作树启动并保持运行；真实状态为 `v2_status_v5` /
  `v2_decision_view_v3` / `release-contract-2026-08-30-v6`，P2 ID/hash、人工授权、T+1 自动结算与禁止自动
  更新字段全部匹配。最终 live 诊断 4 项通过、0 失败：Web 连续 3 轮、交易所 5212/5212、三只 qfq 历史
  3/3 和腾讯行情通过；只有用户尚未配置 token 的 Tushare 受控降级。
- `Regression-Key: tencent-qfq-equivalent-day-history-v1`：先运行新增回归并观察合法 qfq 等价 `day`
  用例按预期失败，再实现严格归一化；定向 component/unit/contract 共 54 项通过，受影响 Python 的 Ruff、
  mypy 与格式检查通过。真实拆源修复前，688981 的腾讯历史为 0/2 可用、东方财富为 0/2 且两次请求错误、
  组合为 0/2；修复后腾讯与组合各 2/2 可用、0 空行、0 错误，P95 分别为 244.2ms 与 221.3ms。
- 对精确确认的旧服务 PID 执行一次正常 SIGTERM 后，真实运行 `./run.sh` 从当前工作树重新启动服务；
  修复后 `live` 统一诊断返回 Web 3/3 样本无错误或告警、沪深交易所基础资料 5212/5212 完整、三只代表股
  历史 3/3 可用、腾讯报价通过。总体 4 项通过、1 项因用户明确暂缓的 Tushare Token 缺失而受控降级，
  没有失败项。2026-08-30 为周日，实际 phase 为 `closed`，历史预热 planned/completed 均为 0，Today、
  Tomorrow、D25 均为 `not_ready`，因此本次证据没有把非交易时段合法空状态误写为完整评分/冻结通过。
  高风险完整门禁 `make format-check`、`make lint`、`make type-check`、`make test` 和 `make package` 全部通过；
  其中格式门禁覆盖 426 个文件、mypy 覆盖 254 个源码文件，全量 pytest 100% 通过并成功生成 sdist/wheel。
- `scoring-policy-integrity-review-v1` 失败先行测试先复现本地 veto 被清除、五类 V4 风险丢失、固定门禁
  可漂移、零权重行业维度误计和 epoch 缺少类型限制；修复后评分融合、DeepSeek、配置与 Tomorrow
  决策四组定向测试通过，并额外覆盖六类风险的全部 18 个严重度组合和未知模型风险失败关闭。首次全量
  测试暴露实时链契约仍要求已过期的“研究尚未实现”措辞；契约已改为验证当前真实边界——工程能力已实现，
  但尚未生成正式晋级档案且生产发布尚未获准。最终 `make format-check`、`make lint`（严格重构债务为零）、
  `make type-check`（254 个源码文件）、`make test` 和 `make package` 全部通过，`git diff --check` 通过，
  暂存范围仅包含本批 16 个配置、文档、评分实现和回归测试文件；`./run.sh validate-config` 实跑返回
  `status=ok`，有效策略身份为
  `strategy_sha256_2d20115d2f8aca72741f`。仓库外 wheel 安装和三档浏览器验收不适用：本批未改依赖、
  入口、包资源、API、SSE 或活动 Web 行为，构建门禁已覆盖安装包生成边界。
- `authoritative-doc-retirement-review-score-plan-v1` 先行失败契约已证明旧权威文档缺少证券主数据职责/P2
  终态措辞；归并后 `.venv/bin/ruff check tests/contract/test_v2_only_product_contract.py` 通过，文档单一
  真相源、评分计划、权威一致性和数据平面 4 组契约共 26 项通过。活动树引用扫描只保留本契约中的退役
  文件名，`git diff --check` 通过。
- `make format-check`、全仓 `make lint`/`make type-check`/`make test`/`make package`、仓库外 wheel 和三档
  浏览器验收不适用：本批只修改 Markdown 和一项只读文件存在性/文案契约，不改变 Python 运行实现、
  类型边界、依赖/入口、API/SSE、Web 资源或桌面行为；对应权威文档定向契约已覆盖实际影响边界。
- P2-1 定向契约通过：领域规范/报告、单次训练与正式验证隔离、合法空池、固定模型确定性、工件幂等/冲突/
  报告及模型篡改、CLI 与策略/架构文档契约共 49 项通过。真实命令
  `research-tomorrow-p2-screen` 首次按预注册门槛返回 `historical_rejected`（预期退出码 1），报告哈希
  `fc6d58e0...f2dec`；再次执行读取相同哈希和相同指标，证明未重训或改参。`research-status` 实测投影
  `v2_research_readiness_v4.tomorrow_p2.status=historical_rejected` 且前向资格为 false。
- 高风险完整门禁通过：`make format-check`、`make lint`（含零严格重构债务）、`make type-check`
  （254 个源码文件）、`make test` 和 `make package`。从仓库外 `/tmp/trader-wheel-p2.*` 安装构建 wheel 后，
  已验证 `trader` 从该安装目录导入、新 P2 应用/工件模块可导入、`trader-cli --help` 暴露新命令，并能
  读取模板、CSS、JavaScript 和 SVG 包资源。浏览器三档验收不适用：本批没有修改活动 HTTP/Web 行为，
  P2 也未获生产接线权限；供应商网络实测不适用：执行只读既有 H0 归档。
- `web-recommendation-state-explanation-v1` 失败先行回归先分别证明类型化漏斗缺少两档达线计数、公开状态
  仍自动投影和页面仍使用旧 release；实现后 bootstrap/input-runtime/dashboard/Web-health 定向 51 项通过，
  同时覆盖 Today 冻结错过、采集中、候选/基础资料/历史阻断、评分完成空池、最高分/门槛差、两档计数、
  前三原因及上市日期细节隐藏。
- 无头 Chrome 零外网夹具最终 `passed=true`：实际捕获“采集中｜候选行情 360 / 360”、
  “暂不可发布｜基础资料 120 / 360，要求 360 / 360”和最高 74.25/距正式线 3.75/观察线 2只/正式线
  0只/前三原因；1280x720、1440x900、1920x1080 均无白屏、重叠、页面级横向溢出或浏览器错误。
- 高风险完整门禁 `make format-check`（420 个文件）、`make lint`（严格债为零）、`make type-check`
  （251 个源码文件）、`make test`、`make package` 全部通过；`make performance-check` 在 5500 行全市场、
  360 候选、三策略和 100 tick 工作负载下通过，无外网调用、RSS 增长 0%。仓库外安装 wheel 后能从安装
  路径导入包、执行 `trader-cli --help`，并读取模板、CSS、JavaScript 与 SVG 资源。
- 当前工作树 `trader-server` 实际启动后，统一 `runtime` profile 连续 3 轮通过且无 finding；原始状态
  为 `v2_status_v4` / `v2_decision_view_v2` / `release-contract-2026-08-30-v5`，健康为 normal、phase 为
  非交易日 `closed`，官方证券主数据仍为 5212/5212。服务随后正常停止。
- 本批定向契约与回归：官方沪深快照原子校验、冲突/部分/未来上市日期拒绝、瞬时断连有限重试、
  来源健康投影、启动主动刷新、SQLite 批量持久化与恢复、官方身份优先级、上市交易日龄、报价 deadline
  隔离、失败保留旧资料、无参考资料“幽灵行情行”、统一诊断脱敏契约及架构分区等高风险定向
  contract/component/unit 测试通过。
- 真实来源与当前 release：统一 `security-master` profile 通过，官方快照 5212/5212（上交所 2315、
  深交所 2897，主板 3193、创业板 1403、科创板 616）；当前服务正常启动与再次重启后
  `/api/v2/status` 均为 `total_rows=listing_date_rows=listing_age_rows=complete_rows=5212`，交易所来源
  `planned=1/success=1/error=0`，持久化调度错误为 0。统一 runtime profile 连续 3 轮通过；当前日期为
  非交易日，phase 正确为 `closed`。
- 高风险全量门禁 `make format-check`、`make lint`（含重构债务零基线）、`make type-check`、
  `make test`、`make package` 全部通过；`make performance-check` 在 5500 行全市场、360 候选、三策略和
  100 tick RSS 工作负载下无失败、无网络调用、内存增长 0%。仓库外临时 venv 成功安装 wheel，能够导入
  新证券主数据客户端、执行 `trader-cli --help`，并读取模板、CSS、JavaScript 与 SVG 图标资源。
- 本批未修改模板、CSS、JavaScript 或布局，三档桌面浏览器视觉验收不适用；公开状态仅加法投影有界
  `exchange` 来源计数，并已由 Web contract 和完整测试覆盖。
- `score-tomorrow-historical-p2-contract-v1` 按基线 `9b8a067` 完整 Review。失败先行测试先证明 P2 模块、
  固定模型种子/线程、稳定组合排序和 50/100bp 压力字段尚不存在；实现后 P2 单元、权威文档契约、迟到
  身份持久化与历史诊断路径共 22 项定向测试通过，受影响 Ruff、format 与 mypy 检查通过。
- 评分契约按高风险执行 `make format-check`、`make lint`、`make type-check`、`make test`、`make package`；
  完整命令组最终全部通过，打包成功生成 sdist 与 wheel。首次全量测试暴露虚拟环境 pandas 2.0.3 与
  NumPy 2.4.6 ABI 不兼容，环境升级到 pandas 2.3.3 后恢复收集；随后暴露的硬编码路径与高负载测试同步
  缺口已按上一条修复并完成全量重跑。最终 `git diff --check` 通过。
- 本批没有改动 `bootstrap.py`、依赖/入口、运行服务或 Web 资源，也没有执行 P2 历史 I/O；仓库外 wheel
  安装、真实服务/供应商诊断与三档桌面浏览器验收不适用，未伪报为通过。固定融合 `83.40`、架构 AST、
  `create_app()` 无副作用、预算/冻结/API/SSE 等共享契约由最终全量测试覆盖。
- `score-p2-accelerated-promotion-plan-v1` 完整 diff 已按基线 `5d2eede` 审查；评分计划契约、权威文档
  一致性契约与 `git diff --check` 通过。该批只修改非权威 Markdown 计划和 Changelog，不改变机器契约、
  构建入口或运行行为，因此 Ruff、mypy、全量测试、打包、wheel 和浏览器/真实服务门禁不适用，未伪报
  为通过。
- `web-data-readiness-semantics-v1` 失败先行契约在旧模板/脚本上稳定失败 2 项；实现后 dashboard state、
  app factory 与 V2 Web 契约全部通过。回归固定行情 360、基础资料 120、历史 78、完整评分 56，断言
  首页显示“基础资料 120 / 360”“行情 360 / 360 · 历史有效 78”且不包含上市日期，同时覆盖首次评分
  “准备中”、已发布短线、历史空日期和 Long 当前名单行情路径。
- Firefox `browser` 诊断通过：10 个 DOM 更新样本最大 1.049 秒，11 个 patch-to-paint 样本最大 11ms，
  决策 patch 已应用且 35 秒保留窗口仍有 33.951 秒余量。`v2-desktop-browser-v1` 在 1280x720、
  1440x900、1920x1080 三档全部通过，无白屏、重叠、页面级横向溢出或浏览器错误；短线质量快照、
  采集中状态、空观察草稿与 Long 224 只完整名单路径均通过。
- 高风险完整门禁 `make format-check`（411 个文件）、`make lint`（严格债为零）、`make type-check`
  （248 个源码文件）、`make test` 和 `make package` 全部通过。format 首轮发现基线研究文件仅有一处
  Ruff 换行格式债，机械修正后全量重跑通过；package 首轮仅因沙箱禁止隔离构建下载失败，获准网络后
  相同命令成功生成 sdist/wheel。
- 真实 `trader-server` 以原配置正常 SIGTERM 后从当前工作树重启；`runtime` 三次采样通过且无 finding，
  对外 release 为 `v2_status_v3` / `v2_decision_view_v2` / `release-contract-2026-08-28-v4`。稳定后 Tomorrow
  直接阶段计数为行情 360、基础资料 120、历史 77、完整评分 56、正式 0，主要阻塞仍为
  `security_master_coverage_incomplete`，证明本批只改变展示而未绕过真实门禁。
- 最终 `full` 诊断 6 个子检查为 5 通过、1 受控降级、0 失败：真实 Web、Tencent、120 分 Tushare、
  Firefox 刷新和生产性能均通过；历史源 3 个有界样本中 2 个可用、1 个空响应，因此只报告
  `history_sources_degraded`。该外部来源抖动没有改变最近有效快照或本批 Web 验收结论。
- 批次 6 资格审计确认系统日期为 2026-08-28，P1 规范仍固定 2027-06-14 至 2027-08-06 的历史窗口与
  2027-08-09 至 2027-09-03 的前向窗口；仓库运行目录和 Git 跟踪文件扫描均未发现日历证明、P1 逐日
  证据或 historical/forward/combined 报告。预注册规范、collector/gate、工件防篡改和权威计划定向
  回归全部通过，证明缺证据时保持 `production_authority=false` 且不能越过历史资格写前向证据。
  本批只修正文档状态和 Changelog，不改变机器契约或运行行为；按低风险门禁运行相关文档契约和
  `git diff --check`，全量 make、package、wheel、供应商、服务重启和桌面验收不适用。
- Skill 手册失败先行契约在实现前因入口未路由且 reference 不存在稳定失败 2 项；实现后
  `tests/contract/test_reusable_diagnostics_contract.py` 全部 5 项通过。受影响契约测试 Ruff format/check、
  `skill-creator` 的 `quick_validate.py`、代理质量策略契约和 `git diff --check` 均通过。该批只修改
  Skill/诊断交付说明、契约测试和 Changelog，不改变产品运行、行情、调度、冻结、API、Web 或包资源，
  因此全量 `make test`/`make package`、真实供应商、服务重启、wheel 和三档桌面验收不适用。
- Web 漏斗定向回归 `.venv/bin/python -m pytest -q tests/unit/application/test_v2_input_runtime.py`
  通过 22 项；UTC 较晚完成时刻测试在修复前稳定复现上海时区值对象构造失败，修复后通过。调度、组合根、
  Web 状态/API 跨边界回归共 62 项通过，dashboard state 浏览器契约通过；受影响运行和研究模块 Ruff、
  mypy 定向检查通过。
- 高风险完整门禁 `make format-check`（411 个文件）、`make lint`（严格债为零）、`make type-check`
  （248 个源码文件）、`make test`、`make package` 全部通过。`make package` 首次仅因受限沙箱无法下载
  隔离构建依赖失败，获准网络重跑后成功生成 sdist/wheel；lint 首轮发现的批次 5 六参数债已在同一
  Review 循环修复并重新通过。
- 修复前真实服务连续出现 `refresh:value_error`，`close_quotes` 重试且三策略为
  `360 → 采集中 → 0`；修复后正常重启，刷新失败计数为 0，`close_quotes` 首次完成，Tomorrow 漏斗为
  `360/360/120/78/56/0`，D25 为 `360/360/120/79/58/0`。统一 `runtime` 采集 6/6 成功且无 finding；
  `full` 的 Web、腾讯、Tushare、浏览器刷新和生产性能 5 项通过，历史源 3 个样本中 2 个可用、1 个空响应，
  因而总状态为受控 `degraded`、零失败。
- 最终文档契约 20 项通过，覆盖权威文档一致性、实时流水线和 `fenshu.md` 整节计划；`git diff --check`
  通过，工作树只包含本批 6 个文件。
- Firefox 无头桌面门禁在精确 `1280x720`、`1440x900`、`1920x1080` 三档全部通过：无白屏、页面级横向
  溢出、关键重叠或浏览器错误，漏斗的 collecting/质量两种状态及详情交互均满足契约。仓库外 `/tmp`
  安装最终 wheel 后可从安装目标导入 `trader`、执行 `trader-cli --help`，并读取模板、CSS、JavaScript
  和两个 SVG 图标资源。
- 批次 5 定向回归：`.venv/bin/python -m pytest -q tests/unit/domain/research
  tests/unit/application/research tests/component/test_market_research.py tests/component/test_score_r5_forward_store.py
  tests/component/test_preregistered_shadow_store.py tests/component/test_v2_research_trace_store.py
  tests/contract/test_score_plan_contract.py tests/contract/test_score_research_detailed_strategy_contract.py
  tests/contract/test_v2_architecture.py` 通过，覆盖新预注册、日历失败关闭、精确证据 manifest、三档成本
  全区块统计、合法空组合、固定家族/门禁、前向资格、工件冲突/篡改及旧 R5 统计兼容。
- 批次 5 静态门禁：受影响文件 `ruff format --check`、`ruff check` 与 mypy 定向检查通过；最终
  `git diff --check` 通过。该批只影响离线 research 与文档，不接入生产运行、Web 或浏览器，统一运行诊断、
  供应商实测、完整仓库 pytest、wheel 安装和三档桌面验收不适用；定向测试已经覆盖全部直接依赖的
  research domain/application、研究工件、架构与权威策略契约。
- 新增领域、应用、工件和架构契约回归，覆盖个股成本改变排序、D25 双门槛、Tomorrow 无 incumbent、两模型/两窗口/两 horizon 完整映射、Top6/行业/板块约束、行业空白归一化、合法空池、生产隔离及工件幂等/篡改拒绝。本批按评分研究与持久化边界运行 `make format-check`、`make lint`、`make type-check`、`make test`、`make package`，全部通过；package 首次仅因沙箱禁止隔离构建访问 PyPI 失败，获准网络重跑后成功生成包含新增 research 模块的 sdist/wheel。
- 针对用户报告的 Web 推荐漏斗异常，复用 `scripts/diagnose_runtime.py --profile runtime` 对本机服务采样 3 次；status 与 Today/Tomorrow/D25 四个端点均为 `connection_failed`，没有取得可用于判断漏斗值的运行样本。`trader-delivery` skill 已在批次开始时加载并按影响矩阵执行。
- 从仓库外新建隔离虚拟环境完整安装构建出的 wheel，验证 `trader` 与 LightGBM 影子训练适配器可导入、模板/CSS/JavaScript/图标包资源可读取，并确认 `trader-cli --help` 可执行。
- 新增领域、应用、真实 LightGBM、工件和契约回归，覆盖 ridge/logistic、仿射/Platt 校准、Tomorrow/D25 embargo、expanding/rolling_252、同数据同标签同成本、完整预测、确定性模型哈希、工件幂等/篡改拒绝及生产隔离；本批涉及评分研究、依赖和工件边界，按高风险运行 `make format-check`、`make lint`、`make type-check`、`make test`、`make package`。
- 新增领域/应用/契约回归，覆盖五类固定特征、跨截面残差化、历史不足 missing、财务/公告未来披露拒绝、行业时点、R2 identity/context hash 绑定及生产隔离；本批按评分研究高风险运行 `make format-check`、`make lint`、`make type-check`、`make test`、`make package`。
- 定向回归覆盖完整人口值对象、未来证据拒绝、legacy v1、v2 SQLite 重启 round-trip 和 14:50 迟到排除；本批影响研究审计、持久化及运行观察边界，最终按高风险执行 `make format-check`、`make lint`、`make type-check`、`make test`、`make package`。
- `tests/unit/scripts/test_diagnose_runtime.py` 及既有研究/权威文档 contract 共 55 项通过，覆盖
  `research` 精确 profile、权威 CLI 路由、schema 失败关闭、不可恢复状态、活动窗口摘要和计划外日期不泄漏；受影响
  Python 文件 Ruff format/check 与 mypy 通过，统一 CLI `--help` 正确公开 `research`，Skill
  `quick_validate.py` 返回 `Skill is valid!`，`git diff --check` 通过。真实只读 profile 按预期以退出码 1
  报告 `score_p0_v2_historical_planned_dates_missed`、36/40 和 `recoverable=false`。本批不改生产评分、
  Web/API、供应商、数据库 schema、配置、DeepSeek、冻结或包资源，因此全量 test/package、wheel 和三档
  浏览器门禁不适用。
- 统一诊断 unit/contract 共 39 项通过，覆盖六档精确 profile、组合顺序、失败后继续、脱敏聚合、共享
  JSON 输出、共享 p50/p95、旧包装脚本不存在，以及五个内部模块均可通过 `python -m ... --help` 启动且
  不再公开 `--output`。受影响 Python Ruff 检查通过；`skill-creator` 的 `quick_validate.py` 返回
  `Skill is valid!`。
- 获准真实网络与 Firefox 后执行短时 `--profile full`，同一命令的 Web、历史、腾讯、Tushare、浏览器、
  离线生产性能 6/6 全部通过：历史 3/3 样本可用，Tushare 3/3 成功并识别 120 积分、50 次/分钟和
  8000 次/日，浏览器 patch-to-paint p95 为 9ms，生产性能 `failures=[]`。仓库外聚合报告扫描未发现三个
  样本代码、Token、stderr 或供应商原始载荷。
- 入口与门禁路由变更按高风险执行 `make format-check`（376 个文件）、`make lint`（严格重构债为零）、
  `make type-check`（229 个源码文件）、`make test` 和 `make package`，全部通过；打包前两次分别受沙箱
  禁网和 PyPI TLS EOF 影响，获准联网重试后成功生成 sdist 与 wheel。桌面页面布局、业务 Web 资源和
  产品运行依赖未变化，三档分辨率重复验收及仓库外 wheel 安装不适用；本批已用真实 Firefox profile
  验证被迁移的浏览器诊断入口。
- 原生因子诊断定向 unit/component/contract 测试覆盖 Pearson/ICIR、单调性、换手、集中度、完整/全缺失
  因子、三档成本、严重亏损、四类分层、剪枝前后召回、R2/R3/维度错配、幂等、冲突、防篡改和组合根隔离；
  受影响 Python 模块 Ruff 与 mypy 检查通过。评分/研究协议按高风险执行 `make format-check`、`make lint`、
  `make type-check`、`make test` 和 `make package`，全部通过；wheel 已包含新增领域、应用和基础设施模块。
  本批没有生产装配、HTTP、Web、供应商或 DeepSeek 改动，现场运行/浏览器/外部请求实测不适用。
- 统一诊断与 Skill 定向 unit/contract 共 8 项通过；受影响 Python 文件 Ruff format/check 通过，
  `quick_validate.py .agents/skills/trader-delivery` 返回 `Skill is valid!`，统一 CLI `--help` 和 Makefile
  发现性契约通过。获准网络后的最小 `live` 实测一次执行 4 项：Web、腾讯、Tushare 通过，历史源受控
  降级但后续检查未中断；报告脱敏扫描未发现样本代码、价格、Token、stderr 或供应商载荷。
- 短时 `full` 实测一次执行全部 6 项：Web、腾讯、Tushare、Firefox 通过，历史源 3 个样本中 2 个可用、
  1 个空响应，离线性能明确失败于 `market_merge:absolute_budget`；该失败证明统一入口按契约保留并定位
  所有子检查，不被首个降级掩盖。完整生产性能优化不属于本批诊断/Skill 改动，已记录为后续独立任务。
- 文档归并及权威一致性定向验证共 24 项通过，覆盖 `test_v2_only_product_contract.py`、
  `test_score_plan_contract.py`、`test_authoritative_document_consistency.py`、`test_recommendation_sections.py`
  和 `test_agent_quality_gate_policy.py`；Python 契约测试文件执行 Ruff 检查和格式检查；完整 diff
  执行 `git diff --check` 并核对三份退役文档不存在；除契约中的“不存在”断言外，文件名引用只保留在
  历史 Changelog/报告语境。
- `make test`、`make package`、仓库外 wheel 和三档浏览器验收不适用：本批只修改 Markdown 与文档契约
  断言，不改变运行代码、配置、依赖、入口、包资源或 Web 行为。
- 研究证据链定向领域、持久化、CLI 与文档契约共 53 项通过；真实 `./run.sh research-status` 只读核验
  返回 `v2_research_readiness_v3`、`historical_collection_failed`、缺失 2026-08-24 至 2026-08-26、
  最大 37/40 和下一计划日 2026-08-27。`make format-check`（365 个文件）、`make lint`（严格重构债为零）、
  `make type-check`（225 个源码文件）、`make test`、`make package` 与 `git diff --check` 全部通过；打包
  首次仅因沙箱禁止联网安装 setuptools 失败，获准网络后原命令通过。本批不改变活动 Web 和布局，
  三档浏览器验收不适用。
- 官方权限页核实 120 积分为 50 次/分钟、8000 次/日且仅可访问非复权日线，`adj_factor` 从 2000
  积分起；随后用 `scripts/sample_tushare_daily.py --codes 000001 600519 300750 --days 61` 在真实网络
  和现有受保护 Token 上实测 3 次请求全部成功，每只返回 83 行 raw 日线，总耗时 1159.5ms，未触发
  本地限流。Tushare adapter/reference/API/脚本/config 定向回归 105 项及新增错误码回归通过；
  `make format-check`、`make lint`（严格重构债务为零）、`make type-check`（225 个源码文件）、
  `make test`、`make package` 全部通过。打包首次因沙箱禁止联网安装 setuptools 失败，获准网络后同一
  命令通过；本批仅为状态 API 加法字段，未改变桌面布局，三档浏览器验收不适用。
- 现场先用 `scripts/check_web_recommendation_health.py` 复现 5 股批次在途超过 15 秒并在 20 秒后使失败
  计数增长；再用新增历史脚本确认真实供应商 5 股最大约 1.21 秒。修复后按生产 4.5 秒尝试上限运行
  两轮：10 个观测中 8 个取得 61 行，最大 1.173 秒，2 个空响应受控降级；488 条成功 K 线逐条
  488 事务耗时 1754.2ms，按股 8 个批量事务耗时 98.7ms，缩短 17.77 倍。定向 warmup、历史缓存、
  数据平面、Web API 和脚本契约全部通过；`make format-check`、`make lint`、`make type-check`
  （225 个源码文件）、`make test`、`make package` 与 `git diff --check` 全部通过。首次隔离打包因沙箱
  禁止下载 setuptools 失败，获准网络后原命令通过；本批不改页面布局，三档浏览器验收不适用。
- 本批失败先行回归先因缺少生产 warmup policy 无法收集；实现后 history warmup unit 与 component
  定向 18 项通过。`make format-check`、`make lint`（严格重构债务为零）、`make type-check`
  （225 个源码文件）、`make test` 与 `make package` 全部通过，`git diff --check` 无错误。
  本批不改变 Web、评分、冻结、依赖或包资源，仓库外 wheel 安装和三档浏览器验收不适用。
- 本批高风险完整门禁 `make format-check`、`make lint`、`make type-check`、`make test` 与
  `make package` 全部通过；覆盖架构 AST、`create_app()` 无副作用、固定融合 `83.40`、DeepSeek
  原子预算、latest-wins、冻结恢复、哈希一致性、SSE 游标/慢客户端和新增回归。最终 Review 清理一处
  冗余类型判断后，生命周期与调度集成定向 30 项、Ruff、format-check 和 mypy 再次通过；
  `git diff --check` 无错误。
- 最终离线生产性能门禁使用 5500 行、360 候选、三策略和 100 tick 通过且零网络、RSS 增长 0%；
  P95 为行情标准化 132.689ms、合并 435.927ms、quote-to-draft 1958.520ms、board-to-draft
  268.578ms、TopK overlay 提交 46.560ms、snapshot/status API 2.689/1.696ms、SSE 发布 0.016ms。
- 最终 Firefox SSE 验收直接应用 1 次推荐 patch 与 10 次 overlay patch，零 resync/浏览器错误，
  patch-to-paint P95 10ms、DOM 最大刷新 1.050s，35 秒保留窗余量 33.950s。三档实际视口
  1280x720、1440x900、1920x1080 均无白屏、横向溢出、关键重叠或外网请求，加载 v3 资源。
- 最终 wheel 在仓库外全新虚拟环境安装全部声明依赖，从独立 `site-packages` 导入 `trader`，
  `trader-cli --help`、`validate-config`、模板/4 CSS/2 JavaScript/2 SVG 共 9 项资源及 `pip check`
  全部通过。
- 六类整改的定向回归通过：架构/文档/发布状态契约、正式记录 codec 与研究审计、运行时问题恢复、
  类型化行情 health、拆分后的 156 个行情组件测试、SQLite 数据平面和 Node Web 状态契约全部通过；
  Review 额外发现并修复 hybrid 成功发布未恢复旧问题状态，以及列式批次身份仍用 `__dict__` 哈希，
  两者均补契约或行为回归。
- 高风险全量门禁 `make format-check`、`make lint`、`make type-check`（225 个源码文件）、`make test`、
  `make package` 全部通过。打包首次因沙箱禁止隔离环境联网获取 `setuptools` 失败，获准后成功生成
  0.2.0 sdist/wheel；这不改变本批仍归属 `Unreleased` 的发布状态。
- 仓库外 `/tmp` 虚拟环境以 `--no-deps` 强制安装最终 wheel，并从该 wheel 路径导入 `trader`，执行
  `trader-cli --help`、当前 V2 配置校验，读取模板、主 CSS、主 JS、新增 `dashboard_stream.js` 和 SVG，
  全部通过；临时安装和输出未进入仓库。
- 离线活动生产性能门禁通过且物理网络调用为 0、100 tick RSS 增长为 0：5500 行行情合并 P95
  571.881ms/600ms、quote-to-draft P95 2300.795ms/5000ms、三策略评分 P95 51.817ms/750ms、status API
  P95 0.978ms、SSE publish P95 0.007ms。
- Firefox headless 1280x720、1440x900、1920x1080 三档桌面验收通过：实际视口与请求一致，无白屏、
  页面级横向溢出、区域重叠或浏览器错误；新增 SSE 资源从打包快照加载，完整决策、观察池、Long、
  错误抽屉和断线相关契约保持有效，外部网络调用为 0。报告和截图只写入 `/tmp`，未提交。

- 本批定向验证覆盖正式记录 codec/查询/持久化/架构契约，DeepSeek V4 schema、模型、provider 与失败
  降级，配置 schema 13、市场状态/行情特征，以及三策略 scored 选择、融合、投影、冻结、调度和组合根，
  全部通过。高风险门禁 `make format-check`、`make lint`、`make type-check`（222 个源码文件）、
  `make test`、`make package` 最终均通过；全量测试只保留两条既有 Python 3.14 SQLite datetime adapter
  弃用警告。打包首次受沙箱构建隔离网络限制，授权后成功生成 sdist/wheel。
- 离线活动生产性能门禁通过且零网络调用、100 tick RSS 增长为 0：5500 行 `quote_to_draft` P95
  2039.877ms/5000ms、`board_ready_to_draft` P95 239.742ms/500ms、三策略活动评分 P95
  30.303ms/750ms、双源市场合并 P95 428.698ms/600ms。Review 同时删除了对同一
  `quote_to_draft` 的重复 Tomorrow 性能操作与重复预算，避免一项行为被双重计时并产生冲突结论。
- 仓库外虚拟环境强制重装最终 wheel 后，从 wheel 路径导入包和 `scored_v2_projection`，读取
  HTML/CSS/JavaScript/SVG 包资源，并执行 `trader-cli --help` 与 schema 13 配置校验，全部通过。
  本批未修改 Web 静态资源、HTTP schema 或布局，三档桌面浏览器验收不适用；相关只读投影行为由定向
  契约和全量测试覆盖。最终完整 diff、`git diff --check` 与仅暂存本批文件检查在提交前复核。

- 高风险全量门禁：`make format-check`、`make lint`、`make type-check`、`make test` 全部通过；
  `make package` 首次因沙箱禁止构建隔离环境联网失败，授权后成功生成 sdist/wheel。仓库外新虚拟环境
  安装 wheel 及全部运行依赖后，包导入、`trader-cli --help`、HTML/CSS/JavaScript/SVG 资源读取通过。
- 活动生产函数离线性能门禁通过：5500 行全市场、360 候选、三策略、100 tick 且零网络调用；
  `quote_to_draft` P95 1706.754ms、Tomorrow 原生投影 P95 1999.520ms、targeted overlay commit
  P95 43.269ms、status API P95 1.516ms、SSE publish P95 0.017ms，RSS 增长 0%。
- Firefox 实时专项通过：overlay SSE P95 1.037s、浏览器 DOM P95 1.045s、patch-to-paint P95 57ms，
  10 次 overlay patch、0 次 resync、0 浏览器错误；35 秒快照保留窗口仍有 33.955 秒实测余量。
  1280×720、1440×900、1920×1080 三档桌面验收均无白屏、重叠或页面级横向溢出。
- 定向回归覆盖 TopK 紧急 worker、候选/TopK 与全市场/tail 隔离、latest-pending 评分、慢 hybrid 不阻塞
  新 local、共享评分输入 batch、首次漏斗 pending 语义、SSE 重连参数和 overlay 禁止整表重绘；
  `git diff --check` 通过。

- 输入驱动评分、最终窗口、双策略检查点重试、SSE envelope/patch、DeepSeek 内存预算和 status 投影的
  定向 Python/Node 回归通过；固定融合向量 `83.40`、DeepSeek 并发原子预算、SSE 游标/慢客户端、检查点
  哈希往返与重启恢复专项通过。高风险门禁 `make format-check`、`make lint`（严格债务为 0）、
  `make type-check`（225 个源码文件）、`make test` 和 `make package` 通过；打包与浏览器首次仅因沙箱
  禁止联网/启动 Firefox 失败，获准后原命令通过。
- 禁止外网的 production performance gate 通过：5500 行两源合并 P95 373.586ms、Tomorrow 原生投影
  P95 1272.714ms、status P95 0.637ms、overlay CAS P95 27.341ms，100 tick RSS 约 220.1MiB，零网络调用。
  Firefox SSE overlay 最大间隔 1.051 秒、DOM 最大间隔 1.059 秒、patch-to-paint P95 7ms、零 resync/
  浏览器错误；1280x720、1440x900、1920x1080 三档均无白屏、重叠或页面级横向溢出。
- 仓库外 wheel 以 `--no-deps --target` 安装后从外部目录导入，`trader-cli --help` 及模板、CSS、
  JavaScript、SVG 包资源读取通过；`git diff --check` 和仅暂存本批文件检查在提交前复核。

- 荐股健康脚本的 14 项定向 unit/contract 回归通过，覆盖合法空集、持续上游零值、业务空评分、非零
  回退、input quality 消失/形状错误、冻结阶段、运行重启、status/current projection 失配、缺少合法空
  诊断、status/current 覆盖计数失配、报告脱敏和 CLI 参数；
  受影响文件 Ruff 与脚本直接 mypy 通过；以当前 HEAD 加仅暂存 diff 的隔离副本重跑结果一致。不可达
  回环端口实测生成脱敏失败 JSON 并返回 1。
- `make test`、`make package`、仓库外 wheel 和三档浏览器验收不适用：本批仅新增仓库辅助诊断脚本、
  契约测试和运维文档，不改 `src/trader`、依赖、打包资源、活动 Web/API 或策略行为。

- 本批定向回归已通过：cadence/input/runtime/decision identity/query/stream、Tomorrow 投影、DeepSeek
  预算、bootstrap/app factory/架构/诊断/入口契约共 171 项；受影响文件 Ruff 与 mypy 通过，Node D4
  overlay patch/无关事件过滤/status 对账测试通过。
- `performance-check` 的最终活动函数报告通过全部固定预算：5500 标的双源合并 P95 349.559ms（600ms
  上限）、status P95 1.077ms（100ms 上限）、TopK overlay CAS P95 28.974ms（100ms 上限），且
  360 候选、三策略评分、API/ETag/SSE、Tomorrow 原生投影和 100 tick RSS 均通过、网络调用为 0。
- Firefox SSE 专项门禁通过：overlay 发布最大间隔 1.043 秒、DOM 最大间隔 1.063 秒、
  patch-to-paint P95 6ms（100ms 上限），9 次更新全部走行级 patch，零 resync 和浏览器错误；三档桌面
  1280x720、1440x900、1920x1080 均无白屏、重叠或页面级横向溢出。
- 2026-08-25 早盘复用 `scripts/sample_tencent_quotes.py` 以 2 秒间隔采样 5 次；两只固定样本均出现
  4 个不同供应商版本，请求 P95/最大值 585.7ms，证明当前供应商响应为 2 秒 TopK 周期保留实际余量。
- 高风险完整门禁 `make format-check`、`make lint`（严格重构债为 0）、`make type-check`（225 个源码
  文件）、`make test` 和 `make package` 全部通过；最终 wheel 在仓库外临时目录以 `--no-deps` 安装，
  导入路径确认位于安装目录，`trader-cli --help` 及模板、CSS、JavaScript、SVG 资源读取通过。

- 对照 `docs/software-business-design.md` 的进程内类型状态与显式 JSON adapter 契约，以及
  `tests/contract/test_v2_architecture.py` 的现有 AST 门禁，逐项 Review 新增 `AGENTS.md` 六条规则；运行
  文档关键词契约检查、架构契约 10 项和 `git diff --check` 均通过。`make format-check`、`make lint`、
  `make type-check`、全量 `make test`、`make package`、wheel 和浏览器验收不适用：本批只修改 Markdown，
  不改变运行、构建、测试收集、API 或策略行为。

- 失败先行架构契约确认旧实现会因应用序列化方法和非类型化 `status()` 返回而失败；统一后，架构、
  worker、cache、latency、来源 lane/健康 JSON、DeepSeek 共享缓存和 V2 runtime 共 121 项定向回归通过，
  公开行情健康投影的既有 component 断言保持通过。以 `HEAD + 本批 diff` 的隔离副本执行高风险全量
  门禁：`make format-check`、`make lint`（严格重构债为 0）、`make type-check`（224 个源码文件）、
  `make test`（全仓 100%）和 `make package` 全部通过；打包首次仅因沙箱禁止隔离构建下载
  `setuptools>=68` 失败，获准联网后原命令成功生成 sdist 与 wheel。`git diff --check` 通过。
  当前工作树另有未纳入本批的 Realtime-R1 cadence/配置修改，其固定配置契约尚未闭合，不能把该
  外部失败或改动记入本批门禁与提交。

- 失败先行与定向回归通过：overlay 以网络完成时刻发布且拒绝无本机时间支撑的未来供应商时间、overlay
  故障后续成功恢复、必需 typed `input_quality`/publisher 架构契约、状态 JSON 投影、调度接口和 Web
  release 契约共 66 项通过；Node D4 状态契约通过，受影响源码与两个验收脚本直接 mypy 通过。
- 真实 Firefox 首轮严格验收发现观察草稿中未知来源会覆盖可靠聚合来源，修复并升级 Web revision 至
  v17 后重跑通过：卡片显示 `352 / 360`、行情缺失 8、身份缺失 286（上市日期 221、交易日龄 65、
  免费行情+交易日历补齐中）、`360 → 65 → 0`、过滤 216、观察草稿 2、最高 74.25 和腾讯行情；合法
  空草稿显示 0 条但保留真实漏斗。1280x720、1440x900、1920x1080 均无白屏、溢出、重叠或浏览器错误。
- 复用生产调度刷新诊断完成 65 秒 scheduler→overlay→SSE→Firefox DOM 实测：3 次刷新、3 次 SSE、
  3 次 DOM 变价；刷新最大 30.002 秒、SSE 最大 30.003 秒、DOM 最大 29.991 秒，35 秒快照保留余量
  5.009 秒，9 个 patch-to-paint 样本 P95/最大 41ms，无浏览器错误、重同步或丢样。
- 高风险全量门禁通过：`make format-check`、`make lint`（严格重构债为 0）、`make type-check`
  （224 个源码文件）、`make test`（全仓 100%）及 `make package`。打包首次仅因沙箱禁止隔离构建下载
  `setuptools>=68` 失败，获准联网后原命令成功生成 sdist 与 wheel。
- wheel 在仓库外 Python 3.14 环境安装全部声明依赖后，`pip check`、site-packages 来源导入、
  `create_app()`、`trader-cli --help`、`trader-server --help` 及模板、JavaScript、CSS、SVG 六类资源通过；
  加载的 Web release 为 v17。`git diff --check` 和暂存区范围在提交前最终复核。

- 本批失败先行及定向回归通过：`tests/unit/test_v2_settings.py`、
  `tests/contract/test_v2_e8_web_contract.py`、`tests/contract/test_v2_bootstrap.py`、
  `tests/contract/test_reusable_diagnostics_contract.py` 共 68 项通过；Node D4 Web 状态契约通过；受影响
  Python 文件 Ruff 通过。
- 复用 `scripts/measure_web_refresh_interval.py` 完成 65 秒真实 headless Firefox 验收：生产调度刷新
  P95/最大 30.001 秒，SSE overlay P95/最大 30.001 秒，DOM P95/最大 30.003 秒；页面实际读取
  35.000 秒保留窗口，剩余 4.997 秒，9 个 patch-to-paint 样本 P95/最大 31ms，浏览器错误、重同步和
  丢样均为 0。
- 高风险全量门禁通过：`make format-check`、`make lint`（严格重构债仍为 0）、`make type-check`
  （222 个源码文件）、`make test`（全仓 100%）及 `make package`；格式检查首次发现实测脚本需 Ruff
  格式化，修复后重跑通过；隔离打包首次仅因沙箱禁止下载 `setuptools>=68` 失败，获准联网后原命令
  成功构建 sdist 与 wheel。
- 最终 wheel 在仓库外全新 venv 安装全部依赖后，`pip check`、`trader` 的 site-packages 导入、
  `trader-cli --help`、`trader-server --help`、schema 10 `validate-config`、应用工厂及模板、CSS、
  JavaScript、SVG 共 8 类资源通过。Firefox 三档桌面门禁在 1280x720、1440x900、1920x1080 均无
  白屏、页面级横向溢出、Long 区域重叠或浏览器错误，加载的 Web revision 为 v16。

- 诊断脚本契约与静态质量通过：`make format-check`、`make lint`、两个新脚本的直接 mypy、
  `.venv/bin/python -m pytest -q tests/contract/test_reusable_diagnostics_contract.py` 和 `git diff --check`；
  完整 `make type-check`、`make test`、`make package` 不适用，因为本批未修改活动产品、依赖、构建入口、
  包资源或 Web 行为，脚本不进入 wheel，定向契约与真实执行直接覆盖新增行为。
- 固化后的 Firefox 端到端脚本默认 65 秒实跑通过：后端刷新间隔 30.000/30.002 秒，SSE overlay
  30.001/30.002 秒，DOM 价格变化 29.991/30.002 秒，浏览器 patch-to-paint 9 个样本 P95/max 30ms，
  无浏览器错误，退出后未残留 Firefox、geckodriver 或测试服务进程。
- 固化后的腾讯脚本以 2 个代码、2 轮、0.2 秒间隔真实只读采样通过：请求延迟 P50 211.0ms、
  P95/max 215.3ms；两轮来源版本不变并明确返回 `source_changed=false`，未把价格未变化误判为系统
  未执行刷新。报告均写入 `/tmp`，未进入仓库。

- 高风险全量门禁通过：`make format-check`、`make lint`（严格重构债仍为 0）、`make type-check`
  （222 个源码文件）、`make test`（全仓测试 100%）及 `make package`；首次隔离构建因沙箱禁止联网
  无法安装 `setuptools`，获准联网后同一命令成功，不把环境拒绝记录为代码通过。
- 定向回归通过：V2 架构/Today/Tomorrow/权威文档契约、生产输入适配器、统一调度器并发/冻结 overlay
  及 Tomorrow/D25 纯投影测试；`git diff --check` 无空白错误，活动 `V2SchedulerRuntime` 保持不超过
  800 行，C901/PLR0913 新增诊断为 0。
- 仓库外一次性 venv 从生成 wheel 安装成功；从 `site-packages` 导入 `trader`，执行 `trader-cli --help`，
  并读取模板、CSS、JavaScript 与 SVG 包资源，未从工作树回退导入。
- 隔离 `/tmp` 运行库、独立 `127.0.0.1:51237` 的真实 wheel 服务实测成功：`/api/v2/status`、根页面、
  四策略 current、Today history/dates 和静态资源正常返回，SSE 收到 `connected`；实际外部行情进入
  Web 状态的全市场行为 5,548 行、候选/Long 224 行，Long current 为 ready 且 224 项。
- Firefox 三档桌面报告 `v2-desktop-browser-v1` 通过：1280x720、1440x900、1920x1080 均无白屏、
  页面级横向溢出、重叠或浏览器错误；观察草稿、行情覆盖、漏斗、Long 固定名单和报价列正常渲染。

- 启动入口定向契约：
  `.venv/bin/pytest -q tests/contract/test_v2_e9_entry_contract.py` 通过；覆盖无参数仍启动看板、帮助在
  缺少虚拟环境时保持零安装副作用、日常/离线分组完整、未知命令在环境准备前快速失败、原研究命令
  与 R7 参数透传保留及 PowerShell 分类一致；`bash -n run.sh` 通过。
- 入口高风险全量门禁最终通过：`make format-check`、`make lint`（含零严格重构债务）、
  `make type-check`（228 个源文件）、`make test` 和 `make package`。首次格式检查发现新增测试需要 Ruff
  重排，格式化后从门禁起点重跑通过；首次隔离打包仅因沙箱禁止下载 `setuptools` 失败，获准联网后
  原命令成功构建 sdist 与 wheel。仓库外 wheel 和三档浏览器验收不适用：本批未修改打包内 Python、
  依赖、Web 行为或静态资源，启动脚本本身也不进入 wheel。

- 本批身份闭环定向验证：
  `.venv/bin/pytest -q tests/component/test_v2_market_data.py tests/contract/test_v2_e8_web_contract.py tests/contract/test_v2_app_factory.py`
  通过；覆盖稀疏刷新不删上市日期、交易日龄派生、晚到富身份无需下一评分周期即可经独立 lane
  落库、状态 API 脱敏投影和 Web release 契约。
- 浏览器摘要 JavaScript 契约：
  `node tests/js/test_dashboard_d4.js src/trader/web/static/dashboard.js` 通过，确认身份缺失构成与免费
  补齐来源同时显示。
- 高风险全量门禁最终通过：`make format-check`、`make lint`（含零严格重构债务）、
  `make type-check`（228 个源文件）、`make test` 和 `make package`。首轮 lint 发现身份合并函数新增
  一个复杂度诊断，拆分单股合并步骤后重跑归零；首次隔离打包仅因沙箱禁止下载 `setuptools` 失败，
  获准联网后原命令成功构建 sdist 与 wheel。
- 从仓库外 `/tmp` 前缀安装最终 wheel 后，确认 `trader` 从 `site-packages` 导入、`trader-cli --help`、
  `validate-config`，以及模板、4 份 CSS、3 份关键 JavaScript 和 2 个 SVG 资源均可读取。Firefox
  headless 三档桌面门禁在 1280x720、1440x900、1920x1080 精确视口全部通过：无白屏、页面级
  横向溢出、关键区重叠或浏览器错误，加载的 Web revision 为 v15。

- 本批失败先行回归在旧实现稳定得到空身份仓，前端也只能显示身份缺失总数；修复后市场数据、输入
  质量、Web 应用/HTTP 契约定向共 184 项通过，JavaScript D4 状态契约通过。`make format-check`
  （354 个文件）、`make lint`（含零严格重构债务）、228 个源文件 mypy、完整 `make test` 和
  `make package` 全部通过；打包首次仅因沙箱禁止隔离环境下载 `setuptools>=68` 失败，授权联网后
  原命令成功构建 sdist/wheel。
- 仓库外 `/tmp` 安装 wheel 后可导入包、执行 `trader-cli validate-config`，并读取 v14 模板、
  `status_view.js` 与 release contract。Firefox headless 加载的 12 个资源均携带 v14 revision，
  1280x720、1440x900、1920x1080 三档无白屏、页面级横向溢出、Long 重叠或浏览器错误，外部请求为 0。

- 失败先行证据：新增回归在旧实现下稳定得到 MIDDAY 对四策略提交数均为 0，JavaScript 将空
  `draft.items` 判为 `open` 并缺少空草稿文案。实现后 V2 scheduler/input adapter/Web contract
  定向测试 35 项、JavaScript D4 状态契约和受影响 Ruff 均通过；覆盖成功一次、空草稿一次、lane
  活动不重复、刷新失败重试、Long handoff 拒绝及 Web 四态。
- `make format-check`、`make lint`（含零严格重构债务）、228 个源码文件 mypy、完整 `make test` 和
  `make package` 均通过；package 首次仅因沙箱禁止隔离环境下载 `setuptools>=68` 失败，授权联网后
  原命令成功构建 sdist/wheel。仓库外 `/tmp` 安装 wheel 后可导入包、执行 `trader-cli
  validate-config`，并读取模板、三份受影响 JavaScript、release contract、三份 CSS 与 SVG 资源，
  安装包和服务端资源身份均为 v13。
- Firefox headless 真实加载 v13 后，先从候选采集迁移到 Tomorrow/D25 各两行观察草稿，再把 Tomorrow
  替换为空草稿；DOM 明确显示“本轮无股票达到观察条件”、0 行及“观察草稿 0”，无外部网络调用。
  1280x720、1440x900、1920x1080 三档均无白屏、页面级横向溢出、Long 重叠或浏览器错误。

- 本批失败先行证据：修复前 Web 契约测试稳定得到 `v2_status_v1`、首页仅 11 个 revision 资源且缺少
  `release_contract.js`；当前常驻服务三个 current 均返回 `v2_decision_view_v1`，同时三条短线 lane
  `completed_count=58`、`failed_count=0`、`running=false`、`pending=false`。修复后定向 HTTP/Web/Node
  契约测试通过，覆盖 status/release 双身份、真实旧 v1 形状拒绝、活动 lane 才能显示“正在生成”及
  空闲 lane 显示“未形成”。
- `make format-check`、`make lint`、228 个源码文件 mypy、单独完整 `make test` 与 `make package` 均通过；
  package 首次仅因沙箱禁止隔离环境下载 `setuptools>=68` 失败，获准联网后成功构建 sdist/wheel。
  wheel 在仓库外 `/tmp` 目标安装后从安装目录导入，`trader-cli validate-config`、首页、进程内静态
  响应及模板/JavaScript/CSS/SVG 资源通过，新增 `release_contract.js` 已包含在 wheel。
- 最终 Firefox headless 实际加载 `v2_status_v2` 与 revision v12，Tomorrow/D25 均从有运行 lane 的
  “正在生成”迁移为两行观察草稿，分数按 74.00、72.00 降序且行情列完整；1280x720、1440x900、
  1920x1080 三档均无浏览器错误、页面级横向溢出或 Long 区重叠，外部网络调用为 0。

- 失败先行回归在修复前以缺少 `trader.application.decision_drafts` 稳定失败；修复后的应用、评分适配、
  HTTP 契约定向集 26 项通过，JavaScript D4 状态契约与受影响 Ruff 检查通过。Firefox 真实浏览器
  1280x720、1440x900、1920x1080 已验证首屏 `360 / 360`、`身份缺失 待评分`、
  `360 → 采集中 → 0`、腾讯来源/HMS 年龄及“采集中”，并在下一次真实 15 秒状态轮询后自动显示
  Tomorrow 两行观察草稿；D25 草稿、Long 布局、错误抽屉和三档无横向溢出同时通过。
- 高风险门禁 `make format-check`（354 文件）、`make lint`（严格重构债务为零）、`make type-check`
  （228 个源文件）、完整 `make test`（100%）和 `make package` 全部通过；打包首次仅因沙箱禁止下载
  `setuptools>=68` 失败，授权联网后成功生成 sdist/wheel。仓库外 wheel 从临时目标导入，CLI help 与
  `validate-config`、10 项模板/CSS/JavaScript/SVG 资源和 `pip check` 均通过；仓库 `./run.sh
  validate-config` 同样返回 `status=ok`。

- 本批失败先行回归先稳定复现 `not_ready` 页面只显示 `0 → 0 → 0`、预算缺少 `limit`、状态缺少安全
  行情聚合，以及非有限指标可进入响应；修复后应用/组合根/HTTP 定向集 23 项、调度降级集成 1 项和
  `tests/js/test_dashboard_d4.js` 均通过。Review 新发现的 `NaN/Infinity` 边界已补回归并修复为丢弃。
- Firefox 真实浏览器三档 1280x720、1440x900、1920x1080 均通过：页面无横向溢出和脚本错误，空
  current 下实际显示 `352 / 360`、`行情缺失 8 · 身份缺失 286`、`360 → 65 → 0`、
  `观察草稿 2 · 最高 74.25`、腾讯行情 HMS 年龄、预算上限 168 和冻结未就绪。
- 高风险最终门禁 `make format-check`（353 文件）、`make lint`（strict refactor debt 为零）、
  `make type-check`（227 个源文件）、完整 `make test`（100%）和 `make package` 全部通过；隔离构建因
  沙箱网络限制需获准获取 `setuptools>=68`。`./run.sh validate-config` 返回 `status=ok`；最终 wheel
  在仓库外导入 `trader`、执行 CLI/配置校验、读取 9 项模板/CSS/JavaScript/SVG 资源并通过 `pip check`。

- 本批失败先行测试在旧实现分别以未知 `security_master_codes` 参数和缺失批量写端口稳定失败；修复后
  受影响应用、数据平面与市场数据定向集共 80 项通过，另复验历史慢尾保留成功股、参考刷新只走
  `HistoryWarmup`、公告 250 条三页补全/分页失败、DeepSeek 缺钥零物理调用和固定策略配置。
- Review 新发现的 DeepSeek HTTP 投影遗漏先以 `KeyError: deepseek` 复现，修复后完整 V2 Web 契约及
  DeepSeek 缺钥/预算不可用只读回归共 6 项通过，并验证测试注入的 `api_key`/外部载荷不进入响应。
- 高风险门禁 `make format-check`（353 文件）、`make lint`（strict refactor debt 为零）、
  `make type-check`（227 个源文件）、完整 `make test`（100%）和 `make package` 全部通过；打包首次仅因
  沙箱禁止下载 `setuptools>=68` 失败，授权联网后同一命令成功生成 sdist/wheel。`git diff --check`
  通过；本批未修改 Web 资源或页面行为，三档浏览器验收不适用。
- 真实只读检查确认 `./run.sh validate-config` 为 `status=ok`、本机环境无 `DEEPSEEK_API_KEY`，现有
  `/api/v2/status` 的 DeepSeek 物理预算使用量为 0。08:59 启动的既有服务仍是本批改动前进程，检查时
  市场阶段为 `closed`、调度提交为 0、活动数据平面证券主数据行为 0，因此未把该旧进程记为新实现的
  全市场覆盖通过证据。

- 启动故障定向验证通过：新增两项物理损坏回归先在旧实现稳定失败，修复后与完整数据平面单元、迁移和
  V2 bootstrap contract 共 17 项通过；受影响文件 Ruff、format check 和 mypy 通过。
- 真实 `./run.sh` 在原损坏库仍存在时已越过初始化并输出受控降级；隔离损坏库后再次启动，无降级告警，
  输出 `浏览器登录地址->http://127.0.0.1:5000`，`GET /api/v2/status` 返回 HTTP 200。新数据平面
  `PRAGMA quick_check=ok`、schema version 为 3；`./run.sh validate-config` 返回 `status=ok`，随后用
  一次 Ctrl+C 在共享关闭期限内正常停止。
- SQLite 恢复和进程启动高风险完整门禁通过：`make format-check`（353 文件）、`make lint`（严格
  refactor debt 为零）、`make type-check`（227 个源文件）、完整 `make test` 和 `make package`。
  打包首轮仅因沙箱禁止隔离环境下载 `setuptools>=68` 失败，获准联网后相同命令成功生成 sdist/wheel；
  HTML/CSS/JavaScript 未修改，三档浏览器视觉验收不适用。

- Score-P2 高风险门禁通过：`make format-check`、`make lint`（含零 strict refactor debt）、
  `make type-check`（227 个源文件）、`make test`（全量 100%）和 `make package`（sdist 与 wheel）。
  隔离构建首次受沙箱网络限制，授权联网获取构建依赖后成功完成。

- 定向回归覆盖：历史慢尾保留同批成功并只重试未覆盖代码、参考刷新仅走有界预热、公告 250 条三页
  补全/10 分钟完整基线首页增量/分页失败、候选特征与证券主数据及恰好 99% 历史覆盖漏斗、状态不含
  股票代码，以及 DeepSeek 缺少密钥时物理调用为 0 且原因固定为 `api_key_missing`。

- `git diff --check` 通过。桌面三档浏览器验收不适用：本批未修改模板、CSS、JavaScript、路由或页面
  展示行为，状态 API 仅对既有 scheduler 摘要增加脱敏字段。

- Score-P1 高风险门禁通过：`make format-check`、`make lint`、`make type-check`、`make test`、
  `make package`。隔离构建首次因沙箱禁止联网下载 `setuptools>=68` 失败，获准联网后同一命令成功
  生成 sdist 和 wheel；这不是代码失败。
- 定向回归覆盖 50% 历史拒绝、恰好 99% 历史通过、候选特征缺页、证券身份缺失、聚合状态投影、
  正常下行通过、趋势破位、保护输入缺失、DeepSeek 100 分仍降观察、动作原因持久化，以及 Today/
  Tomorrow/D25 投影、冷启动冻结、统一调度、Web 状态契约和固定融合向量 `83.40`。
- 完整 diff 已执行 `git diff --check`；Ruff、mypy、全仓 pytest 和构建均通过。未运行三档浏览器门禁：
  本批没有修改模板、CSS、JavaScript、决策列表 API 或布局，仅向状态 JSON 增加后台聚合字段。

- Score-R6S 定向领域、应用、制品、父身份、CLI、文档、架构与 `create_app()` 无副作用回归通过。
  真实 H0 覆盖 `4937/5000`、共 `3,103,160` 根日线；封存报告哈希
  `027f979ba5a7c85a6faff39c4bf9e8c062d57639e7ae95bda71fe1a774557bd1`，失败原因严格为
  `daily_stability_diagnostic_return_failed` 与 `daily_stability_diagnostic_recall_failed`。

- 高风险完整门禁 `make format-check`（353 文件）、`make lint`（严格复杂度债为零）、
  `make type-check`（227 个源码文件）、`make test` 和 `make package` 均通过；打包首次仅因沙箱禁止
  隔离环境下载 `setuptools>=68` 失败，授权联网后相同命令成功生成 sdist 与 wheel。仓库外安装 wheel
  后可导入 R6S、执行新 CLI 并读取 HTML、CSS、JavaScript 与 SVG 包资源。

- Firefox headless 三档桌面报告 `passed=true`：1280x720、1440x900、1920x1080 均无白屏、重叠、
  页面级横向溢出或浏览器错误，Tomorrow/D25 观察池继续按 `74.00 > 72.00` 排序，Long 报价字段完整，
  外部网络请求为 0；报告、截图和 wheel 安装目录仅位于 `/tmp`，未纳入提交。

- Score-R6D 定向契约、领域、应用、SQLite 点时特征、制品和 CLI 回归通过；真实 H0 归档覆盖
  `4937/5000`、共 `3,103,160` 根日线，封存报告哈希
  `aaa9a270aaecd0844c5786996a0318e6663812432a23f0c153fd33f256294ae2`，且只因
  `daily_trend_validation_turnover_failed` 拒绝。`make format-check`（346 文件）、`make lint`（含零
  refactor debt）、`make type-check`（223 个源码文件）、完整 `make test` 和 `make package` 均通过；
  打包首次仅因沙箱禁止隔离环境下载 `setuptools` 失败，授权联网后原命令成功生成 sdist 与 wheel。

- wheel 在仓库外临时目录安装后可从安装目录导入 `trader`、执行 `trader-cli --help` 并读取模板、CSS、
  JavaScript 和图标。Firefox headless 三档桌面报告 `passed=true`：1280x720、1440x900、1920x1080
  均无白屏、重叠、页面级横向溢出或浏览器错误，Tomorrow/D25 观察池仍为高分优先；报告、截图和
  wheel 安装目录只位于 `/tmp`，未纳入提交。

- 观察池高分优先批次通过 `make format-check`、`make lint`、`make type-check`、`make test` 和
  `make package`；隔离构建首次因沙箱禁止联网获取 `setuptools>=68` 失败，获准联网后同一命令成功
  生成 wheel 与 sdist。定向 `tests/unit/application/test_decision_queries.py`、V2 HTTP/Web contract、
  Dashboard JavaScript contract 均通过。Firefox headless 在 1280x720、1440x900、1920x1080 下验证
  Tomorrow/D25 两行观察池均为 `74.00 > 72.00`，且无白屏、重叠、页面横向溢出或浏览器错误；报告
  与截图仅写入 `/tmp`，未纳入仓库。

- 高风险完整门禁通过：`make format-check`（339 文件）、`make lint`（含零 refactor debt）、
  `make type-check`（219 个源码文件）、完整 `make test` 和 `make package` 均通过。打包首次仅因沙箱
  禁止隔离环境下载 `setuptools` 失败，获准网络后原命令成功生成 sdist 与 wheel；定向 36 项回归覆盖
  意图优先级、本地先发布、研究非阻塞、周期节奏、整批失败、多策略交错屏障、风险重评分和组合根状态。

- Firefox headless 三档桌面验收 `passed=true`：1280x720、1440x900、1920x1080 均无白屏、页面级
  横向溢出、Long 重叠或浏览器错误，Tomorrow/D25 观察行和行情字段完整，外部网络请求为 0。仓库外
  `/tmp` 安装 wheel 后可导入新增 `V2ResearchRuntime` 并读取 HTML、JavaScript、CSS 和 SVG 包资源；
  `git diff --check` 通过，运行中的旧服务根页面仍返回 200，且本批未停止或重启该进程。

- Score-R7 发布级门禁通过：`make format-check`、`make lint`（严格复杂度债为零）、
  `make type-check`、`make test`、`make package`。定向补充验证覆盖 R6 全制品加载、R7 复算、
  local-only/hybrid 分流、固定九组敏感性、逐门禁详情、幂等封存、篡改、路径 identity、CLI 缺证据
  拒绝及文档契约；Review 修复后再次运行完整门禁均通过。

- 最终 wheel 在仓库外 `/tmp` 目标以 `--no-deps` 安装，可从 `site-packages` 导入 `trader`，读取
  `score_r7_promotion_dossier_v1`，执行含 `research-r7-dossier` 的 CLI parser，并读取模板、CSS、
  JavaScript 和 SVG 资源。`make package` 首次仅因沙箱禁止隔离环境联网获取 setuptools 失败，获准
  联网重跑后成功生成 sdist 与 wheel。

- Firefox headless 三档桌面验收报告 `passed=true`：1280x720、1440x900、1920x1080 均精确命中，
  无白屏、页面横向溢出、Long 重叠或浏览器错误；Tomorrow/D25 观察行和 Long 报价字段完整，外部
  网络请求计数为 0。`git diff --check` 通过，活动配置、`bootstrap.py`、正式推荐领域和用户已有截图
  均未改动。

- Score-R6 定向回归共 35 项，覆盖规范哈希、固定候选、训练/验证隔离、全局/板块拟合与回退、生产
  权重映射、H0 覆盖门槛、20 日/100 对同股前向评价、local-only、hybrid bootstrap、不可变冲突、
  逐日 manifest、篡改拒绝、CLI 无写入失败路径和文档契约。严格 Ruff 复杂度检查保持零新增债务。

- 高风险完整门禁 `make format-check`、`make lint`、`make type-check`、`make test`、`make package` 均
  通过；完整 pytest 到达 100%，仅有既存 DeepSeek fixture 模型名及 Python SQLite adapter 弃用告警。
  `make package` 首次因沙箱阻止隔离构建环境下载 `setuptools` 失败，获准联网后原命令通过。仓库外
  安装 wheel 后成功导入 R6 模块、执行 `trader-cli --help` 并读取模板、CSS、JavaScript 和 SVG。
  本批未修改活动 Web 页面或布局，三档桌面浏览器视觉验收不适用。

- Score-H0 定向回归覆盖固定规范、66 根完成门槛、ST/停牌/三板股票池过滤、最大 5 worker、断点续跑、
  脱敏失败、未来/未复权拒绝、SQLite 幂等/冲突/篡改检测、61+5 SQL 窗口、训练/验证隔离、报告哈希、
  CLI/脚本入口和空归档只读性；腾讯组件回归确认缺少 `qfqday` 时不再接纳原始 `day`。

- 高风险完整门禁执行 `make format-check`、`make lint`、`make type-check`、`make test`、`make package`；
  首轮 Review 分别发现并修复格式/导入、mypy 返回类型、CLI 日期序列化、既有 R6 文档措辞兼容和新增
  C901 复杂度债务。`make package` 首次仅因沙箱阻止隔离环境下载 `setuptools` 失败，获准联网后同一
  命令成功生成 sdist/wheel；最终五项门禁全部通过，格式覆盖 324 个文件，mypy 覆盖 211 个源文件，
  全量 pytest 到达 100%，仅保留既有 DeepSeek fixture 模型名和 Python SQLite adapter 弃用告警。
  本批未修改 HTML/CSS/JavaScript 或桌面布局，三档浏览器验收不适用。

- 失败先行测试确认旧实现缺少 research spec 模块、活动身份和 readiness v2；实现后新旧 spec 日期/
  哈希/随机种子、R2 无替换窗口、R2→R5 身份贯通、v2 schema、前向目录隔离、CLI 只读状态及权威
  文档契约定向测试通过；最终 Review 发现的 v1 hash/解码兼容性由 48 项研究定向回归覆盖。
  `make format-check`、`make lint`（严格重构债务仍为零）、`make type-check`、
  完整 921 项 `make test` 和 `make package` 均通过；打包首次仅因沙箱禁止隔离环境下载 `setuptools`
  失败，获准联网后原命令成功生成 sdist/wheel。仓库外安装 wheel 后成功导入 `score_p0_v2` spec、
  校验其首日与哈希、执行 CLI parser 并读取 Web 模板；`git diff --check` 在最终 Review 再次执行。

- 运行库只读审计确认旧 committed observation 单库有 165 条、3 个交易日、载荷
  `67,106,829 / 67,108,864` 字节；仓库外安装最新 wheel 后执行 `trader-cli research-status`，正确
  返回 legacy 165 条、20GB 容量、3 个日期、outcome 尚未初始化及 R6 两项阻塞，命令未改写运行库。

- 新增/恢复的研究分区、observer、CLI、真实 outcome 仓储/结算、盘后冷启动、不可变冲突和 NaN
  质量回归全部通过。`make format-check`、`make lint`（含零 refactor debt）、`make type-check`、完整
  915 项 `make test` 和 `make package` 均通过；全量测试首轮发现 5 项文档固定语句回归，恢复兼容
  表述后全绿。打包首轮仅因沙箱禁止隔离环境下载 `setuptools` 失败，使用获准网络后成功生成 sdist
  与 wheel；仓库外 wheel 可导入真实结算适配器并读取模板、CSS、JavaScript 和 SVG 资源。
  `git diff --cached --check` 通过，暂存区仅包含本批 23 个文件，用户未跟踪截图明确排除。

- 架构、`create_app()` 无线程/网络/数据库/写文件副作用、固定融合 `83.40`、DeepSeek 预算并发、SSE
  游标/慢客户端、冻结恢复、哈希及入口契约均包含在完整测试中。未修改 HTML、CSS、JavaScript 或
  布局，三档桌面浏览器视觉验收不适用；状态 API 只新增 observer/settlement 字段并由契约测试覆盖。

- IPv4 与 IPv6 回归在实现前均按预期失败，分别证明缺少 `http://` 和 IPv6 方括号；实现后入口定向
  10 项通过。首次 `make format-check` 发现入口文件需格式化，执行 Ruff formatter 后重新 Review，
  `make format-check`、`make lint`、`make type-check`、完整 `make test` 和 `make package` 全部通过，
  sdist 与 wheel 均成功生成；`git diff --check` 通过。未修改页面资源或布局，三档桌面视觉验收不适用。

- 入口回归在旧实现上按预期失败，显示 Web 开始服务前标准输出为空；实现后新增回归与 V2 入口契约
  9 项通过，受影响文件 Ruff 与入口源码 mypy 通过。`make format-check`、`make lint`、
  `make type-check`、完整 `make test` 和 `make package` 均通过；打包首次仅因沙箱禁止隔离环境下载
  `setuptools` 失败，获准联网后原命令成功构建 sdist 与 wheel。`git diff --check` 通过。未修改
  HTML、CSS、JavaScript、API 或桌面布局，因此三档浏览器视觉验收不适用。

- 失败先行回归在旧实现上因缺少 `DecisionQuote` 按预期失败；实现后领域/应用/冻结/持久化/契约/调度
  扩展集与 68 项架构、`create_app()` 无副作用、固定融合 `83.40`、预算并发、SSE 游标/慢客户端、
  冻结恢复及哈希专项均通过。`make format-check`、`make lint`、`make type-check`、完整 894 项
  `make test` 和 `make package` 通过；打包首次仅因沙箱禁止隔离环境下载 `setuptools` 失败，获准联网
  后原命令成功。仓库外安装最终 wheel 后确认从安装目录导入 `DecisionQuote`/适配器、执行
  `trader-cli --help` 并读取 15 项模板/CSS/JavaScript/SVG 资源。真实 headless Firefox 在
  1280×720、1440×900、1920×1080 均无白屏、重叠、页面级横向溢出或浏览器错误，Tomorrow/D25
  观察池四项报价均非 `—`，三档截图已人工复核。

- 新增 JS 回归先在旧代码上因 `formatDurationHms` 缺失按预期失败，实装后通过；定向 Web 契约、
  JavaScript 语法和 runner 严格 `SyntaxWarning` 检查通过。因共享工作树同时存在用户未提交的报价
  锚点文档/测试，其中一个未格式化且一个有未使用导入，本批以已推送 `c77c0ed` 加仅 HMS diff 的
  `/tmp` 隔离副本运行 `make format-check`、`make lint`、`make type-check`、`make test` 和
  `make package`，全部通过；完整 pytest 到达 100%。最终 wheel 从仓库外 Python 3.14 环境导入，
  `trader-cli --help`、15 项模板/CSS/JavaScript/SVG 和 `pip check` 通过。Firefox headless 在
  1280×720、1440×900、1920×1080 三档均显示 `27h 2m 26s` 形态，无白屏、横向溢出、双栏错位、
  Long 重叠或浏览器错误，1440×900 截图已人工复核。

- 失败先行契约与实现后回归通过：`.venv/bin/pytest -q tests/contract/test_v2_app_factory.py`、
  `node tests/js/test_dashboard_d4.js src/trader/web/static/dashboard.js`。`make format-check`、
  `make lint`、`make type-check`、`make test` 和 `make package` 全部通过；完整 pytest 到达 100%，
  隔离打包首次仅因沙箱禁止下载 `setuptools` 失败，获准联网后成功生成 sdist/wheel。仓库外临时
  Python 3.14 环境安装最终 wheel 与全部声明依赖后，包来源指向安装目录，`trader-cli --help`、
  15 项模板/CSS/JavaScript/SVG 资源和 `pip check` 通过。Firefox headless 在 1280×720、
  1440×900、1920×1080 三档均无白屏、页面级横向溢出、双栏错位、Long 重叠或浏览器错误；三档
  均显示行情覆盖 `1 / 224`、`行情缺失 223 · 身份缺失 0`、标题日期 `2026-08-13`，正文只显示
  `12:30:00`，1440×900 截图已人工复核。

- 协作策略契约先在旧 `AGENTS.md` 上按预期失败，证明缺少架构最优修复原则；规则更新后定向契约
  2 项通过，受影响测试文件 Ruff 与格式检查通过，`git diff --check` 无诊断。仅协作 Markdown 与
  契约测试受影响，产品运行、构建、wheel 和桌面行为均未改变，故全量测试、打包和浏览器门禁不适用。

- 本批先以失败回归复现参考数据未调度、午后仍计算 Today、`freeze_sealed` 被登记为运行错误、免费
  证券主数据未持久化和生产交易日历未参与上市交易日数计算；修复后应用/调度/日程/行情组件定向
  矩阵全部通过。Firefox headless 在 1280×720、1440×900、1920×1080 三档均无白屏、横向溢出、
  双栏错位或 Long 侧栏重叠，Tomorrow/D25 各显示 1 条观察项；Long 样例“蓝特光学 688127”的
  名称、行业、价格、涨跌幅、成交额、换手率、市值、来源和时间均完整显示。`make format-check`、
  `make lint`、`make type-check`、`make test` 和 `make package` 最终全部通过。打包首次仅因沙箱禁止
  隔离环境联网下载 `setuptools` 失败，获准联网后原命令成功构建 sdist/wheel。仓库外安装 wheel 后
  可导入新组合根，模板、JavaScript、CSS 资源可读，`trader-cli validate-config` 返回 `status=ok`。

- 本批定向验证：`.venv/bin/python -m pytest -q tests/integration/test_v2_scheduler_runtime.py
  tests/contract/test_v2_e8_web_contract.py tests/contract/test_v2_app_factory.py`（15 passed）；
  `node tests/js/test_dashboard_d4.js src/trader/web/static/dashboard.js`；受影响 Python 文件 Ruff
  check/format-check；`git diff --check`。Firefox 桌面发布 runner 在 1280×720、1440×900、
  1920×1080 下通过，三档均无页面横向溢出、双栏等高、五卡单行、Long 重叠或浏览器错误；错误抽屉
  三条记录、主界面隐藏原因码和复制降级路径通过。完整门禁 `make format-check`、`make lint`、
  `make type-check`、`make test`、`make package` 全部通过；打包在受限网络内首次无法安装隔离构建依赖，
  获得联网权限后重跑成功生成 sdist 与 wheel，不属于产品代码失败。

- 本批定向回归 33 项通过，覆盖刷新完成时间晚于请求、三策略共享输入、未来供应商时间继续拒绝、
  名称/行业从投影到查询与 HTTP、正式记录新旧往返、状态版本和 scheduler 摘要。生产只读诊断确认
  三策略同时报告 `decision:valueerror`，隔离生产构建进一步复现原始原因
  `scored native input cannot contain future features`；另验证 5,542 条真实全市场名称/行业无非法字段，
  排除显示文本导致构建失败。

- 高风险完整门禁最终通过：`make format-check` 覆盖 307 个文件，`make lint` 保持零严格重构债务，
  `make type-check` 覆盖 202 个源文件，`make test` 共 881 项通过，仅保留既有 DeepSeek fixture
  RuntimeWarning 和 Python SQLite adapter 弃用告警。`make package` 首次仅因沙箱禁止隔离环境下载
  `setuptools` 失败，获准联网后同一命令成功生成 sdist 与 wheel；仓库外安装 wheel 后确认实际从安装
  目录导入、名称/行业身份有效、模板与 JavaScript 资源可读，且 `trader-cli validate-config` 成功。
  本批未修改 HTML、CSS、JavaScript 或桌面布局，三档浏览器视觉验收不适用。

- 本批定向回归覆盖 V2 输入 single-flight、三板各自限额、合法/临时空集、非预期 owner 释放、刷新失败
  保留与恢复清错、状态 API、去重覆盖、正式记录往返、Today/Tomorrow 投影、Web 契约及 research
  trace，共 44 项通过；后续新增 owner 释放回归也随全量测试通过。`make format-check` 覆盖 307 个文件，
  `make lint` 首轮发现 `ScoredDecision.__post_init__` 新增覆盖校验越过 C901 零债务门，抽取纯校验后
  Ruff 与严格重构债务检查通过；`make type-check` 覆盖 202 个源文件，`make test` 共 878 项通过，仅保留
  既有 DeepSeek fixture RuntimeWarning 和 Python SQLite adapter 弃用告警。

- `make package` 首次因沙箱禁止隔离构建下载 `setuptools` 失败，获准联网后成功生成 sdist 与 wheel；
  在仓库外临时目录安装 wheel 后成功导入 `V2MarketDataAdapter`、`ScoredDecision`，并读取模板和
  JavaScript 包资源。未修改 HTML/CSS/JavaScript 或桌面布局，三档浏览器视觉验收不适用；运行中的
  用户进程未被本批擅自停止或重启，因此部署后实盘观察列入剩余风险。

- Score-R5 定向领域、应用、组件与契约回归通过，覆盖 SHA-256 派生随机种子、10,000 次配对非循环
  bootstrap、短区块拒绝、固定五变体 Holm step-down、探索性历史终止、固定二十日前向日期、collector
  准入拒绝、`failed/no_decision` JSON 往返、幂等、内容冲突和篡改检测。首轮 Review 修复了前向报告
  缺失、完整绑定校验不足、正贡献先错误抵消负贡献及复杂度债务；全量测试首轮发现旧契约仍要求
  “尚未实现”标识，明确改为尚未实现 R6/R7 后重跑通过。

- `make format-check`（307 个文件）、`make lint`（零严格重构债务）、`make type-check`（202 个源文件）
  和 `make test` 最终全部通过；全量测试仅保留既有 DeepSeek fixture 模型 RuntimeWarning 与 Python
  SQLite adapter 弃用告警。`make package` 首次仅因沙箱阻止隔离构建下载 `setuptools` 失败，获准
  联网后原命令成功构建 sdist/wheel；仓库外临时安装 wheel 后成功导入统计器、collector、最终封存器、
  JSON 前向仓储，并核对主种子 `20260811` 与重复次数 `10000`。本批不修改活动 Web、静态资源、组合根
  或生产运行行为，三档浏览器验收及真实服务验收不适用。

- Score-R4 定向契约、领域、应用、R2/R3 组件回归通过，覆盖五版本/manifest 身份、11 条连续入场端点、
  三板热带与三项弱结构、关键缺失、硬热度拒绝、coverage shrink、Top120 外 loaded active-set、
  production/local/hybrid 同股集合、零/等权、稳定 Top6、集中度、facts/control-copy 和确定性哈希。
  `make format-check`（301 个文件）、`make lint`（零严格重构债务）、`make type-check`（198 个源文件）
  与 `make test` 全部通过；全量测试只保留既有 DeepSeek fixture RuntimeWarning 和 Python SQLite
  adapter 弃用告警。`make package` 首次仅因沙箱禁止隔离构建下载 `setuptools` 失败，获准联网后
  原命令成功构建 sdist/wheel；仓库外临时安装 wheel 后成功导入 `ScoreR4ChallengerReplayer`，并核对
  参数 manifest 为 5 个变体、11 条入场端点。本批不修改活动 Web、静态资源或桌面布局，三档浏览器
  验收不适用。

- Score-R3 定向单元、组件和契约测试通过，覆盖成本公式、平均秩 Spearman、五分组、40 日状态门、
  `no_decision` 零暴露、production/oracle 排名与集中度校验、微平均召回、指标汇总、确定性哈希、
  不可变写入、幂等、冲突及篡改拒绝。`make format-check`、`make lint`、`make type-check`、
  `make test` 最终全部通过；格式门禁覆盖 294 个文件，mypy 覆盖 195 个源文件，全量 pytest 通过。
  `make package` 首次仅因沙箱禁止隔离构建下载 `setuptools` 失败，获准联网后原命令成功构建 sdist
  与 wheel；仓库外临时目录安装 wheel 后可导入 `ScoreR3BaselineReplayer` 和
  `JsonBaselineReportStore`。本批不修改 Web、静态资源或桌面布局，三档浏览器验收不适用。

- Score-R2 定向契约、领域、应用、端口和组件回归通过，覆盖 40 个主窗口交易日、2026-06-19 休市、
  最近前序补足、`2026-05-18` 下界、真实失败身份、生产 Top120、50 分/30% 门、正式/观察池 Top6、
  板块 60%、行业最多 2、上界保护、共享输入去重、三板结算、稳定哈希、幂等/冲突及分区/manifest
  篡改检测。`make format-check`、`make lint`、`make type-check`、`make test` 全部通过；全量 pytest
  仅保留既有 DeepSeek fixture 模型 RuntimeWarning 与 Python SQLite adapter 弃用告警。`make package`
  首次因沙箱阻止隔离环境下载 `setuptools` 失败，获准联网后成功构建 sdist/wheel；仓库外安装
  wheel 后成功导入 `ScoreR2HistoricalExtractor`、`PolarsHistoricalPartitionStore` 和乐观上界函数。
  对活动研究库只读汇总确认仅有 2026-08-13 的 committed observation，预注册历史窗口交集为 0，
  支持本批 `exploratory` 剩余风险判断。桌面验收不适用，因为本批没有活动 Web、静态资源或组合根变化。

- 失败先行契约分别复现短线 `view=current` 返回 false、Today 在 `11:20:00.000001` 返回
  `missed_freeze`，修复后 Today/调度/统一 runtime/Web 定向 49 项及 JS 状态契约通过。实机只读证据
  确认 Tomorrow/D25 当日均为 `ready`、各 2 条 observe，Today 研究 trace 当日 9 次提交但正式记录
  缺失。Firefox 发布 runner 使用无外网 `midday + ready + observe` fixture 验证 Tomorrow/D25 观察池
  均可见、计数 1、股票行 1；1280x720、1440x900、1920x1080 均无白屏、重叠、页面级横向溢出或
  浏览器错误，并加载 `snapshot-identity-2026-08-13-v6`。

- `make format-check`、`make lint`、`make type-check`、`make test` 最终通过；全量 pytest 仅保留既有
  DeepSeek 测试模型名 RuntimeWarning 和 Python SQLite 默认时间适配器弃用告警。`make package`
  首次因沙箱阻止隔离环境下载 `setuptools` 失败，获准联网后成功构建 sdist/wheel；仓库外临时安装
  wheel 后通过包导入、`trader-cli validate-config`、10 项模板/JS/CSS/SVG 资源及 v6 身份脚本核验。

- 风险组件专项回归先在旧实现精确复现同时间第二次提交产生的八条失败告警（含用户报告的
  `penalty code=603083`），修复后验证八条首提交记录完整保留且不再误报；研究持久化相关 3 项组件
  测试通过。`make format-check`、`make lint`、`make type-check`、`make test` 最终通过；lint 首轮发现
  新增导入顺序问题，修正后重跑通过。全量测试仅保留既有 DeepSeek 测试模型提示与 Python SQLite
  默认时间适配器弃用告警。`make package` 首次因沙箱禁止隔离环境下载 `setuptools` 失败，获准联网
  后原命令成功构建 sdist 与 wheel。

- 历史持久化专项回归通过：新增用例先在旧实现稳定复现
  `ValueError: observed_at cannot be before source_time`，修复后与历史恢复/持久化相关的 4 项组件
  测试全部通过。`make format-check`、`make lint`、`make type-check`、`make test` 均通过；全量测试
  仅保留既有 DeepSeek 测试模型提示与 Python SQLite 默认时间适配器弃用告警。`make package` 首次
  因沙箱禁止隔离环境下载 `setuptools` 失败，获准联网后原命令成功构建 sdist 与 wheel。

- Score-R1-Migrate 定向矩阵通过：覆盖研究 SQLite 重启/幂等/冲突/双哈希/损坏隔离/条数与字节
  上限、observer 有界与消费者失败隔离、三策略投影及冻结重放、通用调度、文档与架构契约。
  `make format-check`、`make lint`、`make type-check` 通过，零严格重构债务；`make package` 首次因
  沙箱禁止隔离环境下载 `setuptools` 失败，获准联网后成功，仓库外 wheel 可导入新研究模块并读取
  Web 资源。本批未重复运行此前超过 17 分钟的全量 `make test`，以受影响包和共享边界定向矩阵替代。

- 本批 `make format-check`、`make lint`、`make type-check`、`make test` 最终通过；格式门禁覆盖
  279 个文件，全量 pytest 全部通过。`make package` 首次仅因沙箱禁止隔离环境下载 `setuptools` 失败，
  获准联网后原命令成功构建 sdist/wheel。JS 状态契约、Python 决策/调度/冻结/文档契约定向回归均
  通过。Firefox 三档 1280x720、1440x900、1920x1080 验收无白屏、横向溢出、Long 两栏重叠或
  浏览器错误。真实服务重启后，无头 Firefox 首屏为 Long、当前分组 5 行，首行显示蓝特光学 66.53、
  +2.83% 及腾讯行情时间，资源为 v5；手动切换 Tomorrow 并跨过一次 15 秒状态刷新后仍保持选择。

- 本批 `make format-check`、`make lint`、`make type-check`、`make test` 最终通过；全量测试首次暴露
  两个 history lane 测试替身缺少 `status()`，补齐真实端口边界后定向回归及全量测试均通过。
  `make package` 首次仅因沙箱禁止隔离环境下载 `setuptools` 失败，获准联网后原命令重跑并成功构建
  sdist 与 wheel。实机 V2 status/current 验证 Long 为 `ready` 且包含 224 条记录，Tomorrow 为
  `ready`、`frozen=true`、`freeze_kind=close_fallback`；本批未修改 Web 静态资源或布局，因此三档
  浏览器发布验收不适用。

- `make format-check`、`make lint`（含 Long 资源一致性与零严格重构债务）、`make type-check`
  （187 个源文件）、`make test` 和 `make package`：最终均通过。全量测试首次只发现一条过期计划
  状态断言，修正为 E0-E11 完成后重跑通过；打包首次因沙箱阻止下载隔离构建依赖失败，获准网络后
  原命令重跑通过。
- 专项发布测试覆盖固定融合向量 `83.40`、DeepSeek 并发原子预算、SSE 过期/超前游标重同步与慢
  客户端隔离、Today 边界后禁止迟到冻结、Tomorrow 检查点同身份恢复、半提交 SHA-256 恢复、
  V2 AST 依赖和 `create_app()` 无线程/网络/数据库/写文件副作用，共 16 项通过；`pip check` 通过。
- Firefox headless 三档桌面验收通过：三档 requested/actual 视口精确一致，均显示 3 个 Long 分类和
  当前分组 5 行固定股票，无白屏、横向溢出、侧栏重叠或浏览器错误；API 503 场景仍显示固定名单，
  实时字段不伪造。
- 仓库外临时前缀安装 `trader_research_dashboard-0.2.0-py3-none-any.whl` 后，包导入、
  `trader-cli validate-config`、根页面，以及模板、三份 CSS、Long/主 JS、名单资源和两个 SVG 均通过；
  临时安装未进入仓库。
- 真实 `trader-server` 验收通过：旧进程收到一次 `SIGTERM` 后在共享 30 秒期限内退出；当前代码以
  schema 9 重启，根页面、status 和四个 current 均返回 200，Long 返回 224 项且
  `score_status=not_applicable`。新进程创建 `deepseek-budget.sqlite3`，旧名文件时间未变化。

- `PYTHONPATH=.:src .venv/bin/pytest -q`：通过。
- `make format-check`、`make lint`、`make type-check`、`make test`、`make package`：均通过；其中
  `make package` 首次在隔离环境因缺少构建依赖下载失败，获准网络权限后重新执行通过。
- `.venv/bin/ruff check src/trader`、`node --check src/trader/web/static/dashboard.js`、`git diff --check`：通过。
- 仓库外 wheel `pip --no-deps --target` 安装后，V2 Web 模板/静态资源导入和 `trader-cli validate-config`
  均通过；安装目录为临时目录，未进入仓库。

### Residual Risks

- BaoStock 真实登录、可用磁盘和 2000 日全量覆盖仍是外部前置条件；当前没有合格 manifest，`train-tomorrow` 只能按既有父工件状态有界失败关闭，不代表 V3 模型或生产授权已完成。

- `codex-b-baostock-blocker-evidence-v1`：本批复核未解除 A 的外部前置条件；真实登录探针仍失败，2000 日命令
  因本机可用空间约 8.85GiB 低于 30GB 在外部 I/O 前阻塞。尚无 2000 日全量 SQLite/合格覆盖 manifest，也未
  实测峰值 RSS、全量耗时或 Python 3.10–3.14 的真实 SDK 行为。BaoStock 单独不能证明历史行业、资格、硬过滤和风险事实
  `effective_at`，能力工件因此保持 `historical_data_insufficient`，15.1.35 仍阻塞；未训练模型、未打开
  14:50 留出，`point_in_time_parity=false`、`production_authority=false`。

- `tomorrow-v3-input-compatibility-v1`：当前只证明 B 的 fixture 消费边界，A 尚未提供真实 BaoStock v2
  manifest/port，D 尚未集成 SDK、入口和进程控制，也未执行 2000 日全量下载；历史行业、资格和风险事实
  `effective_at` 同样未证明。因此 15.1.38 整节仍为 `pending`，15.1.35 训练仍被阻塞，本批不构成覆盖、
  收益、点时一致或生产授权证据。

- `codex-c-baostock-holdout-isolation-v1`：Codex A 的冻结 manifest/schema 与真实 2000 日数据尚未形成，本批
  仅使用 1250 日确定性 fixture 证明隔离契约；未读取行情、收益或模型，也未执行确认、日线代理、影子比较
  或新的 14:50 点时留出。上述真实研究继续受 A 数据/历史事实和 B 唯一 bundle 父 hash 阻塞；本批不构成
  点时一致、收益改善或生产授权证据。

- `baostock-2000-v3-roadmap-consistency-v1`：本批只修订权威计划，尚未把 BaoStock 加入 `pyproject.toml`，
  也未实现/执行 2000 日下载。SDK 逐行接口的 Python 3.10–3.14 兼容性、停牌行语义、退市证券覆盖、许可
  边界、磁盘/RSS 和真实全量覆盖仍待第 15.1.38 节验证；历史申万行业、资格和风险事实 `effective_at` 仍无
  已证明来源，因此即使日线下载合格，V3 也可能继续阻塞。本批不构成收益、点时一致或生产授权证据。

- `baostock-1500-daily-roadmap-v1`：BaoStock SDK 使用全局 socket，且 0.9.3 的 `get_data()` 依赖 pandas
  已删除的 `DataFrame.append()`；后续实现必须使用逐行接口、隔离进程分片并验证 Python 3.10-3.14。
  BaoStock 服务可用性、许可/个人研究使用边界、全市场 95% 应有单元覆盖、退市证券完整性和确定性复权仍
  待真实全量审计。即使日线覆盖通过，历史 11:20/14:50、行业/风险/证券状态 `effective_at` 缺口仍会使
  Today/Tomorrow/D25 保持 `historical_data_insufficient`；本计划不构成收益或生产可用性证明。

- `codex-b-tomorrow-joint-insufficient-v1`：联合器现在可以如实封存父工件不足，但尚无 V1/V2/C3 同日原始
  预测、OOF、联合收益或 Holm 证据。该终态不授予 V3 或生产权限，未来必须使用新的完整 C3 父工件和预注册
  日期窗口另立批次，不能覆盖本次 hash。

- `codex-b-historical-data-insufficient-closure-v1`：B 现在具备可重复、不可变的父工件不足收口，但没有历史
  H1 点时数据，因此仍无真实过滤召回、候选收益、Holm 显著性或 V3 模型结论。未来只有新的 Codex A
  completion 以完整切分和成熟残差账本封存后，才能另立批次执行 15.1.28–15.1.30；本批终态不可覆盖或改写。

- `codex-c-terminal-failure-closure-v1`：C 报告已如实封存 A 的数据不足，而不是收益否定或验证结论；当前仍无
  可执行的 200 日终端样本、候选收益或 DeepSeek 历史证据。只有新的 H1 身份同时满足点时字段、95% 股票覆盖、
  至少 1000 个共同交易日和 200 日保留段后，才能另立批次执行真实留出；不得覆盖本批报告、重开同一留出或
  把 `historical_data_insufficient` 改写为 `historical_rejected`/`historical_validated`。

- `codex-a-h1-capability-audit-v2`：东方财富历史分钟端点本次只能证明探测失败，不能断言供应商永久不支持；
  但腾讯已返回的日线深度、两类历史锚点和有效证券状态证据仍不足，当前 H1/C3 路线必须保持数据不足。
  只有未来出现能同时满足点时字段、95% 股票覆盖、至少 1000 个共同交易日和 200 日保留段的新来源或新
  研究身份时，才能另立批次重新审计；不得覆盖本次 hash、用当前事实回填历史或绕过失败关闭终态。

- `codex-a-h1-residual-c3-v1`：本批完成 CodexA 工程能力和失败关闭边界，但没有可核对的真实 H1 父工件，
  因而未执行真实来源下载、全市场特征构建、C3 训练/确认或收益评价，也未生成 D 编排可消费的真实 Parquet
  分区、handoff、`report.json` 或 `model.json`。空归档证据不能证明收益改善；真实来源达到 95% 股票覆盖、
  至少 1000 个共同交易日和最新 200 日点时保留前，后续必须保持 `historical_data_insufficient`，不得打开
  留出或申请生产权限。

- `tomorrow-v3-single-command-two-artifact-contract-v1`：`./run.sh train-tomorrow` 的 Codex D 编排、工件、
  状态和资源门禁已实现，但真实 H1/C3/联合/留出 handoff 尚未生成，因而本批没有产生 `model.json`、终态
  `report.json`、真实 Parquet 证据或收益结论。两级留出和后续人工授权的 wheel 资源发布仍须按第 15.1.37
  节后续波次完成。

- `four-lane-tomorrow-research-roadmap-v1`：Codex D 的 `./run.sh train-tomorrow` 已可执行并在缺少父工件时
  受控阻塞，但第 15.1.25–15.1.36 节仍未形成真实数据完整链，也没有任何收益提高证据。H1 能否取得 95% 股票覆盖、至少
  1000 个共同有效交易日及独立 200 日 14:50 点时保留段仍待能力探针和下载审计；若免费来源不能证明
  11:20/14:50 点时字段，对应策略必须以 `historical_data_insufficient` 收口。C3/V3 即使日线代理通过，
  仍须独立 Tomorrow 点时留出和用户再次授权；V3 当前不得写配置、装入 wheel 或进入生产。

- 当前 CLI 不持有活动进程句柄，因此只能确认静态工件并报告
  \`live_identity_unverified\`；在任何后续人工生产变更前仍需由真实运行身份探针补齐该项。审计本身不
  产生收益改善或模型晋级证据，15.1.25 及后续历史章节仍保持待执行。

- `tomorrow-daily-close-training-proposal-v1`：本批只归纳方案，没有实现训练命令、模型工件、收益验证或
  生产接入，也不证明荐股盈利已经提高。日线 `D` 收盘与生产 14:50 输入存在约 10 分钟分布差异，当前
  权威策略仍禁止用收盘代理宣称点时一致；只有用户另立高风险批次并先更新权威契约，才可评估是否接入。
  用户未跟踪的 `docs/question.md` 保持原样且不纳入本批。

- `historical-score-roadmap-priority-pruning-v1`：本批只收敛权威计划和机器文档契约，没有执行
  15.1.23–15.1.34 的任何未完成能力，也没有证明实时性、资源成本或荐股收益已经提高。H1 的 95% 股票
  覆盖、各策略 1000 个有效交易日和 200 日终端留出能否取得仍待实际下载审计；透明有限候选可能全部
  被拒绝，这是真实可接受终态。历史 DeepSeek 盈利增量不再尝试验证，生产 DeepSeek 和固定融合继续按
  现行契约运行；任何未来生产策略变化仍需用户明确授权和独立完整高风险门禁。

- 当前 2026-09-01 正式记录已在 14:50 冻结，业务契约禁止用迟到数据或新代码覆盖；本批只修正读取说明，
  下一交易日的新评分才会由现行分类逻辑生成新记录。实时拆源显示代表性科创板代码的腾讯 qfq 历史稳定
  为空、东方财富 3 只 × 3 轮均失败，运行预热仍有 14 只唯一失败；公司风险历史也尚无足以宣称完整的
  现场覆盖证据。这些真实数据缺口继续显式降级，不通过隐藏提示、把 raw 日线冒充 qfq、降低可靠度或
  风险门槛解决；DeepSeek 未用于本次诊断，也不保证必然产生推荐或提高收益。

- 一级名单能力已接入生产，但现有外部来源不能证明一次运行已经穷尽全市场全部历史 ST 和全部年度财报；
  名单会由已验证的当前/历史行情、正式财报、权威公司风险和人工配置持续追加，财报源以
  `financial_history_complete` 显式标记覆盖，未知不能宣称已清查。供应商全市场接口若不支持代码裁剪，
  仍可能物理返回已排除行，但这些行不会发布或触发后续逐股请求。既有历史缓存不做破坏性删除；冻结
  TopK 报价 overlay 与原正式 outcome 仍按冻结身份更新/结算，这是不可覆盖冻结契约的有意例外。

- 本批只修复 Tomorrow 合法 0 分空仓的解释和诊断归因，不把单日 0 分包装成收益改进。最终现场候选历史
  覆盖仅 72/297（约四分之一）且午间历史预热存在大量失败/重试；代表性 3 只股票的历史源诊断 3/3 成功，说明
  不是供应商整体中断，但覆盖与预热效率仍是独立运行风险。本批不据单日现象改模型、固定成本或门槛，
  评分优化继续按历史 H1 路线和最终留出门禁执行。一次带大量在途历史任务的正常 SIGTERM 达到 30 秒
  关闭上限并返回 2，但进程和文件锁均已释放；该历史预热资源问题未由本批解释修复改变。

- 活动实例提示不会自动终止或替换旧进程；这是单实例与冻结安全边界，部署新代码仍需操作者在原终端
  正常停止后重启。现场服务已由修复后的 `run.sh` 重新启动并保持运行；证券主表刷新与历史预热仍存在
  供应商降级，其中历史覆盖仅 116/360、采样期新增 1 次失败，这些不是入口修复造成，也未通过删除锁、
  放宽评分或伪造数据掩盖。三档浏览器布局门禁不适用：本批未改模板、CSS、JavaScript 或页面布局。

- 本批只封存历史评分优化的执行顺序、样本门槛和失败关闭条件，没有下载 H1、生成候选或开启最终留出，
  因而不宣称 Today、Tomorrow 或 D25 收益已经改善。11:20/14:50 历史锚点、证券/行业/风险生效时间若
  无法达到 95% 股票覆盖、1000 个共同有效日和至少 200 日最终留出，后续路线必须停在
  `historical_data_insufficient`；不得用当前字段、收盘代理或未来采集解除。当前生产权重、模型、阈值、
  DeepSeek、冻结、API 和 Web 均未改变。

- 本批没有用仓库运行数据执行 V2 历史风险终态，因此不宣称 Brier/ECE 已通过；H0 验证段不足 122 个合格
  日期或存在字段缺失时会保持 `historical_data_insufficient`，Web 继续显示
  `loss_probability_status=not_modeled`。旧未来研究和运行比较文件可能仍存在于用户运行目录，但当前代码
  不读取、不迁移也不删除它们；旧 R6 v1、P0/P2 结论保持不可变审计。`v2_status_v11` 与
  `v2_research_readiness_v7` 是显式 schema 更新，外部本地脚本须同步。正式 `0.2.0` 发布仍需用户另立发布批次。

- Firefox 专项的 `browser_refresh` 因当前浏览器/驱动与运行服务连接条件失败；统一 full 还记录 Web endpoint
  `connection_failed`、交易所 SSE 失败、腾讯行情失败、历史 3 个样本均为空和 Tushare 缺 token。真实
  供应商/DeepSeek 现场、五时段运行矩阵和外部服务状态不能由离线门禁替代，需在可用环境补测；正式 `0.2.0`
  release 仍未声明，需用户另立发布批次。
- 既有 `score_p0_v2_historical_planned_dates_missed`、V1/V2 收益证据不足和 overlay 性能长尾保持原状；本批
  只清理架构与治理痕迹，不回填研究身份、不改变评分策略、不放宽任何质量门槛。

- 研究诊断如实报告既有 `score_p0_v2_historical_planned_dates_missed`：历史窗口已错过 7 个固定计划日，
  当前最大可达 33/40 且 `recoverable=false`。本批不回填、顺延或改写该研究身份；这不是包迁移回归，后续
  新窗口必须另立预注册身份。
- 本批未执行 Firefox 浏览器专项和真实供应商/DeepSeek 现场门禁，因为没有改变 Web、行情、评分、冻结或
  预算边界；最终发布批次仍需按计划补齐可用环境中的发布级验收。批次 10 的迁移痕迹清理尚未开始。

- 统一 `browser` profile 在运行行为断言前因本机未安装 geckodriver，以
  `Firefox and geckodriver are required` 退出；因此 Firefox 下的 SSE 重连、cursor resync 和
  patch-to-paint 专项仍是精确未验证门禁，不宣称通过。直接 JS 状态回归、SSE/HTTP 契约和 Chrome 三档
  端到端证据均已通过；本批未改变静态资源、SSE 算法、供应商 I/O、评分、冻结或持久化，当前无已知
  实现缺陷。

- `v2_status_v10` 对 schedule point 字段是有意的破坏性 schema 更新；旧页面或外部读取方必须随 v11 资源
  正常重启/刷新，不提供兼容字段。真实供应商、DeepSeek 配额和五时段现场行情未调用，因为本批不改变
  行情、评分、预算、冻结或时间窗行为；这些边界由全量回归和调度集成覆盖，目前无已知未解决问题。

- 本批完成 V1 同口径留出、全候选配对、正式输入精确绑定、T+1 标签和两层报告工程，但不伪称 V1/V2
  已有收益胜者。V1 留出和 V2 历史路线均未通过收益/风险门禁；前向比较仍需真实累积 522 个独立交易日，
  V2 风险 challenger 仍需新的 60/20/40 日标签窗口。达到门槛前不得自动切换、调权或宣称收益改善。
- 统一研究诊断仍报告与本批独立的 `score_p0_v2_historical_planned_dates_missed`：2026-08-24 至
  2026-08-31 已错过 6 个历史计划交易日，当前最大可达 34/40、`recoverable=false`。本批不回填或改写
  该既有研究身份；Tomorrow V1/V2 的 H0 工件与新前向采集链不依赖它。
- 真实冷启动预热累计 10 次供应商批次超时、45 次请求失败，最终仍覆盖 342/360 并完成评分；启动日志
  另有两次证券主数据同观测时间持久化冲突，但进程保留内存中 360/360 证券身份并受控继续。它们未阻断
  本批逐股资格结论，但属于数据源/持久化可靠性后续问题，不能解释成收益改善证据。

- 固定离线性能两次仅 `targeted_overlay_commit` 未过 100ms 旧绝对预算，P95 分别为 135.504ms 和
  112.803ms；该路径未被本批修改且不经过人口/候选评分。相关评分指标均通过：板内本地评分 P95
  12.650ms、三策略板内评分 P95 32.891ms、三板墙钟 P95 12.950ms；浏览器 patch-to-paint P95 6ms。
  本批不借评分修复改写无关 overlay 实现或放宽性能预算，该既有长尾继续单独留证。

- 本批不把历史覆盖不足或结构化研究缺失伪装成修复完成。修复前现场候选历史为 351/360，低于 99%
  发布门槛，且 `structured_risk_unavailable`、`corporate_risk_history_unavailable` 各为 360；因此即使
  本地评分漏斗恢复，策略仍可合法保持 `not_ready/history_coverage_incomplete`，候选也只能观察而不能
  进入 DeepSeek 或正式执行池。历史/研究来源恢复属于既有异步数据就绪过程，不通过降低门槛解决。

- 本批不放宽 99% 历史覆盖、评分、过滤、动作、风险、融合、TopK、DeepSeek 预算或冻结规则。现场历史
  重启后从 81 推进至 288/360，策略当轮可用历史最高为 239/360，且观察到有界 warmup timeout；达到门槛
  前会继续合法产生 `not_ready/history_coverage_incomplete`。这与修复前已有 229 个完整评分却因旧
  coverage 显示 0 的错误不同；该独立运行容量问题如持续，应另立批次处理。
- 固定离线性能两次只有未被本批修改、也不经过 coverage/SSE replacement 的
  `targeted_overlay_commit` P95 约 109-113ms，高于 100ms 绝对预算；同一未修改基线在本机复测为
  38.8ms 并通过，显示该旧指标存在已知长尾波动。本批相关 `sse_publish` P95 为 0.029ms、真实浏览器
  patch-to-paint P95 为 16ms，未放宽任何性能预算；旧 overlay 长尾仍作为发布残余风险保留。

- 命令整合和默认 V1 不构成收益结论。V1 同口径留出与 V2 历史门禁都已拒绝晋级；两档谁更能挣钱
  必须等待已预注册的同日同股前向比较，后台不得自动切换。`research-screen` 若任一合法研究门禁拒绝，
  会完成其余阶段并最终返回非零，这是可观察门禁结果而非组合器故障。

- V1 已有同口径独立留出但净增量为负，V2 虽有正平均历史净增量仍为 `historical_rejected`；两者都
  没有已验证的逐股亏损概率。全候选前向链从首个合法输入开始采集，但尚无 522 个真实独立日终态报告；
  V2.x 风险 challenger 目前只有预注册、没有拟合或校准结果。这些时间性证据不影响当前所选档位运行，
  也没有收益保证或自动发布权限。
- `p1_manual_residual_momentum_v1` 是用户授权的 H0 日线 proxy，不是
  `score_tomorrow_shadow_p1_v1` 五候选研究的胜者；它缺少原 P1 的行业/市值/流动性完整中性化和真实
  2027 点时证据，状态会持续公开该差异。当前默认已按用户要求改为 V1；两种人工 profile 都未建立逐股
  亏损概率头，也不自动更新参数。切换通过启动参数并重启，且只影响尚未冻结的新 Tomorrow 决策。
- P2 历史终态仍为 `historical_rejected`：严重亏损率 15.9472% 高于代理 8.2734%，换手增加 56.2350 个
  百分点且 Q5-Q1 为负。本次是用户知情授权的生产例外，不代表收益门禁通过；在线 Top120 点时横截面也
  不等同于训练时完整 H0 收盘横截面。生产会自动结算新证据，但任何模型、权重或门槛更新仍需新的人工
  Review 与发布批次。
- 2026-08-30 为非交易日，真实 `run.sh` 只能证明启动、资源 hash、状态/Web、外部来源和生产函数性能，
  当日调度按契约处于 `closed/not_ready`，不能伪造 14:50 冻结样本；交易日整链行为由集成/契约测试覆盖，
  下一真实交易日由后台正常采集并结算。Tushare 因本机未配置 token 显式降级，但腾讯 qfq 历史、腾讯
  行情和交易所证券主数据均通过，不阻塞本地推荐；DeepSeek 同样因缺少 API key 保持零调用降级。本机
  没有 Firefox/geckodriver，因此统一诊断的 Firefox 刷新探针保持环境失败；Chrome 三档桌面发布门禁已
  通过，但两者不是同一浏览器刷新间隔测量，后续安装 Firefox/geckodriver 后仍可补采该专项证据。
- 用户明确选择暂不处理 `.token_key`，因此 DeepSeek 继续以 `api_key_missing` 零调用降级，Tushare 继续以
  `missing_token` 降级；两者不是本批 688981 历史修复的失败，也未被隐藏。东方财富三个历史主机的外部
  `ConnectionError` 仍可能发生，但腾讯严格 qfq 等价路径已使本次 688981 组合历史恢复。
- 独立的 2027 `score_tomorrow_shadow_p1_v1` 研究身份仍保持未开始且不影响当前评分；本次启用的是已经
  封存的 P2 工件。P1 若未来继续，仍须绑定官方日历并按原 40+20 身份形成不可回填证据，但不得覆盖、
  调权或自动晋级当前 P2 生产版本。
- 本批只治理文档所有权，不修改运行代码、研究工件或生产状态；既有外部风险不因删除旧文件而消失。
  当前仍无获准生产的新评分候选，P2 保持终止。若未来提出不同候选，必须从两份权威文档另立完整研究
  身份和独立交付批次，不能从已删除文件恢复任务或放宽既有拒绝门禁。
- P2 研究报告仍是 `historical_rejected`，且 H0 不能证明真实 14:50 基线、历史 ST/行业/披露、公司风险或
  DeepSeek facts；用户人工越权只授权当前工件进入 Tomorrow，不会改写这些失败证据。Web 已真实展示
  模型版本和预测成本后净超额，但 P2 没有逐股亏损概率头，因此该指标仍不得伪造。
- 上交所公开 HTTPS 在连续新连接时仍可能出现瞬时 `connection_failed`，免费接口没有供应商 SLA；当前以
  单请求有限重试、调度级 300 秒退避、启动/缺口续刷和最近有效持久化快照降级，不会接受部分响应，
  但首次安装且所有重试均失败时仍会保持基础资料未就绪，等待下一次调度。
- 2026-08-30 为非交易日，无法形成新的 360 候选评分漏斗；本批已证明全局基础资料 5212/5212 和对应
  门禁回归，但仍需下一个真实交易窗口确认候选作用域达到 360/360。只有取得完整评分分布后，才能另立
  批次判断 78 分正式线是否过严；本批不提前下调门槛。
- H0 不含真实历史 ST/行业、14:50 分钟输入、披露/接收时点风险或 DeepSeek facts；当前生产仍执行真实
  硬过滤、行业约束和本地/DeepSeek 风险，但历史筛选无法回补证明这些差异。pandas/NumPy ABI 问题只
  修复了本地验证环境，干净构建仍需由常规依赖门禁观察间接依赖漂移。
- 首页按用户偏好不再解释“基础资料”缺口的上市日期/交易日龄构成；真实缺口仍会影响正式发布，并可从
  类型化 status、推荐漏斗和诊断工具查看。行情、历史与基础资料计数随实时来源和预热进度变化，示例
  `120/360`、`77/360` 不是固定产品常量；已打开的旧浏览器标签需要刷新后才会加载 v4 静态资源。最终
  有界抽样仍出现 1 次历史源空响应，运行链按最近有效值和显式降级处理，后续交易日仍需持续观察。
- P1 批次 6 仍缺少 2027 年官方日历证明、40 个历史日、20 个前向日、至少 300/100 条配对及全部晋级
  门禁；该身份不能通过代码、mock、回补、顺延或降低门槛恢复资格。P1 继续只读保留，但已不再决定
  产品等待时间；P2 历史身份已在读取结果前冻结，后续前向身份仍须在历史报告通过并封存后、任何前向
  输入可见前另行预注册，不能复用 P1 规避审计。
- 事故手册降低的是诊断误判和交付遗漏风险，不会自动修复外部来源覆盖、冻结窗口内未形成正式记录或新的
  产品缺陷，也不能把历史根因套用到未来事件；每次仍须取得当次六检查点证据。`trader-delivery` 是代理
  交付 workflow，不是产品运行时 hook；符合 `AGENTS.md` 的仓库修改必须显式加载，Skill 自动发现策略
  不能替代代理遵守仓库流程。
- 当前真实运行不再有已知的刷新结果构造缺陷，但正式推荐仍被独立数据质量门禁阻断：证券主数据只覆盖
  `120/360`，Tomorrow/D25 历史可评分分别为 78/79，完整评分为 56/58，因此正式选择仍为 0；Web 现在
  展示这些真实计数和 `security_master_coverage_incomplete`，不能把候选非零误解为门禁应被绕过。
  Today 在 11:20 后冷启动保持 `not_ready` 是冻结契约要求，不允许用收盘行情补造。完整诊断另观测到
  历史源 3 次有界抽样中 1 次空响应；来源继续按最近有效值和显式降级处理，后续交易日仍需持续观察。
- 批次 5 预注册日尚无 2027 年上交所官方年度休市文件。规范已冻结 60 个精确日期，但 collector 会在首日
  前缺少 `score_tomorrow_shadow_calendar_attestation_v1` 时失败关闭；若官方日历与日期不一致，该身份必须
  终止且不得换日。真实 40+20 观察、至少 300/100 配对和收益/风险门禁尚未发生，因此没有
  `promotion_eligible` 或任何生产授权。
- 当前真实点时窗口不足，新增模型和成本感知选择尚无可信样本外净超额、真实概率校准、换手或尾部风险改善证据；固定阈值仅完成工程预注册，不代表最优，批次 5 仍须在标签可见前冻结身份并取得历史样本外与连续前向证据。
- `score_p0_v2` 已错过的正式计划日不可回填；对应运行日志缺失，运行级直接原因仍待验证。新研究身份须等待后续评分规范与完整未来窗口冻结，本批不创建占位身份。
- 本次真实 `full` 是当前网络、供应商、本机浏览器和运行服务的单次有界样本，不代表后续交易时段永不
  抖动；`sources/live/full` 仍会实际消耗供应商调用配额。上一批记录的历史空响应和
  `market_merge:absolute_budget` 本轮均未复现且性能门禁通过，说明它们至少不是本次脚本合并造成的稳定
  回归，但若再次出现仍应保留完整统一报告并按首个失败边界另开业务批次诊断。
- 当前真实 R2 点时覆盖仍不足 40 个有效交易日，因子报告只能标记 `exploratory`；本批完成的是诊断工程
  能力，不证明任何因子可提高未来荐股收益，也不创建生产晋级资格。市值和流动性值必须由后续显式离线
  研究执行从同一 R2 point-in-time bundle 投影后传入，缺失只进入 `unknown`，禁止用后来数据回填。
- 真实供应商、交易窗口和本机浏览器状态仍会随时间变化，`sources/live/full` 会消耗适用的外部调用配额；
  Skill 和统一入口只能强制选择证据、聚合定位，不能自动证明或修复业务根因。当前 `full` 现场仍存在
  `market_merge:absolute_budget` 性能门禁失败，且历史源有一个空样本；两者未被本批越界修改，应分别在
  后续性能/供应商批次复现首个故障边界后处理。当前运行服务通过只读 Web 检查，但正式新代码装载仍需
  每个影响生产运行的后续批次按 Skill 要求正常重启并核对 release 身份。
- 当前代码仍为 `Unreleased`，正式 0.2.0 发布批次尚未发起；原生评分因子诊断层只完成离线工程能力，
  没有声明正式发布、生成新研究身份或改变生产评分。历史 Changelog/报告仍可
  以过去时引用已删除文件名，这是审计证据而非活动文档依赖。
- `score_p0_v2` 已不可逆地无法达到 40/40；已有证据继续保留且不补写。四日缺少 committed event 和
  正式决策是已确认的直接证据缺口，但缺失产生的运行级原因因没有对应日志仍待验证，数据库隔离记录
  不能单独证明因果。本批只修复研究资格与状态真相，不改变生产评分或直接提高收益；下一批应按计划先
  补齐点时股票池、历史 ST、行业、退市、公司风险和精确 14:50 输入，完整冻结新规范后再预注册新身份。
- 120 积分官方权限仍只有非复权日线，复权因子和 `pro_bar(qfq)` 明确要求 2000 积分，因此本批不能让
  Tushare 替代评分历史主源；评分继续使用腾讯 qfq 与东方财富 qfq 回退。状态中的 `process_*` 调用计数
  随进程启动，不能表示其他诊断进程或供应商账户的跨进程实际余量；供应商最终限额仍是权威门禁。
- 当前常驻服务仍是本批修改前启动的进程，必须正常重启后才会采用批量历史事务、4.5 秒来源尝试上限和
  新增状态字段；本批没有跨进程强制终止用户服务。真实供应商仍可能返回空历史或发生操作系统级长尾；
  实测两轮 10 个观测中 8 个取得 61 行、2 个受控空响应，空响应会逐股退避而不应制造 batch deadline。
  若四段外部 I/O 或 SQLite 在预留预算后仍真正超过 20 秒，系统仍会保留同名告警和 timeout 计数，不能
  以静默吞掉真实 deadline 的方式追求日志绝对为零。
- 当前没有已知未解决的代码侧实时性缺陷。确定性性能与 Firefox fixture 不消耗真实 DeepSeek 额度，
  也不能替代交易时段供应商网络、限流和整日 P95；应在开市窗口继续复用已有参数化脚本采样真实腾讯
  延迟和端到端 waterfall。本机实际运行 Python 3.14 与 Firefox，Python 3.10-3.13 由 Ruff、mypy
  与 wheel metadata 静态覆盖。
- 当前没有已知未解决代码、测试、类型、打包或桌面问题。正式 0.2.0 release 尚未由用户发起，版本归档
  和 tag 仍是 `docs/implementation-plan.md` 中独立未闭合 Gate；本批只提交并推送 `Unreleased` 改进。
- R2-R5 离线研究模块仍因 `score_p0_v2` 和 Score-R6/R7 的权威预注册边界而保留，但生产组合根、HTTP、
  冻结和 DeepSeek 请求链均不可达。未来真实交易日前向证据与人工晋级仍受日期约束，本批不能提前生成。
- 本批验证使用确定性离线行情和浏览器 fixture，没有访问真实供应商，也没有改变候选、评分、风险、融合、
  168 次预算或冻结规则；真实交易窗口的供应商时延与可用性继续属于既有外部风险，不能从离线门禁推断 SLA。

- 当前无已知未解决代码或契约问题。旧 schema v1 正式记录必须随完整旧 release 离线保留，当前 release
  有意只接受当前 schema；这是一项明确迁移边界，不是隐藏兼容。两条 Python 3.14 SQLite datetime
  adapter 弃用警告早于本批且不位于改动边界，后续应在独立持久化批次迁移显式 adapter/converter。

- 本批离线生产函数、确定性 SSE/Firefox 和三档桌面证据均通过，但 5000 端口原常驻服务在盘中只读检查后
  已停止，未能用新提交重启并覆盖真实供应商的早盘、午后及 14:50 后三个窗口。推送后必须正常重启服务，
  再复用 `scripts/check_web_recommendation_health.py`、`scripts/sample_tencent_quotes.py` 和
  `scripts/measure_web_refresh_interval.py` 执行盘中复测；当前不能把离线结果表述为真实供应商 SLA。

- 确定性回归已覆盖 14:49:20 检查点缺稿、输入完成后重评分、策略级重试和 14:50 CAS；本批执行时尚无
  对应真实交易日午后窗口，实际供应商延迟下的检查点年龄和正式冻结仍需在下一次 14:49:20-14:50
  复用现有状态/诊断工具留证。该外部证据缺口不改变固定公式、门槛、预算或冻结规则。

- 脚本能检测并留证 Web 投影与推荐漏斗异常，但不会自动修复数据链。当前没有本批真实交易时段的异常
  样本，导致用户所见持续归零的生产根因仍待确认；应在下次出现时保留完整聚合报告，并结合其中阶段、
  blocker、runtime/release/projection 身份继续定位。短于采样间隔的瞬时闪断可能不形成持续异常。

- 离线生产性能、确定性 Firefox fixture 和浏览器 patch-to-paint 可在任意时刻复验；腾讯供应商真实
  早盘窗口已留证，但午后及 14:50 后两个窗口尚未发生，仍受交易日、交易时段、网络与供应商可用性
  约束，必须复用已固化脚本分窗口补证，不能用早盘或确定性 fixture 替代其权威数据年龄验收。当前
  35 秒仅保留为 Web 展示余量，未被用来放宽 cadence 或数据年龄门槛。

- `AGENTS.md` 约束代理和协作者，但不能单独阻止绕过流程的人工提交；活动源码仍以现有 AST 契约作为
  机器门禁。新增真正的外部 JSON 边界可能需要精确豁免，必须按本批新增规则提供证据，不得泛化豁免。

- 本批不改变 `/api/v2/status`、行情健康、评分、过滤、冻结或 DeepSeek 行为；显式 JSON 投影仍需在
  新增公开字段时同步维护 schema 测试。工作树中的 Realtime-R1 未提交改动由其独立批次负责，本批只
  分块暂存状态边界相关 hunk，不得混入或替其宣告验证通过。

- Firefox 卡片和 65 秒刷新验收使用确定性内存行情以隔离休市期外部来源不变，完整覆盖真实应用查询、
  HTTP、SSE 和 DOM，但不替代开市期间供应商网络与整日数据年龄观测；本批未改行情 provider、评分、
  门槛、冻结或 DeepSeek 配置，也未向运行库写测试数据。线上进程必须重启后才会加载 v17 资源和新状态链。

- 本批实测使用确定性变价输入隔离供应商在休市时版本不变化的干扰，完整经过活动生产调度、统一索引、
  SSE、Flask 和 Firefox，但不是开市期间真实供应商压力测试；行情源网络抖动仍应在交易时段复用同一
  脚本复核。`CadencePlanner` 尚未接入生产调度、当前活动链约 30 秒才提交一次到期策略的问题不由 Web
  保留窗口掩盖，1–10 秒配置 cadence 与实际提交间隔的架构差距仍需作为独立高风险调度改造处理。

- 端到端脚本使用确定性递增报价隔离外部供应商变化，能测内部稳定间隔和浏览器渲染，但不替代真实
  交易时段的来源数据年龄与整日 P95；运行它需要本机已有 Firefox/geckodriver 和可绑定的回环端口。
  腾讯脚本需要公开网络可达，其来源不变化只表示采样窗口内版本稳定，不证明系统漏采或供应商固定
  更新周期；脚本不会调用 DeepSeek、修改运行库或验证荐股收益。

- 隔离真实服务使用全新空运行库且测试时东方财富全市场请求失败、Sina 行情成功，因此首次状态为
  security master 0、历史预热 30/360，Today/Tomorrow/D25 按契约 `not_ready`；状态已明确显示
  5,548 行实时行情、224 行候选、身份/历史缺口和来源错误。该外部来源/冷启动缺口未通过猜测身份、
  放宽门槛或复用正式运行库掩盖，本批也未改行情 provider、身份持久化、历史预热或评分策略。
- 隔离机仍未配置 `DEEPSEEK_API_KEY`，真实状态为 `configured=false`、`physical_attempts=0`、
  `zero_call_reason=api_key_missing`；代码不能生成外部凭据，且本批未改 DeepSeek 预算和降级行为。

- 帮助文本可以解释命令用途，但不会代替离线研究所需的覆盖、父报告、前向身份和人工授权门禁；
  `research-*` 仍可能耗时、访问网络或写不可变研究工件，普通看盘应继续只运行无参数 `./run.sh`。
- 当前 Linux 验证机未安装 PowerShell，`run.ps1` 本批只能由共享命令保留、帮助分组和前置校验文本
  契约覆盖；Windows 入口保持原执行模型，`run.bat` 仍只委托 `run.ps1`。

- 免费全市场富身份仍依赖公开行情端点的可用性和响应完整度；端点超时或暂时只返回稀疏字段时，
  系统保留最近有效身份、显示剩余覆盖缺口并继续本地降级，不伪造上市日期。没有 Tushare token 只会
  缺少其可选增强，不再妨碍免费身份闭环。

- 代码只能保存供应商已经完整返回的真实证券身份，不能伪造上市日期。当前真实东方财富端点存在
  间歇断连；部署 v15 后身份覆盖会在下一次东方财富完整全市场响应完成并经独立 `reference` lane
  落库时收敛。若外部来源持续失败，状态卡会继续如实显示剩余上市日期/交易日龄缺口，推荐保持
  `not_ready`，不会用猜测绕过安全门；Tushare token 不属于该闭环的恢复条件。

- 本批不改变 Tomorrow 固定 78 分正式阈值、观察门槛、风险门或候选供给；现场 Tomorrow 最高分
  72.35 且 `selected_observe=0` 时仍会真实显示空观察池，D25 可独立保留其已达到观察条件的条目。
  当前时刻已越过午间，因此 `midday_recovery` 只能由可注入上海时钟回归验证，不能伪造生产时段；
  部署后新 revision 仍需正常重启进程才生效。本机缺少 `DEEPSEEK_API_KEY` 的外部凭据缺口保持不变。

- 本批不会擅自终止仍在运行的本地服务；代码提交后必须正常重启，旧进程才会从 v1 切换到
  `v2_status_v2`/`v2_decision_view_v2` 并重新建立纯内存观察草稿。release 握手能彻底消除混版被误报
  为“正在生成”，但不能替代上游行情、身份、历史覆盖或评分本身；新进程若 lane 空闲仍无草稿会如实
  显示“未形成”，不补造股票也不降低既有阈值。

- 草稿只代表“本地评分已完成但正式输入质量门禁未通过”，不会提高行情、证券身份、历史或公司风险
  的真实覆盖，也不会产生正式推荐、冻结或收益证据；78/76 等既有动作阈值、风险、融合和排名均未
  改变，因此本批不修改 `docs/recommendation-strategy.md`。若草稿中没有达到观察动作的股票，观察池
  会正确显示空草稿而不会补数。
- 已运行的常驻进程不会热加载 `v2_decision_view_v2` 或新 revision 的静态资源；提交部署后仍需正常
  停止旧服务、执行 `./run.sh validate-config` 并重启。本机缺少 `DEEPSEEK_API_KEY` 的外部凭据缺口
  仍然存在，代码只会继续如实报告零物理调用原因。

- 本批恢复的是既有运行事实的安全投影和 Web 展示，不会补造正式推荐、观察项或上游数据。证券身份、
  行情和历史覆盖不足时短线仍可合法保持 `not_ready`，但卡片会明确显示缺口、草稿和最高分；评分、
  风险、融合、排名及 78/76 等动作门槛均未改变，故未修改 `docs/recommendation-strategy.md`。
- 已运行的常驻进程不会热加载本批 Python 与带新 revision 的 JavaScript；部署提交后必须正常停止旧
  `run.sh`/`trader-server`，先执行 `./run.sh validate-config` 再重启，随后核对 `/api/v2/status` 的
  `runtime_version`、`scheduler.input_quality` 和 `market_data`。本机缺少 `DEEPSEEK_API_KEY` 时物理
  调用仍为 0，页面只会如实显示 `configured=false`/缺钥原因，代码不会伪造凭据。

- 当前正在运行的 `trader-server` 早于本批代码修改，且本轮检查处于 `closed` 阶段；需要用户正常停止
  并重启后，在活动行情阶段至少完成一次共享输入刷新，再核对 `security_master_recent` committed
  行数接近本轮规范全市场股票数。固定回归证明代码范围与事务行为，不替代真实供应商完整交易日证据。
- 本机仍未配置 `DEEPSEEK_API_KEY`，因此物理调用按安全契约保持 0；代码没有伪造凭据。Tomorrow 固定
  78 分门槛未调整，用户提供的当日最高本地分 75.72 也未被当作放宽依据；本批修复数据供给与诊断，
  不承诺立即产生可执行推荐或改善收益。

- 108MiB 损坏数据平面副本仍保留在 `.runtime/v2/v2-data`，没有尝试从物理损坏页恢复其中的最近证券
  主数据、紧凑历史、风险证据或游标；活动库会按公开来源重新预热。正式决策、DeepSeek 预算、研究
  observation、历史筛选和 outcome 使用独立数据库，未被本批移动。损坏的上游触发事件仍待验证；若
  需要取证或尽力恢复副本，应作为独立的只读 SQLite 恢复任务处理，不能把未校验记录写回活动库。

- 本批不承诺立即产生更多可执行荐股或改善真实收益。证券主数据仍依赖上游真实上市日期与交易日历，
  未覆盖时继续 `not_ready`；公告首次完整建基线最多需要 50 页且受免费来源稳定性影响，失败时继续
  保留最近有效事实并降为观察。CNInfo 的交易所公告交叉校验仍为既有 `pending` 后续项。

- 当前机器若未配置 `DEEPSEEK_API_KEY`，DeepSeek 仍按契约不发物理请求并回退本地链；本批只让原因
  可验证，不写入或伪造密钥。评分/过滤阈值未因空池自动放宽，只有供给门禁全部通过后仍长期为空，
  才能在独立预注册样本外收益/回撤批次评估阈值调整。

- 审计时 V2 数据平面约 5,567 只全市场股票中仅 842 只具有已持久化证券主数据，公司公告全历史完成
  覆盖为 0，正式 recommendation outcome 也为 0。Score-P1 会如实显示 `not_ready`，但不会伪造
  风险已核或为了增加数量放宽门槛；因此本批能先阻止新的不可靠可执行推荐，不能证明既有观察股
  的损失已经追回或未来收益已提高。
- DeepSeek 物理请求仍只对结构化风险通过且进入 `pass` 的候选发生。公司公告全历史覆盖恢复前，
  候选会按契约降为观察，DeepSeek 继续可能为 0 次；生产风险登记簿回填/官方公告来源接线必须作为
  下一独立批次完成，不能把风险缺失猜成 `known_clear`。
- 审计时没有运行中的 `serve` 进程；部署本提交后需要重新启动 `./run.sh serve` 才会加载新门禁和
  下行保护。观察池仍是不可执行研究项，不能按正式推荐买入；真实收益改进只能由后续可执行样本的
  不可变结算证据验证。

- 三类机制显著降低了复用历史诊断换手、严重亏损和波动，但未保住预注册收益/召回门槛；且该区间已被
  R6D 观察，仍有当前股票池幸存者偏差及历史 ST/行业/公司风险/盘中尾部缺失。因此不能宣称收益提高，
  不能启动前向资格或生产发布。若继续，应另立新研究身份探索更温和、分层或持有期感知的稳定机制，
  并使用未观察证据验证；不得改写本报告或在当前生产中开启本候选。

- 当前候选虽改善历史验证收益、严重亏损和波动，但换手增幅超过预注册容忍度，且 H0 仍存在当前股票池
  幸存者偏差、历史 ST/停牌/行业/公司风险和盘中尾部未完整重建，因此不能声称生产收益已提高。若继续，
  必须以新研究身份预注册换手稳定机制并重新训练/样本外验证；不得修改本报告门槛或自动晋级。

- 当前已经运行的 `./run.sh serve` 进程不会热加载 Python 查询实现，需要用户在方便时正常重启服务后
  才能看到新顺序；本批没有擅自中断它。收益优化、权重、动作门槛和 DeepSeek 策略均未在本批调整，
  应作为下一独立交付批次基于历史日线与固定样本外门禁推进。

- 当前运行中的服务仍加载修复前代码，`/api/v2/status` 尚无 `company_research` 且 DeepSeek 物理调用为
  0；需在本批提交后由用户正常重启才会启用新接线。本机项目根 `.token_key` 和当前 shell 的
  `DEEPSEEK_API_KEY` 均未配置，因此即使结构化研究恢复，模型请求仍会按降级契约保持本地结果；用户
  需自行安全配置密钥，不能提交到 Git。外部公司研究源的实际可用性仍可能触发有界退避，但不会阻塞
  本地发布、实时行情或只读 Web；本批不承诺收益提升，日线趋势优化留作后续独立批次验证。

- 当前运行目录没有 Score-R6 历史/前向制品，真实 H0 覆盖、冻结候选及不重叠的 20 日前向证据尚未
  完成，因此本批没有也不能生成正式 PromotionDossier 实例，更未授权生产发布。证据到齐后需显式
  运行 R7 命令并由人工审查；确认后的策略版本提升和活动配置变更仍是未来独立交付批次。

- 本机实际门禁使用 Python 3.14 与 Firefox headless；Python 3.10-3.13 由 Ruff 目标、mypy 配置和
  wheel `Requires-Python` 元数据静态覆盖。真实供应商/DeepSeek 网络未调用，符合离线 R7 边界。

- 本批建立并验证的是 Score-R6 工程能力，没有伪造真实筛选或前向结论：运行归档尚未产生满足门槛的
  H0 报告/候选时不会写 R6 历史报告；尚未预注册并完成新的 20 个真实交易日前向窗口，因此当前不能
  获得生产晋级资格、不会改动策略配置。Score-R7 的人工审批、配置发布和回滚演练仍待下一批执行。

- H0 数据只可重建日线量价代理，历史 ST/行业、盘中尾部、公司风险和 DeepSeek 点时事实缺失并已写入
  报告限制，不能解释为零风险；全市场归档上的离线查询耗时和资源占用尚未以用户真实完整归档压测，
  但入口保持显式离线、只读生产状态，失败不会阻塞 `serve` 或本地推荐。

- 本批没有实际联网下载全 A 股 640 日历史，因此真实供应商可用性、全量耗时、磁盘占用和最终覆盖率
  仍取决于用户运行环境；命令可断点续跑，失败只影响 H0 研究归档，不阻塞 `serve` 或本地推荐。
  当前股票池按下载时存续证券构建且缺少历史 ST/行业、盘中尾部、公司风险与 DeepSeek 点时事实，报告
  会显式列出这些限制，任何 H0/R6 回顾性结果都不能生成 `promotion_eligible` 或自动修改生产策略。

- `score_p0_v2` 的 40+20 证据依赖未来真实交易日、服务连续运行、点时输入和后续结算完整，当前
  `research-status` 为 `historical_collecting` 且记录进度 `0/40`，因此 Score-R6 仍不可执行。
  `serve` 只采集 committed observation/outcome；窗口完成后仍须显式运行 R2-R5，且只有真实
  `promotion_eligible` 才能开始 R6。任一计划日失败将诚实阻止本批晋级，不会自动换日。

- 原 `score_p0_v1` 的 2026-06-15 至 2026-08-10 点时窗口没有形成完整不可变分区，后来的日 K、缓存
  或 production 运行不能反向重建当时输入；该身份继续保持 `historical_rejected`，Score-R6 仍因
  缺少真实 `promotion_eligible` 而不可执行。修复只保证新进程启动后的后续证据，绝不回填或换日。

- legacy 研究库保持原字节且已接近旧上限，新分区在声明的 120 交易日/20GB 容量耗尽时会再次显式
  拒绝而不自动删除不可变证据；磁盘、SQLite 或行情源失败仍可能使单次研究/outcome 记录失败，但会
  进入 observer/scheduler 状态且不阻塞本地推荐。已运行的旧进程需正常重启后才会加载本次接线。

- 终端是否单击、Ctrl+单击或 Cmd+单击打开链接由具体终端模拟器决定；输出本身是标准 HTTP URL，
  不支持超链接的终端仍可复制到浏览器。已运行的旧进程需要正常停止并重新执行 `./run.sh` 才会加载
  新提示；本批不自动启动浏览器，也不改变 Web、API、运行时、策略或数据行为。

- 已在运行的旧 `trader-server` 不会热加载本次入口变化，需正常停止后重新执行 `./run.sh` 才会看到
  地址提示。输出仅表示本机监听端口已成功绑定，不替代 `/api/v2/status` 健康检查；本批没有改变
  运行时、行情、策略、DeepSeek、冻结、API 或 Web 页面行为，未跟踪截图保持未暂存。

- 旧 schema v1 正式记录只能随完整旧 release 离线保留；新决策若上游对某个可选行情字段本身未提供值，
  该字段仍显式缺失且不会阻断本地评分。当前运行中的旧进程需正常
  重启后才会生成含锚点的新决策；本批不改变候选、过滤、评分、融合、风险、动作、排名或冻结时点。

- HMS 使用英文单位缩写以消除“分数”歧义；它表示距行情来源时间的持续时长，不是北京时间时刻。
  当前运行中的旧 Web 进程需正常重启后才会加载静态资源 revision v9。共享工作树中的报价锚点相关
  未提交修改和截图不属于本批，将保持未暂存；本批不改变 API、行情采集、评分、风险或冻结行为。

- 行情覆盖只判断当前名单的核心报价与证券身份是否足以展示，不代表成交额、换手率、市值等所有
  可选行情字段都完整；真实覆盖数量仍取决于运行时数据源和最近有效快照。当前运行中的旧 Web 进程
  必须正常重启后才会加载静态资源 revision v8；本批没有修改 API、候选、过滤、评分、融合、风险、
  冻结或 DeepSeek 预算，也不承诺产生正式荐股。未跟踪截图保持原状且不会纳入提交。

- “最优方案”无法只靠关键词机械判定，后续批次仍必须用根因、备选方案、目标架构和自动验证证据
  接受 Review；允许全仓重构可能扩大交付时间和回归范围，但不得据此退回已知较差的局部补丁。当前
  工作树中的未跟踪截图不属于本批，保持原状且不会纳入提交。

- 当前用户运行中的服务仍是本批修改前进程，本批遵守进程所有权约束未代为停止；提交后需要用户
  正常重启才会加载新运行链。若东方财富全市场主源在首次启动和本地证券主数据均为空时持续失败，
  当轮仍会诚实显示身份缺失降级；源恢复后的共享批次会自动补齐并持久化。企业风险历史、结构化
  风险证据和跨源偏差是独立质量维度，仍可能让个股保持观察或被过滤；本批不承诺必然产生正式荐股。

- 最近错误历史按设计仅保存在当前进程内，服务重启后清空；本批没有新增诊断数据库或文件 I/O。
  外部数据源未提供可信时间时界面显示“时间待确认”，不会伪造发生时间。推荐、评分、风险、融合、
  DeepSeek 预算和冻结业务边界均未改变。

- 当前 `127.0.0.1:5000` 进程仍加载本批修复前代码并持续报告三策略 `decision:valueerror`，必须由用户
  正常停止后重新执行 `./run.sh` 才会生效；本批未擅自停止该进程。若 Today 在新进程形成合格 current
  前已越过 11:20，则按冻结契约禁止当日追补；Tomorrow/D25 仍按各自时点处理。修复后行情源失败、
  历史预热不足、真实过滤门槛仍可能产生结构化 `not_ready` 或合法空池，本批不降低任何选股、风险、
  动作或融合门槛，也不承诺一定产生正式荐股。缺少名称/行业的旧正式记录不进入当前 release 的历史
  查询；只有当前 schema 的 scored 决策和正式记录可由活动 codec 恢复。

- Score-R5 统计、collector 与最终封存工程能力已闭合，但当前真实 R2/R4 历史点时证据仍不足 40 日；
  因此五个变体均只能诚实终止为探索性历史拒绝，不能启动真实前向 collector。固定前向日为
  2026-11-02 至 2026-11-27，当前日期尚未到达；届时任一停机、数据失败、身份冲突或少于 100 条
  前向同股配对都会不可逆地 `forward_rejected`。只有真实历史、前向及 40+20 合并门禁均通过后，
  才能另立 Score-R6 批次；本批没有收益提高或晋级结论。

- Score-R4 工程能力已经闭合，但活动研究证据仍不足预注册的 40 个完整历史点时日，因此当前只能形成
  `exploratory` 配对 manifest，没有真实历史收益、Holm 通过或晋级结论。下一章 Score-R5 才执行固定
  bootstrap/多重检验并决定是否进入 20 日连续前向；本批冻结参数不得根据后续收益回看修改。

- Score-R3 能力已经完成，但活动运行库仍没有预注册历史窗口的 40 个完整点时 epoch；因此当前只能形成
  `exploratory` 报告，没有可据此主张的 40 日真实净超额、召回、Rank IC 或晋级证据。下一章 Score-R4
  还必须先冻结连续入场过渡宽度和高热弱结构阈值，不能根据本批探索指标事后试参。

- Score-R2 能力已完成但预注册窗口的真实完整点时输入不存在于活动运行库；这属于诚实的数据覆盖
  结论，不以当前供应商响应回填。故当前没有 40 日历史收益、召回或晋级证据；Score-R3 对不足
  40 日的输入只输出 `exploratory`，只有 `extracted` 且 40 日 manifest 全部可验证后才标记
  `replayed`，活动生产策略保持不变。

- 2026-08-13 Today 已在 11:20 封口后丢失正式记录，按不可变冻结契约不能由本批盘中或重启追补，
  因而当日“今早”仍如实保持 `not_ready`；调度秒修复从下一交易日防止复发。Tomorrow/D25 当前两条
  均为 `observe` 而非 `executable`，修复后应显示在“不可执行，仅供观察”的观察池，正式荐股数仍为
  0。外部行情质量或业务门槛仍可能产生合法空池，本批不为补数量降低门槛。

- 同一观测时刻发生内容冲突时，数据平面继续按权威 recent 契约保留先提交记录；后续不同内容不会合并
  或覆盖，因此调用方应使用更精确的新 `observed_at` 表示真正的新观测。本批不改风险事实、处罚或评分
  语义，也未改 Web/API/静态资源，三档桌面浏览器验收不适用。

- 公开历史源若在盘中返回当日日 K，其来源本身可能仍是未收盘的临时值；本批只修正持久化时间不变量，
  不把临时值解释为正式收盘值。修复前因该异常未写入的记录不会自动回填，后续正常历史刷新会按既有
  recent upsert 语义补齐。未改动 Web、API、布局或静态资源，三档桌面浏览器验收不适用。

- 新研究库只从本版本部署后的 V2 committed observation 开始积累，不从已删除的旧 snapshot 或
  shadow 运行库回填；因此 Score-R2 的最多 40 日历史提取仍是下一独立章节。正式冻结二次裁剪身份
  若没有同批投影只保存通用事件，不伪造候选审计；源提交审计仍按其原 decision identity 保留。

- 冻结记录不可覆盖，因此修复前已经固化的 2026-08-12 Tomorrow/D25 空结果不会被本批删除或重算，
  对应 Tab 当日仍会如实显示空；新代码只阻止后续临时无效空集再次发布。Today 错过 11:20 后保持
  `not_ready` 仍是权威冻结契约。外部行情失败时 Long 会继续展示完整固定名单，但价格字段可能为空并
  明确提示降级。

- 外部行情质量仍可能使 Tomorrow/D25 在状态 `ready` 时通过业务过滤后选中 0 条，这与运行时
  `not_ready` 不同；本次实机 Tomorrow 的 28,127 个候选均被数据质量/策略规则拒绝。Today 在错过
  11:20 冻结后当日保持 `not_ready` 是权威契约要求，不属于本修复残留缺陷。外部行情不可用时系统
  仍按契约保留最近有效快照并显式降级。

- 外部公开行情、Tushare 和 DeepSeek 的可用性及实时质量不由本地 release 控制；服务继续按契约
  失败开放并显式降级。运行目录中可能仍有旧 release 遗留的 `runtime.sqlite3`，本批不执行破坏性
  删除；schema 9 组合根不会探测或打开它，预算只写 `deepseek-budget.sqlite3`。

- 用户再次发送“继续”，要求执行 `docs/implementation-plan.md` 下一个完整未完成章节，并追问
  为什么反复修改仍未完成。本批完成 V2-E9“唯一组合根与入口”：启动脚本不再把旧 `HOST`/`PORT`
  映射为 `TRADER_HOST`/`TRADER_PORT`，`trader-cli` 移除 `migrate-v17`、`recommendation-archive`
  和 `tomorrow-cutover-evidence` 命令；计划推进到 E9 已完成、E10 为下一工程章节。现状原因是此前
  E8 只收敛统一 Web 面，E9/E10/E11 被计划明确拆开，且上批全量 pytest 超过 17 分钟未完成，留下
  验证缺口；本批不把旧链物理删除和最终发布验收提前混入入口改动。

- 用户指出每次改动都强制全量测试没有必要。协作流程现改为低/中/高风险三级门禁：文档和
  非运行元数据只做相关契约与格式检查，局部实现验证受影响包和定向测试；共享架构、评分/
  冻结、持久化、API/SSE、打包入口、Web 行为和最终发布仍必须执行完整命令组及适用专项验收。
  未运行的门禁必须记录为“不适用”并说明原因，不能冒充通过。

- 用户发送“继续”，要求执行总计划下一个完整未完成章节。本批完整交付 V2-E8“统一 API、SSE
  与根页面”：根页面改为单一 V2 工作台，展示数据年龄、覆盖、漏斗、DeepSeek 预算、冻结、
  降级和逐股诊断；四策略 committed event、Today overlay 与 Long current 投影统一进入事件流。
  计划推进到 E8 已完成、E9 为下一工程章节，不提前切换 `.runtime/v2` 入口或物理删除全部旧链。

- 用户要求先做一次独立“权威文档去历史化与冲突清理”批次。现将两份权威文档收敛为当前
  有效产品/策略契约：明确 V2-E0 至 E7 已交付、E8 至 E11 未交付，最终 V2-only 状态不再冒充
  当前代码事实；逐批影子、cutover、v17/P1-P6 施工记录只由 Changelog 和报告保存。

- 用户发送“继续”，要求执行总计划下一个完整未完成章节。本批仅交付 V2-E7“Long 正式接管”：
  Long 从旧 Pipeline snapshot/P6/publisher 切换到统一索引 current projection，计划推进到 E7
  已完成、E8 为下一工程章节；不提前修改统一 API/SSE/根页面、唯一入口或其余旧链删除。

- 用户要求继续 `implementation-plan.md` 的未完成任务。同步上游后发现 V2-E6 代码提交已经
  推送，但计划仍标记 E6 待执行，权威文档与 Changelog 未更新，且定向测试和格式门禁实际
  失败；按未闭合批次优先规则，本批先完整 Review、修复并闭合 D25 正式接管，不进入 E7。
  计划现推进到 V2-E6 已完成、V2-E7 为下一工程章节，评分研究下一章仍为 Score-R2。

- 用户要求继续 `implementation-plan.md` 的下一个未完成任务。本批完整交付 V2-E5“Today
  正式接管”：生产 Pipeline 只为 Today 组装同批点时原生输入，不再生成旧正式 snapshot；
  全部 V2 冻结控制在评分前后执行，计划状态推进到 E5 已完成、E6 为下一工程章节。本批不
  提前实现 D25、Long 或 E8 统一 API/Web。

- 用户要求继续 `implementation-plan.md` 的未完成任务。本批完整交付 V2-E4“Tomorrow 正式
  接管”：生产组合根改由原生 `TomorrowNativeInput` 直接生成统一 local/hybrid
  `ScoredDecision`，Tomorrow 从旧 Pipeline 的正式评分、P6 冻结和盘后重建集合退出；计划
  状态推进到 E4 已完成、E5 为下一工程章节。本批不提前接管 today、d25 或 long。

- 用户要求继续 `implementation-plan.md` 的未完成任务。本批只交付下一完整工程章节
  V2-E3“独立调度与生命周期”：总计划状态推进到 E3 已完成、E4 下一章；独立 V2 runtime
  现在按策略调度数据刷新、local/hybrid 决策、CAS 发布、observer、冻结和结算，仍保持旁路，
  不提前接管 tomorrow/today/d25/long 的生产组合根、HTTP 或旧 Pipeline。

- 用户再次确认两个 G1 分支是否彻底删除，并要求在总计划未完成时继续下一完整章节。本批先
  核对两个远端引用已不存在、对应提交均由 `feature/tomorrow-v2` 可达，再移除两个干净的
  本地 worktree 与本地分支；随后完整交付 V2-E2“统一决策核心与持久化”。总计划仍未完成，
  当前状态推进到下一工程章节 V2-E3，下一研究章节仍为 Score-R2。

- 用户要求重新 Review 整份 `implementation-plan.md`，确认计划是否均已在远端
  `feature/tomorrow-v2` 落实，并在安全时清理两个 G1 worker 分支。审计结论区分了“总计划
  完成”和“G1 worker 已合并”：当次审计时仅 V2-E0/E1、Score-R0/R1 与 G1 的 R2 接口设计完成，
  V2-E2 至 E11、Score-R1-Migrate、完整 R2 至 R7 及 G2 至 G14 尚未完成；因此不宣称整个
  V2/研究路线已经落地，但 G1 分支内容已完整进入远端 feature。

- 用户要求解释并把 `codex/v2-g1-e1`、`codex/score-g1-r2` 两个 Gate G1 worker 分支合并到
  `feature/tomorrow-v2`。本批按冻结顺序先集成 E1 统一数据平面，再集成 R2 接口适配设计；
  R2 的历史读取扩展显式继承 E1 唯一 `DataPlaneReadPort`，只冻结日摘要与按代码完整字段的
  两阶段 schema，不接管生产链，也不把尚未实现的 40 日提取器提前标记完成。

- 用户要求由 `implementation-plan.md` 会话 C 继续下一个未完成工程章节。本批完整收口
  V2-E1“统一 V2 数据平面”：应用层只读边界统一命名为 `DataPlaneReadPort`，四类不可变
  epoch 现在把父版本、交易日历版本、逐字段来源/源时间/接收时间/质量/内容版本/载荷哈希
  纳入校验与内容身份；计划状态推进到 V2-E2。权威产品和策略文档同步锁定证券主数据 100%
  覆盖、候选核心历史不低于 99% 及无效空输入不得覆盖最近有效快照。低频数据仓储同步覆盖
  交易日历，recent 记录按观察时间单调更新，同观察时间异内容拒绝，formal 记录按完整元数据
  与载荷保持幂等不可覆盖。

- 用户要求把 `docs/V2_plan.md` 与 `docs/score.md` 合并为可供多个 Codex 会话同步施工的唯一
  总计划。本批把 V2 工程和评分研究统一为 `docs/implementation-plan.md` 的 V2-E/Score-R
  双 lane，固定协调集成会话 C、V2 工程会话 E、评分研究会话 R 的分支、worktree、文件
  所有权、接口冻结、交接证据、G1-G14 同步 Gate 和 E 后 R 串行集成顺序；计划不改变生产
  策略、运行配置、API、冻结或评分行为。

- 用户要求继续并提交 `docs/score.md` 的下一个完整未完成章节。本批将 P1“紧凑决策轨迹”
  状态更新为已完成、P2 待执行，并把研究轨迹的配对版本、数据裁剪、非阻塞写入、容量和
  幂等失败语义同步到两份权威文档。候选分计算现在公开复用同一纯函数组件，并在既有板内
  横截面上为全部硬过滤通过股票形成研究候选组件、缺失掩码、覆盖率、可靠度和审计排名；
  生产预选、Top120、本地评分、融合、动作及排序公式不变。为维持活动源文件 800 行上限，
  纯策略装配从组合根拆到 `bootstrap_policy.py`，`bootstrap.py` 仍是唯一对象组合根。

- 用户要求产品完全切到 V2 且不保留旧版兼容。本批把目标契约重置为 V2 唯一活动链路，
  固定 `.runtime/v2`、统一决策 API/SSE、完整旧 release 回退方式和 V2-0 至 V2-11 施工顺序。

- 用户要求继续 `docs/score.md` 的未完成任务；由于 P0 尚未 Review、提交和推送，本批先闭合
  完整 P0。此前权威文档只写了 40+20 上限，缺少固定日期、随机种子、bootstrap 实现身份和
  完整晋级指标，仍可能给后验挑日或试参留下解释空间。本批把历史主窗口固定为
  2026-06-15 至 2026-08-10、前向窗口固定为 2026-11-02 至 2026-11-27，并预注册 10,000 次
  配对移动区块 bootstrap、派生种子、Holm 检验族、五个挑战者和全部收益/回撤/召回/集中度门禁。

- 用户要求把完整挑战者、候选召回、配对 bootstrap 和收益晋级设计同步到荐股策略文档。
  原文只有预注册名称、窗口和高层门禁，尚不足以唯一实现；现将可执行计算口径、失败状态、
  哈希绑定及“研究通过后另立生产批次”的顺序写入唯一策略权威，不改变当前活动评分、过滤、
  融合、冻结、DeepSeek 预算或 Web 结果。

### Fixed

- 修复 Long 接口失败、尚未发布或返回 `not_ready` 时固定列表被清空的问题。`displayPayload()`
  现在始终以打包名单补齐卡脖子、高成长和低价潜力股票身份，并仅按代码合并可用实时行情；初次
  进入 Long 即可见名单，503/断网时显示“实时行情暂不可用，固定长期名单仍可查看”，价格等未知
  字段保持 `--`。同时停止为静态名单伪造当前发布时间，避免把降级身份冒充实时快照。
- 修复 V2-only 组合根仍把 DeepSeek 预算写入旧名 `runtime.sqlite3` 的发布边界漏洞。预算现独占
  `.runtime/v2/deepseek-budget.sqlite3`，不迁移、不读取旧库；并把启动文档从历史双 Codex 实施计划
  重写为当前 V2 启动、状态、关闭、恢复与回退说明。
- 修复严格重构门禁最后一个 `PLR0913`：部分启动资源改为不可变 `_StartedResources` 聚合，保持原
  逆序关闭和共享 deadline 行为，严格复杂度债务归零。

- 修正权威文档内部互相冲突的冻结、发布与类型口径：Today 不再出现 11:19:50 检查点或
  `close_fallback`，Tomorrow/D25 检查点窗口统一为 14:49:20 至 14:50；local/hybrid 统一为
  `ScoredDecision -> UnifiedDecisionIndex`，Long 使用无评分 `LongProjection`。
- 修正研究候选乐观上界把已确认本地风险罚分假定为 0 的问题；上界现在扣除不可消失的已知
  风险罚分，并继续受硬过滤、关键缺失、veto、动作资格和集中度约束。

- 修正 Long 名义上“不评分”但活动生产仍构造带零分 `ScoreBreakdown` 的旧
  `RecommendationSnapshot`、经过 P6/publisher 并占用 Pipeline 自有 worker 的架构冲突。
  Long 现使用无评分领域身份，定向行情慢请求仅占用 `trader-v2-long`；部分失败按代码保留
  同日最近有效报价，未知、未来或非正价格被拒绝，完整固定名单和配置顺序不再因行情失败变化。
  最终 Review 另修正把网络完成后的正常 `received_time` 误判为相对请求观察点的未来数据问题：
  runtime 现使用注入完成时钟形成投影观察点，接纳请求后、完成前收到的真实行情，同时继续拒绝
  晚于完成时刻的来源或接收时间。

- 修正 D25 原生输入组装读取不存在的 `ScoredNativeBatch.candidate_pool_size`，导致 D25 每轮在
  进入 V2 worker 前抛出 `AttributeError`、无法形成任何 local/hybrid 决策的问题；现与
  Today/Tomorrow 一致使用 Pipeline 已校验的候选容量。同步修正 Tomorrow 分支括号格式和
  D25 端口导入排序、过期计划断言、组合根超 800 行门禁，以及检查点测试使用未来决策时间
  而产生的伪失败。

- 修正 E6 初稿只接入 D25 runtime、却仍无从统一索引和正式仓储读取 D25 current/history 的
  应用层查询实例。通用只读查询现在显式绑定策略，D25 与 Tomorrow 同日 current、合法空正式
  记录、历史和状态互不泄漏；待重试的 14:50 sealed version 会阻止 15:00 fallback 覆盖，
  冷启动收盘补算只生成 local、模型请求增量保持 0。

- 修正 Today 在 11:20 无可冻结稿或边界后启动时仍可能接纳迟到 local/hybrid、再由启动、
  checkpoint、P6 或收盘路径补造正式记录的问题。统一索引新增按策略/交易日关闭态；仅
  11:20:00 精确边界可封口 11:19:59 及此前的 current，首次调用晚于边界即清除草稿并保持
  `missed_freeze/not_ready`。正式提交失败只重试同一 sealed version，仓储读取失败也先关闭
  发布门，模型即使伪报边界前完成时间也不能在实际返回越过 11:20 后升级 hybrid。

- 修正单一辅助 runtime 生命周期无法同时拥有 Today 与 Tomorrow、第二个 runtime 启动异常
  会遗留第一个线程的问题；组合根现在显式启动/关闭两个独立 runtime，共享一个关闭 deadline，
  部分启动失败按逆序回收。已有正式 Today 只接受父版本、交易日和入选代码匹配的报价 overlay，
  不修改名单、分数、风险、动作或排名；overlay 适配器异常仅记录降级，不阻塞其它策略。

- 修正 Tomorrow hybrid 必须等待 v1 baseline、正式冻结仍依赖 shadow/cutover 身份的问题：
  hybrid 现在只引用仍为 current 的 local V2 版本，14:48 后完成的模型结果拒绝，14:49:20
  检查点、14:50 原子封口、失败重试、重启恢复与 15:00 收盘恢复全部只接受统一 V2 决策。
  Review 另发现真实组合配置的 `config+strategy` 版本会被统一身份正则误拒绝，现已纳入合法
  稳定版本字符并补回归。

- 修正检查点可能保存观察池或未入选候选、正式冻结研究轨迹仍停留在盘中版本的问题：检查点
  和正式记录均先执行 official-only 投影，合法空结果照常提交；formal 决策再通过同一个
  `V2DecisionCommitted` observer 记录，current、freeze 和 trace 共享完全相同的决策版本。
  收盘恢复把规范 `official_close` 输入版本写入不可变身份，运行中保留既有 current，冷启动
  才执行一次 local-only 收盘重建，已有正式记录或待重试封口均不可覆盖。

- 最终 Review 修正两个边界竞态：14:48:00 整点完成的 review 现在与更晚结果一样拒绝；
  14:50 控制先于本轮评分封口并在评分后复查，因此边界后的 local/hybrid 无法抢先发布。
  同时，14:50 后正式记录尚未提交时，Tomorrow 当前与状态查询返回 `not_ready`，不再把盘中
  草稿误显示为正式结果；合法 formal 提交后才恢复 `ready`。

- 修正独立 V2 调度若复用共享 worker 可能被 today 或其它策略阻塞 tomorrow 的容量风险：
  四个策略现各有一个运行项和一个 latest-wins pending 槽，tomorrow 从数据到发布拥有完整
  独立 lane；同日冻结和结算控制键成功后有界记忆并去重，失败保留可重试语义。修正初稿直接
  读取系统时间及停止后仍尝试控制提交的问题，改为显式注入 `Clock`、模型调用前复核 deadline，
  且关闭门生效后不再触碰日历、数据或控制端口。完整测试另发现两个正式记录仓储实例同时
  创建同一不可变文件的 TOCTOU 竞态；hard-link 竞争失败后现复核目标 SHA-256，同内容保持
  幂等并 fsync 目录，异内容继续冲突。

- 修正此前仅删除远端 worker 引用、本地 `codex/v2-g1-e1`、`codex/score-g1-r2` 与对应
  `/tmp` worktree 仍保留的清理不完整状态。统一 CAS 现拒绝跨交易日 hybrid 父版本、旧交易日、
  旧 sequence、同 sequence 异内容和错误 expected version；报价 overlay 同步拒绝错误父身份、
  未来报价、迟到内容、越界代码和跨策略污染。
- 修正正式记录可能在 SQLite staged manifest 与不可变 JSON 之间中断的问题：manifest 保存
  有界恢复载荷，恢复时校验规范 SHA-256 与完整业务身份，补齐合法半提交，并把缺失、损坏、
  哈希不符或身份不符的已提交文件移入隔离目录后 fail closed。同键同内容并发提交保持幂等，
  同键异内容明确冲突且不可覆盖。

- 修正可能把“worker tip 已被 feature 包含”误判为“整份实施计划已完成”的状态歧义；总计划
  在该审计批次推进到 V2-E2，本批完成 E2 后已继续推进到 V2-E3；下一研究章节仍为 Score-R2。
  后续会话必须从最新 feature tip 公布统一 `BASE_SHA`，避免从已退役 G1 分支继续施工。

- 修正两个并行 worker 基于共同基线开发后形成两个同名但方法不同的 `DataPlaneReadPort`
  协议风险；研究侧现使用明确命名的历史扩展并继承 E1 规范端口，保持唯一数据平面边界。
  同时修正完整字段请求代码校验会接受乱序输入的问题，现严格拒绝非排序或重复请求，避免
  相同代码集合因调用顺序形成不稳定身份。

- 修正交易日历持久化只保存增量游标和当批计数、重启后无法恢复实际 session 集合的问题；
  当前按日期累计保存开放状态、交易所和前交易日，恢复后重新注入参考数据网关，失败/空批次
  不推进游标。修正历史缓存只落原始 bars、无法恢复紧凑长窗口摘要的问题；最新 bar 现在携带
  经 JSON 冻结的 `HistoryContext`，恢复时先校验摘要，缺失或损坏才从可用原始窗口重算。
- 修正嵌套不可变 JSON 载荷写入 SQLite 时仍含 `MappingProxyType` 而不可序列化的问题；持久化
  边界现在递归 thaw 映射与序列。补充价格-only 更新不得清空名称等更丰富字段的回归，并将
  CNInfo 能力基线从过期的“完全未接入”改为仅准入离线风险登记簿的真实现状。
- 修正旧观察、同时间冲突、无效空事实可能覆盖最近有效低频数据的问题；正式记录处于 staged
  状态时允许同内容重试闭合，已提交同内容不重复改写，损坏或冲突内容继续隔离。

- 修正两份活动计划各自维护阶段状态、共享文件边界和执行顺序，导致多会话可能在组合根、
  权威文档、Changelog 与跨 lane 接口上并发冲突的问题。总计划现在规定每波统一
  `BASE_SHA`、独立 worktree/runtime、Gate 独占文件、接口哈希与临时组合树验证；文本冲突
  视为所有权失败并退回责任会话，不由集成会话临场拼接业务代码。

- 修正提前存在的 P1 草稿仍同步写轨迹、读取不存在的 `FeatureSnapshot.industry`、允许迟到
  内容覆盖新轨迹、哈希混入写入时钟、重复保存 native/baseline 全量输入且把研究计数加入
  正式状态 API 的问题。当前每个 `input_version` 只保留一条不可变配对轨迹；相同内容重放
  为 `duplicate`，不同内容为 `conflict` 且不覆盖，研究构建与写入整体进入生命周期拥有的
  有界 executor，并在正式发布、P6 比较和冻结之后提交。队列拒绝、载荷/总容量超限与 worker
  异常只形成脱敏研究状态，不能阻塞本地推荐。

- 修正权威设计与迁移计划仍要求影子比较、生产指针和旧 API 保留的冲突；这些内容现在只
  作为待删除现状，不再授权新 release 读取旧数据库、旧快照、旧 schema 或旧 Web。

- 修正 `docs/score.md` 仍标记 P0 整体“待执行”，以及软件设计文档仍称 60 日与数据裁剪
  “待由 P0 同步”的状态冲突；P0 现明确标记完成、P1 待执行，两份权威文档保持一致。
- 明确研究证据只允许保存硬过滤通过总体的逐股身份；硬拒绝侧只保留按日期、板块和原因
  聚合的数量、版本与哈希，禁止保存代码、简称、逐股事实、分数或未来收益。

### Removed

- 删除启动脚本的旧 `HOST`/`PORT` 兼容映射；删除 `trader-cli` 的 v17 迁移、推荐归档和
  tomorrow cutover evidence 操作及其直接入口测试。旧 Pipeline、旧仓储、旧 Web 源文件和 shadow
  运行实现仍是 V2-E10 的明确物理清理范围，本批不宣称已删除。

- 取消旧 `/api/status`、`/api/recommendations/*`、`/api/recommendation-dates`、
  `/api/events/stream`、`/v2/tomorrow` 和 `/api/v2/tomorrow/*` 的路由注册；根页面不再加载
  `dashboard_patches.js`、旧拆分渲染脚本或独立 Tomorrow CSS/JavaScript。旧源文件保留为
  V2-E10 的物理清理对象，但已不可从活动 Flask 产品路由到达。

- 从权威文档移除已闭合的影子/cutover 章节、版本事故时间线、旧 P1-P6 公共接缝施工细节、
  旧 API/envelope 兼容说明和重复迁移决策，避免其与 V2 最终契约形成第二套定义。

- 删除旧 `application/long_quotes.py` 及其 legacy snapshot 单元测试；从 Pipeline 移除
  `trader-long-quotes` executor、旧 latest-request lane、Long P6 admission、RuntimeState 发布、
  SnapshotPublisher 推送和 `long_quote_snapshots_published` 路径。Long 不写正式记录、推荐历史、
  结算或评分 committed event；其它旧生产链仍按 V2-E8 至 V2-E10 分节处理。

- 从旧 Pipeline 的正式评分提交、P6 冻结和盘后旧链恢复集合移除 D25；迁移期 Pipeline 只形成
  同批点时 `D25NativeInput`。旧实现文件仍按 V2-E10 保留，本批不提前删除 Long、旧 API、
  shadow/cutover 或 Web 资源。

- 从生产旧 Pipeline 的正式评分、P6 冻结和盘后恢复集合移除 Today；移除 Today 对旧
  RecommendationSnapshot、启动检查点和 `close_fallback` 的正式决策依赖。迁移期旧实现文件
  仍按 V2-E10 计划保留，本批未越界删除 D25、Long、旧 API 或 Web 资源。

- 从生产组合根移除 Tomorrow 的 `CurrentDecisionIndex`、`TomorrowShadowWorker`、baseline
  snapshot wrapper、cutover gate、shadow evidence、旧冻结仓储和研究 baseline 捕获依赖；
  旧实现文件仅作为 E10 待删除迁移代码保留，不再被活动组合根读取或写入。

- 移除 V2-E3 新链路对隐式系统时钟、无界 observer 回调和跨策略共享决策排队的依赖；被更新
  pending 覆盖的旧周期及关闭时尚未运行的普通 pending 会明确丢弃并计数。本批未删除旧
  Pipeline 或生产资源，旧链删除仍严格属于 V2-E10。

- 删除本地 `codex/score-g1-r2`、`codex/v2-g1-e1` 分支及其干净 worktree；结合此前已完成的
  远端删除，这两个施工分支引用现已在本地和 origin 全部清理。分支中的有效提交继续由
  `feature/tomorrow-v2` 历史可达，未删除提交或改写历史。

- 删除远端 `codex/score-g1-r2` 与 `codex/v2-g1-e1` 分支引用；两个 tip 及其全部提交仍由
  `origin/feature/tomorrow-v2` 的合并历史可达，不删除提交、不改写历史，也未删除本地
  worktree 或本地分支。

- 移除应用层旧协议名 `RealtimeDataPlaneReaderPort`，统一使用契约规定的
  `DataPlaneReadPort`；未移除或接管旧生产 Pipeline、Web、冻结或评分链路，这些仍按后续
  V2-E2 至 V2-E10 的独立章节执行。

- 删除已被总计划完整吸收的 `docs/V2_plan.md` 与 `docs/score.md` 活动入口，并同步产品概览、
  权威设计、阶段报告和契约测试引用；历史 Changelog 仍保留原文件名作为当时交付证据。

- 移除 P1 草稿对 native 与 baseline 两份完整候选输入的重复留存，以及正式 shadow 状态中的
  `research_trace_*` 字段；研究状态只经独立端口读取，不改变普通 API 内容。

- 移除 V2 最终产品对旧 URL、重定向、双读、双写、旧格式回放、cutover gate 和兼容窗口的
  契约要求；本批尚未删除运行代码，代码删除固定由 V2-10 独立完成。

- 移除旧的“不少于 250 个评价交易日”要求和 P0 尚待同步的过期表述；替换为最多 40 个
  历史有效日加固定 20 日前向窗口，且失败日不得被其它盈利日期替换。

### Verification

- V2-E9 定向契约覆盖默认 `.runtime/v2`、旧 CLI 命令拒绝、启动脚本环境变量边界、server lock
  以配置 runtime 为根和权威文档计划状态；相关 contract、component、unit 和全量 pytest 全部通过。
  `make format-check`、`make lint`、`make type-check`、`make test`、`make package` 全部通过；
  仓库外 wheel 导入、CLI help、HTML/CSS/JavaScript/SVG 资源和根页面 smoke 通过。E9 不改变页面
  布局，三档桌面视觉验收属于 E11，当前不适用。

- 本批只修改协作流程、交付记录和对应契约测试；新增流程契约 pytest 通过，测试文件的 Ruff
  format/check 通过，`AGENTS.md` 完整 diff、章节引用、分级覆盖与 `git diff --check` 无发现。
  不改变生产 Python、依赖、构建、运行时、API 或 Web，因此全量 pytest、mypy、package、
  wheel 和浏览器验收均不适用。

- V2-E8 定向回归覆盖四策略统一 shape、Long 无历史、正式日期、ETag/304、错误 strategy/date、
  旧 URL 404、HTTP 无外部 I/O、SSE 跨策略单调序列、游标超前/过期、显式 identity resync、
  慢客户端隔离、组合根及 Today/Long 事件接缝，全部通过；`make format-check`、`make lint`、
  `make type-check` 和 `make package` 通过。仓库外 wheel 导入、HTML/CSS/JavaScript 资源读取及
  `trader-cli --help` 通过；Chrome 1280x720、1440x900、1920x1080 验收无白屏、横向溢出、
  顺序错误或浏览器错误，且外部网络请求为零。`make test` 全量运行因持续超过 17 分钟被中止，
  未记录为通过；本批以直接覆盖 E8 的 unit、contract、component 和 performance 回归作为证据。

- 权威文档及适用契约测试共 162 项通过；`make format-check`、`make lint`、`make type-check`
  和 `make package` 通过，Ruff 严格复杂度债务为零，mypy 检查 256 个源码文件。最终 wheel
  从仓库外目标目录导入，`trader-cli --help`、`validate-config`、`pip check` 及 HTML、CSS、
  JavaScript、SVG 资源读取通过。本批不改变 Web 或布局，三档桌面视觉验收不适用。

- V2-E7 定向领域、运行时、统一索引、Pipeline 集成和组合根回归覆盖完整固定顺序、分组唯一、
  全字段 current、部分失败、同日 retained、missing 占位、未来/未知报价拒绝、整体行情失败、
  收盘 current、零正式记录、慢 Long 不阻塞短线和 `create_app()` 无线程副作用。
  `make format-check`、`make lint`、`make type-check`、4 worker 并行 `make test` 和 `make package`
  全部通过；Ruff 严格复杂度债务为零，mypy 检查 256 个源码文件，完整 pytest 到达 100%，仅保留
  10 条既有未知 DeepSeek fixture 模型警告。最终 wheel 从仓库外 `/tmp` 目标目录安装并导入，
  `trader-cli --help`、模板、CSS、JavaScript 和 SVG 资源均可读取。Chrome + CDP 对统一根页面、
  Long 和 Tomorrow V2 完成 1280x720、1440x900、1920x1080 实机验收，均无白屏、页面级横向
  溢出或浏览器错误；Long 37 个分组无内部溢出，主看板 24 个 patch 全部应用、零 resync，
  patch-to-paint P95 为 11ms（门槛 100ms），浏览器 fixture 外部网络请求为 0。

- V2-E6 定向回归覆盖 D25 原生输入、专属 local/hybrid、父 CAS、14:49:20 检查点、14:50
  热/冷恢复、合法正式空结果、Tomorrow/D25 同日隔离、待重试封口、15:00 冷启动 local-only
  fallback、零 DeepSeek review、统一 current/history 查询及组合根接线。`make format-check`、
  `make lint`、`make type-check`、`make test` 和 `make package` 全部通过；Ruff 严格复杂度债务为
  零，mypy 检查 255 个源码文件，完整 pytest 到达 100%，仅保留 10 条既有未知 DeepSeek
  fixture 模型警告。最终 wheel 从仓库外 `/tmp` 目标目录安装并导入，`trader-cli` console
  entry point、模板、CSS、JavaScript 和 SVG 资源均可读取。宿主 Firefox 实际未安装且无
  geckodriver，改用本机 Chrome 123 + CDP 对统一根页面和 Tomorrow V2 页面完成 1280x720、
  1440x900、1920x1080 实机验收，六个视口均无白屏和页面级横向溢出；Tomorrow 面板顺序、
  分区间隔、4 行数据和浏览器零错误同时通过。

- V2-E5 回归覆盖 11:19:59/11:20:00、首次晚于边界、边界后冷启动、仓储读取/写入失败、
  同 sealed version 重试、实际模型返回越界、local/hybrid 父 CAS、观察阶段无可执行动作、
  正式记录 overlay 作用域、双 runtime 启动回滚和 Today/Tomorrow 生产接线。首次完整
  `make test` 已到达 100%；最终格式、Ruff、mypy、package、仓库外 wheel 和三档桌面验收见
  本批提交前最终门禁记录：
  - `make format-check` / `make lint` / `make type-check` / `make test` / `make package` 全部通过；`make
    package` 中 `ruff` 与 `mypy` 均为零复杂度债务。
  - 外部 wheel 验收：仓库外独立 venv 从 wheel 安装成功，`trader-cli --help`、`validate-config`、模板/CSS/JavaScript/SVG 均可读取。
  - 桌面验收：`run_t1_browser.py` 与 `run_tomorrow_v2_browser.py` 均生成 `passed: true` 报告，分辨率 1280x720、1440x900、1920x1080 全部 `body` 与 `overflow` 通过，主看板 patch-to-paint p95 为 30ms（P95 阈值 100ms），网络请求为 0，`tomorrow` 覆盖行数为 4。

- V2-E4 定向测试覆盖 native local/hybrid 父 CAS、证据 manifest、14:48 迟到拒绝、统一
  formal event、14:49:20 检查点、14:50 热/冷冻结、幂等重试、冻结后不可覆盖、收盘恢复、
  合法正式空结果、检查点消费及损坏 fail-closed。契约、unit、component、integration 扩展
  回归均通过。`make format-check`、`make lint`（严格复杂度债务为零）、`make type-check`、
  `make test` 和 `make package` 全部通过；pytest 到达 100%，仅保留既有 10 条 fixture 模型
  名警告和 2 条 Python 3.14 SQLite datetime adapter 弃用警告。仓库外 `/tmp` 安装 wheel 后，
  `trader` 从目标 `site-packages` 导入，`trader-cli --help` 可执行，模板、CSS、JavaScript 和
  SVG 均可读取。Firefox 对 Tomorrow 页面和统一根页面分别完成 1280x720、1440x900、
  1920x1080 验收，均无白屏、横向溢出、面板重叠或浏览器错误；根页面 patch-to-paint P95
  为 42ms（门槛 100ms），浏览器 fixture 外部网络请求为 0。

- V2-E3 定向契约、latest-wins 生命周期、异步 observer、统一决策核心、调度、worker、优雅
  关闭和停用旧 Pipeline fixture 回归通过，覆盖 tomorrow 独立进展、共享 DeepSeek 168 硬
  上限契约、local/hybrid 发布、迟到模型跳过、数据/模型失败保留 local、冻结/结算去重、同一
  deadline 超时与无线程残留。配置 Ruff 通过，严格重构债务为零，mypy 检查 246 个源码文件；
  `make format-check`、`make lint`、`make type-check`、`make test` 和 `make package` 全部通过，
  完整 1,244 项 pytest 仅有既存 10 条未知 DeepSeek 测试模型告警与 2 条 SQLite adapter 弃用告警；
  并发正式记录回归额外连续运行 50 次通过。最终 wheel 在仓库外目标导入 5 个新增模块，
  8 项关键模板/静态资源、`trader-cli --help` 和 `pip check` 通过。真实 Firefox 在主看板、
  long 与 tomorrow V2 的 1280x720、1440x900、1920x1080 均无白屏、页面级横向溢出或浏览器
  错误；主看板 25 个 patch 全部应用、零 resync/外部网络，patch-to-paint P95 为 77ms，
  tomorrow overlay 更新未触发额外完整 GET。

- V2-E2 领域、应用、持久化和契约定向测试通过，覆盖 today/tomorrow/d25 统一评分身份、long
  无评分投影、并发 CAS 单胜者、迟到与跨日父版本拒绝、overlay 隔离、同键幂等/冲突、半提交
  恢复、损坏隔离和通用无 research 依赖事件。`make format-check`、`make lint`、`make type-check`、
  `make test` 和 `make package` 全部通过；严格复杂度债务为零，mypy 检查 242 个源码文件，完整
  pytest 仅保留 10 条既有未知 DeepSeek 测试模型告警和 2 条 SQLite adapter 弃用告警。最终
  wheel 从仓库外目标导入新增领域、应用与仓储模块，`trader-cli --help`、9 项模板/静态资源和
  `pip check` 通过。Firefox 在 1280x720、1440x900、1920x1080 对主看板、long 和 tomorrow V2
  均无白屏、页面级横向溢出或浏览器错误；主看板 25 个 patch 全部应用、零 resync/外部网络，
  patch-to-paint P95 为 79ms，tomorrow overlay 更新没有触发额外完整 GET。

- 刷新 origin 后确认 `origin/feature/tomorrow-v2` 为
  `200580272768a220c411814f39be21a04c93e4f9`；两个 worker tip 对 feature 的未包含提交数均为
  0，且均列入 `git branch -r --merged origin/feature/tomorrow-v2`。删除后再次刷新并查询远端
  heads，确认两个 worker ref 不再存在、feature tip 不变。`make format-check`、`make lint`、
  `make type-check`、`make test` 和 `make package` 全部通过；严格复杂度债务为零，mypy 检查
  237 个源码文件，完整 pytest 仅保留 10 条既有未知 DeepSeek 测试模型告警和 2 条 Python
  SQLite adapter 弃用告警。最终 wheel 从仓库外环境导入，`trader-cli --help`、绝对配置
  `validate-config`、9 项模板/静态资源和 `pip check` 通过。

- Gate G1 合并后的研究领域、两阶段历史端口、唯一数据平面定义、点时截止、三板覆盖、共享
  复权窗口、架构分区和总计划契约定向回归通过。最终 `make format-check`、`make lint`、
  `make type-check`、`make test` 和 `make package` 全部通过；严格复杂度债务为零，mypy 检查
  237 个源码文件，完整 pytest 仅保留 10 条既有未知 DeepSeek 测试模型告警和 2 条 Python
  SQLite adapter 弃用告警。最终 wheel 从仓库外环境导入新增 application/domain research
  模块，`trader-cli --help`、绝对配置 `validate-config`、9 项模板/静态资源和 `pip check` 通过。

- V2-E1 契约、epoch 领域、原子数据平面、tomorrow 只读用例、字段合并、SQLite 数据平面及
  全量市场数据组件定向回归共 205 项通过；覆盖父版本一致性、字段血缘匹配、100%/99% 覆盖
  门禁、无效空拒绝、失败保留、累计日历恢复、20 根原始 bars 加 60 日紧凑摘要恢复，以及
  未准入来源隔离。最终 `make format-check`、`make lint`、`make type-check`、`make test` 和
  `make package` 全部通过；严格复杂度债务为零，mypy 检查 231 个源码文件，完整 pytest 仅
  保留 10 条既有未知 DeepSeek 测试模型告警和 2 条 Python SQLite adapter 弃用告警。
- 最终 wheel 从仓库外 `/tmp` 目标目录导入，新增日历状态模块来源确认在安装目录；
  `trader-cli --help`、绝对路径 `validate-config`、9 项模板/CSS/JavaScript/SVG 资源和
  `pip check` 通过。真实 Firefox/geckodriver 在 1280x720、1440x900、1920x1080 对旧看板、
  long 和 tomorrow V2 均无白屏、页面级横向溢出或浏览器错误；tomorrow 三档截图有效，旧
  看板 25 个 patch 全部应用、零 resync/外部网络调用，patch-to-paint P95 为 30ms。

- 总计划合并批次运行计划/项目记录/V2-only/来源准入契约测试，检查活动文档无旧计划引用，
  并运行完整 format、Ruff、mypy、pytest、package 及仓库外 wheel 资源/CLI 验收。

- P1 定向契约、领域、应用、组合根、架构和影子集成回归已通过；覆盖硬拒绝代码/简称不进入
  载荷、全体硬过滤通过股票候选审计、同输入配对、DeepSeek 请求增量 0、稳定 SHA-256、
  duplicate/conflict、单载荷/记录/总字节上限、队列拒绝、worker 失败、本地推荐不阻塞、
  正式状态 API 无新增字段、`build_system()` 无线程/文件副作用及源文件行数门禁。最终
  `make format-check`、`make lint`、`make type-check`、`make test` 和 `make package` 全部通过；
  严格 Ruff 复杂度债务为零、mypy 检查 230 个源码文件，完整 pytest 仅保留 10 条既有未知
  测试模型名告警。仓库外安装最终 wheel 后，新增研究模块从隔离目标导入，`trader-cli
  --help`、绝对配置 `validate-config`、9 项模板/CSS/JavaScript/SVG 资源和 `pip check` 通过。
  离线 Headless Chrome 在 1280x720、1440x900、1920x1080 的短线与 long 视图均有正文、无
  页面级横向溢出或浏览器错误；24 个 patch 全部应用、零 resync/外部网络调用，patch-to-paint
  P95 为 11.7ms，低于 100ms 预算。

- 新增 `tests/contract/test_v2_only_product_contract.py`，锁定 V2 唯一运行目录、唯一 API、
  V2 原生决策身份、无旧数据读取和无双链切换计划；同步更新项目记录契约的计划状态断言。

- 新增 `tests/contract/test_score_plan_contract.py` 并调整项目记录契约，锁定 P0 状态、固定窗口、
  统计常量、五个挑战者、研究隐私边界和晋级门禁。基于上一已推送提交加本批 P0 文件的
  隔离副本，`make format-check`、`make lint`、`make type-check`、`make test` 和 `make package`
  全部通过；完整测试仅有既存未知测试模型告警，无失败。
- 在仓库外从最终 wheel 目标安装后，验证 `trader` 确实从安装目标导入、`trader-cli --help`、
  `validate-config`、10 项模板/CSS/JavaScript/SVG 资源和 `pip check` 全部通过。无头 Chrome
  在 1280x720、1440x900、1920x1080 的短线与 long 布局均非白屏、无页面级横向溢出、关键
  区域顺序正确且 `browserErrors=[]`。

- 新增策略文档契约测试，固定覆盖率/上界公式、active-set、五挑战者、同日同股配对、非循环
  区块抽样、固定 p 值修正、研究状态、前向日期、R6 新窗口和人工晋级关键语义；本批仅改变
  文档与契约测试，不执行历史回放，也不声明已有收益改善。`make format-check`、`make lint`、
  `make type-check`、`make test` 和 `make package` 全部通过；完整 pytest 到达 100%，只有 10 条
  既有未知 DeepSeek fixture 模型警告。仓库外 wheel 已验证从安装目录导入包、执行
  `trader-cli --help` 并读取 HTML、CSS、JavaScript 和 SVG 资源；本批无 Web 或布局行为变化，
  三档桌面视觉验收不适用。

### Residual Risks

- E9 只关闭旧入口和旧操作命令；旧 Pipeline、旧 snapshot/仓储、旧 Web 资源及 shadow 实现仍待
  E10 物理删除。E11 仍需完成全量测试、wheel、真实进程和桌面验收。

- 风险等级依赖对实际 diff 和依赖传播的审查；若定向验证无法证明影响范围，规则要求立即升级
  门禁。该分级不会降低最终 release 或评分、冻结、预算、持久化等高风险边界的完整验收要求。

- V2-E8 已闭合统一 Web 产品面，但 V2-only 总计划仍需完成 E9 唯一入口与 `.runtime/v2`、E10
  旧生产链和未注册 Web 源文件物理删除、E11 最终发布验收。固定离线 fixture 只证明读取、事件、
  布局和降级契约，不构成真实交易日外部来源连续性、DeepSeek 尾延迟或收益改善证据。

- V2-E8 的全仓 pytest 未在本批取得完成结果；此前运行超过 17 分钟后按用户要求停止长时间等待。
  E11 最终发布前仍必须完成全量测试，并应先定位或拆分拖慢全仓反馈的测试组，避免再次无界等待。

- 本批只清理权威契约，不提前实现 V2-E8 至 E11，也不改变生产代码、运行配置或活动评分。
  `continuous_entry` 的过渡宽度、`heat_weak_structure` 的准确阈值及研究零分母等机器契约仍须
  在 Score-R4/R5 对应独立批次中预注册；当前不得据此运行收益比较或晋级。
  4 worker 全量 pytest 到达 93% 以上且未见失败后按用户“直接 push”指令终止，因此不计为
  完整全量测试通过证据；后续生产实现批次仍须重新执行完整门禁。

- V2-E7 已闭合，但 V2-only 总计划仍需完成 E8 统一 API/SSE/Web、E9 唯一入口、E10 旧链删除
  和 E11 发布验收；Long current 当前只进入统一索引，统一公开路由和页面消费属于 E8。
  固定 fixture 证明身份、冻结与恢复状态机，不构成真实交易日外部行情连续性、DeepSeek 尾
  延迟或收益改善证据；外部失败继续按权威契约显式降级。

- V2-E8 至 V2-E11 与 Score 后续章节仍未完成；Today/D25/Long 的统一公开 API、SSE 和根页面
  按计划属于 V2-E8。固定输入证明状态机
  与身份正确，不构成真实交易日行情覆盖、外部 DeepSeek 尾延迟或荐股收益改善证据；这些仍需
  后续真实运行观测，且任何外部失败继续按契约显式降级。

- V2-only 总计划尚未完成：统一根 API/SSE/Web、最终组合根与旧链删除仍属
  E8-E10，最终发布验收属 E11；当前兼容 Tomorrow 页面从统一身份投影有限字段，完整统一
  工作台与报价 overlay 仍由 E8 接管。旧 shadow/冻结源文件尚未物理删除，但生产组合根已无
  可达依赖。免费外部来源在真实 14:50/15:00 边界的连续运行证据仍需 E11 留档，本批测试不
  构成收益保证。

- V2-E3 的通用调度基础仍保持旁路，但 E4 已用专属原生 runtime 接管 Tomorrow 组合根；
  V2-only 总计划尚未完成。下一次“继续”只应执行 V2-E5 Today 正式接管，后续仍有
  E6-E11、Score-R1-Migrate 和 Score-R2 至 R7。真实交易日外部来源延迟、模型
  限流及冻结时点仍需在对应接管和最终发布章节留证，本批固定 fixture 不构成收益证明。

- V2-E2 决策核心已由 E4 的 Tomorrow 路径接入，V2-E3 通用调度仍为其余策略的旁路基础。
  总计划后续仍需逐批完成 V2-E5 至 E11、
  Score-R1-Migrate 与 Score-R2 至 R7；下一次“继续”按规则只执行下一完整未完成章节，不得
  把当前基础设施完成误解为 V2-only 总迁移已发布。

- 删除 G1 worker refs 及完成 V2-E2/E3/E4 仍不代表总计划完成。Score-R2 最多 40 日提取器及
  后续 V2-E5 至 E11 仍未交付；旧 Pipeline、`.runtime/v17`、独立
  tomorrow 页面、shadow/cutover 及旧 CLI 当前仍存在，必须按后续章节逐批替换和删除。

- Gate G1 只完成 E1 数据平面与 Score-R2 接口适配设计；最多 40 日点时提取、Top120 乐观
  上界保护、Polars 不可变分区和可复算 manifest 仍按 G2 的 Score-R2 整节实施。本批没有
  生产接线、外部行情请求、DeepSeek 请求或收益提升结论。

- V2-E1 数据平面与 V2-E2 决策核心已由 E4 接入 Tomorrow，V2-E3 通用调度对其余策略仍为
  旁路基础；Today、D25 与 Long 正式接管属于 V2-E5 至 E7。交易所、mootdx 和 BaoStock
  仍未准入；CNInfo 只允许离线风险登记簿。120 日/20GB 长期压缩审计仍是文档约束，尚未实现
  归档与容量驱逐，因此本批不宣称 V2-only 发布完成。

- 本批只统一施工治理，不实现 V2-E1 或 Score-R2，也不改变生产运行；计划能减少文件级冲突，
  但语义冲突仍需协调会话按冻结接口、权威契约和临时组合树门禁拒绝或退回。历史报告中的
  P0/P1 名称作为已发生批次标签保留，并映射到当前 V2-E 章节。

- P1 只提供进程内、64 MiB 总上限的紧凑研究证据；P2 的最多 40 日点时持久化、乐观上界、
  上界保护集合和只读导出尚未实现，进程停止后不保留本批内存轨迹。P4 五个挑战者同样尚未
  实现；当前 `research_shadow` 只复用本输入已有合法 facts，没有 facts 时保存 control 副本，
  因此本批不宣称收益改善或策略晋级。

- 本批只完成 V2-0 契约重置，活动代码仍包含旧 Pipeline、旧快照、旧 Web 和 shadow/cutover
  实现；它们必须按 V2-1 至 V2-10 逐项替代并删除，当前 release 还不是 V2-only。

- 评分收益研究仍处于非生产阶段；P0 只消除了预注册歧义，没有实现 P1 决策轨迹、P2 历史
  点时数据或后续回放/统计，因此没有收益提升结论。固定前向窗口尚未发生，能否取得 20 个
  连续有效日取决于届时数据与运行连续性；任一门禁不足时继续使用当前生产策略。

- 详细策略现已冻结，但挑战者、历史点时提取、召回审计、统计引擎和前向 collector 仍需按
  Score-R1-Migrate 至 R5 分节实现。历史主窗口的真实点时证据覆盖仍待 Score-R2 验证；固定
  前向窗口须在 2026-11-27 结束且最后标签完成结算后才能形成晋级证据，不能用回填或模拟替代。

### Added

- 新增 `TomorrowV2Runtime`、原生统一投影、`TomorrowV2FreezeCoordinator`、可校验且可消费的
  `V2DecisionCheckpoint`、统一 Tomorrow 只读投影与有界 committed-event 研究轨迹。正式记录
  仓储新增隔离 checkpoint manifest/不可变 JSON，统一索引新增按策略原子 seal、formal commit
  和 restore；所有路径保持可注入上海时钟、latest-wins 生命周期与只读 Web 无外部 I/O。

- 新增应用层 `V2SchedulerRuntime`、类型化 V2 数据/决策/模型/冻结/结算端口、共享 DeepSeek
  运行契约、每策略 `LatestWinsWorker` 和有界 `AsyncDecisionObserver`。后台资源均显式
  start/close/stop/wait/status，冻结拥有独立紧急控制容量，所有停止步骤共享调用方提供的
  `ShutdownDeadline`，consumer 与受控外部失败只形成脱敏状态且不反向修改当前决策。

- 新增纯领域 `ScoredDecision`、`LongProjection`、匹配身份的报价 overlay 和正式记录模型；
  规范载荷稳定派生 SHA-256 与版本，评分身份覆盖 today/tomorrow/d25，long 类型在结构上不含
  评分字段。新增应用层 `UnifiedDecisionIndex` expected-version CAS 和通用
  `V2DecisionCommitted`，事件完整携带决策版本、输入版本、过滤聚合、降级原因及逐项结果，
  且不依赖 research 类型。
- 新增正式记录端口与隔离的 `v2-decisions` SQLite/不可变 JSON 仓储，按策略和交易日唯一提交，
  支持并发幂等、类型化冲突/不可用错误、启动恢复、损坏隔离与孤儿文件清理。本能力保持旁路，
  等待后续策略迁移章节通过唯一组合根装配。

- 新增 G2 基线发布约束：在 G2 开始时读取本批审计记录推送后的最新 feature tip 并公布精确
  SHA，新 worker 必须从该提交创建，防止已删除远端引用后出现隐式旧基线或重复集成。

- 新增 Score-R2 研究专用不可变接口值：逐字段来源与内容哈希、候选/最终组件、日摘要、
  硬拒绝聚合、三板点时覆盖、完整候选字段、日线/分钟、共享复权因子窗口及成本结算证据；
  边界拒绝未来输入、非上海时区、覆盖缺口、代码错配和同键异内容。架构契约同步登记
  `domain/research` 为独立纯领域能力包，继续禁止已退役的扁平旧研究模块。

- 新增 `DataPlaneCoverage`、逐字段 `FieldValue` epoch 血缘门禁和 V2-E1 反向契约测试；新增
  实际交易日历集合与紧凑历史摘要的持久化/恢复回归，使同一只读快照能够证明数据身份、覆盖
  和恢复来源，而不是仅依赖游标、行数或供应商对象。
- 新增交易日历 recent/formal SQLite 表、迁移、恢复和类型化读写端口；候选 epoch 记录本轮
  `requested_codes`，使空响应和 99% 核心历史覆盖可按实际请求总体审计。

- 新增 `docs/implementation-plan.md`，作为唯一活动施工计划，完整定义 V2-only 迁移、评分研究、
  多 Codex 并行隔离、跨 lane 事件接口、Gate、测试矩阵和逐章提交推送协议。

- 新增研究专用不可变 P1 schema、独立 `TomorrowResearchTraceRecorderPort`、有界异步记录器
  和内存存储。轨迹仅保存硬过滤拒绝的板块/原因聚合；逐股侧只包含硬过滤通过总体的候选审计、
  已评分集合的本地组件/风险/下行保护/动作/排名、同输入 `production_local` 与
  `research_shadow` 决策，以及配置、规则、策略、融合、schema、引擎和规范哈希身份。

- 新增评分研究 P0 反向契约测试，防止后续漂移固定日期、随机身份、Holm 检验族、硬过滤后
  研究总体、五个挑战者或将研究证据误写入生产链。

- 新增 V2-only 产品契约测试，防止后续重新引入旧 API 保留期、旧运行数据读取、旧 schema
  回放或运行时生产指针。

- 用户要求修复荐股数据扫描发现的问题。本批为历史预热状态新增批次超时累计数、当前在途
  年龄和生产批次截止秒数，使 30 只批次是否卡住、何时释放可从状态 API 直接判断。

- 用户要求继续 `V2_plan.md` 未完成任务。本批完成 `P8：tomorrow 独立生产运行时` 的计划收口：
  `TomorrowShadowRuntime`、`TomorrowShadowWorker`、`TomorrowFreezeCoordinator`、`CurrentDecisionIndex`
  和 `ShadowObservingSnapshotIndex` 的独立运行时边界已由现有回归证明；native input 直接从同一
  规范输入构造，不回读当前生产 tomorrow snapshot；固定融合向量 `83.40` 与 `local_score`
  不重复扣本地风险的契约继续保持不变。`docs/V2_plan.md` 的 P8 状态已改为已完成，并同步修正
  `tests/contract/test_project_records.py` 的章节计数断言。

- 用户要求继续 `V2_plan.md` 未完成任务。本批完成 `P7：实时路由归一化和 mootdx 影子准入`：
  生产路由保持东方财富先发、1 秒后新浪对冲、腾讯定向候选报价的既有边界；新增回归锁定
  字段级报价合并、来源别名归一化、`mootdx_shadow` 不写生产实时字段，以及物理失败/timeout/
  熔断跳过/source lane 淘汰的分离健康计数。腾讯价格-only 响应只能更新允许字段，不能整行清空
  名称、上一收盘、证券身份或风险标记；通达信/mootdx 在权威准入前仍不进入组合根、生产路由、
  评分、冻结或 Web 查询路径。

- 本批完成 `P5：历史特征仓库和 BaoStock 校验` 章节收口：`HistoryCache` 增加
  `history_data_plane` 注入与恢复方法 `recover_from_data_plane()`，重放 61 日完整样本窗口并修复
  20 日内存保留上下文；`ReferenceLoader` 在刷新后持久化 `security_master` 与交易日历游标。

- 用户要求继续 `V2_plan.md` 未完成任务。本批完成 `P6：公司风险登记簿和 CNInfo 增量链`：
  `ResearchLoader` 增加风险组件持久化与恢复，`bootstrap_data_plane` 恢复链路加入研究恢复分支；
  新增 CNInfo 离线增量登记簿模块，按公告唯一键去重并持久化 `cninfo-announcement:*`、
  `cninfo-risk-component:*` 和 `cninfo.announcements:{code}` 游标。空增量、重复页、单组件失败或
  DataPlane 写入失败都不会清空既有风险事实；CNInfo 恢复出的公告风险事实可进入结构化
  `ResearchObservation`，但交易所公告交叉校验仍固定为 `pending`，不宣称交易所级复核完成。

- 本批 P4 交付（`docs/V2_plan.md`）完成“证券主数据和交易日历”运行闭环：新增
  `src/trader/application/ports/data_plane.py`、`src/trader/infra/persistence/data_plane_sqlite.py`、
  `src/trader/infra/persistence/data_plane.py` 与 `ReferenceLoader` 数据平面恢复与持久化接入；
  同步新增回归测试 `tests/unit/infra/test_data_plane.py`、`tests/unit/test_data_plane_migration.py`、
  `tests/component/test_v2_market_data.py`、`tests/contract/test_v2_bootstrap.py`。

- 本批继续 `V2_plan` 未完成章节：完成 P3 持久化、迁移和恢复骨架，新增
  `src/trader/application/ports/data_plane.py`、`src/trader/infra/persistence/data_plane_sqlite.py`、
  `src/trader/infra/persistence/data_plane.py`。

- P2 章开始落地字段级质量模型：新增 `src/trader/domain/market/quality.py`（字段级质量/血缘域模型）、
  `src/trader/infra/market_data/field_quality.py`（字段白名单、同源更新、跨源优先级、冲突与降级状态的
  纯函数选择器）以及回归测试 `tests/unit/test_v2_market_data_field_quality.py`。

- 新增 `docs/V2_plan.md` 的 P0 基线章节（`2026-07-30`），并新增
  `docs/reports/v2-p0-baseline.md`：记录活动树中生产链/影子链/历史资产的基线归类、
  冻结相关术语边界和下阶段迁移顺序，作为 P0“冻结现状、术语和目标契约”批次交付证据。

- 用户要求 Review `docs/V2.md` 的计划可靠性并把可执行步骤写入 `docs/V2_plan.md`。本批
  新增非生产执行计划：基于当前代码区分已删除旧包、当前生产链和 tomorrow v2 影子链，
  给出 P0-P13 的依赖图、逐批目标、文件边界、实施步骤、验收条件、统一交付流程和量化口径。

- 用户要求把 `docs/V2.md` 的数据源按新计划合并并删除重复内容。本批新增一张唯一的免费
  数据源职责表，统一记录九类来源的固定职责、主备路由、执行方式和降级边界，并集中保留
  官方参考入口、字段级合并规则、接入顺序与准入校验；同时新增“待执行、非生产契约”
  状态和两份权威文档指向，避免迁移草案被误作活动产品或策略契约。

- 用户要求把前景行业并入长期“卡脖子”页，只实时展示股票信息且不评分。本批新增
  `高端科学仪器`、`航空发动机/燃气轮机`、`新型电力系统/储能` 和
  `可控核聚变关键材料/装备` 四个固定观察分组；补充普源精电、鼎阳科技、华峰测控、
  航发控制、航发科技、国电南瑞、阳光电源、许继电气、平高电气、南网科技、安泰科技和
  永鼎股份 12 只已由正式披露支持产业链关系、且免费行情端点可识别的观察标的。

- 用户要求按既定计划修复 V2 三轮连续空结果。本批新增 CAS 前原生输入质量判定：
  分开记录全市场/显式候选数量、拒绝数、已评分数、候选临时失败、候选可选降级及结构化
  原因。`stale_quote`、历史缺失/非法和候选核心字段缺失造成的零评分固定标记为
  `transient_invalid_empty`；`/api/v2/status` 的 shadow 状态和顶层运行失败可直接看到
  最近质量结论，不再从 5533 只股票的聚合总数猜测候选是否真正进入评分。
- 新增东方财富免费全量字段 `f26` 的上市日期，以及板块、交易所低频证券主数据投影；
  东财成功后只保留低频字段作为有界内存 reference，后续实时行情降级到新浪时继续复用，
  且 Tushare/官方级参考源保持更高优先级。没有真实字段时仍留空，不推造上市日期。

- 用户要求按“公司风险历史缺失及更严重的系统问题”优化计划开始修复。本批新增公司研究
  逐股成功冷却、60/120/240/480/900 秒失败退避和全失败批次短路状态；`/api/status`
  加法暴露冷却/退避股票数、下一次重试、门控代码数、短路批次/代码数、门控容量与淘汰数，
  便于区分真实来源失败、退避跳过和正常冷却。

- 用户要求合并 V2 实时性与高频行情源失败改进计划，并明确只使用免费行情源。本批新增
  `runtime` schema v8 的新浪独立 8 秒 I/O timeout、1 秒全市场对冲延迟和免费源路由
  契约；来源状态新增物理失败、超时、熔断跳过、latest-wins 淘汰及轻量恢复探测的独立
  计数，避免把“没有发请求”和“请求失败”混为一类。
- 新增东财/新浪单轮 HTTP session 复用、截止/对冲取消检查和轻量半开探测。东财恢复只取
  首个全市场页，新浪恢复只取证券计数；完整分页仅在探测成功后执行。回归覆盖东财在来源
  worker 内仍有界并行、单轮只创建一个 session、截止后不再发页、东财快返不启动新浪、
  东财变慢后新浪首胜立即发布，以及双源失败保留最近有效快照。

- 用户要求把 `start_stop.md` 相关修改完整提交。本批新增交易 session tracker、不可变
  `FreezeAttempt`、调度点生命周期和进程级 `ShutdownDeadline`：状态 API 加法暴露
  calendar、session generation、调度点和冻结重试；联合测试覆盖冷启动时间矩阵、日历
  离线、时钟跳变、同对象重试、进程信号、队列 drain 及两套冻结仓储 kill point。

- 用户指出收益挑战者要求 250 个严格点时交易日过重，并要求只保留硬过滤后的逐股研究数据。
  本批把 `docs/score.md` 重构为非生产实施计划：评价窗口最多 60 个交易日，固定为最多
  40 日历史点时回放加 20 日连续前向影子；全市场只临时使用硬过滤最小输入，硬拒绝股票仅
  保存按日期、板块和原因聚合的数量及批次哈希；`allowed=true` 总体先只保存候选最小字段
  和可复算乐观上界，只有生产 Top120 与上界仍可能进入 TopK 的保护集合加载完整评分数据，
  在不丢失召回证明的前提下控制历史体积。计划保留四个独立变体、合并挑战者、300 条总
  配对、100 条前向配对、成本、回撤、99% 召回、分数单调性和配对 bootstrap 门禁，并明确
  60 日证据不足时继续使用当前基线。本文不自行覆盖权威策略；活动文档当前仍是 250 日
  口径，须在 P0 独立批次同步修改两份权威文档、契约测试和版本后才生效。软件业务设计和
  文档治理契约同步登记 `score.md` 的非权威身份，防止它被误作活动策略或被文档清理门禁拒绝。

- 用户明确要求“观察池里面的数据不保存，只保存真正推荐的”。本批新增统一正式记录投影：
  today/tomorrow/d25 在冻结检查点、正式 JSON、SQLite、归档 backlog、tomorrow 原生冻结、
  收盘 overlay 和收益结算边界只保留 `action=executable`；逐股字段来源、缺失原因、冲突、
  锚点、过滤明细和正式重放输入也按正式代码同步裁剪。SQLite 推荐与归档 backlog 新增
  派生 `action` 列，旧库从已通过哈希校验的不可变文件回填，未知动作安全排除在结算之外。
- 新增观察池生命周期状态：today 只在 09:30-11:20 当前页开放，tomorrow/d25 只在
  09:30-14:50 当前页开放且午间保留；冻结或盘后显示“已关闭”，显式历史显示“不保存”。
  `close_fallback` 没有正式推荐时保存可审计的不可变空记录，页面直接说明收盘补算未产生
  正式推荐，不再用观察项填表。

- 用户反馈公司风险研究全部超时、页面把“模型未复核”和“扣分未复核”混在一起，并要求确认
  财务、减持、亏损、近半年负面新闻的硬过滤与运行时获取方式。本批新增独立公司研究协调器、
  独立端点线程池和逐股 `ResearchRefreshResult`：每批最多 4 只、40 秒预算，合并重复代码，
  保留已完成股票并只延后慢股票；状态 API 新增批次运行/待处理/完成/部分/失败/延后计数及
  财务、公告、质押、解禁四项覆盖数。推荐 API 和详情新增独立“风险研究：已核/部分/未获取”
  状态，不再用 DeepSeek 复核状态代替数据获取状态。

- 用户要求重新增加独立观察池，并明确每策略正式推荐最多 6 只、观察最多 6 只、当前接口
  合计最多 12 只。本批新增当前 today/tomorrow/d25 的第二张观察池表，逐股显示实时行情、
  最终分与直接观察原因。观察池标题显示
  实际门槛和容量，悬停明确“观察门槛 = 正式门槛 - 观察余量”。API 新增执行/观察门槛、
  两池容量与入选数、逐股阻断原因计数、集中度跳过原因计数及受控
  `readiness_reason`。

- 用户要求修复本次工程 Review 的四项发现。本批把 `tomorrow_native_projection` 接入正式
  `perf-check end-to-end/all`，固定使用 5500 行全市场特征、三板各 120 候选、1 次预热和
  5 次采样，P95 预算为 5 秒；新增 v1/v2 三板同批决策对照，覆盖入选顺序、本地分、动作、
  原因、排名、veto、本地风险和硬过滤计数。

- 用户在重新启动后继续未完成任务，要求在评分结果正确前提下进一步合并/拆分并行事件，
  使荐股收益、总耗时和数据实时性达到当前安全边界内最优。v29 真实样本确认时区失败已消除，
  但 8 条新样本过滤一致率仍为 0，原生投影单次处理约 7 至 10 秒，5/10 秒证据还把更早的
  行情年龄计入本地流水线。本批新增第 2.13/12.3 节原生直投影、同语义过滤证据和批次就绪
  时延契约，以及独立 failure-first 回归。

- 用户在 v28 重启后要求继续未完成任务。只读现场状态确认规范行情
  `observed_at=2026-07-29T05:27:04+00:00`，v2 已接收 16 份原生输入，却因
  `decision risk observed_at must use Asia/Shanghai` 累计 35 次失败且
  `native_processed=0`。本批新增第 2.12 节原生输入时间规范化契约和独立 failure-first
  回归，覆盖 UTC/上海同一绝对时刻、候选本地/外部风险、证据以及未来点时拒绝。

- 用户在上一批指出完整交易日证据尚未具备后重新启动服务并要求继续。真实 v27 重启确认
  新配置和证据 SQLite 已加载，但旧 P6 恢复的上一交易日 frozen tomorrow 快照被影子
  worker 当作当天 baseline，形成
  `RuntimeError:tomorrow shadow query did not expose the accepted decision` 和一条永久
  `processing_error`。本批新增第 2.11 节“跨日启动与证据窗口隔离”契约、独立
  `baseline_stale_trade_date_skipped` 计数，以及进程内与持久化跨日回归。

- 用户再次发送“继续”，要求在保证评分正确的前提下继续压缩 tomorrow v2 总耗时并提高
  数据实时性。真实只读状态复核发现旧进程累计 142 条 baseline，但仅 97 条成功，
  45 条处理失败，选择一致率约 16.5%、过滤一致率为 0，且最新 local 停留数分钟；本批
  新增第 2.10 节“影子同批输入收敛”契约和非空同策略对照回归，逐项固定 v1/v2 入选代码、
  本地分及同语义硬过滤计数必须相等，不能把固定 fixture 或旧失败样本转换为切换证据。

- 用户再次发送“继续”，要求在评分正确前提下继续压缩流水线总耗时并提高数据实时性。现状
  复核确认第 2.7 节要求先保存真实完整交易日证据再由独立批次复核，而当前
  `TomorrowCutoverGate` 只有进程内 deque，重启即丢失样本，无法安全进入生产指针切换。
  本批新增独立 `tomorrow-v2/tomorrow-shadow-evidence.sqlite3`：按交易日、baseline 和
  原生输入幂等保存最近 4096 条规范观测，绑定 decision、配置、策略、融合、schema、
  父决策、三段时延、一致性、资源、DeepSeek 增量和冻结内容哈希，并以 SHA-256 校验；
  SQLite 锁等待上限为 50ms，失败时优先保住实时决策并阻断切换资格。
- 新增启动恢复和只读 `trader-cli tomorrow-cutover-evidence` 离线复核。CLI 验证每条载荷
  哈希、manifest 身份和领域约束，输出窗口摘要、工程门禁及整组 `evidence_hash`；
  `--require-eligible` 可把任一 blocker 映射为非零退出码。证据初始化、恢复或写入失败
  只增加 `evidence_persistence_failed`，不阻塞本地决策、冻结、SSE 或只读 Web。

- 用户发送“继续”，要求在评分正确和实时性不退化的前提下继续压缩流水线总耗时。现状审查
  确认上一版 tomorrow v2 影子必须等 v1 完成 `prepare_snapshot`、DeepSeek 合并和 P6
  接纳后才开始 local，因而测得的 v2 时延包含整段 v1 串行前置。本批新增深层不可变
  `TomorrowNativeInput` 和显式应用端口：同批全市场/候选点时数据完成后先非阻塞投递给
  单线程 latest-wins v2 worker，再提交 v1 策略评分；v2 local 与 v1 评分由此并行。
- 新增原生输入/baseline 关联记录和独立可观测计数。相同输入只发布一次 local，后到 v1
  snapshot 只用于一致性门禁、经校验 review 的可选 hybrid 和正式冻结；重启过渡期缺少
  原生记录时保留受控 snapshot fallback，旧 baseline 或旧原生输入只计 superseded，
  不覆盖更新决策。
- 用户明确要求 `long` 当前不需要评分，只需要最高实时性的固定池行情。本批新增
  `LongQuoteProjectionService` 与独立 `long_quotes` latest-wins 通道：按配置顺序直接把
  腾讯定向报价投影为 `observe` 当前快照，评分兼容字段固定为 0，并通过
  `score_status=not_applicable` 明确表示“不适用评分”。部分代码失败时保留同交易日最近
  有效报价或显式缺失占位，不自动换股、不缩短名单。
- 新增 Long 独立 worker、独立队列年龄/新鲜度状态、独立 `tencent_long` 熔断与生产
  cadence：交易活动阶段每 1 秒、午间每 10 秒、15:00 最后刷新一次。新通道绕过共享行情
  缓存，确保每个到期周期直接请求实时报价；慢或失败的 Long 请求不能占用候选腾讯通道、
  tomorrow 事件合并或策略评分容量。
- 用户继续下一完整迁移章节，并澄清影子运行不是下载历史 60 日数据，要求该下载先在文档
  明确暂停。本批新增 tomorrow v2 生产旁路影子：`ShadowObservingSnapshotIndex` 只在旧
  P6 成功接纳后把同一份不可变点时 `replay_input` 交给独立单线程 latest-wins worker，
  生成 local、复用既有结构化 facts 的可选 hybrid、独立冻结、SSE 和 v2 Web 投影。影子
  不持有行情、历史或 DeepSeek 网络端口，不增加物理模型请求，也不写旧运行库。
- 新增有界 `TomorrowCutoverGate`，按交易日和 baseline/input 身份去重记录选择代码、过滤
  原因、行情接收到 local 可见时延、决策年龄、冻结代码、处理错误、资源状态和 DeepSeek
  请求增量。至少 100 个成功样本、一个含匹配冻结的完整交易日、零错误/额外请求/一致性
  偏差以及 5 秒/10 秒 P95 全部通过时才报告 `eligible`；状态只供 `/api/v2/status` 观察，
  不自动切换或降低门槛。
- 用户发送“继续”，要求按迁移顺序完成下一整节。现状确认 tomorrow v2 已有决策索引和
  正式冻结，但没有从该索引到浏览器的独立只读链，旧 `/api/*` 和旧首页仍只能读取旧 P6。
  本批新增应用层 `TomorrowDecisionQueries`、匹配决策身份的纯内存报价 overlay 索引、
  `/api/v2/tomorrow/current`、精确日期正式历史、状态 API、ETag，以及有界
  `TomorrowDecisionEventStream`。当前响应按排名最多返回 10 项，历史只读取请求日期的
  不可变正式冻结，不从 HTTP 抓行情、评分、调用 DeepSeek 或现场补算。
- 新增并行 `/v2/tomorrow` 桌面工作台和独立 CSS/JavaScript。页面展示当前/历史决策、
  数据年龄、冻结、模型预算、降级、核心报价/分数/动作与选中项详情；正常 SSE 在线时
  overlay 只更新匹配 projection 的报价，不轮询完整 current。新增离线 Firefox 验收
  fixture 与 `tests/performance/run_tomorrow_v2_browser.py`，固定验证三档桌面分辨率、
  页面溢出/区域重叠、浏览器错误、截图非空和 overlay 零完整 GET。
- 用户要求继续完成 tomorrow v2 重构的下一整节，并明确当前决策不能采用持久化仓储式
  抽象。本批新增纯内存 `CurrentDecisionIndex`：local/hybrid 只能通过显式
  `expected_current_version` CAS 发布，hybrid 必须引用当前 local 父版本；并发竞争只允许
  一个胜者，旧交易日、旧 sequence、同 sequence 冲突、父版本错配和冻结封口后的迟到结果
  均拒绝。
- 新增 tomorrow v2 14:50 决策检查点与不可变正式冻结领域契约、应用协调器和独立
  `tomorrow-v2` JSON/SQLite manifest repository。冻结按“索引原子封口、正式记录持久化、
  最后切换 frozen 指针”的顺序执行，失败保留同一候选供幂等重试；重启只恢复 30 秒边界内
  且匹配当前配置/策略/融合身份的检查点。15:00 后同日正式记录缺失时可固化当前 P6 决策，
  或接受已经由完整官方收盘行情生成的 local 冷启动重建；锚点必须与实际入选代码完全一致。

- 新增 tomorrow v2 旁路 DeepSeek 融合链：同一数据快照先生成不可变 local
  `DecisionEpoch`，再仅对合法结构化复核子集生成引用 local 父版本的 hybrid
  `DecisionEpoch`。epoch 绑定实际生效的 market/candidate/research 版本、待审集合、
  特征、风险、模型审计、动作、排名、降级原因和规范哈希；完整条目限于全局最多 360 只
  已评分候选，其余全市场过滤保留聚合诊断，避免复制约 5500 行特征。
- 新增当前规范行情与有效候选尾盘的确定性 point-in-time evidence，并把匹配
  `ResearchEpoch` 的点时 evidence 和最新结构化公司风险注入 tomorrow 特征；纯函数从
  `pass`、无 veto 候选中按高风险、动作边界、TopK 边界、证据冲突和本地排名稳定选择
  最多 28 只待审/保护集合。
- 新增 tomorrow v2 旁路确定性本地选择：只读用例从一组一致数据 epoch 组装
  `FeatureSnapshot`，纯领域管道统一执行三态硬过滤、三板横截面、每板最多 120 只预选、
  tomorrow 六组件评分、本地风险单次扣分、稳定 Top10 和每行业最多 2 只，并为逐股过滤、
  缺失、候选、风险、排名与跳过原因保留紧凑审计。观察候选与正式选择分离，真实无通过
  候选时返回空结果，不降阈值补数。
- `CandidateQuoteEpoch` 新增有界 `CandidateFeatureRow`，让 14:20 后形成的尾盘结构和
  入场质量进入规范哈希并覆盖昨日基线；`MarketEpoch.market_regime` 同样进入当日身份。
  候选报价新增本轮跨源偏差与复核状态，epoch 只接受有限、非负、`<=0.50%` 且已复核的
  定向价格。
- 新增 tomorrow v2 旁路数据平面：`DailyFeaturePack`、`MarketEpoch`、
  `CandidateQuoteEpoch` 和 `ResearchEpoch` 使用上海时区、规范 SHA-256、配置/来源/
  上游版本和深层不可变载荷；`RealtimeDataPlane` 通过单锁原子发布、父版本校验、
  单调 sequence、并发 highest-wins 和每通道有界内存历史提供一致只读快照。
- 新增 tomorrow v2 目标契约与仓库级反向测试：固定不可变 `DailyFeaturePack`、
  `MarketEpoch`、`CandidateQuoteEpoch`、`ResearchEpoch`、`DecisionEpoch` 和
  `CurrentDecisionIndex`，并定义只读 v2 API、SSE、内部 5/1/10/15 秒 P95 验收及
  并行影子后原子切换边界。测试同时禁止目标契约重新引入用户否决的
  `CurrentDecisionStore`。
- 新增 DeepSeek 持久化健康门：连续 2 个传输失败、连续 2 个 schema 失败或最近 5 个完成批次
  候选应用率低于 40% 时，在共享 SQLite 总账中熔断 15 分钟；冷却后只允许一个最多 2 股的
  原子半开探针，11:15/14:43 后不再半开，连续 3 个合法恢复批次且滚动应用率达到 60% 后
  才关闭健康门。健康状态随进程重启保留，并通过预算状态摘要只读暴露。
- 新增跨 today/tomorrow/d25 的原始 DeepSeek facts single-flight。同股、同证据和同模型身份
  的并发复核只由一个 owner 发起物理 HTTP，其余策略等待缓存结果后执行各自的本地分类与融合，
  避免上午重点审 tomorrow 时重复消耗预算。
- 仓库级约束、两份权威文档和荐股冻结/恢复回归场景统一覆盖 today 11:20 当场持久化、11:30 身份不变、错过
  11:20 后启动不追补、15:00 热运行/冷启动只恢复 tomorrow/d25、正式空结果不重算、
  冻结后 `view=current` 不泄露草稿，以及旧 today `close_fallback` 仅可按历史日期审计。
- 用户要求在长期页每个具体行业/赛道 tab 后直接看到该组股票的平均涨跌情况。左侧子 tab
  现在显示组内有效实时行情的当日涨跌幅等权平均值，并在可访问名称和悬停说明中给出
  “有效行情数/分组总数”；顶部三个长期大类按钮保持原样。
- 用户要求把 11:20 后的今早正式推荐展示为可持续跟踪的冻结锚点。推荐 Web envelope 与
  SSE 推荐 patch 新增兼容字段 `anchor_source_time`，记录每只股票冻结时实际接受的带时区
  报价时间；同日冻结 today 新增专用单表和详情展示，同时给出 11:20 锚点价、锚点时涨跌、
  当前价、当前涨跌和锚点至今涨跌。
- 用户要求修复四项不值得大规模重构、但会持续增加维护风险的小问题。新增
  `scripts/generate_long_watchlist_asset.py`，以 `config/v2/long_watchlist.json` 为唯一来源
  确定性生成打包的 `long_watchlist_data.js`；新增 `make long-watchlist-check` 并接入
  `make lint`，配置变更后若忘记同步静态资源会直接失败，而不再依赖人工复制。
- 根据用户指出最近一周反复出现的“交易时段无荐股”和“15:00 后重启无荐股”，复盘
  2026-07-21 至 2026-07-27 的行情陈旧、历史预热阻塞、事件饥饿、收盘恢复、P6/Web
  选态和冷启动修复，在权威设计文档固化五时段、热运行/冷启动、四策略和真实服务验收
  矩阵，并增加契约测试，后续相关改动不得只验证单个时点或 fixture。
- `/api/status` 的 P6 状态新增按策略字节上限和最近一次超限的策略、实际字节数、限制字节数；
  Web 新增“午后开始增强模型复核”的中文降级说明，避免上午 tomorrow/d25 本地草稿把
  内部状态码直接显示给用户。
- 新增权威文档反向一致性契约测试，直接读取活动 `runtime.json` 和 `strategy.json`，固定校验
  盘中/午间调度、板内候选容量、板块可靠度、P6 缓存与驻留视图上限、当前/历史展示、
  收盘补算原因码及在线可观测性边界，避免实现变化后文档继续保留旧口径。
- 荐股策略权威文档新增“待验证收益路线”，把原收益复核提案按当前实现状态收敛为明确的
  非生产路线：完整点时决策轨迹、连续形态分、覆盖率向 50 分收缩、候选乐观上界、热度
  组合观察和配对 bootstrap 均标记为尚未实现，并固定 60 个有效交易日、300 条有效配对、
  95% 置信下界、严重回撤、99% 候选召回及 20bp/50bp/100bp 成本门禁。软件业务设计文档
  新增“已实施实时与降级基线”，集中说明 versioned DAG、本地先发布、异步复核、独立
  TopK overlay、收盘缓存、历史退避、三板尾盘轮询和选择诊断的当前边界。
- 新增仓库内可复用 Chrome/CDP 桌面看板验证脚本
  `tests/performance/run_chrome_dashboard.py`，替代每次在 `/tmp` 临时重写的检查脚本。脚本基于
  离线 D4 fixture 启动 Flask 应用和 headless Chrome，固定覆盖 1280x720、1440x900、
  1920x1080 三档桌面视口、静态资源修订号、浏览器错误、SSE patch 应用数和
  patch-to-paint P95，并输出稳定 JSON 报告；新增契约测试确保该入口持续保存在
  `tests/performance`。
- 新增前端静态资源治理入口 `web_asset()` 和统一 `WEB_ASSET_REVISION`，页面 CSS/JS 资源统一
  使用同一个 `rev` 修订号；新增版本治理契约测试，禁止活动业务身份继续使用
  `strategy_v20`、`engine_v20`、`board_policy_v18` 这类施工编号。
- 新增 `dashboard_patches.js`，把推荐 patch、overlay patch、projection identity 和空推荐
  文案等纯前端状态机规则从主 `dashboard.js` 拆出；新增缺失补丁模块回归，覆盖静态资源
  缓存撕裂时主页面不再因 `TraderDashboardPatches` 缺失而抛出 undefined。
- 新增 DeepSeek schema 常量与 prompt/cache identity 专门模块，保留 `schema.py` 兼容重导出，
  使解析、prompt 构造和缓存 key 职责分离。

- 新增 `docs/reports/chokepoint-watchlist-document-split-2026-07-25.md`，归档用户要求“卡脖子行业
  按扫描文档更新，并在中间画线区分文档名单与当前龙头股”的修改说明、行为变化、验证证据和
  剩余风险。

- 新增 `docs/reports/long-watchlist-changes-2026-07-25.md`，逐项列出长期固定名单本批新增、
  迁移、分组改名和未纳入候选，避免只给结论而不说明股票如何变化。新增翔宇医疗、麦澜德、
  中科曙光、工业富联、同飞股份和中恒电气 6 只；浪潮信息和紫光股份从低价池迁入 AI 算力。

- 新增 `docs/reports/a-share-long-industry-research-2026-07-24.md`，回应用户要求使用互联网公开的
  A 股历史行情、财务和主营事实重新审查长期页三个大类。报告以现有 43 个细分行业为框架，
  对 207 只唯一候选执行财务初筛、前复权历史复核、主营核验和全局去重；每行业最多保留
  5 只，达标不足时不补位并说明原因。本批只交付研究报告，不修改长期页面固定名单。

- 新增 `docs/hi.md`，归档用户要求的“分板评分后最多 28 支送审、最高实时性但不降低荐股
  安全”的完整实施计划、固定业务顺序、版本化执行、TopK/收盘专项、中文诊断和验收边界。
  状态接口新增执行模式、TopK overlay lane、分周期排队/执行总耗时、`close_quotes` 分阶段
  耗时，以及每策略正式推荐数和观察数。
- 新增 `versioned_dag` 运行模式：候选行情、市场新闻和公司风险完成后提交独立本地评分
  事件；本地 P6 先发布，DeepSeek 通过每策略“一个在途、一个 latest-wins 待处理”的有界
  通道异步复核，结果只经单一 merge worker 且通过基础快照、交易日、数据版本、配置版本和
  未冻结校验后发布 hybrid。schema v5 缺省继续使用旧串行模式，便于完整进程回退。
- 新增 `docs/queston.md`，归档 2026-07-24 “候选很多但 Web 无荐股、TopK 超时和多项降级”
  的整链路证据、Review 结论、实施边界与验收计划；推荐快照、状态 API、推荐 API 和 SSE
  patch 新增确定性的选择诊断，页面可区分无可评分候选、低于观察门槛、风险/执行拦截及
  最终集中度限制，并在分数不足时展示最高最终分和观察门槛。
- 新增严重公司风险事实注册表：只从带稳定公告 ID 的发行人/交易所/监管/司法结构化披露
  映射大股东减持、财务造假、正式立案、重大违法、资金占用、违规担保和强制退市程序；
  研究缓存按事实身份持久化合并，并在状态和推荐快照元数据中暴露覆盖数、事实数及版本。
- 新增 local/hybrid 两阶段发布身份。P4 本地评分完成即发布 `projection_stage=local`，
  DeepSeek 结果随后以不同 snapshot ID/ETag 发布 `projection_stage=hybrid`；long 固定
  local-only。
- 新增长期固定研究池分组：`long_watchlist.json` 改为卡脖子/国产替代固定池，股票来源追溯到
  `main` 历史中的 `CHOKEPOINT_INDUSTRY_LEADERS`；`long_groups` 暴露卡脖子行业分组和
  `低价潜力股` 分组，其中卡脖子每行业最多 5 只，低价潜力股最多 26 只。Web 长期页新增
  顶部等宽铺满的 `卡脖子行业`/`高成长赛道`/`低价潜力股` 二级 tab，以及下方左侧行业/
  赛道分组栏和右侧股票信息表，避免三个大类挤在窄侧栏；卡脖子和高成长页内继续按行业/
  赛道过滤，低价潜力股拆为 `芯片与电子`、`智能制造与软件`、`算力与卫星`、`材料与资源`、
  `种业与生物育种` 五个子 tab，右表明确展示行情来源与时间。三个大类紧跟在“长期研究 ·
  仅展示当前数据”说明后方，位于同一策略控制行且不带外层方框；它们只随长期策略一起显示，
  切换今早、明日或 2-5 日时立即与长期说明一起消失，显隐不再依赖长期行情是否已加载完成。
  下方左侧子 tab 与右侧股票信息保持同一顶部起点和相同面板高度。
- 新增长期 `高成长赛道` 固定分组：按用户要求对“除卡脖子外、长期涨幅较大且具长期研究价值”
  的全市场方向重新 Review，依据 2026-07-24 可见官方产业政策口径和全 A 历史强势方向，
  固定加入低空经济/无人机、商业航天/卫星互联网、AI 算力/光模块、具身智能/人形机器人、
  智能电动汽车、新型储能/固态电池、创新药/高端医疗、生物制造/合成生物和量子通信/6G
  未来网络 9 个赛道，每组最多 5 只，Web long 页新增 `高成长赛道` scope tab；长期三类
  固定池统一按“潜力赛道中的头部或弹性龙头观察标的”维护，不再使用旧 long 荐股策略。

### Changed

- 历史预热现在按 30 只、5 个 worker、每只最多 4 次来源请求和 12 秒单请求 timeout 推导
  300 秒硬截止；截止后释放逻辑在途身份、对失败代码执行既有退避，并继续轮转未尝试代码。
  东方财富历史回退固定为三个 host 单轮尝试，确保实际最坏请求数与预算公式一致；全市场
  请求的双轮容错保持不变。
- 交易 session 可用且盘后仍无正式记录时，推荐 API 按策略返回 `today_freeze_missed` 或
  `afternoon_close_recovery_pending`，Web 因而能准确说明 today 禁止补算或下午策略等待收盘恢复。

- `docs/V2_plan.md` 的 P5 章节状态更新为“已完成”，并补充本批 `2026-07-30` 交付项：
  历史特征恢复、交易日历游标恢复及持久化失效隔离。

- `docs/V2_plan.md` 的 P6 章节状态从“未开始”更新为“进行中”；新增本批交付说明：
  `ResearchLoader` 风险组件状态恢复与持久化、启动初始化研究恢复链路、新闻与结构化风险持久化边界。
- `docs/V2_plan.md` 的 P6 章节在 `2026-07-30` 增补第一里程碑结论：已将风险组件持久化纳入
  可追溯交付边界，并明确本批未完成 `CNInfo` 增量链与公告交叉校验的依赖条件。

- `bootstrap.py` 与 `service_tushare.py` 完成 P4 数据平面接入：`MarketFeatureService` 的
  `ReferenceLoader` 由启动时可恢复的 `DataPlaneRepository` 提供最近有效主数据与交易日历；
  启动恢复异常仅记录警告、不阻塞应用。

- `docs/V2_plan.md` 的 P3 章节状态从“未开始”更新为“已完成”；补充本批 `2026-07-30` 交付项，
  明确数据平面仓储端口、版本化 SQLite schema 与 staged/committed 恢复流程边界。

- P2 章从“未开始”更新为“已完成”；`docs/V2.md` 与 `docs/V2_plan.md` 的批次状态、产能边界与未开始
  章节计数保持一致，`tests/contract/test_project_records.py` 与 `tests/contract/test_v2_source_capability.py`
  同步覆盖该批次交付边界与 P1/P2 文档约束。

- `docs/V2_plan.md` P0 章节标记为“进行中”，并补齐基线执行结果与待切换矩阵；
  `tests/contract/test_project_records.py` 已更新与 `docs/V2.md` 目标章节一致（基线报告纳入
  版本控制、未开始章节数由 14 改为 11）。

- `docs/V2.md` 从可直接施工的九批草案调整为目标概览，执行顺序改为引用
  `docs/V2_plan.md`。优化后的关键路径先做来源能力探测、字段质量模型和持久化，再接证券
  主数据、历史和风险来源；随后续建 tomorrow 独立运行时并原子切换，最后依次迁移
  today、d25、long 和统一 Web，避免重复建设已完成的 tomorrow v2 组件。

- `docs/V2.md` 后续章节现在只引用“免费数据源的固定职责”，不再分别维护推荐表、逐源说明、
  最终组合和独立接入顺序。中英文来源名称、实时/离线边界和主备关系已收敛到同一处；
  文档治理契约与白名单同步登记该临时迁移草案。本批不改变活动运行路径、评分、冻结或
  降级契约。

- 长期固定池从 212 只/46 组更新为 224 只/50 组，卡脖子类别从 33 组更新为 37 组。
  原 `科学仪器/高端医疗设备` 按主营边界拆为 `生命科学/高端医疗装备` 与
  `高端科学仪器`，原 `精密零部件` 校正为 `高端传感器/精密测量`；航发动力、
  中航重机和应流股份迁入独立航空发动机分组，川仪股份迁入高端科学仪器。所有迁移保持
  股票全局唯一，long 继续只走定向实时行情和 `score_status=not_applicable`，不接入候选、
  评分、DeepSeek、TopK、冻结、推荐历史或收益结算。

- tomorrow v2 只有输入完整的真实业务空集才允许发布。临时失效空集在热运行保留最近同日
  有效 `DecisionEpoch` 且不发 decision SSE，冷启动保持 `not_ready`；snapshot 兼容
  fallback 也重新执行同一接纳门禁，不能绕过原生拒绝。候选已有本地评分但带板块、风险、
  历史或其他数据限制时继续形成观察项，不进入 DeepSeek 或可执行池。
- 全市场历史组装在免费来源刷新暂时失败时可复用 shared cache 的最近值；刷新到期或超过
  动作时限统一附加 `history_data_degraded` 并强制候选 `observe_only`，从未取得历史的
  股票继续触发 `missing_liquidity_history`。运行配置升级为
  `runtime_v35_tomorrow_input_quality_free_master_2026_07_30`，东财来源契约升级为
  `eastmoney_quote_v17_security_master`，公式、阈值、冻结和 DeepSeek 预算不变。

- 活动 cadence 计划器不再在每个 scheduler tick 提交整组公司研究，只由 `stock_risk`
  周期、新进入本地正式/观察集合的代码和收盘恢复显式触发。新进入代码仍先发布本地结果；
  versioned 模式把首次异步模型复核推迟到研究返回后的一次 risk 重评分，避免同一初始输入
  重复评审。单批全失败后只保留首次探测，剩余代码统一等待退避窗口，不再高速形成空批次。

- 全市场活动路由从“东财和新浪同时抓取并等待两份结果合并”改为“东财先发，1 秒后按需
  对冲新浪，首个覆盖达标且截止前完成的免费来源立即形成 P2”。未启动的第二路取消；
  已发出的第二路只在 20 秒总 deadline 内完成缓存和健康校验，不阻塞或迟到改写规范快照。
  东财和新浪来源契约升级为 v16，配置身份升级为
  `runtime_v34_free_market_hedged_route_2026_07_30`。
- 全市场正常计划间隔统一为 10 秒；连续三次物理失败后的来源退避从 60 秒缩短为 30 秒，
  退避结束先执行单请求轻量探测。东财与新浪单请求 timeout 分别固定 8 秒，候选/TopK
  腾讯定向路线、20 秒全市场总截止、冻结、荐股公式和 V1/V2 生产指针均未改变。

- 启动调度现在区分冷启动和持续运行迟到 tick：today 在 11:20 边界及以后启动永久
  `missed`，tomorrow/d25 只在 14:50（含）至 15:00（不含）恢复有效检查点，15:00 起
  只进入收盘恢复。交易日历无可靠结果时 fail closed 且不阻塞只读 Web；跨日、回拨、
  wall/monotonic 偏差和休眠跳跃轮换 session generation 并拒绝旧结果。
- 第一次 Ctrl+C、SIGTERM 或 Windows SIGBREAK 现在为 Web、scheduler、流水线、来源、
  研究、shadow、缓存和 executor 创建同一个 30 秒绝对总期限；普通任务取消，已接纳的
  freeze/risk 优先排空。第二次信号立即强制退出，期限到达以脱敏 report 和退出码 2
  终止；关闭浏览器仍不会停止本地服务。

- 观察池由“冻结后继续展示并进入历史”改为纯盘中内存投影；冻结边界起立即关闭，旧不可变
  文件不破坏性重写，但所有活动读取只投影其中的正式推荐。显式历史请求统一使用
  `top_n=12`，旧记录最多 10 个 `executable` 仍可显示，旧 `observe` 不再返回。
- 正式投影同时在应用协调器和持久化仓储执行，避免漏调路径写入观察身份；tomorrow 原生
  决策在封口时形成新的正式-only 内容版本，冻结当前与历史绑定同一正式版本。删除完整
  候选/观察输入后，冻结校验改为验证不可变 SHA-256、正式字段一致性以及所有保留输入均为
  正式代码，不再伪称可重放已经明确丢弃的观察池横截面。

- 公司研究不再在 8 秒周期事件内同步等待，也不再受“15:10 停止”这类绝对时刻约束。
  `stock_risk` 事件只提交研究意图；15:00 后无论 15:05、15:10、19:30 或更晚启动，都按
  当前缓存继续有界补齐，并在缺失 tomorrow/d25 正式记录的收盘补算中优先等待一批最多
  40 秒。财务、质押、解禁原始分项缓存 6 小时，公告缓存 10 分钟，整股解析观察缓存
  10 分钟；刷新只请求到期分项。当前 120 积分 Tushare 仍只提供未复权 daily 能力/健康
  观测，不进入公司风险或活动推荐评分。

- 活动新策略从正式 10 + 观察 8 收敛为正式 6 + 观察 6，盘中当前推荐 API 默认和硬上限
  统一为 12，tomorrow 原生 local/hybrid 决策使用同一 6+6 容量。冻结与显式历史只读正式
  推荐；旧策略冻结原文件不重排、不重算，但读取时过滤观察项；long 继续只
  发布当前实时数据，不评分、不冻结、不写推荐历史，摘要、状态和详情不再把兼容用的 0 分
  字段显示成真实评分。73 分观察线明确记录为 78 分正式门槛减 5 分观察余量，不再表述为
  已经证明的收益最优参数。

- 统一行情 merge epoch 改为绑定排序后的已接受观测代码、来源、点时、版本、载荷哈希、
  缺失原因和定向范围；最终规范快照哈希仍覆盖完整输出。固定性能 runner 的 360 行定向
  提交现在按权威契约只计 overlay 与有界 `MarketChangeSet`，来源融合属于提交前准备。

- tomorrow v2 影子现在直接从深层不可变 `TomorrowNativeInput` 选择，不再合成并拆解
  daily/market/candidate epoch；常规数据平面仍沿用真实 epoch 组装。全市场人口继续只用于
  硬过滤和板内横截面，显式候选才进入评分；审计 epoch 身份由规范输入哈希稳定派生，
  local/decision age 从候选批次完成的 `evaluated_at` 起算。活动身份提升为
  `runtime_v30_tomorrow_native_direct_projection_2026_07_29`。

- tomorrow v2 原生输入现在按上海时区校验全部全市场/候选点时数据，并在进入本地风险推导
  前深层规范化最多 360 条候选特征、报价、证据和外部风险事实；约 5500 行全市场人口继续
  在 v2 worker 组装规范 market epoch 时转换，输入哈希中的时间也统一规范化，避免在 v1
  提交前增加全人口深拷贝。活动运行身份提升为
  `runtime_v29_tomorrow_risk_timezone_2026_07_29`。

- tomorrow 工程切换门禁在存在完整交易日后，只评估观测时间最新的一个完整日；SQLite
  仍保留最近 4096 条跨日审计，并额外报告 `retained_sample_count` 与
  `evaluation_trade_date`。较早或较晚的不完整日不能污染或补足完整日的 100 样本、错误、
  一致率和 P95，更新的完整日形成后会原子取代旧评估窗口。活动运行身份提升为
  `runtime_v28_tomorrow_cross_day_evidence_2026_07_29`。

- tomorrow 评分候选完成后现在重新读取一次注入时钟，并将不跨交易日的完成水位、同一份
  不可变全市场人口和候选集合同时交给 v2 原生输入与 v1 `prepare_snapshot`；配置身份更新为
  `runtime_v27_tomorrow_shadow_convergence_2026_07_29`。v2 只从显式
  `candidate_features` 评分和排名，全市场特征仅提供硬过滤、板内横截面和聚合审计；
  观察候选继续使用既有 78/73 分动作线，低于 73 分不再因 `observe_only` 绕过观察线。

- 工程门禁的“完整交易日”由简单日期去重收紧为同日同时具备不晚于 10:00 的成功观测，
  以及不早于 14:50、v1/v2 冻结代码一致且带可恢复 SHA-256 内容哈希的成功观测；运行配置
  身份提升为 `runtime_v26_tomorrow_evidence_2026_07_29`。固定 68/32 融合、候选、评分、
  风险、动作、排名和生产读写指针均未改变。

- tomorrow v2 输入身份改为只绑定交易日、phase、上海时区评估时间、配置/数据版本、
  全市场/候选特征身份、请求代码和年龄/容量边界，不再包含后到的 v1 snapshot ID 或 review
  集合。全市场回放裁剪在 v2 worker 内执行，避免在 v1 提交前重复复制约 5500 行特征；
  v1 正式 P6、旧 API/首页、冻结和 DeepSeek 预算均未切换，runtime 身份更新为
  `runtime_v25_tomorrow_native_2026_07_29`。
- `long` 从候选代码、新闻、风险、参考数据、历史特征、共享 TopK overlay、三策略评分和
  收盘补算链中拆出；当前快照只保留报价字段、固定分组和行情来源/时间，不再进入
  `RecommendationEngine.prepare_snapshot/finalize_snapshot`。Long 仍不冻结、不写推荐
  历史或结算，全部荐股策略的评分、融合、排名和冻结规则保持不变。
- runtime schema 升至 v7 并加入显式 `long_quotes` cadence；v5/v6 旧配置缺少该项时沿用
  同 phase 的 `topk_quotes` cadence，v5 的串行执行模式兼容行为保持不变。推荐 API 新增
  `score_status`，三类荐股为 `scored`、未就绪荐股为 `not_ready`、Long 为
  `not_applicable`。
- `bootstrap.py` 现在显式装配独立 v2 决策索引、报价 overlay、冻结 repository、事件流、
  查询、门禁和影子 worker；`build_system()` 仍不启动线程或创建目录，`ApplicationSystem`
  统一启动和有界停止影子线程。旧 today/d25/long、旧 tomorrow P6、旧首页和
  `/api/recommendations/tomorrow` 继续作为正式口径，尚未执行生产读写指针切换。
- 权威设计文档新增第 2.7 节，明确历史 60 个交易日下载/回填暂停，不属于影子运行，不得
  阻塞启动、local 预览、采样或切换资格；当前影子只消费活动流水线已经取得的点时特征，
  缺失历史继续按权威过滤规则返回观察、合法空集或 `not_ready`。
- `CurrentDecisionIndex` 新增同锁读取 decision/frozen 的原子只读快照，避免冻结提交瞬间
  查询分别读取两个指针形成撕裂视图；冻结端口拆出仅含 `load_frozen` 的
  `TomorrowDecisionFreezeReader`，使 Web 查询在类型边界上不持有冻结写能力。
- `create_app()` 新增可选注入的 tomorrow v2 查询与事件流，但默认构造仍无线程、无网络、
  无数据库和无文件写入副作用；`bootstrap.py`、旧首页、旧 `/api/*`、旧 P6 和旧运行库
  保持不变，静态资源修订号更新为本批 v2 读模型版本。
- tomorrow v2 冻结选择现明确以 `observed_at <= 14:50` 的最新已接纳完整决策为准：
  deadline 前已接纳 hybrid 则冻结 hybrid，否则冻结 local，不等待迟到模型。检查点、正式
  冻结和收盘恢复均绑定规范 SHA-256；同日不同正式内容冲突，损坏文件或 manifest 不进入
  当前索引。冷启动收盘补算额外验证所有决策条目确实来自 15:00 后官方收盘特征，禁止只给
  最终入选股换收盘标签而沿用旧评分输入。

- tomorrow v2 复核响应改为合法子集逐股接纳：已返回的 `applied/abstain` 可参与固定
  68/32，缺失、拒绝或迟到股票保持本地分并标记 `deepseek_incomplete`；无可审候选、
  传输失败、deadline、全迟到和代码错配均保留 local，不制造伪 hybrid。融合后正式池
  最多 10 只、观察池最多 8 只，分别执行最终分/本地分/代码排序、单板 60% 和行业 2 只
  上限。
- tomorrow v2 的数据到本地选择边界现明确区分昨日基线、全市场当前报价和候选实时特征：
  组装缺少一致父 epoch、交易日不匹配或候选代码不属于父市场时返回 `not_ready`；候选价
  更新会同步保守扩展 high/low，避免产生价格超出 OHLC 的伪硬过滤。当前生产 P1-P6、
  DeepSeek、冻结、API 和 Web 读写路径保持不变。
- 按用户要求从已推送基线新建 `feature/tomorrow-v2` 开发分支，数据平面保持旁路，不接管
  当前 P1-P6、Web 或冻结路径。权威文档保留“压缩数据按交易日分区、默认 120 个交易日、
  20GB 上限”的目标，但本批明确不实现磁盘归档、清理、容量驱逐、运行目录或配置。
- 用户指出冷启动历史预热、三策略异步评分和 P1-P6 都是后续实现机制，不应反向成为最终
  产品需求。本批把权威目标重置为 tomorrow 唯一最高优先级生产决策链，固定 0-10 只、
  14:50 冻结、DeepSeek 继续参与融合以及 tomorrow 独占正常目标 36/硬上限 66；当前
  v1 API、`versioned_dag` 和既有 21/38 活动预算在原子切换前仍保持生产事实，本批不改变
  运行行为。
- 用户反馈明日页出现“本轮没有可评分候选”。调查确认页面准确展示了旧空快照，后台新一轮
  today/tomorrow/d25 同时评分时却共用每板唯一待处理槽，后提交策略会覆盖 tomorrow；
  同时完整板块横截面与逐股结果被反复 JSON 序列化用于缓存估算，使评分超过 15 秒 deadline。
  三板通道现按 `strategy + board` 保留各策略最新 epoch，并按
  `tomorrow -> d25 -> today` 公平轮转；横截面缓存只保留总体参数和参考分布，正式评分仅
  投影最多 360 个候选并直接执行纯本地公式，失去用途的 `local_score` 缓存配置和方法
  同步移除，运行配置身份提升为 `runtime_v23_tomorrow_scoring_2026_07_28`。评分、风险、
  动作、TopK、DeepSeek 和冻结规则均未改变。
- 用户要求把 DeepSeek 常规目标/硬上限调整为 36/66，并把预算重点从 today 转向 tomorrow。
  现状原因是旧配置仍为 146 次阶段目标、168 次策略桶并给 today 预留 68 次，而 tomorrow
  上午只生成本地草稿，不能落实用户指定的明日荐股优先级。运行配置现固定 shared `2/4`、
  today `5/8`、tomorrow `21/38`、d25 `8/16`，另设 `0/5` emergency；正常目标 36、正常
  硬上限 66、计划最坏 71，168 只保留为不可突破的全局灾难保护线。
- tomorrow/d25 从 09:36 开始复用上午模型 facts，09:30-09:36 与午休不新增请求；Flash
  主审固定最多 4 股，预热/健康 canary 最多 2 股，Pro 全日最多 2 次且全部保留给 tomorrow。
  today 在 11:18、tomorrow/d25 在 14:46 停止提交，已提交请求仍分别可在 11:20/14:48 前
  接纳；调度器新增两个停止提交唤醒点，冻结 CAS 与 11:20/14:50 正式记录不可覆盖规则不变。
- 行情来源职责保持“东方财富/新浪全市场、腾讯候选与 TopK 定向刷新”。现场调查确认本次
  东方财富连接被远端关闭后新浪兜底成功，并非新浪 SDK 整体失败；腾讯定向批次缺失个别代码
  时继续使用规范快照中的新浪全市场报价或最近有效值，不强制把所有定向刷新切换到较慢的
  新浪全市场请求。静态资源 revision 更新为 `freeze-recovery-boundary-2026-07-27`。
- 冻结与盘后恢复职责按策略分离：today 只允许在持续运行的 11:20 边界冻结并立即写库，
  11:30 仅作入库验收；若进程错过边界则当日保持 `not_ready`。tomorrow/d25 上午仍正常
  生成，14:50 冻结，正式记录缺失时才允许在 15:00 后从本进程 P6 或完整收盘行情恢复。
- 长期行业均值随现有完整快照、SSE overlay 和重同步重绘即时更新，不新增后端接口或浏览器
  行情请求；静态资源 revision 升级为 `long-group-average-2026-07-27`，避免旧脚本和样式缓存
  继续显示只有行业名称与股票数量的 tab。
- 同日正式冻结的 today 仍只展示 `executable` 名单，名单、排名、评分和动作保持不可变，
  但 11:20-15:00 继续消费既有 TopK overlay，15:00 后保留 closing overlay 至下一交易日；
  页面状态改为明确说明冻结决策与最新可用行情的边界。静态资源 revision 升级为
  `today-freeze-anchor-2026-07-27`，避免浏览器继续使用缺少锚点视图的旧脚本和样式。
- 长期观察池 20 处暴露退役 `stock_analyzer/...` 实现路径的来源文字统一改为稳定业务描述
  “历史卡脖子行业龙头名单复核”；股票、顺序、分组、研究属性和运行策略均未改变。软件
  业务设计同步明确 JSON 唯一来源、确定性生成入口和常规 lint 门禁。
- 板内评分改为“全市场只构建横截面总体、每板最多 120 只新鲜候选投影后评分”，候选报价
  版本只失效候选批次和逐股分数，不再把约 5500 只全市场股票重复执行九组候选/本地评分后
  再与候选代码求交集。候选定向报价提交同步改为有界增量 `MarketChangeSet` 和代码索引
  更新，不再在状态锁内重建完整列式批次；完整全市场刷新仍重建规范基线。
- P6 保持 today/tomorrow/d25 每视图 160 KiB，long 当前固定研究池使用独立 512 KiB 上限；
  long 仍不冻结、不写推荐历史，最大 64 个驻留视图和 12 MiB P6 总池不变。
- Web 静态资源 revision 升级为 `recommendation-availability-2026-07-27`，确保服务重启后
  浏览器不继续命中 7 月 26 日的 dashboard/render 缓存。
- 用户要求根据工程代码实际情况逐条反向核对两份权威文档。核对活动过滤器、板块评分、
  融合、选择、冻结恢复、调度、P6、状态 API 和前端渲染后，文档现按生产路径记录：
  必需阻断与可选观察限制的真实原因码；today 的 `relative_strength_3d` 和 d25 活动五维；
  未应用 hybrid 时最终分等于本地分；每板最多 120、三板最多 360；午间全市场、候选和
  TopK 均为 10 秒；盘中当前视图只显示正式推荐，收盘补算与历史显示完整 API 结果。
  本批只纠正文档和文档契约测试，不改变生产公式、阈值、调度、冻结、API 或 Web 行为。
- 用户要求把 `celue.md`、`hi.md` 和 `queston.md` 根据当前工程实际合并进两份权威文档。
  核对配置、选择函数、流水线、历史预热、尾盘加载、Web schema 和回归测试后，确认生产
  实现是每策略每次投影最多 28 只送审、每日 168 次物理请求、`versioned_dag`、local 先于
  hybrid 发布、逐股 60/120/240/480/900 秒退避、三板稳定轮询及七字段选择诊断；故障修复
  初稿中的 24 只上限已被后续实现取代。三份来源文档不再作为并行状态源，已实现内容归入
  软件业务设计，未实施的收益建议归入荐股策略“待验证”章节；本批不修改生产代码、配置、
  策略公式、门槛、预算、冻结或 API 行为。
- 用户指出浏览器检查验证脚本每次临时写入、重复创建，浪费且不可复用。现在验证入口成为
  项目测试资产，并把 `websocket-client` 纳入 dev 依赖；后续排查“快照状态”和桌面布局时可直接
  执行同一脚本，不再依赖一次性 `/tmp/trader_cdp*` 脚本。
- 用户要求 Review 全工程 JS、代码框架、结构、文件、类、函数、设计和划分是否仍有优化空间，
  并要求执行计划；随后反馈“快照状态 Cannot read properties of undefined (reading
  'projectionVersion')”，并要求优先清理乱七八糟的 V 几版本命名。现状判断：核心分层依赖
  仍符合 `entrypoints/web/infra -> application -> domain`，但 `dashboard.js`、DeepSeek
  `schema.py` 和 `bootstrap.py` 分别承担过多职责；前端每个静态文件各自维护 `?v=数字`，
  容易让浏览器缓存到不一致资源。现在主 dashboard 降到轻量编排，patch 规则独立，DeepSeek
  prompt/cache identity 独立，`bootstrap.py` 通过私有 context/helper 拆分装配步骤，仍保持
  唯一组合根且 `create_app()` 无线程、网络、数据库或文件写入副作用。
- 活动可读版本身份改名为 `strategy_review28_2026_07`、`engine_review28_2026_07`、
  `fusion_local68_deepseek32`、`board_policy_score_first_2026_07`、
  `market_cache_p1_p6` 和 `long_watchlist_document_merge_2026_07`。历史冻结回放常量、
  SQLite/API/SSE schema 版本、`config/v2` 路径和 DeepSeek `deepseek-v4-*` 模型名保留，
  避免破坏协议和旧快照兼容。

- 用户再次要求 Review 全工程命名，选择“严格全改”，要求继续处理不专业、不简单通俗的残留。
  本批确认源码和文件名已无新的拼音旧词、旧阶段词或 `support/utils/helper/manager/processor`
  泛称文件名；剩余可优化项集中在暴露实现细节的诊断码和前端长期区域命名。现在收盘补算
  观察门槛原因码改为 `close_fallback_observe_floor`，列式投影和合并降级码改为
  `columnar_projection_failed`/`columnar_merge_failed`，不再把 `scalar_fallback` 实现细节暴露
  给 API、测试和归档。长期页左侧区域改为 `long-sidebar`，内部 JS 绑定改为
  `longSidebar`、`longTitle`、`longMeta` 和 `resultLayout`。保留项包括 `service_*` 行情子服务
  分层、精确财务/风险业务字段、描述性测试名、协议版本词、恢复语义和浏览器标准
  `cache: "no-store"`。

- 用户要求继续 Review 全工程命名，明确把旧英文阶段词、拼音旧词和缓存泛称全部清掉，
  并追问是否还有“又臭又长”、不专业或不通俗的命名。现状已确认：旧阶段词仍散落在活动公共
  契约、P6/SSE 发布补丁、Web envelope、测试、fixture、报告路径和性能指标名中；多个
  `support`/`utils` 后缀模块只说明“辅助”，没有说明实际职责。现在活动代码、测试、fixture、
  报告路径、配置和权威文档统一改用具体业务名：公共契约为 `pipeline_contracts`/
  `PipelineContractError`/`pipeline_schema_versions`，P6/SSE 发布补丁为 `projection_patch`，
  Web 快照上下文为 `SnapshotViewContext`，前端格式化资源为 `dashboard_formatters.js`，
  行情、DeepSeek、持久化和评分边界分别改为 `market_cache_identity`、`gateway_runtime`、
  `tushare_records`、`budget_audit`、`reviewer_selection`、`snapshot_files`、
  `recommendation_policy_codec` 和 `scoring_calculations`。指标分组同步改为 `p6_projection`
  与 `sse_publish`。描述性测试名保留，协议词 `schema_version`/`data_version`、状态词
  `supported`/`unsupported`、恢复语义 `restore/restored`、标准 Fetch 值 `cache: "no-store"` 和
  Python 标准库 `contextmanager` 保留。

- 用户要求把最近两次提交后的卡脖子行业按扫描文档更新，并把当前龙头股合并到扫描文档正式
  名单对应的股票列表里，用横线分隔，避免 tab 页太多。原因是扫描报告已产出正式名单，但活动
  配置只合并了局部分组调整，且初始分区方案会增加过多左侧按钮。现在 `long_watchlist.json`
  为分组增加 `sections`，同一行业内先排列 `document_scan` 文档正式股票，再排列
  `current_leaders` 当前龙头补充；前端在右侧 long 股票表的补充段首行绘制横向分隔线。验证
  覆盖配置加载、打包静态名单、Web/API 契约和 JS 分组选择；剩余风险是扫描数据截止
  2026-07-24，主营摘要仍不能替代公告级国产替代证据；桌面 Chrome 截图验收因本地端口绑定
  需要提权且提权请求被中断，仍需人工打开长期页核对三档桌面分辨率。

- 用户指出 `docs/reports/a-share-long-industry-research-2026-07-24.md` 的正式股票没有合并进
  卡脖子行业。原因是活动配置仍使用研究前固定名单，仅生成了量化报告而未执行回写。本批新增
  卡脖子 `固态电池` 分组，迁入报告正式通过的国轩高科、当升科技、亿纬锂能和恩捷股份；删除
  高成长 `新型储能/固态电池` 混合分组，并移除未进入正式结果的容百科技，保持全局不重复且
  不为凑满 5 只降低标准。

- 用户要求卡脖子增加脑机相关龙头，并将 AI 算力、液冷和电源完全拆开。长期固定池从 207 只、
  43 组调整为 213 只、46 组：新增 `脑机接口`、`AI算力`、`液冷`、`数据中心电源` 四个
  卡脖子分组，删除组合分组 `AI算力液冷/电源`；高成长 `AI算力/光模块` 改名为 `光模块`，
  原 5 只光模块股票不变。低价 `算力与卫星` 移出浪潮信息和紫光股份后从 5 只缩减为 3 只，
  低价类别总数从 26 只缩减为 24 只。
- 脑机接口按质量优先只加入翔宇医疗和麦澜德 2 只。`*ST益通` 因风险警示、扣非利润恶化和
  ROE 为负排除；创新医疗和爱朋医疗虽有产品布局但最新 ROE 为负，三博脑科仍以临床研究和
  实验验证为主，均不为凑满 5 只而纳入。

- 用户质疑现有“高成长赛道”和“低价潜力股”来自人工固定名单、缺少历史数据依据。本批明确
  将原名单仅作为候选入口，不再把人工标签视为结论；207 只候选均取得同花顺财务摘要，163
  只进入历史复核，152 只通过基础财务和历史门槛，分类硬门槛审查后正式保留 113 只。历史
  结果统一用新浪前复权日线重算，并修复首轮腾讯序列中非正早期复权价格导致的极端失真收益；
  上市历史不足相应周期时不再把“上市以来”冒充 5 年或 10 年收益。
- “低价潜力股”改用可审计的报告口径：2026-07-24 收盘价不高于 20 元、2025 完整年度
  EPS 为正、近似 PE 不高于 35 倍、2026 年一季度每股净资产对应近似 PB 不高于 4 倍。
  原 26 只候选中仅沃顿科技同时通过机械门槛；其余不为凑满每行业 5 只而继续标为低价低估。

- 用户反馈长期界面重点不足、股票跨 tab 重复，并看到 `unavailable`、`long_watchlist` 等英文。
  原因是固定名单允许同一代码跨多个分组复用，且行情源和静态回退状态直接显示内部标识。
  现将长期固定池调整为 207 只股票对应 207 个唯一分组席位，每只股票全局只出现一次；原有
  53 个重复席位全部替换为行业语义匹配的新观察标的，新增代码和简称已通过腾讯行情只读校验。
  配置加载新增跨组重复和未分组股票硬校验，打包静态名单同步保持一致。
- 长期界面标题改为“重点研究方向”和“重点股票行情”，激活分组增加强调线、渐变底色，股票行
  增加悬停高亮并强化股票名称。运行阶段、行情来源、固定名单来源和动作原因统一映射为中文，
  `unavailable` 显示为“行情暂不可用”，`long_watchlist` 显示为“长期观察名单”，顶部
  `DeepSeek` 标签改为“模型额度”，未知英文状态使用中文兜底且原值仅保留在本地诊断中。

- 腾讯定向实时行情现在解析响应中的总市值字段并换算为元；长期页实时简称优先使用行情源
  返回值，行情源行业为空时保留固定观察池行业，避免公司更名后仍显示旧简称或实时行情覆盖
  掉固定行业。长期代码继续并入统一 `candidate_quotes` 请求，不新增浏览器抓取或专用线程。
- 用户将分板本地评分后的 DeepSeek 送审候选上限从每策略每次投影 24 支调整为 28 支；
  活动策略/回放身份升级为 `strategy_v20_review28_2026_07` /
  `engine_v20_review28_2026_07`，v19 冻结输入仍按记录策略确定性回放。评分、严重公司风险
  硬过滤、0.85 板块可靠度、68/32 融合、动作门槛、TopK、最后执行的同行业最多两支及 long
  当前观察语义均未改变。
- TopK 报价改由独立单 worker、单 latest-wins 槽处理，不再排在 `close_quotes`、参考数据或
  DeepSeek 后面；来源完成触发的本地评分拥有独立 15 秒执行/38 秒含排队预算，DeepSeek 完成
  事件也拥有 38 秒排队窗口及开始执行后的 2 秒预算。收盘重试只复用逐行同一 merge epoch、
  同日 14:59 后、正价格且三板 20 日历史各至少 100 只的完整缓存，并只补取缺失候选报价。
- DeepSeek 每日物理 HTTP 硬上限由 188 调整为 168：today/tomorrow/d25/shared/
  emergency 硬桶固定为 68/45/35/15/5，阶段目标合计 146，失败、超时、schema 修复和
  重试仍计数。评分后每策略每次投影只复核最多 24 支可靠候选，并以同一集合判断 hybrid
  完整性；v17/v18 冻结输入继续按旧范围回放，未复核候选保留本地评分。
- 用户追问 long 是否“单独开线程抓固定股票涨跌幅”、旧 long 无效代码是否删除。现状确认并
  固化为契约：long 不新增专门行情线程，固定池代码并入统一行情刷新范围，`trader-long`
  worker 只包装固定观察快照；DeepSeek runtime 预算、挑战者上限和阶段上限不再包含 long
  的 0 额度槽，本地评分权重和模型维度权重也不再允许 long。
- tomorrow 尾盘分钟线只改变 I/O 调度顺序：在保持候选集合、评分和返回顺序不变的前提下，
  按主板、创业板、科创板稳定轮转，避免单一板块在 3 秒执行窗口内占满请求。
- 用户要求“先判断分数，最后再处理行业”，并质疑行业参数造成无荐股。确认活动评分原先
  同时使用同行收益差、领先组、行业趋势、长期行业政策和 DeepSeek 行业维度，且竞争组上限
  会覆盖全局行业上限。现在 today/tomorrow/d25/long 的活动候选分、本地分、DeepSeek 分、
  动作和下行保护均不读取行业；行业只保留展示/审计标签，在最终稳定排序后限制每行业最多
  2 只。正式池与观察池分别计数，空行业统一进入 `unknown` 组，竞争组不再覆盖该限制。
- 用户明确要求“去掉现在 long 的荐股策略，新的跟现在策略没有关系”。long 现在不再计算旧
  长期价值/成长/质量/风险评分，不调用 DeepSeek，不做 TopK 或行业集中度选择，也不因旧
  long 研究字段缺失降级；运行时只按配置顺序生成 `observe` 固定观察项并刷新价格、涨跌幅、
  来源和时间。推荐 API 对 long 不再按 `top_n` 截断，SSE patch 和发布索引会保留
  `long_groups`，避免长连接或 P6 发布后丢失二级 tab 数据。
- 用户反馈“长期页面没变，左侧没有行业栏，右侧也没有股票信息”。确认此前实现只把长期
  分组渲染为表格上方横向 tab，且静态资源版本未覆盖左侧布局诉求；现在长期策略渲染时
  切换为固定宽度左侧 `long-sidebar` + 自适应铺满右侧 `recommendationTable` 双列布局。
  右侧长期专用表只展示排名、股票、最新价、今日涨跌、成交/换手、总市值和行情时间，
  不再混入短线评分列；长期请求不再发送通用 `top_n=18`，从而让 `低价潜力股` 的 26 只
  固定股票可以完整显示。`dashboard.css` 内部导入的组件 CSS 同步带版本号，避免 Chrome
  继续复用旧 `dashboard_components.css` 后把新长期结构渲染成左上角小区域或残留横向滚动条。
- 用户继续反馈中间仍显示“长期策略当前尚无可用数据”。确认运行策略阶段此前只在下午
  `AFTERNOON/FINAL_REVIEW/FINAL_QUOTE` 处理 long，上午或 warmup 点长期时后端没有当前
  long 快照；现在 long 固定观察池随 warmup、today 盘中和下午阶段一起从统一行情缓存生成
  当前快照，布局和股票表不再因上午缺少 long 发布而隐藏。
- 用户要求硬过滤增加“大股东减持、财务造假、被立案调查等黑历史”。大股东现固定为控股
  股东、实际控制人和持股不低于 5% 股东；减持计划至完成/终止后 90 天阻断，正式立案和
  强退程序至结案后三自然年阻断，确认造假/重大违法/资金占用/违规担保永久阻断。一般问询、
  普通诉讼、亏损预告和风险提示不再通过旧 `negative_announcement_level` 直接硬过滤，
  DeepSeek 也不能创建、解除或 veto 这些硬事实。
- 行业组件删除后的权重按剩余组件原比例归一化，固定融合公式
  `local_score*0.68 + deepseek_score*0.32 - deepseek_risk_penalty`、动作阈值和本地风险
  上限保持不变。放量突破不再要求行业上涨宽度。

- 用户要求启动历史严格按“最多 360 只、每只内存最多 20 根原始日线”收敛。冷启动现在
  不再读取旧历史种子或 `.runtime/v17/history_cache.sqlite3`，而是从腾讯/东方财富临时取得
  最多 61 根 qfq 日线，计算 MA60、60 日收益锚点等紧凑摘要后只保留最近 20 根原始记录；
  重启重新预热，现有因子公式、阈值和策略版本不变。

- 活动推荐库只保留 today/tomorrow/d25 最近 20 个不同交易日、每策略每日最多 18 条。
  更旧冻结、overlay 和 outcome 先写入带 SHA-256 的 `recommendations-v1` 不可变文件归档，
  验证成功后才从活动库删除；未结算目标留最小 backlog，CLI 支持 `list|verify|export`，
  Web 不读取归档。

- 流水线事件 CAS 与实时来源健康全部改为有界进程内状态；必要 SQLite 写入只剩冻结、
  检查点、结果结算和 DeepSeek 168 次原子预算，并由组合根注入的同一写锁协调。

- `close_fallback` 的短线 TopK 现在增加只限收盘补算的空池恢复：today/tomorrow/d25 仍先按
  正常动作阈值和 5 分观察窗口选择；若本地候选非空但正式/观察池都为空，则按原集中度规则
  发布最多 8 个无 veto 的本地候选为 `observe`，追加
  `close_fallback_observe_floor`，不生成 `executable`，不改盘中普通快照。
  Web 查询层对已落盘的旧空 `close_fallback` 冻结快照执行只读 replay 投影，保留冻结文件不变。

- Web 当前视图的可见行规则现在区分盘中和收盘补算：today/tomorrow/d25 盘中仍只展示
  `executable`，但 `phase=close_fallback` 与历史视图会展示 API 返回的全部 TopK 项，避免
  收盘补算结果全为 `observe` 时被前端过滤成空表。`selection.js` 静态资源版本同步升到 v2，
  避免浏览器继续使用旧过滤逻辑。

- 冻结时点缺少 pre-cutoff 草稿现在只增加 `freeze_missing_pre_cutoff_snapshot` 计数，不再写入
  最近错误；后台 outcome settlement 被 latest-wins source lane 取代时只增加
  `outcome_settlement_superseded` 计数，不再用
  `outcome settlement degraded: SourceRequestSupersededError` 覆盖真正的推荐诊断。

- 15:00 后冷启动收盘重建现在允许 d25 在结构化研究字段或板块可靠度不足时降级发布
  `close_fallback`，逐股动作保持 `observe` 并保留 `board_data_reliability_below_threshold`
  诊断；只有收盘报价、20 日历史样本或板块人口不足继续阻断创建。

- 15:00 后冷启动现在会为已配置的 long watchlist 生成使用同日收盘价的当前非冻结快照，只发布
  当前视图，不写冻结历史，避免服务盘后启动时长期页一直 `not_ready`。

- 15:00 后冷启动收盘重建现在按缺失策略独立提交：单个策略遇到可降级研究字段或可靠度不足时
  创建带诊断的观察项；遇到不可降级的收盘报价、历史样本或板块人口阻断时，只记录该策略错误
  并继续提交其它已满足契约的 `close_fallback`，不再把 ready 策略一起丢弃。

- tomorrow/d25 的 `volume_to_5d_average` 因子从 v1 升级到 v2：优先使用行情源点时
  `volume_ratio`，若供应商未提供，则用同日点时成交额除以最近 5 个已完成交易日平均成交额
  派生。该修改不读取同日历史 bar，不降低 0.85 板块可靠度门槛，不改变动作阈值、融合公式
  或 DeepSeek 预算。

- DeepSeek 预算仓储的物理请求预留、完成和恢复写事务现在在同一进程内串行进入 SQLite
  `BEGIN IMMEDIATE`，保留数据库原子计数和跨进程锁语义，同时避免 16 线程并发预留时直接抛出
  `database is locked`。

- 15:00 后缺失正式记录的收盘恢复现在给 `close_quotes` 留出 180 秒有界执行预算，
  覆盖慢收盘行情源返回和本地补算写入；后续重试若已有完整、同日、三板历史样本达标且未被
  可靠度/样本错误标记的收盘全市场缓存，会直接复用该缓存继续创建 `close_fallback`，
  不再反复同步抓取慢全市场来源。该修改不降低 0.85 板块可靠度门槛，不改变候选、评分、
  动作阈值、DeepSeek 预算或冻结不可覆盖规则。

- 收盘补算使用的候选研究只读缓存现在会在内存/shared cache 未命中时读取未过期的本地
  structured research JSON，并回填进程内缓存；该路径仍不发起 AkShare 或其他网络研究请求，
  不改变 0.85 板块可靠度门槛、候选公式、动作阈值、DeepSeek 预算或冻结不可覆盖规则。

### Fixed

- 修复扫描发现的两个运行问题：会话感知查询曾把盘后三策略统一压成
  `official_record_missing`，导致 Web 只能显示泛化状态；历史预热曾没有批次 deadline，单个慢尾
  可长期保留整批 `inflight` 且状态 API 无法显示在途年龄或超时次数。当前实现分别恢复分策略
  生命周期原因，并为历史批次增加可观测的硬截止与回归覆盖。
- Review 全量门禁时发现两项 DeepSeek 并发契约把线程进入 fake HTTP 的调度窗口固定为 1 秒，
  在完整套件负载下重复超时、隔离运行则通过；测试同步与释放窗口统一放宽到 5 秒，不改变生产
  deadline、传输 timeout、原子预算、single-flight 或半开探针行为。

- 修复用户在约 17:00 冷启动后看到空的 `close_fallback` 已冻结快照：当收盘预选为空且过滤审计仍包含
  `history_warming` 或 `missing_liquidity_history` 时，不再固化空记录，保留 `not_ready` 并按现有
  3/5/10/20/30 秒退避继续等待历史数据。
- 修复冷启动收盘补算把预期的 `local_only` 错报为“模型复核未在冻结前完成”：
  `close_fallback` 不再生成 `deepseek_pending`；正式投影为空时同时移除与不存在候选相关的模型、
  tomorrow 尾盘和 d25 研究覆盖降级原因，保留真实市场/数据源降级。

- 修复 `ReferenceLoader._refresh_tushare_reference_data` 在 `listing_dates` 为空时未定义
  `valuation_observations/financial_observations` 导致的 `UnboundLocalError`，并保留回放
  行为的幂等输出。

- 修复 `ResearchLoader` 风险组件恢复覆盖中的两个边界问题：修正状态读取时的无效载荷处理，
  并修复持久化记录构造中的重复字段问题，避免写入失败导致组件覆盖链路中断；`status()` 改为
  结构化样本的组件覆盖计数。
- 固定 P5 历史特征恢复与写入降级行为：`HistoryCache` 与 `ReferenceLoader`
  在数据平面不可用时仅记录告警并继续返回已可用历史/行情；单条恢复记录反序列化失败则
  跳过，不使单券历史加载或启动恢复失败。
- 修复 `DataPlaneRepository._load_records` 的 `ORDER BY` 使用未定义字段列表函数导致 `NameError`
  的恢复路径中断问题，保障启动恢复与 cursor 读取的稳定性。

- P3 数据平面恢复路径修复：`DataPlaneRepository._load` 与 `_recover_staged_row`
  不再因单条 `committed` 损坏记录抛异常导致批处理中断；改为按条隔离为
  `quarantined` 并写入恢复审计，保留最近有效快照可读性与可追溯性。

- 修复 `PLR0913`（函数参数过多）引起的重构质量闸门告警：`field_quality._apply_new_selection`
  改为状态容器参数版本，`scripts/check_refactor_quality.py` 重新回归零诊断；`make type-check` 也通过。

- 修复原计划把已删除的 `stock_analyzer`、当前 `RecommendationPipeline/P6` 和 tomorrow
  v2 影子链都称为 V1，导致第一、二、九批范围重叠的问题；同时修复先接数据源、后定义
  字段和仓储，以及在切换前删除影子比较器的依赖倒置。

- 修复 `docs/V2.md` 同一数据源职责在四处重复、容易产生口径漂移的问题。合并后明确
  AKShare 是适配器而非独立容灾源，BaoStock 和 Tushare 120 积分只承担历史校验，
  通达信/mootdx 必须先影子实测，且任何行情 fallback 都不能整行覆盖证券主数据。

- 用户可观察问题是长期卡脖子页的科学仪器、医疗装备、精密测量和航空发动机关键环节被
  混在宽泛旧分组中，且新型电力系统与核聚变方向没有固定实时观察入口。根因是版本控制的
  `long_watchlist.json` 尚未按本轮行业口径拆分和补齐，而不是评分规则遗漏。现在数据源
  已按产业链边界拆分、迁移和补充，页面沿用既有长期分组导航与实时行情投影直接展示，
  不通过放宽评分或增加评分因子解决。

- 用户可观察症状是 V2 连续三组为空，而 V1 每组仍有两只；V2 的 5533 行全部未评分并同时
  报 `missing_listing_date`、`board_identity_degraded`、公司风险缺失和行情过期。确认根因
  不是 worker 或发布线程停止，而是原生管道把全市场可选缺失与候选必需输入混成一个聚合
  口径，并无条件用临时失效的空 decision 覆盖最近有效结果；免费全量证券主数据在实时源
  切换时也会丢失，历史刷新到期则过早退化成硬缺失。现在候选级质量门禁阻止无效空覆盖，
  热运行保留最近有效结果、冷启动显式未就绪，合法业务空集仍可发布；板块/上市日期跨免费
  实时源保留，历史降级值可评分但只能观察。
- 修复 V2 对 `execution_restrictions` 没有统一降为观察项的问题，避免超过动作时限的历史、
  行情或研究降级候选仍进入正式执行池；修复生产 `TomorrowDecisionQueries` 未连接 shadow
  runtime telemetry，导致输入保留原因只能在内部 shadow 字段看到、顶层状态仍像正常运行。

- 用户可观察症状是公司研究批次累计异常快、失败频繁且待处理股票在来源熔断时仍被连续
  扫描。根因确认：生产 `submit_due()` 每次调度 tick 都绕过 `stock_risk` cadence 提交
  全组代码，协调器又会在首批全失败后继续排空剩余代码。现在常规 tick 不再直提研究，
  已完成股票进入冷却，部分/失败股票逐股退避；全失败首批会短路剩余批次并保留最近有效
  快照。公司风险缺失仍保持观察、已确认事实继续保留，硬过滤和风险扣分口径均未放宽。

- 修复慢新浪约 70 页分页拖住东财有效结果、导致整轮全市场超时的问题；路由不再等待第二
  来源。修复东财从共享来源 worker 调用时退化为串行分页的问题，改为随请求关闭、最多
  6 worker 的有界分页执行器，避免嵌套等待共享来源池。
- 修复全市场缓存到期后先返回旧快照并后台刷新的实时性错误：用于候选发现的到期缓存不再
  冒充本轮成功，必须等待本轮东财/新浪物理结果；只有来源失败时才显式保留最近有效规范
  快照。修复熔断期间每次计划跳过仍累计 `error_count`/连续失败的问题，未发出的 deadline
  跳过也不再打开熔断。

- 修复晚启动追补 today、14:50 后补发 cutoff/最终报价、任务入队前即被标记完成，以及
  队列拒绝、SQLite/JSON 暂时失败或 P6 拒绝后不再重试的问题。冻结现在只重试同一
  official-only 对象、ID、规范载荷和 SHA-256，pending afternoon attempt 会阻止不同的
  close fallback 抢占。
- 修复关闭 timeout 按组件重复累计、超时后无界 join、普通事件关闭时仍被排空、第二次
  信号不能强退，以及 staged manifest 在任意 kill point 后可能永久占用交易日的问题。
  恢复会完成同一载荷、清理未提交占位或对 committed 损坏 fail closed。

- 修复冻结、`close_fallback` 和历史仍可能把已丢弃观察项解释成“达到评分门槛但被风险或
  执行拦截”的问题。正式空记录现在使用独立冻结空状态，观察诊断计数、门槛、逐股原因和
  最高分只按正式项重建；收盘补算返回、P6、状态、SSE、overlay 与磁盘统一使用同一个
  正式投影，避免页面看到内存观察项而重启后消失。
- 修复旧持久化记录的观察项仍可能进入收益结算、归档 backlog 或 native tomorrow 历史的
  兼容漏洞；初始化回填动作后只调度正式目标，所有旧冻结读取和原生历史读取再次执行
  official-only 投影。

- 根因确认：旧 `stock_risk` 的 8 秒总截止包含事件排队，而每只股票串行访问四个最长
  8 秒端点；旧批次等待逻辑一旦存在未完成 future 就在消费已完成 future 前抛出整批超时，
  因而多次调度仍可能得到零条有效结果。现在截止先消费所有完成 future，再取消可取消的
  未开始任务并记录延后；慢结果不会覆盖本批状态，最近有效研究事实继续保留。DeepSeek
  未参与时表格仍显示“模型未复核”，但“模型扣分”改为实际生效的 0，不再误显示第二个
  “未复核”。
- 最终 Review 修复了“物理请求完成即视为研究变化”的重复触发：协调器现在仅在新旧研究
  数据版本确实不同（包括覆盖或降级状态变化）时提交风险优先级重评分；缓存过期后返回相同
  研究版本只更新完成状态，不重复计算或影响冻结记录。
- 硬过滤口径未被放宽或扩大：官方结构化大股东减持和严重公司风险、解禁、质押以及正式
  财报触发的财务恶化继续按既有阈值阻断；一般负面新闻、普通问询/诉讼和未经正式财报确认
  的亏损预告仍只作提示，不能由标题或 DeepSeek 自由文本创建硬过滤。来源未获取不会猜成
  “无风险”，而是保留已确认事实并按覆盖不足降级观察。

- 用户反馈 11:20 后今早没有推荐却显示“候选达到评分门槛，但被风险或执行拦截”，同时
  tomorrow/d25 和冻结状态也使用同类泛化提示。原因是主表过滤掉 `observe`，而旧诊断只
  提供聚合空结果码，页面又把快照生命周期、逐股动作阻断和模型复核状态混在同一文案。
  现在主表无正式项但有观察项时直接指向观察池；无观察项时显示最高分与两条门槛，或列出
  真实阻断/集中度原因。未就绪区分尚未发布、today 错过 11:20、14:50-15:00 冻结收口、
  15:00 后收盘恢复及 long 无当前数据；冻结后的 `deepseek_pending` 改为“冻结前未完成，
  已按本地评分固化”，不再误报仍在修改正式结果。

- 修复当前交易日基准行情缺失时结算服务过早返回、导致已到期个股连毛收益和
  `benchmark_missing` 状态也不落库的问题；现在只跳过基准及净超额，仍保存个股毛收益。
- 修复盘中请求超时取消回归依赖 10ms 线程调度、偶发在首个 I/O 尚未退出时开始下一轮的
  问题；测试现在显式释放并等待首个阻塞调用完成，再验证下一刷新会重试。
- 修复固定行情性能绝对门禁红项：合并不再为 epoch 重编码 5500 个完整投影报价，性能
  fixture 也不再重复进行字符串数值转换；三轮 market-data P95 均通过原 250/600/900/100ms
  上限，未放宽预算。

- 修复 v1 的非硬拒绝诊断 `history_warming` 被直接拿来与 v2 真实硬过滤计数比较，导致
  真实历史预热期间过滤一致率恒为 0 的问题。门禁现在只从 v1 侧剔除该已知诊断别名，其余
  新增、缺失或计数不同的原因继续严格阻断；入选代码仍按排名顺序比较，没有降为集合比较。
- 修复 tomorrow 原生输入已完成点时校验和哈希后，worker 又复制约 5500 行构造三类合成
  epoch、立即拆回特征，重复全量校验/哈希并推高发布时延的问题。该删除不改变候选、本地
  分、风险事实、动作、排名、DeepSeek、冻结或 v1 正式读写路径。

- 修复规范行情使用 UTC 表示同一时刻时，候选特征生成的本地风险事实仍携带 UTC
  `observed_at`，导致 v2 已完成评分却无法构造 `DecisionEpoch`、原生输入持续失败的问题。
  转换使用 `astimezone` 保持绝对时刻；仅时区表示不同的输入现在得到相同输入哈希、相同
  决策版本和相同分数，未来或无时区风险/证据仍 fail-closed，v1 正式链不受影响。

- 修复午间/跨日重启时，恢复的上一交易日 tomorrow 正式快照在当天 v2 current 尚未形成
  时被误记为影子处理错误的问题。较早交易日 baseline 现在在投影、CAS 和证据写入前按
  注入上海时钟受控跳过；未来日期、非法日期和同日真实投影异常仍失败，不会被跳过规则
  隐藏。已持久化的旧失败证据不删除、不改写，也不会被转换为成功样本。

- 修复候选 I/O 在评分轮次开始后完成时，原生边界仍使用旧轮次时间而把合法本轮特征误判
  为 future、造成 `tomorrow_native_inputs_failed` 且 v2 current 长时间不更新的问题。
  同时修复 v2 用候选增强字段覆盖全市场人口并从约 5500 股重新选候选、把候选差异混入
  评分一致性的问题；硬过滤门禁现在只比较拒绝事实，不再把可选告警、候选分/缺失、风险、
  动作和集中度跳过原因错误地与 v1 全市场过滤计数比较。
- 修复同一原生 local 已升级 hybrid 后，重复 local baseline 会被当成“当前决策不匹配”，
  以及第二个不同 hybrid 触发同 sequence 冲突并写入处理失败样本的问题。重复 local 只
  比较已经发布过的 local，不降级当前 hybrid；同输入的后到替代 hybrid 计
  `baseline_superseded`，不覆盖决策、不重复评分也不污染切换样本。

- 修复影子门禁只把“某日期出现过成功样本”当作“完整交易日”、且进程重启丢失全部证据仍
  可能重新累计并误报资格的问题。恢复后的内存窗口与持久层使用相同幂等身份和容量；
  跨日迟到失败仍可留证但不计入完整交易日；非法冻结哈希、同身份同时间冲突、载荷或
  manifest 篡改均拒绝进入离线合格报告。

- 修复 tomorrow v2 工程影子被 v1 全部评分和 P6 串行阻塞、无法独立达到 local 实时性目标
  的结构性问题。原生输入投递失败现在只记录 `tomorrow_native_inputs_failed` 并继续提交
  v1；重复输入不会重复评分或产生新 SSE 决策身份；无 `merge_epoch` 的兼容输入按实际特征
  内容生成哈希，防止同 quote 版本下的内容变化被误判为同一输入。
- 修复 Long 虽然最终分固定为 0，运行时仍经过统一准备/评分/finalize，并混入候选代码和
  共享 TopK 行情，造成无意义历史/研究工作以及与荐股流水线竞争资源的问题。进一步修复
  仅增加应用 worker 仍会共用底层腾讯 lane、熔断和缓存的问题：Long 现在拥有独立物理
  熔断身份并绕过共享缓存；其失败不会打开候选腾讯熔断，候选评分也不会等待慢 Long 请求。
- 修复 v2 组件已完成但真实组合根始终未注入查询和运行数据、导致生产进程
  `/api/v2/tomorrow/current` 只能长期 `not_ready` 且无法量化新旧链路差异的问题。当前
  接受的 tomorrow P6 会异步形成可观察 v2 current；冻结成功后额外发布同身份事件，使 Web
  不必等下一次完整刷新才能看到 frozen 状态。重复 baseline 不再虚增门禁样本，失败样本也
  不再被错误计入“100 个成功样本”；跨午夜处理的失败证据继续归属原快照交易日，不会污染
  次日样本。
- 修复新 Web 链缺失导致已形成的 tomorrow v2 决策无法被独立预览的问题；修复 overlay
  可能跨决策套用、旧 overlay 阻碍新决策报价接纳、旧交易日决策冒充当前以及冻结
  decision/frozen 分离读取的竞态。overlay 现在发布前校验当前决策身份，决策换代后使用
  新 CAS 身份接纳，查询再次按 projection 匹配，不一致时保持完整决策并要求 resync。
- SSE 显式区分无游标、游标超前、过期、不连续和慢客户端：无游标在订阅锁内从当前序列
  开始，显式游标才回放；每客户端队列有界，队列满只隔离该客户端而不阻塞 publisher。
  decision 事件只携带完整身份，overlay 只携带报价字段，浏览器遇到 schema、projection
  或身份不匹配时通过 ETag 完整重读。
- 修复 tomorrow v2 目标设计中尚缺“当前决策如何并发换版、14:50 如何封口、持久化失败
  如何重试、重启如何恢复、15:00 后如何避免伪收盘重建”的系统空白。此前仅有
  `DecisionEpoch` 生成能力，没有可执行的单提交者、正式记录提交顺序或独立恢复边界；本批
  通过 CAS、封口状态、运行身份校验、不可变文件和唯一交易日 manifest 将这些行为变成
  可验证契约，且没有接回旧 P6、旧运行库或 HTTP 请求路径。

- 修复 tomorrow v2 旁路组装未消费 `ResearchEpoch`，会让真实 V4 facts 缺少 point-in-time
  manifest 并频繁 abstain，也会漏掉当日新增官方公司风险的问题；研究历史不完整时现在
  只允许新增风险并标记覆盖不足，不能清除昨日已确认事实。另区分
  `deepseek_skipped_no_eligible_candidates` 与真实 incomplete，避免未发生请求却误报模型
  失败。融合前现在还核对当前 evidence manifest 哈希并把供应商 UTC 完成时间规范为上海
  时区，防止旧证据缓存或错误时区结果进入新决策；适配器内部异常统一转换为受控不可用，
  避免数据库或协议异常越过应用降级边界。`abstain` 只保留审计、不能映射模型风险或
  veto，仅含拒绝/迟到结果也不能创建伪 hybrid；晚于决策时点的风险事实会被拒绝。固定
  行业和板块集中度同样不能被运行配置放宽。
- 修复 tomorrow v2 初版设计中两个会降低过滤准确性的缺口：当日尾盘/入场字段不再被迫
  从 `data_as_of < trade_date` 的日基线读取而退化为中性 50；候选实时特征不再接受任意
  因子键，因而不能用高频来源覆盖财务恶化、严重公司风险或证券身份。另补齐低于本地门槛、
  核心缺失、候选分不足、板内容量、行业集中度和 TopK 的独立诊断，避免都显示成无可评分
  候选。候选单股来源时间早于父市场同股报价时，定向价格及其依赖该价格的实时特征整组忽略，
  不再让迟到批次覆盖更新行情。
- 数据平面现在在原子发布前拒绝无上海时区、未来事实、非有限数值、空全市场批次、空来源
  身份、报价接收早于来源时间、同 sequence 不同内容、父 epoch 或配置不匹配等输入；
  来源失败只记录有界结构化原因并保留最近有效一致视图，不会用失败或半更新状态清空数据。
- 修正文档把冷启动预热、三策略调度和缓存阶段当成终极需求、导致实时链路设计继续围绕
  既有架构打补丁的问题。目标态现在只保留点时输入、准确过滤、14:50 不可变冻结、只读
  Web 和真实降级等业务不变量，并明确提高收益须经不少于 250 个交易日样本外回放及连续
  20 个交易日前向影子验证，不能作为收益保证。
- 修复 tomorrow 待处理板块任务被同轮 d25 覆盖后，三个板均以 `RuntimeError` 降级并长期
  保留早先空快照的问题；同策略新 epoch 仍只替换本策略旧待处理任务，不会跨策略清空。
  修复持续 tomorrow 输入可能饿死 d25/today 的边界，并统一候选预选与正式评分对行情
  `merge_epoch` 的使用。5,500 行、三板、三策略并发基准从约 30.3 秒降至约 4.3 秒，
  低于 15 秒评分发布期限。
- 修复 DeepSeek 传输失败仍会立即执行一次 HTTP 重试、使单次复核放大预算和尾延迟的问题；
  连接失败、429、5xx 和超时现在直接本地降级，只有 HTTP 200 且严格 schema 非法时允许一次
  结构修复，所有真实请求仍先原子计数。修复 tomorrow/d25 上午固定显示
  `deepseek_deferred_until_afternoon` 的旧行为，上午本地快照统一标记为可继续增强的
  `deepseek_pending`。
- 修复盘后恢复把 today 与 tomorrow/d25 统一视为可补算策略，导致错过 11:20 后仍在
  15:00 生成“今早推荐”的契约错误；today 缺失不再让盘后恢复无限重试，历史遗留的同日
  today `close_fallback` 也不会进入当前/正式视图。修复 `view=current` 绕过冻结判断而在
  11:20/14:50 后展示残留草稿的问题；显式 `view=live` 仅保留诊断兼容。
- 修复 closing overlay 已由并发/重试成功固化时，仓储幂等返回 `False` 被上层误记为
  `closing overlay persistence failed` 的假错误；真正缺失 closing 记录或冲突仍保留错误诊断。
- 修复长期页只能逐个切换行业并查看股票明细、无法从 tab 快速比较行业整体涨跌方向的观察
  盲区。均值只计算有限数值行情，缺行情不再可能被误作 0%，真实 0% 正常计入；整组无有效
  行情显示 `--`，避免把数据缺失伪装成行业平盘。
- 修复用户看到 11:20 后今早页面仍像普通当前推荐、无法判断行情是否继续更新的问题。调查
  确认后台一直为冻结快照发布实时 overlay，缺口在 Web 将同日冻结 today 继续路由到普通
  九列表格，导致已有锚点字段和锚点至今变化未被渲染。现在专用七列表和详情同时展示稳定
  锚点与变化中的当前行情；`close_fallback`、显式历史和其他策略不会误进入该模式，新交易日
  仍由查询身份拒绝上一日快照。
- 快照原语 `_review_mapping()` 原先经 `snapshot_items` 间接取得实际定义在
  `snapshot_review_items` 的 `_review_from_dict`，形成隐藏依赖；现在保留避免循环导入所需
  的局部导入，但直接依赖真实所有者模块。`pyproject.toml` 的 Ruff 注释同时从不存在的
  `make lint-strict` 修正为实际严格检查入口 `make lint`。
- 修复用户反馈的“今早无荐股、长期股票实时信息不显示”。根因分别是评分阶段把全市场总体
  当成候选集合重复计算，慢于 3 秒 cadence 并在最后按候选代码过滤成空；输入完成触发与
  周期评分重复排队，旧评分大量过期；long 约 435 KiB 的当前投影超过所有策略共用的
  160 KiB 上限而被 P6 拒绝。现在 versioned DAG 只由已完成输入触发评分，尚未开始的旧
  评分采用 latest-wins；候选与总体分离，long 使用仍有界的独立上限，超限错误包含实际值
  与限制值。进一步核对用户业务要求后，确认 tomorrow/d25 上午无数据不是预期行为：阶段
  路由和动作门此前只允许 afternoon/final 阶段。现在 09:30-11:20 同步生成两策略本地草稿，
  13:00 后再增加尾盘分钟数据与 DeepSeek 增强复核；仍不把上一交易日冒充当前。
  若服务在 11:20-13:00 冷启动且上午草稿缺失，tomorrow、d25 和 long 允许各补一次本地
  当前快照，已有同日快照后停止午间重复评分，增强复核仍不会提前到 13:00 前。
- 修正文档与活动实现不一致或表述过宽的问题：旧文档漏写 today 活动评分中的三日相对强度，
  把旧通用 d25 “不过热”组件写成活动路径，把可选主数据告警写成统一硬过滤，把
  收盘补算写成退役的 `close_fallback_observation_floor_relaxed` 而非活动
  `close_fallback_observe_floor`，并要求 Web 分栏展示实际只在历史/补算返回的观察项。
  同步澄清板块人口不足才阻断收盘固化、可靠度不足只降为观察，以及
  `PublishedSnapshotIndex` 的 64 个驻留视图与共享缓存 72 条预留容量互不替代。
- 修正文档把性能命令和浏览器验收采集项误写成 `/api/status` 在线承诺的问题。在线状态现
  只记录代码真实暴露的聚合延迟、缓存、周期、worker、P6、publisher、DeepSeek 最近批次及
  预算统计；RSS/USS、列式字节、patch 传输和浏览器应用时间明确归入离线性能或桌面验收证据。
- 修正权威文档仍把活动结果审计描述为旧 v18、把收益晋级指向外部计划文件，以及无法从
  当前文档直接判断 24/28 支送审上限和收益变体是否上线的问题。现在权威文本明确区分
  “代码已实施”“旧阶段值已取代”和“尚未实现”，并由契约测试确保三份来源记录退役后
  两份权威文档仍完整承载相应事实。
- 修复快照状态区域可能显示 `Cannot read properties of undefined (reading 'projectionVersion')`
  的前端启动错误。根因是拆分后的 `dashboard_patches.js` 被主 `dashboard.js` 作为硬依赖使用，
  当浏览器缓存、模板版本或静态资源加载出现不一致时，`window.TraderDashboardPatches` 可能缺失。
  现在模板统一加载补丁模块和主模块的同一资源修订号，主模块在补丁模块缺失时使用降级补丁对象：
  完整推荐快照仍可渲染，SSE patch 会触发 resync，诊断中记录
  `dependency_missing:TraderDashboardPatches`，不再让页面抛 undefined。

- 用户看到 `TopK live overlay degraded: ... batch deadline`、板块可靠度和公司风险英文原因，
  并反复遇到 Web 无荐股。确认 TopK 超时本身只影响报价 overlay，不是“行情差导致零荐股”
  的直接证据；无数据必须结合 P6 是否产生、正式/观察数量和选择诊断判断。现在页面对全部
  已登记原因、板块前缀、融合模式、风险码和最近错误显示简短中文，未知英文使用中文兜底，
  原始值只保留在最多 20 条的浏览器诊断中。
- 修复 TopK 与最长 `close_quotes` 共用主事件队列造成的排队超时，以及约 31.3 秒总耗时无法
  区分排队和业务执行的问题；TopK 现可在主全市场事件阻塞时独立更新，周期同时暴露
  `queue_wait:<cycle>`、`execution_total:<cycle>` 和 `cycle_total:<cycle>`，收盘链路进一步
  拆分恢复、P6、全市场、完整性、预选、逐策略、long 和结算耗时。
- 修复收盘重试因历史 `last_error` 否定当前已通过完整校验的收盘缓存而重复抓取全市场，
  以及部分行缺少 merge epoch 仍可能被误判为同批次的问题；缓存命中与未命中现有独立计数，
  完整命中路径远程全市场请求为零。修复被拒绝的 TopK 调度仍误计“已提交”，并避免把候选
  行情 3 秒截止时间用于后续评分，防止来源事件过期后已经发布结果的边界竞态。
- 修复历史预热失败批次被回调立即递归重提、数分钟制造百万级计划/失败计数并挤占实时行情
  线程的问题。失败代码现在按 60/120/240/480/900 秒逐股退避，未尝试代码继续推进，成功
  或移出 universe 后清理状态；历史 entries 读取移出 warmup 锁，状态额外暴露冷却数、唯一
  失败代码数和最近重试剩余秒数。
- 修复 Web 把所有正式空结果统一误报为“未通过下行保护”的问题。空结果现在来自评分后的
  选择诊断，TopK overlay 超时仍作为独立数据降级展示，不再被描述为无荐股的直接原因；
  本批未降低分数、可靠度或风险门槛，也未强制凑股。
- 修复运行态已有完整选择诊断、但冻结记录经 `PublishedSnapshotIndex` 压缩后把整个
  `metadata` 清空，导致推荐 API 重启后退回 `diagnostics_unavailable` 的跨层丢失。
  轻量发布视图现在只白名单保留 `selection_diagnostics`，评分证据和内部元数据仍不向
  Web 泄露；回归同时覆盖启动预载和运行中发布。
- 修复本地评分必须等待 DeepSeek 终态才进入 P6 的发布顺序；复核失败、超时或空响应现在
  保留已经接纳的 local 快照，迟到 hybrid 继续受冻结 compare-and-set 保护。
- 修复行业缺失股票被同行算法归入单一 `unknown` 后执行两两比较的二次复杂度；活动板内
  评分不再构造同行/领先差，5500 行全行业缺失场景保持线性横截面处理。
- 修复策略语义更新后仍复用旧重放版本的问题；活动冻结输入升级为 v19，缺少
  `projection_stage` 的 v14-v18 历史快照继续沿用旧 snapshot ID、竞争组限制、行业宽度
  突破条件和元数据结构，避免不可变历史因新策略上线而无法校验。

- 用户指出此前只在 `_fail_event` 外包异常并没有解决数据库错误。重新调查确认运行库本身
  `quick_check=ok`，真正的写放大来自每个流水线事件的 reserve/running/terminal 三次审计写、
  高频来源健康 upsert，以及每次新连接都执行写性质的 `PRAGMA journal_mode=WAL`；历史缓存
  另有 360 只、32,235 行，但不应属于运行数据库路径。现在常规连接只设置 foreign keys 和
  busy timeout，WAL 仅初始化一次；事件/健康完全不落库，历史 SQLite 路径被移除，因此失败
  事件不再递归触发第二次数据库写错。

- 用户反馈“已经重启了，2-5 日还是没数据”。现场确认 d25 API 已是
  `ready + close_fallback + frozen=true`，但 `items=[]`；真实 replay input 合并后有 192 个
  本地候选，最高 68.49 分，而活动 d25 阈值为 76、观察窗口 5 分，观察 TopK 最低需 71 分，
  因此旧逻辑把降级观察池筛成空。现在 close fallback 在正常 TopK 空池时保留本地观察行；
  同一 2026-07-23 旧空冻结记录经只读 replay 可返回 7 行 observe。

- 用户反馈“Web 上还是没数据”，现场复核默认 5000 端口确认 API 已有
  today/tomorrow `ready + close_fallback` 数据，但返回项的动作都是 `observe`；前端
  `visibleRecommendations()` 对当前短线策略只保留 `action === "executable"`，导致有数据的
  收盘补算结果被 UI 过滤成空表。现在 close_fallback 不再过滤观察项，真实 API payload 经新
  选择逻辑验证 today 可见 5 行、tomorrow 可见 3 行。

- 用户补充最近错误 `d25 freeze unavailable: no current pre-cutoff snapshot`。该信息只表示
  14:50 前没有可冻结草稿，15:00 后应由收盘补算接管，不应作为错误压到 Web 最近错误栏。
  现在该场景只计数不写 last_error。

- 用户补充最近错误 `outcome settlement degraded: SourceRequestSupersededError`。这是后台结算
  读取行情时被 latest-wins source lane 的新请求取代，不代表推荐生成失败；现在该场景不再写
  last_error，避免遮挡 D25 研究字段缺失等真正诊断。

- 用户明确反馈“明日、2-5 日、长期三组都没有数据”。现场复核显示明日 API 已有
  `ready close_fallback` 但 Web 过滤掉观察项；d25 API 本身因 `growth_score`、`quality_score`、
  `value_score` 缺失触发板块可靠度硬阻断；long 在 15:00 后启动时没有盘后当前补算路径，worker
  提交数为 0。现在 Web 显示 close_fallback 全部 TopK，d25 可靠度不足改为降级观察项发布，
  long 盘后按 watchlist 收盘价生成当前非冻结快照。

- 用户反馈“你改的什么，Web 上还是没有数据”，并提供最新错误
  `entry_quality` 导致三板可靠度低于阈值。现场复核确认根因不是 8.5/0.85 阈值过高：
  当前可用行情源 Sina 不提供 `volume_ratio`，但收盘补算已有点时成交额和 5 日历史成交额，
  旧逻辑仍把 `volume_to_5d_average` 置空，进而让 tomorrow/d25 的 `entry_quality` 缺失并压低
  板块可靠度。现在补上成交额强度派生路径，Sina 场景下可恢复 `entry_quality` 输入。

- 继续实机验证发现第二个链路问题：修复 `entry_quality` 后，D25 仍可能因
  `growth_score`、`quality_score`、`value_score` 研究字段缺失而保持降级；旧收盘重建对
  today/tomorrow/d25 采用整批失败返回，导致 D25 一个策略失败会让已生成的 today/tomorrow
  也不提交，Web 三个策略看起来都为空。现在收盘重建改为逐策略降级、逐策略提交，D25
  缺研究字段时不会阻止 today/tomorrow 创建 `close_fallback`。

- 全量门禁暴露 DeepSeek 全局 168 次预算并发测试稳定失败：16 个线程同时对同一
  `DeepSeekBudgetLedger` 预留请求时，SQLite 写锁竞争会在 `BEGIN IMMEDIATE` 阶段抛出
  `database is locked`，导致原子预算用例中断而不是返回 168 个允许和剩余拒绝。现在预算写入口
  使用实例级写锁串行化本进程写事务，回归确认并发预留结果稳定为 168 次允许，其余为
  `daily_hard_limit` 拒绝。

- 用户反馈“15:00 之后运行，使用收盘价还是得不到荐股信息，Web 展示为空”。现场检查确认
  v17 运行库没有当日冻结或发布快照，`freeze` 事件成功只表示没有可冻结的预截止 P6，
  真正负责冷启动收盘补算的 `close_quotes` 多次因 60 秒执行 deadline 过期而失败；当时
  Sina 收盘来源 P95 已接近 58 秒，Eastmoney 熔断，重试又会重新抓取全市场，导致
  `close_fallback` 始终没有创建。现在延长 `close_quotes` 预算，并在完整缓存可复用时跳过
  后续慢全市场抓取，使 15:00 后后台恢复能继续本地筛选、评分、TopK 和冻结写入。

- 用户追问“15:00 后没有荐股是否因为 8.5 阈值太高、如何修复”。确认根因不是降低阈值，
  而是冷启动收盘补算只读路径没有恢复已落盘的结构化研究字段，且 `FinancialReport.report_date`
  以日期字符串写入缓存却按带时区 datetime 反序列化，导致 `quality_score`、`value_score`
  和 `growth_score` 在重启后保持 `null`，D25 等候选可靠度被压低并触发
  `board_data_reliability_below_threshold`。现在修复 structured research 磁盘缓存只读恢复和
  财务报告日期反序列化，字段存在时可恢复候选可靠度；字段仍缺失时继续按契约拒绝冻结并
  保留诊断。

- 针对用户反馈“15:00 后当天没有荐股、明日/2-5 日仍显示板块可靠度降级”补齐收盘恢复边界：
  收盘补算使用收盘时刻重新校验报价年龄，三板评分改用全市场样本后再过滤候选；组件可靠度按已知输入比例计算，
  盘后冷启动不把天然缺失的尾盘分钟字段作为永久阻断，入场质量不再因可选行业宽度缺失而整体置空。
  收盘补算只读取候选缓存，不再同步抓取 AkShare 研究；板块人口或可靠度阻断时拒绝冻结并保留重试诊断。
  新增延迟报价、历史样本、全市场板块、缓存候选、可靠度和冻结回归测试。

### Verification

- 定向 Web/API、启动调度、历史预热、东财来源与 JS 提示回归通过；`make format-check`、
  `make lint`、`make type-check`、`make test`（1174 项）和 `make package` 全部通过，严格重构债务
  为 0。仓库外 `/tmp` target 安装 wheel 后确认从安装目录导入、`trader-cli --help` 可执行，
  两个模板、CSS、JavaScript 和 SVG 资源均存在且非空。
- Chrome 桌面门禁在 1280x720、1440x900、1920x1080 的当前与 long 视图均非白屏、无页面级
  横向溢出或关键区域重叠，`browserErrors=[]`；24 个 patch-to-paint 样本 P95 为 17.8ms，低于
  100ms 预算。首次紧接全量测试和构建的采样为 821ms，系统空闲后按相同门禁复跑通过。

- 收盘恢复、推荐终结和正式投影 3 项定向回归通过；在 `HEAD` 加本任务完整 diff 的隔离副本中，
  `make format-check`、`make lint`、`make type-check`、`make test`、`make package` 全部通过，严格
  重构债务为 0；仓库外安装 wheel 后包导入、`trader-cli --help` 及模板、CSS、JavaScript、图标读取通过。
- 当前含用户既有未提交改动的工作树中，全量门禁仍被非本任务文件阻断：
  `tests/contract/test_project_records.py` 需要格式化，`bootstrap_builders.py` 存在 4 个 mypy 错误且超过
  800 行，相关 `/api/v2/status` 契约有 5 项失败；本任务文件的 Ruff、mypy、定向回归和 diff check 均通过。

- P8 收口验证：`.venv/bin/pytest tests/contract/test_project_records.py tests/integration/test_v2_shadow_cutover.py tests/integration/test_v2_pipeline.py::test_started_pipeline_routes_stages_to_bounded_workers_and_isolates_long tests/unit/application/test_tomorrow_native_pipeline.py tests/unit/application/test_tomorrow_fusion.py tests/unit/application/test_tomorrow_shadow.py tests/unit/application/test_tomorrow_freezing.py -q`
  通过；复核了 native input 手递手、独立 shadow 运行时、冻结封口、重启恢复和 `83.40`
  融合契约。  

- P7 定向验证：`.venv/bin/pytest tests/unit/test_v2_market_data_field_quality.py tests/unit/test_v2_market_data_merge.py tests/component/test_v2_market_data.py -q`
  通过，共覆盖 174 项字段级选择、统一行情合并、实时路由、候选报价回退和来源健康用例。首次
  直接执行 `pytest ...` 因当前环境未安装全局 `pytest` 失败，已改用仓库 `.venv` 执行。

- 本批 P6 完整验证：`make format-check`、`make lint`、`make type-check`、`make test` 均通过；
  `make package` 首次在沙箱内因 setuptools 代理访问受限失败，提升权限后通过。P6 定向回归
  `tests/unit/infra/test_cninfo_incremental.py`、三项 `tests/component/test_v2_market_data.py`
  研究数据平面用例、`tests/contract/test_v2_source_capability.py` 与两项
  `tests/contract/test_v2_bootstrap.py` 启动恢复用例共 12 项通过。仓库外 wheel
  `--target --no-deps` 安装后，已在 `/tmp` 工作目录验证 `trader` 可导入、8 项 Web 模板/静态资源
  可读取、`trader.entrypoints.cli --help` 可执行；完整带依赖临时安装因上游包 hash mismatch
  停止，未作为产品行为失败处理。

- 本批专项验证：`./.venv/bin/pytest -q tests/component/test_v2_market_data.py tests/contract/test_v2_bootstrap.py tests/contract/test_project_records.py tests/unit/infra/test_data_plane.py tests/unit/test_data_plane_migration.py`
  全部通过；`./.venv/bin/ruff check` 与 `./.venv/bin/mypy` 对新增/修改模块全部通过。

- 本批专项回归新增：
  `tests/component/test_v2_market_data.py::test_research_loader_recover_from_data_plane_overrides_component_statuses`
  、`tests/component/test_v2_market_data.py::test_news_research_does_not_persist_risk_components`
  、`tests/component/test_v2_market_data.py::test_research_data_plane_persistence_unavailable_does_not_block_research_load`
  与 `tests/contract/test_v2_bootstrap.py::test_reference_data_plane_recovery_initializes_data_plane_and_loader`
  等。

- P4 交付本地验证：执行
  `pytest -q tests/unit/infra/test_data_plane.py tests/unit/test_data_plane_migration.py tests/component/test_v2_market_data.py tests/contract/test_v2_bootstrap.py tests/contract/test_project_records.py`
  全部通过。
- 本批专项复核：`tests/contract/test_v2_bootstrap.py` 覆盖启动初始化顺序与非阻塞恢复；
  `tests/component/test_v2_market_data.py` 覆盖 `ReferenceLoader` 恢复回放与数据源不可用隔离；
  `tests/contract/test_project_records.py` 覆盖未开始章节计数与文档计数一致性。

- 本批新增并复核：`tests/component/test_v2_market_data.py::test_research_loader_recover_from_data_plane_overrides_component_statuses`、
  `tests/component/test_v2_market_data.py::test_news_research_does_not_persist_risk_components` 与
  `tests/component/test_v2_market_data.py::test_research_data_plane_persistence_unavailable_does_not_block_research_load`，
  验证了 P6 第一期风险组件持久化、news 非持久化及持久化降级行为；`make format-check`、
  `make lint`、`make type-check`、`make test`、`make package` 在本批已全部通过。

- 本批新增 `tests/unit/test_data_plane_migration.py` 与 `tests/unit/infra/test_data_plane.py`（8 项）
  全部通过；`./.venv/bin/ruff check`、`./.venv/bin/mypy`（覆盖本批新增 5 个文件）也全部通过。

- 本批验证范围：`tests/unit/test_v2_market_data_field_quality.py`（11 项）、`tests/contract/test_v2_source_capability.py`、
  `tests/contract/test_project_records.py` 与 `tests/unit/test_v2_market_data_merge.py`（含旧 merge 兼容回归）；
 `make format-check`、`make lint`、`make type-check` 全部通过；`make test` 在当前仓库状态下发现 2 个
  与本批无关的既有关闭流程回归失败（`tests/integration/test_graceful_shutdown.py`、`tests/unit/application/test_runtime.py`）；
 `make package` 因隔离构建环境访问代理受限失败。完整的构建/仓库外安装未在本次完成内复现成功。

- `tests/contract/test_project_records.py` 通过：`test_docs_keep_two_authorities_and_pipeline_reports`
  验证 `docs/V2_plan.md` 现有章节数、两份权威文档引用、`reports` 基线文件清单
  （含 `reports/v2-p0-baseline.md`）和其他契约约束一致。

- 本批 Review 以当前组合根、路由、运行配置、市场适配器、tomorrow v2 影子/证据实现、
  权威迁移状态和相关契约/集成测试为依据；确认活动树无 `stock_analyzer` 导入，
  `/v2/tomorrow` 仍为并行入口，交易所、巨潮、BaoStock 和 mootdx 适配器尚未实现。
  文档治理定向测试 19 项通过；`make format-check`、`make lint`、`make type-check`、
  完整 `make test` 和 `make package` 均通过。仓库外安装 wheel 后可导入 `trader`、执行
  `trader-cli --help`、读取 8 项模板/静态资源且 `pip check` 无断裂依赖；本批无 UI
  或运行行为变化，桌面浏览器实机验收不适用。

- 文档 Review 已逐项核对合并前的数据源、主备路由、字段级合并约束、接入顺序和来源链接，
  确认九类来源均保留且只在新方案第二节定义一次。文档治理契约测试同步覆盖 `V2.md`
  的非生产状态和权威文档指向。`make format-check`、`make lint`、`make type-check`、
  完整 `make test` 和 `make package` 均通过；仓库外安装 wheel 后可导入 `trader`、执行
  `trader-cli --help`、读取 8 项模板/静态资源且 `pip check` 无断裂依赖。本批无运行代码、
  配置或 API 变更，桌面 UI 验收不适用。

- 长期固定池定向验证通过：配置加载与 Web 资产契约、long 行情投影/API 的
  `score_status=not_applicable` 回归共 105 项通过；确定性静态资产检查通过。配置审计确认
  224 个代码全部唯一且恰好归属一次、50 个分组无遗漏，37 个卡脖子分组和 8 个高成长分组
  均未超过每组 5 只上限，12 个新增代码均由腾讯免费实时行情端点返回有效证券记录。
  `make format-check`、`make lint`、`make type-check`、完整 `make test` 和 `make package`
  全部通过；仓库外全新虚拟环境安装 wheel 后可导入包、执行 CLI/`validate-config`、读取
  8 项模板与静态资源且 `pip check` 无断裂依赖。无头 Firefox 三档桌面验收通过，37 个
  卡脖子子 tab 在 1280x720、1440x900、1920x1080 均无白屏、页面级横向溢出、面板错位、
  tab 内容溢出或浏览器错误，patch-to-paint P95 为 22ms。

- 本批回归覆盖：有效决策后临时空输入保持同一对象且 SSE sequence 不变；冷启动临时空
  返回 `not_ready`；原生与 baseline-only fallback 都不能发布无效空集；ST 等真实业务
  空集仍可发布；候选风险/板块原因按显式候选而不是 5533 行人口计数。市场组件覆盖东财
  上市日期/板块/交易所解析、东财失败转新浪仍保留低频主数据、Tushare 高优先级不被覆盖、
  refresh-due 历史复用及动作限制。
- 本批 `make format-check`、`make lint`、`make type-check`、完整 `pytest tests` 和
  `make package` 通过；隔离构建在沙箱内因代理网络权限失败，批准联网后 wheel/sdist 构建
  成功。仓库外目标目录确认从 wheel 导入 `trader`、`trader-cli --help`、
  `validate-config` 及 8 项模板/CSS/JavaScript/SVG 资源。主看板和 V2 页面在
  1280x720、1440x900、1920x1080 均无浏览器错误、页面级横向溢出或面板重叠；V2 SSE
  overlay 更新未增加完整 current GET。

- 公司研究调度定向回归覆盖：300 次重复意图只执行 1 批；6 只股票按每批 2 只执行时，
  首批全失败后只产生 1 个研究批次并短路剩余 4 只；60 秒后再次失败进入 120 秒退避；
  部分覆盖与完整覆盖分别进入重试和成功冷却；生产 cadence tick 不再直提研究；本地新
  正式/观察集合只触发一次研究，并通过首轮研究屏障避免重复 DeepSeek 评审；研究全失败
  同样释放屏障、触发一次降级重评分并继续模型终态。
- 本批 `make format-check`、`make lint`、`make type-check`、`make test` 和
  `make package` 全部通过；隔离构建首次受沙箱代理权限限制，批准联网后生成 wheel/sdist。
  仓库外 wheel 安装已验证 `trader` 导入、`trader-cli --help` 以及首页/V2 模板、CSS、
  JavaScript 和图标资源。Firefox/geckodriver 在 1280x720、1440x900、1920x1080 对首页
  与 V2 页面验收均通过，无浏览器错误、页面级横向溢出或面板重叠。

- `make format-check`、`make lint`、`make type-check`、`make test` 和 `make package` 通过；
  全仓测试覆盖架构 AST、`create_app()` 无副作用、固定融合向量 83.40、预算并发、SSE、
  冻结恢复、哈希一致性和本批免费全市场路由回归。隔离构建首次因沙箱禁止访问本机代理
  失败，批准联网后成功生成 wheel 与 sdist。
- 从仓库外将 `trader_research_dashboard-0.2.0-py3-none-any.whl` 安装到临时 target，
  已从该安装路径导入 `trader`、执行 `trader-cli --help`，并读取首页/V2 模板、
  dashboard CSS/JavaScript 和 SVG 图标资源。Firefox 真浏览器门禁在 1280x720、
  1440x900、1920x1080 三档均通过：旧看板和 tomorrow V2 均无浏览器错误、页面级横向
  溢出或面板重叠；旧看板 patch-to-paint P95 为 36ms，预算 100ms。

- 生命周期专项联合回归：
  `tests/integration/test_start_stop_integration.py`、
  `test_startup_scheduling.py`、`test_graceful_shutdown.py` 和
  `tests/component/test_freeze_crash_recovery.py` 共 67 项通过；包含 SIGINT/SIGTERM
  子进程、第二信号、绝对期限和持久化 kill-point 场景。
- 最终 `make format-check` 覆盖 318 个文件，Ruff 与严格重构债务均为零诊断，mypy
  211 个活动源码文件和完整 1113 项 pytest 通过；架构、`create_app()` 无副作用、
  83.40 融合向量、预算并发、SSE 游标/慢客户端及冻结恢复/哈希契约均包含在全量门禁。
  sdist/wheel 构建成功，最终 wheel 在仓库外全新虚拟环境安装全部声明依赖后
  `pip check` 无破损，从 `site-packages` 导入 `trader`，`trader-cli --help`、
  `validate-config` 及模板、CSS、JavaScript、SVG 共 17 项资源通过。主看板、long 看板和
  tomorrow v2 在真实 headless Firefox 的 1280x720、1440x900、1920x1080 三档均通过，
  无页面级横向溢出或浏览器错误，tomorrow overlay 未触发额外完整 current GET。

- 本批新增/更新组件、契约、集成、原生 tomorrow 冻结和 JavaScript 状态机回归，覆盖
  观察代码及逐股元数据不进入文件、checkpoint/freeze/overlay 双层投影、旧冻结读取过滤、
  outcome 只包含正式代码、today 午间关闭、tomorrow/d25 午间开放、冻结/历史摘要和
  `close_fallback` 正式空记录。通过 `make format-check`、`make lint`、`make type-check`、
  `make test` 和 `make package`；仓库外安装 wheel 后验证包导入、`trader-cli --help`、
  `validate-config` 以及模板、三份 CSS、四个本批 JavaScript 和图标资源。稳定版
  Firefox/Geckodriver 在 1280x720、1440x900、1920x1080 均确认冻结观察池隐藏且 DOM
  观察行清空、无横向溢出、无浏览器错误；SSE patch-to-paint P95 为 18ms（预算 100ms）。

- 本批 failure-first/聚焦回归已覆盖：快股票完成、慢股票延后且不整批回滚；迟到股票不进入
  本批研究观察；协调器在途任务不被新优先代码取消；财务/质押/解禁 6 小时与公告 10 分钟
  分项缓存；相同研究版本不重复触发评分；19:30 冷启动仍创建允许的 `close_fallback`，
  运行中 19:30 调度仍提交公司研究；推荐 API 独立返回四项风险研究覆盖。通过
  `make format-check`、`make lint`、`make type-check`、`make test` 和 `make package`；
  仓库外安装 wheel 后已验证包导入、`trader-cli --help`、`validate-config` 及模板、CSS、
  JavaScript、图标资源。稳定版 Firefox/Geckodriver 在 1280x720、1440x900、1920x1080
  通过主看板和 long 布局、无横向溢出、无浏览器错误，patch-to-paint P95 为 13ms（预算
  100ms）。

- 本批通过 `make format-check`、`make lint`、`make type-check`、`make test` 和
  `make package`；全仓 pytest 覆盖架构 AST、`create_app()` 无副作用、固定融合结果
  83.40、预算并发、SSE 游标/慢客户端、冻结恢复与哈希一致性。仓库外安装构建 wheel 后
  已从外部 `site-packages` 导入 `trader`、执行 `trader-cli --help` 与
  `validate-config`，并读取模板、三份 CSS、四份本批 JavaScript 和图标共 9 项资源。
  离线 Firefox/Geckodriver 桌面门禁在 1280x720、1440x900、1920x1080 均通过，正式表和
  6 行观察池可见、无页面级横向溢出、无浏览器错误，SSE patch-to-paint P95 为 44ms。

- 结算领域/应用 8 项、持久化 outcome 2 项通过；盘中取消重试回归连续 12 次通过；三板
  v1/v2 语义回归通过。`perf-check market-data` 连续三轮全部通过，P95 分别为
  `168.0/404.8/669.5/77.6ms`、`173.7/408.1/597.6/92.1ms` 和
  `140.0/386.5/596.1/72.7ms`；`perf-check end-to-end` 的 tomorrow 原生投影 P95 为
  `1396.7ms < 5000ms`，全部离线且物理网络调用为 0。
- `make format-check/lint/type-check/test/package` 全部通过：零重构债务、202 个源码文件
  mypy、1015 项测试及 sdist/wheel 均通过。实际 `trader-cli perf-check --suite all`
  的 18 项绝对预算、相对回归、零网络和 100 tick 内存增长全部通过，其中行情
  `110.8/341.1/558.4/74.3ms`，tomorrow 原生投影 `1358.0ms`。仓库外 Python 3.14
  从 wheel 路径导入包、执行 `trader-cli`/`validate-config`、读取 8 项模板/静态资源并
  `pip check` 通过；Firefox 主看板与 tomorrow v2 在三档桌面均无白屏、横向溢出、重叠
  或浏览器错误，主看板 patch-to-paint P95 为 18ms。

- failure-first 回归先因缺少同语义过滤比较入口而在收集阶段失败；实现后，原生输入哈希
  派生的 market/candidate 审计身份、批次就绪水位、只剔除 v1 `history_warming`、真实
  原因/计数差异继续失败均通过。5500 条全市场、360 条候选的固定直投影三次为
  1.942/1.766/1.512 秒，最大值低于 5 秒且三次决策版本一致；该负载只验证工程耗时和
  确定性，不代表真实收益。
- 本次修复前的 v30 阶段已通过架构 AST、`create_app()` 无副作用、固定融合 83.40、
  SSE、冻结与证据哈希专项 62 项；当时唯一失败的 benchmark-unavailable settlement
  回归现已由本批实现并纳入上述 1015 项全绿结果。
- Firefox 在 1280x720、1440x900、1920x1080 分别生成
  116739/130080/137523 字节截图，无白屏、横向溢出、面板重叠或浏览器错误，overlay 更新
  未增加完整 current GET。本次修复前固定 5500×360 性能曾有行情绝对红项，现已由上述
  18 项全绿结果闭合。

- failure-first 回归在修改前稳定复现现场
  `ValueError: decision risk observed_at must use Asia/Shanghai`，并证明未来风险事实此前
  未在原生输入边界拒绝；实现后，UTC/上海输入哈希与 `DecisionEpoch` 版本一致，风险事实
  均为上海时区，未来风险和证据均拒绝。tomorrow 原生输入、同批选择、融合、冻结、当前
  索引、证据持久化和 v2 Web 定向回归共 81 项通过，定向 Ruff 与 mypy 通过。
- 5500 条全市场、360 条候选的固定构造负载在 Review 初版全人口深拷贝时约
  430.8ms；收窄为“全人口规范身份/校验、候选深层转换”后为 131.6ms，且相关决策哈希完全
  一致。该数据用于防止本批时区修复扩大 v1 前置时间，不代表真实行情或收益结果。
- `make format-check`、`make lint`、严格重构债务零基线和 202 个源码文件 mypy 通过。
  全仓收集 1012 项，只有用户开始前已有的 benchmark-unavailable settlement 新断言失败；
  仅排除该断言后其余 1011 项通过。`make package` 首次仅因沙箱禁止访问本机 pip 代理失败，
  获准后成功生成 sdist/wheel；仓库外 Python 3.14 从 wheel 路径导入包、执行 CLI/绝对配置
  v29 校验并读取 8 项模板、CSS、JavaScript 和 SVG 资源。Firefox 在 1280x720、
  1440x900、1920x1080 分别生成 116775、130158、136482 字节截图，无白屏、横向溢出、
  面板重叠或浏览器错误，overlay 更新未增加完整 current GET。
- 固定 5500×360 离线 `perf-check --suite all` 两次均保持零网络、零相对失败、100 tick
  分配增长 0%；第一次 13/16 项绝对指标通过，第二次 14/16 项通过。未被本批修改的
  `market_merge`/`targeted_overlay_commit` 在复跑中约为 670.0/121.3ms，仍高于
  600/100ms 旧门槛；`canonical_snapshot` 从首轮 904.5ms 波动到复跑 853.4ms 并通过，
  本批没有放宽门槛或把部分通过写成全性能门禁通过。

- 第 2.11 节定向回归覆盖上一交易日 frozen baseline 受控跳过、未来日期继续失败、旧不完整
  日错误与最新完整日隔离，以及 SQLite 离线报告保留 3 条跨日审计但只用最新完整日 2 条
  样本得到 eligible；相关 application/infra/component/integration 共 30 项通过，定向
  Ruff 与 mypy 通过。
- `make format-check`、`make lint`（严格重构债务为零）和 202 个源码文件 mypy 通过；
  全仓收集 1010 项，除用户开始前已有的 benchmark-unavailable settlement 新断言外统一
  1009 项通过。架构 AST、`create_app()` 无副作用、固定融合 83.40、SSE 游标/慢客户端、
  冻结恢复和持久化哈希专项 81 项通过。sdist/wheel 构建成功，仓库外 wheel 完成包来源、
  CLI、v28 配置、8 项模板/静态资源和依赖完整性验收；Firefox 三档桌面无白屏、溢出、
  重叠或浏览器错误，overlay 未增加完整 current GET。
- 固定性能的 board-scoring、api-sse 和 end-to-end 三套分别通过，100 tick 内存增长为
  0、网络调用为 0；全量/market-data 在运行中服务持续占用约半个 CPU 核时复跑仍有下述
  旧行情算子绝对预算波动，因此未把性能总门禁写成通过。

- failure-first 回归先稳定复现完成时间误判、全市场重新评分、过滤审计口径混用和重复
  baseline 冲突；实现后，同一非空 100 股生产策略批次的 v1/v2 入选代码、本地分和硬过滤
  计数逐项相等，观察线 72.99/73.00 边界、重复 local/hybrid、原生先投递及 v1 降级隔离
  均通过。`make format-check`、`make lint`、`make type-check` 通过，严格复杂度债务为零，
  202 个源码模块 mypy 无问题；全仓 1007 项中仅任务开始前用户已有的 outcome settlement
  新断言失败，排除该单一断言后的 1006 项通过。
- 固定 5500×360 离线 `perf-check --suite all` 零网络、零绝对或相对失败：
  `market_merge`/`canonical_snapshot`/`targeted_overlay_commit` P95 分别约
  526/661/88ms，三策略板评分约 323ms，100 tick 内存门禁通过。`make package` 在获准访问
  构建依赖后成功生成 sdist/wheel；仓库外 Python 3.14 环境从 wheel 导入包、执行 CLI 与
  绝对配置校验、读取 8 项模板/CSS/JavaScript/SVG 资源并通过 `pip check`。Firefox 在
  1280x720、1440x900、1920x1080 下分别生成 116490/129966/137499 字节截图，无白屏、
  横向溢出、面板重叠或浏览器错误，overlay 更新未增加完整 current GET。

- 本批 failure-first 用例先因证据 repository 尚不存在而按预期停止；实现后证据规范往返、
  较新身份替换、哈希篡改拒绝、缺库只读失败、完整交易日、持久化失败 blocker、启动恢复
  降级和 CLI 退出码定向回归通过。`make format-check`、`make lint`、`make type-check`
  全部通过，严格重构债务为零、202 个源码模块 mypy 无问题。共享工作树 `make test` 只失败
  于任务开始前用户已有的 outcome settlement 新断言；排除该断言后全仓通过，且从已推送
  基线叠加仅本批文件的隔离树完整 999 项 pytest 全绿。
- `make package` 首次仅因沙箱禁止访问本机 pip 代理失败，获准后成功生成 sdist/wheel。
  仓库外 Python 3.14 环境安装最终 wheel 及全部声明依赖后，`pip check`、新证据模块导入、
  `trader-cli --help`、绝对配置校验和 8 项模板/CSS/JavaScript/SVG 资源读取通过。
  Firefox/geckodriver 在 1280x720、1440x900、1920x1080 分别生成
  117396/130623/138132 字节截图，均无白屏、横向溢出、面板重叠或浏览器错误，SSE overlay
  更新仍保持完整 current GET 为 1。
- 固定 5500×360 离线 `perf-check --suite all` 两次均保持零网络和零相对回归，但在宿主
  load average 约 2.7 时，未被本批修改且不经过证据 repository 的 `market_merge`、
  `canonical_snapshot`、`targeted_overlay_commit` P95 分别约为 670-690ms、
  966-973ms、123-131ms，高于 600/900/100ms 绝对门槛；其余 13 项绝对指标和 100 tick
  内存门禁通过。本批没有修改这些旧算子或放宽预算，失败保留为发布残余风险。

- 本批 failure-first 测试先确认原生输入 API 缺失；实现后目标单元、契约、启动态流水线和
  v2 冻结集成回归通过，证明原生输入在 v1 `prepare_snapshot` 提交前送达、投递失败不阻塞
  v1、同输入不重复发布，且原生 local 与稍后 v1 replay local 的输入哈希和决策版本一致。
  `make format-check`、`make lint`、`make type-check` 已通过；完整 `make test` 仅命中本批
  开始前已有且未纳入提交的用户修改
  `test_settlement_records_due_stock_outcome_when_benchmark_is_unavailable`，排除该单测后的其余
  完整测试集通过。`make package` 成功生成 sdist/wheel；干净仓库外 Python 3.14 环境从
  wheel 导入 `trader` 和 `TomorrowNativeInput`，CLI/绝对配置校验、8 项模板/CSS/JS/SVG
  资源及 `pip check` 通过。固定 5500×360 离线 `perf-check --suite all` 的全部绝对指标、
  相对回归和零网络门禁通过；headless Firefox 三档桌面均无白屏、溢出、重叠或浏览器错误，
  overlay 更新不触发完整 current GET；最终提交和上游一致性由本批 Git 提交元数据核对。
- Long 定向单元/组件/集成回归覆盖固定顺序、无评分语义、部分报价沿用、共享 TopK 排除、
  独立 latest-wins worker、慢 Long 不阻塞 D25、Long 熔断不打开候选熔断、生产缓存启用
  时仍逐周期物理刷新，以及 v5/v6 cadence 兼容迁移。`make format-check`、`make lint` 和
  `make type-check` 通过，严格重构债务为零；排除用户既有未提交结算测试文件的全仓测试
  通过，该文件原有两项测试也单独通过。完整 `make test` 唯一失败为该文件新增断言。
- `make package` 在获准访问本机 pip 代理后成功生成 sdist 和 `py3-none-any` wheel；仓库外
  Python 3.14 全依赖环境通过 `pip check`，实际从 wheel 路径导入 `trader` 和 Long 新模块，
  `trader-cli --help` 可执行，模板、CSS、JavaScript 与 SVG 资源可读。
- headless Firefox/geckodriver 在 1280x720、1440x900、1920x1080 三档均无白屏、页面级
  横向溢出或浏览器错误，Long 侧栏可见、tab 无溢出且左右面板对齐；稳定复跑 25 个 SSE
  patch 全部应用、零 resync，patch-to-paint P95 为 16ms，低于 100ms 门禁。首次浏览器
  冷启动运行布局同样通过，但 P95 为 435ms，稳定复跑后恢复。
- tomorrow v2 影子定向回归覆盖门禁全部 blocker、成功样本分母、baseline/input 去重、
  跨午夜交易日归属、latest-wins、线程有界停止、无历史/DeepSeek 外部端口、点时 epoch
  投影、组合根无副作用、current/history/SSE、14:50 独立冻结和不一致时拒绝切换；相关
  42 项测试和定向 mypy 通过。
- `make format-check`、`make lint`、`make type-check` 通过，293 个受检文件格式正确、
  严格重构债务为零、198 个源码模块 mypy 无问题。主工作树完整测试仅被用户已有
  `test_outcome_settlement.py` 新断言阻断；从已推送基线叠加本批文件且保留该测试基线版本
  的隔离树完整 971 项 pytest 通过，本批未修改或暂存用户文件。
- `make package` 成功生成 sdist 和 `py3-none-any` wheel；首次构建仅因沙箱禁止连接本机
  `127.0.0.1:7897` 代理失败，获准后原命令通过。最终 wheel 从仓库外 prefix 导入包和新增
  影子模块，实际 `trader-cli --help` 可执行，8 项模板/CSS/JavaScript/SVG 资源可读，活动
  锁定依赖环境 `pip check` 无破损依赖。
- 真实 headless Firefox/geckodriver 在 1280x720、1440x900、1920x1080 分别渲染 6 行，
  截图 116973/130449/137328 字节；三档均无白屏、横向溢出、区块重叠或浏览器错误。SSE
  overlay 更新后完整 current GET 计数保持 1。
- tomorrow v2 定向门禁通过：应用查询、overlay CAS、历史正式只读、状态年龄/预算、SSE
  单调序列/游标恢复/身份不匹配/慢客户端、HTTP ETag/304、无注入 503、应用工厂副作用和
  架构契约共 67 项测试通过；隔离用户已有结算测试修改后，全仓 961 项测试、格式检查、
  零债务严格 Lint 与全包 mypy 通过。主工作树全测仅该用户文件的既有断言失败。
- `tests/performance/run_tomorrow_v2_browser.py --output /tmp/tomorrow-v2-browser.json`
  使用 headless Firefox/geckodriver 通过。1280x720、1440x900、1920x1080 分别渲染
  6 行决策，截图约 118KB、131KB、138KB；三档均无页面级横向溢出、区域重叠或浏览器
  错误。SSE 把首行报价更新为 13.37 后，完整 current GET 计数保持 1。
- 隔离源码通过 `make package` 构建 wheel/sdist；仓库外 `--target` 安装确认实际导入路径
  来自 wheel，`pip check` 无破损依赖，`trader-cli --help` 可执行，且 v2 模板、CSS、
  JavaScript、Lucide 图标和 Flask 静态路由均可读取。
- `tests/unit/domain/test_tomorrow_freeze.py` 覆盖 30 秒检查点、14:50 边界、入选锚点、收盘
  原因和未来价格拒绝；`test_current_decisions.py` 覆盖 CAS、并发单胜者、父版本与冻结后
  拒绝；`test_tomorrow_freezing.py` 覆盖先持久化后切索引、失败同版本重试、重启恢复、
  运行身份不匹配、15:00 恢复和伪收盘重建拒绝；独立 repository 测试覆盖检查点消费、
  local/hybrid 往返、幂等、同日冲突、上海时区恢复与损坏文件 fail-closed。
  `make format-check`、`make lint` 和 `make type-check` 通过，严格复杂度债务为零；仅由基线
  加本批文件组成的隔离树完整 `make test` 通过。`make package` 通过，仓库外 wheel 可导入
  新模块、执行 `trader-cli --help` 并读取模板、CSS、JavaScript 和图标。真实 headless
  Firefox 在 1280x720、1440x900、1920x1080 三档均无浏览器错误、页面横向溢出、面板错位
  或文本覆盖，26 个样本的 patch-to-paint P95 为 17ms，低于 100ms 门禁。

- 本批失败先行测试先因 tomorrow v2 融合模块、DecisionEpoch 和明确的复核不可用异常尚不
  存在而停止；实现后，ResearchEpoch 风险/evidence 注入、28 只待审上限、合法子集、
  代码错配、传输失败、迟到隔离、固定 `83.40`、风险单次扣分、动作双池、板块/行业集中度
  和哈希确定性定向测试通过。固定 360 条已评分候选连续构建 25 次的 DecisionEpoch
  P50/P95/最大耗时为 59.217/73.829/74.414ms，低于 100ms 稳定选择门槛。
  `make format-check`、零严格债务的 `make lint`、184 个源码文件的 `make type-check`、
  sdist/wheel 构建均通过；基线加本批 diff、排除用户既有未提交结算测试修改的干净临时
  工作树全量 pytest 通过。原工作树全量测试唯一失败为既有
  `test_outcome_settlement.py` 对无基准结算数量的未提交断言变化，本批未修改或暂存该文件。
  最终 wheel 在仓库外安装全部依赖后通过 `pip check`、新模块导入、`trader-cli` 和四类
  Web 资源读取；Firefox 在 1280x720、1440x900、1920x1080 均无白屏、横向溢出或浏览器
  错误，补丁到绘制 P95 为 47ms。
- 本批失败先行测试先因 tomorrow 领域/应用模块和候选实时特征行不存在而停止；实现及多轮
  Review 修复后，epoch、原子数据平面、三态过滤、板内评分、融合、本地选择和已发布快照
  定向回归通过。`make format-check`、`make lint`、182 个源码文件的 `make type-check` 和
  `make package` 通过；共享工作树完整测试只失败于任务开始前用户已有的 outcome settlement
  新断言，本批未修改或暂存该文件，仅叠加本批 diff 的干净副本完整 `make test` 通过。
  仓库外 Python 3.14 环境安装最终 wheel 和全部依赖后，`pip check`、新模块导入、
  `trader-cli --help` 以及模板、CSS、JavaScript、SVG 资源读取通过。headless Firefox 在
  1280x720、1440x900、1920x1080 三档桌面均无白屏、页面级横向溢出或浏览器错误，
  patch-to-paint P95 为 54ms，低于 100ms 门槛；本机未安装 Chrome，故使用产品范围内的
  Firefox 完成发布布局门禁。
- 本批失败先行测试先因 epoch 模块不存在而停止；实现后，数据 epoch、原子数据平面及项目
  记录定向测试全部通过。`make format-check`、`make lint`、`make type-check` 和
  `make package` 通过，严格结构债务保持零；仅叠加本批 diff 的干净副本完整 `make test`
  通过。共享工作树全测只受任务开始前已有的 outcome settlement 用户断言阻塞，本批未
  修改该文件。仓库外 wheel 可导入新增模块、执行 `trader-cli --help` 并读取模板、CSS、
  JavaScript 和图标。Firefox 三档桌面布局、浏览器错误和溢出门禁通过，稳定复跑
  patch-to-paint P95 为 67ms；首次冷启动为 150ms，未放宽 100ms 预算。
- 本批先新增 tomorrow v2 文档契约测试并观察到缺少目标定义时按预期失败；补齐两份权威
  文档后，定向用例和 `tests/contract/test_project_records.py` 全部通过。
  `make format-check`、`make lint`、`make type-check` 和 `make package` 通过；仅叠加本批
  diff 的干净副本完整 `make test` 通过。共享工作树完整测试只受任务开始前已有的 outcome
  settlement 新断言阻塞，本批未修改该文件。仓库外 wheel 完成全依赖安装，可导入
  `trader`、执行 `trader-cli --help`，并读取模板、CSS、JavaScript 和图标资源。
- 本批失败先行测试覆盖跨策略待处理隔离、tomorrow 首轮优先、公平轮转、同策略
  latest-wins、紧凑横截面缓存、候选代码缓存回投和规范 `merge_epoch`；板块评分、缓存、
  推荐单元测试及完整 `tests/integration/test_v2_pipeline.py` 通过。生产形态离线基准使用
  5,500 行总体、每板最多 120 候选和三个并发策略，三板批次均完整，评分墙钟约 4.3 秒。
  `make format-check`、`make lint`、`make type-check` 通过；仅叠加本批 diff 的仓库外副本
  完整 `make test` 通过。共享工作树测试仅受任务开始前未暂存的 outcome settlement 测试
  阻塞，该文件未修改且不属于本批。`make package` 成功，仓库外 wheel 通过导入、CLI、
  绝对配置、14 项 Web 资源和 `pip check`。Firefox/geckodriver 三档桌面视口无页面横向
  溢出或浏览器错误，25 个 patch 全部应用且无 resync，patch-to-paint P95 为 47ms。
- 本批新增预算分配、上午路由、停止提交截止、在途接纳、无传输重试、单次 schema 修复、
  emergency 条件、Pro 全日 2 次、跨策略 single-flight，以及健康门连续失败、跨重启恢复、
  14:43 后禁止半开和并发单探针回归。`make format-check`、`make lint`（严格复杂度债务为零）、
  `make type-check` 通过；从当前 `HEAD` 叠加仅本任务暂存 diff 的仓库外副本执行无排除
  `make test` 全部通过。共享工作树中的完整测试另受任务开始前未暂存的 outcome settlement
  测试阻塞，该文件未修改、未暂存且不属于本批。隔离 `make package` 成功，仓库外 wheel
  通过包导入、CLI、活动配置和 14 项 Web 资源读取。
  Firefox/geckodriver 在 1280x720、1440x900、1920x1080 均无页面级横向溢出或浏览器错误，
  25 个 patch 全部应用、无 resync，patch-to-paint P95 为 19ms（预算 100ms）。宿主未安装
  Chrome/Chromium，故 Chrome 专用 CDP runner 未执行；本批未修改 Web 资源。
- 本批失败先行回归覆盖 today 正常 11:20 当场入库与 11:30 身份不变、错过边界不追补、
  15:00 热运行/冷启动只恢复 tomorrow/d25、正式空结果保持、冻结后 current/live 隔离、
  历史 today fallback 隔离、腾讯定向部分返回时新浪全市场逐股保底，以及 closing overlay
  幂等固化不误报。`make format-check`、`make lint`、`make type-check`、完整 `make test`
  和 `make package` 通过；仓库外 wheel 通过包导入、`trader-cli --help`、绝对配置校验、
  14 项模板/CSS/JavaScript/SVG 资源读取和 `pip check`。Firefox 在 1280x720、
  1440x900、1920x1080 三档均无页面级横向溢出或浏览器错误，patch-to-paint P95 为
  12ms（预算 100ms）。
- 本批失败先行 JS 与 Web 契约已覆盖正负混合等权平均、真实 0%、`null`/空值/非有限值排除、
  非本组股票排除、整组无行情、同组行情二次重绘由正转负、两位小数、涨跌颜色、覆盖数说明
  和原股票数量保留；实现后
  Node 状态契约及长期侧栏/文档定向契约通过。最终 `make format-check`、`make lint`、
  `make type-check`、完整 853 项 `make test` 和 `make package` 全部通过；隔离构建首次因
  沙箱阻止本机 pip 代理失败，获准后原命令成功。全新无系统包 wheel 环境通过 `pip check`、
  包导入、`trader-cli --help` 和 13 项模板/CSS/JavaScript/SVG 资源读取。Firefox 实机在
  1280x720、1440x900、1920x1080 均渲染 33 个当前大类行业 tab，左右面板同顶同高，
  `+20.00%`/`-20.00%` 长度样本与数量无覆盖且无页面级横向溢出；浏览器错误为 0，25 个
  patch 全部应用、无 resync，patch-to-paint P95 为 29ms（预算 100ms）。当前主机没有
  Chrome/Chromium，持久化 Chrome 门禁已同步扩展长期视图，但本批真实浏览器证据来自同属
  目标范围的 Firefox。
- 本批新增失败先行 API、SSE、JS 与 Overlay 集成回归，覆盖实际锚点报价时间稳定、午后
  当前行情变化、today/tomorrow 冻结 Overlay、来源失败保留上一有效值、15:00 closing
  持久化，以及历史/其他策略/`close_fallback`/下一交易日身份隔离。`make format-check`、
  `make lint`、`make type-check`、完整 `make test` 与 `make package` 通过；隔离构建首次因
  沙箱禁止访问本机 pip 代理失败，获准后原命令成功。仓库外全依赖 wheel 环境通过导入、
  `validate-config`、14 项模板/静态资源和 `pip check`。Firefox/geckodriver 实际渲染确认
  11:20 锚点表、实际 11:19:50 报价时间、13.20 当前价和 +10.00% 锚点变化，三档桌面均
  无页面横向溢出或浏览器错误，25 个推荐 patch 全部应用、resync 为 0、patch-to-paint
  P95 为 26ms。最终代码重启真实服务后，首页 10 项资源均使用新 revision；冷启动约
  20 秒后历史预热完成 356/360，tomorrow 同日 ready 1 只、d25 同日 ready 合法空集、long
  同日 ready 212 只，today 没有使用上一交易日结果。
- 本批失败先行契约分别复现隐藏编解码依赖、20 处旧来源路径、缺失生成器和错误 Ruff 命令；
  修复后架构、长期观察池、工程记录契约及完整持久化组件共 51 项通过，生成器连续
  `--check` 字节一致，直接导入 smoke test 未触发循环导入。最终 `make format-check`、
  `make lint`、`make type-check`、完整 848 项 `make test` 和 `make package` 全部通过；
  package 首次仅因沙箱禁止连接本机 pip 代理失败，获准后原命令成功。仓库外 wheel 从安装
  路径导入并通过 CLI、配置、14 项模板/静态资源及 `pip check`；Firefox/geckodriver 在
  1280x720、1440x900、1920x1080 均无横向溢出或浏览器错误，24 个 patch 全部应用、
  resync 为 0、patch-to-paint P95 为 21ms。
- 新增并通过事件 latest-wins、versioned DAG 禁止周期评分、总体/候选评分分离、long P6
  独立上限与超限诊断、定向行情增量 change set、Web 时段文案回归；定向提交固定 5500 行
  总体/120 行报价连续 5 次实测最大 87.10ms。最终 `make format-check`、`make lint`、
  `make type-check`、完整 848 项 `make test` 和 `make package` 全部通过；仓库外最终 wheel
  从安装路径导入，CLI、配置、9 项模板/静态资源和 `pip check` 通过。固定离线性能 16 项
  最终全部通过，定向覆盖 P95 96.57ms、外部网络调用 0、100 tick 分配增长 0%。
- 最终 Firefox/geckodriver 在 1280x720、1440x900、1920x1080 三档均无横向溢出或浏览器
  错误，24 次 patch 全部应用、resync 为 0、patch-to-paint P95 33ms。真实旧服务曾占用
  78.3% CPU；停止旧进程并以最终代码两次冷启动后，`2026-07-27` 午间 API 实际发布
  tomorrow 1 只、d25 ready 合法空集、long 212 只，long 返回 Sina 实时价、涨跌、来源和
  时间；P6 四个 current 槽、long 512 KiB 上限均生效，首页 10 项资源均使用新 revision。
- 本批权威文档反向一致性定向契约测试及过滤、三板评分、融合、调度、P6、Web API
  相关 127 项回归全部通过；`make format-check`、`make lint`、`make type-check` 和完整
  `make test` 通过，仅保留既有未知 DeepSeek fixture 模型告警。`make package` 首次因沙箱
  禁止隔离构建访问本机代理失败，获准后成功生成 sdist/wheel；仓库外全依赖虚拟环境确认
  `trader` 从 wheel 路径导入、`trader-cli --help`、`validate-config`、模板、CSS、JavaScript、
  SVG 和 `pip check` 全部通过。
- 宿主没有 Chrome/Chromium，持久化 Chrome runner 按预期报告缺少浏览器；Firefox/
  geckodriver 使用同一离线 fixture 完成 1280x720、1440x900、1920x1080 三档布局验收，
  24 个 patch 全部应用、零 resync、零浏览器错误、无页面级横向溢出。首轮图形栈冷启动
  patch-to-paint P95 为 833ms，稳定态复跑为 25ms 并通过 100ms 门禁。
- 本批文档归并契约测试通过，额外读取活动 `runtime.json` 与 `strategy.json`，确认权威文档
  对应 `versioned_dag`、168 次物理硬上限、`strategy_review28_2026_07` 和 28 只送审上限。
  `make format-check`、`make lint`、`make type-check` 和完整 `make test` 通过；全量测试仅有
  既有未知 DeepSeek fixture 模型告警。`make package` 首次因沙箱禁止隔离构建访问本机代理
  失败，在获准联网后以同一命令成功构建 sdist/wheel。仓库外 `/tmp` 安装确认 `trader`
  从隔离目录导入，`trader-cli --help`、`validate-config`、模板、CSS、JavaScript、SVG 和
  `pip check` 均通过。宿主没有 Chrome/Chromium；Firefox/geckodriver 固定 runner 连续两次
  通过 1280x720、1440x900、1920x1080 三档桌面验收，24 个 patch 全部应用、零 resync、
  零浏览器错误、无页面级横向溢出，patch-to-paint P95 分别为 55ms 和 21ms，低于 100ms
  预算。
- 本批验证脚本持久化已通过：`ruff format --check
  tests/performance/run_chrome_dashboard.py tests/contract/test_project_records.py pyproject.toml`、
  `ruff check tests/performance/run_chrome_dashboard.py tests/contract/test_project_records.py`、
  `pytest -q
  tests/contract/test_project_records.py::test_chrome_dashboard_gate_is_persisted_under_tests`，以及
  `.venv/bin/python tests/performance/run_chrome_dashboard.py --output
  /tmp/trader-chrome-dashboard.json`。Chrome 报告显示 `passed=true`、三档桌面视口无横向溢出、
  `browser_errors=[]`、24 个 patch 均应用、`patch_to_paint.p95_ms=15` 且预算为 100ms。完整
  `make format-check`、`make lint`、`make type-check`、`make test` 和 `make package` 通过；
  仓库外安装 wheel 后，`trader-cli --help` 可执行，并确认模板、CSS、JavaScript 和 SVG 资源完整。
- 本批 targeted 验证通过：`node --check src/trader/web/static/dashboard.js`、
  `node --check src/trader/web/static/dashboard_patches.js`、
  `node tests/js/test_dashboard_d4.js src/trader/web/static/dashboard.js`、
  `pytest tests/contract/test_v2_app_factory.py tests/contract/test_v17_recommendation_sections.py
  tests/unit/test_v2_settings.py
  tests/integration/test_v2_pipeline.py::test_v17_and_v18_frozen_board_snapshots_remain_replayable`、
  `ruff check` 受影响文件、`mypy src/trader` 和 `scripts/check_refactor_quality.py`。完整
  `make format-check`、`make lint`、`make type-check`、`make test` 和 `make package`
  均通过；仓库外 wheel 安装后可执行 `trader-cli --help`，并确认模板、CSS、JavaScript 和
  SVG 资源完整。Chrome headless 通过离线 fixture 打开首页，确认脚本资源均为统一
  `?rev=dashboard-refactor-2026-07-26`，`hasPatches=true`，`browserErrors=[]`，快照状态正常；
  1280x720、1440x900 和 1920x1080 三档桌面视口均无页面级横向溢出，关键区域顺序正常。

- 二次命名清理验证：严格扫描确认活动代码、测试、配置、报告路径和 `CHANGELOG.md` 不再包含
  旧收盘补算观察原因码、旧列式投影/合并降级码、旧列式投影函数名或旧长期区域驼峰 DOM id。
  本批 targeted 测试覆盖推荐原因码、Web API、长期页结构、应用工厂、行情降级和列式合并；
  完整门禁和仓库外 wheel 安装验收记录见本批最终提交说明。

- 命名清理验证：活动代码、测试、配置、报告路径和权威文档扫描无旧英文阶段词、拼音旧词、
  旧前端格式化资源名、旧契约类名或旧导入路径命中；文件名扫描无
  `*support*.py`、`*utils*.py`、`*helper*.py`、`*manager*.py`、`*processor*.py` 泛称命名。
  剩余 `support`/缓存相关文本均为协议状态、标准库/浏览器标准、恢复语义或历史归档说明，
  不是本批需要改名的项目自有 API。定向契约测试
  `tests/contract/test_v2_architecture.py tests/contract/test_project_records.py
  tests/contract/test_pipeline_contract_base.py tests/contract/test_pipeline_contract_a2_public_skeleton.py
  tests/contract/test_v2_app_factory.py` 通过；受影响组件测试
  `tests/component/test_v2_market_data.py tests/component/test_v2_deepseek.py
  tests/component/test_v2_deepseek_v4.py tests/component/test_pipeline_deepseek_c2.py
  tests/component/test_pipeline_deepseek_c3.py tests/component/test_pipeline_deepseek_c4.py`
  通过；`make format-check`、`make lint`、`make type-check`、`make test` 和 `make package`
  均通过。仓库外 `/tmp` 目标目录安装 wheel 后，导入来源指向安装目录，entry point 元数据为
  `trader.entrypoints.cli:main`，CLI 模块 `--help`、模板、CSS、JavaScript 和 SVG 资源读取通过。

- 新长期名单配置契约通过：213 只股票、46 个分组、213 个唯一分组席位，每只股票恰好归属
  一次；新增 `脑机接口` 2 只、`AI算力` 4 只、`液冷` 3 只和 `数据中心电源` 3 只，光模块
  5 只保持不变，低价类别共 24 只。配置与打包静态数据完全一致，名单资源缓存版本升级为 v4。
- 19 只脑机接口、AI 算力、液冷和电源候选已通过公开财务、前复权历史和巨潮主营只读复核；
  新增或迁入的 12 个分组席位均有非空主营和截至 2026-07-24 的历史行情。脑机接口公告证据
  只用于证明研发或产品布局，不冒充商业化成功或收益保证。
- 本批完整 `make format-check`、`make lint`、`make type-check`、`make test` 和 `make package`
  通过；仓库外安装 wheel 后 `pip check`、`trader-cli --help`、模板、CSS、JavaScript、SVG 以及
  拆分后的四个卡脖子分组静态数据读取均通过。

- 互联网数据漏斗实测完成：新浪全市场快照返回 5530 只证券；207/207 只候选取得财务摘要，
  163/163 只财务前列候选取得首轮历史，152/152 只基础入围项使用新浪前复权日线重新计算并
  通过正价格、至少一年有效历史和有限数值检查；分类门槛最终保留卡脖子 89 只、高成长 23 只、
  低价 1 只，合计 113 只。152 只基础入围项均取得巨潮公司概况及主营信息。
- 报告结构核验覆盖 43 个现有细分行业、股票代码全局唯一、每行业不超过 5 只和 31 个不足
  5 只行业的基础缺口说明；分类硬门槛后正式名单为 113 只且全局唯一，37 个行业不足 5 只、
  其中 6 个行业为 0 只。低价组 26/26 只取得收盘价、完整年度 EPS 和每股净资产，严格门槛
  复核结果为 1 只通过、25 只不通过。
- `make format-check`、`make lint`、`make type-check` 和 `make test` 通过；`make package` 在允许
  获取隔离构建依赖后成功生成 sdist 与 wheel。最终 wheel 安装到仓库外 `/tmp` 目标后，可从
  安装目录导入 `trader`、执行 `trader-cli --help`、通过 `pip check`，并读取模板、CSS、
  JavaScript 和 SVG 资源。

- 本批长期名单契约验证通过：43 个分组共 207 个席位，配置中恰有 207 只股票，所有代码全局
  唯一且每只股票恰好归属一个分组；配置与打包 `long_watchlist_data.js` 完全一致。53 个新增
  替补席位的代码、简称和非空行情已于 2026-07-25 通过腾讯定向行情只读请求校验，响应中的
  最新行情源时间为 2026-07-24，未保存完整外部响应。
- Headless Chrome 在 1280x720、1440x900、1920x1080 下逐一切换卡脖子、高成长和低价
  三类：左右面板顶部与高度完全一致，`scrollWidth == clientWidth`，浏览器错误为 0；页面
  可见文本英文单词扫描为 0，运行阶段显示“连续交易”，行情源显示“腾讯行情”，标题显示
  “重点研究方向”和“重点股票行情”，蓝色强调线、激活分组和股票名称强化样式均实际生效。

- 本批最终长期界面 targeted 验证通过：配置与打包静态名单完全一致，低价五子分组覆盖 26 只
  且无重复，统一候选行情请求同时包含短线候选和长期代码，腾讯适配器总市值解析、实时简称/
  固定行业合并、行情来源与时间渲染均有回归。2026-07-24 实际请求 `688012`、`300346`、
  `600118`、`002335`、`000713` 返回 5/5 只，价格、涨跌、成交额、换手率、总市值、
  `source=tencent` 和带时区行情时间均非空。Headless Chrome 在 1280x720、1440x900、
  1920x1080 完成最终截图；三个分类按钮位于“长期研究 · 仅展示当前数据”后方的同一控制行，
  下方左右面板同起点同高，无按钮横滑或页面级横向溢出。
- 长期三分类按钮最终去除按钮自身的矩形边框和块状激活背景，只保留文字、悬停反馈和底部
  激活线，避免三个按钮视觉上仍被方框包围；卡脖子、高成长和低价三页共用同一固定双栏
  高度，切换只改变子分组与股票行，不改变面板外框高度。
- 按用户最终位置要求，`卡脖子行业`、`高成长赛道`、`低价潜力股` 移到策略控制栏内，紧跟
  `长期研究 · 仅展示当前数据` 说明之后；进入 long 时一起显示，切走 long 时一起消失，不再
  单独占用页面行或受独立外框宽度限制。
- 最终 Chrome DOM/CSS 验收覆盖 1280x720、1440x900、1920x1080 及三个长期分类：说明文字
  与分类按钮间距固定为 `12px`，按钮外框和按钮左边框均为 `0px`、背景透明；左右面板顶部
  均为 `251px`，左侧第一个子 tab 与右侧股票表头顶部均为 `334px`，三档面板高度分别为
  `405px`、`585px`、`765px` 且左右差值均为 0。三档均满足 `scrollWidth == clientWidth`、
  浏览器错误为 0；切换离开 long 后分类按钮在异步请求完成前即刻隐藏。
- 本批新增 targeted 验证通过：`pytest tests/unit/test_v2_settings.py
  tests/contract/test_v2_web_api.py tests/unit/application/test_recommendations.py
  tests/unit/application/test_published_snapshots.py` 共 114 项通过；`node
  tests/js/test_dashboard_d4.js src/trader/web/static/dashboard.js` 通过，覆盖 long 固定池
  配置校验、`long_groups` API、long 不受 `top_n` 截断、发布索引 metadata 保留、前端二级
  tab 过滤和 long 26 条 patch 校验。
- 本批完整 `make format-check`、`make lint`、`make type-check`、`make test` 和
  `make package` 通过；`make test` 仅保留既有 DeepSeek 测试模型名 RuntimeWarning。
  仓库外用 `pip --prefix /tmp/trader-wheel-long-check-prefix` 安装 wheel 后，`trader-cli --help`
  可执行，且可读取 `index.html`、CSS、`dashboard.js`、`dashboard_formatters.js`、`long_groups.js`、
  `render.js`、`selection.js` 和 SVG 资源。Headless Chrome 在 1280x720、1440x900、
  1920x1080 三档截图成功，DevTools 指标均为 `scrollWidth == clientWidth`、有正文且无
  页面级横向溢出。
- 本批完整 `make format-check`、`make lint`、`make type-check`、`make test` 和
  `make package` 通过；全量测试仅保留既有 DeepSeek 测试模型名 RuntimeWarning。新增回归
  覆盖 28 支送审、v19/v20 回放、schema v5/v6、TopK 与阻塞主事件隔离、本地先于 DeepSeek
  发布、复核异常从 pending 收敛到本地降级终态、hybrid 单提交器及 38 秒排队预算、陈旧
  结果拒绝、收盘完整缓存零全市场请求、缺失 merge epoch 拒绝、TopK 拒绝计数、中文原因
  与原始诊断隔离。
- 仓库外安装最终 wheel 后已从外部目录导入 `trader`、执行 `trader-cli --help`，并读取
  `index.html`、CSS、三项 JavaScript 和两项 SVG 资源。真实 Firefox/geckodriver 在
  1280x720、1440x900、1920x1080 三档均无页面级横向溢出，24 个 SSE patch 全部应用、
  浏览器错误和 resync 均为 0，patch-to-paint P95 为 28 ms（预算 100 ms）。
- 本批 816 项 pytest 全量通过；`make format-check`、`make lint`、`make type-check`、
  `make test` 和 `make package` 通过。新增回归覆盖 60/120/240/480/900 秒历史退避、
  未失败代码继续推进、三板 intraday 轮转、24 支复核上限及冲突优先、168 次并发原子
  预算、long 零调用、v17/v18 冻结回放、冻结发布诊断保留、选择诊断 API/SSE 与前端文案。
  实机 14:52 至 15:04 观察期间，历史计划/完成/失败由 360/358/2 有界推进到
  421/414/7，未再自旋；15:00 后 today/tomorrow/d25 分别从 133/220/238 个候选发布
  2/2/2 项 `close_fallback`，重启后推荐 API 从冻结记录恢复相同数据及完整诊断，
  `last_error` 为空。仓库外安装最终
  wheel 后验证 `trader` 从隔离目录导入、`trader-cli --help`、`validate-config` 和 7 项
  模板/CSS/JavaScript/SVG 资源；真实无头 Firefox 在 1280x720、1440x900、1920x1080
  均无页面级横向溢出或浏览器错误，24 个 patch 全部应用、无 resync，patch-to-paint
  P95 为 62 ms（预算 100 ms）。
- 新增回归覆盖行业字段不改变板内评分、`unknown` 行业限 2、正式/观察池独立限额、
  local 在阻塞复核完成前可见、local/hybrid 身份不同、long 不复核、严重风险永久/90 日/
  三自然年边界、终止标题不生成重复风险周期、严格公告映射、风险覆盖缺失只观察、研究缓存
  序列化及 v14-v17 冻结重放兼容。完整通过 `make format-check`、`make lint`、`make
  type-check`、`make test` 和 `make package`；仓库外安装最终 wheel 后已验证包导入、
  `trader-cli --help` 与模板/CSS/JavaScript/图标资源。真实无头 Firefox 在 1280x720、
  1440x900、1920x1080 三档均无页面级横向溢出或浏览器错误，24 个 SSE patch 全部应用，
  patch-to-paint P95 为 22 ms。

- 隔离目录真实执行 `ApplicationSystem.start()` 并请求 `/api/status`：返回 HTTP 200、
  `started=true`、`status=running`；外部行情在沙箱禁网时按契约降级，未出现 SQLite/WAL/
  locked 错误。隔离运行库 `quick_check=ok`，不存在 `pipeline_events`、
  `data_source_health` 或 `history_cache.sqlite3`；原 `.runtime/v17/history_cache.sqlite3`
  启动前后 SHA-256、大小和 mtime 完全一致。
- 聚焦回归覆盖：20 日活动留存与第 21 日归档、归档后 outcome 结算、归档哈希校验、
  20 根原始历史+61 日摘要、进程内事件 CAS、普通连接不切 WAL、`/api/events` 返回 404、
  SSE 保留，以及 DeepSeek 原子预算组件与流水线集成。
- 完整通过 `make format-check`、`make lint`（严格重构债务为零）、`make type-check`
  （173 个源码文件）、`make test` 和 `make package`；仓库外安装最终 wheel 后已验证包导入、
  `trader-cli` 入口与模板/CSS/JavaScript/图标资源。无头浏览器在 1280x720、1440x900 和
  1920x1080 三档桌面分辨率全部通过，无页面级横向溢出或浏览器错误，SSE patch-to-paint
  P95 为 39 ms。

- 失败先行回归 `test_history_connections_close_and_preserve_event_persistence` 在旧连接边界下确认
  五个连接离开操作后仍可执行 SQL；修复后确认五个连接全部关闭，并在模拟最多五个活动连接的
  资源压力下继续完成事件 `pending -> running -> expired` CAS。
- 通过完整 `tests/component/test_v2_market_data.py`、`tests/component/test_v2_persistence.py` 和
  `test_expired_full_market_event_does_not_commit_candidates_or_set_recent_error`；目标文件 Ruff、mypy
  通过，实际 v17 运行库 `PRAGMA quick_check` 返回 `ok`。
- 使用真实配置和运行库启动服务：新浪全市场返回 5530 行，历史预热完成 357/360；进程 FD
  稳定在 9-10，持久化执行器完成 78 次且零拒绝，新评分事件 `1384` 超时后成功由 `running`
  落为 `expired`，未再出现 SQLite/WAL 终态写入异常。
- 完整 `make format-check`、`make lint`、`make type-check`、803 项 `make test` 和
  `make package` 通过；仓库外全新虚拟环境安装 wheel 后，包导入、`trader-cli
  validate-config`、模板/CSS/JavaScript/图标资源和 `pip check` 通过。
- 无头 Firefox 桌面验收覆盖 1280×720、1440×900、1920×1080，三档均无页面级横向溢出，
  浏览器错误和 resync 均为 0，24 个 patch 全部应用，patch-to-paint P95 为 16ms。

- 通过：`tests/unit/application/test_recommendations.py::test_snapshot_returns_zero_recommendations_instead_of_lowering_threshold`、
  `tests/unit/application/test_recommendations.py::test_close_fallback_observes_local_candidates_below_observation_floor`、
  `tests/integration/test_v2_pipeline.py::test_after_close_commits_ready_strategies_when_d25_research_is_missing`、
  `tests/contract/test_v2_web_api.py::test_current_view_replays_empty_close_fallback_snapshot_from_archive`。
- 通过：使用当前代码直接读取真实 `.runtime/v17` 运行库，2026-07-23 d25 旧空冻结记录经
  `RecommendationQueries.current_recommendation()` 只读 replay 返回 `ready close_fallback frozen`
  且 7 行 observe，降级原因为
  `main/star:board_data_reliability_below_threshold`、
  `close_fallback_observe_floor`、`deepseek_incomplete` 和
  `d25_structured_research_incomplete`。

- 通过：`tests/contract/test_v17_recommendation_sections.py`、
  `tests/integration/test_v2_pipeline.py::test_outcome_settlement_superseded_request_does_not_replace_last_error`、
  `tests/integration/test_v2_pipeline.py::test_missing_pre_cutoff_freeze_is_counted_without_replacing_last_error`。
- 通过：`tests/integration/test_v2_pipeline.py::test_after_close_cold_start_builds_long_current_snapshot`、
  `tests/integration/test_v2_pipeline.py::test_after_close_commits_ready_strategies_when_d25_research_is_missing`、
  `tests/integration/test_v2_pipeline.py::test_after_close_publishes_unreliable_board_features_as_degraded_observe`。
- 通过：Node 直接执行 `selection.js`，验证普通当前视图只显示 `executable`，`close_fallback`
  显示 `observe + executable` 全部项。
- 通过：默认 5000 端口真实 API payload 离线套用新 `selection.js` 后，
  today 为 `ready close_fallback 5/5`，tomorrow 为 `ready close_fallback 3/3`；默认 5000
  旧运行进程首页仍返回 `selection.js?v=1`，需重启服务进程后加载本批 `selection.js?v=2`。
- 通过：`make format-check`、`make lint`、`make type-check`、`make test`、`make package`；
  全量 pytest 仅保留既有 DeepSeek 测试模型名 RuntimeWarning。
- 通过：仓库外 `/tmp/trader-wheel-nodeps-20260723-webdata` 安装 wheel 本体后，可导入
  `trader`，可执行 `trader-cli --help`，并可读取 `index.html`、`dashboard.css`、
  `dashboard.js`、`selection.js`、`render.js`、`trader-mark.svg` 与 `lucide.svg`。完整依赖
  target 安装受 `polars-runtime` 大包下载速度影响被人工中止，代码包与资源验收已在仓库外路径通过。

- 通过：`tests/unit/test_v17_feature_entry_inputs.py`、`tests/unit/test_v2_settings.py`、
  `tests/component/test_v2_market_data.py::test_strategy_factor_registry_is_complete_and_required`，
  以及收盘恢复相关集成回归
  `test_after_close_commits_ready_strategies_when_d25_research_is_missing`、
  `test_after_close_rebuild_reads_cached_candidate_features`、
  `test_after_close_retry_reuses_complete_cached_close_market`、
  `test_after_close_publishes_unreliable_board_features_as_degraded_observe`。
- 通过：隔离临时 runtime 复制现有 v17 历史缓存后，实机启动
  `TRADER_PORT=5051 trader-server --config /tmp/trader-runtime-web-check-c.json`；
  `/api/status` 显示 `after_close_recommendations_recovered=2`、`snapshots_frozen=2`，
  `/api/recommendations/today?view=current` 和
  `/api/recommendations/tomorrow?view=current` 返回 `ready`、`phase=close_fallback` 且含
  items；`/api/recommendations/d25?view=current` 因研究字段缺失继续按契约返回 `not_ready`。
- 通过：`tests/component/test_pipeline_deepseek_c4.py::test_c4_global_168_limit_is_atomic_under_concurrent_reservations`、
  `tests/component/test_v2_deepseek.py` 和 `tests/component/test_pipeline_deepseek_c4.py`。
- 通过：本批 `make format-check`、`make lint`、`make type-check`、`make test`、`make package`；
  仓库外安装 `dist/trader_research_dashboard-0.2.0-py3-none-any.whl` 后，可导入 `trader`，
  可执行 `trader-cli --help`，并可读取 `index.html`、`dashboard.css`、`dashboard.js` 和 SVG 资源。
- 通过：架构 AST、`create_app()` 无线程/文件副作用、固定融合向量 `83.40`、DeepSeek 全局
  168 预算并发、冻结检查点哈希和哈希不一致隔离的关键契约集合。

- 通过：`tests/unit/application/test_cadence.py::test_close_quotes_budget_allows_slow_close_source_and_local_rebuild`、
  `tests/integration/test_v2_pipeline.py::test_after_close_retry_reuses_complete_cached_close_market`、
  收盘恢复可靠度/历史样本/缓存候选/延迟报价相关集成回归，以及 cadence/events 单元回归。
- 通过：本批 `make format-check`、`make lint`、`make type-check`、`make test`、`make package`；
  仓库外 wheel 安装后可导入 `trader`、执行 `trader-cli --help`，并读取模板、CSS、JavaScript
  和 SVG 资源。
- 通过：`tests/unit/domain/test_board_scoring.py`、`tests/unit/domain/test_downside.py`、
  `tests/unit/application/test_board_scoring.py`、`tests/unit/application/test_recommendations.py`
  及收盘恢复集成测试；Ruff 检查通过。
- 通过：structured research 缓存重启复用组件回归、research cache 过期/复用回归、收盘恢复
  缓存候选/可靠度/延迟报价集成回归，以及板块评分、下行风险和推荐应用层相关单元测试。
- 通过：`make format-check`、`make lint`、`make type-check`、`make test`、`make package`；
  仓库外 wheel 安装后可从安装目录导入 `trader`、执行 `trader-cli --help`，并读取模板、CSS、
  JavaScript 和 SVG 资源。
- 实机复核仍发现未解决项：2026-07-23 冷启动收盘补算最高候选仍低于 `0.85`，
  因此按契约拒绝冻结，Web 当前三策略继续 `not_ready`；当天三条错误快照已按用户授权备份并清除，
  历史日期查询已恢复。剩余根因需后续针对真实行情字段覆盖继续处理，不能将本批次宣称为当天荐股已恢复。

### Removed

- 当前后端不再产生通用 `official_record_missing` readiness reason；前端仅保留该值作为旧版本
  响应兼容输入，并按 strategy 映射到精确提示。未删除旧冻结记录、运行数据或历史 API 兼容能力。

- 移除 `docs/V2.md` 中把九个高层能力组直接当作“继续”施工顺序的定义；未删除任何目标
  能力、活动代码、路由、配置、历史解码器或运行数据。

- 移除 `docs/V2.md` 开头重复的“推荐的数据源”、五段逐源说明、“V2 推荐的最终组合”和
  独立“接入顺序”。未移除任何计划中的数据源、降级路径、参考链接或实施要求。

- 移除长期页旧的 `科学仪器/高端医疗设备` 和 `精密零部件` 混合分组标签；没有删除股票、
  评分因子、历史记录或运行数据，原组内标的全部迁入语义更精确且仍全局唯一的固定分组。

- 本批不删除 V1 生产指针、旧运行库、冻结记录、评分因子、阈值或免费行情 fallback；只移除
  V2 对临时失效空 decision 的无条件发布资格，以及降级数据进入可执行池的隐式路径。

- 移除活动全市场路径对同周期东财和新浪双份响应的强制等待、强制字段回退及无条件新浪
  全市场请求；移除 5 秒/尾盘 3 秒这种低于免费全市场分页可持续完成能力的计划频率。

- 移除只读查询对空 `close_fallback` 的现场 replay/评分，以及关闭期限到达后的无界
  worker join；重启不新增候选、观察池、历史预热、review、backoff、breaker 或 session
  持久化文件。

- 移除观察项在冻结检查点、正式/收盘补算 JSON、SQLite 推荐明细、closing overlay、归档
  backlog、收益结算和历史 API 中的持久化身份；移除冻结 today 的观察池锚点跟踪、盘后
  `close_fallback` 观察补位和只读重放生成观察项的兼容行为。未删除或改写旧不可变文件，
  未删除公共全市场行情/历史缓存，也未改变 long 当前实时行情。

- 移除活动当前接口的 18 条默认容量、前端 18 条 SSE TopK 接纳，以及会掩盖真实原因的
  “候选达到评分门槛，但被风险或执行条件拦截”“等待策略数据更新”等泛化主提示；移除冻结
  快照把 `deepseek_pending` 继续表述为“模型复核进行中”的错误语义。

- 移除基准缺失时阻断全部个股结算的早退，以及行情 merge epoch 对完整投影报价的重复
  JSON 编码和已无生产调用的旧列式 epoch helper；未删除结算目标、基准历史、行情快照、
  评分、冻结或运行数据。

- 移除 tomorrow v2 影子专用的 `DailyFeaturePack`、`MarketEpoch` 和
  `CandidateQuoteEpoch` 合成后立即拆解的往返，以及把行情 source/received age 当作本地
  批次排队/计算时延的隐式语义；没有删除真实数据平面 epoch、评分/风险/冻结实现、历史或
  证据数据，也未执行 production tomorrow 指针切换。

- 本批移除的是原生候选风险事实保留非上海时区表示的隐式行为；未删除或改写评分公式、
  风险规则、DeepSeek facts、冻结/证据记录、旧 release、v1 正式链或用户运行数据，也未
  执行 production tomorrow 读写指针切换。

- 本批未删除或重写任何证据数据库、冻结记录、评分规则、DeepSeek facts、旧 release 或
  用户运行数据；生产 tomorrow 读写指针仍未切换。

- 移除 tomorrow v2 影子从全市场重新生成评分候选、候选字段覆盖人口字段，以及把完整 v2
  审计原因直接当作 v1 硬过滤比较事实的隐式行为；未删除数据源、评分公式、风险规则、
  DeepSeek 预算、冻结记录、v1 正式读写链或旧 release，也未执行生产指针切换。

- 移除 `insufficient_trade_days` 仅按成功样本日期数判断门禁的宽松语义，统一改为可验证的
  `incomplete_trade_day`；未移除任何活动荐股能力、实时数据来源或回退链。

- 移除 tomorrow v2 local 对“v1 已完成评分并成功发布 snapshot”的正常路径前置依赖；保留
  snapshot 投影仅作为升级过渡 fallback 和 baseline 对照，不删除 v1 正式实现，也不执行
  生产读写指针切换。
- 移除 Long 的通用策略 worker、评分 fallback、收盘 prepare/finalize 重建和共享行情
  overlay；Long 不再产生 DeepSeek 请求、冻结检查点、正式推荐历史或结果结算。保留 Web
  envelope 中的零分字段仅为现有前端兼容，不代表投资判断。
- 影子运行和工程切换门禁的活动范围明确移除“批量下载/回填历史 60 个交易日”前置步骤；
  本批没有删除既有历史特征字段、缓存或用户运行数据，后续恢复下载必须另立独立交付批次。
- v2 页面移除对旧 `RecommendationQueries`、旧 `SnapshotPublisher` 和旧 Web envelope
  的隐式依赖；移除 SSE 正常在线时的完整 current 周期轮询。未删除旧生产路由、资源或
  运行数据，生产切换仍留给后续影子验收整节。
- 本批没有删除旧生产代码、运行数据或 Web 资源；按用户边界未引入当前决策的持久化仓储
  抽象，也未实现原始行情 120 交易日/20GB 压缩保留代码。后者继续只存在于权威文档。

- 移除 tomorrow v2 融合边界接受池外 review、让低可靠/observe/veto 候选消耗 DeepSeek，
  或在没有任一合法结果时创建 hybrid 决策的能力；当前生产 P1-P6 和旧冻结数据未删除。
- 移除 `CandidateFeatureRow` 对任意数值因子键的开放写入能力；实时覆盖现在仅限登记的
  尾盘、入场、执行质量和日内结构风险字段，结构化财务、公司风险、证券身份和历史基线
  只能由其各自权威 epoch 更新。
- 本批未新增或删除磁盘归档实现；契约测试明确阻止提前加入
  `compressed_partitions.py` 或 `market_epoch_archive.py`。现有 v1/P1-P6、运行库、缓存、
  API、Web 和冻结代码均保持原样，完整旧 release 回退不受影响。
- 从 tomorrow v2 目标契约中移除“必须沿用冷启动历史预热、三策略异步评分、P1-P6、
  `versioned_dag` 或 store 对象”的假设；这些当前实现仍原样保留，待并行影子通过后才
  随完整 release 原子切换，本批未删除生产代码、运行数据或兼容 API。
- 移除长期行业子 tab 只显示行业名称和股票数量、没有组内行情方向摘要的旧呈现；未删除或
  调整长期名单、分组、股票顺序、行情字段、后端 API、评分和冻结逻辑。
- 本批未删除冻结记录、推荐字段、历史视图、观察项、评分逻辑或数据库结构；只替换同日正式
  冻结 today 的当前页面呈现方式。
- 长期观察池配置及其打包静态资源不再包含 20 处退役
  `stock_analyzer/scoring_core/theme_scores.py` 内部路径或旧常量名。
- 移除生产 `versioned_dag` 中与输入完成触发重复的周期评分提交；保留 `serialized`
  兼容模式和原 cadence 配置，未删除评分公式、候选门槛、风险门、冻结或回退能力。
- 移除两份权威文档中已经被活动代码取代的口径：旧收盘补算原因码、盘中强制正式/观察分栏、
  d25 活动“不过热”组件、低可靠度一律阻断收盘固化，以及在线状态必须暴露尚未实现性能
  明细的要求；没有删除生产代码、配置、测试数据、冻结记录或运行数据。
- 删除已完成归并的 `docs/celue.md`、`docs/hi.md` 和 `docs/queston.md`，并纳入用户在本批
  开始前已经删除的 `docs/strage.md`、`docs/times.md`。五份实施计划、问题记录、旧阶段参数
  和未实施提案不再与两份权威文档形成并行真相源；历史用户问题、修改与验证记录继续保留
  在本文件。
- 移除对一次性 `/tmp/trader_cdp*` 浏览器验证脚本的流程依赖；本批没有删除产品代码、业务测试、
  冻结记录或运行数据。
- 移除二次命名审查中确认的旧可见诊断码、旧前端 DOM id 和旧内部函数名；旧字符串不再作为
  当前仓库契约或前端选择器保留。

- 移除活动产品、测试、fixture、报告路径和权威文档中的旧英文阶段词命名，以及项目自有类名、
  函数名、变量名、模块名、静态资源名、指标名和错误码中的旧泛称命名。保留
  `restore/restored` 的“恢复”业务语义、协议状态和前端 Fetch 标准缓存值
  `cache: "no-store"`，它们不属于本批命名债务。

- 删除旧 long 本地评分策略实现和导出，包括 `score_long`、`LONG_COMPONENT_WEIGHTS`、
  `score_strategy(Strategy.LONG, ...)` 的旧五维评分入口，以及 long 在
  `local_strategy_weights`、`dimension_weights`、DeepSeek 预算桶和阶段槽中的配置残留。
  long 页面、API、固定池、`long_groups`、实时行情刷新和只读观察语义保留。
- 本批未删除任何评分因子、安全校验、冻结记录、运行数据库或 long 观察路径；只从推荐关键
  路径移除对 DeepSeek 完成和 TopK 主事件排队的同步等待。
- 从活动评分链删除同行收益差、领先组差、残差动量、行业趋势、长期行业政策分和放量突破
  行业宽度条件；删除竞争组作为 TopK 数量限制的行为，保留字段仅供兼容回放与审计。

- 删除 SQLite `pipeline_events`、`data_source_health` 活动表与仓储实现、启动重放、
  `GET /api/events` 分页接口及其配置。
- 删除本地历史种子和 v17 SQLite 历史热缓存实现；现有
  `.runtime/v17/history_cache.sqlite3` 保持原文件不动，但新代码不再创建或打开它。

### Residual Risks

- 已运行的服务必须重启后才会使用新的 readiness reason、300 秒批次截止和健康字段。批次 deadline
  只能取消尚未开始的 worker future；已进入 HTTP 的请求仍按单次 12 秒 timeout 退出，最迟启动的
  单只腾讯/东财回退链可能在逻辑批次释放后继续占用一个历史 worker，但不会提交 deadline 后结果。

- 2026-08-10 17:28 已经写入的空冻结记录属于不可变运行数据，本批不删除或覆盖；修复阻止后续同类
  记录产生，但该历史日期仍会保留原记录，除非另行执行带审计的隔离/修复流程。
- 工作树中先于本任务存在的 bootstrap、状态路由和文档测试改动尚未闭合，仍需由其原批次修复上述
  format/mypy/架构与状态契约失败；本任务通过隔离基线验证，未把这些改动混入修复范围。

- P8 不新增独立生产切换指针，也不改变当前 production tomorrow 的读写治理；后续 P9 仍需在
  真实交易日证据下完成原子切换与回退演练。

- P7 不接入真实通达信/mootdx 节点、不建立生产 fallback，也不新增生产配置；其后续准入仍依赖
  连续真实样本、节点切换/断线、时间戳、价格、停牌和延迟对比证据。关闭 mootdx 影子能力后，
  当前业务继续依赖最近有效统一行情与腾讯定向报价降级。

- P6 仍保留外部准入风险：CNInfo 当前是离线增量登记簿和数据平面写入边界，未接入真实生产
  调度、行情路由或交易所公告交叉校验；`exchange_cross_check_status=pending` 不能解释为交易所级
  复核完成。仓库外完整带依赖安装遇到第三方包 hash mismatch，需要后续在干净包索引环境复验。

- P5 仍有边界未完成：BaoStock 对历史完整性与价格一致性未接入正式评估链，`history_data_plane`
  回放依赖 `tushare`/历史缓存窗口可用性；若无数据源与持久化可达性，历史加载将降级为可用来源回退而不阻断。

- P4 交付仍保留边界：本批不切换 production today/tomorrow/d25 读写指针，
  也不包含交易所官方主数据、交易所公告或风险证据链的完整实现；后续章节(P5/P6/P7)
  仍需接入并验证更多来源与生产化回放路径。

- P3 目前仅完成持久化底座和恢复测试回归，未接入生产 `bootstrap.py` 依赖图；到现有
  读取链路前仍需由后续批次实现具体仓储读取、写入点时机和回写策略，避免提前扩大面。
- 该批为本地 SQLite 持久化能力建立最小闭环，未做旧运行库/历史数据库的并行写迁移测试；
  旧 `.runtime` 数据库读写仍采用已有路径验证，P3 的新仓储需在后续迁移批次完成线上接入评估。

- 本批 `make test` 仍有 2 项既有失败（`test_graceful_shutdown` 超时关闭与 `RuntimeSupervisor` 线程清理）
  与本批字段质量改动无关；`make package` 在当前沙箱环境下受网络代理限制而失败，需要获得可访问
  包仓库网络后再执行仓库外 wheel/sdist 安装验证。

- P0 仅完成“基线冻结与术语边界定义”；今日任务不包含运行路径/产线切换实现。
  后续章节需继续按计划推动 today/tomorrow/d25/long 的读写指针切换、生产与影子运行路径落地，
  并在切换时保留本批基线可追溯结论。

- 本批只评审并拆解计划，不实现 P0-P13，也不证明交易所、巨潮、BaoStock 或 mootdx 在
  当前网络环境可长期稳定使用。tomorrow v2 仍未切生产指针；P0 还需固定最终 runtime
  schema、历史解码器保留期、旧 API 弃用窗口和真实交易日发布证据要求。

- `docs/V2.md` 仍是迁移计划而非产品、策略或依赖权威；实际行为继续以两份权威文档和
  `pyproject.toml` 为准。本批只整理文档，不实现或联网验证交易所、巨潮、BaoStock、
  mootdx 等后续适配器；免费来源的限流、字段变化和可用性仍需在对应实施批次验证。

- 本批固定名单只表达长期产业链观察范围，不构成收益或龙头地位承诺，也不随实时价格、
  营收或评分自动换股。可控核聚变 A 股映射仍处产业早期，因此只纳入正式披露已有聚变部件
  或超导带材应用的安泰科技、永鼎股份，其余概念关联较弱标的不补足。免费行情源仍可能
  限流、改字段或短时不可达，届时页面按既有契约保留股票并显式显示行情缺失；正在运行的
  旧进程需要重启后才会加载新固定池版本。

- 本批解决的是输入口径、无效空覆盖和免费源降级连续性，不宣称外部免费接口具备 SLA。
  东方财富、新浪、腾讯仍可能限流、超时或改字段；当显式候选真实缺少新鲜报价或从未取得
  历史时，系统会保留最近有效决策或保持 `not_ready`，不会补造推荐。当前运行进程必须重启
  才会加载 v35；重启后仍需在真实交易时段重新采三组候选级质量、行情年龄和端到端时延，
  完整交易日证据达标前不切换 V1/V2 生产指针。

- 本批只闭合公司研究调度止血，不声称解决免费来源自身停机、限流、schema 变化或历史公告
  覆盖不足；公司风险历史闭合、分项来源隔离/官方免费复核以及全市场硬截止仍是后续独立
  章节。连续失败会用退避换取来源保护，最坏恢复探测间隔为 900 秒，但最近有效事实、本地
  推荐和只读 Web 继续可用并显式降级。

- 东方财富、新浪和腾讯均为无付费 SLA 的公开免费接口，供应商仍可能限流、改字段、关闭
  入口或在本地网络环境中不可达；本批通过首胜对冲、8/20 秒截止、30 秒熔断、轻量探测和
  最近有效快照控制影响，但不能保证外部可用性。单源 P95 不超过 10 秒仍需在完整交易日
  真实网络样本中复核；在证据达标前不自动切换 V1/V2 生产指针。

- Windows `SIGBREAK` 已有平台分支和可注入信号测试，但当前 Linux 验收环境不能完成
  Windows 实机任务管理器、第二信号和无残留进程检查；发布到 Windows 前仍需一次实机
  外部验收。强制结束、断电、第二次信号及 30 秒期限强退属于异常终止，只承诺正式冻结
  的持久化恢复一致性，不承诺保留纯内存观察和预热状态。

- 观察池不落盘后不能用于冻结后回测、收益结算或离线优化 73 分观察线；73 仍只是正式门槛
  78 减 5 分观察余量，不是已验证收益最优值。旧不可变文件仍可能物理包含历史观察项，但
  活动读取、归档 backlog 和结算均过滤；本批不执行破坏性历史改写。运行服务需要加载
  `runtime_v33_ephemeral_observation_official_only_2026_07_29` 后才会采用新边界，真实交易
  日仍需观察外部行情和研究来源可用性。

- 公司研究仍依赖东方财富公开端点的可用性和 schema；独立池、分项缓存、部分成功接纳与
  熔断只能避免拖垮本地推荐，不能保证供应商持续返回完整数据。未覆盖分项会显式显示并
  降级，不能据此断言股票不存在对应风险；真实外网全覆盖率仍需在新进程运行后观察状态 API。

- 观察线 73 仍是当前策略参数 `78 - 5`，没有足够样本外证据证明它能产生最高收益；任何
  门槛调整仍需按权威策略文档完成预登记收益、成本和稳定性门禁。当前机器没有 Chrome/
  Chromium，因此本批真实桌面验收使用产品范围内的稳定版 Firefox；Chrome/Edge 的 CSS
  兼容由相同标准 Web API、静态契约和后续具备浏览器环境时的门禁继续覆盖。旧冻结原文件
  保持不可变，但显式历史只返回其中的正式项；盘中当前接口始终遵守 12 条上限。

- 本批只修复 tomorrow v2 影子工程时延和证据语义，没有调整选股、评分、融合、动作或
  排名，因而不能宣称荐股收益已经提高。v30 午后启动仍无法补齐不晚于 10:00 的真实成功
  样本；须在下一完整交易日重新取得至少 100 条成功样本、100% 有序选股/硬过滤一致、
  5/10 秒 P95 和 14:50 匹配冻结，再由离线 CLI 独立复核后才可讨论生产切换。

- 本批修复工程时区边界，不提高或调参候选、评分、融合、动作与排名，因此不能证明荐股
  收益提升。v28 午后运行已错过 10:00 前样本且其旧失败证据保持不可变；v29 仍须在下一
  完整交易日 10:00 前启动并持续到 14:50，重新取得至少 100 个成功样本、匹配冻结、
  100% v1/v2 一致和 5/10 秒 P95，再由离线 CLI 复核。生产 tomorrow 指针继续保持 v1。
- 2026-07-29 12:31 的 v27 午间重启已经错过不晚于 10:00 的完整日窗口；现场产生的一条
  旧日恢复失败仍作为不可变审计保存在证据库中。v28 修复只能保证下一完整交易日按独立
  窗口评估，不能追补今天早盘或证明真实收益；仍须在下一交易日 10:00 前启动并持续至
  14:50，取得至少 100 条成功样本、匹配冻结和 5/10 秒 P95 后，才能另立批次复核并切换。
- 当前 5000 端口仍运行上一构建，未重启到 v27；本批没有在 11:20 后中断用户正在使用的
  本地服务。当天又缺少不晚于 10:00 的持久化成功观测，因此即使现在重启也不能形成第
  2.9 节完整交易日证据；必须在后续真实完整交易日重新累计至少 100 条成功样本、14:50
  匹配冻结及 5/10 秒 P95 后再复核，旧 97 成功/45 失败样本不得沿用或改写。生产 tomorrow
  指针和收益路线均未切换，收益提高仍无真实前向证据。
- 宿主没有 Chrome/Chromium，Chrome 专属 runner 无法启动；产品支持范围内的 Firefox
  主看板与 tomorrow v2 三档真实渲染、SSE overlay 和 patch-to-paint 已全部通过。

- 当前仓库和本机没有真实交易日形成的持久化证据，因此新增数据库/CLI只能证明保存、恢复、
  篡改检测和工程门禁机制，不能证明样本来源真实、服务整日连续或荐股收益提高；固定 fixture
  仍不得作为切换依据。本批不切换 tomorrow 生产读写指针，后续须运行至少一个完整交易日、
  保存至少 100 个成功样本并由独立批次复核。

- 当前环境没有运行中的本地服务或 `.runtime/v17/tomorrow-v2` 真实交易日证据，因此尚未
  获得第 2.7 节要求的至少 100 个成功样本、完整 14:50 匹配冻结、零错误/额外 DeepSeek
  请求及 5 秒/10 秒 P95；fixture 不能替代该证据，本批不切换旧首页、旧 API、P6 或正式
  冻结指针。
- 本批使用固定行情 fixture、并发阻塞回归和离线浏览器验证机制与隔离性，未在真实交易日
  调用外部腾讯行情，因此供应商实际 1 秒吞吐、网络抖动和真实数据年龄仍需运行观测；
  Long 是固定研究观察池且无评分，本批不构成收益承诺。下一流水线优化章节未在本批顺带
  实施，需等待用户下一次“继续”。
- 本批已把 v2 影子接入真实组合根，但没有切换 tomorrow 生产读写指针。首次启动后只有旧
  P6 成功接纳包含点时 `replay_input` 的同日 tomorrow 快照，v2 current 才会从
  `not_ready` 变为旁路结果；真实完整交易日尚未形成 100 个成功样本、匹配冻结和 5/10 秒
  P95 证据，因此门禁预期保持 `eligible=false`。固定 fixture 只能证明机制，不能冒充真实
  交易日性能或收益证据。
- 历史 60 个交易日批量下载/回填按用户要求暂停；影子复用当前输入已有历史，历史不足会
  依法减少候选或返回合法空集，不会后台补抓。该暂停可能降低早期影子覆盖率，但不能通过
  伪造历史或放宽过滤解决。工程同版本影子也不替代策略文档要求的 250 日样本外回放、20 日
  挑战者前向影子和收益晋级门禁。
- 浏览器门禁使用产品支持范围内的 Firefox；宿主未提供 Chrome/Chromium，本批没有新增
  Chrome 实测证据。测试未调用真实行情供应商或 DeepSeek，也不能证明未来收益；外部来源
  失败时仍会保留最近有效决策并显式降级。
- 决策索引、冻结协调、v2 repository、API/SSE/Web 和组合根影子现已连通，但旧 P6 仍是
  影子输入来源且生产读写指针未切换。因此仓库门禁可以证明冻结到旁路 Web 的身份与展示
  一致性，仍不能证明真实交易日行情覆盖、端到端 1-15 秒时延或荐股收益提高。外部数据、
  DeepSeek 与真实前瞻收益仍须在真实影子观察和后续显式切换批次留证；压缩原始行情的
  120 交易日/20GB 磁盘治理仍未实现。

- 当前运行中的服务进程仍加载旧代码，必须重启后才会使用按策略隔离的评分通道和紧凑缓存。
  外部行情、历史预热或候选质量不足仍可能合法产生空推荐；本批只消除跨策略覆盖和缓存
  序列化超时，不放宽候选门槛，也不承诺推荐数量或投资收益。离线基准不能替代真实交易日
  对行情来源延迟、候选覆盖和最终收益的持续观测。
- 本批调整的是调用资源、时段、降级和重复请求控制，不改变固定 68/32 融合公式、风险映射、
  候选门槛、动作门、排序或冻结规则，也不构成收益保证。36 是健康且有合格候选时的停止目标，
  不是必须耗完的配额；缓存命中、候选不足、健康熔断或外部服务失败都会使实际调用低于目标。
  V4 模型可用性、真实交易日响应质量与推荐收益仍需使用受保护密钥和至少 60 个有效交易日的
  点时样本外评估，不能由 mock 回归或预算增加推断。
- 外部行情供应商仍可能超时、断连或只返回部分代码；系统只能按已验证优先级使用新浪等
  全市场兜底或最近有效快照，并显式标记降级，不能保证每轮都有新成交。当前正在运行的旧
  进程必须重启后才会加载新的 Web 静态 revision 和 closing overlay 误报修复；本批不改变
  候选、评分、动作门槛、DeepSeek 预算或冻结不可覆盖规则。
- 全新虚拟环境复制全部第三方依赖时触发宿主 `/tmp` 磁盘配额；项目 wheel 的隔离构建成功，
  随后在仓库外环境复用当前已锁定运行依赖完成导入、CLI、配置、14 项资源与 `pip check`。
  因此未发现包内容或依赖声明错误，但本机本批没有留下“一份全新复制的完整依赖环境”证据。
- 行业均值反映外部行情快照中的当日涨跌幅，不是股票加入观察池后的累计收益，也不代表
  推荐评分或买卖信号。行情源超时、陈旧或部分缺失时均值只能基于当前有效子集计算，页面
  通过有效数/总数披露覆盖范围；本批不改变既有降级和最近有效快照策略。
- 实际锚点报价允许早于 11:20 最多既有冻结接受窗口，因此页面同时显示“11:20 锚点”业务
  边界和逐股实际时间，不把两者伪装成同一时刻。外部行情源失败时只能保留最近有效报价并
  标记降级，不能承诺每秒都有新成交；本批不改变候选、评分、门槛、冻结或收益表现。真实
  验收在 14:31 重启，当天不存在可恢复的 11:20 pre-cutoff 正式 today，因此该接口按冻结
  不可伪造规则保持同日 `not_ready`；真实交易日锚点展示仍需在下一次上午持续运行并完成
  11:20 冻结后观察，本批的锚点与 Overlay 行为由确定性集成和真实 Firefox fixture 验证。
- 本批没有调整长期观察池股票、评分、行情、Web 布局或冻结行为；稳定来源文字只保留业务级
  追溯，不等价于新增外部研究证据。生成器不会自动改写配置，维护者仍需明确更新 JSON，
  但未同步静态 JS 会被 `make long-watchlist-check`、`make lint` 和契约测试阻断。
- 本次真实服务在 11:20 后启动，因此 today 按冻结不可变规则保持同日 `not_ready`，不能
  用迟到评分伪造上午结果；上午热运行行为由完整交易日集成回归覆盖，下一真实交易日上午
  仍须按永久可用性矩阵观察。d25 本次不是链路未就绪，而是完成 220 只评分后最高 68.63
  低于 71.00 观察门槛的 ready 空集；这类合法空结果不会为增加数量而放宽。
- 外部行情源仍可能超时、熔断或返回不完整历史；本批消除本地重复全市场评分、旧评分堆积和
  long P6 容量误拒绝，但不承诺供应商持续达到 cadence，也不放宽 100 只板内总体、0.85
  可靠度、硬过滤、动作或冻结门槛。健康数据下没有股票达到观察门槛时仍允许真实空结果；
  已经开始执行的旧版本评分不会被强制中断，最终由 snapshot/freeze CAS 拒绝迟到发布。
- 本批以当前活动代码、配置和测试为真相源纠正文档，没有改变运行行为。供应商真实覆盖、
  DeepSeek 外部响应、真实交易日冻结恢复和收益效果仍受既有外部条件约束；宿主没有
  Chrome/Chromium，故桌面证据来自同属目标范围的 Firefox，且其首轮冷启动渲染时延仍有
  明显波动，尽管稳定态复跑已通过 100ms 门禁。
- 当前宿主未安装 Chrome/Chromium，因此本批实际桌面证据来自同属目标范围的 Firefox。
  Firefox 首次冷启动运行布局全部通过，但 patch-to-paint P95 为 124ms，超过 100ms；随后
  两次相同固定 runner 为 55ms 和 21ms 并通过。该波动未由本批文档变更引入，但说明宿主
  图形栈冷启动时延仍需在发布环境持续观察。
- 本批是文档治理变更，不新增收益挑战者或运行时功能。待验证收益路线仍缺真实前瞻交易日、
  有效配对样本、候选召回审计和成本压力证据，未达到荐股策略第 15.1 节门禁前不得描述为
  收益改善；外部供应商时延和数据覆盖风险仍按现有显式降级策略处理。
- 持久化 Chrome 验证脚本仍依赖本机安装 Chrome/Chromium、允许打开本地 DevTools socket，并且
  需要 dev 依赖 `websocket-client`；无浏览器或权限受限环境只能运行普通单元/契约测试，不能替代
  桌面真实渲染验收。该脚本当前覆盖 Chrome/CDP，Firefox/geckodriver 验收仍需使用既有发布环境
  流程补跑。
- 静态资源缺失时的前端兜底只保证完整快照可继续显示并触发 resync；若用户仍运行旧进程或旧
  wheel，需要重启到本提交后才能获得统一 `rev` 模板和新 `dashboard_patches.js`。Firefox/
  geckodriver 在当前环境不可用，官方 Firefox 浏览器性能脚本仍需在具备该依赖的发布环境补跑；
  本批已用 Chrome headless 验证首页无浏览器错误。

- 本批按用户选择执行严格破坏性改名：依赖旧降级原因码、旧 metadata key 或旧 DOM id 的外部
  脚本、浏览器自动化和历史断言需要同步更新；本批不提供兼容 alias。真实运行中已落盘的旧
  metadata 仍可能包含旧原因码，只能按旧 release 解读或另建迁移任务。本批不改变推荐公式、
  数据库表结构、冻结规则、CLI 入口、DeepSeek 预算或行情采集行为。

- 本批是破坏性命名迁移：旧 Python 导入路径、旧 schema 字符串、旧 fixture/报告路径、旧前端
  静态资源文件名、旧指标键和旧错误码文本不再作为当前仓库契约保留。真实运行数据、外部脚本
  或监控看板若仍依赖旧名称，需按完整旧 release 读取或另建迁移任务；本批不改变数据库表结构、
  推荐公式、冻结规则、CLI 入口或 DeepSeek 预算数值。`restore/restored` 与
  `cache: "no-store"` 因语义准确或标准要求保留。宿主 `python3 -m venv` 因缺少 ensurepip 不能
  创建全新隔离 venv，本批用仓库外 `--target` 安装验证 wheel 内容和 CLI 模块入口。

- 脑机接口仍处于早期产业阶段，本批只纳入公告证据和质量门槛均可核验的 2 只股票；年报中的
  研发、注册证或产品布局不能证明形成规模收入。AI 算力、液冷和数据中心电源拆分是固定研究
  分类调整，运行时仍不会根据财务、价格或公告自动换股。

- 本批报告不是全 A 股完整穷举：同花顺公开板块完整分页受反爬限制，东方财富全市场接口被
  当前代理断开，Tushare 令牌无证券主数据权限，雪球和百度估值接口不可稳定访问。因此原
  207 只名单仍仅作为候选入口，报告可能遗漏名单外更优公司，不能宣称为全市场最优组合。
- 巨潮公司概况能够核验主营相关性，但不能单独证明国产替代已经实现、技术领先或形成收入；
  卡脖子候选仍需后续逐家公司核验年报、公告、客户验证、收入占比和研发投入。近似 PE/PB
  也不等同于供应商 TTM 或行业分位估值，沃顿科技只通过机械门槛，不构成收益承诺。

- 当前长期页的代码、名称和固定行业/赛道归属来自版本控制的 `long_watchlist.json`，价格、
  涨跌、成交额、换手率、市值和行情时间来自统一行情快照；公司公告、主营构成、财务指标、
  产业地位等长期基本面事实尚未进入 long Web envelope。后续若展示这些字段，必须由后台
  有界采集、校验并携带来源与时间戳后发布，不能由浏览器现场抓取或用静态文案冒充事实。
- `高成长赛道` 是 2026-07-24 的人工固定 Review 结果，工程门禁只证明配置、接口、推送和
  展示正确，不证明未来收益；若要动态按 3/5 年复权涨幅、估值或财务质量重筛，需要另建
  离线研究任务和可审计数据集。
- 本批新增替补股票只完成代码、简称、实时行情可用性和分组唯一性的工程校验；其行业归属与
  长期研究价值仍属于人工固定观察判断，不构成收益承诺，也未以本次界面去重替代财务、公告、
  主营或估值数据的离线研究。
- `低价潜力股` 本批固定为芯片、智能制造、算力卫星、材料资源和种业等相关战略行业的
  观察标的，运行时只刷新行情，不自动按最新价格重筛；若未来希望按实时低价阈值或财务质量动态换股，需要作为新的
  策略/配置任务另行设计，并重新定义数据来源、阈值和回测口径。
- 本批已调用腾讯真实定向行情验证长期事实字段，但未调用真实 DeepSeek 服务，也不以工程
  门禁或单次行情成功证明推荐收益；外部供应商超时、
  熔断或字段覆盖不足时仍可能合法只保留最近有效快照或得到零正式推荐。需要在真实交易日
  持续观察 `queue_wait/execution_total`、overlay 年龄、触发评分丢弃数、收盘缓存命中率和
  各策略正式/观察数，才能量化 31.3 秒现场长尾的实际改善。28 支送审会增加单次候选覆盖，
  但不会突破现有 168 次物理请求硬上限，预算耗尽时继续使用本地结果。
- 本批终止了已确认的历史失败自旋并提高三板尾盘覆盖公平性，但外部行情/历史/DeepSeek
  供应商仍可能超时或返回不完整数据；系统会保留本地快照并显式降级。未降低 today、
  tomorrow、d25 的分数、可靠度、风险或冻结门槛，因此健康数据下最高分仍低于观察门槛时
  推荐可以合法为 0，页面现在会显示最高分和门槛。history source lane 的线程拓扑未改；
  若上线观测仍证明不同来源互相阻塞，需以新的压力证据另立任务。
- 严重风险历史使用发行人法定披露索引的 `total_hits` 校验完整性；供应商截断、schema
  漂移或全量历史未返回时会保守标记覆盖不可用并把对应候选降为观察。公告标题映射刻意
  从严以避免把问询、诉讼或传闻误判成永久黑历史，未被官方标题明确表达的复杂事实仍可能
  需要后续扩展结构化字段映射。

- 流水线事件和来源健康按用户要求只存在于当前进程，重启后不会恢复旧事件明细；运行状态、
  SSE 快照和调度会从当前进程重新建立。归档随交易日增长，需要用户按本机磁盘容量定期通过
  CLI 校验和导出，但 Web 与活动 SQLite 的大小保持 20 日上限。
- 本批环境禁止访问外部行情供应商，因此不能用真实供应商成功响应复验 360 只完整预热；
  禁网失败已按契约降级，隔离启动、HTTP 路由、三档本地浏览器、运行库结构和旧历史文件
  不触碰均已验证。

- `close_fallback` 不再用降级 `observe` 补表；没有达到正式门槛的股票时会形成可审计的
  正式空记录。这能精确表达当日无正式推荐，但不会增加推荐数量或提高收益。

- D25 仍严格依赖 `growth_score`、`quality_score`、`value_score` 研究字段。若本地结构化
  研究缓存为空且外部研究源未成功落盘，D25 不会以中性值伪造正式推荐；板块人口满足时可
  固化正式空 `close_fallback`，板块人口不足时仍保持 `not_ready` 并重试。

- 本批修复了现场 Web 空结果的 `close_quotes` 超时与重复慢抓阻断；若 15:00 后所有行情来源
  持续不可用、三板历史样本仍不足、或候选输入真实达不到可靠度门槛，系统仍会按契约保持
  `not_ready` 并重试，而不会降低 0.85 制造推荐。
- 本批用缓存复用和收盘恢复回归验证了结构化研究字段可在重启后恢复；尚未在新的真实交易日
  15:00 后实机证明三策略都会形成当日 `close_fallback`。如果外部研究、历史或行情字段本身
  仍未成功落盘，系统仍会保持 `not_ready` 或可靠度降级，而不会降低门槛制造推荐。

### Added

- Web 荐股展示批次：新增独立策略/日期选择状态机和可见策略说明，明确今早、明日、
  2-5 日与长期的持有期、冻结时间和历史能力；显式历史日期跨三种短线策略保持，
  缺失归档时保留所选日期并显示“无数据”，不会隐式切回当前结果。

- 用户反馈此前保存的推荐历史全部无法查询。新增 P6 按策略独立驻留与冷读能力：最近
  20 个交易日内，同日即使只保存 today、tomorrow 或 d25 中的部分策略，已有 committed
  快照仍可通过对应日期列表和历史 API 读取，不再依赖三策略日期交集。

- 用户反馈 Web 同时提供“临时实时”和“正式当前”两个并列按钮，难以判断日常应该选择
  哪一个。新增只读 `view=current` 自动当前视图：同日 P6 未冻结时解析为实时草稿，
  正式冻结后自动解析为正式结果，`close_fallback`、long、历史和未就绪状态分别显示
  “收盘补算”“当前快照”“历史冻结”和“未就绪”；原 `official|live` API 保留供兼容
  调用与诊断。

- 用户反馈 Web 长期只有三板样本不足、可靠度不足、DeepSeek 不完整和 tomorrow 尾盘
  不完整，实时荐股为空。新增 afternoon/final-review/final-quote 评分前的有界尾盘分钟
  刷新，使周期评分不再只读一个永远为空的缓存；新增独立 v17 qfq 历史热缓存，使优化
  或服务重启不再把已预热的 360 只三板历史恢复为仅 40 只旧种子的冷状态。

- 用户确认只执行 `docs/times.md`、暂不执行 `docs/strage.md`；本批完成 T1 真实延迟瀑布
  与性能门禁。新增组合根共享的有界 `LatencyWaterfall`、来源 lane 排队与物理请求/
  本地处理分离计时、状态 API 聚合诊断，以及读取统一预算的 Firefox/geckodriver
  patch-to-paint 与三档桌面验收 runner；不改变候选、评分、风险、融合或排名策略。

- 用户要求把 `plan_c.md`、`plan_sudu.md` 和 `plan_pipeline.md` 中仍有效的策略归并到两份
  权威文档后删除，并补充确认已被其他文档取代的 `plan.md` 可直接删除。软件权威契约新增
  provider 三段式、列式类型/dtype 边界、dirty 路由矩阵、P1-P6/DeepSeek/SSE 观测指标和
  长期公共接缝说明；策略权威契约新增 V4 事实映射、证据质量收缩、复核优先级、
  批处理/整批一次修复和 58/66/71 软目标说明。

- 用户要求继续未完成的任务 A，本批闭合 `docs/plan.md` 第 2.6 节。新增按只读能力拆分的
  Web 状态、推荐、事件/SSE、请求解析和服务契约模块，并为 DeepSeek cache identity
  增加独立类型契约，使路由与外部调用参数可由 mypy 静态核对。

- 用户要求把 SDK/API 取股、行情统一、候选/评分、P6/SSE 和 Web 实时展示的性能优化计划
  写入 `docs/times.md`。新增非权威执行计划，归档真实链路审查证据、P0/P1 瓶颈、T1-T5
  独立实施顺序、拟议接口影响、确定性/冻结/资源边界和量化验收矩阵。

- 用户要求将硬过滤、评分、融合、荐股和 Web 展示的全链路收益优化审查方案写入
  `docs/strage.md`。新增非权威执行计划，记录数据覆盖基线、v17/v18 同期影子、风险分层、
  热度组合、流动性对照、分支独立入场形态、候选召回、缺失值收缩、融合归因、TopK、
  Web 决策轨迹及收益晋级门禁。

- 用户继续未完成的任务 A；本批按下一完整章节发布 G5 最终共同门禁，新增
  `docs/reports/pipeline-g5-final-gate.md` 和对应失败优先交付契约。报告逐项确认 B5/C5/D5
  均签字通过、A4/A5 仍满足全部门禁、文档/代码/测试/配置一致，并明确本批完成后
  `docs/plan_pipeline.md` 全部闭合，不进入其他计划章节。

- 用户继续未完成的任务 A；本批完成 A5.1-A5.5 最终交付审查，新增
  `docs/reports/pipeline-a5-final-review.md`，收齐 B5/C5/D5 的
  `PASS / ready_for_gate=yes` 签字并归档完整 diff 审查、提交映射和剩余外部风险。权威设计
  补记 384 MiB 迁移硬上限、`387,186,688` 字节实测峰值、峰值并存场景、纯 columnar
  `254,447,616` 字节结束 RSS，以及未来是否收紧上限必须另立决策；本批不发布 G5。

- 用户继续未完成的任务 D；本批完成 D5.1-D5.2 最终差异审查，并在
  `docs/reports/pipeline-d1-p6-web.md` 第 12 节向 A 提交 `PASS / ready_for_gate=yes`
  签字。审查覆盖 P6 current/resident/cold、P6-first 公共接线、持久化分流、SSE、DOM
  四元身份和 ETag resync；新增主动 resync schema 与游标分类竞态回归，不进入 G5。

- 用户继续未完成的任务 A；本批按下一完整章节复核 A4/B4/C4/D4 的阶段 4 交接证据，
  确认 D4 留给 A 的 P6 接纳原子性事项已由 A4-F04 关闭，并新增
  `docs/reports/pipeline-g4-gate-review.md` 发布 G4。用户可观察行为不变：不修改推荐、
  冻结、API 或页面逻辑，不进入 A5。

- 用户继续未完成的任务 B；本批完成 B5.1-B5.2 终审签字，新增 B5 行情、三板评分和
  P1-P6 集成内存复验证据及 `report_to_a.md`。终审范围覆盖 P1-P3 provider/列式批次、
  scalar 等价合并、dirty 扩张、A-owned 公共 envelope 适配、性能、内存和 A4-F01 降级闭环，
  结论为 `PASS / ready_for_gate=yes`，不代替 A 执行 G4/G5、squash 或发布。

- 继续 Codex B4：新增完整行情 canonical 行的严格 Polars 列式标准化、Eastmoney/Sina 窄路径合并与
  5500 行/360 候选/100 tick 固定验收 fixture；保留 partial、reference、Tencent overlay 和
  degraded 输入的 scalar 回退。

- 用户要求继续未完成的任务 D；现状确认 G3 已发布而 D4 尚未执行。新增 D4 P6/SSE/API
  固定 18 行性能回归、可执行的 Node 浏览器状态机契约、离线真实页面/SSE 桌面夹具，并在
  `docs/reports/pipeline-d1-p6-web.md` 第 11 节形成 D4.1-D4.4 标准交接包；本批不提前执行 D5。

- 用户要求继续未完成的 Codex A 任务；A 完成 A4.1-A4.6 全量验收与问题闭环。新增
  `docs/reports/pipeline-a4-acceptance.md`、固定 v17 `perf-check` manifest 和同进程 A4 内存 runner，
  汇总 B4/C4/D4 handoff、关闭 Polars scalar fallback 与 P6 发布原子性两项失败，并覆盖六个
  P1-P6 字节池近上限、双 epoch/双路径、DeepSeek 最大批次、P6 冷读和慢客户端并存场景；
  A4 标记 `ready_for_gate=yes`，本批不发布 G4、不进入 A5。

- 用户再次发送“继续”后，A 按阶段 3 共同门禁复核当前 B3/C3/D3 交接状态。新增
  `docs/reports/pipeline-g3-gate-review.md`，记录 A3 handoff 已发布且 B3/C3/D3 标准
  `ready_for_gate=yes` 报告均已到达；A 因此发布 G3，但不启动 A4、不创建 PR/tag/release。
  同批纳入 B3 fixture、C3 raw facts cache identity 修复与测试、D3 P6/Web 差量 patch 修复与报告。

- 用户再次发送“继续”后，A 按 `docs/plan_pipeline.md` 执行 A3.1-A3.7。新增
  `docs/reports/pipeline-a3-integration.md`，记录 B2 列式 P1-P3/change set、C2 DeepSeek V4
  facts/预算/long 隔离、D2 P6/SSE/Web 增量补丁已按 B -> C -> D 纳入 A 集成工作树；同时
  明确本批只发布 A3 集成 handoff，G3 仍等待 B3/C3/D3 基于本集成提交完成专业复验。

- 用户再次发送“继续”后，A 复核 B2 最新交接报告，确认 B2 已补齐
  `ready_for_gate=yes`、A2 public envelope 适配、component/type-check/性能证据；A 因此发布
  G2。`docs/reports/pipeline-g2-gate-review.md` 新增 2026-07-23 再复核与 G2 发布记录，
  明确 A2/B2/C2/D2 均已具备阶段 2 门禁证据，但本批不启动 A3。

- 用户再次发送“继续”后，A 复核阶段 2 门禁新状态：C2 报告已补齐标准
  `ready_for_gate: yes` 字段，但 B2 仍为 `ready_for_gate=no`，因此仍不发布 G2、不进入 A3。
  `docs/reports/pipeline-g2-gate-review.md` 增加 2026-07-23 复核记录，更新 C2 状态和当前唯一
  阻塞项。

- 用户发送“继续”后，A 按阶段 2 共同门禁复核当前 B2/C2/D2 交接材料，但因 B2 自报
  `ready_for_gate=no` 且 C2 未使用标准 `ready_for_gate` 字段，未发布 G2、不进入 A3。新增
  `docs/reports/pipeline-g2-gate-review.md`，记录三方报告路径、base、gate 状态、阻塞原因和
  后续等待条件；同时纳入 B2/C2 报告与 B2 性能 fixture，保留 D2 追加报告。

- 用户发送“继续”后，A 按 `docs/plan_pipeline.md` 执行 A2.1-A2.5。新增
  `src/trader/application/ports/pipeline.py`，集中提供
  `pipeline_contracts_v1` 下的 P3/P4 `MarketChangeSet`、P4/P5 高价值复核 manifest、
  DeepSeek V4 facts、P6 projection/overlay event、resync reason 和 248/384 MiB 内存契约；
  新增 `src/trader/application/pipeline_contract_doubles.py`，为 B/C/D 单域开发提供只记录身份和
  计数的 P4 consumer、review input、projection/overlay producer 替身；新增
  `docs/reports/pipeline-a2-public-skeleton.md` 作为 A2 交接报告。

- 用户发送“继续”后，A 复核当前工作树中新到达的 B1/C1/D1 阶段 1 报告，并发布 G1。新增
  `docs/reports/pipeline-g1-contract-base.md`，固定
  `CONTRACT_BASE=45bd2fab992d36eb873b7c448fbd9739f0cad43c`、三方 `ready_for_gate=yes`
  状态、唯一 owner/schema 清单、接口申请归并结果和 A2/B2/C2/D2/G2 合并顺序；同时纳入
  B1/C1/D1 交接报告文件，便于阶段 2 从同一公共契约基线开始。

- 用户指定本任务为 Codex A，并要求严格按 `docs/plan_pipeline.md` 先完成 A1.x，等待 B1/C1/D1
  报告后再发布 `CONTRACT_BASE` 和 G1。新增 A1 基线报告
  `docs/reports/pipeline-a1-baseline.md`，记录当前 `HEAD/upstream`、owner 范围、质量/
  测试/package/性能/Web 三档基线、已知既有失败、B/C/D 报告等待状态和 G1 未发布状态。
  新增契约测试固定 pipeline 双层内存口径、公共接缝版本、owner 归属和 G1 等待条件。

- 用户继续执行全工程重构计划 2.5 整节。新增 cache schema v6 的 P1-P6 六池、Polars
  `ColumnarQuoteBatch` 与 `MarketChangeSet`、内存式 `PublishedSnapshotIndex`、20 个完整
  三策略交易日驻留、按日期 single-flight 的三策略冷读、11:19:50/14:49:50 冻结检查点、
  schema-v2 推荐/overlay SSE patch、源目录只读的 `migrate-v17` 和固定无网络 `perf-check`。

- 用户继续执行全工程重构计划 2.4。新增行情源协调器、DeepSeek 复核上下文/请求执行器/
  状态跟踪器、原子预算批次仓库和预算报告器等有类型组合组件；新增统一的适配器失败码，
  覆盖超时、截止、熔断、负缓存、取消、被更新任务取代、无数据、限流、schema 和源失败。

- 用户要求把 SDK/API 采集、结构化和 Web 实时展示的优化方案写入独立文档。新增非权威
  `docs/plan_sudu.md`，将已选定的 Polars 列式方案落实为 provider 三段式适配、P1-P3
  列式批次、dirty code/board/industry 增量重算、P6 热投影、SSE v2 差量补丁、浏览器
  局部 DOM 更新、完整阶段观测、scalar 等价回退和量化激活门禁，并记录 NautilusTrader、
  vn.py、OpenBB、Qlib、Arrow/Polars 及本地开源库的吸收边界。

- 用户继续执行全工程重构计划 2.3。新增按行情、报价、研究、参考数据、快照、事件、复核与
  结果拆分的应用端口，以及不可变的流水线依赖、选项和资源集合。

- 用户要求把 DeepSeek 各类物理请求“什么时候使用”写清楚。`docs/plan_c.md` 新增共享预热、
  today、tomorrow、d25、Pro 和 emergency 的使用时段、准入条件、停止提交时间、跨策略
  归属和物理请求计数规则，并给出主审 58 次、含 Pro 66 次、含 emergency 71 次的正常日
  计划上界。该文档仍是非权威执行计划，不表示活动策略已经切换。

- 用户继续执行全工程重构计划的 2.2 整节，并再次强调源码仍以 800 行为上限而不是机械拆成
  更小文件。新增领域能力拓扑契约，固定 `market`、`recommendation`、`review`、`outcome`
  四个包及旧根级路径零容忍；新增板块横截面、融合、动作/选择、长期研究、风险映射和结果
  结算的有类型请求值对象，复杂调用不再依赖长参数表或动态类型导出。

- 用户诉求：再次审查完整评分荐股策略，并形成让 DeepSeek API 以较低成本发挥更大作用的
  执行计划。现状判断是本地确定性候选、过滤、风险和冻结边界应继续保留，主要浪费来自
  重复五维打分、重复 prompt、整批修复、阶段化缓存和过高正常调用目标。新增非权威
  `docs/plan_c.md`，规划改用“催化与风险事实提取 + 本地确定性映射”、跨策略复用、1-8 股
  自适应批次、逐股修复、Flash/Pro 分层和 70-83 次正常日软目标；仅用正式推荐到期结果
  做在线关联观察，不实施本地回测、shadow 或自动调参。验证要求覆盖文档治理契约及完整
  质量、打包和 wheel 安装门禁。剩余风险：该计划尚未实施，也不能在缺少可靠对照时宣称
  已提高实际荐股收益。

- 用户要求继续实施全工程重构计划，并确认活动源码继续采用 800 行上限而非任意 500 行
  限制。新增六个独立工程重构章节、章节状态和严格 Ruff 债务单调收敛门禁；2.1 当时登记
  `C901=42`、`N818=7`、`PLR0911=16`、`PLR0912=15`、`PLR0913=69`、`PLR0915=14`，
  任一计数变化都必须经 Review 并同步更新，最终目录切换时全部归零。

- 用户诉求：热点本身可以接受，首要目标是避免推荐次日出现大幅回撤。新增 v17 下行保护，
  用 ATR20 日内反转、趋势破位、板内低波动/低回撤尾部和弱市弱收盘四类结构事实，把原本
  可执行的高风险候选降为观察；必要风险输入缺失时关闭可执行入口，单纯热门或高热不触发
  降级。新增缩量回踩/放量突破入场质量，以及冻结推荐 T+1、T+2/T+3/T+5 的收益、20bp
  成本净超额、MAE 和 MAE/ATR 后台审计表。

- 用户诉求：把 SDK/API 取数、标准化、硬过滤、评分和 Web 展示的完整优化分析写入
  `docs/plan.md`。新增非权威执行计划，记录本机固定负载基线、四个开源仓库的可借鉴边界，
  以及按“闭合 v17、历史复权正确性、P1-P6 热路径、候选总体、收益证明”排序的五个批次、
  退出门禁和不承诺收益边界。

- 用户规则：程序持续运行到 15:00 时依赖本次运行已经层层筛选并发布到 P6 的推荐；程序
  重启后先读数据库，数据库缺失才获取同日收盘行情重新得到推荐并写库。新增逐策略收盘
  恢复协调器、P6 选股身份与收盘锚点确定性回放、冷启动三板本地重建，以及
  3/5/10/20/30 秒无重叠退避重试。

- 用户问题：点击推荐股票后的详情抽屉平铺大量空值、计算中间量和技术审计字段，核心结论
  难以识别；同时推荐接口仍为每只股票传输这些页面不再消费的数据。新增 Web envelope
  schema v3 精确字段白名单、精简风险去重投影，以及“推荐结论 / 核心行情 / 评分与风险”
  三组详情契约；核心行情缺失、快照降级和模型未复核仅在实际发生时显示一条可读状态。

- 用户问题：历史推荐中的“今日涨跌”和“锚点至今”再次全部为空，同时同日已经生成的
  临时推荐没有可见页面。新增显式 `view=live` 同交易日临时草稿只读接口与桌面“临时实时”
  视图；新增 14:50 后冷启动的一次性 P2 当日报价索引恢复任务，不经过候选、评分、
  DeepSeek 或冻结写入。

- 用户诉求：适配器层名称过长，希望统一缩短为 `infra`。新增架构契约，要求
  `src/trader/infra` 必须存在、旧目录必须消失，且活动源码必须统一导入
  `trader.infra` 命名空间。

- 用户诉求：后台行情应在 SDK/API 能稳定返回时尽可能实时刷新，并允许按实际耗时自动调整。
  新增周期全市场必须物理刷新、临时空筛选保留候选、实时事件依赖顺序和生产秒级 cadence
  契约回归；状态计数 `candidate_selection_preserved_degraded` 可审计因历史预热或行情陈旧
  而保留最近候选池的次数。

- 用户诉求：把 `.deepseek_key` 统一改为受保护的 `.token_key`，在同一赋值文件中分别保存 DeepSeek API Key 与 Tushare Token，并在 Tushare 120 积分上限内恢复“今早/明天/2-5 天”从实时采集、历史预热、层层筛选到发布/Web 查询的完整链路。新增双凭据严格解析、120 积分能力矩阵、官方 `daily` 批量日线适配、历史预热覆盖/在途/失败状态，以及旧运行库最近有效前复权日线的只读冷启动种子。

- 用户本轮性能与实时字段回归：新增当前快照/overlay 必须从运行态索引读取且不得触碰持久化仓库、最近历史冻结只预热一次并生成紧凑交付视图、未冻结日期不得负缓存，以及运行态 overlay 随刷新/收盘恢复的契约与集成覆盖。

- 用户补充反馈回归：新增“当前日期只有昨日冻结时必须返回空 not_ready 且无 ETag”、前端不再接受或提示上一交易日 current fallback，以及 P2 特征尚未提交时仍可从已合并规范行情读取历史股票当日价的测试。

- 用户诉求：把 `docs/` 下分散的需求、实施计划、问题单、架构清单和运维资料合并为两份文档。现状审计确认目录内共有 8 份文件、3623 行，活动契约、历史执行记录和未完成 v17 路线相互交叉；新增并相互链接 `software-business-design.md`（产品、架构、运行、API/UI、运维、验收和工程路线的唯一权威）与 `recommendation-strategy.md`（候选、过滤、因子、评分、DeepSeek、融合、动作与 TopK 的唯一权威），契约测试禁止 `docs/` 再出现第三份并行业务文档。

- 用户问题回归：历史推荐表“锚点至今”持续为空，且“今日涨跌”显示冻结锚点涨跌。新增历史 API 对 P2 当前报价索引、未再次入选股票、当日行情缺失和旧日 overlay 隔离的契约测试，并新增行情服务当前报价索引优先采用更新腾讯定向报价的组件测试。

- 用户诉求：继续完成 `docs/hi.md` 中尚未闭合的批次二。现状审计确认上一提交虽已推送 v16 半成品，但计划仍为执行中，核心板块评分/缓存专门测试与性能文件缺失，质量门禁存在 18 个 pytest 失败、23 个 mypy 错误、格式/静态检查失败及 7 个超长活动模块。新增三板评分、缓存、风险、集中度边界测试和固定 360 候选性能 runner/fixture；补齐三 lane 单 worker及队列等待观测、板内同行/领先组边界、缓存 epoch 隔离、七项风险去重与 25 分截断、TopK 60% 和竞争组限制证据。

- 用户诉求：将基于 `58e6d39` 的本地 v15 修改恢复到已前进的远端分支；给出的 460KB tar 只包含 13 个重叠文件，不能代表完整工作树。审计确认旧工作目录仍保留同基点的完整安全 stash（66 个文件），因此以完整 stash 恢复源码、配置、测试、性能 fixture 和文档，并把后续三批远端修复按语义合并：页面快照身份对账继续使用 `dashboard.js?v=8`，结构化研究成功缓存继续复用，腾讯候选/TopK 报价在五来源普通 worker 之外保留独立紧急执行位。为保持活动源码 500 行门禁，来源 latest-wins 生命周期独立到应用层 `source_lanes.py`；未把仅含重叠文件的 tar 当作完整实现覆盖当前树。

- 用户问题：顶部持续显示 `TopK live overlay degraded: data source task exceeded its batch deadline`。现场 `/api/status` 显示腾讯定向报价 917/917 成功、P95 约 700ms、无熔断，而共享数据池 6 个 worker 已全部被全市场、历史或研究任务占用且有排队任务；新增数据池紧急 lane 指标与对应排查说明，用来源延迟和 lane 状态区分真实腾讯超时与内部 FIFO 饥饿。

- 用户问题：页面反复提示 `deepseek_incomplete`、`tomorrow_tail_data_incomplete` 和 `d25_structured_research_incomplete`。运行审计确认 DeepSeek 当日存在 73 次无 HTTP 状态的读取超时且相关阶段额度已消耗，tomorrow 尾盘分钟覆盖随后已恢复为 7/7，D25 结构化研究则因每 3 分钟强制重抓全部候选而在 8 秒批次截止下反复处理固定排序前部代码；新增三类提示的可操作排查与恢复说明。

- 用户问题：今早、明日和 2-5 日页面在服务端已经生成今日实时快照后仍可能停留在昨日数据。现场运行库与只读 API 确认 `2026-07-21` 草稿、冻结和秒级候选报价均正常发布；新增前端状态心跳快照身份对账，仅当服务端当前策略 `snapshot_id` 与页面身份不一致时补拉一次推荐，作为 SSE 推送之外的低流量恢复路径。

- 用户诉求：按 `docs/hi.md` 执行 v15 多源并行采集、统一缓存、结构化合并和三板第一批风险门，同时保持 v14 评分、动作阈值与 TopK 不变。新增东方财富、新浪、腾讯、Tushare、AKShare 五个 latest-wins 来源 lane、不可变 `SourceObservation`、确定性 `CanonicalMarketSnapshot`、应用层缓存身份与唯一有界 LRU/负缓存/single-flight、Tushare 可选 extra 和慢数据适配，以及板块身份、上市日龄、交易规则、逐字段来源、冲突与降级的冻结/API/UI 加法审计字段。新增固定 5500 行全市场和 360 候选性能工具与脱敏 fixture；本批不启用 v16 同行、领先滞后、换手冲击、板内评分或新 TopK 选择器。

- 用户诉求：检查 `docs/need.md` 最近三次提交后，补齐整个活动工程中尚未实现或与契约不一致的功能，并以职责边界拆分超大文件。新增配置化结构风险硬过滤、V4-Flash 主审/V4-Pro 挑战者、固定点时证据路由、挑战者保守合并与策略级缓存、模型/指纹/cache-token 审计、结构化风险冻结重放，以及活动 Python/CSS/JavaScript/HTML 的 500 行架构门禁；新增失败、迟到、预算、schema 修复、黑名单、结构风险和一字涨跌停回归。

- 用户诉求：把 `docs/hi.md` 从方向性方案改为 Codex 可直接执行的详细计划。文档现已固定统一执行协议、两个独立交付批次、允许修改的文件范围、失败先行测试、类型和接口、五个数据源 lane、三个板块评分 lane、精确候选/评分权重、DeepSeek 全局协调、融合与故障降级、逐项验收矩阵、提交信息和停止条件；本批仍不修改活动契约、配置或实现。

- 用户诉求：将多源并行采集、沪深主板/创业板/科创板独立评分、结构化合并、DeepSeek全局协调、TopK集中度和分批交付门禁形成完整计划文档。新增 `docs/hi.md`，明确 v15 数据合并批次与 v16 三板评分批次、来源职责、板内候选/评分权重、合并 epoch、故障降级、API/冻结兼容、测试验收和未验证收益风险。本次仅新增计划文档，未改变 `docs/need.md`、运行配置或活动代码。

- 用户问题：`GET /api/status` 先报 `sqlite3.OperationalError: unable to open database file`，随后 Werkzeug 报 `OSError: [Errno 24] Too many open files`。原因已确认：DeepSeek 预算库与共享快照库都把 `sqlite3.Connection` 当作会自动释放资源的上下文管理器使用，但该上下文只提交或回滚事务、不关闭连接；页面轮询状态时会从预算摘要和持久化观测路径遗留数据库文件描述符。修改后两个 SQLite 边界在成功、提前返回、初始化失败和异常退出时都确定关闭；预算运行库暂时不可访问时，DeepSeek 状态返回可解析的 `budget_ledger_unavailable` 降级结果，`/api/status` 继续只读返回 200，顶部额度显示“不可用”而不是产生 500 或伪造可用余额。
- 用户问题：`TopK live overlay degraded: data source task exceeded its batch deadline` 等最近错误过长时挤压顶部其他状态。原因是最近错误与行情、推送、评分、DeepSeek 和冻结状态共用 `nowrap` flex 行。修改后最近错误成为 Header 独立第二行，标签与正文分列，正文允许对无空格错误码任意断词换行；第一行状态不再被错误长度占用，900px 以下已有窄屏布局顺延错误行但不增加业务分支。验证：静态页面/CSS 契约、`make format-check/lint/type-check/test/package` 全部通过（417 tests），仓库外 wheel 的 CSS v4、独立错误节点、断行规则、CLI 和 `pip check` 通过。剩余风险：1280x720、1440x900、1920x1080 无头截图被宿主 Firefox 的 SWGL framebuffer 映射故障阻断，机器无 Chromium 备选；未发现代码侧已知问题，但本批不能宣称截图门禁通过。
- 用户问题：DeepSeek 五维结果显示 `rejected`。运行审计确认三个策略批次均因 `api_key_missing` 跳过，原因是受保护的 `.deepseek_key` 已存在且权限为 `600`，但 v2 只读取进程环境。修改后配置边界按“`DEEPSEEK_API_KEY` > `DEEPSEEK_API_KEY_FILE` > 项目根目录 `.deepseek_key`”加载密钥，安全解析单行原值或赋值格式并拒绝 POSIX group/other 可读文件；页面按错误类别显示“未配置、禁用、额度、截止、调用失败或结构校验拒绝”，数据库 `rejected` 终态及 `local_degraded` 门保持不变。验证覆盖文件加载、环境优先、不安全权限、零物理调用审计和静态资源契约；`make format-check/lint/type-check/test/package` 全部通过（417 tests），仓库外干净虚拟环境 wheel 导入、资源、CLI 和 `pip check` 通过。剩余风险：外部 DeepSeek HTTP 有效性仍取决于用户密钥、网络和供应商服务，本批未发起消耗额度的真实请求。
- 本批优化（2026-07-20）聚焦评分与荐股策略参数化链路：在 `StrategySettings` 增加 `local_strategy_weights` 配置并下发到 `RecommendationPolicy`，`application/recommendations.py` 的本地评分与评分融合改为透传该权重；`FrozenReplayPolicy` 与快照序列化/反序列化（`infra/persistence/snapshots.py`）支持 `local_strategy_weights`，旧快照缺失字段回退默认组件权重，未改变 68/32 融合公式与风险扣分上限。
- 本轮（2026-07-20）补齐 DeepSeek 审计字段闭环：schema 解析、审核短路/失败路径注入、持久化往返与 API review 节点透出全部落地，并补 `tests/component/test_v2_deepseek.py`、`tests/component/test_v2_persistence.py`、`tests/contract/test_v2_web_api.py` 回归。
- 本批（2026-07-20）补齐 DeepSeek 审计字段闭环，但保持字段只读，不允许 `challenger_status`、`review_stage`、`rating` 或置信度改变动作、融合或 TopK 排序。
- 本次“收益型 shadow”落地：`domain/strategies/shadow.py` 新增 today/tomorrow/d25 的同行收益差/领导对照影子评分路径；`application/recommendations.py` 在快照元数据输出 `shadow_scoring`（覆盖率、`top_shadows`、`rank_gap`）供离线审阅，不改动生产排序门槛与 `68/32` 融合。
- 本批续作（2026-07-20）聚焦“标准化/路由/数据库”风险可回归：新增 `tests/unit/test_v2_market_data_router.py` 覆盖 required/optional 混合降级、无数据优先语义、failed/vendor 汇总；新增 `tests/unit/test_v2_sqlite_migrations.py` 覆盖 schema 初始化、幂等性与旧 schema 升级迁移。
- 本轮“标准化/路由/数据库”继续推进（2026-07-20）：新增标准化输入收口（`MarketQuoteInput` 字段时区/格式校验）并在 `normalize_quotes` 中隔离坏行；路由空结果判定收口到 `_is_empty_payload()`；补充 SQLite 遗留 schema_version（`N/A`）可恢复测试。
- 本轮“标准化/路由/数据库”补齐（2026-07-20）：`normalize.py` 将 `MarketQuoteInput` 进一步限定到 `6` 位数字代码、非空 `source/data_version` 与可用时区，新增非法输入回归，确保坏行情仅在标准化层隔离，不影响后续评分与异常归类。
- 本批（2026-07-20）补充本地评分链路回归：新增 `tests/unit/domain/test_strategies.py` 覆盖 `local_strategy_weights` 覆盖注入后的组件打分变化；新增 `tests/unit/application/test_recommendations.py` 覆盖快照重放策略字段与今日推荐排序是否同步 `RecommendationEngine` 的权重注入。
- 落地 `docs/issues/2026-07-20.md` 第三档 P12 证据落盘统一：`AkshareResearchClient` 的研究源（news/financial/announcement/pledge/unlock）原始载荷统一持久化到 `runtime/evidence_cache/<source>/<code>.json`；`MarketFeatureService` 的 `research` 读取路径加入持久化重放与过期清理，在重启场景可在 TTL 内优先命中 cache 并回放 `ResearchObservation`。
- 本轮“标准化/路由/数据库”继续子项（2026-07-20）路由可观测性细化：`gateway.py` 的 `health()["route"]` 新增 `attempted_count/success_count/failure_count/no_data_count/skipped_count` 并保留 vendor 序列轨迹；`router.py` 将 `circuit_open` 映射为 `status="skipped"`，用于识别熔断跳过与失败差异。
- 本轮（2026-07-20）进一步补齐路由语义：`router.py` 将 optional 供应商的 `no_data` 标记为 `skipped` 而非 `failed`，`gateway.py` 的候选报价腾讯源在熔断窗口内也计入 `route` 健康 `skipped_count`，区分可恢复降级与真实失败。
- 本轮续作（2026-07-20）补齐数据库观测闭环：`infra/persistence/sqlite.py` 将 `SCHEMA_VERSION` 提升至 4 并新增 `data_source_health` 持久化字段 `route_json/route_status/route_fallback_reason/route_degraded`；`infra/persistence/writer.py` 持久化 `market_data.health()["route"]` 结构，形成从路由结果到 SQLite 行记录的可回放链路。
- 用户诉求：标准化收敛。新增 `infra/market_data/normalize.py`，提供 `to_float`、`normalize_quotes`、`MarketQuoteInput` 与 `build_market_quote`；`eastmoney.py`、`sina.py`、`tencent.py` 统一调用该入口组装 `MarketQuote`，非有限值与异常字段统一降噪，行为口径与字段名保持不变。
- 用户诉求：标准化契约闭环。`infra/market_data/features.py` 新增 `FEATURE_SCHEMA` 及版本常量，并在配置层校验 `factor_contract.feature_schema_version` 与注册表一致，`tests/unit/test_v2_settings.py` 增补 `feature_schema_version` 错配拦截与可选 `feature_names/feature_schema_expected` 一致性验证回归。
- 本批数据库健壮性补齐：`tests/unit/test_v2_sqlite_migrations.py` 新增空白 `schema_meta` 值回归（覆盖 `schema_version=''` 与 `N/A` 场景），验证初始化可自动回写 `schema_version=SCHEMA_VERSION`。
- 用户诉求：完成 `docs/issues/2026-07-20.md` 第四档 Web 序列化收敛（P15）。新增 `web/serializers.py`，将路由错误、历史日期列表和事件列表响应统一为独立序列化入口；`web/routes.py` 保持仅作参数校验与状态分支。接口字段与错误码不变。
- 用户诉求：继续推进 `docs/issues/2026-07-20.md` 剩余未完成项目（P6/P10/P11/P12/P13）。1) `bootstrap.py` 保持唯一组合根并通过小型工厂注入具体适配器。2) 新增 `FeatureSchema` dataclass + `RAW_FEATURE_SCHEMA`/`DERIVED_FEATURE_SCHEMA` 注册表。3) `BoundedExecutor` 继续作为有界执行内核。4) evidence 与 observation 缓存分命名空间并共用唯一持久化 worker。
- 用户诉求：按 `docs/issues/2026-07-20.md` 第二至四档继续落地优化（P5-P16）。新增市场数据路由、required/optional 过滤审计、受控 `Rating` 审计枚举和 SQLite 迁移注册表；`Rating` 不映射生产动作，避免模型自由文本绕过本地风险规则。
- 用户诉求：对照 `X:\github\TradingAgents` 开源库对全链路流水线做系统审查并落地第一批优化（P1-P4）。现状是 DeepSeek 客户端为单一具体实现，不支持测试注入 mock，也不处理 `deepseek-reasoner`/`deepseek-v4-pro` 思考模型的 `reasoning_content` round-trip；DeepSeek prompt 中没有独立的权威数字快照，模型可能对本地已计算字段产生幻觉。修改后：1) 新增 `DeepSeekClientBase` ABC + `DeepSeekHttpClient` 实现，`DeepSeekReviewer` 接受抽象接口使测试可注入 mock；2) 新增 `ModelCapabilities` 声明表，按模型自动控制 `temperature`、`reasoning_effort`、`reasoning_content` round-trip 和 `response_format`，解决 reasoner 模型 400 错误；3) 新增 `model_catalog.py` 已知模型白名单 + 非阻塞警告，未知/即将停用模型启动时提示；4) 新增 `GroundTruthRenderer` 把 `FeatureSnapshot` 渲染为确定性数字快照供 DeepSeek prompt 使用，降低幻觉边界。新增 `tests/unit/test_v2_deepseek_base.py` 覆盖 ABC、capabilities、catalog、round-trip 和 ground truth 渲染。
- 用户诉求：把 `docs/back1.md` 中更可能改善收益质量的策略合并到唯一生产契约 `docs/need.md`。现状是 back1 同时混有三板机制、候选权重、离线晋级、机器学习和低星项目引用，不能整体视为已验证生产方案。修改后新增第 26 节 `strategy_v10_board_aware_draft`，固定三板身份、板内总体、同行收益差、领先滞后、换手/成交冲击、执行质量和集中度机制，并将六组精确权重、P50/P80 与 0.85 明确登记为不影响当前生产的 `candidate_initial`。
- 用户诉求：补充当前高关注度的量化和 DeepSeek 荐股开源参考，同时删除低于 20K Star 的链接。修改后新增 OpenBB、NautilusTrader、FinGPT 和 LEAN 四个达到门槛且与金融数据、量化事件模型或金融 LLM 直接相关的 canonical 仓库，并明确只有 TradingAgents、daily_stock_analysis 和 TradingAgents-CN 属于 DeepSeek 接入参考，避免把一般金融 AI 项目误称为 DeepSeek 荐股库。
- 用户诉求：结合网络上一手资料优化 `docs/need.md` 中 DeepSeek API 荐股方案，但暂不处理“离线验证与晋级”。现状是需求仍以即将停用的 `deepseek-chat` 单模型五维复核为中心，16 条证据只有总上限，没有固定类别、反证和点时路由，模型自报置信度也容易被误解为真实概率。修改后固化 V4-Flash 非思考主审、V4-Pro 思考挑战者、16 条点时证据配额、稳定 prompt 前缀/上下文缓存审计、三态挑战结论和模型身份；校准字段只作可空影子审计，不定义样本量、统计门槛或生产晋级。
- 用户诉求：把本库策略所依赖或借鉴的 GitHub 高星项目链接写入 `docs/need.md`，确认使用 DeepSeek API 进行股票推荐的开源库，并恢复 `strategy_and_prediction.md` 历史中的方法来源。现状是第 2 节只有六个无链接名称，末尾另有不可渲染的重复终端表格，未区分实际运行依赖、机制参考和 DeepSeek 荐股类项目。修改后统一记录 canonical 仓库、可复核的 2026-07-19 Star 快照与借鉴边界；12 个历史策略参考可追溯到首次提交 `841355c`，并补入 DeepSeek/A 股项目和实际依赖 AKShare，后续按当前 Star 门槛筛选展示。
- 用户诉求：连续完成第 19-25 节代码，最后统一补测试并 Review。现状是当前/历史响应身份不足、跨日冻结缓存表达含混，状态缺少可重启查询的来源/DeepSeek/冻结审计。修改后推荐 envelope 新增请求日期、当前交易日、历史/fallback 身份，历史行可叠加独立当前行情；新增持久化来源健康、逐物理 DeepSeek 调用和冻结证据汇总，以及本地 Lucide 图标资源。第 24 节已有固定输入完整日证据，需求文档没有第 26 节，均未重复或虚构实现。
- 用户诉求：先统一完成第 14-16 节 DeepSeek 代码，再补测试和 Review。现状是候选会被定性证据门提前清空，批次/候选状态混用，只有六桶总额而没有阶段目标与上限。修改后新增持久化批次和逐股终态、十阶段 133 次目标/188 次上限、受条件约束的 emergency、原始/策略两级缓存、优先复核及重启 `abandoned` 恢复；新闻或公告不再作为调用资格。
- 新增架构契约测试，固定快照编排模块必须使用职责明确的 `snapshot_workflow.py`，并禁止旧快照编排路径重新出现。
- 用户诉求：继续闭合 `docs/issues/2026-07-17.md` 中第 4-7 节未完成任务；现状是配置中的 worker 和刷新频率未形成真实消费者，关键单点可能因调度延迟错过，TopK 无独立报价链，数据年龄、乱序、单飞、熔断和发布延迟缺少统一证据。修改后新增生命周期受控的有界执行器和独立 cadence 计划器，真实启动数据、标准化、三策略、DeepSeek、合并、持久化及 long 消费者，并形成全日计划、来源恢复、实时 overlay 和时效状态回归。
- 新增第 13 节 d25/long 点时研究输入：d25 候选和固定 long 名单按代码获取东方财富财务、精确发布时间公告、累计质押比例与未来 90 天解禁比例；纯领域公式生成价值、成长、质量、行业/政策、风险保护及四类本地风险，来源时间、接收时间、版本、脱敏原始摘要和派生摘要随输入保存。
- 新增第 12 节 tomorrow 候选级未复权 1 分钟输入：按连续 30 个交易分钟计算原始收益和尾盘量比，再通过配置化固定公式映射到 0-100；分钟来源、源时间、接收时间、输入版本和公式中间值随证据及冻结回放保存。
- 用户问题：需要比较 `docs/archive/v1` 与当前 `docs/need.md` 的交易、过滤和评分策略，并判断还能如何优化；现状判断：v1 归档同时包含生产规范、运行配置和不具生产约束力的研究计划，不能拼成一套口径。修改说明：以 v1 自称唯一规范的 `strategy_and_prediction.md` 及其 `config/runtime.json` 为旧生产基线，以 `plan.md` 仅补充研究证据，完成策略角色/时间线、候选与硬过滤、本地/DeepSeek 评分、TopK、退出验证和长期池的可追踪差异归纳，并形成不越过当前只读研究看板边界的分阶段优化建议。
- 新增第 11 节 today 点时新闻信号：候选新闻按配置化正负关键词多数规则生成 75/50/25 极性均值，并按最新有效证据年龄生成 1 小时满分、72 小时线性归零的新鲜度；纯领域计算、配置校验、候选特征、冻结输入和离线回放形成闭环。
- 新增第 10 节配置驱动本地风险表：逐项登记适用策略、触发因子/运算符/精确阈值、严重度、扣分、置信度、证据有效期、互斥/叠加组和稳定事实 ID 字段；风险明细增加实际值与阈值并随冻结、API 和桌面明细返回。
- 新增逐股 `filter_details` 审计记录，保存股票代码、`filter_code`、阈值、实际 JSON 标量、来源和时间，并随冻结 JSON 往返及通过只读 API 返回。
- 新增第 8 节完整因子登记表和严格 schema：52 个生产因子逐项声明策略、输入、公式、单位、方向、时点、复权、窗口、样本、截尾、归一化、缺失策略、范围与版本；横截面统计随冻结输入保存并由 API 返回。
- 新增与冻结快照身份绑定的可恢复 `live_overlay`：冻结后只刷新 TopK 当前报价，独立持久化版本、点时时间和收盘标志，通过 SSE/ETag 通知而不改写冻结 JSON；15:00 后首份有效收盘值禁止被迟到结果覆盖。
- 新增 `trader-cli threshold-report` 冻结输入预注册报告，按策略输出完整回放候选的分数分布、推荐数、空推荐比例、相邻 TopK Jaccard 变化、DeepSeek 覆盖、整版本地降级比例和风险拦截率，并拒绝混合策略/融合版本。
- 用户问题：今日多项 Bug 缺少集中状态记录；修改说明：新增 `docs/issues/2026-07-17.md`，逐项归纳行情降级、Tab 加载、空值展示、缺失原因、AKShare JSONP、DeepSeek 进程注入和 P2 数据缺口，并区分已修复、待运行生效、待验证和待实现。
- 推荐 API 为 `missing_fields` 同步提供可展示的 `missing_reasons`，明细抽屉直接说明缺失数据的上游原因。
- 候选池接入带 8 秒硬超时的 AKShare 兼容个股新闻证据，新闻证据进入特征快照、DeepSeek 输入和冻结回放；成功结果缓存 10 分钟，失败负缓存 60 秒。
- 新增第 25 节最终验收闭环：新冻结快照保存完整市场预选输入、定向候选与经校验 DeepSeek 结果，`trader-cli verify-freeze` 可离线复算并核对过滤、评分、风险、veto 和排名；`/api/status` 新增活动 TopK 报价 P95 与 DeepSeek 物理调用验收摘要。
- 新增固定行情完整交易日影子门禁，按 09:20-15:00 时间线在两个隔离目录运行真实 SQLite/JSON 冻结链，对照 today/tomorrow/d25 manifest、long 非冻结和全部 JSON SHA-256 确定性。
- 新建单一 `src/trader` 安装包，按 `domain`、`application`、`infra`、`web`、`entrypoints` 和唯一组合根分层。
- 新增 today、tomorrow、d25、long 四策略的确定性评分、硬过滤、风险事实去重、TopK 和动作判定。
- 新增东方财富/新浪/腾讯行情适配、AKShare 研究边界、交易日历、历史特征缓存和多源降级。
- 新增 DeepSeek 五维 schema、证据子集校验、共享代际缓存、逐物理请求原子预算及 188 次六桶上限。
- 新增 SQLite/不可变 JSON staged-committed 冻结协议、哈希校验、隔离恢复、优先事件重放和跨平台单进程锁。
- 新增只读推荐 API、ETag、有界审计查询、SSE 游标恢复/慢客户端隔离及包内桌面工作台资源。
- 新增分层单元、组件、契约和集成测试，以及根级 `AGENTS.md`、迁移清单和 v2 运行手册。
- 用户诉求：为 `docs/hi.md` 增加性能、缓存和实时性优化计划。计划新增独立 v17
  等价硬化批次，并把 v15 行情缓存、v16 评分缓存、固定录制负载、延迟/数据年龄/
  内存预算、背压、状态指标、性能 CLI、回归矩阵和停止条件落实到可执行文件与命令。

### Changed

- 用户要求把“已冻结 · 收盘补算 · 降级…”状态和最近错误移到页面上方并固定高度，随后
  明确摘要行应位于四策略按钮行上方、两行紧邻股票表。页面现将快照状态与最近错误放入
  Header 顶部信息区，两栏均固定为 52px，长文本在各自区域滚动；主体顺序固定为摘要、
  策略/日期、股票表，不再由状态长度改变纵向位置。

- Web 荐股展示批次：移除与主表空态重复的“当前策略尚未发布快照”通知，短线无快照时
  只在表格显示唯一空态并在通知栏提示“等待策略数据更新”；长期策略保留专属当前数据
  说明。摘要栏移除重复的冻结状态、评分版本和内部路由健康，改为最高评分、模型复核
  数和数据状态；行情路由故障仍通过行情来源和最近错误反馈。

- Web 荐股展示批次：用户要求将“实时草稿”改为“实时数据”、保持荐股表格不变、
  不展示观察池并理清四个策略与日期选择。当前日期继续只读 `view=current`，历史日期
  继续使用显式 `date`；短线主表只显示正式推荐，long 在同一主表显示当前固定名单，
  进入或离开 long 均从“当前”开始。长期未就绪文案改为“长期策略当前尚无可用数据”。

- 15:00 后冷启动收盘补算的完整性判断从“存在三板收盘价”收紧为主板、创业板、科创板
  分别至少 100 只具备 20 日有效流动性历史；历史热缓存仍在预热时保持 `not_ready` 并
  按既有 3/5/10/20/30 秒退避重试，不提前冻结三板样本不足的半成品。

- 桌面首页改为单一“当前推荐”状态入口，策略或日期切换仍隔离迟到请求；Web 明确请求
  `view=current`，SSE 可在同一页面从草稿无缝切换为冻结结果，冻结完成后拒绝迟到草稿。
  省略 `view` 的 API 仍保持原 `official` 默认语义，不把 UI 调整扩散为调用方兼容破坏。

- 历史预热 360 个槽改为只在主板、创业板、科创板间稳定轮询，每板最多 120 个；保留
  板内至少 100 个样本和可靠度 0.85 的原风控门槛，不通过降低门槛制造荐股。

- 正式 `perf-check` 从 `infra` 迁到入口层并改为调用活动标准化、融合、列式投影、板内
  评分、全局选择、推荐准备/终态化、P6/SSE 和 Web 路由。性能配置升级为 schema v2，
  固定 5500/360/120/18 行与三策略负载，并将标准化、融合、canonical、targeted commit、
  SSE 和浏览器预算收紧到 `docs/times.md` T1 约定值。

- 三份专项旧计划同时包含已实施方案、阶段施工指令和未落地拟议值，继续保留会形成第二套
  策略真相源。本批以活动代码、`config/v2`、G5 报告和现有测试为准，把已实施内容改写为
  长期契约；A/B/C/D 阶段分工只保留在 `docs/reports/` 的历史证据中。活动行为不变：
  V4 仍使用至少 2 个有效维度和 0.50 覆盖门禁，today 主审/挑战者分别以 11:20/11:18
  截止，long 永久零物理请求，融合仍为 0.68/0.32 且固定向量为 `83.40`。`plan.md`
  的重构章节已全部闭合且相对当前权威文档过时，按用户确认不再重复归并。

- 组合根仍唯一位于 `bootstrap.py`，`ApplicationSystem` 继续显式拥有 worker、source
  lane、pipeline、持久化和 DeepSeek 生命周期；流水线初始化、冻结恢复、事件执行、市场
  缓存、评分准备和审查发布改为分阶段函数与不可变请求对象。Web 由单文件路由改为薄注册
  facade，保持原 API、ETag、SSE 游标、错误码和桌面 payload 等价。

- `docs/times.md` 将后续实时性能工作固定为先建立真实延迟瀑布，再依次处理 SDK 连接与
  有界分页、列式/dirty 提交、本地推荐先发布和浏览器逐行绘制；本批不修改生产线程、
  `fusion_mode`、API/SSE schema、评分、冻结或运行配置。

- 本批只补充后续策略研究路线，不修改活动 v17、固定 0.68/0.32 融合、硬预算、冻结、
  运行配置、API schema 或 Web 行为。计划要求先恢复至少 95% 历史覆盖并积累点时配对样本，
  所有判断条件调整先以不增加 DeepSeek 请求的 v18 影子运行。

- G5 不改变任何产品、策略、schema、配置、迁移、运行代码或 Web 资源；最终交付状态从
  “A5 已完成、G5 未发布”推进为“G5 已发布”。“最终一个交付 commit”按仓库强制规则落实为
  G5 独立批次只创建一个新提交，保留此前已推送审计历史且不 squash/amend/force-push。

- C5 在不改变公共 schema 版本、融合权重或 HTTP 硬预算的前提下收紧 V4/Pro 解析：拒绝
  未知字段、错误文本类型、超长响应和 manifest 外证据；正向催化/价格/基本面/政策映射只
  接受合格可信证据，跨时区事件按统一时间去重。raw facts prompt/cache 只包含共同结构化
  输入和证据身份，普通策略分数、board policy 与 merge epoch 不再拆分跨策略原始复核。

- P6 resident 初始化、current 恢复、cold future ownership、完整日期装载和按日期淘汰拆为
  单一职责私有方法；完整日期补足、三策略 single-flight、异常传播、LRU 和字节上限行为不变。
  严格复杂度债务从 `C901=38 / PLR0912=16` 降为 `36 / 15`。

- `SnapshotPublisher.resync()` 现在与 Web 游标/慢客户端 resync 使用同一 v2 事件身份，
  固定携带 `patch_schema_version=2`。SSE 订阅在 publisher 锁内保存打开时的服务端序列，
  游标原因分类不再读取生成器运行时可能变化的 sequence；历史回放、队列和事件 ID 不变。

- B5 将成员插入/删除视为所有登记字段族的脏变更，并让板块/行业 dirty 集合同时覆盖旧、新
  快照维度；普通纯报价 overlay 仍保持窄 dirty 路径。B4 报告同步采用其 acceptance JSON
  已保存的准确 P95、RSS/USS 和已发布提交哈希，不改变历史门禁结论。

- B4 将标准化、观察值构造与两源合并的固定组合路径纳入相对验收，严格复杂度债务基线因拆出
  有效观察合并 helper 从 `C901=39` 下调为 `C901=38`；新增的 Polars 入口只在 infra 内部可见，
  不改变公共行情 schema、应用端口、配置或 Web API。

- publisher 状态把 100ms 内部 SSE 入队耗时与 2s 权威发布年龄拆成两个有界 P50/P95 指标；
  无游标的新 SSE 连接从当前 sequence 开始，只在显式 `Last-Event-ID`/`cursor` 时执行历史恢复。
  patch ETag 现与 `snapshot:trade_date:view` HTTP 身份一致，dashboard 资产版本提升到 15。

- A4 质量复验在安装声明的 Polars 后发现 mypy 会递归解析当前 NumPy 第三方 stub，并以
  Python 3.10 目标拒绝 stub 中的 Python 3.12 `type` 语句；`pyproject.toml` 现把 Polars
  明确作为 mypy 外部导入边界，活动 `src/trader` 仍按完整严格规则检查，未改变运行依赖或
  Python 3.10-3.14 产品范围。

- 运行态推荐发布改为 P6-first 接纳：普通草稿、重算和重启恢复只有在 P6 接纳后才更新
  RuntimeState、session、检查点与 SSE；正式冻结继续先落不可变记录，但 P6 拒绝时不切换
  运行态、不设置冻结标记、不消费检查点、不广播新身份；较旧正式三策略记录进入驻留历史时
  日期索引保持倒序且不替换当前投影。

- A3 集成后，权威策略文档从旧 DeepSeek 证据/预算口径收敛到当前实现：每股 prompt 证据
  上限为 12，long 物理请求永久为 0，today/tomorrow/d25/shared_preheat/emergency 软桶为
  22/14/12/10/5，Pro 挑战者批次最多 4 只且全日软上限 8；普通 quote-only change 命中 raw
  facts cache 时只做本地投影和 P6/SSE 发布，不新增 DeepSeek HTTP。

- A3 集成后，权威 Web/SSE 契约明确 recommendation/overlay patch 均使用
  `patch_schema_version=2`，推荐 patch 携带 base/current projection、ETag、view、upserts 和
  removed codes；overlay 只携带报价字段且必须匹配当前 projection，不匹配时触发 ETag
  resync。

- A3 Review 发现 `scripts/check_refactor_quality.py` 的严格债务基线落后于已推送 `HEAD`
  实际计数；已同步为 C901=39、N818=5、PLR0911=15、PLR0912=16、PLR0913=55、PLR0915=11。
  本批新增的列式 options 值对象把自身引入的 PLR0913 增量降回 0。

- G2 状态从阻塞更新为已发布；A 只发布阶段 2 共同门禁，仍不合并 B/C/D 生产实现、不连接真实
  实现、不修改生产默认，A3 等待下一次用户继续指令。

- G2 阻塞原因从“B2 未就绪且 C2 缺标准字段”收敛为“B2 未就绪”。A 继续只登记报告状态，
  不合并 B/C/D 实现、不连接真实实现、不修改生产默认、不开始 A3。

- G2 状态明确为未发布：A 只接收并登记阶段 2 报告，不合并 B/C/D 实现、不连接真实实现、
  不修改生产默认，也不开始 A3；当前等待 B2 补 `ready_for_gate=yes`。

- A2 将运行配置 `performance_budgets.memory` 从旧 `cache_total_bytes` 单字段改为
  `cache_logical_bytes=260046848` 与 `process_peak_rss_bytes=402653184` 双字段，并更新解析
  校验、性能报告 payload 和权威设计说明；旧字段或把进程峰值当缓存容量的配置会在启动前被
  拒绝。同步收紧 `PublishedSnapshot*Port.status()` 返回类型，避免 application 公共边界继续
  暴露 `Mapping[str, object]`。

- A1 基线报告从“等待 C1/D1，G1 未发布”更新为“B1/C1/D1 均已收到，G1 已发布”，并把
  `ready_for_gate` 更新为 `yes`。本批仍不进入 A2.1-A2.5，不实现公共 port/event/config
  骨架，也不执行 B/C/D 内部算法。

- 权威文档将 P1-P6 内存验收从旧 `248/8/256 MiB` 单进程口径调整为
  `248 MiB` 逻辑缓存和 `384 MiB` 迁移期进程峰值 RSS 双层契约，并冻结
  `p3_p4_feature_snapshot_market_change_set_v1`、`p4_p5_high_value_review_manifest_v1`、
  `p4p5_p6_projection_event_v1`、`p6_overlay_event_v1` 与
  `deepseek_v4_review_facts_v1`。本批只做 A 侧契约和基线，不执行 B/C/D 内部算法；
  具体配置、port/event 实现和集成替身留给 A2。

- 本批现状判断：三组 v16 缓存不能隔离观测、规范快照、特征、评分、模型复核和交付生命周期；
  查询用例直接持有归档仓储导致热读访问 SQLite；盘中草稿与 overlay 被完整持久化；SSE
  失效通知又触发浏览器完整 GET。修改后配置严格固定 248 MiB 六池与 8 MiB 运行预留，
  Web 只读用例只依赖 P6，普通草稿/盘中 overlay 只更新 P6 与局部 patch，冻结、检查点和
  closing overlay 才进入持久化边界；活动运行目录由 `.runtime/v2` 隔离到 `.runtime/v17`。

- 行情、DeepSeek、预算与快照观测从多继承 mixin 改为构造时显式注入的类型化组合；复杂预算
  批次入口改用不可变请求/完成对象。历史 K 线现在强制生产者声明 `raw/qfq` 和来源，120 点
  预热主路径只消费腾讯或供应商明确返回的 qfq 数据；融合公式、排名、冻结、API 和 Web
  行为未改变。

- 现状判断是外部 SDK 延迟之外的主要浪费来自 Python 行对象重复物化、变化范围过度重算和
  SSE 通知后的完整 HTTP 回读；计划固定 Polars 只进入 `infra` 的 P1-P3，最多 360 候选
  在 P4 前物化为现有领域对象，不复制评分实现，也不增加 DeepSeek 请求。权威文档治理
  同步登记 `plan_sudu.md` 仅可细化既有第 7.2 节 P1-P6 原子章节，禁止形成第二套契约。

- 流水线事件审计改为有类型记录和状态枚举，持久化边界负责转换，跨线程 JSON 深层冻结；
  24 参数构造函数收敛为三个显式对象，业务时间线、Web v3 和冻结投影保持不变。

- 用户决定暂缓历史 60 日数据与 300 条配对建设，先完整设计实时荐股和 DeepSeek 低成本
  协同方案。重写 `docs/plan_c.md`，固定本地主链、新闻/公告证据门槛、V4 结构化事实、
  确定性映射、高价值候选路由、Flash/Pro 分工、跨策略缓存、实施批次和延期晋级门禁；
  历史回补、shadow、Bootstrap、自动调权和在线学习均移到后续独立任务。

- 领域值对象和纯函数按行情事实、推荐决策、结构化复核、冻结结果四种能力重组；过滤规则改为
  表驱动注册对象，板内横截面构建拆为样本基准、人口统计和特征丰富三个阶段，长期研究拆为
  估值、成长、质量、事件、行业政策和保护纯函数，排名限制改为显式策略与选择状态。活动源码
  仍全部低于 800 行，领域严格 Ruff 告警由 18 项降为 0，仓库债务基线从 163 项降为 145 项。

- `SelectionPolicy` 与 `RecommendationPolicy` 的空映射和默认硬过滤策略改由 dataclass
  `default_factory` 为每个实例创建，再沿用既有不可变副本逻辑；候选、评分、风险、融合、
  排名、冻结、Web API 和持久化格式均不变。Ruff 开发工具固定为 0.15.21，避免严格诊断
  基线因工具规则版本漂移而失真；原候选总体和收益证明路线保留为工程重构后的独立策略批次。

- tomorrow 三板各把 15% 权重转给确定性入场质量，d25 用入场质量替代原独立
  `not_overheated` 正向组件；融合公式、DeepSeek 预算和冻结时点保持不变。正式推荐与观察
  候选改为独立 Top10/Top8，再分别应用相同集中度规则，Web 当前视图分为“正式推荐”和
  “观察列表”，历史视图与原 `items` 单数组兼容契约不变。

- 文档治理仍保留软件业务设计和荐股策略两份唯一权威，但允许 `docs/plan.md` 作为唯一的
  非权威待办载体；计划与权威契约冲突时以后者为准，实施引起契约变化时必须先修改权威
  文档。交付契约测试同步限制 `docs/` 为两份权威文档加这一份执行计划。

- 用户要求行情服务做“类级完全重构”，而不是继续保留九个 mixin、单个共享状态基类和
  隐式继承依赖。现改为由 `bootstrap.py` 显式组装 `QuoteCache`、`HistoryCache`、
  `HistoryWarmup`、`ResearchLoader`、`IntradayLoader`、`ReferenceLoader` 与执行、健康
  组件；每个组件独立拥有有类型的状态、锁、缓存和外部资源。`MarketFeatureService` 只负责
  协调并实现既有 `MarketDataPort`/当前报价读取边界，不再持有通用 `_lock`、业务缓存或
  模板方法。产品 API、评分、冻结时点、运行数据与序列化格式保持不变。

- 用户诉求：按工程 Review 计划先修复 v15 行情热路径性能。固定 5,500 股双源负载下，
  两源合并和统一快照 P95 分别约为 1,761ms 与 2,252ms，超过 1,000ms/1,500ms 契约；
  根因是逐字段反复规范化来源、分配候选列表并扫描排序，以及规范 JSON 在 Python 中递归
  复制完整对象并重复反射 dataclass 字段。现改为单次扫描选择各字段、对小型来源/字段元数据
  使用有界缓存，并让标准 JSON 编码器原生遍历容器。字段优先级、同序首值规则、冻结 schema、
  缓存键、哈希和内存估算口径均保持不变；本批不处理计划中的下一项历史预热优化。

- `close_fallback` 现在是数据库缺少同日正式记录时创建的正常历史结果，而非临时草稿：
  连续运行保持 P6 股票、评分、动作和排名，仅换入收盘价；冷启动不新增 DeepSeek HTTP，
  使用本地规则完整重建并由正式接口返回 `ready`。页面明确标记“收盘补算”，并提升静态
  资源版本避免浏览器复用旧脚本；已有同日记录仍拥有最高优先级且不可覆盖。

- 用户诉求：将活动源码单文件行数门禁从 500 行调整为 800 行。现状是权威设计与架构
  契约测试同时固定 500 行，单改文档会造成契约与门禁不一致；修改后
  `software-business-design.md` 与架构测试统一以 800 行为上限，超过上限仍必须按职责
  拆分并说明，且继续禁止含义模糊的聚合模块。本批不改变运行逻辑、策略、API、冻结格式
  或打包内容。

- 推荐接口保留原路由并直接升级为 v3，不提供 v2 兼容 shim；逐股响应只保留身份、核心/
  锚点行情、报价身份、动作、四项关键评分、精简风险和复核终态。完整特征、证据、板块
  计算、缺失原因和 DeepSeek 技术审计继续参与领域计算、冻结持久化和离线观察，不因页面
  精简而删除数据采集或改动评分、动作、排序、SSE、ETag 与冻结哈希。

- 历史日期页在可见时每 3 秒重新读取 P2 实时字段，并在同策略报价 overlay 推送时立即
  重读；行级更新继续校验策略、日期、视图和快照身份。正式当前与临时实时使用独立缓存
  和 ETag，临时草稿明确显示“不替代正式冻结”。

- 活动适配器层目录与包名原子统一为 `src/trader/infra`；组合根、
  entrypoints、层内导入、全部测试与性能 runner 同步使用 `trader.infra`，依赖方向和
  业务行为保持不变。`AGENTS.md` 与软件业务设计的架构边界同步采用新名称。

- 实时最短计划间隔调整为：全市场 3-10 秒、120 候选 1-2 秒、TopK 1 秒、本地评分
  3-10 秒；在途任务继续跳过重叠周期，每个来源 lane 只保留最新观察点，因此实际周期
  自动贴合接口完成速度且不补跑旧周期。Tushare 120 积分仍只承担 SDK `daily` 历史，
  盘中全市场使用东方财富/新浪并行路由，候选及 TopK 使用腾讯定向行情。

- 历史日线优先尝试官方 Tushare SDK/API 的 120 积分 `daily` 未复权数据并完成单位归一；
  供应商明确拒绝该接口时永久降级到腾讯完整前复权日 K，东方财富作为第二回退。历史下载
  使用独立 5-worker 有界池并连续链式预热，不再与全市场、候选和 TopK 实时任务争抢
  worker；证券主数据、交易日历、复权因子、`pro_bar`、日度估值和财务指标仍须 2000
  积分并按配置显式启用。

- 推荐 HTTP 当前路径改为读取线程安全的运行态快照与 overlay；服务启动在接收 HTTP 前预热 today/tomorrow/d25 最近 20 日冻结投影，热历史请求不再逐次打开 SQLite、校验哈希和读取完整 JSON。交付投影保留推荐项、评分、证据、锚点和摘要元数据，但不复制逐股全市场筛选审计与冻结重放输入；冻结文件、哈希、数据库和离线核验对象保持不变。

- 当前策略查询只接受 `trade_date == current_trade_date` 的快照；今天尚无快照时直接返回空 `not_ready`，昨日冻结仍可通过日期选择作为历史查看。历史当日行情索引在特征批次尚未提交时可只读复用行情网关已经成功合并的规范报价，不发起新请求。前端资源版本升至 `dashboard.js?v=9`，移除上一交易日 current fallback 的缓存身份和提示分支。

- 文档治理从单一综合需求文件调整为职责互斥的双文档模型；`AGENTS.md`、README 和交付契约测试同步使用新路径与更新边界。合并保留五来源、双冻结、六阶段 256 MiB 目标、v16 三板九组权重、七项本地风险、DeepSeek 188 次预算、68/32 融合、动作阈值、集中度和 long 观察公式，并把尚未完成的 v17 P1-P6 发布池/Web 热路径/冻结检查点/性能 CLI 明确登记为下一完整工程章节；本批不改变任何运行配置、代码、公式或产品行为。

- 历史推荐查询现按历史股票代码只读访问 P2 已缓存的全市场/候选报价索引，不再要求该股票当天仍位于同策略 TopK；HTTP 路径不刷新行情、不评分、不访问网络，也不修改冻结快照、JSON 或 overlay。历史响应继续使用原字段名，当前价、今日涨跌和锚点至今只由同一上海自然日的实时行情派生。

- v16 today/tomorrow/d25 现按三板策略完整启用并保留 long 当前观察语义；将板块评分辅助计算、推荐最终合并、推荐/回放模型、极端结构风险、市场任务执行和快照 review codec 按职责拆分，所有活动源码重新低于 500 行。`docs/need.md` 第 13 节明确旧 d25 双乘数只用于 v14/v15 回放，活动 v16 以第 26.7 节显式不过热组件为准。

- 运行配置继续固定 5 个普通来源 worker；组合根在同一有界数据池中额外创建 1 个 worker 和 1 个等待槽作为紧急 lane，候选及 TopK 腾讯定向报价走紧急 lane，全市场、历史、分钟和研究任务继续走普通 lane。状态 API 新增紧急 worker、容量、在途、提交、完成与拒绝计数，不改变 3 秒候选报价截止、刷新 cadence、来源 single-flight 或冻结规则。

- D25 周期风险刷新改为复用仍在 10 分钟 TTL 内的成功结构化研究，只提交缺失或已过期代码；失败或截止结果继续使用不超过 60 秒的负缓存后重试，不改变空值降级、风险硬过滤、14:50 冻结或来源超时上限。

- 看板脚本资源版本由 `v=7` 提升到 `v=8`，确保浏览器获取包含快照身份对账的脚本；SSE 正常时仍不周期轮询完整推荐响应，历史日期查询也不参与当前身份对账。

- 运行配置升级到 schema 5：生产组合根只创建一个 `source-data` 执行器，包含 `5 normal worker + 5 pending` 的五来源普通 lane，以及 `1 urgent worker + 1 pending` 的腾讯候选/TopK 紧急 lane。东方财富/新浪并行形成全市场快照，腾讯只做候选定向报价，Tushare 只做主数据、日历、前复权日线、估值和财务，AKShare 继续提供研究数据。缓存 TTL、动作年龄、容量、缓存组字节上限和性能预算分别只由 `market_data.cache_policy` 与 `performance_budgets` 注入；旧冻结继续按 v14 回放，新冻结算法标识为 `engine_v15_parallel_market_data_2026_07`。
- 三板身份按 Tushare 主数据、AKShare 清单、行情市场字段和代码前缀降级确定；前缀降级、身份冲突、上市日期/日龄不可验证和多源价格未复核偏差只允许观察，上市第 0-5 个交易日、重上市首日和退市整理首日直接排除。主板 8.00/8.01、创业板和科创板各自 16.00/16.01 使用独立过滤码，无价格限制状态不再计算普通涨停接近度。

- 运行配置升级到 schema 4 和 158/188 阶段目标，策略配置升级到 schema 8；四类结构化负面风险与配置黑名单统一在评分和 DeepSeek 前硬过滤，原本对应的本地风险触发关闭以避免重复扣分。主审/挑战者请求身份包含模型角色、思考模式、reasoning effort、prompt/schema 版本，V4-Pro schema 修复在内存中回传供应商 `reasoning_content` 且不落盘；桌面明细新增两阶段模型、状态、指纹、cache token 和证据 manifest 审计。
- 将 settings、pipeline、recommendations、DeepSeek reviewer/budget、市场服务/AKShare/特征、快照 codec/writer 和 dashboard CSS 按配置模型与校验、生命周期与任务、请求与状态、缓存与研究、序列化与观测等职责拆为显式模块；原门面、依赖方向、组合根、公共入口和运行资源所有权保持不变。

- DeepSeek 预算与共享快照连接边界改为事务与资源生命周期一体的上下文管理器；仅冻结提交保留显式拥有并关闭的原始连接。状态 API 在 SQLite 打开或读取失败时只降级预算依赖，不改变最近有效推荐、冻结记录、评分、188 次原子预算规则或其他只读状态。
- 落地 P12 证据落盘统一细化：`MarketFeatureService._load_research_cache()` 与 `_write_research_cache()` 对 news/structured 证据统一按 `ResearchObservation` 序列化/反序列化落盘，过期缓存返回 `None` 并退回网络；`AkshareResearchClient._cache_payload()` 使用 `atomic_write_json` 统一落地原始 payload，便于 restart 重放与故障复盘。补充组件测试：`test_akshare_news_response_is_cached_with_atomic_writer`、`test_research_cache_is_used_after_restart_before_source_request`、`test_research_cache_expired_calls_research_client`。
- DeepSeek 审计字段闭环完成：`infra/deepseek/schema.py`、`infra/deepseek/reviewer.py`、`infra/persistence/snapshots.py`、`web/schemas.py` 连续透传审计元数据（模型、思考模式、挑战者状态、置信度与 hash）；旧快照在反序列化时回退为 `primary/not_run/neutral`，新增三类测试覆盖解析、持久化和 API 合约。
- `domain/ranking.py` 的 `select_top_k()` 新增可解释性排序维度：在同 `final_score` 下优先保留审计信号较优（挑战者通过、二级阶段、rating 较好、置信度更高）的候选；为保证可复盘与透明，新增 `tests/unit/domain/test_ranking.py` 覆盖同分 tie-break。
- `docs/issues/2026-07-20.md` 第11节“审计信号参与候选重排序”补齐收益型 shadow 阶段：`metadata.shadow_scoring` 作为只读审阅字段接入快照输出，便于后续按 `rank_gap` 和覆盖率复盘 today/tomorrow/d25 候选替换效应。
- 落地 `docs/issues/2026-07-20.md` 标准化收敛：三源行情适配器统一 `normalize.py` 的解析入口，`normalize_quotes` 与 `MarketQuoteInput` 处理空值/非法值与 `MarketQuote` 构建，降低 parser 分支漂移风险并提升 `quotes` 批量解析可读性。
- 落地 `docs/issues/2026-07-20.md` “标准化/路由/数据库”继续子项：路由器增加 `_is_empty_payload()` 空结果收口，`normalize_quotes()` 收紧 `MarketQuoteInput` 校验约束并隔离非法行情输入，`tests/unit/test_v2_sqlite_migrations.py` 新增 `schema_version` 异常元数据回归。
- 落地 `docs/issues/2026-07-20.md` “标准化/路由/数据库”数据库补齐：`src/trader/infra/deepseek/budget.py` 在初始化阶段加入 `schema_meta` 自恢复逻辑，`SCHEMA_VERSION` 按版本向上修复；补充 `tests/unit/test_v2_sqlite_migrations.py` 与 `tests/component/test_v2_deepseek.py` 对空白/非法/缺失 `schema_version` 的初始化路径回归，避免脏元数据导致启动阻塞。
- 落地 `docs/issues/2026-07-20.md` 第四档：新增 `StandardizedFeatureBuilder` 协议并让 `MarketFeatureService` 按协议注入 `FeatureBuilder`，为后续替换特征构建实现保留横向扩展点，不影响 `FeatureBuilder` 当前口径与现网行为。
- 落地 `docs/issues/2026-07-20.md` 性能向量化方向（第三档 #11）：`infra/market_data/history.py` 新增 `HistoryProfile` 与 `summarize_history_metrics()`，在 `FeatureBuilder._raw_features()` 中把 `MA5/20/60`、20 日波动率、20 日最大回撤、20 日成交额中位数、20 日上涨一致性改为一次汇总取值，减少同一历史序列重复计算；`return_pct` 与返回字段值不变，结果可复用性增强，适配后续批次“按批构建 Raw 特征向量”。
- 落地 `docs/issues/2026-07-20.md` 本轮市场路由优化：`infra/market_data/gateway.py` 的 `_fetch_market_once()` 改为一次性将 `eastmoney/sina` 路由表提交给 `MarketDataRouter.route()`，替换逐条 `route((vendor,))` 的循环包装。行为保持 `required` 顺序回退与失败计数、熔断、缓存回退不变，但异常信息从“汇总逐 vendor”转为路由器聚合异常，便于后续可观测性归一化。
- 落地 `docs/issues/2026-07-20.md` 市场数据异常语义：`infra/market_data/router.py` 现在聚合 required 路由失败与无数据，`required` 全部耗尽但存在空返回时抛 `MarketDataNoData`，全失败则抛带 vendor 摘要的 `MarketDataFailed`；`tests/component/test_v2_market_data.py` 新增路由级 no-data/failure 回归，并修订网关路径对无数据与失败上下文的可观测性。
- 落地 `docs/issues/2026-07-20.md` 路由可观测性细化：`router.py` 的路由结果新增 `status/degraded/fallback_reason` 与 vendor 明细（name/status/severity/error/duration），`gateway.py` 在 `health()` 增加 `route` 子字段透传最近一次路由快照；新增/更新 `tests/unit/test_v2_market_data_router.py` 与 `tests/component/test_v2_market_data.py`，验证降级链路与可观测输出。
- 路由可观测性继续细化（2026-07-20）：`/api/status` 的市场数据路由健康进一步透传 `attempted_count/success_count/failure_count/no_data_count/skipped_count` 与 `used_vendor`，并通过前端状态摘要新增“路由健康”卡片，展示 degrade/fallback 与 vendor 级 `status/error`，用于故障归因与熔断排查。
- 落地本批续写可观测性回归：`tests/unit/test_v2_market_data_router.py` 补齐 optional/required 混合、no-data 聚合、错误聚合顺序等边界；`tests/unit/test_v2_sqlite_migrations.py` 补齐 schema 初始化幂等性与版本迁移回归。
- 落地标准化特征注册表契约回归：`infra/settings.py` 在加载策略配置时校验 `factor_contract.feature_schema_version` 与 `FEATURE_SCHEMA_VERSION` 一致，支持 `feature_names` 与 `feature_schema_expected` 与 `FEATURE_SCHEMA` 全量一致性；`tests/unit/test_v2_settings.py` 新增版本错配拒绝与显式注册表一致性回归。
- 落地 P14 评级映射：`domain/risk.py` 的 `Rating/parse_rating` 已在 `infra/deepseek/schema.py` 中应用到 `results[*].rating`，`domain/models.py` 与 `domain/ranking.py` 记录并消费评级，`application/recommendations.py` 将 `APPLIED` 审核中的 `deepseek_bearish/neutral` 映射为显式动作降级；新增 `tests/unit/domain/test_risk.py`、`tests/unit/domain/test_ranking.py`、`tests/component/test_v2_deepseek.py` 覆盖解析与动作映射。
- 落地 `docs/issues/2026-07-20.md` 第一批 P1~P3：新增 `infra/deepseek/factory.py` 的 `create_deepseek_client()`，并把 `infra/container.py` 的 `DeepSeekReviewer` 依赖由直接实例化 `DeepSeekHttpClient` 改为通过工厂注入 `DeepSeekClientBase`，为未来 provider 切换（mock/vLLM）保留入口；同时补充 `tests/unit/test_v2_deepseek_base.py` 工厂测试（默认 provider、兼容 alias 与未知 provider 异常）。
- 下一版契约将 today、tomorrow、d25 的横截面改为主板、创业板、科创板独立总体，拆分两类 20% 板过滤身份，并规定 v10 删除 d25 过热与市场状态双乘数；当前 `baseline_v9_active` 的候选、评分、动作阈值、行业限制和双乘数继续生效，68/32 融合、DeepSeek 158/188、11:20/14:50 冻结及 long 固定观察边界不变。本批只更新文档，不修改配置、活动代码、数据库、API 或 UI。
- DeepSeek 正常交易日目标由 144 调整为 158，阶段目标重新分配为 shared 15、today 68、tomorrow 35、d25 30、long 10；其中主审及预热共 141 次，today/tomorrow/d25 挑战者目标和上限统一为 6/6/5 共 17 次。六个预算桶、emergency 使用条件、冻结截止和 188 次物理 HTTP 请求硬上限均不变，候选不足时仍禁止为凑目标空调用。本批只更新需求契约，不修改配置、活动代码、数据库、API 实现或 Web 行为。
- 开源参考表改为截至 2026-07-19 只展示当前不少于 20,000 Star 且与量化、金融研究、A 股数据或 DeepSeek 接入直接相关的项目；新增项目分别标明可借鉴边界、非运行依赖及非 DeepSeek 项目身份。
- DeepSeek 正常日目标由 133 调整为 144，新增 11 次目标全部在既有策略桶中用于 today/tomorrow/d25 挑战者，策略桶上限、emergency 规则和全局 188 次物理请求硬上限不变；today 挑战者 11:18 停止提交，其余请求仍遵守 14:48 截止和 11:20/14:50 冻结。本批只更新需求契约，不修改配置、活动代码、数据库、API 实现或 Web 行为。
- 本批次只更新开源参考契约和链接，不新增 Python 依赖、不复制外部源码，也不修改本地/DeepSeek 评分、风险、预算、冻结、API 或 Web 行为；DeepSeek 荐股项目仅作为 provider 封装、多智能体分工、证据展示和历史验证机制参考。
- 当前推荐 ETag 绑定当前交易日、快照、overlay 与 fallback；历史响应必须精确匹配请求日期，前端缓存同时校验策略和日期，只接受明确标记的上一交易日 stale fallback。桌面顶部补齐行情来源/时间/年龄、评分时间、DeepSeek 已用/剩余和冻结状态，历史表与明细抽屉补齐今日涨跌、锚点至今、权重、截尾口径和风险评估。SQLite schema 升级到 v3，为来源健康审计补充有界错误摘要。
- DeepSeek 五维原始结果按策略配置中的权重、至少两个已知维度和 0.50 加权置信覆盖在本地分类；全部维度未知或覆盖不足逐股 `abstain` 并回退本地分。每批物理 HTTP 仍最多 8 股、最多两次尝试，429 遵循 `Retry-After`，非法 schema 的修复与网络重试共享两次硬上限，14:48 后不再预留请求。
- 用户诉求：将含义宽泛的快照“生命周期”命名改为职责导向名称。现状判断：该模块实际负责编排评分、冻结和实时 overlay 流程，不拥有应用或资源生命周期；现重命名为 `snapshot_workflow.py`，同步生产导入和 `docs/need.md` 结构契约，不改变评分、冻结、持久化、API 或线程行为。
- 生产行情分页、历史、分钟和研究任务改为复用组合根唯一的 6-worker 数据池；策略评分拆为 worker 内的不可变本地准备、DeepSeek worker 复核和单合并线程融合/TopK，long 使用独立低优先级 worker，并在三策略复核完成后优先复用策略无关缓存。应用层按事件生命周期、worker 阶段和快照/冻结生命周期拆分，SQLite/JSON 发布、冻结、overlay 和事件状态统一经单写线程串行提交；Web 查询端只依赖拆分后的只读事件端口。
- 运行配置升级为 schema v3，以逐任务、逐阶段 `pipeline.cadence_seconds` 驱动全市场、候选、TopK、评分、行业、新闻、风险和参考数据事件；周期错过或同类任务仍在运行时从当前时刻重新计时，不突发补跑。冻结、DeepSeek 截止和收盘单点可在延迟或重启后幂等补提交，14:49:50 最终候选只允许在冻结前补交。
- 策略配置 schema 升级到 v7，d25 的 15/30 过热边界、0.85/0.75 系数、市场宽度 60/40 分类与 1.03/1/0.92 状态乘数，以及 long 的年化、估值、成长、质量、公告关键词、质押和解禁公式全部进入规范化策略哈希；冻结回放算法升级到 `engine_v9_section13_2026_07`，d25/long 输入版本绑定完整因子值与结构化证据版本。
- 策略配置 schema 升级到 v6，新增固定 30/30/25/50 尾盘信号参数及原始/派生因子登记，策略哈希覆盖完整契约且启动时拒绝登记与执行窗口、公式、样本、缺失或范围互相矛盾；冻结回放算法升级到 `engine_v8_section12_2026_07`，tomorrow 输入版本同时绑定候选报价和分钟证据，Evidence/API 增加可选接收时间与数据版本；DeepSeek 证据子集校验与实际进入 prompt 的 16 条上限保持一致。
- 本批次仅新增策略文档审计归纳，不修改 `docs/need.md`、配置、活动代码、测试、公式、阈值或运行行为；当前 `need.md` 继续作为唯一业务契约，v1 的模拟交易、结果回填、产品内验证、自动调参和预测能力没有被重新引入。
- 策略配置 schema 升级到 v5，`today_news_signal` 的 72h/1h 窗口、75/50/25 分值和有界正负词表进入规范化策略哈希；`news_sentiment`、`evidence_freshness` 因子登记升级到 v2，冻结回放算法升级到 `engine_v7_section11_2026_07`。
- 策略配置 schema 升级到 v4，冻结回放算法升级到 `engine_v6_section10_2026_07`；本地风险统一由通用规则解释器触发，缺失和非有限输入不再隐式转为零，API 同时返回 25/30 分封顶前风险明细合计。
- 冻结回放算法升级到 `engine_v5_section9_2026_07`；硬过滤股票数改为按审计明细中的唯一股票计数，Top120 池截断不再误算为过滤，long 不再继承普通候选池的过滤记录。
- 策略版本改为由除声明标签外的完整规范化策略配置生成 `strategy_sha256_*`；运行配置版本与该哈希组合后写入快照，任一因子、权重、风险规则、阈值或融合配置变化都会形成新版本身份。
- 运行配置版本升级到 `runtime_v3_freeze_overlay_2026_07_17`，SQLite schema 升级到 v2；冻结 manifest 新增快照 schema 和逐股锚点来源/年龄，冻结 JSON 新增配置版本，运行库补齐 `deepseek_calls` 与 `live_overlays` 表。
- 策略版本升级到 v9、冻结回放算法升级到 v4；today、tomorrow 和 d25 只在各自执行阶段应用动作门槛，预注册数据禁止跨版本混算。
- 用户问题：此前交付规范只要求更新变更日志，没有明确把用户反馈与修改逐项归纳；修改说明：每个批次现在必须在 `Unreleased` 记录问题/诉求、原因判断、行为变化、验证证据和剩余风险，契约变化仍同步 `docs/need.md`，敏感信息禁止入文档。
- 今早、明日和 2-5日推荐在桌面页面启动后后台预取，推荐日期与快照请求并行执行；相同策略/日期请求合并并使用 ETag 后台刷新。
- 策略版本升级到 v8；新闻只对候选和长期观察池抓取，全市场扫描不发起逐股新闻请求。
- DeepSeek 风险事实不再直接控制生产 veto；策略 v7 和冻结回放算法 v3 由本地风险表按风险代码、允许证据类型、证据有效期和最低置信度确定扣分与重大安全 veto。
- 最终验收矩阵明确区分可重复仓库门禁与真实交易日、真实 DeepSeek 密钥、三档桌面截图等外部发布证据；旧 v2 快照继续可读，但不能充当新增冻结复算门禁证据。
- 迁移清单新增 `docs/need.md` 第 24 节逐阶段完成证据；运行手册固定生产影子留证字段和回退 tag `v1-rollback-20260717`，仓库门禁完成与真实交易日发布观察明确分离。
- “继续”命令的交付粒度从下一个最小独立任务调整为下一个完整未完成章节；章节内全部明确子项统一实现、Review、提交和推送，同时禁止顺带合并相邻章节。
- 项目入口统一为 `trader-server` 和 `trader-cli`；Linux/macOS/WSL、PowerShell 和 CMD 启动脚本只调用安装后的入口。
- 依赖、构建、包发现、console scripts、Ruff、mypy 和 coverage 统一由 `pyproject.toml` 管理。
- 运行配置迁移到 `config/v2`，运行数据隔离到 `.runtime/v2`，配置路径必须显式且为绝对路径。
- 最终分固定为 `clamp(local_score * 0.68 + deepseek_score * 0.32 - deepseek_risk_penalty, 0, 100)`，并以 `ROUND_HALF_UP` 保留两位。
- Web 产品范围固定为个人 PC 桌面浏览器；发布验收分辨率为 1280x720、1440x900 和 1920x1080，手机和平板不在范围内。
- v1 需求、设计、研究登记和配置移入 `docs/archive/v1`，`docs/need.md` 成为唯一活动业务契约。
- `docs/hi.md` 的后续交付由两个功能批次扩展为三个独立批次：v15/v16分别建立缓存
  正确性，v17只在固定业务投影和冻结哈希不变的前提下测量并优化。缓存策略只从
  `runtime.json.market_data.cache_policy` 注入，性能预算只从
  `runtime.json.performance_budgets` 读取，禁止适配器、评分lane或性能脚本自带默认值。

### Fixed

- 修复长快照状态和长错误文本换行时撑高整个 Header、把股票列表向下推移的问题；同时
  修复运行服务热加载新 CSS、但仍缓存旧模板时，“正式推荐”旧标题失去样式后反而按默认
  `h2` 放大的现场表现。最终模板、CSS 与 JavaScript 资源版本同步提升，实际本地服务重启
  后已确认只返回同一版布局。

- Web 荐股展示批次：修复短线策略切换会无条件清空显式历史日期的问题；目标策略没有
  同日归档或推荐接口返回 `snapshot_not_found` 时，现在显示该策略、该日期的正常空态，
  不再用当前日期数据替代。策略、日期列表和荐股请求均绑定选择序号，迟到响应不能覆盖
  新选择。

- 修复 P6 初始化只取 today/tomorrow/d25 日期交集，导致旧库中按策略有效但同日不齐全的
  5 份正式历史全部不可见；同时修复收盘协调器只验证三板行情存在、未验证历史横截面，
  从而在 qfq 预热完成前把 `board_population_insufficient`、可靠度不足和 0 只推荐固化为
  当日 `close_fallback` 的问题。当前机器已用只读 `migrate-v17` 将旧 v2 的 5 份 committed
  快照导入 v17，源目录前后摘要一致。

- 修复用户必须理解内部“可变草稿/不可变冻结”发布状态并手动切换视图的问题。两种后端
  状态继续用于盘中实时性、冻结不可覆盖和失败降级，但页面自动选择同日最新有效状态，
  草稿明确提示“未冻结，结果可能变化”，不会冒充正式推荐或回退到上一交易日。

- 修复历史预热把 `unsupported` 当作第四板平均分配，导致三个活动板块理论上每板最多
  90 只、永远低于 100 样本门槛的问题；同时修复尚未开始就被取消的分钟请求被写入负
  缓存、后续评分无法继续推进的问题。无合格候选现在报告
  `deepseek_skipped_no_eligible_candidates`，不再误报为 DeepSeek 请求不完整；永久不
  复核的 long 不再附加任一 DeepSeek 降级原因。

- 修复正式性能报告用 Polars self-join、排序和 JSON 序列化代替生产链路，导致旧门禁无法
  暴露实时瓶颈的问题。运行态现在对事件和行情周期记录有界完成/失败/超时/被替代/丢弃
  结果；同一 trace 的重复终态不会重复计数，服务端状态不泄漏关联身份。

- 修正文档治理仍把已经闭合的 `plan.md`、`plan_c.md`、`plan_sudu.md` 和
  `plan_pipeline.md` 当作活动计划的问题，并补齐此前仅在实现/测试中存在的 V4 本地映射和
  P1-P3 列式增量约束。
  同时明确排除旧计划中与生产配置冲突的“至少 3 维/覆盖 0.60”“today 全部 11:18
  截止”和“long 硬桶 0”等拟议值，避免删除计划时把过期设想误升格为生产规则。

- 第 2.6 节此前仍为 pending，严格质量脚本还接受 `C901/N818/PLR0911/PLR0912/PLR0913/
  PLR0915` 共 137 项既有债务；原因是组合、缓存回退、并发调度、DeepSeek 和 Web 请求职责
  长期堆叠在大函数与宽参数边界。现已拆分恢复、调度、缓存、降级和序列化阶段，使用真实
  `Error` 命名与类型化请求边界，并把上述严格诊断归零；冻结、188 次物理预算、固定融合
  和最近有效快照降级语义由回归保持。

- 文档计划修正“当前毫秒级 P6/API/SSE 是实时链路首要瓶颈”的判断偏差，明确优先处理
  定向报价重复重建 5500 行、DeepSeek 同步等待占用 merge/event 线程、SDK Session/分页
  生命周期、浏览器整表重绘和合成性能门禁失真；生产缺陷仍待各独立实施批次验证和修复。

- 文档计划明确修正后续实施中容易出现的四类设计缺口：结构化风险硬过滤使软扣分失效、
  候选分与最终分不一致造成不可观测误杀、回踩和突破分支错误共用全部输入，以及缺失值在
  评分、可靠度和动作门中的语义不一致。本批只记录修复方案，尚未改变生产判断。

- G5 交付契约修复“最终共同门禁可以在缺少任一 B/C/D 签字、A4/A5 状态或 Git 一致性证据时
  被文字宣告发布”的流程缺口；报告必须同时包含四项完成条件、单批提交语义及
  `HEAD == @{upstream}`。本批未发现需要修改的生产缺陷。

- A5-F01 修复阶段 4 P6 新方法通过抬高严格 Ruff 债务基线接纳复杂度的问题；重构后两项
  新增复杂度诊断消失，P6/Web/pipeline 回归保持一致。C5 同批修复 explicit V4 回退 legacy、
  未确认或软来源正向加分、证据时区去重、prompt/cache 身份缺口、重复 coverage 阈值和
  emergency 资格跨物理批次泄漏；所有模型输出仍由本地规则决定应用与风险。

- D5-F01 修复主动 `resync_required` 缺少 patch schema、同一事件类型载荷不一致的问题；
  D5-F02 修复订阅打开时为 `cursor_ahead`、但流式产出前新事件追平后被误报成
  `cursor_expired` 的竞态。两项均先增加精确失败回归，再修改 publisher/SSE 实现。

- B5-F01 修复局部 P3 重算范围可能小于全量重算：股票删除或跨板/跨行业时，旧板块和旧行业
  现在也会标脏；插入/删除代码进入风险 dirty 集合并触发全部相关字段族。新增同时覆盖插入、
  删除、板块迁移和行业迁移的回归，防止旧横截面继续复用。

- A4-F01（Polars 构造失败会阻断有效标量行情）已修复：行情/候选提交在列式批次构造抛出
  `PolarsError`、`RuntimeError`、`TypeError` 或 `ValueError` 时保留 scalar snapshot，生成完整
  invalidation change set，并记录 `columnar_projection_failed`；列式合并自身
  失败也记录 `columnar_merge_failed`。新增精确注入回归覆盖返回值、health epoch、
  dirty count 和降级原因。

- A4-F04（P6 拒绝后 RuntimeState/session/checkpoint/SSE 仍可能前进）已修复：公共 pipeline
  统一使用 P6 写端口的显式接纳布尔值并调用 `admit_snapshot_to_p6()`；超限、迟到、冻结替换或
  旧日期拒绝时保留最近有效 P6 和运行态，记录有界计数与策略降级。同步、worker、冻结、收盘
  恢复和重启恢复均使用同一接缝。

- C4 修复挑战者尝试误占普通主审软桶、emergency 以不可达策略硬桶判断“普通额度耗尽”的问题：
  普通软用量只计主审，挑战者保持独立 8 次软上限，emergency 在对应主审软桶耗尽后才可用；
  正常/含 Pro/含 emergency 上界保持 58/66/71，全局原子硬上限仍为 188。

- 修复 overlay projection、patch schema/身份/base/TopK 错配被前端静默丢弃的问题：现在按
  原因执行 ETag resync；有效在线 patch 保持零完整 GET。P6 current pin 拒绝冻结后的同日
  迟到草稿和冻结身份替换，且不会被较旧投影覆盖；publisher 同步禁止这些非权威投影发出
  SSE。P6 当前视图超限改为显式失败，阻止调用链继续广播错误 SSE。新页面也不再在完整
  GET 最新投影后重放旧 SSE 导致短暂回滚。

- D4 最终独立 Review 进一步封闭同身份内容漂移：即使 `snapshot_id` 未变，只要同日冻结
  投影内容不同，P6 与 publisher 也会拒绝替换并保持原冻结版本及 SSE sequence 不变。

- 修复架构契约把仅残留忽略 `__pycache__`、没有任何 `*.py` 的退休目录误判为活动业务包：
  目录拓扑与退休路径检查现在只认真实 Python 源文件，仍零容忍旧业务实现，且不会因本地
  解释器缓存产生伪失败。

- 修复阶段 2 合并后公共契约和权威文档之间的漂移：DeepSeek prompt 证据仍写 16 条、long
  仍有预算、SSE 只描述游标恢复而未固定 patch v2 projection/base/overlay 身份。现在文档、
  生产代码和契约测试使用同一组版本、预算、缓存和增量更新语义。

- 修复最近历史按单策略预热可能形成不完整交易日、冷缓存逐项淘汰可能留下部分三元组、慢
  SSE 客户端与正常客户端共享失效式全量回读，以及冻结边界重启只能依赖旧 published 指针
  的问题。现在只接纳 manifest/SHA 合格的完整三策略驻留日期，冷区整日装载和淘汰，慢客户
  端独立丢弃并返回 resync，边界恢复只读取同日、30 秒内、配置一致且未消费的检查点。

- 修复 Tushare 原始价历史此前缺少复权元数据、可能进入收益率、均线和波动率特征的问题；
  历史存储与特征入口现在双重拒绝 raw，未知数据也不能再通过默认值伪装为 qfq，并在 raw
  审计数据出现时回退到腾讯 qfq 历史。修复带来源前缀的 `late` 失败可能被误归类为普通源
  失败的问题，失败元数据保持有界且不含外部载荷。

- 修复万能行情端口、隐式依赖、裸字符串事件状态和共享可变边界造成的耦合风险；异常、恢复
  摘要和行情快照元数据均改为真实类型，停止顺序和冻结 compare-and-set 保持显式。

- 修正文档中 long 仍配置 DeepSeek 维度和正常预算、软目标只有数字而没有消费规则的计划
  缺陷。新计划明确 long 的主审、Pro、预热和 emergency 均为 0，并规定未使用软额度不
  跨策略转移、缓存命中不计数、失败/超时/重试/schema 修复均计入物理请求。

- 修复单个领域 `models.py` 同时承载行情、复核和推荐对象，并通过模块级 `__getattr__`
  延迟导出推荐类型所形成的循环耦合和隐藏 API；修复过滤、评分、排名和研究规则被长嵌套分支
  或 6-10 个参数接口包裹、难以独立验证的问题。所有调用方已原子迁移到新内部路径，不提供
  兼容别名；构造请求时冻结映射和序列，避免冻结 dataclass 间接持有可变输入。

- 修复 Python 3.11 在 pytest 收集阶段拒绝 `mappingproxy` dataclass 直接默认值、导致全部
  业务测试无法运行的问题；根因是三个不可变映射仍被 dataclasses 视为不允许共享的默认
  对象。新增实例隔离与运行时不可变回归，同时清理本机退休 `infrastructure` 字节码目录和
  `docs/.mypy_cache` 对架构/文档拓扑契约的忽略缓存污染。

- 根因确认：v16 主要按上行强度、稳定性和软风险扣分排序，没有融合后不可被高分覆盖的
  MAE/ATR 保护，也没有把推荐后的真实最大不利波动沉淀为可验证结果，因此热度扣分不能可靠
  阻止大幅回撤。本批增加独立动作保护和结果结算闭环；Review 同时修复连续性校验误把
  11:20/14:50 锚点价当作推荐日收盘价的问题，现使用推荐日真实日线收盘校验下一交易日，
  避免把正常午后波动误报为公司行为或价格断层。

- 解决用户指定新增 `docs/plan.md` 与原“docs 只能有两份文件”拓扑门禁的直接冲突；没有
  把性能测量、复权风险或收益假设写成已实现行为，也没有修改评分、过滤、冻结或 Web 运行
  逻辑。

- 修复 15:00 后数据库没有同日记录时正式接口永久 `not_ready` 的断链。原实现只恢复 P2
  报价和已有快照 overlay，没有从 P6 固化或从收盘行情继续候选筛选、评分、TopK、冻结、
  P6/SSE 发布；现在两条恢复路径均闭合到 SQLite/JSON 和 Web，行情或三板不完整时不写
  单策略半成品并继续后台重试。

- 股票详情不再为“无风险、无缺失、无证据、未复核”等正常空状态生成独立区块，也不再
  直接展示机器动作原因；已登记动作原因和风险代码改为可读中文，空核心行情隐藏对应指标
  并统一提示“部分核心行情暂缺”。

- 根因确认：冻结窗口或收盘后启动时 cadence 不再运行全市场任务，P2 当前报价索引保持
  空；历史序列化因此按契约把当前价、今日涨跌和锚点至今返回 `null`。同时查询层在冻结
  时点后只接受同日正式冻结，虽运行态已有 tomorrow/d25 草稿，页面仍只能看到
  `not_ready`。现在冷启动后台单次恢复当日报价索引，显式临时视图读取同日 P6 草稿；
  默认正式接口、历史冻结身份及 11:20/14:50 不可变规则均未放宽。

- 修复适配器层目录名及 Python 导入路径冗长且与团队期望不一致的问题；安装后的公开
  CLI 仍为 `trader-cli`/`trader-server`，只改变内部包路径，不改变 API、配置、冻结、
  DeepSeek 预算、评分公式或 Web 行为。

- 修复今早/明天/2-5 天在全市场内存已有 5,000+ 行实时行情时仍没有推荐的主断链：
  周期性 P2 以前命中 stale-while-revalidate 上一轮缓存，P3 按 20/30 秒边界把整批淘汰，
  再用空结果清除候选池，导致候选报价与评分持续 `skipped_cold`。现在 P3 前等待本轮物理
  刷新，事件按“全市场→候选报价→评分”优先级执行；只有整批因行情陈旧或历史预热不可用
  时保留最近有效候选身份和显式过滤降级，真实合格数据仍可产生合法空池。

- 现场继续定位出四个后续断点：Tushare REST 曾错误请求 `/daily` 子路径、当前 Token 实际
  返回 `permission_denied`、约 360 只历史预热占满实时来源池，以及排队事件用创建时间把
  到达后的新报价误判为未来数据。现改为官方根地址协议并精确分类权限错误，腾讯前复权
  日 K 主回退保留成交量/成交额/换手率，历史使用独立池连续预热；非冻结周期按实际执行
  时间判断行情，冻结仍严格使用检查点时间。修复后现场 P3 恢复约 101/97 个候选，P6
  发布 tomorrow 5 条、d25 1 条和 long 4 条；today 因服务在 11:20 后启动保持 `not_ready`。

- 长时间运行继续复现“配置 1 秒但数分钟不更新”：单事件线程中候选报价还同步等待整批
  尾盘分钟线，且新全市场事件持续越过旧候选/TopK/评分，现场累计 139 个过期事件并错过
  14:50 合格检查点。候选报价现不再同步抓分钟线；TopK 使用独立高优先级，
  全市场/候选/评分改为同级 FIFO。候选和评分的事件排队窗口包含上游最坏耗时，开始执行
  后仍分别截断为 3/15 秒，冻结事件继续最高优先级且迟到数据不得补写。

- 修复当前日期三个策略页面无推荐的上游断链：此前全市场事件在 20 秒内同步等待约 360 只历史而过期；拆分后 P2 实时快照先原子提交、历史独立均衡预热。随后发现数据源最近成交时间比本轮接收时间旧 2-3 分钟，P2 发现阶段误把 5,000+ 行全部判 stale；现在发现阶段按本轮接收时间判断是否赶上周期，候选复核和评分仍保留原始来源时间及 20/30 秒可执行限制。Tushare/东方财富历史失败时优先异步注入本地最近有效种子，不再等待三次熔断，也不会停在内存池而漏掉后续硬过滤、九组预选、三板评分、TopK 与 P6 发布。

- 修复“已经读取内存但接口仍明显延迟”的实际错配：此前仅历史股票的当前报价来自内存，推荐快照仍在每个 HTTP 请求中读取并校验冻结 JSON，且 10 行表格响应携带最多 1398 条无关筛选明细，现场 TTFB 为 0.94-1.80 秒；现在快照、overlay 和报价均走内存热路径，历史交付响应只保留页面所需对象。同步确认“今日涨幅”取当日实时报价 `pct_change`，“锚点至今”由同一实时报价与冻结锚点计算，不再读取锚点日涨幅代替实时值。

- 修复当前交易日没有发布快照时仍显示上一交易日冻结结果和“仅供观察”提示的问题；同时修复首轮历史行情读取只覆盖已提交特征/候选缓存，在现场全市场事件过期但规范行情已成功合并时仍导致“锚点至今”为空的问题。

- 修复历史响应在当日行情缺失时把冻结 `pct_change` 回填到“今日涨跌”的字段混用：锚点价格和锚点涨跌继续保持冻结值；当日行情存在时返回真实今日涨跌并计算锚点至今，不存在或不是当前上海日期时返回 `null`，页面显示 `-`，不再伪造锚点值。根因是查询层只扫描当前同策略推荐快照且序列化层对缺失实时行情回退到冻结报价。

- 修复 v16 半成品导致同步评分仍向 `prepare_snapshot()` 传入已删除参数、异步 future 类型串线、旧冻结被误标 v16 回放、序列化后 tuple/list 元数据哈希不一致、DeepSeek 缓存被纯报价版本抖动无条件击穿、Web/持久化可空数值类型错误、结构化减持字段仍沿用旧合并名，以及目标股票进入领先组后把有效 3 只领导样本误降为 2 只的问题。任一板块失败继续保留最近完整三板快照，不发布偏置 TopK。

- 修复 TopK 定向报价虽由腾讯在亚秒级成功返回，却因共享数据池前方排有大量历史或研究任务而在开始执行前耗尽 3 秒批次截止的问题；紧急报价不再被普通 FIFO 队列饥饿，真实网络超过截止时仍显式保留原降级错误和最近有效 overlay。

- 修复 D25 结构化研究在固定候选顺序和短批次截止下长期停留于低覆盖的问题：成功代码不再每轮被强制重抓并占满 worker，后续候选可在连续刷新中逐步获得财务、公告、质押和解禁证据；v15 截止路径统一抛出可分类的 `MarketDataDeadlineExceeded` 并禁止迟到结果写入内存或磁盘缓存，不再存在旧实现负缓存 TTL 未初始化而抛出 `UnboundLocalError` 的边界。

- 修复页面已缓存上一交易日 fallback 后，若推荐发布事件未触发当前页重读，状态心跳虽已看到服务端新快照但表格仍继续显示昨日数据的问题；现在最迟在下一次 15 秒状态心跳发现身份变化并切回当日快照，手工历史选择、冻结规则、ETag、评分和行情采集链路保持不变。

- 移除执行计划中误写的 Tushare 明文凭据；活动配置继续只声明 Token 环境变量名和受权限保护的可选文件路径，运行日志、快照、缓存、fixture 与文档均不保存凭据值。

- Review 修复多源路径中的确定性与资源生命周期问题：过刷新期行情现在先返回旧值再由原来源 lane 刷新；跨供应商同时点不再用无共同语义的版本字符串改变腾讯/东方财富/新浪优先级；0.50% 定向复核统一用较低价格作分母；缓存优先淘汰业务时间已降级条目；Tushare 版本元数据在服务锁内复制；五个适配器支持协作式停止，Tushare 分代码批次在当前 SDK 调用返回后停止后续调用。另修复 intraday lane 超时返回后仍可修改调用方限制字典的竞态，并把 5500 只证券逐只扫描完整交易日历的上市日龄计算改为一次排序后二分计数。

- 修复最近需求把负面公告、减持/解禁、质押和财务恶化改为硬过滤后，活动实现仍只在 d25/long 本地风险表扣分且 today/tomorrow 不读取结构化风险的问题；风险字段缺失现在保留本地推荐并记录 `structured_risk_unavailable`，真实正等级在四策略评分和模型调用前剔除。修复行情源未显式标记时一字涨跌停无法识别，以及策略配置黑名单未进入硬过滤的问题。
- 修复生产仍默认旧 DeepSeek 别名、无挑战者执行、动态候选数据位于 prompt 固定前缀之前、同一批候选因上游顺序变化导致 prompt 尾部不稳定、挑战者结果未进入策略融合缓存，以及 V4-Pro schema 修复丢失上一轮临时推理字段的问题；挑战者失败、超时、预算耗尽、schema 错误和迟到继续保留有效主审，重试与修复仍共享最多两次物理尝试并计入原子 188 上限。

- 修复 DeepSeek 预算查询与共享快照读写持续泄漏 SQLite 连接并最终耗尽进程文件描述符的问题；补充两个连接边界的正常/异常关闭回归，以及预算库不可用时 `/api/status` 仍返回 200 的故障注入回归。
- 修复合法 DeepSeek 五维响应缺少 `rating` 时默认 `neutral` 导致所有过阈值候选被降为观察、最终无可执行荐股的问题；恢复最终分/本地分/代码固定排序，并保留评级为只读审计。
- 恢复跨交易日最近有效冻结的显式 stale fallback，不再冒充当日快照，同时保留原推荐日期和降级原因。
- 所有非冻结周期任务越过 deadline 时统一进入 `expired`，不污染全局最近错误；全市场特征与历史缓存均在提交前复核 deadline。
- 原始研究 payload 与标准化 observation 缓存改用独立目录并经同一 persistence executor 串行写入；恢复 `bootstrap.py` 唯一组合根。

- 用户问题：运行中“最近错误”偶发显示 `event deadline expired during execution: full_market`。运行库证据显示该事件前后全市场任务连续成功，来源健康无失败；根因是低优先级事件的排队时间计入固定 20 秒 deadline，完成越线后又被通用异常路径误记为系统失败，同时行情服务存在先提交缓存、后检查截止的不一致窗口。修改后，非冻结 deadline 耗尽进入可审计的 `expired` 终态并计入 `events_expired`，不污染全局最近错误；`full_market` 在特征缓存和候选池提交前执行截止校验，历史预载也受同一 deadline 约束并取消未开始任务。真实行情源失败仍保持 `failed`/降级错误语义，11:20/14:50 冻结边界不变。新增组件与集成回归分别覆盖截止后不提交行情特征，以及事件 `expired`、候选不变、`events_failed` 不增加和最近错误不被写入。
- 用户问题：启动后出现 `today freeze unavailable: no current pre-cutoff snapshot`；原因是冷启动回补会把 `today` 与 `tomorrow/d25` 一起补提交，即使当日今日稿件不存在或已超过今日截止窗口。
  修改说明：`initialize()` 现在在进行启动回补前，先校验对应策略当日预截止稿件是否存在且未越过 11:20/14:50 截止窗口；若不满足则跳过该策略回补，避免把“上游缺稿/过期稿件”误报为全局异常。
  结果：无论何时启动，`run.sh` 冷启动再无该条错误污染 `last_error`，tomorrow/d25 仍保留原有回补与冻结逻辑；验证点为 `tests/integration/test_v2_pipeline.py` 中的新增回归。
- 修复跨交易日复用旧冻结 ETag、历史页面沿用锚点涨跌冒充今日行情，以及刷新/关闭字符图标口径不一致的问题；修复评分耗时若写入快照元数据会导致同步/异步与跨运行冻结哈希不确定的问题，耗时现仅进入线程安全运行状态。DeepSeek 调用终态和数据源健康经单写路径持久化，重启后仍可查看 429、超时、延迟、来源年龄及最近冻结哈希，审计不保存密钥、prompt 或完整外部载荷。
- 修复 DeepSeek 缓存因报价 `data_version` 每次变化而失效、跨策略重复请求以及迟到结果可能进入缓存/融合的问题；缓存身份现在绑定结构化特征、证据、风险事实、模型/prompt/schema 和阶段，价格相对变化达到 1% 或量比差达到 0.3 时用十进制精确比较失效。Review 同时修复非重试 4xx 被重复请求、schema 部分返回拖累有效股票、截止后重试预留及崩溃遗留 `running` 状态。
- 修复事件审计 UPSERT 可无条件覆盖状态且同一幂等键缺少有效执行者门的问题：风险/冻结先以 `pending` 原子预留，执行和终态使用 compare-and-set，崩溃遗留高优先级事件可重放，旧配置事件被失败关闭；普通行情满队列时按主体保留最新版本，容量、深度、合并、拒绝和重放均进入状态。Review 同时修复 DeepSeek 异常阻断本地快照、单策略数据失败阻断其他策略、事件嵌套载荷可跨线程改写或写入非有限 JSON 数值、全部数据 worker 同时借用自身队列会自等待、调度启动中断或关闭超时残留，以及停止时冻结写入未完成便返回的风险。
- 修复旧版或迟到候选报价可能覆盖更新全市场行情、分钟刷新失败清空有效尾盘信号、草稿 overlay 已持久化但只读查询忽略、并发全市场重复物理请求及来源故障持续冲击上游的问题；全市场使用 single-flight，连续失败熔断并半开单探针，候选/分钟应用版本门和最近有效缓存，TopK 失败保留现有 overlay。状态接口使用运行时已记录阶段，不在 HTTP 读取中刷新交易日历，并报告全市场/候选/TopK 年龄与 2 倍/3 倍时效分级、SSE 和 today 报价到评分发布延迟。
- 用户问题：d25 市场状态/过热规则仍硬编码，long 五项和财务、公告、质押、减持解禁风险长期缺失，无法区分真实无风险与来源未接入。修改后 d25 评分只消费配置派生乘数；long 使用明确的点时财务与公告公式，成功空源才生成真实 0，任一来源失败、未来、过期、非有限或结构畸形时依赖字段保持 `null`，快照增加 `d25_structured_research_incomplete` 或 `long_research_incomplete` 覆盖降级。
- Review 修复多语义公告只保留一种证据类型可能漏过减持或监管证据门，以及财务/公告冻结摘要不足以离线复算的问题；公告现在可同时保留通用、持股和监管证据，另存去重关键词命中摘要，财务摘要包含 EPS、BPS、三项同比、ROE 和利润字段。新闻与完整研究使用隔离缓存，已知新闻 JSONP 失败不会缩短或污染 d25/long 结构化缓存；四类来源摘要在 15 条证据上限内优先保留，畸形日期、质押超过 100%、单批解禁比例超过 100% 或累计超过 100% 均按来源失败保持 `null`，研究超时配置超过 8 秒时启动失败。
- 用户问题：tomorrow 的 `tail_return_30m` 和 `tail_volume_ratio` 在生产 FeatureBuilder 中固定为 `null`，尾盘结构长期只靠中性默认分；原因是候选链没有分钟数据端口且因子登记只有不可执行的“映射到 0-100”描述。修改后只为 tomorrow 硬过滤候选抓取东方财富分时，严格执行同日点时和连续交易分钟规则；不可用时仍返回本地推荐，但原始值/得分保持 `null` 并标记 `tomorrow_tail_data_incomplete`。
- 修复 5 日收益精确为 0 时量价确认被 `copysign` 错当正方向的问题；0 收益现在固定为中性 50。分钟抓取增加单次 HTTP 尝试、整批截止、短期负缓存和硬容量上限，全源超时不再按 120 只依次拖住本地评分或冻结；健康覆盖只统计收益与量比均可计算的候选，非空但样本不足的序列不再误报 100%。
- Review 修复观察时点之后才接收的分钟被误纳入、非 tomorrow 快照出现无意义尾盘缺失字段，以及只具派生分却缺少冻结原始值仍被计为完整覆盖的问题；tomorrow 现在要求原始收益、原始量比和两项派生分全部存在，d25/long 与全市场预选不构建尾盘字段。
- 澄清“v2 契约更确定”与“v2 收益更高”不是同一结论：v1 研究计划记录的当前版本真实前瞻样本为 0，当前需求又明确移除产品内验证/回测，因此现有文档只能证明 v2 的点时、过滤、融合、冻结和降级边界更可复算，不能提供收益优越性证据。
- 用户问题：today 情绪组件长期把未接入的 `news_sentiment` 与 `evidence_freshness` 当中性缺失值，无法体现真实候选新闻；原因是候选新闻虽已抓取并进入证据列表，FeatureBuilder 仍固定生成 `None`，且无效发布时间会被错误回填为观察时刻。修改后只接受非空标题、带时区且年龄为 0-72 小时的点时新闻，未来、无效和过期记录不参与评分；有证据时输出真实派生值，无证据或来源失败时 API 继续保留 `null`、缺失原因和本地推荐。
- 修复新闻结果先按请求数量截断、再校验点时时间导致前置无效记录挤掉后续有效记录的问题；适配器现在仍只请求至多 10 条，但先逐条拒绝无效/未来记录，再截取调用方所需的有效证据。
- 用户问题：风险阈值仍硬编码且多个缺失风险因子显示为零；修改说明：删除实现内 `0.5/0.75/4.0` 等触发常量，配置启动时验证完整风险表、因子策略范围和叠加组，风险事实按股票、风险、实际值、来源和交易日稳定生成并只扣一次，互斥组取最高、独立风险正常叠加且本地总扣分封顶 25；本地监管 veto 现可直接阻止执行。
- 用户问题：20 日成交额中位数缺失或非有限时仍可进入评分；修改说明：缺失、NaN/Infinity、低于 5000 万分别使用 `missing_liquidity_history`、`invalid_liquidity_history` 和 `insufficient_liquidity` 剔除，5000 万精确通过，缺失实际值在快照中保持 `null`。
- 硬过滤新增未来报价、非有限涨幅/跨源偏差和明显 OHLC 矛盾检查；主板 8.00/8.01、创业板与科创板 16.00/16.01、跨源偏差 0.5/0.5001 和报价年龄边界均使用确定性比较。
- 用户问题：候选池在历史加载前形成且旧候选自我强化；修改说明：冷启动先并发加载候选池三倍大小的行业分层历史集合，再在同一行情批次 `data_version` 内构建候选横截面，预选拒绝核心历史覆盖不足；覆盖率、失败数和版本进入服务健康状态，避免全市场逐股请求拖慢刷新。
- 用户问题：生产分位未截尾且缺失涨幅被当作下跌；修改说明：横截面因子统一执行 2.5%/97.5% 截尾与并列平均秩，NaN/Infinity 保持缺失，市场宽度只使用有效涨幅，冻结数据保存可复算边界和缺失掩码计数。
- 用户问题：补冻结只检查快照发布时间并把 today 错放宽到 30 秒；修改说明：冻结前逐条以边界时间检查报价，today 仅接受 0-20 秒，tomorrow/d25 仅接受 0-30 秒，未来或任一超龄报价会拒绝整版且记录股票与年龄。
- 用户问题：冻结后页面只能看到锚点、重启恢复不校验 committed 损坏项；修改说明：恢复现在校验 staged/committed 哈希和身份/版本，隔离损坏文件并在当前指针失效时回退上一份有效冻结；当前查询对跨交易日回退显式返回 stale、原日期和原因。
- 用户问题：today 观察期高分候选可能错误显示可执行；修改说明：09:30-09:36 观察阶段现在无条件最多为 observe，09:36 后才按 70 分主窗口门槛判断，跨策略或非执行阶段明确 unavailable。
- 用户问题：TopK 用低分候选补满导致推荐不可能为空；修改说明：排名前先应用“动作门槛减 5 分”的最低观察边界，再按最终分、本地分和代码排序并执行单行业最多 3 只，合格候选不足时不降门槛回填。
- 修正问题记录中“冻结 ID 稳定”的歧义：具体 ID 只作为 2026-07-17 当次连续查询证据，同一 committed 快照应保持不漂移，但新交易日、新数据版本或新合法冻结必须生成新 ID。
- 用户问题：11:20 后 today 无数据；修改说明：启动时会将当日截止前 30 秒内最后有效草稿按固定边界补提交，当前查询在截止后只接受 committed 冻结记录，缺少符合时效的截止前草稿时明确不伪造。
- 用户问题：tomorrow/d25 切换时先显示相同或另一波数据；修改说明：共享候选允许股票重合，但 API 和浏览器缓存按策略/日期隔离，过期草稿不再先显示后被新快照替换。
- 修复浏览器将 JSON `null` 经 `Number(null)` 错误显示为 `0.00` 的问题，并把未执行的 DeepSeek 评分、风险扣分和置信覆盖明确标记为“未复核”。
- 快速切换策略 Tab 时不再清空推荐表等待重复网络请求；已加载快照立即从页面内存显示，后台刷新失败时保留缓存快照并显式提示。
- 全市场行情请求增加有界瞬时故障重试：东方财富三个 host 首轮均断连时再尝试一轮，新浪计数或分页遇到连接错误、5xx 或无效 JSON 时最多重试一次，避免单次 `RemoteDisconnected` 或分页 504 直接触发双源降级。
- AKShare 个股新闻路径不再调用无 timeout 的库内裸请求，新闻发布时间统一归一化为 `Asia/Shanghai`；新闻失败仅增加研究源错误计数并回退到结构化行情证据，不阻塞本地推荐。
- 修复模型声明 `veto=true` 即可阻止执行、风险规则 `evidence_ttl_hours` 读取后未生效的问题；错误类型、未来或过期证据均不会进入 DeepSeek 风险扣分与 veto。
- 东方财富、新浪和腾讯实时行情请求显式绕过会导致 TLS EOF 的系统代理，避免本机代理可用但不兼容行情域名时全市场数据持续不可用；DeepSeek 等其他外部请求仍沿用原代理环境。
- 全市场行情源同时不可用时仅捕获明确的可恢复异常，保留既有候选和最近发布快照继续本地评分，并在运行状态中记录降级原因与失败计数；预期降级日志不再输出误导性的完整 traceback。
- 防止本地风险在 68/32 融合中重复扣除；固定向量 `82 - 2 / 100 - 3` 得到 `83.40`。
- 融合保留未舍入本地分精度到最终计算，修正临界值被提前舍入抬高 0.01 的问题。
- 修正 d25 在 20 日涨幅恰好 30% 时应使用 0.85、仅高于 30% 才使用 0.75 的边界。
- 定向报价刷新后再次执行硬过滤，并沿用同版本全市场横截面分位，避免过热/过期股票继续评分和候选内重排漂移。
- DeepSeek 每次重试前重新检查 14:48 截止，完成时间等于截止也按 late 处理；429、超时和成功逐物理请求独立记账。
- 冻结事件改用保留优先级、入队前持久化并支持重启重放；冻结事务同步提交当前发布指针，消除 commit 后 publish 前退出的旧草稿窗口。
- 过期交易日历刷新失败时严格 fail-closed，不再使用超过有效期的日期猜测交易日。
- 配置拒绝 NaN/Infinity，启动时锁定五维键、预算桶、阈值键和 0.68/0.32 融合契约。
- SSE 对超前或过期游标统一要求 resync；慢客户端不会阻塞发布线程。
- 修正桌面表头覆盖首行以及 Tab/SSE 在途请求竞态，迟到响应不再覆盖用户当前策略。
- 补齐原计划只有功能验收、缺少统一性能基线和缓存容量契约的问题：现固定5500只
  全市场行情、三板各120只候选、冷/热轮次、nearest-rank P95、256 MiB项目缓存
  上限、规范JSON字节估算、100 tick增长公式和绝对/相对退化失败条件。

### Removed

- 按用户要求移除股票表上方重复的“正式推荐”标题 DOM、样式及历史/长期动态标题赋值；
  推荐类型仍由策略按钮、摘要、表格内容和空态文案表达，不改变 API 或推荐业务语义。

- Web 荐股展示批次：移除形似按钮的“当前推荐/收盘补算”状态胶囊及独立观察池 DOM、
  样式和事件监听；`close_fallback` 仅作为“已冻结 · 收盘补算”非交互状态显示一次，
  `official/live` API 兼容入口保持不变。

- 本批未删除或改写任何历史快照、当日冻结记录、推荐公式、风险阈值、DeepSeek 预算、
  API 兼容参数或 Web 资源；仅移除测试夹具为缺失策略伪造同日 companion 快照的旧假设。

- 移除桌面的“临时实时/正式当前”双按钮、对应点击状态和重复 CSS；未移除草稿、正式
  冻结、收盘补算、历史、SSE 或显式 `view=official|live` API 能力。

- 从历史预热和远端历史重试集合移除 `unsupported` 股票；硬过滤和 Web API 兼容不变。
  旧 `.runtime/market_data.sqlite3` 仍保持只读，未被新热缓存替代或写回。

- 删除旧 `trader.infra.performance` 合成 runner 及其全部占位操作；CLI 仍保持原有
  `perf-check` 命令和报告入口，不增加网络调用、线程或 `create_app()` 副作用。

- 删除过时的 `docs/plan.md`，以及已完成归并的 `docs/plan_c.md`、
  `docs/plan_sudu.md` 和 `docs/plan_pipeline.md`；文档交付契约以失败测试防止已删除计划
  重新成为并行真相源。

- 移除 Web 单文件中的状态、推荐、事件和 SSE 具体实现，以及严格 Ruff 的非零债务基线；
  活动实现不再依赖宽 `**kwargs` 或超限函数来绕过审查。未移除任何推荐策略、行情源、
  DeepSeek、冻结、历史、CLI、API 或桌面能力。

- 本批未移除或修改任何生产行情源、推荐、DeepSeek、P6、API、SSE、Web、配置或测试能力；
  `docs/times.md` 明确禁止以增加隐藏线程、放宽实时门限或削弱确定性换取表面性能。

- 本批未移除任何生产过滤、因子、风险、推荐、API、持久化或 Web 能力；计划明确禁止在
  收益门禁通过前删除 v17 或把影子结果展示为实际收益。

- G5 未移除产品能力、策略、数据源、API、历史、配置、迁移、测试或资源；仅结束
  `plan_pipeline` 的未发布状态。后续工程、收益验证和外部运行风险没有被删除或伪装成完成。

- A5 未移除产品、策略、数据源、冻结历史、公共 API、配置项、迁移或桌面资源；移除的是
  P6 两项新增复杂度债务和 C5 raw facts identity 中无关的策略/板块/merge-epoch 分裂因子。
  legacy DeepSeek V3 仍只为冻结/回放兼容保留。

- D5 未移除产品能力、API、策略、持久化 schema、SSE 兼容字段或 Web 资源；仅补齐既有
  resync v2 契约和并发原因分类，并追加终审签字材料。

- B5 未移除产品能力、数据源、策略、公共 schema、配置、API、冻结记录或 Web 资源；终审仅
  收紧 dirty 失效范围、校正既有证据并新增签字材料。

- D4 未删除活动产品能力、业务数据或兼容字段；SSE patch 继续同时携带
  `patch_schema_version=2` 与 `schema_version=2`，Web envelope 保持 v3。

- 本批未删除活动产品代码、业务契约或历史数据；A4 失败探针只在本地复现期间存在，确认
  缺陷后已移除，复现条件和 owner 保留在 A4 报告中。

- 本批未移除产品能力、API、策略、行情源、DeepSeek 能力或 Web 资源；只收敛旧文档口径和
  集成已有 B/C/D 实现包，避免形成第二套 schema 或公共接缝。

- 移除活动流水线对 `SnapshotRepository.publish/latest`、`published/` 草稿 JSON 和
  `published_snapshots` 当前指针的读写；保留旧 SQLite 表仅供完整旧 release 忽略，不再
  作为 v17 事实源。移除 SSE 正常路径对推荐和 overlay 的完整 HTTP 回读。

- 删除行情源、DeepSeek 复核、预算批次/状态/汇总的 5 组旧 mixin 实现文件和继承装配路径，
  不保留兼容别名；本批未删除任何策略、行情能力、DeepSeek 配额、冻结记录、API 或 Web
  资源。

- 删除旧 `application/ports.py` 聚合端口和旧行情异常名，不提供兼容导入别名。

- 从当前 DeepSeek 优化计划中移除 long 的模型评分与请求额度，以及本批历史行情下载、
  60 日/300 配对验证实现；后者保留为需要用户另行确认的延期晋级批次，未删除权威策略
  已有的验证门禁。

- 删除领域根级 `models.py`、`recommendation_models.py`、过滤/评分/融合/排名/研究/风险/
  结果模块及根级 `strategies` 路径，同时删除动态兼容导出和未被使用的 `FilterReason` 类型
  别名；未删除或改变任何策略、行情源、DeepSeek、冻结、API、CLI 或 Web 产品能力。

- 本批未删除任何活动业务能力、策略、API、配置、冻结数据或 Web 资源；只移除未跟踪的退休
  包字节码和文档类型检查缓存。旧工程计划中的性能基线、候选 shadow 和收益验证边界已迁入
  新章节计划，没有作为“重构”名义下的废弃项丢失。

- d25 不再把“不热门”作为独立正向加分组件；过热事实及其既有软风险扣分仍保留，未删除
  行情源、DeepSeek、本地评分、正式冻结或历史推荐能力。本批不引入自动交易、自动调权或
  自动回退。

- 本批未删除产品能力、运行代码、依赖或既有权威契约；计划明确排除把 Lean、OpenBB、
  TradingAgents 和 daily_stock_analysis 引入为运行依赖。

- 删除行情服务的 `MarketFeatureState` 共享可变状态基类、当前报价 mixin 文件，以及九个
  service mixin 的继承/模板方法路径；组件间依赖全部改由构造参数显式传入。本批没有删除
  `MarketDataPort` 对外能力，也没有增加兼容 shim、第二套服务身份或仓库外迁移要求。

- 从推荐 Web 响应和普通详情中移除原始特征、权重、分位与截尾、板块策略/总体/竞争组、
  完整证据与缺失清单、逐字段来源、交易规则、快照内部版本及 DeepSeek 模型指纹、缓存
  Token、挑战者和证据 hash；这些信息未从领域模型或冻结存储删除。

- 移除历史页面仅首读一次、SSE 正常时永不刷新实时收益列的前端限制；没有删除或改写
  任何冻结记录、历史锚点、评分、动作或推荐日期。

- 移除适配器层旧长名称目录及包导入，不保留双命名兼容层，避免同一实现出现两个导入
  真相源；仓库内历史路径记录也统一改用当前 `infra` 名称。

- 移除 P3 候选发现消费 stale-while-revalidate 上一轮全市场快照的路径，以及评分优先于
  行情/候选刷新的旧事件顺序；展示和全源失败降级仍可读取最近有效行情，冻结边界、评分
  公式、DeepSeek 预算、API 与 Web 资源均未放宽或删除。

- 移除活动配置对 `.deepseek_key` 和独立 Tushare token 文件的依赖，并在 120 积分运行档禁止所有需要 2000 积分的 Tushare 物理请求；没有删除旧运行库或历史快照，本地历史种子边界只读且不向旧库写回。

- 从 Web 交付投影移除全市场逐股 `filter_details` 和 `replay_input` 大对象；这些对象仍完整保存在 committed 冻结 JSON/SQLite 身份链并供离线审计、恢复和哈希核验使用，没有删除或改写任何历史数据。

- 移除浏览器对 `previous_trade_date_snapshot` current fallback 的合法身份判断、冻结状态标签和警告提示；不删除昨日冻结文件、历史日期入口或任何审计数据。

- 删除已被两份权威文档吸收的 8 个旧文档文件及空的 `architecture/`、`issues/`、`operations/` 层级。逐批实现历史继续由本 Changelog 和 Git 历史保存，2026-07-17 审计、2026-07-20 外部项目比较、迁移清单和最终验收记录的仍有效结论已归入软件业务设计文档，不再保留会与活动契约竞争的并行副本。

- 本批未删除历史快照、锚点字段、日期接口、SSE、ETag 或任何策略数据；未以主动 HTTP 抓行情填补历史展示，也未修改冻结身份、评分、动作和哈希。

- 本批未增加第七个数据 worker，未放宽候选报价 3 秒总截止，也未隐藏真实腾讯超时、清空推荐、改写冻结快照或让普通历史/研究任务占用紧急 lane。

- 本批未隐藏任何降级提示，也未清除或返还 DeepSeek 已计数的失败预算；未用昨日数据或伪造零值替代缺失的分钟、财务、公告、质押或解禁证据。

- 本批未删除昨日冻结、跨日 stale fallback、SSE、ETag、历史日期或任何策略数据；昨日快照仍只在当日快照尚未就绪时按契约显式降级展示。

- 移除生产全市场“东方财富失败后才请求新浪”的串行回退路径，以及生产组合根中可由普通慢来源占用全部 6 个数据 worker 的通用采集形态；现在 5 个普通来源位与 1 个腾讯紧急位职责隔离，组件独立测试仍可使用受控本地执行器。未删除 v14 候选、评分、固定 68/32 融合、动作阈值、TopK、旧冻结或只读 API 路径。

- 删除与第 26 节“尚未授权生产启用”冲突的活动 `domain/strategies/shadow.py`、策略导出和快照 `shadow_scoring` 元数据；活动代码、API、UI、草稿及冻结 JSON 不再计算或携带候选初值影子排名。未删除历史文档、既有冻结数据、生产评分、预算或只读 API。

- 本批未删除预算审计、历史调用记录、API 字段、策略、冻结数据或运行依赖；数据库不可用只产生显式状态降级，不以清空数据或重建运行库规避错误。
- v10 目标契约不再允许创业板与科创板共享换手、波动、分位或模糊成长板过滤身份，也不再使用 d25 双乘数缩放总分；本批未删除或改写当前 v9 实现。`back1.md` 中的 long 三板扩池、12 套模型、ECDF、机器学习、FDR、收益标签、离线晋级、影子运行及低于 20K Star 的仓库链接均未合入生产契约。
- 从 `docs/need.md` 当前开源参考表删除 Star 低于 20K 的 Qbot、FinRL、myhhub/stock、QUANTAXIS、RQAlpha、WonderTrader、CZSC、Sequoia-X、UZI-Skill 和 QuantsPlaybook 链接；未删除归档历史、活动代码、依赖或策略实现。
- 本批次未删除现有评分、风险、预算、冻结、API、代码或测试；按用户范围不加入离线收益验证、统计晋级或运行时自动调参规则。
- 本批次未删除任何策略、依赖、代码、测试或既有参考项目；只移除 `docs/need.md` 末尾重复且断行损坏的终端表格，其全部 12 个方法引用已去重并入第 2 节。
- 本批次未删除策略、公式、冻结记录或历史兼容路径；仅用本地固定 Lucide sprite 替换页面中的刷新/关闭字符图标，未引入 CDN、移动端分支或新的运行依赖。
- 移除运行配置中重复的 DeepSeek 置信覆盖阈值；该阈值与最少已知维度继续只由版本化策略配置定义，避免运行配置和策略配置产生两个真相源。
- 移除含义模糊的内部快照编排模块路径；该路径不是公共 API，未保留会掩盖半迁移状态的兼容转发文件。
- 移除生产行情服务按调用临时创建 history/research/intraday/Eastmoney 分页线程池的路径，以及可绕过 CAS 的 `append_event()` 无条件事件写入口；组件脱离运行时独立调用时仍使用同一有界执行器适配层完成局部回收。本批不修改评分公式、冻结边界或 Web 视觉布局。
- 移除 d25 市场状态与过热乘数的实现内阈值表，以及 long 五项、财务恶化、公告、质押和减持解禁在生产特征链中的固定缺失占位路径；未删除固定 long 名单、人工目标价或只读观察边界。
- 移除生产特征构建中两个 tomorrow 尾盘因子的固定 `None` 占位路径；DeepSeek 功能未在第 12 或第 13 节中删除或扩展。
- 本批次未删除任何策略、配置、代码、测试或归档资料，也未用历史口径覆盖当前需求。
- 移除 AKShare 新闻缺失或非法时间回填为“当前时刻”的伪新鲜证据路径；本批次未删除相邻策略、API 或 Web 功能。
- 删除活动 `stock_analyzer` 包、根 `app.py`、旧 static/templates、旧配置和重复 requirements。
- 删除验证、回测、自动调参、预测、paper trading、OOS/实验功能及其 Web 路由、资源和旧测试。
- 删除根 `analysis`、`experiments` 活动产物和旧依赖指纹脚本；有保留价值的资料仅归档，不进入 wheel。
- 本批未删除或修改活动策略、风险、融合、冻结、API、UI、配置和代码；计划明确不引入
  第二个数据库、缓存框架、benchmark依赖、移动端分支或用性能优化放宽实时性门槛。

### Verification

- 顶部信息区与紧凑列表布局通过 Web 资源/主表契约测试和真实 Firefox/geckodriver 验收；
  用户提供的完整长收盘补算降级串与长最近错误在 1280x720、1440x900、1920x1080 下
  均保持两栏 52px、各自可滚动，摘要紧邻策略行、策略行紧邻表头，页面无横向溢出、
  浏览器错误或 resync。24 个 SSE patch 的 patch-to-paint P95 为 23ms（预算 100ms）。
  对实际 `127.0.0.1:5000` 服务重启前后分别取 HTML 与 1440x900 截图，确认混合缓存时
  放大的“正式推荐”存在，重启加载最终模板后该节点消失且行序正确。`make lint`、
  172 个源码文件 mypy、除一条既有盘后恢复用例外的全量 pytest、`make package` 通过；
  仓库外 wheel 可从安装目录导入、执行 `trader-cli --help` 与绝对配置校验、读取模板与
  10 项 CSS/JavaScript/SVG 资源，并通过 `pip check`。

- Web 荐股展示批次：`make format-check`、`make lint`、`make type-check`、`make test`
  （784 项）和 `make package` 通过；38 项 Web/API/SSE 定向测试与 Node 策略日期状态机
  通过。Firefox/geckodriver 离线真实页面验证 1280x720、1440x900、1920x1080 均无页面级
  横向溢出或浏览器错误，24 个 SSE patch 的 patch-to-paint P95 为 19ms（预算 100ms）。
  仓库外安装 wheel 后确认 `selection.js`、模板、CSS、其他 JavaScript 与图标均可读取，
  `trader` 从安装目录导入，`trader-cli --help`、绝对配置 `validate-config` 和
  `pip check` 通过。

- 本批先以本机运行库确认旧 v2 有 5 份 committed 历史但三策略日期交集为空，v17 当日
  三策略均已 committed 但各为 0 只，且 qfq 热缓存随后达到三板各 119 只。新增失败回归
  分别复现部分策略历史被隐藏、历史未就绪仍固化收盘结果；修复后验证 resident 与 cold
  部分策略日期、日期级 single-flight、冻结 current pin、历史 API、收盘退避重试，以及
  预热完成后三策略均实际产生股票且不含三板样本/可靠度不足降级。相关 Web、P6 和流水线
  完整测试通过；`make format-check`、`make lint`、172 个源码文件 mypy、全量 pytest
  和 `make package` 通过。仓库外 wheel 从隔离目标导入，`trader-cli validate-config`、
  `pip check` 与模板、4 个 CSS、2 个 JavaScript、2 个 SVG 共 9 项资源通过。
  Firefox/geckodriver 应用 24 个实时 patch，零 resync、零页面错误，patch-to-paint P95
  为 61ms；1280x720、1440x900、1920x1080 均有有效页面且无页面级横向溢出。

- 自动当前视图覆盖冻结前草稿、冻结后正式结果、冻结失败保留同日草稿、上一交易日拒绝、
  空 `not_ready`、历史和显式 API 兼容；Node 状态机覆盖 current 模式的实时 patch、
  草稿到正式切换和冻结后迟到草稿拒绝。`make format-check/lint/type-check/test/package`
  全部通过，仓库外 wheel 的包导入、CLI、模板/CSS/JavaScript/SVG 与 `pip check` 通过。
  Firefox/geckodriver 应用 24 个实时 patch，零 resync、零页面错误，patch-to-paint
  P95 为 19ms；1280x720、1440x900、1920x1080 均有有效页面且无页面级横向溢出。

- 本批先用四项失败回归复现三板配额不足、分钟请求错误负缓存、评分前未刷新尾盘缓存和
  DeepSeek 降级误报；并发回归连续三轮及包含原 deadline 语义的扩展回归通过。另以
  同 TTL 重启零远端调用、过期刷新失败回退、容量淘汰/损坏库不中断三类回归验证独立
  历史热缓存，并覆盖同步 `run_once`、异步评分事件和录制影子适配器。完整
  `make format-check`、`make lint`、`make type-check`、`make test` 和 `make package`
  通过；37 项架构、`create_app()` 无副作用、固定融合 `83.40`、188 原子预算、SSE
  游标/慢客户端、冻结和哈希专项通过。仓库外 wheel 安装、包导入、`trader-cli --help`
  及模板、4 个 CSS、2 个 JavaScript、2 个 SVG 共 9 项资源读取通过。Firefox 无头真实
  浏览器在 1280x720、1440x900 和 1920x1080 均有有效页面、无浏览器错误和页面级横向
  溢出；24 次实时补丁均应用，patch-to-paint p95 为 39ms。

- T1 定向验证通过延迟采集、配置、真实性能入口、行情网关、架构、Web/API、流水线和
  JavaScript 状态机测试；mypy 覆盖 172 个生产文件。真实浏览器应用 24 个 SSE patch，
  最终 patch-to-paint P95 83ms（预算 100ms），1280x720、1440x900、1920x1080 均无
  页面级横向溢出。`make format-check/lint/type-check/test/package` 全部通过；仓库外
  全新虚拟环境成功安装最终 wheel，包导入、CLI、配置、模板、CSS、JavaScript、双图标
  和 `pip check` 均通过。

- 本批已逐项对照四份旧计划、两份权威文档、活动 DeepSeek/列式/P6 实现、`config/v2`
  和 G5 报告；32 项定向契约测试与完整 `make format-check`、`make lint`、
  `make type-check`、`make test`、`make package` 均通过，严格重构债务为零。仓库外
  wheel 安装、顶层包导入、模板/CSS/JavaScript/SVG 资源读取、`trader-cli --help` 和
  `pip check` 通过；首次 `--no-deps` CLI 探测因验收环境按设计没有 Polars 而失败，随后
  使用项目已锁定依赖环境加载同一 wheel 安装目标复验通过。`git diff --check` 通过。

- 任务 A 的隔离候选树通过 `make format-check`、`make lint`（严格诊断为零）、
  `make type-check`、`make test` 和 `make package`；架构/`create_app()` 无副作用、固定
  融合 `83.40`、188 原子预算、冻结恢复、哈希、SSE 游标/慢客户端均包含在全量回归。
  仓库外安装 wheel 后从 `site-packages` 导入包、执行 `trader-cli --help`、读取模板、
  4 CSS、2 JavaScript、2 SVG 共 9 项资源并通过 `pip check`。Firefox 在 1280x720、
  1440x900、1920x1080 精确内容视口均显示 18 个唯一代码，无白屏、页面横向溢出、关键
  重叠、页面脚本错误或更新后布局跳动，详情抽屉 3 分区完整位于视口。

- `docs/times.md` 已核对非权威边界、两份权威文档链接、现有性能证据、固定融合/冻结/
  188 次预算和桌面范围。以当前上游叠加本批 3 个文件的隔离快照执行五项 make 门禁，
  235 文件格式、严格 Ruff 债务、164 个源码文件 mypy 和完整 pytest 均通过；sdist/wheel
  构建成功，仓库外安装可导入、执行 CLI、读取 9 项 Web 资源且 `pip check` 无破损依赖。

- `docs/strage.md` 已按非权威计划边界复核完整链路、固定融合、冻结、预算和不承诺收益
  约束；文档契约 3 项及 `make format-check`、`make lint`、`make type-check`、`make test`、
  `make package` 全部通过。仓库外 wheel 可导入包、读取 6 项 Web 资源并执行
  `trader-cli --help`；`git diff --check` 未发现空白错误。

- G5 复跑 A4 同进程压力，逻辑 `205,468,511 B`、峰值 RSS `385,851,392 B` 均通过；
  正式 v17 `perf-check --suite all` 16 项通过且零网络。B4 相对 CPU 首轮改善
  `16.842%` 未达 20%，相同身份无并行负载重跑为 `26.854%` 并通过，业务哈希、绝对时延、
  内存始终一致；v16 三板四项绝对预算通过且未宣称 CPU 加速。五项 make、仓库外 wheel、
  `pip check`、CLI/配置/9 项资源和 Firefox 三档由本批最终门禁复核。

- A5 定向复验架构、app factory、固定融合 `83.40`、冻结/P6、SQLite 迁移、DeepSeek
  预算/C2-C5、SSE/Web 与端到端；P6 重构后的 83 项及 DeepSeek/P6/Web 联合集 148 项通过。
  Firefox 152.0.4 在 1280x720、1440x900、1920x1080 均显示 18 行，无页面横向溢出、
  关键重叠或浏览器错误，抽屉三分区完整；两次 patch 为零完整推荐 GET、成功应用 2 次。
  五项 make、仓库外 wheel 导入/CLI/资源和最终 `HEAD == @{upstream}` 由本批最终门禁核对。

- D5 的 publisher/P6/Web API/app factory 53 项、P6/冻结恢复定向集成 9 项、D4 性能、
  架构、固定融合 `83.40`、188 并发预算及 Node 状态机均通过。Firefox 152 在
  1280x720、1440x900、1920x1080 精确内容视口均显示 18 行，无横向溢出、关键重叠或页面
  错误，三分区抽屉位于视口内；两次 patch 为零完整 GET，显式 resync 产生一次 ETag GET。
  五项 make 门禁全部通过；仓库外 wheel 可导入、执行 CLI 并读取 9 项 Web 资源。

- G4 发布批次复验五项 make 门禁、固定 `83.40`、C4/D4 定向回归、v17 16 项性能、
  B4 5500 行/360 候选/100 tick、A4 同进程内存和 Firefox 152.0.4 三档桌面；最终 B4
  columnar 改善 `32.404%`，标准化/合并/canonical P95 为
  `169.247/675.536/1219.953ms`。综合逻辑字节 `205,468,511B`、当前/峰值 RSS
  `370,069,504/387,112,960B`、USS `312,655,872B`、Polars `1,282,816B`，均在上限内。
  仓库外 wheel 的包导入、CLI、配置、9 项资源和 `pip check` 通过；三档均为 18 个唯一代码，
  无白屏、横向溢出、关键重叠或页面错误，增量 patch 不产生完整 GET。

- B5 定向 B 域 171 项测试通过；最终固定行情复跑的 scalar/columnar process-CPU P95 为
  `1545.992/1172.998ms`，改善 `24.126%`，标准化/两源合并/统一快照 P95 为
  `140.455/545.278/1314.793ms`。三板预选/评分/三板三策略/稳定选择 P95 为
  `51.411/9.030/519.941/3.700ms`。B-owned 100 tick 逻辑字节 `29,661,328B`、增长
  `0.0%`、峰值 RSS `273,195,008B`；集成并存场景逻辑字节 `205,468,511B`、峰值 RSS
  `387,186,688B`，均通过固定上限，业务和 canonical SHA-256 保持一致。隔离重建的精确
  `HEAD + B5 staged diff` 通过 format、lint、164 源文件 mypy、738 项 pytest 和 package；
  仓库外 wheel 可导入、执行 CLI/配置校验、读取 9 项 Web 资源且 `pip check` 无断裂依赖。
  并行 A 侧补齐 G4 报告后，共享工作树同样通过五项 make 与仓库外 wheel 验收。

- B4 固定验收 runner 通过：标准化+观察值构造+两源合并 process-CPU P95 相对 scalar 改善
  `27.22%`；标准化/两源合并/统一快照 wall P95 为 `134.059/586.035/1130.823ms`，
  100 tick 逻辑缓存 `29,661,328B`、分配增长 `0.0%`、峰值 RSS `288,051,200B`，均在门禁内。
  B 域定向单元 `48` 项、行情组件 `122` 项、Ruff、mypy 和严格债务基线均通过；完整证据见
  `tests/fixtures/market_data/pipeline_b4/report_to_a.md`。

- D4 固定门禁记录 P6→SSE 入队 P95 `4.357ms`、权威 SSE 年龄 `0.000s`、当前/驻留/ETag/日期/状态
  API P95 `2.382/1.808/0.797/1.352/1.758ms`；单股 patch `1,133B` 对完整响应 `10,952B`，节省
  `89.655%`。Firefox 152 三档精确内容视口用 10 条正式 + 8 条观察、长错误、详情抽屉和
  实际 SSE 验收，无白屏、关键重叠、页面横向溢出或页面错误；有效 patch 的完整 GET 增量
  为 0、布局位移 0px，显式 resync 只产生一次条件 GET 并命中 304。最终稳定共享树的
  `make format-check/lint/type-check/test/package` 全部通过；仓库外安装 wheel 后可导入包、
  执行 `trader-cli --help`，并读取模板、CSS、JavaScript 和图标资源。

- A4 最终稳定树验证：F01/F04 精确失败用例、pipeline/P6/publisher 定向回归、C4 七项及完整 pytest
  通过；`make format-check/lint/type-check/test/package` 全部通过，mypy 检查 164 个活动源码，
  严格债务保持基线。正式 `perf-check --suite all` 16 项通过且零网络；B4 最终 columnar 改善
  `35.544%`，v15/v16/D4 专业预算全部通过。A4 同进程六池约 70%、双快照/列式 epoch、
  DeepSeek 最大批次与 P6/SSE 压力保持强引用并存时，逻辑字节
  `205,468,511B <= 260,046,848B`，峰值 RSS `387,452,928B <= 402,653,184B`，USS
  `358,887,424B`。仓库外最终 wheel 可导入安装目标、执行 CLI/配置校验、读取 9 项 Web 资源，
  `pip check` 通过；D4 Firefox 三档桌面证据通过。

- G3 门禁复核批次验证：读取阶段 3 计划、A3/B3/C3/D3 报告，确认四方均已提交
  `ready_for_gate=yes` 交接证据并发布 G3；定向契约测试
  `tests/contract/test_project_records.py tests/contract/test_pipeline_contract_base.py tests/component/test_pipeline_deepseek_c3.py`
  覆盖 G3 发布状态、docs 报告白名单和 C3 raw facts 缓存复验。`make format-check`、`make lint`、
  `make type-check`、`make test` 和 `make package` 通过，生成物已清理。
  仓库外 wheel 安装到 `/tmp/trader-wheel-g3` 后可导入 `trader`、读取 Web 模板/静态资源，
  并可执行 `trader.entrypoints.cli --help`。

- A3 集成批次验证：定向集成测试
  `tests/unit/test_v17_columnar_changes.py tests/unit/test_v17_columnar_provider_adapter.py tests/unit/test_v2_market_data_normalize.py tests/unit/test_v2_market_data_merge.py tests/unit/test_v2_market_data_router.py tests/unit/application/test_candidate_features.py tests/unit/test_v2_deepseek_base.py tests/component/test_v2_deepseek.py tests/component/test_v2_deepseek_v4.py tests/component/test_pipeline_deepseek_c2.py tests/unit/application/test_published_snapshots.py tests/unit/application/test_publisher.py tests/contract/test_v2_web_api.py tests/contract/test_v2_app_factory.py tests/contract/test_pipeline_contract_a2_public_skeleton.py tests/contract/test_pipeline_contract_base.py`
  通过 183 项；`make format-check`、`make lint`、`make type-check`、`make test`、
  `make package` 和 `git diff --check` 均通过。仓库外 wheel 安装到 `/tmp/trader-wheel-a3`
  后可导入 `trader`、读取 Web 模板/静态资源，并可执行 `trader.entrypoints.cli --help`。

- G2 发布批次验证：仅读取 B2/C2/D2 报告和 B2 fixture，确认 A2/B2/C2/D2 均为
  `ready_for_gate=yes`；定向契约测试
  `tests/contract/test_project_records.py tests/contract/test_pipeline_contract_base.py` 通过 8 项，
  `git diff --check` 通过。

- 2026-07-23 G2 复核批次验证：仅读取 B2/C2/D2 报告，确认 C2 标准字段已补齐、B2 仍为
  `ready_for_gate=no`；定向契约测试
  `tests/contract/test_project_records.py tests/contract/test_pipeline_contract_base.py` 通过 8 项，
  `git diff --check` 通过。

- G2 门禁复核批次验证：仅读取 B2、C2、D2 交接报告和 fixture 路径，未执行 B/C/D 内部算法；
  定向文档契约测试 `tests/contract/test_project_records.py tests/contract/test_pipeline_contract_base.py`
  通过 8 项，`git diff --check` 通过。当前判定证据是 B2 报告明确 `ready_for_gate=no`、
  C2 报告缺少标准字段、D2 报告 `ready_for_gate yes`，因此共同门禁不满足。

- A2 公共骨架批次验证：定向契约与配置测试
  `tests/contract/test_pipeline_contract_a2_public_skeleton.py tests/contract/test_v2_architecture.py tests/unit/test_v2_settings.py`
  通过；扩展文档契约后 5 个定向文件共 71 项通过，覆盖公共 schema/version、long 零复核、
  DeepSeek V4 facts 证据边界、P6
  projection/overlay CAS、HTTP/DeepSeek 零副作用替身、248 MiB 逻辑缓存拒绝和 384 MiB
  进程峰值拒绝。A2 范围 Ruff format/check 通过；`make type-check` 通过 162 个源码文件；
  `make package` 沙箱内因构建依赖联网失败，提升权限后通过并清理生成物。全局
  `make format-check`/`make lint` 被非 A2 的 DeepSeek/C2 未提交文件格式与导入问题阻断；
  全局 `make test` 运行完成，剩余 2 个既有失败：bootstrap duplicate start 和 final
  candidate cadence 计数。

- G1 发布批次验证：复核 B1、C1、D1 三份标准报告均包含 `ready_for_gate=yes`；确认
  `HEAD == @{upstream}` 后发布 `CONTRACT_BASE`。`make format-check`、定向契约测试
  `tests/contract/test_project_records.py tests/contract/test_pipeline_contract_base.py`、
  `git diff --check` 和 `make package` 通过；`make type-check` 通过。全局 `make lint`
  仍失败于既有严格债务计数漂移，全局 `make test` 仍失败 5 项，失败面与 A1 记录一致。

- A1 基线：`make format-check` 通过；`make lint` 在既有严格债务计数漂移处失败；
  `make type-check` 通过；`make test` 在 A1 修正文档白名单后仍有 5 个既有失败；`make package` 沙箱内因
  构建依赖联网失败，提升权限后通过。离线 `perf-check --suite all`、v15 market-data
  runner、v16 board-scoring runner 均通过，且无外部网络调用。1280x720、1440x900 和
  1920x1080 无头 Chrome 截图非白屏；无运行态页面显示 `not_ready`，SSE 因未注入
  publisher 返回 503。完整证据见 `docs/reports/pipeline-a1-baseline.md`。

- 本批新增严格配置、列式 dirty set、P6 完整日期预热、日期 single-flight、检查点
  hash/consume、SQLite schema v8、SSE patch 和慢客户端自动回归。首轮 145 个相关用例
  发现 8 个旧契约或实现问题，修复后 8/8 定向复测通过；最终完整质量、打包、wheel、性能
  与桌面门禁结果在本批提交前继续更新。

- 2.4 的 659 项完整 pytest（验收时暂时隔离并随后原样恢复与本批无关的未跟踪
  `docs/plan_pipeline.md`）、213 文件 Ruff format、Ruff lint/严格债务、154 个源码文件
  mypy、源码/架构 AST、`create_app()` 无副作用、固定融合向量 83.40、预算并发、冻结恢复、
  SSE 游标/慢客户端、哈希一致性和 `make package` 均通过；PLR0913 从 58 降至 55，活动
  Python 单文件均小于 800 行。固定性能负载通过：v15 为 5500 行行情/360 候选且三个 P95
  均低于预算，v16 为三板各 120 候选且四个 P95 均低于预算。仓库外 wheel 安装、CLI、
  包内模板/CSS/JavaScript/图标以及 1280x720、1440x900、1920x1080 桌面渲染亦纳入验收。

- 本批逐项核对 P1-P6、256 MiB 初始目标、性能 P95、只读 Web、SSE、冻结不可变、固定融合
  和 DeepSeek 预算/截止契约；验证计划明确要求 scalar/columnar 逐字段等价、最终分/动作/
  TopK/哈希完全一致、Python 3.10-3.14 wheel 可安装，以及三档桌面实际渲染。本文档批次
  通过 Markdown 链接、禁词/边界检索、`git diff --check` 和适用仓库文档契约检查。

- 2.3 通过架构 AST、154 个源码文件 mypy 和完整 pytest 回归；严格债务从 145 降至 142
  （N818 7→5、PLR0913 59→58）。

- 本批逐节核对 `AGENTS.md`、`docs/software-business-design.md` 和
  `docs/recommendation-strategy.md` 的固定融合、188 次全局上限、冻结时间、纯本地收盘
  补算及验证门禁；当前文档契约 3 项、`git diff --check` 和 `make package` 通过。起始
  `HEAD=8c81db9` 的隔离源码通过 Ruff format/check、严格债务门禁、146 个源码文件 mypy、
  完整 pytest、无隔离构建及仓库外 wheel 的包导入、CLI 和静态资源验收。当前工作树的
  format/lint/mypy/test 被本批开始后出现的无关 `application/ports` 重构中间态阻断，
  本批未修改、暂存或掩盖该并发变更。

- 本章最终 Review 已通过 Ruff format/lint、领域严格告警归零、146 个源码文件 mypy 和完整
  651 项 pytest；仅保留 10 条既有未知测试模型名警告。固定融合向量仍为 `83.40`。以起始
  提交 `a90eea` 和当前实现分别运行同一整日冻结
  fixture，today/tomorrow/d25 三份冻结 JSON 与四份发布 JSON 的相对路径、SHA-256 和记录数
  逐项完全一致。固定 5,500 行行情性能门禁通过（标准化/合并/统一快照 P95 分别为
  97.151/511.747/749.581ms），360 候选板内评分门禁通过（预选/评分/全局选择/三板墙钟
  P95 分别为 17.533/2.232/1.790/207.246ms）。sdist/wheel 隔离构建通过；wheel 从仓库外
  前缀导入，新领域请求类型、`trader-cli --help` 及模板、4 个 CSS、2 个 JavaScript、2 个
  SVG 共 9 项资源通过。1280x720、1440x900、1920x1080 三档 Chrome 实际截图复核无白屏、
  重叠、页面级横向溢出或明显布局跳动。

- 本章 Review 通过 Ruff format/lint、严格债务精确基线、140 个源码文件 mypy、完整 649 项
  pytest 和架构/无副作用应用工厂定向契约；pytest 仅保留 10 条既有未知测试模型名警告。
  sdist/wheel 隔离构建通过；wheel 从仓库外前缀导入，两个 CLI、`pip check`、模板、4 个
  CSS、2 个 JavaScript 和 2 个 SVG 共 9 项包资源通过。1280x720、1440x900、1920x1080
  三档 Chrome 实际截图复核无白屏、重叠、页面级横向溢出或明显布局跳动。

- v17 最终 Review 通过 Ruff format/lint、140 个源码文件 mypy、完整 647 项 pytest 和
  sdist/wheel 隔离构建；pytest 仅保留 10 条既有未知测试模型名警告。79 项定向回归覆盖
  下行保护、入场点时性、正式/观察分池、Web 加法投影、结果结算及冻结哈希不变；固定融合
  `83.40` 和旧 v16 回放由全量契约继续通过。wheel 从仓库外前缀导入，`trader-cli --help`
  及模板、4 个 CSS、2 个 JavaScript、图标资源均通过。1280x720、1440x900、1920x1080
  三档桌面 DOM 复核无白屏、头部重叠或页面级横向溢出，观察表仅保留内部横向滚动。

- 本批计划文档通过文档拓扑契约、Markdown diff 审查和空白检查；完整质量、测试、构建及
  仓库外 wheel 安装结果在提交前复核。本批没有修改 Web 资源或运行布局，三档桌面渲染
  行为不受影响。

- 以本批起始提交 `d0614bc` 加且仅加类级组合重构 diff 的隔离副本执行：Ruff format/lint、
  137 个源码文件 mypy、完整 627 项 pytest、sdist/wheel 构建全部通过，pytest 仅保留 10 条
  既有未知测试模型名警告；仓库外安装后可从独立前缀导入，两个 CLI、配置校验、模板、
  4 个 CSS、2 个 JavaScript、2 个 SVG 和 `pip check` 均通过。架构契约同时校验协调类
  零继承、旧状态文件消失、六个核心组件存在且各自锁身份不同；行情组件定向回归全部通过。
  v15 固定快照哈希保持
  `234b923cb17d1979365892791f38545598ae2d25f0cbe14817980a3080c3329b`，最终隔离复测的
  规范化/合并/快照 P95 为 151.621/800.098/1,378.152ms，均在 800/1,000/1,500ms 预算内；
  v16 固定评分复测亦在预算内。本批未改 Web 资源和布局，因此未重复三档桌面截图。

- v15 固定录制负载最终复测通过：两源合并 P95 860.711ms/1,000ms、统一快照 P95
  1,090.479ms/1,500ms，规范化 P95 157.525ms；固定完整快照哈希继续为
  `234b923cb17d1979365892791f38545598ae2d25f0cbe14817980a3080c3329b`，内存估算继续为
  28,378,644 字节。新增规范 JSON 字节和代表性合并快照哈希回归；v16 三板评分性能复测仍
  全部通过。`make format-check/lint/type-check/test/package` 全部通过（mypy 139 个源码文件，
  pytest 仅保留 10 条既有未知测试模型名警告），sdist/wheel 隔离构建成功。最终 wheel 从
  仓库外前缀导入，两个 CLI、配置校验、模板、4 个 CSS、2 个 JavaScript、2 个 SVG 和
  `pip check` 均通过。本批未改 Web 资源或 UI 布局，因此未重复三档桌面截图。

- 收盘恢复回归覆盖同进程 P6 身份保留与收盘价替换、冷启动三策略本地重建、数据库优先、
  仅补缺失策略、三板不完整不落盘并重试，以及冻结 JSON 往返、确定性回放、正式查询
  `ready` 和 Web `close_fallback` 标识。`make format-check/lint/type-check/test/package` 全部
  通过（mypy 139 个源码文件、pytest 624 项；仅保留 10 条既有未知测试模型名警告），
  sdist/wheel 构建成功。最终 wheel 在仓库外目录安装并从独立 site-packages 导入，
  `trader-cli --help`、模板、CSS、JavaScript 和图标资源通过。Firefox 152 在精确
  1280x720、1440x900、1920x1080 视口均完整加载 `dashboard.js?v=12`，无白屏、损坏图片、
  Header/主区或工作区块重叠、页面级横向溢出。

- 行数门禁架构契约定向回归通过；为避免把并行中的用户业务修改混入本批，使用已推送
  `08c4d43` 基线叠加本批 3 个文件的隔离副本完成 Ruff format/lint、138 个源码文件
  mypy、完整 618 项 pytest、sdist/wheel 构建。仓库外 wheel 的包导入、`trader-cli`、
  9 项 Web 资源与 `pip check` 通过；当前仓库 editable 安装已恢复并指向活动源码。

- v3 ready/not_ready/error、当前/历史、正式/临时实时、overlay、精简复核和风险去重契约
  通过；JavaScript 语法检查、Ruff format/lint、138 个源码文件 mypy、完整 pytest 与冻结
  持久化关联回归通过。sdist/wheel 构建成功；wheel 在仓库外独立前缀完成导入、v3 schema、
  `trader-cli`/`trader-server`、模板、CSS、JavaScript、图标和 `pip check` 验收。

- 本批定向验证已通过 cadence 冻结后冷启动单次恢复、恢复任务不评分/不发布、正式接口
  继续 `not_ready`、`view=live` 返回同日草稿、非法视图拒绝、历史实时行刷新和包内静态
  资源契约；`make format-check/lint/type-check/test/package` 通过（mypy 138 个源码文件、
  pytest 617 项，wheel/sdist 构建成功）。仓库外 wheel 的 `trader` 导入、`trader-cli`、
  `validate-config`、模板/CSS/JavaScript/图标资源和 `pip check` 通过。Firefox 152 在
  1280x720、1440x900、1920x1080 完成截图与 DOM 尺寸检查，均无白屏、重叠或页面级横向
  溢出，“临时实时”入口可见。

- 失败先行回归覆盖实时核心任务 FIFO、TopK 快通道、候选事件不等待分钟历史、依赖排队
  窗口与实际执行预算分离、历史预热单批在途、上海日期边界和重复系统启动生命周期；
  `make format-check`、Ruff、138 个源码模块 mypy、完整 612 项 pytest、sdist/wheel 构建
  全部通过，仅保留 10 条既有未知测试模型名警告。最终 wheel 在仓库外目标目录导入，
  `trader-cli --help`、`validate-config` 和 9 项 Web 资源通过。真实服务确认修复前已恢复
  tomorrow/d25 的 74/81 个候选与 4/2 条草稿，但旧进程累计 139 个事件过期并错过
  14:50 合格检查点；最终进程启动正常、Tushare 精确显示 `permission_denied`。

- 失败先行架构测试已复现新目录缺失，并在迁移后确认 `src/trader/infra` 可导入、旧目录
  和旧活动导入均不存在；最新上游叠加本批后的 188 个源码/测试文件格式检查、Ruff、138 个
  源码模块 mypy、完整 612 项 pytest、sdist/wheel 构建均通过，仅保留 10 条既有未知测试模型名警告。
  仓库外安装 wheel 后确认从隔离包路径导入 `trader.infra`、旧包不可发现、
  `trader-cli --help`、配置校验、9 项 Web 资源及活动环境 `pip check` 通过。本批不修改 Web
  资源或布局，桌面三档沿用同一已验收资源基线。

- 失败先行回归分别复现并修复周期全市场 `force=False`、历史预热空筛选清除既有候选、
  `SCORE < CANDIDATE_QUOTES < MARKET_QUOTES` 的逆依赖顺序；`make format-check`、Ruff、
  138 个源码模块 mypy、完整 600 项 pytest、sdist/wheel 构建均通过，pytest 仅保留 10 条
  既有未知测试模型名警告。仓库外 `--target` 安装 wheel 后确认从隔离包路径导入、
  `trader-cli --help`、`validate-config`、9 项模板/静态资源和活动环境 `pip check` 通过；
  完整依赖独立 venv 安装受宿主磁盘配额阻断，未伪报通过。本批未修改 Web 资源，桌面三档
  布局沿用同一已验收资源基线。

- 失败先行回归覆盖双凭据权限/优先级、120 积分能力门、`daily` 多代码单请求、HTTPS 直连且不继承环境代理、批内部分成功、实时任务不等待历史、接收时间发现与原始来源时间保留、本地种子只读及有界并行分页。2026-07-22 12:08-12:12 真实服务首轮在 8.1 秒内由 Sina 取得 5,529 行全市场行情，事件成功完成；本地种子首批计划/完成 30/30、失败 0，历史覆盖达到 31/360，后续 34/360。Tushare 请求实际到达官方接口并被分类为 `quota_or_rate_limit`，未泄露 Token；Web 对尚未形成当日冻结快照正确返回 `not_ready`，未用昨日推荐伪装实时结果。`make format-check/lint/type-check/test/package` 全部通过（138 个源码模块、597 项 pytest，只有 10 条既有未知测试模型名警告）；最终 wheel 在仓库外安装后通过隔离包路径导入、配置校验、CLI、9 项模板/静态资源读取，活动环境 `pip check` 无损坏依赖。

- 本地真实运行库预热 5 个历史视图耗时 3469.498ms，发生在 HTTP 接收前；同一工作树 Flask 热请求实测当前空响应 3.694ms/572B、tomorrow 2026-07-20 历史 3.030ms/92,923B、d25 同日历史 3.832ms/107,395B，均低于 200ms 当前/驻留历史预算，且响应 `filter_details` 为 0。现场旧进程对相同有效历史的修复前 TTFB 为 0.942-1.800 秒、响应最大 350,970B；有效样例同时返回实时“今日涨幅”和独立“锚点至今”。完整门禁、wheel 与提交后运行态复验见本批最终记录。

- 用户补充反馈的失败先行回归已复现并转绿：当前查询不会复用昨日快照或生成 current ETag，页面包不再包含上一交易日 fallback 提示，历史报价读取在 P2 特征提交前可命中当日规范行情。`make format-check`、`make lint`、134 个源码模块 mypy、完整 584 项 pytest 与隔离 `make package` 通过，pytest 仅保留 10 条既有未知测试模型名 RuntimeWarning；最终 wheel 在仓库外覆盖安装后通过 `pip check`、配置校验、v9 缓存版本及模板、4 个 CSS、2 个 JavaScript、2 个 SVG 共 9 项资源读取。HTML/CSS 布局未变，桌面三档沿用同布局资源已通过基线。

- 本批双文档结构契约、旧路径残留扫描、相对链接、`git diff --check` 和本批 Python 文件 Ruff format/lint 通过；全量 Ruff lint、134 个源码文件 mypy、584 个 pytest、sdist/wheel 构建通过。wheel 在仓库外临时虚拟环境安装后可从隔离路径导入，模板、CSS、两个 JavaScript 和两个 SVG 资源齐全，`trader-cli --help`、`validate-config` 与 `pip check` 通过。

- 历史行情修复的 Web API 契约测试和行情索引组件测试通过；`make format-check`、`make lint`、134 个源码模块 mypy、完整 583 项 pytest 与 `make package` 全部通过，pytest 仅保留 10 条既有未知测试模型名 RuntimeWarning。仓库外隔离安装 wheel 后通过包导入、配置校验、CLI、`pip check` 及模板、4 个 CSS、2 个 JavaScript、2 个 SVG 共 9 项资源读取；本批未修改 HTML/CSS/JavaScript，桌面布局沿用同资源三档已通过基线。

- 批次二 195 项局部矩阵和完整 580 项 pytest 通过，保留 10 条既有未知测试模型名 RuntimeWarning；`make format-check`、`make lint`、134 个源码模块 mypy、`make package` 和 `git diff --check` 通过。v16 性能报告使用预热 1 轮、测量 5 轮和 nearest-rank，并真实启动三条 lane、对每策略 360 只候选执行全局选择：板内预选 P95 28.446ms、单板评分 3.583ms、三板三策略墙钟 295.877ms、全局选择 2.434ms，均通过 250/250/1000/100ms 配置预算；报告同时保存三板各 18 个队列等待样本、串行参考、墙钟比、3.344947 秒进程 CPU 和 1080 峰值条目。最终 wheel 在仓库外安装全部声明依赖后从独立 site-packages 导入，两个 CLI、配置、9 项资源和 `pip check` 通过；Firefox 152 在实际 1280x720、1440x900、1920x1080 视口均生成有效 PNG，DOM 检查及人工复核确认无白屏、关键同级重叠、裁切或页面级横向溢出。

- 两层失败先行回归已复现普通数据 lane 饱和时紧急任务无法启动，以及候选报价因此超过批次截止；实现后紧急任务和 `MarketFeatureService.refresh_candidate_quotes()` 均在普通 lane 被阻塞时按时完成，组合根契约确认生产 6-worker 池内恰有 1 个紧急 worker，背压回归确认只允许 1 个紧急等待任务且更多提交被显式拒绝。`make format-check`、`make lint`、111 个源码文件 mypy、完整 457 项 pytest、`make package` 和 `git diff --check` 通过，pytest 仅保留 10 条既有未知测试模型名 RuntimeWarning；最终 wheel 在仓库外隔离目标目录安装全部依赖后通过包导入、两个 CLI、9 项 Web 资源与 `pip check`。本批未修改 Web 资源，桌面门禁沿用同资源 1280x720、1440x900、1920x1080 三档已通过基线。

- 失败先行组件回归已复现同一代码在相隔 3 分钟的周期风险刷新中被请求两次，以及整批截止时未初始化 TTL 的异常；修复后成功结构化研究仅请求一次，整批截止写入短期降级并正常返回。`make format-check`、`make lint`、111 个源码文件 mypy、完整 454 项 pytest、`make package` 和 `git diff --check` 通过，pytest 仅保留 10 条既有未知测试模型名 RuntimeWarning；最终 wheel 在仓库外隔离目标目录安装全部依赖后通过包导入、两个 CLI、9 项 Web 资源与 `pip check`。本批未修改 Web 资源，复核上批同资源 1280x720、1440x900、1920x1080 三档截图无白屏、重叠或页面级横向溢出。

- 失败先行契约已复现看板缺少状态快照身份对账；修复后 `make format-check`、`make lint`、111 个源码文件 mypy、完整 452 项 pytest、`make package` 和 `git diff --check` 通过，pytest 仅保留既有未知测试模型名 RuntimeWarning。最终 wheel 在仓库外干净虚拟环境安装全部依赖后通过 site-packages 导入、两个 CLI、9 项模板/静态资源和 `pip check`；Firefox 152 在 1280x720、1440x900、1920x1080 三档截图中无白屏、重叠或页面级横向溢出，`dashboard.js?v=8` 契约通过。

- v15 局部回归矩阵、`make format-check`、`make lint`、124 个源码文件 mypy、完整 pytest、sdist/wheel 构建和 `git diff --check` 全部通过；pytest 仅保留既有未知测试模型名 RuntimeWarning。显式性能报告在固定 5500/360 负载、1 次预热和 5 次测量下通过：标准化 P95 142.228ms/800ms、双源合并 P95 910.468ms/1000ms、统一快照 P95 1296.614ms/1500ms，冷热缓存、峰值条目/估算字节和单慢源隔离均通过。最终 wheel 从仓库外目标目录导入，`trader-cli --help`、schema v5 `validate-config`、`pip check` 及模板、4 个 CSS、2 个 JavaScript、2 个 SVG 共 9 项资源通过。真实 Chrome 在 1280x720、1440x900、1920x1080 下均无白屏、资源失败、脚本异常、同级重叠、文字裁切、页面级横向溢出或抽屉越界，三档 v15 明细截图已人工复核。

- 本批通过 `make format-check`、`make lint`、111 个源码文件 mypy、完整 pytest、`make package` 和 `git diff --check`；架构 AST、`create_app()` 无副作用、固定融合 83.40、预算并发/重试、冻结恢复/哈希、SSE 游标与慢客户端均在完整套件内通过。最终 wheel 从仓库外 `/tmp` 目标目录导入，`trader-cli --help`、`validate-config`、`pip check` 及模板、3 个拆分 CSS、2 个 JavaScript、2 个 SVG 均通过。真实 Chrome 在 1280x720、1440x900、1920x1080 下加载全部 CSS，无白屏、脚本异常、关键同级重叠、文字裁切或页面级横向溢出，三张截图已人工复核。

- 本批 `docs/hi.md` 可执行计划通过 `markdownlint` 和 `git diff --check`；`make format-check`、`make lint`、111 个源码文件 mypy、完整 pytest、sdist/wheel 均通过，pytest 仅保留既有未知测试模型名 RuntimeWarning。最终 wheel 以 `--target` 安装到仓库外 `/tmp` 后从隔离路径导入，`trader-cli --help`、`validate-config`、模板、CSS、两个 JavaScript、两个 SVG 和当前环境 `pip check` 均通过。本批无活动 UI、API 或运行逻辑变化，未重复桌面截图。
- 本批文档验证：`markdownlint docs/hi.md`、`git diff --check -- docs/hi.md CHANGELOG.md` 和 `make package` 通过。`make format-check`、`make lint`、`make type-check` 受到本批开始前工作树中代码拆分改动的既有格式、导入和类型错误阻断；全量 `make test` 仅有既有 `tests/contract/test_v2_app_factory.py::test_dashboard_uses_packaged_v2_assets` 因拆分后的 CSS 未包含 `.runtime-error` 的失败。本批未修改这些实现或测试文件。
- 本批失败先行回归已复现连接离开上下文后仍可用及预算库异常导致 `/api/status` 500；修复后预算与共享快照连接在正常和异常路径均报告已关闭，模拟 `sqlite3.OperationalError` 时状态接口返回 200 与 `budget_ledger_unavailable`。`make format-check`、`make lint`、77 个源码文件 mypy、420 个 pytest、sdist/wheel 均通过；仓库外隔离目录安装最终 wheel 后，包来源、首页 200、6 项 Web 资源、`trader-cli --help` 和 `pip check` 通过。1280x720 无头 Firefox 默认配置被已有无响应实例拒绝，隔离 profile 超过两分钟仍未生成截图并已安全终止；1440x900、1920x1080 因同一宿主浏览器阻断未重复运行，本批未把三档桌面门禁记录为通过。
- 项目级 Review 回归覆盖 DeepSeek 审计字段不影响动作/排序、跨日显式 stale fallback、`full_market` 执行前/执行中超时、候选池/特征/history cache 迟到隔离、唯一组合根和 JSON/SQLite 共享单 persistence worker；`make format-check`、`make lint`、77 个源码文件 mypy、413 个 pytest 与 sdist/wheel 构建全部通过。最终 wheel 在全新仓库外虚拟环境安装全部依赖后，`pip check`、site-packages 导入、`trader-cli --help`、`trader-server --help` 及模板、CSS、JavaScript、Lucide 图标和产品图标资源验收通过。本批修复未修改 Web 资源；本地临时页面返回 200，三档截图因宿主 Firefox SWGL 无法映射 framebuffer 未生成，未将环境失败记为视觉通过。
- 评分链路回归证据：本批在 `tests/unit/domain/test_strategies.py` 与 `tests/unit/application/test_recommendations.py` 增补 `local_strategy_weights` 覆盖注入与推荐快照字段持久化回归；`local_strategy_weights` 变更后的本地评分通道与推荐排序行为已在代码层落地，验收建议在完整门禁前补跑 `make format-check && make lint && make type-check && make test && make package`。
- 本次 P12 落盘统一提交新增/更新了 3 个组件回归测试，覆盖 `news` raw payload 落盘、缓存命中回放与过期降级。标准化收敛新增 `tests/unit/test_v2_market_data_normalize.py`，覆盖 `to_float` 的空值/非有限值分支、`normalize_quotes` 的生成器输入兼容、`None` 过滤与字段转换边界。当前未执行全局 `make quality` 门禁；如需验收请补充 `make format-check`、`make lint`、mypy、pytest 及 `make package` 验证（包含仓库外 wheel 安装与资源读取）。
- 第 26 节 Review 复算 today/tomorrow/d25 三组候选权重和三组本地评分权重均精确为 100%，并逐项核对 v9/v10 状态隔离、互斥因子无生产方向授权、三板总体、上市日龄、流动性回退、可靠度、集中度、83.40 融合向量及 DeepSeek 158/188 边界。`make format-check`、`make lint`、67 个源码文件 mypy、336 个 pytest、sdist/wheel 和 `git diff --check` 均通过；仓库外目标目录强制安装最终 wheel 后，包从隔离路径导入，`trader-cli --help`/`validate-config` 和模板、CSS、两个 JavaScript、两个 SVG 共 6 项资源验收通过。本批无活动 UI 变化，三档桌面视觉验收沿用既有通过基线。
- 通过 GitHub 官方仓库页逐项核对当前 Star 和项目定位：新增 OpenBB 70.7K、NautilusTrader 24.8K、FinGPT 20.9K、LEAN 20.6K，并确认从 Qbot 18.1K 到 QuantsPlaybook 5.6K 的十个被移除项目均低于 20K；复算 15+68+35+30+10=158、挑战者 6+6+5=17、预算桶上限总和仍为 188。`make format-check`、`make lint`、67 个源码文件 mypy、336 个 pytest、sdist/wheel、`git diff --check` 均通过；仓库外目标目录强制安装最终 wheel 后，包从隔离路径导入，`trader-cli --help`/`validate-config` 和模板、CSS、两个 JavaScript、两个 SVG 共 6 项资源验收通过。
- 对照 DeepSeek 官方 V4 迁移、思考模式、上下文缓存和 JSON Output 文档核对模型名、参数与错误边界；逐项复算阶段目标为 144、预算桶总和为 188，并检查挑战者目标包含在原策略桶上限内。`make format-check`、`make lint`、67 个源码文件 mypy、完整 pytest、sdist/wheel、`git diff --check` 和仓库外 wheel 导入/CLI/6 项包资源验收通过。
- 通过 GitHub 官方仓库页面、可用 REST 结果和本地 Git 历史核对 17 个项目的 canonical 链接、DeepSeek/A 股能力及借鉴边界；确认 12 个策略方法引用出自首次提交 `841355c` 且该章节后续被删除。`make format-check`、`make lint`、67 个源码文件 mypy、完整 pytest、sdist/wheel 构建和 `git diff --check` 通过。仓库外虚拟环境安装最终 wheel 后 `pip check`、`trader-cli --help`、包来源和模板/CSS/两个 JavaScript/两个 SVG 共 6 项资源验收通过。
- 第 19-23 节新增回归覆盖当前/历史/fallback 精确身份、跨日 ETag、历史当前行情叠加、400/404 请求上下文、静态资源与抽屉字段、来源计划/成功/失败/P50/P95、DeepSeek success/failed/abandoned/429/超时/token 审计、持久化健康与冻结哈希重启查询，以及每策略候选/过滤/耗时/TopK/版本/veto 状态。`make format-check`、Ruff、67 个源码文件 mypy、336 个 pytest、sdist/wheel 和 `git diff --check` 通过；仓库外 Python 3.11 环境强制安装最终 wheel 后依赖一致、包从 site-packages 导入、`trader-cli validate-config`/`--help` 正常，模板、CSS、两个 JavaScript 和两个 SVG 共 6 项资源可读。无头 Chrome 在 1280x720、1440x900、1920x1080 均无白屏、页面级横向溢出、区块重叠、图片失败或非预期脚本异常，未启动 publisher 时 SSE 503 按预期回退。
- 第 14-16 节回归覆盖五维 schema/证据子集、未知维度和 0.50 覆盖回退、无新闻候选调用、429/超时/非重试 4xx、schema 修复、部分返回、迟到隔离、两级缓存、价格 1%/量比 0.3 边界、六桶/十阶段/emergency 并发预算及重启恢复；Ruff format/lint、67 个源码文件 mypy、329 个 pytest、sdist/wheel 和 `git diff --check` 通过。最终 wheel 在仓库外 `--target` 强制安装后从隔离路径导入，`pip check`、`trader-cli validate-config`/`--help` 与模板、CSS、两个 JavaScript、SVG 共 5 项资源通过；无头 Chrome 在 1280x720、1440x900、1920x1080 均无白屏、页面级横向溢出、区块重叠、图片失败或非预期脚本错误，未启动 publisher 时 SSE 503 按预期回退。
- 快照工作流模块重命名回归覆盖新路径存在、旧路径禁止、生产导入和流水线集成；`make format-check`、Ruff lint、67 个源码文件 mypy、319 个 pytest、`make package` 和 wheel 模块清单检查均通过。最终 wheel 在仓库外 Python 3.11 venv 强制重装后，`pip check`、新模块导入、旧模块缺失、`trader-cli validate-config` 及模板/CSS/两个 JavaScript/SVG 共 5 项资源验收通过；无头 Chrome 在 1280x720、1440x900、1920x1080 均无白屏、页面级横向溢出、区块重叠、图片失败或非预期脚本异常，未启动 publisher 时 SSE 503 按预期回退。
- 第 4-7 节统一回归覆盖完整事件/CAS/重放与有界 worker 生命周期，虚拟交易日每类 cadence 精确次数、周期错过不补跑、关键单点延迟及重启恢复、同任务在途跳过，TopK 草稿/冻结 overlay、全市场 single-flight、熔断半开恢复、候选与分钟乱序拒绝、失败保留最近有效数据、时效 2 倍/3 倍边界、SSE 与 today 发布延迟，以及状态读取不触发日历 I/O；`make format-check`、Ruff lint、67 个源码文件 mypy、318 个 pytest、`make package` 和 `git diff --check` 均通过。最终 wheel 在仓库外 Python 3.11 venv 安装全部声明依赖后 `pip check` 无 broken requirements，`trader-cli validate-config`/`--help` 正常，模板、CSS、两个 JavaScript 和 SVG 共 5 项资源可读；无头 Chrome 在 1280x720、1440x900、1920x1080 均无白屏、页面级横向溢出、区块重叠、图片失败或非预期脚本异常，未启动 publisher 时 SSE 503 按预期回退。
- 第 13 节回归覆盖 d25 15/30、40/60 精确边界及线性中点，long 3/6/9/12 月年化、估值/成长/质量/行业政策/风险保护公式，质押 10/20/35 与解禁 1/5/10 精确分级，财务公告点时过滤、成功空源、单来源失败、畸形/越界输入、多语义证据、证据上限、双模式缓存、配置缺失/漂移/关键词重叠、输入版本、缺失降级及确定性回放；受控真实请求确认 600036 财务、57 条有效公告、质押和解禁点时源均可解析且无结构化来源错误，未保存完整外部载荷。完整门禁为 63 个源码文件 mypy、Ruff format/lint、265 个 pytest、sdist/wheel 和 `git diff --check`；仓库外 Python 3.11 环境安装全部声明依赖并强制重装最终 wheel 后，`pip check`、site-packages 包及新领域模块导入、`trader-cli validate-config`/`--help`、首页和模板/CSS/两个 JavaScript/SVG 资源均通过。Headless Chrome 在 1280x720、1440x900、1920x1080 均无白屏、页面级横向溢出、关键区重叠、图片失败或非预期脚本异常；未启动 publisher 时 SSE 503 按预期回退。
- 第 12 节 134 项章节回归覆盖 tomorrow 六组件/全部子权重、收益/量比/收盘位置 0/50/100 端点、源时间晚于观察时点/接收时间越界/跨日/非交易时段/重复/缺口/午休/非法与非有限数据、未复权分钟端点和直连超时、候选限定、d25/long 字段隔离、批次截止、缓存容量、四项输入完整性、缺失降级、健康覆盖、配置与登记一致性、输入版本、prompt 证据子集、API/冻结往返和确定性回放。完整门禁为 62 个源码文件 mypy、Ruff format/lint、238 个 pytest、sdist/wheel 和 `git diff --check`；仓库外 Python 3.11 环境安装 wheel 及全部声明依赖后，`pip check`、site-packages 导入、新尾盘领域模块、`trader-cli validate-config`、首页和模板/CSS/两个 JavaScript/SVG 资源通过。Headless Chrome 在 1280x720、1440x900、1920x1080 均无白屏、页面级横向溢出、关键区重叠、图片失败或脚本异常，并保存三档截图；未启动 publisher 时 SSE 503 按预期回退。
- 对照检查 `docs/need.md` 第 1、5、8-17、23、25 节，v1 的 `strategy_and_prediction.md`、`money.md`、`software_design.md`、`plan.md` 和 `config/runtime.json`，并抽查当前 `config/v2/strategy.json`、领域过滤/排名实现及对应单元测试；确认旧口径 75/25 融合、Top5 和模拟退出链与当前 68/32、Top10、双冻结及只读研究边界的差异均有原文或配置证据。
- 第 11 节回归覆盖 today 五组件及全部子权重、正/负/中性关键词多数、重复证据、1/72 小时边界、未来/无效/过期时间拒绝、有效新闻截断顺序、候选缓存、来源失败 `null` 降级、配置缺失/重叠/固定值/版本哈希，以及新闻证据与派生值的冻结 JSON 往返和确定性回放；Ruff format/lint、60 个源文件 mypy、191 个 pytest 和 sdist/wheel 构建通过。仓库外 Python 3.11 环境安装 wheel 及全部声明依赖后，`pip check`、包导入、`trader-cli validate-config`、首页和模板/CSS/两个 JavaScript/SVG 资源通过；headless Chrome 在 1280x720、1440x900、1920x1080 均无白屏、页面级横向溢出、关键纵向重叠或脚本异常，并完成三档截图捕获。
- 第 10 节回归覆盖带宽公式边界/非法参数/非有限输入、配置风险触发边界、缺失值、策略适用范围、稳定事实 ID、实际值/阈值/来源/时间、证据 TTL、本地 veto、互斥去重、独立叠加、25 分封顶、配置 schema、恶意身份字段、桌面渲染文本和因子登记约束；Ruff format/lint、59 个源文件 mypy、182 个 pytest、sdist/wheel 构建及仓库外真实依赖安装、包导入、五项资源和 `trader-cli` 验收通过。
- 第 9 节回归覆盖成交额历史缺失、NaN、Infinity、49999999/50000000 边界，报价年龄和未来时间、OHLC 矛盾、跨源偏差复核、逐股过滤明细冻结往返、旧快照兼容、Top120 计数及离线重放；Ruff format/lint、59 个源文件 mypy、173 个 pytest、sdist/wheel 构建及仓库外 wheel 导入、五项资源和 `trader-cli` 验收通过。
- 第 8 节回归覆盖有界行业分层历史预热、冷启动加载、缓存过期重载、部分失败覆盖率、批次版本横截面隔离、宽度缺失分母、极值截尾、并列平均秩、NaN、单样本、冻结统计往返、完整因子登记及删除登记启动失败；Ruff format/lint、59 个源文件 mypy、162 个 pytest、sdist/wheel 构建及仓库外 wheel 导入、五项资源和 `trader-cli` 验收通过。
- 第 18 节回归覆盖 today 20 秒与 tomorrow/d25 30 秒精确边界、混合超龄/未来报价拒绝、配置/schema/锚点 manifest、两阶段版本复核、staged/committed 损坏隔离与上一冻结回退、跨交易日 stale 身份、overlay 持久化/哈希不变/迟到拒绝/源失败保留/15:00 收盘固化、SSE 和组合 ETag；Ruff format/lint、59 个源文件 mypy、154 个 pytest、sdist/wheel 和仓库外 SQLite v2/资源/CLI 验收通过。Headless Firefox 在 1280x720、1440x900、1920x1080 均完整加载 `dashboard.js?v=5`，页面级横向溢出为 false。
- 第 17 节回归覆盖 09:30、09:35:59、09:36 动作边界、主/降级窗口门槛、跨策略阶段拒绝、TopK 0-18 上界、最低观察分、0 推荐、行业限制、稳定排序、完整候选阈值报告及混合版本拒绝；Ruff format/lint、59 个源文件 mypy、138 个 pytest、sdist/wheel 构建均通过，仓库外安装后可导入 `trader`、读取五项 Web 资源并执行 `trader-cli threshold-report --help`。
- `docs/issues/2026-07-17.md` 已登记 16 项 `need.md` 符合性缺口，每项包含需求条款、证据与影响、修复步骤、验收条件、交付章节和状态；交付契约测试约束完整编号及必备字段。Ruff format/lint、58 个源文件 mypy、126 个 pytest、sdist/wheel 构建及仓库外包资源和 `trader-cli` 验收通过。
- 错过窗口补冻结、截止后冻结优先/草稿拒绝、策略身份及 30 秒缓存回归通过；Ruff format/lint、58 个源文件 mypy、125 个 pytest、sdist/wheel 和仓库外安装通过。重启真实服务后，today 因无截止前草稿明确返回 `not_ready`，tomorrow/d25 以不同冻结 ID 和分数连续稳定响应，页面加载 `dashboard.js?v=4`；Firefox 在 1280x720、1440x900 和 1920x1080 下切换 d25 正常且无页面级横向溢出。
- 今日 Bug 记录逐项包含用户问题、现状判断、修改说明、状态和后续验收，并明确未保存 DeepSeek 密钥或完整外部载荷；Ruff format/lint、58 个源文件 mypy、121 个 pytest、sdist/wheel 构建及仓库外 CLI/包资源验收全部通过。
- 交付契约测试校验 `AGENTS.md` 与 `docs/need.md` 均强制记录问题、修改、验证和风险；Ruff format/lint、58 个源文件 mypy、121 个 pytest、sdist/wheel 构建及仓库外 `trader-cli`/包资源验收全部通过。
- 推荐缺失原因与静态渲染契约测试通过；Ruff format/lint、58 个源文件 mypy、120 个 pytest、sdist/wheel 构建全部通过，仓库外安装后可导入包、执行 `trader-cli` 并读取模板、CSS、JavaScript 和图标。
- Web 资源契约校验三策略预取、策略/日期缓存、同键在途请求合并、日期与推荐并行加载及 `dashboard.js?v=3` 缓存失效版本。
- 组件回归覆盖东方财富三个 host 首轮断连后恢复，以及新浪单页首次 504 后恢复，确认重试次数有界且保留显式直连与 timeout。
- 组件回归覆盖 AKShare 新闻 JSONP 规范化、显式 timeout/直连参数、候选缓存复用和新闻源失败降级。
- 风险融合回归覆盖模型 veto 无效、本地监管规则有效 veto、错误证据类型和过期证据拒绝，以及策略 v3 配置字段解析。
- 第 25 节集成回归覆盖冻结输入 JSON 往返与确定性复算、有效配置和候选触发非零物理 DeepSeek 请求，以及 TopK P95 超过 10 秒时的显式失败状态。
- 本批次完整格式、Ruff、mypy、pytest、sdist/wheel 门禁通过；仓库外强制重装后可导入 `trader`、执行 `trader-cli` 与 `verify-freeze --help` 并读取全部 Web 资源；`run.sh` 实际状态返回 TopK P95 和 DeepSeek 零调用原因，headless Firefox 三档桌面窗口均完整加载且无页面级横向溢出。
- 第 24 节完整日影子测试使用相同固定输入运行两次，验证三个策略 committed 冻结、long 仅展示、四策略发布和跨运行 JSON 哈希一致；迁移矩阵逐项关联 24.1-24.9 的现有门禁。
- 新增交付契约测试，校验 `AGENTS.md` 与 `docs/need.md` 对“继续”整节交付、章节内全部子项和相邻章节边界的语义一致，并排除旧的最小任务措辞。
- 宿主机实测东方财富和新浪经系统代理均触发 TLS EOF、直连均返回 HTTP 200；组件测试覆盖东方财富全市场/历史、新浪全市场和腾讯定向报价的显式直连参数。
- 新增双行情源同时失败的网关契约，以及刷新失败后沿用既有候选、继续本地推荐、记录降级状态且不输出 traceback 的流水线回归覆盖。
- 对 `AGENTS.md` 与 `docs/need.md` 的单任务交付规则执行一致性 Review，覆盖任务边界、Review 基线、审查与交付状态、提交粒度、推送失败重试和成功后停止条件。
- `make quality`：Ruff format/lint、58 个源文件 mypy 和 106 个 pytest 测试全部通过。
- `make package`：从干净生成目录成功构建 sdist 和 `py3-none-any` wheel；sdist 不包含旧包或旧测试。
- 仓库外 `/tmp` 虚拟环境覆盖安装 wheel 后，`trader.__file__` 位于 site-packages，CLI 配置校验、首页和进程锁导入通过。
- wheel 内模板、CSS、两个 JavaScript 和 SVG 均可通过包资源读取，`create_app().test_client().get('/')` 返回 200。
- 无界面 Chrome 在 1280x720、1440x900、1920x1080 下均渲染 3 行 fixture，页面无横向溢出，抽屉在视口内且无脚本异常。
- 浏览器竞态测试通过：延迟 today 响应后立即切换 tomorrow，迟到响应未覆盖当前 Tab。
- `./run.sh validate-config`、架构 AST、无副作用 app factory、冻结恢复、预算并发和 SSE 慢客户端契约均已纳入门禁。
- 本批性能/缓存/实时性计划通过 `markdownlint docs/hi.md` 和
  `git diff --check -- docs/hi.md CHANGELOG.md`；当前工作树的 `make format-check`、
  `make lint`、111个源码文件mypy、完整452项pytest和 `make package` 均通过；pytest
  仅保留既有未知测试模型名RuntimeWarning。最终wheel安装到仓库外 `/tmp` 后可隔离
  导入，两个CLI、配置校验、`pip check` 和模板/4个CSS/2个JavaScript/2个SVG共9项
  资源通过。本批未改活动UI、API或运行逻辑，未重复三档桌面截图。

### Residual Risks

- 顶部两栏固定高度意味着超长状态必须在栏内滚动，避免页面跳动是本次明确取舍；手机和
  平板仍不属于产品范围。当前任务开始前已有的 `src/trader/application/recommendations.py`
  未提交修改不符合 Ruff format，导致全仓 `make format-check` 仍会在该无关文件失败；
  同批既有 `tests/integration/test_v2_pipeline.py::test_after_close_waits_for_reliable_board_features`
  与对应应用修改当前也存在预期不一致，导致不排除该节点的 `make test` 失败。本批 Python
  文件的独立 format-check/Ruff、其余测试、lint、mypy、package 和 wheel 验收通过，且未
  改写上述用户变更。

- Web 荐股展示批次：没有已知未解决代码问题。真实交易日中“某短线策略存在所选历史日期、
  另一策略缺失”的具体归档组合取决于用户运行库；本批用纯状态机、日期/API 契约和
  `snapshot_not_found` 回归覆盖客户端行为，验收过程未修改或伪造用户冻结历史。首次
  仓库外依赖全量复制因临时目录磁盘配额不足中止，已删除该临时目录；随后使用已通过
  工程门禁的开发依赖作为只读依赖路径完成 wheel 本体、资源、CLI、配置和 `pip check`
  验收。

- 2026-07-23 已在本批修复前提交的三份 0 只 `close_fallback` 属于不可变正式记录，按冻结
  契约不能删除、覆盖或用迟到预热结果改写；本批保证旧历史恢复可查，并阻止后续冷启动在
  三板历史未就绪时再次固化同类半成品，但不承诺每日必有推荐或放宽既有收益/风险门槛。

- 单入口只消除人工选态，不改变外部行情、DeepSeek、冻结或收盘补算的可用性；供应商持续
  失败时页面仍会展示最近同日有效草稿及降级状态，不能据此承诺数据始终实时或一定产生
  正式推荐。显式 `official|live` 仍属于兼容 API，普通页面不再暴露对应按钮。

- 本批恢复数据就绪和展示链路，不保证每日一定产生推荐或提高收益；候选仍必须通过既有
  硬过滤、每板 100 样本、可靠度 0.85、风险和动作门槛。外部行情源持续失败时仍会明确
  降级并保留最近有效快照。2026-07-23 15:00 在修复前固化的空 `close_fallback` 按冻结
  契约不可覆盖；新热缓存需完成一次预热写入后才能为后续重启提供即时复用。

- T1 的职责是建立可信测量而非提前优化。固定 Python 3.14.4 全链路报告中，标准化
  148.936ms 通过，但两来源融合 701.392ms、canonical 1620.766ms、targeted overlay
  commit 910.829ms 分别超过 600/900/100ms；其余评分、推荐、P6/SSE 和 API 指标通过。
  这三项继续作为 T2/T3 发布阻断项，TopK 来源年龄仍需真实交易日外部供应商观测。

- 本批只归并和删除文档，不改变生产评分或请求逻辑，也不构成收益改善证据。历史
  `docs/reports/` 为保持审计原貌仍会提到已删除计划的旧路径；活动规则只能从两份
  权威文档读取。四份旧计划内容仍可从 Git 历史恢复，后续收益/性能实验分别以 `strage.md`
  和 `times.md` 的独立门禁推进。

- 本批是工程等价重构，未使用真实供应商或 DeepSeek 网络证明外部时延，也未改变收益策略；
  宿主只实际运行 Python 3.14 与 Firefox 152，Python 3.10-3.13 由 Ruff/mypy/打包契约覆盖。
  Firefox 仍输出宿主 SWGL 与浏览器实验告警，但页面级错误捕获、DOM、SSE 和三档布局均
  通过；用户并行的 `docs/name.md`、`docs/times.md` 及其 CHANGELOG/交付契约修改未纳入
  本任务提交。

- `docs/times.md` 只记录审查结论和实施门禁，T1-T5 尚未实现；供应商真实网络时延、
  Session 复用线程安全、dirty 全量等价和 `local_pending` 用户语义必须在对应批次验证，
  固定离线性能结果不证明真实投资收益。

- 当前运行样本仍不足以证明任何规则能提高荐股收益：审查时 5,548 只股票中 5,349 只缺少
  20 日流动性历史，v2 仅有 5 个冻结快照。风险分层、热度、流动性、形态、候选和 TopK
  方案必须在至少 60 个交易日、300 条有效配对、净超额收益差 95% 置信下界为正且严重
  回撤率下降后，才能另立版本晋级。

- G5 没有已知未解决的仓库内缺陷。B4 首轮与重跑再次证明相对 CPU 指标受共享宿主调度
  影响，发布判断必须同时保留固定身份、业务哈希、绝对预算和重复测量上下文。本机仍只
  实跑 Python 3.14.4；真实行情/DeepSeek 时延、其他宿主、Firefox SWGL warning、fixture
  model warning 和前瞻收益证明继续作为外部或独立后续风险，不因 G5 发布而消失。

- A5 没有已知未解决的仓库内缺陷。固定离线证据只在当前宿主实际运行 Python 3.14.4，
  不证明 Python 3.10-3.13、不同宿主、真实行情/DeepSeek 时延或推荐收益；完整实时
  columnar 窄路径当前只覆盖 Eastmoney/Sina，其他输入保守降级 scalar。Firefox 的 SWGL
  warning 和 fixture model catalog warning 均未形成测试失败。是否收紧 384 MiB 必须另立
  任务收集真实峰值分布，不能把当前余量直接转换成缓存或业务容量。

- D5 没有已知未解决的 D-owned 缺陷。Firefox 的 SWGL framebuffer warning 属宿主日志，
  三档 DOM、WebDriver、SSE 和页面 JavaScript 均成功；真实外部网络时延、Python
  3.10-3.13 本机矩阵及推荐收益证明沿用 G4 已记录的外部或延期风险，不在本批伪称已验证。

- G4 已发布且 A5 尚未开始。共享宿主的 B4 三次预跑有绝对时延抖动，最终普通优先级固定
  样本通过，所有样本的业务哈希和内存一致；因此结果只证明当前固定离线负载，不代表真实
  供应商或 DeepSeek 时延。本机只实际运行 Python 3.14.4，3.10-3.13 仅有 Ruff/mypy/wheel
  metadata 静态兼容证据。Firefox 的 SWGL warning 属宿主日志，三档截图、DOM、WebDriver
  和页面 JavaScript 均成功。复验期间出现的并行 B5/D5 工作树与暂存修改已保留；G4
  使用 start HEAD 叠加本批 4 个文件的仓库外树复验和提交，不混入这些并行修改。

- B5 最终固定身份性能复跑通过，但同一共享宿主的两次预跑曾因调度/频率抖动未达到相对
  20% 门槛；业务哈希与内存始终一致，A4 已有 `35.544%` 集成证据。发布复验应保留固定
  fixture/身份并记录所有结果，不把单次计时泛化。优化路径仍只覆盖完整 Eastmoney/Sina
  全市场行，其他合法形态继续使用 scalar；本机只实际运行 Python 3.14.4，真实供应商时延和
  3.10-3.13 本机矩阵仍是外部风险，性能证据不代表投资收益提高。

- B4 快路径按设计只覆盖完整 canonical provider 行；新浪缺失字段、reference/Tencent overlay
  与 degraded payload 继续走已复验的 scalar 路径，因此不应把所有输入都宣称为 35.544% 改善。

- A4.1-A4.6 已完成且四方阶段 4 handoff 均为 `ready_for_gate=yes`；G4 尚未在本批发布，A5
  尚未开始。宿主只安装 Python 3.14.4；Ruff/mypy/wheel metadata 静态覆盖 3.10-3.14，但
  3.10-3.13 没有本机实际运行证据。Firefox 的 SWGL framebuffer warning 仍是宿主警告，D4
  三档截图、DOM、WebDriver 与页面 JavaScript 均成功。

- G2 已发布但 A3 未开始；下一批才能按计划进入 A3 集成。当前工作树仍有 B/C/D 未暂存实现
  改动，本批只归档门禁发布判断，不解决其内部实现或全局质量失败。

- A2 公共骨架已可用且 G2 已发布，但生产默认仍不得接入真实 B/C/D 实现；需下一批 A3 按
  B -> C -> D 顺序集成和复验。当前工作树还存在其他未暂存生产改动和 B/C/D 测试文件，本批
  保留且不纳入 A 侧提交。

- G2 已发布但 A3 尚未开始；阶段 3 必须继续按 A/B/C/D owner 范围集成。全局 `make lint`
  的严格债务计数漂移和全局 `make test` 的既有失败仍未在本批修复，不能宣称完整质量门禁绿色。

- A2 已实现公共类型、配置内存双字段和测试替身；完整 RSS/USS/Polars 原生估算、真实
  pipeline 100 tick 和 P6 发布峰值仍需阶段 2-4 集成门禁补齐。

- Polars 只改变基础设施层批次与变更集合，不改变领域评分、68/32 融合、风险、动作、排名
  或冻结哈希；性能通过也不代表荐股收益提高。真实供应商、真实 DeepSeek 和真实交易日仍受
  外部网络与数据质量影响。未跟踪的 `docs/plan_pipeline.md` 属于用户既有文件，本批保留且不
  提交；若其继续违反三份非权威计划的文档治理契约，完整测试需隔离该文件后证明本批。

- 本批未调用真实外部行情或 DeepSeek 服务，供应商权限、实时限流和网络退化仍由现有超时、
  熔断、负缓存及降级契约承担；Tushare 是否支持明确 qfq 输出取决于运行时 SDK 能力，不支持
  时生产链固定使用腾讯 qfq，raw 只保留审计用途。2.5 应用编排重构仍是下一独立章节；本批
  性能门禁只能证明确定性固定负载不退化，不能证明实际荐股收益提升。并发新增且未跟踪的
  `docs/plan_pipeline.md` 不属于本批提交，并会在本地触发现有“仅 5 份活动文档”契约；其归档或
  纳入文档治理须由所属独立批次处理。

- `plan_sudu.md` 仅为待实施计划；活动代码尚未引入 Polars、`MarketChangeSet`、P6 热索引
  或 SSE 差量协议，不能把目标延迟和节省描述为当前能力。Polars 对约 5500 行热路径的
  实际收益、Python 3.10-3.14 wheel 兼容和 256 MiB 内存适配仍须由固定 fixture 证明；
  若失败，生产继续使用现有 scalar 路径。性能优化本身也不是荐股收益提高的样本外证据。

- 2.4 基础设施适配器尚未开始；活动树仍登记 142 项既有严格复杂度/命名债务，后续章节须
  继续单调下降并在 2.6 归零。真实供应商交易日证据不属于本批离线门禁。

- `docs/plan_c.md` 描述的是待实施方案，活动代码仍会为 long 执行主审，权威策略仍保留
  long 预算与旧五维模型；必须按计划先更新权威契约和测试，再分批实现。历史 60 日、
  300 条有效配对和收益晋级验证已经延期，因此现阶段不能声称新方案提高实际荐股收益。
  此外，无关 `application/ports` 并发重构仍需其所属批次闭合导出、格式和债务基线后，
  当前工作树的完整门禁才能恢复通过。

- 工程重构计划 2.3-2.6 仍待后续独立“继续”批次；当前仓库仍登记 145 项应用层和基础设施层
  复杂度、参数数量及异常命名债务，本章没有越界处理。真实行情源、真实 DeepSeek 尾延迟和
  长期收益不由等价领域重构证明；固定性能数据受宿主负载影响，需继续按同一 fixture 复测。

- 2.1 完成时严格基线登记 163 项既有复杂度、参数数量和异常命名债务；该章只建立可执行棘轮，
  没有越界进入领域、应用或基础设施重构。后续每个完整章节必须降低并同步基线，最终章节
  前不得宣称全工程重构完成。真实行情、真实 DeepSeek 和长期收益仍不由本次基线修复证明。

- v17 的目标是减少结构性大回撤，当前自动门禁只能证明实现、确定性和审计正确性，不能证明
  实盘收益已经提高。既有冻结行没有 ATR20 时不会猜测回填结果；历史 qfq 与实时未复权锚点
  的公司行为一致性、v16/v17 同期影子和配对自举仍是后续独立批次。在至少 60 个交易日、
  300 条有效配对样本且净超额置信下界和严重回撤率同时达标前，不得宣传收益改善；必要输入
  缺失时正式推荐数量可能下降，这是有意的 fail-closed 行为。

- 计划中的复权修正、P1-P6 热路径、全市场候选概要和同期影子验证均尚未实施；性能数据是
  单机固定负载测量，真实行情尾延迟仍需持续观测。在至少 60 个交易日、300 条有效配对
  样本及预设置信门禁完成前，不能把该计划表述为已提高荐股收益。

- 组合重构把原先单个共享锁拆为六个组件锁，锁边界与生命周期已有组件回归、全量测试和
  固定负载覆盖，但真实交易日仍需继续观察来源尾延迟与健康计数。v15 性能脚本在宿主并行
  高负载时出现过一次合并/快照 P95 抖动越线，固定哈希未变化，空闲复测通过；这属于仍需
  持续运行门禁的环境敏感风险。本批保持 `MarketDataPort` 行为只是端口兼容，不代表内部
  保留旧聚合实现；旧 mixin 和共享状态类已完全删除。三板 CPU 评分改为单线程是下一
  个独立交付批次，尚未混入本提交。

- P95 数值受宿主负载和解释器版本影响，后续仍须以固定录制负载和现有预算作为回归门禁；
  有界元数据缓存只覆盖稳定、低基数的来源名和 dataclass 类型，不缓存行情内容。下一计划项
  （历史预热路径）尚未在本批修改；实际行情源延迟和三档桌面渲染行为亦不受本次后端纯计算
  优化影响。

- 收盘冷启动重建依赖行情适配器在同一交易日提供 14:59 后的三板完整收盘批次；供应商
  延迟、全源故障或历史/研究字段仍在预热时保持 `not_ready` 并退避重试，不会改用上一日
  数据或提交行情不完整的记录。冷启动分支按用户确认不新增 DeepSeek 请求，因此明确为
  `local_degraded/local_only`，与盘中已有融合结果可能不同。

- 800 行是宽松后的工程上限，不代表 501-800 行模块天然合理；职责、耦合和可测试性仍须在
  Review 中独立判断，超过 800 行仍由架构契约拒绝。本批只调整工程门禁，不产生运行、数据
  或兼容迁移风险。当前工作树另有用户并行业务修改，未纳入本批验证副本、暂存或提交。

- 推荐 Web schema v3 是有意的破坏性收缩；仓库外仍读取 v2 原始特征、板块、证据、缺失
  或完整 DeepSeek 审计字段的私有脚本需一次性迁移。领域快照与冻结格式保持不变，可继续
  用于离线审计和问题追溯。

- 1280x720、1440x900、1920x1080 实际截图仍被宿主 Firefox Snap/AppArmor 拒绝启动，
  且机器无 Chromium 备选；静态资源、CSS/JS 契约和语法检查通过，但本批不把三档截图
  记为已通过。全新虚拟环境安装全部大型数据依赖还受到宿主磁盘配额限制，仓库外独立
  wheel 前缀改用当前已验收依赖底座完成资源、入口与 `pip check` 验收。

- P2 恢复仍依赖东方财富/新浪全市场接口至少一个返回当日有效行情；若全源失败，历史
  实时列按契约继续显示 `-` 并在状态中记录 `current quote index recovery degraded`，不会
  用冻结日涨幅伪造今日数据。已运行的旧进程需重启后才会加载新后台任务和前端资源。
  宿主临时盘配额不足以在第二个干净环境重复下载全部第三方依赖；wheel 本体在仓库外安装，
  并复用当前已验依赖完成上述导入、资源、CLI 与 `pip check` 验收。

- 2026-07-22 的 today 在 11:20 后才启动，tomorrow/d25 的旧进程又未在 14:50 前形成
  30 秒内合格检查点，因此三个冻结查询在当日保持 `not_ready`；这是禁止迟到结果改写
  冻结记录的预期保护，不能用 14:41 草稿或重启后的行情补造。队列修复只能从下一有效
  交易窗口产生新冻结证据；外部来源延迟仍可能使策略合法返回少量或零推荐。

- 本批没有已知剩余风险；仓库内代码、测试、性能 runner、架构契约和历史路径说明已统一
  使用 `infra`。产品 API、命令入口、运行数据、冻结格式与桌面资源均未改变。

- 1 秒/3 秒等配置是最短计划间隔，不保证外部供应商固定延迟；新浪全市场完整分页若耗时
  5-8 秒，实际周期会由在途跳过自动延长，腾讯、东方财富或新浪限流/断连时仍按熔断和最近
  有效值降级。Tushare 120 积分 Token 的实际 `quota_or_rate_limit` 外部风险仍存在，本批
  不把它用于盘中实时行情，也不绕过 11:20/14:50 冻结规则补造已错过的快照。

- 官方权限文档列出的 A 股 `daily` 最低积分为 120，但当前 Token 的真实接口响应仍拒绝
  该请求并被脱敏归类为 `permission_denied`；运行时已自动使用腾讯/东方财富历史回退，
  仍应在 Tushare 控制台核验账户积分和接口授权。服务在 today 11:20 冻结后才重启，不能
  补造当日 today 快照；tomorrow/d25 已在 13:00-14:50 活动窗口形成，冻结后迟到结果仍
  不得回写。最终推荐允许少于目标数或为零，候选池非零不代表股票必然通过硬过滤、分数、
  可靠度、动作门和 TopK 集中度约束。

- 2026-07-21 的 today committed 快照实际为 0 条推荐，因此该日期不会凭空出现可填写两列的股票行；2026-07-20 的 tomorrow/d25 和 2026-07-17 历史已有推荐项并已现场验证实时列。最近 20 日以外首次访问仍是受哈希校验保护的冷读，随后进入 60 视图 LRU；完整 v17 P1-P6 分池、冷读 single-flight 和固定性能 CLI 仍按权威文档作为独立原子章节，不在本次缺陷批次内宣称完成。

- 当前无快照时页面会按用户要求保持空状态；这不会修复上游快照未发布本身。现场观测到当日规范行情已有 5,548 行、但全市场事件连续过期且 `snapshots_published=0`，属于独立流水线上游问题，后续仍需按事件 deadline、历史覆盖与候选形成链排查。本批只保证不再用昨日结果掩盖该状态。

- 本批是文档信息架构重整，没有运行 UI 或业务行为变化，因此不重复桌面截图和真实外部行情/DeepSeek 验收。旧文档的逐日过程日志已压缩为决策结论，完整原文仍可从本批基线 Git 历史追溯；v17 P1-P6 工程章节仍未实施，不得因文档合并视为完成。

- “锚点至今”和“今日涨跌”依赖 P2 当日内存行情已成功覆盖对应股票；服务冷启动尚未取得当日行情或行情源降级时，这两列会按契约显示 `-`，不会回退为旧锚点值。外部行情时效仍取决于来源可用性；本批没有修改 Web 资源，三档桌面结论沿用当前资源基线。

- v16 固定 fixture 与本地门禁已覆盖确定性和绝对性能预算，但本机串行墙钟/lane 墙钟 P95 为 0.843，未显示纯 Python 计算加速，因此三 lane 只宣告失败域隔离、有界并发和 1000ms 绝对预算通过。外部行情/DeepSeek 的真实交易日延迟仍取决于网络与供应商；本批不消耗真实 DeepSeek 额度、不宣称收益改善，也不提前实施 `docs/hi.md` 批次三的 v17 P1-P6 发布池与 Web 性能硬化。宿主 Firefox 仍记录 SWGL framebuffer 警告且高分辨率截图完成较慢，但本次三档 PNG 和 DOM 证据均有效；图形栈变化后仍应复跑发布截图。

- 紧急 lane 消除的是已确认的内部 FIFO 饥饿，不保证腾讯或本机网络始终在 3 秒内响应；紧急 worker 正在执行一个慢请求时只允许再等待一个紧急任务，更多并发请求会显式拒绝而不是无限堆积。普通 lane 从 6 个并发执行位调整为 5 个，可能增加全市场、历史或研究尾延迟，完整门禁不能替代真实交易日对 `urgent_*`、TopK 年龄和普通 lane P95 的持续观察。

- 本次代码修复只解决已确认的 D25 固定顺序饥饿；DeepSeek 读取超时属于外部网络或供应商响应问题，已失败物理请求按契约继续占用当日 188 次上限，当前阶段耗尽后只能等待合法后续阶段或下一交易日。tomorrow 尾盘分钟虽已现场恢复满覆盖，上游仍可能再次短暂失败；三类场景都会保留最近有效快照和显式降级，不承诺外部来源始终可用。新一轮 Firefox 截图受宿主 `RenderCompositorSWGL failed mapping default framebuffer` 阻断，桌面结论沿用本批未改动 Web 资源的上一批三档通过基线，不把本次环境失败记为新截图通过。

- 现场已确认服务端当日快照持续更新，但真实 SSE 丢事件时的恢复时延受固定 15 秒状态心跳约束；身份对账只解决页面停留旧快照，不改变行情源覆盖、候选过滤、DeepSeek 失败或当日快照确实尚未生成时的显式昨日 fallback。三档截图覆盖布局与资源加载，但其中后两档拍摄时运行进程已停止，动态实时数据切换仍由失败先行契约、API 契约和当日运行库证据覆盖。

- 本批固定响应覆盖并发、缓存、乱序、截止、降级、冻结兼容和性能预算，但未携带真实 Tushare Token 调用官方 SDK，也未替代真实交易日的上游覆盖率、429/额度、尾延迟和 TopK 年龄观测；Tushare 按候选逐代码慢调用可能耗时，但被独立 lane 隔离且不会阻塞四个免费来源。合并性能在当前机器满足预算但接近 1000/1500ms 上限，仍需真实负载持续观测。当前树已删除执行计划中的明文 Token，但该值曾进入已推送 Git 历史，仍须在供应商控制台吊销并轮换。v16 三板独立评分与统一选择尚未启用，工程可靠性改进不构成收益证明或投资收益承诺。

- 本批使用 mock DeepSeek HTTP 覆盖 V4 模型参数、429、超时、截断/非法 JSON、挑战者合并、缓存和预算，没有消耗真实 API 额度；供应商实际模型可用性、响应字段与网络质量仍需受控真实密钥冒烟。结构化风险源、真实交易日全市场负载和冻结时点仍受外部数据覆盖与尾延迟影响，失败时按契约保留最近有效快照并显式降级。工程门禁与策略一致性不构成收益验证，不能据此声称推荐收益提高。

- v15 五来源 lane、统一缓存、确定性合并和三板身份风险门已进入活动代码；v16 三板评分 lane、候选/评分权重和 78/76 门槛，以及 v17 等价性能硬化仍是后续独立批次。权重来自固定业务选择而非点时收益验证，后续实现通过工程门禁也不能据此宣称实际收益提高。
- 故障注入已覆盖 SQLite 打开失败，但无法在单元测试中制造宿主级文件描述符耗尽而不影响测试进程；两个生产 SQLite 边界的确定关闭契约直接覆盖已确认根因。若网络套接字或第三方库独立泄漏句柄，仍需依赖运行期进程 FD 监控定位；本批不重构为长连接，也不声称消除所有可能的宿主资源耗尽来源。三档桌面截图仍受宿主 Firefox 无响应阻断；本次 JavaScript 变化仅涉及预算不可用文本且静态契约通过，但发布门禁不能以此替代真实三档渲染。
- 本批生产逻辑没有新增 Web 资源差异，桌面布局沿用此前三档通过基线；本轮 headless Firefox 在宿主图形栈报 `RenderCompositorSWGL failed mapping default framebuffer`，1280x720、1440x900、1920x1080 未重新生成截图，发布前如宿主图形环境变化应补跑三档视觉验收。
- 固定时钟与故障注入已覆盖 `full_market` 队列等待和执行中越过截止的确定性行为，但真实交易日全市场并发负载、上游尾延迟和事件积压仍需运行观测；截止事件会明确记为 `expired` 并沿用最近有效快照，不再制造全局“最近错误”，但这不等同于消除上游变慢或机器资源不足。
- P12 缓存回放依赖服务时钟与 `wall_clock` 一致性：若时钟异常偏移导致 TTL 解析偏差，可能误判新鲜度；运行期应监控 `research_cache` 命中率与 `research_data_coverage_ratio`，并配套异常告警。
- DeepSeek 审计信号保持只读，`metadata.shadow_scoring` 只用于离线观察；尚未做收益验证回放，不代表今日/明日/2-5 日真实前瞻可提升。
- 本次标准化收敛已覆盖行情源解析与构建器协议，但未补齐 `RAW_FEATURE_SCHEMA`/`DERIVED_FEATURE_SCHEMA` 与策略维度版本之间的统一映射验证；新增适配器前仍需补充映射校验与回放一致性测试，避免字段漂移带来的特征口径变更。
- 本批新增的路由/迁移回归主要覆盖单元边界，尚未引入高并发全量行情与高频迁移压测；实际部署前仍需在真实运行环境补充并发源故障注入与迁移时长监控证据。
- 第 26 节只固定下一版机制和候选初值，活动代码仍运行 v9；三板隔离、同行差、换手状态、流动性层级和集中度控制具有机制意义，但没有真实前瞻收益证据。六组权重、P50/P80 和 0.85 不代表最优参数，也未定义生产启用条件；任何启用必须另建完整业务契约并独立交付，不能通过当前配置或运行时开关提前生效。
- GitHub Star 会持续变化，20K 筛选只代表 2026-07-19 快照，不能证明项目安全、许可证兼容、A 股点时正确或策略收益；Star 跌破门槛或候选项目升至门槛后不会自动更新。158 是预算利用目标而非收益承诺，实际调用仍受候选、缓存、熔断、截止和降级约束；本批没有改动运行配置或代码，实施时仍需单独交付并验证原子预算和阶段调度。
- 本批次只固化下一版契约，当前 `config/v2/runtime.json` 和活动实现仍使用 `deepseek-chat`、单阶段 schema 与 133 次目标，必须在 2026-07-24 旧别名停用前另行完成实现、回归和受控真实 API 冒烟。挑战者与影子校准尚无真实 A 股效果证据，且本批按用户要求不定义离线验证和晋级条件，因此不能据此宣称收益已经提高或允许校准值进入生产融合。
- 外部仓库的安全、许可证、数据点时正确性和策略收益未经本项目验证，后续若引入其源码或机制，仍须固定 commit、独立审查并通过 A 股点时和样本外门禁。本批次未修改 UI，三档桌面验收沿用既有基线；本地回环临时服务授权被中断，未重复生成无行为变化的截图。
- 第 19-23 节仓库内行为已有固定输入与故障注入证据；第 25 节仍缺真实 A 股完整交易日的 TopK P95/冻结时延、受保护真实密钥产生的非零 DeepSeek 调用与阶段总结。固定输入、仓库外安装和本地桌面检查不能替代这些生产证据，齐全前不得宣告发布完成。
- 第 14-16 节仓库内状态机、预算、缓存和降级行为已有固定响应与故障注入证据，但尚未用受保护的真实 `DEEPSEEK_API_KEY` 重启服务并在 A 股交易时段验证非零物理调用、133 次阶段目标、上游 P95/限流和 schema 分布；这些仍是第 25 节发布阻塞证据，密钥不得写入仓库、日志、快照或进程参数。
- 本批次是内部模块纯重命名，仓库内引用和 wheel 均受门禁覆盖；若仓库外代码绕过公共入口直接导入旧内部路径，将需要改用 `trader.application.snapshot_workflow`，不提供旧名兼容层。
- 第 4-7 节及 v15 已有固定输入、虚拟全日时间线和故障注入证据，但尚未在真实交易日验证五来源共享池的全市场 P95、队列峰值、15 秒停机、TopK 全天 P95、来源熔断恢复和 today 报价到评分发布延迟目标；Python 不能中断已进入第三方 SDK/HTTP 的当前调用，停机会先协作取消后续批次并等待显式 I/O timeout，以无残留线程优先。固定输入门禁不能替代第 25 节真实交易日证据。
- 第 13 节使用录制响应覆盖全部边界，并用 600036 完成单股受控真实结构化请求；尚未在真实交易时段验证 120 只 d25 候选与 10 只 long 名单的整体覆盖率、P95 延迟、上游限流和缓存恢复。来源失败会保持 `null`、中性评分和显式降级，但首次生产运行仍需观察研究源成功率与 `research_data_coverage_ratio`。第 12 节分钟输入同样仍需真实交易日覆盖率与延迟证据。
- 本批次没有真实前瞻交易日、成本后组合结果或 DeepSeek 同池反事实数据，因此不判断 v1/v2 哪套更赚钱。进一步优化应先补充不进入产品运行链的离线点时评估：按交易日组合配对 local/hybrid、计入 T+1/涨跌停/停牌/费用与滑点、逐项预注册并控制多重检验；任何权重、硬过滤、动作门槛、TopK 或市场状态规则变化仍须先更新 `docs/need.md`、提升策略版本并单独交付。
- 当前候选预选的一级 35/25/20/10/10 权重已进入契约，但流动性和短期动量的二级权重及带宽参数仍由领域实现给出；在调整策略收益前，应优先把这部分身份纳入可版本化、可冻结复算的契约，避免代码变化未形成新的策略身份。
- 第 11 节 today 评分输入已闭环；配置化关键词是可审计的保守极性规则，不能理解否定、反讽或复杂语境，首次真实交易日仍需抽样核对标题分类分布。AKShare 线上 JSONP 形态尚未用真实脱敏响应闭环，当前证据仅来自录制响应与失败降级测试。
- 旧 `recommendation_snapshot_v2` 文件若生成时尚未写入 JSON 内 `config_version`，仍以 `legacy-unrecorded` 兼容读取；只有 runtime v3 后的新冻结可提供完整配置版本证据。
- 第 17 节 AUDIT-20260717-02、03，第 18 节 AUDIT-20260717-01、05、06，第 10-16 节评分/DeepSeek 状态与预算，第 4-7 节数据编排，以及 AUDIT-14、AUDIT-15 的仓库内实现均已完成；仍待 AUDIT-07/AUDIT-16 的真实交易日、真实 DeepSeek 进程调用与阶段总结证据。
- 回放算法 v9 不会把旧 v8 及更早冻结输入当作当前规则重新解释；旧快照须由对应旧 release 验证，当前阈值预注册只接受 v9 新冻结快照。
- 2026-07-17 运行目录没有 today 截止前草稿或冻结文件，因此不能合规恢复当日 today 推荐；修复只保证后续冻结和持有截止前 30 秒内有效草稿时的重启补提交。
- 问题归纳的内容完整性仍依赖交付 Review 判断；契约测试只能防止必备栏目和目标文档被删除，不能自动证明原因分析正确。
- 待办状态：AKShare 新闻 JSONP 仍需真实脱敏响应闭环，真实 DeepSeek 进程调用与线程/队列/逐阶段刷新仍按后续独立整节交付；tomorrow 尾盘分钟和 d25/long 结构化输入已完成仓库门禁，但仍需真实交易日覆盖率、延迟和上游限流证据。
- 可复算 latest/frozen 文件会增加本地 JSON 体积和序列化 I/O；全市场部分已裁剪为硬过滤和候选排序必需字段，发布前仍需在真实全市场规模下记录文件大小、冻结耗时和磁盘保留策略。
- 第 25 节仓库门禁可重复执行，但生产最终验收仍需真实交易日证明活动 TopK P95 不超过 10 秒、真实 DeepSeek 密钥产生非零调用并输出阶段总结，以及保存三档桌面截图；任一证据缺失时不得宣告发布完成。
- 本批次风险明细 renderer 契约已通过，且未修改布局 CSS；但宿主 snap Firefox 在重采三档截图时因 `RenderCompositorSWGL` 默认帧缓冲映射失败而无法创建 WebDriver 会话，只能沿用上一批三档无横向溢出基线。正式发布截图仍属于第 25 节阻塞证据。
- 固定输入完整日影子已覆盖冻结链和确定性，但真实 A 股 09:15-15:00 不间断影子观察仍未完成；生产发布前必须按 runbook 留存行情年龄、冻结哈希、桌面三分辨率和 v1 运行库未修改证据。
- 单个章节可能包含较多子项，交付 diff 和 Review 时间会相应增长；仍必须维持一个章节、一个提交、一次推送，并通过章节内逐项证据控制范围。
- 行情直连依赖本机网络可直接访问对应域名；若所在网络强制要求代理，三路实时行情会按既有熔断与最近快照策略显式降级。
- 行情提供方的 TLS 可用性仍由外部网络环境决定；全部来源首次启动即失败且没有内存缓存时不会生成新推荐，只保留仓库中最近有效的只读快照并等待后续刷新恢复。
- 尚未完成一个真实 A 股完整交易日的 v2 影子运行，因此 TopK 报价 P95、冻结点实时时延和阈值分布仍需在生产发布前留证。
- 用户已确认现有 `DEEPSEEK_API_KEY` 有效，但当前运行服务状态为 `configured=false`，说明密钥未注入该进程；密钥有效性不再列为阻塞原因，使用该密钥重启后产生非零真实调用与阶段总结仍是待留存的发布证据。
- 当前 Linux 环境没有 PowerShell，`run.ps1`/`run.bat` 已静态审查，仍需在 Windows PC 实机验证创建虚拟环境、单进程锁和 Ctrl+C 停止。
- 外部行情提供方可能发生字段或限流变化；组件测试使用脱敏固定响应，首次真实运行应观察来源覆盖、熔断和降级状态。
- 本批只完善待执行计划，v15-v17缓存、性能CLI和实时性硬化尚未进入活动实现；256 MiB、
  各路径P95及5%相对退化值是后续验收预算，不是已测得的性能提升。真实交易日上游
  尾延迟、数据覆盖和前瞻收益仍需另行留证，工程性能通过也不得表述为收益提高。
- 本批无界面变化，三档桌面验收沿用此前已通过证据；后续活动UI变化后必须重新实测，
  不能用本批文档验收替代。
