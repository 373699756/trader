# V2 与评分研究多 Codex 总实施计划

状态：V2-E0 至 V2-E11、Score-R0、Score-R1、Score-R1-Migrate、Score-R2、Score-R3、Score-R4、
Score-R5 工程能力已完成；V2 工程发布章节全部闭合。Score-R5-Run 运行证据连续性修复及
Score-R0-Rerun 替代窗口预注册与身份贯通已完成；下一研究章节为 Score-R6，但仍须等待
`score_p0_v2` 取得真实 `promotion_eligible`，不得跳过门禁。

本文是唯一活动施工计划，只定义执行顺序、会话协作、文件所有权、同步 Gate 和退出条件，
不定义产品或策略行为。产品、架构、时间线、API、运维和验收以
`docs/software-business-design.md` 为唯一权威；候选、过滤、评分、风险、DeepSeek、融合和
排名以 `docs/recommendation-strategy.md` 为唯一权威；依赖、构建和入口以 `pyproject.toml`
为唯一权威。

## 1. 合并决策与固定边界

- 最终 release 只保留 V2，不保留旧 URL、旧 schema、旧快照读取、双读、双写或运行时开关。
- V2 工程迁移和评分研究共用 V2 点时数据与原生决策身份，但保持独立状态、存储和发布门禁。
- Score 研究不得建立第二套生产数据平面、评分链、冻结链、Web 或 DeepSeek 请求链。
- V2-only 工程发布不等待 20 日评分前向影子；研究未通过时保持当前生产策略。
- 当前 Score-R1 的紧凑轨迹接缝属于迁移期实现；V2-E3 必须改接通用 V2 committed event，
  V2-E10 必须删除旧 shadow 接点。
- 用户每次发送“继续”或语义等价指令，只交付本文下一个可执行的完整同级章节；不同 lane
  可以在同一 Gate 并行开发，但正式集成、Review、提交和推送严格串行。

## 2. 三会话职责

### 2.1 协调集成会话 C

- 唯一拥有 `feature/tomorrow-v2` 集成分支和推送权限。
- 独占本文、两份权威文档、根 `CHANGELOG.md`、跨 lane 契约测试和 Gate 状态。
- 公布每波唯一 `BASE_SHA`、允许文件范围、接口哈希和固定集成顺序。
- 在临时组合树验证 E/R 补丁；发现语义冲突时退回责任会话，不临场发明业务规则。
- 每个正式章节形成一个 Conventional Commit，推送后核对 `HEAD == @{upstream}`。

### 2.2 V2 工程会话 E

- 分支命名 `codex/v2-g<gate>-<section>`，从 C 公布的 `BASE_SHA` 创建独立 worktree。
- 负责 V2 数据平面、决策核心、调度、冻结、查询、API/SSE/Web、入口和旧链删除。
- 不修改 research 领域、研究仓储、统计方法、挑战者或研究报告。
- 只有章节明确授权时才可修改组合根、配置、依赖或公共跨 lane 端口。

### 2.3 评分研究会话 R

- 分支命名 `codex/score-g<gate>-<section>`，从同一 `BASE_SHA` 创建独立 worktree。
- 研究实现收敛到 `domain/research`、`application/research`、`infra/research` 边界；迁移期
  `tomorrow_research_*` 代码在 V2-E3 后逐步移动到该边界。
- 不修改 V2 核心决策、冻结、Web、入口、组合根或正式配置。
- 不直接接线生产运行时，只实现已冻结 observer、转换器、研究仓储、回放和离线报告。

## 3. 无冲突并行协议

### 3.1 工作树、分支与运行数据隔离

1. C 在 Gate 开始时记录 `BASE_SHA` 和上游，确认集成工作树无未闭合批次。
2. E/R 分别创建全新 worktree；禁止共享工作目录、Git index、虚拟环境写目录或 `.runtime`。
3. 测试运行目录分别使用 `/tmp/trader-e-<gate>` 和 `/tmp/trader-r-<gate>`。
4. 波次进行中禁止 rebase、合并主分支或吸收另一会话提交；主分支变化时当前波作废并重建。
5. Worker 不直接推送集成分支，只推送自己的交接分支。

### 3.2 文件所有权

- C 独占：总计划、权威文档、Changelog、项目文档清单和跨 lane 契约。
- E 独占：market/decision/runtime/web/entrypoint 的活动产品实现与对应测试。
- R 独占：research 类型、端口、仓储、统计、回放、报告与对应测试。
- `bootstrap.py`、`pyproject.toml`、运行配置和公共事件端口属于 Gate 独占文件；每波只能指定
  一个会话修改，另一会话必须通过端口或 factory 交付。
- 交接前运行 `git diff --name-only BASE_SHA...HEAD`；出现非本会话所有文件即拒绝交付。

### 3.3 跨 lane 接口冻结

每个 Gate 必须冻结下一波使用的接口，冻结后该波不得更改：

- `DataPlaneReadPort`：只读不可变点时数据，不暴露具体 SQLite、缓存或供应商对象。
- `V2DecisionCommitted`：策略、交易日、输入与决策身份、点时时间、版本、过滤聚合和决策项。
- `DecisionObserver.offer(event) -> bool`：非阻塞提交；拒绝只影响 observer 自身状态。
- `ResearchDecisionTrace`：引用 V2 决策身份，不重新执行或复制生产评分形成伪基线。

V2 domain 不导入 research；V2 application 只发布通用 committed event。R 把事件转换为研究
轨迹。研究队列满、超限、冲突、关闭或写入异常不得反向影响决策、冻结、API 或 SSE。

### 3.4 交接与串行集成

Worker 交接必须包含：

- `BASE_SHA`、提交哈希、文件清单和接口哈希；
- 逐项需求对应、行为变化和无变化边界；
- 定向测试、Ruff、mypy、完整测试适用性和剩余风险；
- 供 C 写入权威文档与 Changelog 的准确文案。

C 固定执行：

1. 在 `/tmp` 创建 `BASE + E patch + R patch + 治理文档` 的候选树。
2. 先检查所有权和接口哈希，再运行定向测试与完整门禁。
3. 候选树通过后，按 E 后 R 的顺序串行形成正式章节提交。
4. 若出现文本冲突，视为所有权或接口冻结失败，退回后提交会话重做；C 不手工拼接业务代码。
5. 每个正式提交推送并核对上游；Gate 两个 lane 都集成后才公布下一 `BASE_SHA`。

## 4. 当前基线

- V2-E0 已完成：唯一产品契约、`.runtime/v2`、统一 V2 API 目标和无兼容原则已固定。
- V2-E1 已完成：统一 `DataPlaneReadPort`、四类字段血缘 epoch、覆盖门禁、最近有效快照保留及
  主数据/交易日历/历史摘要/风险组件恢复边界已固定。
- V2-E2 至 V2-E7 已完成：统一决策身份、独立调度以及 tomorrow、today、d25、long 的原生
  生产接管已进入组合根；三个评分策略隔离 current、observer、冻结、正式记录和查询，long
  只维护无评分 current projection。
- Score-R0 已完成：最多 60 个评价日、40+20 切分、五挑战者、统计身份和人工晋级已预注册。
- Score-R1 已完成：紧凑研究轨迹、有界异步记录、幂等冲突和硬过滤后逐股审计已实现。
- Score-R1-Migrate 已完成：研究 observer 只消费 V2 committed observation；同批 R1 审计与
  committed event 使用独立 schema/hash 写入有界 SQLite 研究库，重启可恢复，失败不反向影响生产。
- Score-R2 与 Score-R3 已完成：最多 40 日点时提取、active-set 证明、三档成本基线回放、研究指标
  和不可变报告能力已闭合；当前真实历史覆盖不足时只输出 `exploratory`，不形成收益或晋级结论。
- G1 的 Score-R2 接口适配设计已经由后续 R2/R3 完整实现消费：研究侧历史扩展显式继承唯一的
  `DataPlaneReadPort`，日摘要、按代码完整字段、40 日提取、分区、manifest 与基线报告均保持离线隔离。
- G1 两个 worker tip 已合并并推送，远端 worker 分支已退役；G2 开始时必须以本批审计记录
  推送后的最新 `feature/tomorrow-v2` tip 公布统一 `BASE_SHA`，E/R 不得复用 G1 分支或其旧
  worktree 作为新基线。
- E10 已完成旧 Pipeline、snapshot、shadow/cutover、旧 Web、旧仓储和只服务旧链的入口、测试
  与配置物理删除；活动入口只装配 V2 调度、决策记录、统一 API/SSE 和 committed event observer。
- 研究采集只从 V2 committed event observer 接收不可变事件；不再从生产运行时读取旧 snapshot、
  baseline 或 tomorrow shadow 状态。
- 2026-08-14 运行可用性修复已闭合：同一观察点的三条评分策略复用一次全市场/候选报价输入，
  本地投影不再同步等待历史或公司研究补抓；`candidate_pool_size` 恢复为每板上限，刷新失败不再
  级联为无批次构建失败，状态 API 暴露按策略错误与 lane 计数，决策覆盖使用去重后的精确计数。
  该直接缺陷批次不推进 Score 章节，下一研究章节仍为 Score-R6。
- 2026-08-14 决策时间与显示元数据修复已闭合：网络刷新完成晚于调度请求时，三条评分策略按同批
  本地最晚观测/接收时刻构建，不再共同触发 future-feature 拒绝；scored 决策从行情身份保留名称与
  行业并贯通 current、正式历史和 HTTP，旧记录哈希保持兼容。状态 API 新增 `runtime_version` 与
  scheduler 摘要用于识别未重启旧进程。该直接缺陷批次不推进 Score 章节，下一研究章节仍为 Score-R6。

## 5. 同步 Gate 与并行波次

| Gate | V2 工程会话 E | 评分研究会话 R | 集成条件 |
| --- | --- | --- | --- |
| G1 | E1 统一 V2 数据平面 | R2 只做接口适配设计，待 E1 集成后实现历史提取 | 数据与研究 schema 使用稳定内容哈希 |
| G2 | E2 统一决策核心和 committed event | R2 最多 40 日点时提取器 | R 只读 G1 冻结端口，不建立第二套数据平面 |
| G3 | E3 调度、observer 队列和生命周期 | R1-Migrate 将现有轨迹改接 committed event | 写入失败不阻塞，DeepSeek 请求增量为 0 |
| G4 | E4 Tomorrow 正式接管 | R 只做交叉 Review | Tomorrow current、freeze、trace 使用同一 identity |
| G5 | E5 Today 正式接管 | R3 Tomorrow 基线回放与报告 | 报告可复算，硬拒绝无逐股研究数据 |
| G6 | E6 D25 正式接管 | R4 五个 Tomorrow 挑战者 | 挑战者只复用 V2 纯领域函数和已有 facts |
| G7 | E7 Long 正式接管 | R5 历史统计门禁并启动前向影子 | long 不参与评分，历史通过者才可前向运行 |
| G8 | E8 统一 API/SSE/Web | R5 持续采集 | 研究数据不进入普通 API、SSE、Web 或正式历史 |
| G9 | E9 唯一入口与组合根 | R5 持续采集 | 本波只有 E 可修改组合根、配置和依赖 |
| G10 | E10 删除旧生产链（已完成） | R 清理旧 shadow 研究接点（已完成） | 活动树和研究树均无旧 snapshot/shadow 依赖 |
| G11 | E11 V2-only 发布 | R5 可继续等待真实交易日 | 非生产研究不阻塞 V2 release |
| G12 | 无生产改动 | R5 完成 40+20 最终报告 | 40 历史、20 连续前向及配对数量全部满足 |
| G13 | 无生产改动 | R6 新窗口权重、风险和门槛研究 | 不复用已用于第一轮晋级的评价窗口试参 |
| G14 | 独立策略发布批次 | R7 人工晋级 | 禁止自动晋级、在线调权或运行时策略回退 |

## 6. V2 工程 lane

### V2-E0：唯一产品契约重置（已完成）

- 固定 V2 唯一链路、`.runtime/v2`、无旧数据读取和完整旧 release 回退方式。
- 固定统一 `/api/v2/decisions/<strategy>`、状态和事件目标。

### V2-E1：统一 V2 数据平面（已完成）

- 收敛证券主数据、交易日历、全市场行情、候选报价、历史特征、研究事实和风险端口。
- 固定 `DailyFeaturePack`、`MarketEpoch`、`CandidateQuoteEpoch`、`ResearchEpoch` 父子身份。
- 字段携带来源、源时间、接收时间、质量状态和内容版本；字段级合并不得清空更完整事实。
- 持久化主数据、交易日历、历史摘要和风险组件；失败保留最近有效 epoch。
- 复核既有 `SourceCapability` 清单与 `docs/reports/v2-p1-source-capability-baseline.md`。
- 未验证来源不进入评分、冻结、组合根或生产配置。

退出条件：同一快照无新旧拼接；潜在可执行代码主数据覆盖 100%，候选核心历史覆盖不低于
99%，无效空不得覆盖最近有效数据。

### V2-E2：统一决策核心与持久化（已完成）

- today/tomorrow/d25 使用统一 scored identity；long 使用无评分 projection identity。
- 当前索引使用 expected-version CAS；hybrid 必须引用当前 local 父版本。
- overlay 匹配 decision/projection version；冻结仓储按策略和交易日唯一提交并校验 SHA-256。
- 发布通用 `V2DecisionCommitted`，不得导入 research 类型。

退出条件：并发 CAS 单胜者、迟到拒绝、半提交恢复、损坏隔离、哈希冲突和跨策略隔离通过。

### V2-E3：独立调度与生命周期（已完成）

- V2 调度器驱动数据刷新、决策 worker、DeepSeek、发布、observer、冻结和结算。
- 每策略使用有界 latest-wins；tomorrow 获得行情、计算、模型和发布保留容量。
- 共享 DeepSeek 预算、缓存和 single-flight，每日物理请求上限保持 168。
- observer 独立有界队列，研究失败不占用冻结或发布保留容量。
- 后台线程可停止、可等待、可观察，关闭共享一个有界 deadline。

退出条件：停用旧 Pipeline 后 fixture 可完成数据、决策、发布、冻结和关闭；HTTP 无外部 I/O。

### V2-E4：Tomorrow 正式接管（已完成）

- native input 直接生成 local；合法结构化 facts 生成引用当前 local 的 hybrid。
- 删除 baseline 关联、cutover gate、shadow evidence 和 shadow 冻结依赖。
- 14:49:20 checkpoint、14:50 封口、幂等冻结重试和合法收盘恢复只使用 V2 决策。

退出条件：固定融合向量 `83.40`，local/hybrid CAS、迟到拒绝、不可覆盖、恢复和合法空通过。

### V2-E5：Today 正式接管（已完成）

- today 原生输入和纯领域选择直接发布 local/hybrid。
- 11:20 当场冻结；错过后保持 `not_ready`，禁止启动、checkpoint 或收盘追补。
- 已有正式记录只允许匹配 overlay，不修改名单、分数、动作和排名。

退出条件：11:19:59、11:20:00、边界后启动、冻结重试、迟到模型和 overlay 测试通过。

### V2-E6：D25 正式接管（已完成）

- d25 原生输入、专属评分、local/hybrid CAS 和查询接入统一核心。
- 与 tomorrow 在统一索引中使用独立策略分区、冻结唯一键、事件身份和错误状态。
- 15:00 后仅在同日正式记录缺失且无待重试封口时创建一次 `close_fallback`。

退出条件：14:50 边界、热/冷恢复、正式空、跨策略隔离和不可覆盖通过。

### V2-E7：Long 正式接管（已完成）

- 固定池和分组继续由 `long_watchlist.json` 唯一维护。
- 定向报价和最近有效行情生成无评分 current projection。
- 不调用 DeepSeek、不执行候选/TopK、不冻结、不写历史或结算；部分失败不自动换股。

退出条件：`score_status=not_applicable`、分组唯一、失败降级和零 DeepSeek 请求通过。

### V2-E8：统一 API、SSE 与根页面（已完成）

- 根页面直接渲染统一 V2 工作台，不保留独立 tomorrow 页面。
- 只保留统一 decisions current/history/dates、status 和 events。
- SSE 使用单调序列、有界历史/客户端队列、游标恢复和慢客户端隔离。
- 页面展示数据年龄、覆盖、漏斗、预算、冻结、降级和逐股诊断。

退出条件：三档桌面通过；ETag、重同步、慢客户端和 HTTP 无外部 I/O 契约通过。

### V2-E9：唯一组合根与入口（已完成）

- `trader-server`、`trader-cli`、启动脚本、配置和组合根只装配 V2。
- 运行目录切到 `.runtime/v2`；删除旧环境映射、迁移命令、archive 和 cutover evidence 命令。
- 启动、初始化、关闭和进程锁只操作 V2 资源。

退出证据：默认配置使用 `.runtime/v2`；启动脚本只接受 `TRADER_*` 环境变量，不再把旧
`HOST`/`PORT` 映射进应用；`trader-cli` 不再注册迁移、archive 或 cutover evidence 命令；
server 使用当前配置的 V2 runtime lock，初始化、关闭和恢复路径不访问旧 runtime 命名空间。

退出条件：全新启动、热重启、异常恢复、进程锁和 graceful shutdown 通过；旧目录零读写。

### V2-E10：删除旧生产链（已完成）

- 删除旧 Pipeline/P1-P6、RecommendationSnapshot、旧 publisher/query/replay。
- 删除旧仓储/schema/迁移器、旧 API/SSE/Web 资源和静态名单副本。
- 删除 shadow runtime/evidence/cutover、双链测试和只服务旧链的 CLI、依赖及配置。
- 删除研究侧旧 shadow 采集接点，保留 committed event observer。

退出证据：`src/trader` 活动树不再包含旧 Pipeline、snapshot publisher/query/replay、shadow/cutover
或旧 Web/API/SSE 模块；测试树删除双链与旧生产链测试；根页面和 API 仅注册 V2 路由，Long
页面继续从固定名单渲染“卡脖子 / 高成长 / 低价潜力”三个 Tab。V2 决策记录仓储、统一事件
observer、三类冻结控制和 Long current projection 均由唯一组合根装配。

退出条件：AST、运行覆盖、源码、测试、配置、文档和 wheel 均无可达旧链资源；完整发布门禁和桌面
验收留给 V2-E11。

### V2-E11：最终验收与发布（已完成）

- 完整质量、测试、构建、架构、冻结、预算、SSE、桌面和 wheel 门禁全部通过。
- 新 release 只读写 `.runtime/v2`；旧 release 回退演练不混用新目录。
- README、启动文档和权威文档只描述 V2 活动产品。

退出条件：零已知 Review 发现，所有门禁通过并确认 `HEAD == @{upstream}`。

## 7. 评分研究 lane

### Score-R0：权威契约与预注册（已完成）

- 评价最多 60 个不同交易日，固定最多 40 日历史和 20 日连续前向。
- 固定五个挑战者、统计种子、区块 bootstrap、Holm 家族和人工晋级。
- 硬拒绝只保存聚合；逐股研究总体固定为硬过滤通过股票。

### Score-R1：紧凑决策轨迹（已完成）

- 已实现不可变研究 schema、独立端口、有界异步记录和规范 SHA-256。
- 每个输入保存 production_local/research_shadow 配对；不新增 DeepSeek 请求。
- 写入失败、队列满、超限和冲突不阻塞推荐，不改变正式 API、P6 或冻结。

### Score-R1-Migrate：迁移到 V2 committed event（已完成）

- 删除对旧 snapshot baseline 和 tomorrow shadow runtime 的采集依赖。
- 只从已提交 `V2DecisionCommitted` 转换研究轨迹，不重新执行生产评分。
- 保留 R1 schema 语义；身份变化必须显式升级 schema/version，禁止静默重写历史。

退出证据：Today、Tomorrow 与 D25 原生评分投影在决策成功提交后，把通用 committed event 与
同批 `v2_committed_research_audit_v1` 组合成 observation；审计保留硬拒绝聚合、硬过滤通过候选、
生产 Top120、production_local/research_shadow 配对和零 DeepSeek 增量，并以
`v2_research_committed_event_v1` 规范载荷写入独立有界 SQLite。正式冻结重放可以只携带事件，
不得覆盖同 decision identity 已存在的审计；损坏行隔离、容量耗尽和数据库失败均由 observer 隔离。

退出条件：启用 observer 前后 DecisionView、冻结哈希、API/SSE 和 DeepSeek 计数完全一致。

### Score-R2：最多 40 日历史点时数据（已完成）

- 只读 E1 数据平面；硬拒绝后立即丢弃逐股载荷。
- 对硬过滤通过总体计算候选分和最终分乐观上界，只为 Top120 与上界保护集合加载完整字段。
- 使用现有 Polars 生成不可变分区和 SHA-256 manifest，不引入第二套数据框依赖。
- 日线/分钟按代码日期去重，前置因子窗口只保存一份共享输入。

退出条件：40 个有效日无未来数据、哈希可复算、裁剪有上界证明、三板与成本结算完整。

退出证据：研究应用层已实现固定主窗口与最早 `2026-05-18` 的向前扩展、最多 40 个有效日、
逐日覆盖失败身份、每板生产 Top120 起始集和约束感知 active-set；完整字段只按稳定唯一代码
二阶段读取，未加载候选均保存可复算的上界/边界/规则哈希证明。研究基础设施使用既有 Polars
按交易日写入不可变 Parquet 分区，日线/分钟同键异内容由边界拒绝，复权窗口每代码只保存一份，
分区文件和顶层 manifest 均绑定 SHA-256。当前活动运行库没有预注册历史窗口的完整点时 epoch，
因此真实运行只能诚实形成 `exploratory` 覆盖结果，不能伪造 40 个有效日或完整收益证据。

### Score-R3：基线回放与报告（已完成）

- 复用 V2 纯领域过滤、评分、风险和选择函数，不复制公式。
- 生成净超额、MAE、召回、覆盖、集中度、Rank IC 和 20/50/100bp 成本报告。
- 报告冻结后不改指标，重复运行结果和报告哈希一致。

退出证据：研究应用层通过 `HistoricalBaselineReplayEvaluator` 显式复用生产纯领域回放结果，
要求 production 与 active-set oracle 均精确覆盖评估集合并校验连续 Top6、板块 60%、每行业最多
2 只及 production Top120 身份。`score_r3_baseline_report_v1` 从同一
`CostSettlementBasis` 计算 20/50/100bp 净超额、平均 MAE/ATR20、严重回撤率、候选召回率、
字段覆盖率、板块/行业集中度、五分组 20bp 净超额和平均日内 Spearman Rank IC；JSON 报告以
规范 SHA-256 不可变写入，相同内容重放幂等，不同内容或篡改冲突。少于 40 个有效日时状态保持
`exploratory`，能力完成不代表已经取得 40 日真实收益证据。

### Score-R4：五个挑战者（已完成）

- 实现 `continuous_entry`、`coverage_shrink`、`candidate_upper_bound`、
  `heat_weak_structure` 和 `combined_v1`。
- 每个变体独立版本、无共享可变状态；local-only/hybrid 同日同股配对。
- hybrid 只复用已有 facts，不新增模型请求，不影响生产结果。

退出证据：`score_r4_preregistered_parameters_v1` 已把连续入场 11 条分段线性端点、三板高热带与
三项弱结构阈值固定在权威文档及不可变机器 manifest。`continuous_entry_v1`、
`coverage_shrink_v1`、`candidate_upper_bound_v1`、`heat_weak_structure_v1`、`combined_v1`
五个独立版本通过
`HistoricalChallengerReplayEvaluator` 接收各自 override，candidate upper-bound 只放行满足 50 分、
30% 缺失和 active-set loaded 证明的生产 Top120 外研究候选；每个变体按同一 R2/R3 日身份生成
production/local-only/hybrid 同日同股行、未选中零权重和同一 `CostSettlementBasis`。没有已记录
facts 时 hybrid 为 local control copy；研究层无模型端口，DeepSeek HTTP 增量固定为 0。少于 40 个
有效日仍只标记 `exploratory`，本章不执行 R5 bootstrap、Holm、前向采集或生产晋级。

### Score-R5：统计门禁与 20 日前向影子（已完成）

- 对 40 日结果执行固定种子配对 bootstrap、多重检验和集中度分析。
- 仅历史门禁通过者进入 20 个连续计划交易日前向影子，参数不得回看修改。
- 最终至少 300 条同日同股配对，其中前向至少 100 条。

退出证据：`score_r5_statistical_gate_v1` 以 `20260811` 主种子为五变体和 3/5/10 日非循环区块
派生独立随机流，每项固定 10,000 次；20bp、5 日单侧 p 值保持五成员 Holm 家族，严重回撤、
召回、删月/删板、正贡献集中度、五分组和 Rank IC 均从同一同日同股行派生。历史不足 40 日、
配对不足 300 条或任一门禁缺证时明确终止为 `historical_rejected`。只有历史通过版本可向
`score_r5_forward_day_v1` 固定 20 日集合追加 `valid/failed/no_decision`，同键同内容幂等、冲突
拒绝且失败日不可替换；最终封存分别保留历史哈希、前向报告与 40+20 合并报告，并对合并序列
重新执行固定家族。当前真实 R2/R4 覆盖不足 40 日，故真实状态仍为探索性历史终止，尚未进入
2026-11-02 至 2026-11-27 的前向采集，也没有版本取得 `promotion_eligible`；该外部证据缺口不以
fixture 或后补日期伪造，活动生产保持不变。

### Score-R5-Run：运行证据连续性修复（已完成）

- committed observation 从 64MiB 单库改为按交易日不可变 SQLite 分区，旧单库只读兼容。
- 公开 120 日/20GB 归档容量、日期覆盖、legacy 计数和 observer 消费失败，生产推荐继续 fail open。
- 新增只读 `research-status` 运维入口，明确 `serve` 不自动执行离线 R2-R5。
- 删除生产组合根中的 no-op 结算器，恢复正式冻结推荐的盘后不可变 outcome/等权基准结算、冷启动
  执行、失败重试和状态计数；结算证据与 committed observation 分库，HTTP 保持只读。
- 原 `score_p0_v1` 缺失窗口保持探索/历史拒绝；修复不得回填、换日或制造 R5/R6 资格。

退出条件：已有 64MiB legacy 单库无需删除或改写即可继续写新日期分区；单日容量隔离、跨分区查询、
幂等/冲突、损坏隔离、总容量拒绝、状态 API 降级和 CLI 只读报告通过。修复只恢复后续证据连续性，
真实 outcome 结算接线通过且不把 Score-R6 标记为可执行。

### Score-R0-Rerun：替代窗口预注册与身份贯通（已完成）

- 原 `score_p0_v1` 保持不可逆 `historical_rejected`，新评价使用独立 `score_p0_v2` 与规范哈希。
- 在首个观察日之前固定 2026-08-21 至 2026-10-23 的 40 个交易日，以及 2026-10-26 至
  2026-11-20 的 20 个前向交易日；任一失败日保留失败，不回退、顺延或替换。
- R2 extraction、R3 baseline、R4 challengers、R5 bootstrap/报告/forward binding 全链绑定同一
  research identity/spec hash；bootstrap 使用独立 `20260820` 主种子。
- 新身份的前向证据使用独立目录，`research-status` 同时报告活动窗口采集进度与旧身份终止状态；
  `serve` 仍只采集 observation/outcome，不隐式执行 R2-R5。

退出条件：新旧身份、日期、随机流、报告和前向仓储不可混用；新窗口不足 40 日时只能报告
`historical_collecting`，完整后只进入显式离线评价，未产生真实 `promotion_eligible` 前
`score_r6_executable` 保持 false。

### Score-R6：第二轮权重、风险和门槛

- 仅在 R5 至少一个结构变体通过后开始，并使用新的预注册窗口。
- 权重非负、合计 1 且向当前权重收缩；小样本板块回退全局参数。
- 联合评估净超额、严重回撤、换手、稳定性、78 门槛和 3/4/5 分风险扣分。
- 单独验证 hybrid 相对 local-only 增量；未通过继续使用 local。

### Score-R7：人工晋级

- 另立生产变更批次更新策略、规则、配置、融合和 schema 版本。
- 禁止运行时自动调权、自动晋级或自动回退。
- 完整执行质量、测试、构建、wheel 和桌面验收。

## 8. 跨 lane 不可破坏约束

- 融合公式固定为 `clamp(local_score * 0.68 + deepseek_score * 0.32 - deepseek_risk_penalty, 0, 100)`，
  `ROUND_HALF_UP` 两位小数；`local_score` 不重复扣本地风险。
- DeepSeek 自由文本不得扣分；物理 HTTP 每日全局上限 168，失败和重试计数。
- today 11:20、tomorrow/d25 14:50 冻结及 close_fallback 语义保持权威契约。
- long 不评分、不冻结、不写推荐历史。
- 研究不保存硬拒绝逐股身份，不读取或写入正式冻结、普通 Web、正式历史或正式结算。
- 数据或模型失败保留最近有效快照并显式降级，不阻塞本地推荐或只读 Web。

## 9. Test Plan

- 所有权：Worker diff 不包含禁止文件；公共接口在波次内哈希不变。
- 隔离：不同 worktree、runtime、数据库和缓存互不访问。
- 事件：observer 队列满、异常、超限和关闭不阻塞决策与冻结。
- 一致性：启用研究前后正式决策、冻结 SHA-256、API/SSE 和 DeepSeek 计数一致。
- 研究：确定性哈希、duplicate/conflict、容量、无未来数据、40+20 和配对数量门禁。
- V2：epoch、CAS、冻结恢复、合法空、SSE 慢客户端、三档桌面和 wheel 安装。
- 每个候选集成树运行 `make format-check`、`make lint`、`make type-check`、`make test`、
  `make package`；涉及 Web 时追加浏览器验收。

## 10. 每章统一交付

1. 记录 HEAD、上游、工作树和文件范围。
2. 先更新权威契约与失败测试，再实现。
3. Review 正确性、冻结、并发、资源、降级、安全、类型、API、桌面和可安装性。
4. 修复到零已知发现并通过所有适用门禁。
5. 更新 Changelog 的 Added、Changed、Fixed、Removed、Verification 和 Residual Risks。
6. 仅暂存本章文件，创建一个提交并推送，确认本地与上游哈希一致后停止。
