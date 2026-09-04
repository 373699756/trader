# 软件业务设计文档

版本：当前发布候选契约

状态：V2-only 工程与发布门禁验收已完成；功能包拆分已完成，当前版本仍为 Unreleased，尚未声明正式 0.2.0 release

版本命名边界：除用户明确确认的评分生产档位 V1/V2/V3 外，本项目不新增或扩展任何 `vN` 版本控制。API、SSE、配置、运行目录、数据集、特征、报告和 Web 资源使用稳定名称；历史审计字符串、供应商模型名、日期及 content hash 仅用于身份或完整性，不能当作新的项目版本。

适用范围：本地 A 股研究看板

本文是产品范围、系统架构、运行时、时间线、数据服务、发布、API、界面、运维、
验收和交付路线的唯一权威。荐股算法、过滤、评分、DeepSeek、融合和选股规则以
[荐股策略文档](recommendation-strategy.md) 为唯一权威；依赖、构建和入口以根目录
`pyproject.toml` 为唯一权威；协作流程以根目录 `AGENTS.md` 为准。

当前交付状态：V2-only 工程与发布门禁验收已闭合；V2-only 是唯一活动产品链。旧 Pipeline、旧 Web 路由
和旧运行目录已从活动树物理删除，不构成发布候选的
兼容能力，也不得反向定义 V2。迁移过程、事故复盘和逐批实现
记录只保存在 `CHANGELOG.md` 与 `docs/reports/`；一次性拆包计划已退役，尚未闭合的产品、发布和工程 Gate 直接记录在本文第 14 节，
策略研究 Gate 直接记录在荐股策略文档第 15.1 节，不再保留并行概览、实施计划或独立运行手册。

## 1. 产品定位与范围

产品是在个人 PC 上运行的只读 A 股研究看板，面向单一使用者，不是多租户 SaaS，
不连接券商，也不提供真实下单。结果只用于研究，不构成投资建议，不承诺收益。

四类视图为：

- `today`：09:36-11:20 的盘中短线研究信号，面向 T+1。
- `tomorrow`：交易日上午开始形成本地草稿并复用跨策略结构化 facts，13:00 后增加
  尾盘数据和增量模型复核，面向 T+1，14:50 冻结。
- `d25`：交易日上午开始形成本地草稿并复用跨策略结构化 facts，13:00 后增加增量模型
  复核，面向 T+2 至 T+5，14:50 冻结。
- `long`：固定长期潜力龙头研究池，只展示当前行情状态；只要统一行情已有固定池代码，
  warmup、上午和下午都生成当前观察快照，不运行荐股策略、不冻结、不生成交易动作、
  不写推荐历史。

系统允许返回 0 只推荐。产品不包含策略验证工作台、自动调参、机器学习训练、股票价格
预测和模拟交易页面；后台可以对正式冻结推荐进行只读收益与最大不利波动结算，作为发布
质量门禁和复盘数据，但不通过普通 Web 提供交易模拟或自动改变生产策略。

### 1.1 tomorrow 优先的目标契约

最终产品以“更快得到可信的明日研究结论，并用点时样本外证据持续提高净超额收益”为
第一目标。tomorrow 是唯一最高优先级生产决策链；today、d25 作为资源隔离的次级视图，
不得占用 tomorrow 的行情采集、计算、DeepSeek 或发布保留容量。tomorrow 在交易日内
提供滚动预览，14:50 固化正式结果，允许返回 0 到 6 只正式推荐和 0 到 6 只观察项，不为补满数量降低过滤、风险或
数据质量门槛。

旧冷启动预热编排、P1-P6、旧执行模式开关和旧缓存分池已经删除，
不是产品目标或兼容要求。V2 保留点时数据、过滤可审计、
只读 Web、冻结不可覆盖、latest-wins 有界背压和失败时不伪造数据等业务不变量。

只使用现有公开来源时，系统只能承诺内部处理时延和真实降级，不能虚构供应商响应 SLA。
“提高收益”不是收益保证。默认研究候选只接受历史 point-in-time 留出门禁；用户于 2026-08-30 先后明确
要求立即启用已封存但历史拒绝的 Tomorrow P2 工件，并将人工日线代理与该工件做成配置切换。活动产品
从 2026-08-30 起统一称为生产档位 V1/V2；既有 `score_tomorrow_shadow_p1_v1`、
`score_tomorrow_historical_p2_v1` 及其 P1/P2 名称属于历史研究身份 P1/P2，必须保持不可变以便审计。
两种人工发布的身份、失败证据、代理差异、监控和禁止自动更新边界以荐股策略文档第 15.1.17–15.1.19
节为准，不得把越权发布或日线 proxy 描述成原 P1/P2 研究门禁通过。

运行范围固定为 Python 3.10-3.14，以及当前稳定版 Chrome、Edge 或 Firefox 桌面版。
验收分辨率为 1280x720、1440x900 和 1920x1080。手机和平板不属于产品范围，不得为
移动端增加业务分支或运行依赖。默认仅监听 `127.0.0.1`，不提供远程身份认证。

### 1.2 V2-only 最终 release 边界

V2 唯一运行目录固定为 `.runtime/v2`，只接受当前 V2 配置、数据库、冻结和事件 schema。
新 release 不读取旧运行目录、旧数据库、旧快照或旧 schema，也不提供迁移导入命令。
V2 数据平面、正式决策与 DeepSeek 预算使用独立持久化文件；预算固定写入
`.runtime/v2/deepseek-budget.sqlite3`，不得探测或复用旧名 `runtime.sqlite3`。

唯一只读产品接口为：

- `GET /`
- `GET /api/decisions/<strategy>/current`
- `GET /api/decisions/<strategy>/history?date=YYYY-MM-DD`
- `GET /api/decisions/<strategy>/dates`
- `GET /api/status`
- `GET /api/events`

不得提供旧 API 别名、重定向、弃用窗口、双读或双写。旧 release 只能与其对应旧运行目录整体回退，
不得与 V2 代码、配置或 `.runtime/v2` 混用。旧运行数据可在仓库外离线保留，
但不进入新 release 的启动、恢复、查询、回放、打包或测试路径。

## 2. 系统能力与业务流程

系统从公开行情和研究数据构建点时快照，经一级发行人永久资格过滤、二级动态硬过滤、候选预选、本地评分、
可选 DeepSeek 结构化复核、风险合并和稳定 TopK 后发布到 Web；冻结策略按时间点保存
不可变历史锚点，当前报价只作为 overlay 展示。

```text
调度与五来源采集
        |
        v
不可变观测 -> IssuerEligibilityRegistry -> 二级过滤与特征
                                  |
                    +-------------+-------------+
                    v             v             v
                 today         tomorrow         d25
                    +-------------+-------------+
                                  v
                     DeepSeek结构化复核（可降级）
                                  v
                        融合、TopK、发布与冻结
                                  v
                        V2当前索引 -> Web/SSE
                                  |
                     检查点/正式冻结/收盘overlay
                                  |
                       后台结果结算（只写审计）
```

任何数据源或 DeepSeek 失败都不得阻塞本地推荐和只读 Web。系统保留最近有效发布，
明确标记 stale、degraded、not_ready 或冻结 fallback，不用空结果覆盖有效结果。

### 2.1 tomorrow v2 目标链路

目标链路按业务时限拆分为独立数据平面和不可变决策平面：

```text
公开来源持续采集 -> 规范化观测日志 -> 全市场最新报价索引
                                      |
                 DailyFeaturePack ----+---- CandidateQuoteEpoch
                                      |
                                  MarketEpoch
                                      |
         一级资格 -> 二级硬过滤 -> 候选预选 -> 本地评分 -> local ScoredDecision
                                                    |
                     ResearchEpoch -> DeepSeek 复核-+
                                                    |
                                      hybrid ScoredDecision
                                                    |
                                      UnifiedDecisionIndex
                                                    |
                                      只读 API -> SSE -> Web
```

`DailyFeaturePack` 在开盘前构建并按新完成的历史、主数据和研究事实增量换版，不阻塞实时
行情；`MarketEpoch` 是一次可审计的全市场决策输入；`CandidateQuoteEpoch` 只更新候选和
已发布股票的高频报价；`ResearchEpoch` 保存经过 schema 和证据校验的结构化研究事实；
`ScoredDecision` 保存 local 或 hybrid 的完整决策身份。每个 epoch 都绑定交易日、上海
时区观察点、上游版本、规范内容哈希，以及按其职责适用的配置、规则、策略和 schema
版本，已发布对象不可原地修改。

一级资格、二级硬过滤、候选预选、本地评分和稳定选择必须是一次确定性管道。过滤结果使用
`pass`、`observe_only`、`reject` 三态并保留逐股原因；缺失关键行情、证券身份或点时
证据不得静默降级为通过。`UnifiedDecisionIndex` 按策略保存最后一个已提交的不可变
决策引用，并通过单提交者 compare-and-set 保证旧行情、迟到 DeepSeek 和失败批次不能
覆盖更新结果。Web 只读取该索引和报价 overlay，不参与采集、过滤、评分或持久化。

### 2.2 V2 数据平面契约

`IssuerEligibilityRegistry` 是生产一级资格的唯一状态源。组合根以独立 SQLite 追加表显式装配该端口；
事实包含代码、原因、事实生效时间、稳定证据 ID、来源和内容哈希，同身份同内容幂等、不同内容冲突。
`permanently_excluded` 代码必须在历史预热、候选定向行情、逐股公司研究、参考数据、Long 定向行情和
分钟行情提交前裁剪，不能进入评分或 DeepSeek；`eligible_unverified` 与覆盖未闭合的发行人仍可进入
受限采集以形成后续权威事实。供应商全市场批量接口不能按代码裁剪时允许物理返回整批，但发布人口和
全部逐股下游必须剔除一级排除代码。冻结 TopK 报价 overlay 与既有 outcome 结算按原正式身份继续，
不得用新名单覆盖历史正式记录。历史回放按事实 `effective_at` 判断，不得以当前名单回填过去。

一级事实只来自正式年度财报亏损、历史 ST/退市警示、权威结构化确认的永久严重风险和人工永久名单；
普通新闻、亏损预告、未有正式结论的调查和 DeepSeek 自由文本不能创建一级事实。已有历史缓存不做
破坏性删除；新请求在调度前阻断。二级动态硬过滤继续处理当前停牌、价格/流动性/上市日龄、板块热度、
存续调查、减持、解禁、质押、财务恶化和其它点时风险，具体公式与原因码以荐股策略文档第 4 节为准。

统一数据平面由 `DailyFeaturePack`、
`MarketEpoch`、`CandidateQuoteEpoch` 和 `ResearchEpoch` 都是深层不可变对象：代码有序
且唯一，业务时间为 `Asia/Shanghai`，内容哈希由规范载荷确定，来源版本、上游版本、
缺失和降级原因属于身份的一部分。非有限核心数值、未来事实、无时区时间和同 sequence
不同内容必须在发布前拒绝。

进程内实时数据平面按交易日和单调 sequence 原子接纳 epoch。`MarketEpoch` 必须引用当前
`DailyFeaturePack`，`CandidateQuoteEpoch` 必须引用当前 `MarketEpoch`；新 feature pack
尚未形成匹配 market epoch 时，读取方继续获得上一组完整一致视图，不能看到新旧拼接。
每个通道只保留有界数量的最近 epoch，旧 epoch、父版本不匹配和迟到结果不得覆盖当前
指针。来源失败只更新结构化失败状态并保留最近有效 epoch；后续成功发布原子清除对应失败。

应用层唯一读取边界名为 `DataPlaneReadPort`，一次返回上述四类 epoch 的一致快照；读取方
不得持有具体缓存、SQLite、供应商客户端或旧 Pipeline 对象。每个业务字段同时携带字段值、
来源、源时间、接收时间、`valid/degraded/stale/missing/conflicting` 质量状态、内容版本和
载荷哈希；epoch 发布前必须校验字段值与该血缘一致。价格-only 或空响应没有权利删除证券
身份、交易日历、历史摘要、研究事实或既有更完整字段。

`DailyFeaturePack` 额外绑定交易日历版本和覆盖清单。潜在可执行代码必须 100% 具备证券
主数据。候选历史覆盖率是加法健康指标，不是 pack 或整批评分的发布开关；pack 可以携带历史不足的
候选，但必须保留逐股历史 session、字段缺失和版本事实，由评分输入适配器按活动策略/profile 判定资格。
不得用无效空 pack、空全市场行情或父版本不匹配的子 epoch 清空最近有效数据。

生产输入适配器在构建 local 前还必须复核请求代码、候选定向特征、证券身份和历史摘要覆盖：
候选定向特征覆盖请求代码 100%，受支持板块且无身份缺失限制的证券身份覆盖 100%。历史资格按策略和
评分 profile 逐股复核：Tomorrow V1/V2 至少 61 个有效 qfq 交易日及模型必需字段，其他策略使用各自
登记窗口和字段。覆盖计数和比例是批次内存事实并以加法状态公开，但不再因低于统一百分比而否决已有
合法分数；活动模型字段资格必须先于板内候选限额，不合格股票不得占用名额、评分、DeepSeek 或执行。
完全没有合法评分人口时返回受控未就绪并保留最近
有效 current。状态 API 同时公开当前策略/profile 的 `history_required_sessions`，使覆盖分母与资格口径
可审计。该复核不读取网络、文件或数据库，也不进入 HTTP 查询。

`DailyFeaturePack` 只保存 `data_as_of < trade_date` 的历史、主数据和结构化风险基线；
不得把当日尾盘值伪装成昨日数据。`MarketEpoch` 把当前 `market_regime` 纳入身份。
`CandidateQuoteEpoch` 可携带
同代码的有界 `CandidateFeatureRow`，仅允许登记的尾盘、入场、执行质量和日内结构风险字段，
禁止覆盖财务、公司风险、证券身份或历史基线。候选报价必须保存本轮跨源偏差，只有偏差有限、
非负且不超过 0.50% 并标记已复核时才可进入 epoch；实时特征行、报价和父
`MarketEpoch` 一并参与规范内容哈希。

证券主数据、实际交易日历集合、每股紧凑历史摘要和风险组件通过应用层持久化端口写入 V2
数据仓储并可在重启时校验恢复；游标只能作为增量位置，不能代替实际交易日历内容。持久化
失败不撤回内存中最近有效 epoch，也不能让未验证来源进入评分、冻结、组合根或生产配置。
服务每次启动恢复持久化资料后，必须在独立 `exchange` reference lane 幂等刷新一次上交所主板/科创板
与深交所 A 股官方证券列表；每个评分共享输入批次发现本轮代码缺少上市日期时也必须触发同一刷新，
三条评分策略复用同一次调度，不得各发一轮。该调度显式携带两组代码：免费证券身份使用
本轮规范全市场快照的完整代码集，估值、财务、研究和历史增强只使用有界候选集，禁止因持久化
身份而对全市场触发候选级慢请求。官方证券列表必须以全量原子快照校验：受支持三板合计不得低于
4000 行、代码必须唯一、沪深两所均须存在且上市日期必须完整；任一校验失败整批拒绝，保留最近
有效快照并按配置负缓存退避。免费证券身份持久化继续在 `reference` lane 异步执行，Tushare 增强
继续使用 `tushare` lane；首次冷启动批次允许按缺失规则降级，完成后的数据必须进入后续批次并
持久化；调度失败不得阻塞本地决策或清空最近有效结果。
沪深交易所官方列表提供的板块、交易所和上市日期必须覆盖完整受支持股票集合并在单个 SQLite 事务中
批量写入 V2 数据平面，不能因候选轮转或 Tushare 积分不足而丢弃。实时全市场报价可以采用低延迟
来源先返回，但任一已经完成物理响应和规范化的富身份来源必须把稳定证券身份晋升到独立身份仓，
即使该响应晚于报价截止时间也不得覆盖当前报价、不得进入当轮评分，却不能随报价抢跑输家一起
丢弃；富身份响应完成后必须立即把合并结果提交到独立 `reference` lane，不能等待下一轮评分或
`tushare` lane。稳定身份按字段无损合并：较新规范记录可以更新已有值，但字段缺席不能删除此前
已验证的上市日期、板块或交易所。上市交易日数统一复用组合根注入的生产交易日历计算。相同来源和
相同规范载荷的证券主数据幂等跳过写入，重启时恢复后继续参与合并。Tushare 只作可选增强，其 token
不是证券身份覆盖或系统就绪条件；状态必须公开免费身份总数、上市日期/交易日龄覆盖、持久化调度
错误数、交易所来源成功/失败/超时、快照行数、上市日期行数、延迟和
`tushare_required=false`，不得再把免费身份缺口归因为缺少 Tushare token。

长期审计的目标约束为：压缩数据按交易日分区，默认保留 120 个交易日并设置 20GB 磁盘上限。
本阶段只保留文档契约，不实现磁盘归档、清理或容量驱逐代码，也不新增相关运行目录、
配置项、后台线程或外部依赖。实现该能力前必须另立交付批次，先确定压缩格式、原子提交、
校验、清理顺序、磁盘满降级和旧 release 只读边界。

### 2.3 tomorrow v2 确定性选择契约

旁路只读用例从数据平面一次取得完整快照；缺少匹配的 `DailyFeaturePack + MarketEpoch`、
交易日不一致或候选代码不属于父全市场 epoch 时返回结构化 `not_ready`，不得评分半组数据。
组装按代码稳定排序，以全市场报价为基线，仅用通过本轮跨源复核的候选价格和白名单实时特征
覆盖；候选单股来源时间早于父市场同股报价时，价格及同批实时特征一起忽略。报价更新同时
保守扩展当日 high/low，避免制造价格超出 OHLC 的假冲突。

纯领域选择器复用权威硬过滤、板内横截面、候选分、tomorrow 六组件本地分和本地风险规则，
固定执行：

1. 全市场逐股产生 `pass`、`observe_only`、`reject` 和细分审计原因；
2. 三板隔离构建至少 100 只的当前总体或最多 5 个交易日的显式 fallback；
3. 核心缺失不超过 30%、候选分不低于 50，可靠候选优先，稳定保留每板最多 120 只；
4. 计算 `local_score = clamp(base_score - local_risk_penalty, 0, 100)`，风险只扣一次；
5. 按本地分、候选分、代码稳定排序，只从 `pass` 且达到本地选择门槛的股票中选 0-6 只，
   每行业最多 2 只；观察候选单独保留，不挤占正式选择。

原生直投影必须为全市场人口预选和候选评分保留两个显式点时水位。人口水位取同一完整全市场
批次的 `observed_at/source_time/received_time` 最大值并使用 `preselect_max_age_seconds`；候选继续
使用最终 `evaluated_at` 与 `score_max_age_seconds`。候选增强耗时不得反向把已接纳的人口批次全部
标记为过期，但候选自身过期仍必须产生 `stale_quote` 并禁止评分、DeepSeek 和正式动作。原生输入
边界须先把人口与候选的观测、来源、接收、证据和风险时间统一到 `Asia/Shanghai`，再计算水位。

统一决策在 local/hybrid 融合后且两个动作池选择前执行《推荐策略》第 9.1 节活动下行保护。保护命中只能把
原本达到执行门槛的候选降为观察，不能改变分数、融合、硬过滤和门槛，也不能把不可用候选补入
观察池；Today、Tomorrow、D25 必须使用各自策略身份计算弱收盘周期信号。

每只股票保留硬过滤、可选告警、候选缺失率/分数/板内名次、本地组件、风险事实和扣分、
板内名次、最终名次及 `candidate_core_missing`、`candidate_score_below_minimum`、
`board_candidate_limit`、`local_risk_veto`、`local_score_below_minimum`、`industry_limit`
或 `top_k_limit`
跳过原因。该纯计算不得从 HTTP 请求触发；发布、冻结和查询只消费其不可变
`ScoredDecision` 结果。

local/hybrid 内部决策 epoch 必须把本轮正式池容量、观察池容量、单行业上限和单板比例封装为不可变
类型对象并纳入内容哈希。epoch 自身按这组同批限制复核连续 rank、正式在观察之前、两池稳定排序、
容量和集中度，不能用宽于活动策略的通用常量替代，也不能因上游选择器已执行约束而省略对象不变量。

### 2.4 tomorrow v2 DeepSeek 融合契约

旁路融合用例只读取一次数据平面快照，并在同一只读快照上完成本地选择、待审投影和决策
身份组装，禁止在 DeepSeek 返回后重新读取另一批行情。当前规范行情形成
`structured_point_in_time` evidence；仅当候选批次不早于父市场同股报价时，才形成
`intraday_tail` evidence；匹配交易日和配置版本的 `ResearchEpoch` 提供点时研究 evidence
和最新结构化公司风险。研究历史不完整时只能新增风险或标记覆盖不足，不得把昨日已确认的
风险事实清零。

同一个纯函数从 `pass`、无 veto 且已有本地分的候选中生成最多 28 只待审与保护集合，优先级
依次为新高风险、距 tomorrow 动作门槛 5 分以内、TopK 边界前后 2 名、证据冲突、本地排名
和代码。`observe_only`、`reject`、未评分和 veto 候选不得进入 DeepSeek。无可审候选时不
调用复核端口；代码错配、传输失败、deadline、迟到、拒绝或空响应均保留已生成的 local
决策；`applied/abstain` 还必须与当前候选 evidence manifest 哈希一致。合法子集可以逐股
形成降级 hybrid 决策，缺失股票保持本地分。

本地结果先生成不可变 local `ScoredDecision`；存在至少一个合法 `applied/abstain` 结果时，
再生成引用 local 父版本的 hybrid `ScoredDecision`。决策绑定 market、实际生效的
candidate/research、配置、策略、融合、阶段、待审集合、逐股特征、风险、模型审计、动作、
排名、降级原因和规范 SHA-256。融合后重新按活动正式池最多 6 只、观察池最多 6 只分别执行
最终分/本地分/代码稳定排序、单板最多 60% 和单行业最多 2 只；无可用 hybrid 时不制造第二
版本。epoch 只保存每板最多 120 只、全局最多 360 只已评分候选的完整条目；全市场过滤只
保留总数、拒绝数、未评分数和结构化原因计数，避免复制约 5500 行完整特征。

`ScoredDecision` 同时保存去重后的 `population_count` 与 `rejected_count`；结构化过滤原因允许
同一股票命中多项，只用于原因分布，API 不得相加这些非互斥计数来推导候选数或拒绝数。
活动正式记录 codec 只接受当前 schema；`population_count` 与 `rejected_count` 必须存在并原样保留，
不得通过旧字段兼容推导。旧 release 的正式记录只能随完整旧 release 和旧运行目录离线保留。

`DeepSeekReviewPort` 的预算、缓存、schema、证据和双模型适配器必须通过组合根显式注入。
V4 监管、减持、解禁、质押、诉讼和业绩六类风险必须先在领域层按事实严重度归一化到活动本地规则；
配置加载必须证明全部 `事实类型 × low/medium/high` 组合均有已注册目标。映射后的规则继续独占 penalty、
TTL、证据类型、互斥组和 veto 资格，未知映射失败关闭，不能把已成功解析的风险无声忽略。模型 veto
与本地 veto 只允许逻辑 OR 合并，任何复核结果都不能清除既有本地 veto。映射版本必须作为有类型的
策略配置字段参与策略内容哈希，映射语义变化必须提升版本以隔离旧缓存和旧决策身份。
融合结果只提交 `UnifiedDecisionIndex`；事件、冻结和 Web 不得直接调用模型或重新计算。

### 2.5 统一 V2 决策索引与冻结契约

统一 V2 决策核心以 `ScoredDecision` 表示 today、tomorrow 和 d25 的评分结果，以不含任何
评分字段的 `LongProjection` 表示 long 当前观察投影。两类身份都绑定策略、交易日、上海
时区观察点、单调 sequence、上游版本与规范 SHA-256；评分身份另绑定配置、策略、融合、
local/hybrid 阶段、结构化过滤聚合和逐项分数、风险、动作与排名。long 不生成正式记录，
也不发布评分提交事件。

每个 scored `DecisionItem` 还必须从形成该项的同批 `FeatureSnapshot.quote` 固化股票名称、行业和
不可变报价锚点；锚点至少包含价格、涨跌幅、成交额、换手率、总市值、来源、来源时间和报价版本。
current、冻结历史和 HTTP 只读投影都复用该身份内元数据，不得在查询时现场抓取或按代码补算。
活动正式记录必须包含显示元数据和报价锚点，投影阶段不得丢弃同批已有名称或行情事实。
报价锚点参与 `ScoredDecision` 规范哈希，但只用于展示和冻结审计，
不得反向改变候选、过滤、评分、风险、动作或排名。
评分原生输入的 `evaluated_at` 取调度请求时刻与同批本地 `observed_at`/`received_time` 的最晚值，
以反映网络刷新真实完成时间；供应商 `source_time` 和公告 `published_at` 仍必须不晚于该时刻，
不得用外部声明时间推进决策时钟或绕过未来数据校验。

应用层 `UnifiedDecisionIndex` 按策略隔离当前身份和报价 overlay。每次发布必须携带调用方
实际读取的 `expected_version` 并执行内存 compare-and-set；旧交易日、旧 sequence、同
sequence 不同内容均拒绝。hybrid 必须引用同策略、同交易日且仍为当前版本的 local 父身份。
overlay 必须匹配当前 decision/projection version、策略和交易日，只能包含当前身份范围内
的代码，并完整携带价格、涨跌幅、成交额、换手率、总市值、来源、来源时间和报价版本；未来报价、
迟到 overlay 和错误 expected version 不得覆盖当前视图。评分身份与其同批初始 overlay 必须在
同一个索引临界区内通过一次 CAS 原子发布，禁止暴露“新决策已可见但行情尚未挂接”的中间状态；
local 升级 hybrid 时同样必须把匹配新父版本的初始 overlay 一起换版。成功提交评分身份时生成
应用层通用 `V2DecisionCommitted`；事件携带完整决策身份和逐项结果，不导入或依赖 research 类型，
observer 失败也无权反向修改决策。

研究 observer 只消费成功提交后的 `V2DecisionCommitted` 与评分投影同批生成的不可变研究审计。
新 `v2_committed_research_audit_v2` 在 local 观察中一次性保存完整点时股票池的有界身份、历史 ST、
行业、上市/重新上市/退市期身份、结构化公司风险、外部风险事实、行情来源时间和
`input_observed_at`；hybrid 观察只引用同一人口 SHA-256，不复制候选输入。该人口仅供离线总体和资格
证明，不进入生产评分、冻结、API 或 Web，也不保存硬拒绝股票的简称、分数、未来收益或供应商原始载荷。
新记录按交易日写入 `.runtime/v2/research/committed-events/YYYY-MM-DD.sqlite3` 独立 SQLite 研究库分区；
已经存在的 `.runtime/v2/research/committed-events.sqlite3` 单库保持原字节不变并作为只读 legacy
分区参与查询、去重和容量统计，不执行破坏性迁移。每个交易日分区拥有独立 schema、规范 SHA-256、
幂等冲突检测、按需校验和损坏隔离；单分区上限必须覆盖全天滚动决策，研究归档同时公开 120 个
交易日和 20GB 总上限、已用字节、剩余字节、日期覆盖及 legacy 记录数。容量不足时显式拒绝新研究
载荷，不删除不可变证据。审计写入失败不回滚或阻塞正式决策。

observer 的队列、接纳、完成、拒绝、消费者失败计数和最后错误代码必须进入 `/api/status`；只要
消费者失败未在当前进程内由后续成功写入恢复，系统健康至少为 `degraded`，不得只在内存对象中保留
而让 Web 显示正常。observer 不写统一决策索引、正式记录库、API、SSE 或活动配置；它
不重新读取行情、重新评分或重新调用模型。初始化失败同样 fail open；
历史数据不从旧 snapshot 或 shadow 运行库回填，
部署后只积累新的 committed observation。新外层记录使用 `v2_research_committed_event_v2`；既有外层
和审计 v1 只按原始载荷形状校验哈希并只读解析，不迁移、不补写。研究库按策略和交易日提供
14:50 截止读取，只接受事件时间与输入时间均不晚于截止点且含完整人口的 local 观察；迟到记录保留审计
但不能恢复计划日。
决策 CAS 成功后必须由提交线程直接写入内存事件流，再异步组装并投递研究审计；研究审计构造失败、
observer 队列满或消费者失败都不得吞掉已经提交的 Web decision 事件。事件流写入失败必须显式降级，
但同样不得回滚已接纳决策。

today、tomorrow 和 d25 的正式记录仓储按策略和交易日唯一提交。仓储先把规范载荷及其
SHA-256 写入 SQLite staged manifest，再原子创建不可变 JSON，最后提交 manifest；同键同
内容重放幂等，不同内容冲突失败。启动恢复使用 manifest 内有界恢复载荷补齐半提交；已提交
文件缺失、损坏、哈希或身份不一致时移入隔离目录并 fail closed，绝不向当前索引返回不可信
记录。活动组合根已经接入 tomorrow、today、d25 的统一核心、原生 worker、正式记录仓储、
observer 和冻结时线，以及 long 无评分 current projection。

#### 独立 V2 调度与生命周期目标

应用层 `V2SchedulerRuntime` 是停用旧 Pipeline 后可独立运行的 V2 调度所有者。它只通过显式
注入的 `Clock`、交易日历、数据刷新、local 决策、DeepSeek 升级、统一决策索引、observer、
冻结和结算端口工作；调度点驱动数据、决策、发布、冻结与结算，HTTP 和只读 Web 不参与
上述工作，也不得产生外部 I/O。当前组合根和入口只装配这套 V2 生命周期；旧 Pipeline、策略专属
Runtime 和旧 Web 外壳均已删除，不构成可调用或可回放实现。

运行时故障身份、合并计数、有界历史、恢复状态和排序由独立 `V2RuntimeIssueRegistry` 负责；
`V2SchedulerRuntime` 只在持有自身锁时记录或恢复问题并读取不可变快照。行情输入与本地决策构建保留在
`V2MarketDataAdapter`，DeepSeek 升级与冻结分别由 `V2DeepSeekAdapter`、`V2FreezeAdapter` 适配端口，
不得重新聚合为同时持有行情、模型和冻结资源的输入运行时模块。

today、tomorrow、d25 和 long 固定为每策略一个运行中任务和一个 latest-wins 待处理槽；
运行中的旧周期允许完成并发布其已经读取完成的不可变输入结果，积压只保留同策略最新交易日与 sequence；
更新的 pending 随后以更高 sequence 替换该临时 current，不能因为行情刷新频率高于评分耗时而让策略长期没有
current。tomorrow 独占完整决策 lane，
从数据刷新、local 计算、可选模型升级到 CAS 发布都不等待其它策略；冻结使用独立紧急
控制容量，结算使用有界普通控制容量，重复的同日冻结或结算键只允许一个成功执行者。

模型端口必须公开不可变 `SharedDeepSeekRuntimeContract`，并同时满足
`daily_physical_limit=168`、共享预算/缓存和共享 single-flight；运行时在真正调用模型前再次
检查注入时钟与 review deadline，失败或迟到只保留已发布 local，不回滚当前决策。该契约
禁止各策略创建独立物理预算、缓存或请求链，long 不进入模型升级。

通用 `V2DecisionCommitted` 只以非阻塞方式进入独立有界 `AsyncDecisionObserver`。队列满、
研究消费者失败或停止中的拒绝只增加脱敏状态计数，不占用发布、tomorrow 或冻结容量，也
不能反向修改当前决策。所有 worker、控制 executor 和 observer 显式公开运行、积压、拒绝、
失败与完成状态；停止时先关闭接收门并取消普通 pending，再排空已接纳控制任务，所有组件
读取同一个 `ShutdownDeadline` 的剩余时间，不得各自重置完整关闭期限。

#### tomorrow v2 决策索引与冻结

Tomorrow 当前指针、封口候选与已提交正式记录统一使用 `UnifiedDecisionIndex`、
`ScoredDecision` 和 `CommittedDecisionRecord`；当前索引仍只
保存内存引用，持久化只发生在显式检查点或正式提交边界。

其 local/hybrid 发布必须携带调用方实际读取的
`expected_current_version`，通过 compare-and-set 后才能替换当前指针。hybrid 还必须引用
当前 local 父版本；旧交易日、旧 sequence、同 sequence 不同内容、父版本错配和冻结后更新
全部拒绝。索引不读取网络、配置、文件或数据库。

14:49:20（含）至 14:50（不含），调度器必须自动为距边界不超过 30 秒的当前决策写
`V2DecisionCheckpoint`；暂时没有合格 current 时按固定时点生命周期重试至 14:50。14:50 冻结先在索引内原子封口并确定唯一候选，再持久化不可变
JSON 和 SQLite manifest，最后才把索引切换为 frozen；写入失败时保持原决策和同一封口候选，
只允许幂等重试相同版本。持续运行优先冻结索引中不晚于边界的最新决策；重启恢复仅允许
tomorrow 使用同日、同配置、哈希有效、尚未消费且边界年龄不超过 30 秒的检查点。已存在的
同日正式记录优先恢复，任何迟到行情或 hybrid 都不能覆盖。

正式记录先执行 official-only 投影，只保留 `selected=true` 且 `action=executable` 的条目；
报价锚点作为匹配 overlay/official-close 输入版本，不改变 `ScoredDecision` 中的股票、分数、
风险、动作和排名。15:00 后仅在同日正式记录不存在且没有
待重试的 14:50 封口时允许创建一次 `close_fallback`：运行中路径只能固化当前索引决策，
冷启动路径只接受调用方已经用完整同日收盘数据生成的 local 决策；两者都必须提供与入选
代码完全一致的正价格收盘锚点，并标记 `close_fallback`、`official_close`，local 决策再
标记 `local_only`。本用例不抓行情、不评分、不调用 DeepSeek。

统一 decision repository 使用独立 v2 表和目录，通过临时文件、flush、fsync、原子创建、SHA-256
和唯一交易日 manifest 提供检查点、正式冻结、冲突拒绝与恢复；损坏或半提交文件不得进入
索引。原始行情 120 交易日/20GB 压缩归档仍只保留目标约束；实现前必须另立交付批次。

### 2.6 V2 查询与发布

活动读取链固定为 `UnifiedDecisionIndex -> application queries -> /api -> SSE -> Web`。
应用层查询一次读取完整不可变决策，并只叠加父版本、策略和交易日匹配的报价 overlay；历史查询
只精确读取请求日期的正式记录。HTTP 不得抓行情、评分、调用 DeepSeek、触发冻结或现场重放旧规则。

统一事件流使用单调序列、有界历史、有界客户端队列和最多 32 个订阅者。无游标连接从打开时的
当前序列开始；显式 `Last-Event-ID` 或 `cursor` 才回放。游标超前、过期、不连续，schema、
base 或 identity 不匹配，以及慢客户端统一发送 `resync_required`，浏览器随后以 ETag 重新读取
current。事件发布不等待客户端消费；客户端按策略、交易日和事件类型过滤，decision 事件才按 ETag
重读完整 current，overlay 事件携带行级报价 patch 并只修改匹配父决策的展示报价。无关策略或日期事件
不得触发 GET。status 与 current 共同公开由决策内容哈希和有效 overlay 哈希形成的
`projection_version`；事件丢失时客户端按该身份对账恢复。SSE 正常时不持续轮询完整决策。

统一公开外壳已交付。`DecisionView` 对 today、tomorrow、d25 和 long 使用同一 envelope；评分策略
公开决策身份、阶段、冻结、覆盖、过滤、分数、风险和匹配父版本的报价 overlay，long 固定
`score_status=not_applicable` 且 history/dates 不适用。current 与 history 支持 ETag/304，dates
只列按交易日倒序的正式记录。status 汇总四策略数据年龄、覆盖、冻结、降级、DeepSeek 预算和
事件流健康；根页面只消费这些统一接口。旧 URL 和独立 tomorrow 页面不再注册，相关源文件已
物理删除，不构成兼容期或保留承诺。

### 2.7 当前发布边界

当前 V2-only 工程与发布门禁验收已闭合：统一数据平面、决策核心、独立运行时、today、tomorrow、d25、long、
统一 `/api/*`、根页面、V2 运行目录和进程入口共同构成唯一活动产品。旧生产链、迁移/归档/
cutover CLI、旧 Web 外壳、旧运行读取和兼容分支均已删除；任何后续改动不得重新引入。

已经闭合的 shadow、cutover、baseline 对比、版本事故修复和分阶段门禁不是活动产品契约；
其证据只保存在 `CHANGELOG.md` 与 `docs/reports/`。后续未完成状态只记录在本文第 14 节或
荐股策略文档第 15.1 节；行为边界发生变化时同步更新对应权威章节。

### 2.8 tomorrow v2 运行契约

活动组合根由 `V2SchedulerRuntime` 的 tomorrow lane 驱动 `V2MarketDataAdapter` 生成
`TomorrowNativeInput`。原生输入先生成 `local ScoredDecision` 并以调用方实际读取的版本执行
CAS；只有 14:48 前完成、代码与证据 manifest 一致且时间合法的结构化 DeepSeek facts 才能
生成引用该 local 版本的 hybrid。模型失败、部分结果、迟到或父版本被更新时保留已发布 local，
不得借用其它决策身份或第二条 DeepSeek 请求链补齐。

当前、冻结和研究轨迹都消费同一个 `ScoredDecision.version`；formal 投影也作为通用
`V2DecisionCommitted` 进入有界 observer，研究写入失败不能回写或阻塞正式链。

14:49:20（含）至 14:50（不含），调度器自动尝试保存距边界不超过 30 秒、同运行身份且已 official-only
投影的 `V2DecisionCheckpoint`，失败在窗口内按固定时点生命周期重试。14:50 先在 `UnifiedDecisionIndex` 原子封口，再提交按策略和
交易日唯一的 `CommittedDecisionRecord`；持久化失败保留同一封口对象供幂等重试，任何同日
local 或 hybrid 都不能越过封口，报价 overlay 只允许匹配封口后的正式父版本且不得改变决策内容。
重启先恢复同日正式记录；仅在 15:00 前允许用未消费、
哈希有效的同日检查点恢复。合法业务空结果与非空结果使用相同提交语义。

15:00 后若同日记录仍缺失且没有待重试封口，运行中路径固化既有 V2 current，冷启动路径用
完整同日收盘原生输入生成一次 local；两者都不调用 DeepSeek，并把规范收盘输入版本与
`close_fallback`、`official_close`、必要时的 `local_only` 一并绑定到不可变决策身份。已有
同日正式记录永远优先，收盘恢复不得覆盖。

### 2.9 today v2 运行契约

活动组合根由 `V2SchedulerRuntime` 的 today lane 驱动 `V2MarketDataAdapter` 生成
`TodayNativeInput`。Today 与 Tomorrow 复用同一套纯领域过滤、板内评分、结构化风险、融合和
稳定排名函数，但原生输入、策略政策、sequence、worker、observer、当前指针和事件身份按策略
隔离。`today_observe` 只允许观察动作；`today_main` 和 `today_late` 分别使用当前权威门槛。
local 先以实际读取版本执行 `UnifiedDecisionIndex` CAS；只有 11:18 前提交且在 11:20 前完成、
代码和证据 manifest 一致的结构化结果，才能形成引用当前 local 的 hybrid。

11:19:59 及此前接纳的同日最新决策可在 11:20:00 当场原子封口；边界一到，索引即关闭同日
Today 发布，即使当时没有可冻结稿也不得再接纳迟到 local、hybrid、行情或风险结果。封口后
提交按策略和交易日唯一的 `CommittedDecisionRecord`；持久化失败保持同一 sealed version，
后续只允许幂等重试。启动时先恢复已有同日正式记录；在 11:20:00 或之后启动且没有正式记录，
立即进入 `missed_freeze`/`not_ready`，禁止 checkpoint、启动检查点、旧发布链、午间补算和
`close_fallback`，当日不可恢复。

生产调度在 11:20 后不得再为 Today 创建评分周期；边界前已经在途但随后被 `freeze_sealed` 或
`freeze_closed` 拒绝的发布属于冻结保护的预期结果，只累计拒绝计数，不得登记为系统错误或
持续降级。Tomorrow、D25 和 Long 的午后调度不受此限制。

已有 Today 正式记录只允许创建父版本、策略、交易日和入选代码均匹配的 `DecisionOverlay`。
overlay 只替换价格、涨跌幅、成交额、换手率、总市值、来源和报价时间；不得修改正式记录中的
名单、分数、风险、动作或排名。运行时在评分前后依次驱动全部 V2 控制端口，使 11:20:00 的关闭先于同轮评分提交；
HTTP 始终只执行只读查询。

11:20 后 Today 不再创建评分周期；tomorrow/d25 的同批全市场刷新会从统一数据平面提取 Today
正式入选代码的最近有效报价，以 overlay CAS 更新并发布 SSE overlay 事件。缺少某只新报价时保留
该代码最近有效 overlay 或正式锚点，不得清空 Web 当前数据，也不得为更新报价重新评分。
overlay 的 `observed_at` 必须取本次调度请求、入选特征本地观察时刻和报价本机接收时刻的最晚值，
并保持同一上海交易日；供应商声明的 `source_time` 不得单独推进该时钟，晚于可信本地时钟的未来
报价必须拒绝。并发 CAS/父版本已推进属于预期竞争，真实时间、代码范围或事件发布失败必须进入
overlay 阶段状态；同策略后续一次成功刷新必须把旧活动问题标记为已恢复。

### 2.10 d25 v2 运行契约

活动组合根把 `D25NativeInput` 直接送入策略独占的单运行、单 pending V2 worker。
D25 复用统一过滤、板内 d25 专属评分、结构化风险、融合和稳定排名纯函数，但与 tomorrow
保持独立的输入身份、sequence、worker、observer、错误状态、`UnifiedDecisionIndex` 分区、
正式记录唯一键和只读查询实例。local 先按实际读取版本执行 CAS；合法 hybrid 只能引用仍为
当前的同策略 local 父版本，不能读取或覆盖 tomorrow 的 current、封口、事件或正式记录。

14:49:20（含）至 14:50（不含），调度器自动尝试保存同运行身份且距边界不超过 30 秒的 D25 检查点；
14:50 原子封口 D25 当前决策，并按 `strategy=d25 + trade_date` 唯一提交不可变正式记录。
合法空结果与非空结果使用同一提交语义，持久化失败只允许重试相同 sealed version；同日
tomorrow 的封口、恢复或仓储冲突不得影响 D25。热启动优先恢复同日正式记录，14:50 冷恢复
只接受哈希有效、尚未消费且身份匹配的 D25 检查点。

15:00 后仅在 D25 同日正式记录缺失且索引不存在待重试的 14:50 封口时允许创建一次
`close_fallback`。运行中路径固化 D25 current；冷启动路径用完整同日收盘原生输入执行一次
纯本地 d25 评分并固化 local，均绑定规范 official-close 版本且不调用 DeepSeek。已有正式
记录、待重试封口、错误策略身份、非官方收盘版本或迟到覆盖一律拒绝。

### 2.11 long v2 运行契约

活动组合根按 `long_watchlist.json` 的唯一版本、项目顺序和全局唯一分组构造
`LongV2Runtime`。调度 cadence 只提交带上海时区观察点、phase、deadline 和 force 标志的
`LongRefreshRequest`；独立 `trader-v2-long` 单 worker、单 latest-wins 待处理槽直接调用固定池
定向行情端口并向 `UnifiedDecisionIndex[strategy=long]` 发布 `LongProjection`。Long current
的 `score_status=not_applicable` 由无评分投影类型和运行状态共同保证。
投影观察点使用组合根注入的处理完成时钟，不得把请求发出后、网络完成前的正常接收时间误判为
未来数据；来源时间或接收时间晚于处理完成时刻时仍必须拒绝。

每个 `LongProjectionItem` 直接携带代码、配置名称、行业、唯一分组身份、价格、涨跌幅、成交额、
换手率、总市值、来源、来源时间和 quote version，不存在候选分、本地分、融合分、风险扣分、
动作阈值或排名字段。投影严格保留配置项目顺序和完整固定名单；同轮有效报价标记 `live`，部分
代码失败时优先使用该代码同交易日最近有效报价并标记 `retained`，仍无报价则保留显式
`missing` 占位。未来报价、未知代码、非正价格和错误时区输入不得进入 current，也不得自动换股。

Long runtime 不注入 reviewer、observer、冻结协调器、正式记录仓储或结算端口，不创建正式记录、
历史日期或结算事件，不读写旧 snapshot。定向行情整体失败时仍以完整固定名单发布或保持最近
有效 current，并在状态中公开 `long_quote_unavailable` 与覆盖计数；同交易日最近有效报价只在
进程内由该 runtime 持有，交易日切换立即清空。Long 慢请求只占用 `trader-v2-long`，不得阻塞
today、tomorrow、d25 的评分、DeepSeek、冻结、overlay 或结算资源。

## 3. 架构与代码边界

活动产品代码只能位于 `src/trader`，固定依赖方向为：

```text
entrypoints / web / infra -> application -> domain
```

### 3.1 功能包目标布局与迁移约束

生产关注域按层内能力收拢为配置与组合、数据采集、过滤与评分、决策运行和展示；离线研究与结果
结算作为隔离的第六维护域。目标包只表达所有权和依赖边界，不改变进程、发行包、公开 API、运行目录
或任何评分/冻结行为：

```text
domain/market
domain/recommendation/filtering
domain/recommendation/scoring
domain/recommendation/risk_fusion
domain/recommendation/selection
domain/research
domain/review
domain/outcome
application/runtime
application/market_data
application/recommendation
application/decisions
application/research
application/outcomes
infra/settings
infra/market_data/providers
infra/market_data/normalization
infra/market_data/history
infra/market_data/references
infra/market_data/service
infra/deepseek
infra/persistence
infra/research
web/api
```

BaoStock 离线基础设施在 `infra/research` 内继续按真实职责拆分：`baostock_gateway.py` 只负责 SDK
逐行载荷翻译，`baostock_daily.py` 只拥有 SQLite 分片，`baostock_daily_serialization.py` 与
`baostock_daily_codec.py` 只拥有显式 JSON codec，`baostock_catalog.py` 负责 catalog/manifest 组装，
`baostock_partition_archive.py` 提供分区归档门面。公开类型和调用方式保持稳定，不增加聚合 `utils` 模块。

最终包状态已固化：研究、结算、运行时、行情、推荐、决策、基础设施和 Web 均位于上方目标目录；旧
根级迁移路径不属于活动树，也没有兼容转发或迁移台账作为运行输入。研究、结算和 profile 证据边界为：
结果结算用例和结算端口位于 `application/outcomes`，离线研究用例及 profile 证据端口位于
`application/research`；生产模型共享的不可变 Tomorrow P2 工件位于 `application/ports/tomorrow_model.py`，
不反向依赖离线研究。`application/research/__init__.py`、`domain/research/__init__.py` 和
`infra/research/__init__.py` 只作为标记包，不聚合导入。`trader.entrypoints.cli` 只在执行显式
`research-*` 命令时加载研究实现，`trader.entrypoints.server` 仅加载权威后台证据消费者；普通生产入口不再
隐式导入离线筛选、回放和模型训练模块。研究/结算仍不启动网络、DeepSeek、HTTP、冻结或活动数据库写入，
也不具备生产配置、正式决策、自动调权或自动 profile 切换权限。

迁移批次必须继续使用显式组合根 `bootstrap.py`；`application/ports` 在没有耦合证据前保持稳定。每个
目标包都应有窄入口、直接测试和影响矩阵记录，层间只能沿上方箭头依赖；同层子包由共享不可变值对象
连接，不得通过聚合 `__init__.py`、动态字典或隐藏服务定位器取得能力。批次完成后，架构契约必须同时
证明旧路径退役、依赖图无反向边和无循环导入。

- `domain`：按 `market`、`recommendation`、`research`、`review`、`outcome` 五个业务能力包组织不可变
  值对象和纯函数。`market` 负责点时行情、因子、研究、新闻与尾盘信号；
  `recommendation` 负责过滤、板内评分、策略组合、融合、下行保护和稳定排名；`review`
  负责结构化复核值与本地风险映射；`research` 负责离线研究身份、报告与晋级值对象；
  `outcome` 负责冻结推荐结果结算。领域包不得读取配置、
  时钟、网络、文件或数据库，不保留旧根级模块或动态兼容导出。
- `application`：端口按行情、候选特征、报价、研究、参考/历史、快照、事件、复核和结果
  读写能力拆分；流水线只接收不可变的依赖、选项和资源集合。跨线程事件使用有类型内存
  记录、状态枚举与深层不可变 JSON 载荷，状态转换、deadline、latest-wins、冻结 CAS 和
  停止顺序由应用层显式拥有；不得导入 Flask、`infra` 或旧包，也不得让
  `Mapping[str, object]` 或共享可变字典穿越新的应用公共边界。
- `infra`：配置、行情、交易日历、DeepSeek、缓存、SQLite、文件和外部适配器；编排门面只
  持有显式有类型组件，不通过 mixin、多继承、共享状态基类或 `Any` 属性取得能力。
- `web`：`web/api` 唯一拥有请求校验、显式 JSON 投影、SSE 响应和注入的只读服务协议；
  `web/app.py` 只创建 Flask 应用、形成 release 资源快照并调用单一 blueprint 注册入口，模板、静态资源和
  `static_assets.py` 留在展示边界。Web 只能调用应用层只读用例，不保留根级 API 转发模块或重复路由入口。
- `entrypoints`：参数、进程生命周期和退出码。
- `bootstrap.py`：唯一组合根，显式创建客户端并注入依赖；禁止全局服务定位器。

进程内状态统一使用不可变、有真实字段类型的值对象；来源、数据集或策略等运行期键控集合可以使用
`Mapping[Key, StatusValue]`，但集合根和集合值都不能用 JSON 字典代替状态类型。应用层不得提供
`as_dict()`、`to_status()` 或 `to_json()` 自行决定线格式，也不得用字典下标读取工作线程、队列、
cadence、缓存、延迟或数据质量状态。JSON 只存在于显式边界：配置和供应商载荷解析、持久化 codec、
schema 约束的不可变事件载荷，以及 Web/可观测性响应投影；转换函数归属对应 adapter，并在该处统一
处理枚举、日期时间、动态键和公开字段白名单。领域或应用对象增加字段不自动改变任何外部 JSON schema。

行情适配器固定采用组合：`MarketSourceCoordinator`、`QuoteCache`、
`HistoryCache`/`HistoryWarmup`、
`ResearchLoader`、`IntradayLoader` 和 `ReferenceLoader` 分别拥有自己的有类型状态、锁和
资源依赖；类之间不得通过 mixin、共享状态基类、`Any` 属性或隐式模板方法取得能力。
`bootstrap.py` 显式装配这些组件，最外层 `MarketFeatureService` 只协调与转发行情、
候选、报价、研究、参考、元数据和结果端口，不保存组件业务状态。DeepSeek 固定按 HTTP、
schema、预算批次、预算汇总、缓存、请求执行、状态和复核编排拆分；
行情网关、来源和 Tushare 的 health 根值必须分别使用 `MarketGatewayHealthStatus`、
`MarketSourceHealthStatus` 与 `TushareHealthStatus`；最终 `MarketDataHealth` adapter 才按公开字段白名单
投影 JSON，预热和其它内部调用者只能读取类型字段。
`SQLiteIssuerEligibilityRegistry` 由 `bootstrap.py` 单例装配并注入门面、`HistoryWarmup` 和健康投影；
它不属于领域层，也不允许 Web 请求现场读取数据库。只读运维命令
`trader-cli eligibility-list --as-of <带时区时刻>` 可审计指定时点的代码、原因和证据 manifest，
不访问网络、不修改名单。Web 只能读取市场健康对象中已经聚合的一级资格状态。
`DeepSeekReviewer`、`DeepSeekBudgetLedger` 只组合这些组件。快照仓库只负责冻结、检查点、
收盘 overlay 和结果结算，不持久化流水线事件或实时来源健康。

`create_app()` 必须无线程、无网络、无数据库和无文件写入副作用。HTTP 请求不得抓取
行情、评分、调用 DeepSeek 或写盘。新代码不得导入 `stock_analyzer`。活动源码单文件
最多 1200 行；接近上限仍须按职责、耦合和可测试性独立 Review，禁止为满足行数机械拆分
或新增含义模糊的聚合模块。

today、tomorrow 和 d25 共用 `ports/scored.py`、`scored_selection.py`、`scored_quality.py`、
`scored_fusion.py`、`scored_v2_projection.py` 与 `scored_v2_freezing.py`。模块名表达复用边界，
策略差异只能由有类型 `Strategy`、板块策略和冻结参数注入，不得再建立以 tomorrow 命名却被三策略
共同调用的别名模块或 Today 包装层。活动评分唯一入口是板块策略 `score_board_strategy()`；旧通用
today/tomorrow/d25 评分器、权重配置和双乘因子不得保留为回放或性能兼容链。

公开入口固定为 `trader-server` 和 `trader-cli`。配置通过 `--config` 或
`TRADER_CONFIG` 传入绝对路径，不得按当前工作目录猜测。HTML、CSS、JavaScript 和
图标随 wheel 作为包资源发布。

## 4. 生命周期、并发与资源所有权

入口依次加载配置、创建适配器、创建运行时、启动流水线并启动 Web。退出时先关闭
事件接收门，排空已接收的冻结和风险事件，再停止来源、标准化、策略、DeepSeek 和
long 执行器，持久化单写线程最后退出；停止完成后不得遗留 worker、future、连接、
single-flight 或回调引用。

第一次 `SIGINT`、`SIGTERM` 或 Windows `SIGBREAK` 创建且只创建一个进程级
`ShutdownDeadline`，默认总期限为 30 秒。Web、scheduler、流水线、来源 lane、研究、
缓存和所有 executor 只能读取同一个 deadline 的剩余时间，不得为每个组件重新
获得一份完整 timeout；普通行情和普通评分在关闭入口取消，已经接纳的冻结和风险事件按
优先级排空。第二次关闭信号立即按第一次信号的退出码强制退出；总期限到达仍有资源未停时
输出不含业务载荷的 shutdown report 并以退出码 2 强制退出。浏览器窗口关闭不等于服务
关闭，正常入口是运行终端中的 Ctrl+C 或操作系统正常终止信号。

默认线程和有界资源按所有权分层：应用协调层为调度 1、八类物理数据任务各 1 个
latest-wins lane、today/tomorrow/d25 策略评分各 1、三个短线策略 DeepSeek 升级各 1、long 行情 1、冻结/检查点/结算控制 3、
决策审计 1、公司研究协调 1；基础设施执行层为实时数据采集 6、历史下载 5、公司研究端点 5、
标准化/过滤 2、DeepSeek 4、合并 1、持久化 1。八类数据任务固定为全市场、候选报价、
TopK overlay、分钟 tail、行业热度、市场新闻、个股风险和参考数据；最终候选报价复用候选 lane，
收盘/当前行情复用全市场 lane，不得另建重复 worker。实时数据池含五个普通来源 worker、五个普通待处理
槽位，以及只供 TopK 腾讯定向报价使用的一个紧急 worker、一个紧急槽位；普通候选报价进入
`tencent` 普通 lane，TopK 进入独立 `tencent_topk` 紧急 lane，分钟尾巴进入独立
`eastmoney_intraday` 普通 lane，全市场仍使用 `eastmoney`，彼此不得以同一 pending 槽互相覆盖。历史池拥有独立五个
worker，公司研究端点池拥有独立五个 worker；两者均使用候选池容量的有界等待槽，不得
占用实时采集位。公司研究协调器同一时刻只运行一个批次，每批最多 4 只股票、预算 40 秒；
批内各股票并行，单只股票的财务、公告、质押和解禁端点仍按确定顺序执行。每个来源最多一个运行任务和一个
latest-wins 待处理请求；同源在途时只保留最新观察点，不补跑旧周期。
东方财富和新浪全市场适配器可在单次来源任务内部创建随请求关闭的有界分页执行器，
并发分别不超过 6 和 5；它们不接受跨轮任务、不持有后台生命周期，也不得占用或等待
同一实时来源池形成嵌套死锁。

事件至少包含事件 ID、主体、交易日、阶段、策略、优先级、数据版本、配置版本、创建
时间、deadline、重试数和不可变载荷引用。幂等键为：

```text
trade_date + phase + strategy + event_type + subject_key + data_version
```

事件在跨线程前转换为深层不可变 JSON 形态。状态固定按
`pending -> running -> success/failed/expired` 在有界进程内账本中进行 compare-and-set；
相同幂等键只有一个有效执行者。冻结、风险变化和 DeepSeek 补审高于普通行情，拥有独立
保留容量；队列满时只能合并普通行情。进程崩溃后的冻结恢复只依赖正式快照和冻结检查点，
不通过普通事件流水重放。

每个调度边界按 `trade_date + schedule_point + strategy` 维护
`pending/inflight/retry_wait/completed/missed`，只有入队成功后才进入 `inflight`，
只有业务处理及必要持久化完成后才进入 `completed`。临时失败按 1/2/5/10/30 秒退避，
重试必须复用同一个冻结对象、ID、规范载荷和 SHA-256；内容冲突是终态。事件同时携带
交易 session generation 和交易日，跨日、系统时钟回拨、wall/monotonic 偏差或明显休眠
跳跃会轮换 generation，迟到的旧 generation 结果不得发布、冻结或更新当前状态。

最终 V2 运行只接受当前 runtime schema，不对旧 schema 补默认值，也不保留旧执行模式开关；
回退只能切换完整 release、配置与运行目录。

TopK 展示报价使用独立单 worker、单 latest-wins 待处理槽，不能因合并器正在处理
`close_quotes`、参考数据或 DeepSeek 而排队；它只提交与当前 V2 decision/projection version 匹配的报价
overlay。long 使用另一条独立单 worker、单 latest-wins 待处理槽，按固定 watchlist
定向抓取腾讯报价并直接生成当前观察投影；该通道不得进入候选、新闻、风险、参考数据、
三策略评分、DeepSeek、共享 TopK、冻结、历史或结算资源，慢 long 请求也不得占用或
阻塞 tomorrow 的事件合并与评分容量。候选报价、市场新闻或公司风险输入完成后才提交
评分事件，同一交易日、阶段和策略输入链的待执行评分使用 latest-wins，只保留最新
`data_version`；输入在评分最小间隔内到达时必须保留一个 latest-pending 身份，并在间隔到期时
提交一次最新评分，不能直接丢弃。评分读取返回的市场与候选特征元组必须按实际内容形成不可变、可寻址的
`ScoringInputEpoch`；读取期间又有更新输入时，只禁止把该元组写入新版本缓存，不得作废已经形成的评分输入。
已经开始的评分必须完成并可先发布 local；若已有更新 pending，则跳过旧结果的可选 DeepSeek 复核，随后运行最新
pending，由统一索引 sequence/CAS 防止倒退。三条板块评分通道各保持一个 worker，待处理
请求按策略隔离；同一策略的新 epoch 只能替换本策略尚未开始的旧 epoch，不得覆盖
tomorrow、today 或 d25 的其他策略请求。多个策略同时等待时优先执行 tomorrow，再执行
d25 和 today；完成一个策略后从下一个策略继续轮转，持续输入不得饿死其他策略。候选和
评分事件的排队过期时间分别包含上游
20 秒和 23 秒最坏等待，但实际开始执行后仍重新截断为各自 3 秒和 15 秒 I/O/计算预算，
排队预算不得被外部调用消费。物理数据任务的 deadline 从 worker 实际开始时刻计算，不能从调度计划时刻
提前消耗；TopK 整体预算固定为 2 秒，腾讯 HTTP timeout 还必须截断到剩余 deadline。

同一调度观察点的 today、tomorrow 和 d25 必须以 single-flight 复用一次全市场读取和一次
有界候选报价刷新；每板候选上限分别应用后再合并请求，不得把 `candidate_pool_size` 当成
三板合计上限。共享批次只从已经恢复的历史、结构化研究和分钟尾部缓存组装本地评分输入，
不得在三条策略 lane 内同步补抓历史或公司研究。tomorrow 可读取同批已缓存分钟尾部，但
today/d25 不得等待其策略特有投影。本轮共享输入失败时停止对应构建，保留最近有效决策，
禁止继续调用无批次的决策构建器并把一个刷新错误级联记成第二个通用决策错误。
三策略构建评分输入时，普通候选特征只能生成一份不可变公共 batch，Tomorrow 额外生成一份带分钟尾巴的
变体；D25 和 Today 必须复用公共 batch。local 决策发布后，DeepSeek 升级提交到按策略隔离的
latest-wins lane；模型网络等待不得占用本地评分 lane，旧 hybrid 返回时仍由统一索引 CAS/序列规则拒绝。
14:50 封口仍只接纳边界前已经完成并已发布的最新合法 current；在途更新结果由冻结 CAS 拒绝，不能改写 formal。

周期 `stock_risk` 事件只把当前正式/观察输出代码和候选代码按优先级交给公司研究协调器，
不得在合并线程内等待外部研究。相同代码在运行批次和待处理队列中合并；新优先代码只插入
后续批次，不能取消已开始批次。批次截止时先接纳所有已经完成的股票，只把未完成股票标记
为 `deferred`，禁止因为一只慢股票丢弃整批成功结果。完成且数据版本变化的股票以 risk
优先级触发本地重算；冻结 CAS 继续拒绝迟到结果。

活动生产计划器不得在每次 scheduler tick 直接提交公司研究。研究意图只允许来自周期
`stock_risk`、盘中本地正式/观察集合的新进入代码，以及收盘恢复的显式请求；同交易日已经
处于正式/观察集合的代码不能因普通报价或评分发布而反复成为“新进入”。新进入代码触发时
本地快照必须先发布，不能等待外部研究；V2 首轮异步模型复核暂缓到该研究
批次返回并完成一次 risk 重评分，研究全失败也必须释放该屏障并以显式降级继续，禁止为同一
初始输入重复消耗 DeepSeek 预算。

公司研究协调器按股票维护 60 秒成功冷却，部分、失败、延后或无终态结果按
60/120/240/480/900 秒指数退避，退避状态最多保留 2048 只。若一个探测批次没有任何完成
股票，则本轮剩余待处理股票不再逐批调用来源，统一进入当前全局退避窗口；窗口内后续意图
只更新有界门控状态，不创建研究批次。状态 API 必须加法暴露冷却股票数、退避股票数、
下一次重试秒数、被门控代码数、短路批次/代码数和门控状态淘汰数；这些门控只减少重复
外部 I/O，不得把缺失研究解释为无风险，也不得清除最近有效风险事实。

候选预选与正式评分读取同一份全市场特征时必须复用行情携带的不可变 `merge_epoch`。
板内横截面缓存以 `trade_date + phase + board + data_version + merge_epoch` 为身份，命中
后只保留总体参数和参考分布，不得缓存或再次序列化约 5500 行完整特征对象；正式评分
只把本轮有界候选投影到该总体并直接执行纯本地公式，候选报价版本仍由上游输入身份校验。

## 5. 交易日与时间线

所有业务时间使用带时区的 `Asia/Shanghai` 时钟，时钟必须可注入。窗口左闭右开；
交易日由 A 股交易日历确定，日历失败时仅可使用带版本、仍有效的本地缓存。没有可靠缓存
时交易 session 必须 fail closed 为 `calendar_unavailable`，Web 和只读状态仍可启动，
但不得抓行情、评分或冻结；后台按 30/60/120/300 秒上限退避重试，并在状态 API 暴露
交易日、calendar state、下一次重试、phase、generation 和 discontinuity reason。

| 时间 | 行为 |
| --- | --- |
| 09:15-09:30 | 共享预热，并以 2 只候选执行一次 DeepSeek 健康 canary |
| 09:30-09:36 | today 观察并复用 canary；tomorrow、d25 生成本地草稿，不新增模型请求 |
| 09:36-10:30 | today 主执行；tomorrow 获得上午重点复核，三策略按 facts 身份单飞并跨策略复用 |
| 10:30-11:18 | today 提高动作门槛，只补审新风险、动作门槛和 TopK 边界 |
| 11:18-11:20 | today 停止提交新模型请求，只接纳 deadline 前完成的在途结果 |
| 11:20 | today 对 11:19:59 及此前的最新决策原子封口并写入正式记录；无可冻结稿则当日保持 `not_ready`，之后只更新已有正式记录的报价 overlay |
| 11:30 | 验收 today 正式记录已入库；持续运行中的同一冻结写入若暂时失败，只允许幂等重试原冻结对象 |
| 11:20-13:00 | 保留上午 tomorrow/d25 草稿；若服务午间冷启动且当日草稿缺失，只补一次本地草稿和 long 当前快照；之后以 10 秒全市场、10 秒候选和 10 秒 TopK 报价维持展示 |
| 13:00-14:20 | tomorrow/d25 增加尾盘数据，只对 facts 身份变化执行增量复核 |
| 14:20-14:46 | tomorrow/d25 新入围、风险、证据和冻结边界变化补审 |
| 14:46-14:48 | 停止提交新模型请求，只接纳 deadline 前完成的在途结果 |
| 14:48 | 拒绝全部新的生产 DeepSeek 请求 |
| 14:49:20-14:50 | 调度器自动尝试为 tomorrow/d25 保存距边界不超过 30 秒的同运行身份检查点；候选报价、新闻或风险输入完成后仍可按 1 秒最短间隔触发纯本地评分，最终报价刷新和评分发布均须在 14:50 前完成 |
| 14:50 | 正式冻结 tomorrow、d25；long 只发布当前观察 |
| 15:00 后任意时刻 | 已有正式记录只保存收盘 overlay；仅缺失的 tomorrow/d25 优先固化本进程 V2 current，冷启动才按完整收盘输入本地补算一次 `close_fallback`；公司研究按缓存和有界批次继续补齐，不设 15:10 绝对停止点 |

当前视图不把上一交易日结果伪装为当日结果。tomorrow、d25 从 09:30 起与 today 共用
候选和板内总体并先发布本地草稿；09:36 后三策略按原始 facts 缓存身份单飞，同股同证据
只允许一次物理请求，随后按策略本地投影，tomorrow/d25 因而可在上午发布 hybrid。
DeepSeek 普通目标/硬上限固定为 36/66 次，其中 tomorrow 为 21/38、today 为 5/8、
d25 为 8/16、共享预热为 2/4；5 次 emergency 独立于普通 66 次上限，计划内最坏为 71 次，
全局灾难保护上限仍为 168。
13:00 后刷新 tomorrow 尾盘分钟数据，只有证据、结构化风险或受控价格反映身份变化才
执行增量复核；14:50 前均为可替换草稿。
任何时段确无达到门槛的候选仍允许返回真实空结果，但不得因阶段路由直接跳过两种策略。
若 11:20-13:00 冷启动且当日 tomorrow、d25 或 long 当前快照缺失，输入完成事件允许
各补算一次；运行时以同日 current 或同日本地草稿作为短线恢复完成证据，以已接纳的 Long 刷新
handoff 作为 Long 恢复完成证据。策略 lane 已运行或已有待处理请求时不得重复排队；刷新在形成任何
输出前失败时允许下一调度 tick 重试。午间恢复使用独立 `midday_recovery` 身份并强制关闭模型复核，
不得追补 Today 或新增 DeepSeek 请求。

持续运行的服务错过单次调度 tick 时可以幂等补提交已经在边界形成的同一冻结对象；
服务启动时不得用检查点追补已经错过的 today 11:20 冻结。tomorrow/d25 仍可在启动时
仅于 14:50（含）至 15:00（不含）使用 14:49:20（含）至 14:50（不含）形成、距边界不超过
30 秒且哈希有效的检查点恢复 14:50 冻结；15:00
及以后启动直接进入收盘恢复判断，不再补发 afternoon freeze、14:48 cutoff 或最终报价。
deadline 后返回的数据只记脱敏审计，不能
更新已有正式记录或冻结 JSON。15:00 后仅当 tomorrow/d25 某策略同日正式记录
不存在时进入收盘恢复：本次进程已有 V2 current 时保持股票、评分、动作和排名，只替换收盘
锚点；冷启动先读数据库，仍缺失才以一份完整同日收盘行情执行本地筛选、三板评分与 TopK，
不新增 DeepSeek HTTP。行情或三板批次不完整时不写半成品，按 3/5/10/20/30 秒退避重试。
该恢复规则以“程序实际启动时刻”为起点：15:00、15:10、19:30 或更晚启动都执行相同
数据库/V2 current/收盘恢复判断，不把 15:10 当作运行截止。收盘本地补算在候选形成后优先提交一批
公司研究并最多等待 40 秒，随后使用已完成和最近有效缓存继续本地评分；未完成分项显式降级，
不得阻塞已有正式记录、只读 Web 或后续后台补齐。

## 6. 数据服务、刷新与降级

### 6.1 来源职责

| 来源 | 职责 |
| --- | --- |
| 上交所/深交所 | 免费官方证券主数据：代码、板块、交易所、上市日期；独立于实时报价 deadline 原子刷新 |
| 东方财富 | 免费全市场、分钟和历史基础行情；全市场路由主来源 |
| 新浪 | 免费全市场延迟对冲与故障回退；已启动的第二路只作异步校验，不阻塞规范快照发布 |
| 腾讯 | 候选和 TopK 定向实时报价；默认提供完整前复权日线历史主来源 |
| Tushare | 120 积分档只执行低频 `daily` 单证券未复权能力审计和来源健康；更高积分 qfq 能力必须按配置显式启用 |
| AKShare | 行业、新闻、公告和候选级研究数据 |

多种行情来源不等于证券主数据存在同等冗余供给。东方财富、新浪、腾讯和 120 积分 Tushare 的活动职责
主要是价格、历史或来源健康；其中新浪、腾讯不拥有全市场上市日期契约，当前 Tushare 权限也不能调用
`stock_basic`。代码、交易所、板块和上市日期由上交所/深交所官方证券主数据通道独立负责，东方财富
富身份只可作为同语义补充；价格来源成功不得被解释为证券身份也已完整。官方通道必须与全市场报价
deadline、连接池和失败域隔离，完整快照原子替换最近有效主数据，失败时继续使用最近有效快照并公开
覆盖缺口。候选规模 360 和历史缓存中的 120 条旧资料都不是证券主数据产品常量，也不得成为成功门槛。

活动行情路由不得接入或自动尝试收费行情源。沪深交易所、东方财富、新浪和腾讯只使用无需订阅的公开
接口；Tushare 仅保留免费 120 积分账户可用的日线能力审计，AKShare 仅聚合公开数据。
免费接口的可用性和结构不构成供应商 SLA，因此必须依靠有界超时、最近有效快照和显式
降级，而不能以收费源作为隐藏兜底。

通达信/mootdx 未通过连续真实样本和权威文档准入前只允许作为独立影子能力探测对象；
不得写入 `source_contract_versions`、组合根、生产行情路由、评分、冻结或 Web 只读查询路径。
影子关闭后活动业务语义必须完全不变，候选和 TopK 继续只用腾讯定向报价及统一行情回退。

Tushare SDK 是默认运行依赖，HTTP 协议固定向官方 API 根地址提交 `api_name=daily`，
Token 缺失或供应商明确返回无接口权限时按永久来源降级，不得把权限拒绝当作限流紧密重试；
transport timeout 固定 8 秒，
`runtime.json.market_data.tushare.points` 明确声明积分档。当前固定为 120，官方权限为每分钟 50 次、
每日 8000 次且仅限非复权日线。活动参考 lane 只对固定代码 `000001` 调用 A 股 `daily` 单证券接口，
复用 `daily_history` 的 6 小时缓存；显式诊断一次最多 50 个代码并逐证券请求，禁止把逗号拼接代码误作
一个合法 `ts_code`。客户端按上海自然日和滚动 60 秒执行进程内 8000/50 次失败关闭门禁，公开
`process_*` 计数明确不伪装成跨重启供应商账本。不得调用需要 2000 积分的 `stock_basic`、`trade_cal`、
`adj_factor`/`pro_bar(qfq)`、`daily_basic` 或财务指标接口。项目根目录受保护文件
`.token_key` 以 `DEEPSEEK_API_KEY=...`、`TUSHARE_TOKEN=...` 两个独立赋值保存凭据；
DeepSeek 和 Tushare 各自仍以同名环境变量优先，其次读取对应 `*_FILE`，最后读取
`.token_key`。POSIX 下拒绝 group/other 可读文件，未知键、重复键、空值和超大文件
拒绝启动。密钥、Token、完整请求或响应不得写入配置、日志、SQLite、快照或 API。
当前 120 积分 Tushare 路径只产生未复权 `daily` 能力观测和来源健康，不进入
活动候选、收益/均线/波动/回撤因子、公司风险研究或最终评分；因此“已配置/已调用”
不等于当前推荐由 Tushare 数据驱动。

### 6.2 刷新频率

| 数据 | 09:15-09:30 | 09:30-10:30 | 10:30-11:20 | 13:00-14:20 | 14:20-14:48 | 14:48-15:00 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 全市场行情 | 10秒 | 10秒 | 10秒 | 10秒 | 10秒 | 14:49:50一次 |
| 每板最多120只候选报价 | 2秒 | 1秒 | 2秒 | 2秒 | 1秒 | 1秒 |
| TopK展示报价 | 1秒 | 1秒 | 1秒 | 1秒 | 1秒 | 1秒 |
| tomorrow 分钟 tail | 停止 | 停止 | 停止 | 5秒 | 3秒 | 停止 |
| long固定池定向报价 | 1秒 | 1秒 | 1秒 | 1秒 | 1秒 | 1秒；15:00最后一次 |
| 输入驱动本地评分最短间隔 | 10秒 | 3秒 | 5秒 | 5秒 | 3秒 | 1秒（仅本地，14:50停止） |
| 行业热度 | 120秒 | 60秒 | 60秒 | 60秒 | 60秒 | 停止 |
| 市场新闻 | 120秒 | 60秒 | 60秒 | 60秒 | 60秒 | 仅展示 |
| 个股公告/风险 | 300秒 | 180秒 | 180秒 | 180秒 | 120秒 | 后台有界补齐；已有正式记录不可改写 |
| 财务/历史因子 | 盘前一次 | 缓存 | 缓存 | 缓存 | 缓存 | 收盘后更新 |

午间暂停评分，但三个报价任务仍按配置低频运行：

| 阶段 | 全市场行情 | 候选报价 | TopK 展示报价 | long 固定池报价 |
| --- | ---: | ---: | ---: | ---: |
| 午间 | 10秒 | 10秒 | 10秒 | 10秒 |

候选池按板内预选执行，每板最多 120 只、三板合计最多 360 只；运行配置中的
`candidate_pool_size=120` 是单板上限，候选报价缓存容量为 360。

频率只能来自 `runtime.json.pipeline.cadence_seconds`。表中数字是最短计划间隔，不是强制
并发数；本地评分只由候选报价、市场新闻或个股风险输入成功完成触发，`score` 间隔只负责
合并短时间内的重复触发，调度 tick 不得独立生成周期评分。14:48 后停止 DeepSeek，但上述输入
仍可触发 tomorrow/d25 纯本地评分，直至 14:50 封口。周期任务在途时跳过本周期，从当前时刻重算下一次，
不排队补跑；每个来源 lane
和 versioned 待执行评分只保留最新观察点。因此实际刷新周期自动取“不小于配置下限且接口
能够持续完成”的最快速度，接口变慢或熔断时不得堆积请求，恢复后自动回到下限。
组合根必须实例化同一个 `CadencePlanner` 并注入活动调度器；全市场、候选、TopK、分钟 tail、
long、评分、固定时点冻结和收盘恢复分别进入可停止、可观察的 latest-wins lane。
`submit_due()` 返回计划器计算的真实最近到期时间，supervisor 不得再以固定 30 秒 tick 限制物理采集。
13:00 后 tail lane 实际刷新 Tomorrow 候选的分钟尾部缓存，失败保留最近有效 tail；14:50 冻结至
15:00 以及 15:00 后已有正式记录只允许 TopK 入选代码 overlay 继续刷新，不再提交全市场、候选或评分。

全市场每轮固定采用有界延迟对冲：先提交东方财富；若 1 秒内已取得覆盖达标且截止前完成
的结果，则不启动新浪；若东方财富失败或 1 秒仍未完成，立即提交新浪。任一来源先返回
完整有效结果就原子发布统一全市场索引，不等待另一来源、也不把两份约 5500 行全市场响应串行合并。
另一任务尚未开始时取消；已经发出时允许在本轮 deadline 内完成缓存和来源健康校验，但
迟到结果不得改写本轮规范快照。东方财富和新浪分别使用 8 秒单次 I/O timeout，共享的
全市场事件硬截止仍为 20 秒。单轮全市场分页共用一个 HTTP session 和连接池；deadline
或对冲取消信号到达后不得再发出尚未开始的页请求。页数、并发数和响应覆盖继续受适配器
固定上限约束。

服务在任意日首次启动都必须在恢复最近有效快照后异步刷新一次官方证券主数据；交易日任意活动阶段
首次启动还必须补交易日历初始化，不得只在 09:15-09:30 执行。官方证券主数据拥有独立
`exchange` lane、每次 HTTP 15 秒上限、有限重试、24 小时成功 TTL 和 300 秒失败退避，不与东方财富/
新浪 20 秒实时报价 deadline 竞争。全市场任务只抓取、统一并原子发布实时行情及已缓存历史特征，
不得在 20 秒 deadline 内同步抓取整批历史。缺失历史按主板、创业板、科创板均衡的稳定
顺序交给独立历史池分批预热。120 积分 Tushare `daily` 明确标记为 `raw`/
`unadjusted_daily`，只可用于来源能力审计，不得进入收益、均线、波动、回撤、ATR 或其他
需要复权的历史特征；历史预热直接使用腾讯完整日 K qfq 主来源，东方财富 qfq 为第二
回退。腾讯显式 qfq 请求若返回 `day` 而非 `qfqday`，只有返回窗口内逐行公司行动元数据为空且两个调整标志均为零时，
才可将该窗口视为与 qfq 等价；任一行缺少证明或出现公司行动/非零标志均失败关闭，不得把一般未复权 `day` 标记为 qfq。
只有 Tushare 明确支持 `pro_bar(qfq)` 时才可进入历史特征缓存。单只缺失不能回滚
同批成功结果，下一周期只续跑未覆盖代码；成交量和成交额分别按供应商原单位显式换算。每批完成
后只链式提交尚未尝试且不在冷却期的下一批；失败代码按 60/120/240/480/900 秒逐级进入
逐股负缓存，成功后清除失败状态，冷却到期前不得再次提交，也不得阻止后续代码继续预热。
固定 360 个历史预热槽只分配给主板、创业板和科创板，按稳定轮询分别最多保留 120 个；
`unsupported` 证券不得占用历史槽或进入远端历史重试。历史预热必须先保证每个活动板块
具备至少 100 个可用横截面样本，不能把四类证券平均成每类 90 个而使板块可靠度永远低于
可执行门槛。
候选代码的异步历史补齐只能由 `HistoryWarmup` 按上述批次和截止控制；证券主数据/交易日历
参考刷新不得再向 history lane 提交整批重复任务。批次截止只取消尚未完成的单股请求，截止前
已经完成且通过 qfq/schema 校验的单股结果必须先原子写入缓存和数据平面，再由预热协调器仅对
未覆盖代码登记退避，不能因一个慢尾丢弃整批成功结果。
单股最多 61 条最近历史记录在数据平面必须使用一个原子批量事务写入，不能为每个交易日重复执行
schema 初始化、连接和事务提交；不同股票仍逐只提交，使先完成股票在同批慢尾超时前可独立恢复。
冷启动不读取任何旧运行目录或旧历史缓存，只恢复当前 V2 数据仓储中通过 schema、来源版本、
交易日和内容哈希校验的最近历史特征记录。覆盖不足的代码再从腾讯主源、东方财富回退源读取
最多 61 根 qfq 日线，计算 MA60、60 日收益锚点和其它历史摘要；最近 20 根原始日线只作为
有界热数据，紧凑历史记录通过数据平面仓储供重启恢复。历史槽最多 360 只，原始热数据最多
7200 根；远端失败按来源降级并保留最近有效记录，不把旧库、游标或损坏记录伪装成有效历史。
若服务在 14:50 后首次启动且统一当日报价索引为空，首个后台 tick 必须单次恢复全市场
当日报价索引，供历史表“今日涨跌/锚点至今”和收盘展示读取。15:00 后收盘恢复协调器
先逐策略读取正式记录；同日正式记录仍缺失时复用该收盘批次执行本地筛选、评分与冻结
写入，不调用 DeepSeek。若后续重试时进程内已有完整、同日、三板历史样本达标且未被
可靠度/样本错误标记的收盘全市场缓存，必须复用该缓存继续本地补算，不得反复同步抓取慢
全市场来源；`close_quotes` 有界执行预算必须覆盖一次慢收盘来源返回和本地冻结写入。失败时
保留 `null`/`not_ready` 并后台退避重试，HTTP 查询仍不得现场抓行情、评分或写盘。

### 6.3 实时性与失败策略

每条观测保存来源、主体、观察/来源/接收/生效时间、响应版本、字段、缺失原因、载荷
哈希、终态和脱敏错误码。未来、无时区、空版本、非法代码、非有限核心值和 deadline
后完成的观测不得进入有效缓存或发布。

目标为：TopK 关键阶段 P95 年龄不超过 2 秒、其他阶段 5 秒；候选主执行 5 秒、
其他阶段 10 秒；全市场主阶段 10 秒、其他阶段 15 秒；SSE 发布 2 秒；today 行情到
评分发布 15 秒。数据年龄超过 cadence 2 倍标记 stale，超过 3 倍标记 degraded；
today 行情超过 20 秒或尾盘冻结行情超过 30 秒只能观察。

Web 对未冻结 current 快照的内存保留窗口固定由
`runtime.json.api.web_snapshot_retention_seconds` 注入，当前值为 35 秒：覆盖活动生产链实测约
30 秒的稳定刷新间隔，并额外保留 5 秒调度、SSE、请求和浏览器抖动余量。该窗口只用于策略切换或
短暂请求失败时继续显示最近同日有效快照，不得改变行情来源时间、数据年龄、stale/degraded 判定、
策略 cadence 或物理采集频率；无效或缺失配置必须在启动校验阶段拒绝，浏览器不得再维护独立硬编码值。

全市场截止 20 秒、候选报价 3 秒、公司研究单端点 8 秒、公司研究批次 40 秒、DeepSeek
单次网络 timeout 20 秒。历史预热固定每批最多 30 只且不得超过历史池 5 个 worker，并由组合根按当前
来源最坏尝试次数、worker 数和单请求 timeout 注入从实际批次提交时刻起算的硬截止，当前生产批次为
5 只、上限 20 秒；批次预算的 90% 供腾讯一次与东方财富三个 host 的最坏串行路由使用，因此历史客户端
单次 HTTP timeout 从配置的 12 秒截断为 4.5 秒，剩余 2 秒用于结果校验、内存提交和 SQLite 批量持久化。
截止后取消尚未开始的逐股请求、
释放该批在途身份并按既有逐股退避继续轮转未尝试代码，禁止单个慢尾永久阻塞后续三板覆盖。
当前免费历史链每只最多尝试腾讯一次及东方财富三个 host 各一次；东方财富全市场路径原有
双轮容错不受历史预热截止约束影响。
公司研究的 40 秒预算属于独立协调器，不得继承周期事件的排队年龄。用于候选
发现的周期性全市场任务必须等待本轮物理刷新完成，不能消费 stale-while-revalidate 返回的
上一轮快照；该缓存模式只允许用于展示或来源失败时的显式降级。全市场成功后
即形成可读统一行情；候选发现阶段以本轮 `received_at` 判断是否赶上当前周期，不能把数据源
最近成交时间误当网络接收时间而整批淘汰；进入候选复核和评分后仍按原始 `source_time`
执行 20/30 秒可执行性约束。历史预热属于独立进度，只有具备所需历史的代码进入候选评分，不能因
尚在预热而把全市场事件记为 expired。来源连续失败
3 次熔断 30 秒，半开只允许一个轻量探测：东方财富只探测首个全市场页，新浪只探测证券
计数，探测成功后才允许完整分页。熔断期间不得周期性提交完整全市场分页。缓存支持
fresh、stale-while-revalidate、
degraded、负缓存和 single-flight；刷新失败保留最近有效值及原始时间，不得用失败条目
覆盖。全源失败也返回最近有效统一快照，并增加降级原因。

所有外部适配器共用结构化失败分类：`timeout`、`deadline`、`circuit_open`、
`negative_cache`、`cancelled`、`superseded`、`no_data`、`rate_limited`、
`schema_invalid` 和 `source_failed`；分类只保存供应商、操作、是否可重试和有界类别，禁止
持久化密钥或完整异常文本。行情以 deadline、熔断、负缓存和 latest-wins 取消共同控制；
DeepSeek 以单次 timeout、物理预算预留、提交 deadline 和 schema 终态控制，失败不阻塞
本地推荐。不同适配器不为追求表面一致而增加无配置依据的隐藏重试或后台线程。
来源健康必须分别统计 `physical_failure_count`、`timeout_count`、
`circuit_skipped_count` 和 `superseded_count`；熔断跳过不等同于一次新的物理失败，
不得反复增加连续失败或错误计数。

严重公司风险注册表复用结构化研究缓存的原子 JSON 生命周期，保存类别、公告/结案时间、
稳定证据 ID、官方披露来源和注册表版本；刷新时按事实身份合并，来源失败不得删除旧命中。
状态 API 至少暴露覆盖证券数、事实数和版本集合。注册表不可用或覆盖未完成时，本地评分和
统一决策发布继续，未被已确认事实阻断的受影响证券只能观察，并显示
`corporate_risk_history_unavailable`。
CNInfo 增量链作为公司风险登记簿的独立数据平面写入者，按 `cninfo.announcements:{code}`
保存来源游标，按公告 ID 保存 `cninfo-announcement:*` 风险证据，并按组件保存
`cninfo-risk-component:*` 四态覆盖状态。该链路不进入 `MarketSourceCoordinator`、
`MarketDataGateway` 或 `source_contract_versions`，不参与行情路由、冻结触发或 HTTP 只读
请求；同步失败、DataPlane 写入失败、空页或重复页都不得清空既有风险事实。交易所公告交叉
校验尚未作为正式来源接入时，CNInfo 证据保留 `exchange_cross_check_status=pending`，
只作为结构化官方披露风险事实和覆盖降级依据。
结构化研究按分项复用原子原始载荷：财务、质押和解禁缓存 6 小时，公告缓存 10 分钟；
整只股票的已解析研究观察缓存 10 分钟。刷新只请求已到期分项，任一分项失败时保留该分项
最近有效观察及原始点时，其他已完成分项照常入缓存。状态 API 同时暴露研究批次运行/待处理、
完成/部分/失败/延后计数，以及财务、公告、质押、解禁四项覆盖数。
东方财富公告适配器必须按供应商实际单页上限执行有界分页，使用稳定公告 ID 去重并把完整聚合
载荷原子缓存；已有完整基线时，刷新首页后可与基线增量合并，只有合并后的有效点时记录覆盖
`total_hits` 才能标记公告历史完整。分页失败、超过有界页数、未来或畸形记录均不得把公司风险
历史猜成完整，也不得清空最近有效风险事实。

进入统一数据平面热路径的 provider adapter 固定分为三个显式步骤：
`transform_query` 只规范代码、市场、日期、分页和非敏感请求指纹；
`extract_data` 只执行带 timeout/deadline 的物理 I/O 并返回供应商元数据；
`transform_data` 负责严格 schema、单位、时区、缺失原因和字段血缘，不能计算策略分。
查询必须绑定数据集、来源、主体、请求字段、请求/deadline 时间和来源契约版本；原始载荷
保留接收时间、字段血缘和缺失原因；报价保留来源/接收时间与数据版本；列式批次再绑定
merge epoch、配置/schema、manifest 和内容哈希。无时区时间、非法代码、空版本和非有限
核心值在 adapter 边界拒绝；未来观测继续由统一观测边界拒绝。缺失使用 `null`，不得用
0、空字符串或 `NaN` 冒充观测。

候选实时报价事件只刷新腾讯报价并立即交还事件线程，不得同步等待整批尾盘分钟线；
tomorrow 评分的数据准备阶段按需加载/读取分钟线并受评分预算约束。分钟线 I/O 在不改变
候选集合、候选分和最终返回顺序的前提下，按既有候选分保持板内顺序并在主板、创业板、
科创板间稳定轮询，使有界批次先覆盖三板高分候选。这样 1 秒 TopK 与
1-2 秒候选计划不会因慢分钟历史长期保持 `inflight`，实际完成速度仍受供应商响应限制。
13:00-14:50 的输入触发评分以及最终复核/收盘评分在读取 tomorrow 特征缓存前，必须对当前
候选执行一次有界尾盘分钟刷新；上午 tomorrow 草稿不抓尾盘分钟线，相关字段按缺失规则
保持中性并显式标记，不能伪造尾盘观测。刷新复用缓存并受 3 秒市场任务预算约束，失败不得
阻塞本地评分。
尚未开始即被取消的分钟请求不得写入负缓存，下一轮评分必须能够继续推进；只有已经发出且
超时的物理请求才进入冷却。

### 6.4 确定性统一行情

本轮首个完整有效全市场来源形成规范基线；只有已经实际完成并被本轮接纳的来源观测参与
统一，不要求为字段回退等待另一份全市场响应。来源观测按股票一次索引，目标复杂度
O(S*N)。先按 `source_time`、`received_at` 选择
更新字段，同时点按来源优先级；`data_version` 只比较同来源同时点，最后仅用载荷哈希
消除输入顺序差异。实时字段同时点优先东方财富、再新浪；新鲜候选/TopK 字段优先
腾讯；慢数据不能覆盖更晚实时价格。腾讯定向批次只返回部分请求代码或整批暂时失败时，
未刷新的代码必须继续使用同一规范快照中的东方财富/新浪全市场行情或最近有效报价，
不得因定向来源缺失清空 Web 已有价格；全市场路由中东方财富失败而新浪成功属于已完成
兜底，状态可以标记来源降级，但不得误报为整条行情链路失败。
字段合并只接受已准入来源的允许字段，来源别名先归一到基础来源；未准入影子来源即使提供
实时价格也不能进入规范行情、候选评分或冻结输入。

已取得的来源间价格偏差不超过 0.50% 可通过。超过时，腾讯定向价必须与东方财富或新浪至少
一个观测偏差不超过 0.50% 才算复核，不能用腾讯与自身比较。相同有效输入无论完成
顺序均产生相同字段来源、冲突、缺失、合并 epoch 和规范 JSON 哈希。合并 epoch 基于
已接受观测的代码、来源、点时、版本、载荷哈希、缺失原因和定向范围生成，不得为了生成
epoch 再序列化整份投影报价；规范 JSON 哈希仍覆盖最终快照内容。

### 6.5 tomorrow v2 实时验收

目标链路以系统实际接收时间和浏览器 Performance API 计量内部时延，固定验收线为：

- 已接收行情到本地预览提交 P95 不超过 5 秒；
- 本地预览到浏览器完成渲染 P95 不超过 1 秒；
- 全市场决策数据年龄 P95 不超过 10 秒；
- DeepSeek 融合结果在本地预览后 P95 不超过 15 秒。

交易时段启动后 15 秒内必须产生第一版可解释的 local 预览；历史或研究增量随后只能换入
新 epoch，不得让 Web 等待整库预热。公开来源尚未响应、时间戳过旧、覆盖不完整或结构
冲突时，状态必须分别暴露 source delay、stale、coverage gap 或 conflict，不能把外部
等待时间伪装成内部达标。14:50 冻结只接受截止前已完成且满足数据门的最新
`ScoredDecision`；截止后完成的行情和模型结果只可用于报价 overlay 或下一交易日研究。

## 7. V2 内存、缓存与性能契约

### 7.1 固定资源上限

内存验收同时约束规范缓存逻辑载荷和整个进程峰值 RSS，二者不得互相替代：

| 指标 | 上限 | 约束 |
| --- | ---: | --- |
| `cache_logical_bytes` | 248 MiB（260,046,848 字节） | 所有有界缓存的规范载荷、身份和计费内容 |
| `process_peak_rss_bytes` | 384 MiB（402,653,184 字节） | Python、原生缓冲、线程栈、队列、临时副本和全部活动 V2 对象 |

384 MiB 是进程硬上限，不是缓存容量；不得把 136 MiB 差额分给缓存、扩大候选、延长 TTL
或保留额外历史。验收必须同时记录逻辑缓存、RSS 峰值和可用时的 USS、Python traced、
Polars 估算及瞬时峰值原因；不得用 Python 分配量或逻辑缓存估算代替 RSS 峰值。配置必须
分别使用 `cache_logical_bytes` 与 `process_peak_rss_bytes`，旧单字段配置启动前拒绝。

缓存身份固定包含数据集、来源、主体、请求指纹、交易日、阶段、来源契约、配置和 schema
版本。TTL 条目具有动作年龄、负缓存和容量；epoch 只命中完整身份；任一分区满只拒绝本区
新写入，不能清空其它分区。当前决策、正式记录和 overlay 的驻留容量必须以 V2 类型和查询
需求核算，不得用旧 Pipeline 阶段编号定义所有权。

### 7.2 性能与背压门禁

每条异步 lane 都必须有固定并发、单 latest-wins 待处理槽、deadline、替换计数和有界停止；
旧输入可以完成审计，但 compare-and-set 不得覆盖更新的索引或正式记录。依赖范围无法证明时
扩大重算范围，不能用近似桶跳过真实变化。

实时生产链必须以不可变数据版本而不是调度时间驱动重算。每个刷新任务返回有类型的变化结果，
至少记录任务、是否改变评分输入、数据版本、变化代码、完成时间和是否使用最近有效回退；无变化、
超时后回退旧快照或相同研究版本只能更新健康状态，不得触发新评分。
刷新完成时间必须先按绝对时刻选取调度请求、本地特征观察和报价接收中的最晚值，再显式投影为
`Asia/Shanghai`；供应商或基础设施返回 UTC 不得使已经成功发布的行情刷新在结果对象构造阶段失败，
也不得通过放宽应用层上海时区类型契约规避该边界。
候选报价刷新产生的完整基础候选特征必须作为共享 `ScoringInputEpoch` 直接复用，Today 与 D25 共用
基础 epoch，Tomorrow 只按同一基础 epoch 追加尾盘分钟差量；禁止刷新后丢弃特征再从缓存重复构建，
也禁止以 `observed_at` 制造相同数据的新缓存身份。运行中评分在数据准备完成、纯本地评分开始和发布前
都必须检查是否已被更新输入替代；已替代任务不得继续发布、请求 DeepSeek 或形成冻结输入。

360 只腾讯候选报价允许按每板上限形成最多三个有界分片并发请求，共用原任务 deadline、来源 lane、
取消信号和统一规范快照；部分分片失败保留对应股票最近有效全市场或定向报价，不能清空整批。
TopK 路径只复用定向刷新已经返回的报价特征，不得立即二次构建完整候选特征。历史预热槽在同一交易日
保持稳定且三板轮询；单批不得超过历史池实际 worker 数，预热自身不得形成第二个 worker 波次，不能
因其内部排队尚未启动就消耗 batch deadline 并进入失败退避。每只已完成历史必须立即进入缓存与
覆盖统计，不能等待同批慢尾结束后才可见。

同一有界 `LatencyWaterfall` 必须贯通来源排队、规范化、合并、候选提交、评分排队、数据准备、
本地评分、统一发布和 SSE 入队。公开状态只投影聚合计数、P50/P95/最大时延和 market change 计数，
不得暴露 correlation id、证券代码、原始载荷或内部缓存内容。decision SSE 默认携带可直接应用的
完整 TopK replacement patch；浏览器仅在 schema、基础身份或 TopK 校验失败时执行条件 GET 重同步。

固定 P95 上限为：5500 行标准化 250ms、两源合并 600ms、统一快照可读 900ms、360 行定向
报价提交 100ms、单板 120 候选预选 250ms、单板单策略评分 250ms、三板三策略墙钟 1000ms、
360 只稳定选择 100ms、本地决策发布 500ms、DeepSeek 结果重发布 1s、5500 行全市场特征加
三板各 120 候选的 tomorrow 原生投影 5s、事件内部入队 100ms、SSE 到浏览器下一帧 100ms、
权威 SSE 发布年龄 2s、当前/驻留历史 API 200ms、ETag 304 50ms、日期和状态 API 100ms。
同身份关键路径相对基线不得退化超过 5%，100 tick 项目分配增长不得超过 20%。

发布性能 runner 必须调用活动生产函数，不得以占位 DataFrame 或纯序列化替代真实
标准化、评分、选择、发布和 Web 路径。延迟轮次关闭 tracemalloc，内存轮次单独开启；fixture
固定数据、配置、策略、代码与环境身份并禁止外网。历史实测数字只属于验收报告，不进入本文。
仓库固定提供底层 `trader-cli performance-check`、公开组合入口 `./run.sh check` 和
`make performance-check`；三者复用 `trader.entrypoints.performance`，报告必须记录代码、配置、有效
Tomorrow 评分档位、fixture、Python、CPU 与系统身份，
并可读取显式 baseline 执行 5% 相对回归。Firefox 的 SSE patch-to-paint 使用
`scripts/diagnose_runtime.py --profile browser` 和 `make browser-performance-check`；供应商真实交易时段
采样使用同一诊断入口的 `sources/live/full` profile，不得混入禁止外网的离线性能实现。

## 8. 发布、冻结与持久化

盘中 current 只存在于 `UnifiedDecisionIndex`：today、tomorrow、d25 保存最新已接纳的
`ScoredDecision`，long 保存 `LongProjection`。local 决策先发布；合法 hybrid 只能引用仍为
current 的 local 父版本并以新身份 CAS 提交。数据源或模型失败保留最近同日有效 current 并
显式降级；业务合法空集可以发布 ready，输入未就绪返回 `not_ready`，两者不得混淆。
未冻结的观察项是纯内存投影：已接纳 current 中的观察项随 current 驻留；评分已经完成但输入质量
门禁尚未通过时，最新同日 local 决策只写入独立、单调更新的 `UnifiedDecisionDraftIndex`，不得写入
正式 `UnifiedDecisionIndex`。草稿查询只投影其中 `selected=true/action=observe` 的条目，只在对应
盘中窗口展示，不发 SSE、不写检查点、正式记录、归档或收益结算；冻结、`close_fallback` 和显式
历史均不展示，且不得用观察项或草稿补位正式推荐。

Today 在 11:20:00 精确关闭同日索引，只能封口 11:19:59 及此前已经接纳的最新决策。没有
可冻结稿时同日保持 `missed_freeze/not_ready`；禁止 checkpoint、启动恢复、午间补算、
`close_fallback` 或任何迟到结果追补。已有正式记录之后只允许父版本、策略、交易日和入选代码
匹配的报价 overlay。

Tomorrow 与 D25 在 14:49:20（含）至 14:50（不含）由调度器自动尝试保存同运行身份、距边界不超过 30 秒、
哈希有效的检查点，暂时失败只在该窗口内重试；14:50 原子封口各自索引。正式提交失败只允许以 1/2/5/10/30 秒退避重试
同一 sealed object，不得重新评分、重新选择或让收盘恢复抢占。14:50（含）至 15:00（不含）
冷启动只可恢复尚未消费且身份匹配的同日检查点。

15:00 后，已有正式记录不可覆盖。Tomorrow/D25 同日正式记录缺失且没有待重试封口时，运行中
优先固化本进程已有 V2 current；冷启动才以完整同日 official-close 输入执行一次纯本地补算。
两者都创建 `close_fallback`，不新增 DeepSeek 请求；合法空结果与非空结果使用相同提交语义。
冷启动补算按活动策略/profile 的历史资格逐股筛选，并对主板、创业板和科创板独立要求可用横截面；
板块人口不足时只跳过该板并记录 `board_population_insufficient`，不得占用其他板额度。三个板均不足
时保持整批 `not_ready`。条件完整但没有正式推荐时仍创建
可审计的不可变空记录，不得降低门槛或用观察项补数。
Long 不冻结、不写正式记录、不进入历史或结果结算。

正式记录先执行 official-only 投影，只保留 `selected=true` 且 `action=executable` 的条目；
overlay 只能改变价格、涨跌、成交额、换手率、总市值、来源和报价时间，不能改变名单、分数、
风险、动作或排名。记录按
策略和交易日唯一，规范 JSON、manifest 与 SHA-256 通过临时文件、flush、fsync 和原子提交
保证；相同内容重试幂等，不同内容冲突。损坏、半提交或旧 schema 文件不得进入 V2 查询。

活动正式记录按策略保留最近 20 个不同交易日，每策略单日最多 6 条。结果结算以正式记录、股票
和 horizon 唯一，后台将全市场等权基准及收益、净超额、MAE 与质量状态不可变写入
`.runtime/v2/research/outcomes.sqlite3`；业务内容相同但进程观察时间不同的重试保持幂等，不同业务
内容冲突。调度器在交易日 15:00 后提交同日唯一结算键，成功后不重复执行；失败或盘后冷启动可在
后续 tick 重试，当前推荐和只读 Web 继续可用，结算完成/失败计数进入状态 API。HTTP 不触发行情、
结算或写库，结算也不得改写冻结记录。归档和结算的细节属于运维契约，不得恢复旧 Web envelope
或旧运行库读取。

## 9. 唯一 Web API 与 SSE

V2-only 最终发布只注册第 1.2 节列出的根页面和 `/api/*` 路由。`strategy` 只允许
`today`、`tomorrow`、`d25`、`long`；long 只支持 current，history 和 dates 返回受控
`history_not_supported`。旧带版本前缀的 API 路由和独立 tomorrow 旁路已删除，不构成别名、重定向、
兼容期或最终验收接口。

current 一次返回完整紧凑 `DecisionView`；公开 schema 为 `v2_decision_view_v4`，绑定决策、数据、
配置、策略、融合、冻结、报价和 schema 身份，并包含数据年龄、覆盖、过滤统计、降级原因、
`selection_diagnostics`、ETag、最多 6 项正式推荐与 6 项观察。已发布且未冻结的 current 可以在
`items` 中同时返回正式与观察；冻结当前、
`close_fallback` 和显式历史只返回最多 6 项 `executable`。允许 ready 的空正式结果；上游
未就绪时正式 `items` 必须保持空且不得回退到其它日期或上一交易日；冻结边界前如存在同日输入质量
未通过的 local 决策，可在独立可空的 `draft` 对象中返回版本、哈希、观测时间和最多 6 项
`observe`，其哈希参与 ETag。`draft` 不改变 `status=not_ready`、正式 coverage、冻结或历史语义。
核心数值不可用时返回 `null`，不得伪造 0。
统一决策身份与正式记录使用各自当前 schema v2，并把不可变 anchor quote、setup、downside、
研究覆盖、复核终态和 selection diagnostics 纳入规范哈希。领域和应用层只操作有类型对象；正式记录
的字段身份材料由唯一显式白名单生成，`infra/persistence/decision_record_codec.py` 只复用该材料执行
JSON 编解码、输入字段校验和身份复算，不得维护第二套逐字段 encoder；研究审计哈希同样必须显式投影
每个字段，禁止通过 `__dict__`、反射或 dataclass 自动展开改变 schema。活动决策和正式记录 codec 只接受
当前 schema 和哈希一致载荷；研究轨迹是唯一例外，只为既有不可变研究事件保留显式 v1 只读 codec。
活动决策和正式记录的旧 schema v1 不进入新 release 的启动、恢复、查询或测试路径，也不得以双读、
现场升级或默认字段恢复；研究 v1 同样不得迁移、补字段或参与新的 14:50 人口读取。

逐股对象只公开页面需要的代码、名称、板块/行业、核心行情、锚点行情、本地/DeepSeek/DeepSeek 风险/
最终分、动作、结构化理由、精简风险、复核终态、`setup_type` 与 `downside`；Tomorrow V1/V2 另外公开
模型信号分、预测超额、估算成本、预测成本后净超额和模型分歧。原始特征、权重、
分位、完整缺失清单、来源载荷和模型技术审计不通过 Web 传输。三类荐股 `score_status=scored`，
long 固定 `score_status=not_applicable`；long 的 `items` 保留配置完整顺序并通过
`long_groups` 提供 `chokepoint`、`future_growth`、`low_price_potential` 分组。
Tomorrow 的 GET 与 SSE 完整替换均从决策 `input_versions.score_model` 公开同一模型版本，页面不得以
策略标签或静态文案替代；旧正式记录没有该输入身份时只隐藏版本字段，不猜测当前模型。

历史接口只按策略和日期精确读取已经提交的 V2 正式记录；日期列表按策略独立，不因同日其它
策略缺失而隐藏记录。切换到 long 时日期固定回到当前并禁用历史；切回短线同样从当前开始。
页面切换策略或日期必须隔离迟到响应，历史缺失显示目标身份的正常空状态，不自动跳回当前。
页面首次打开时按 today、tomorrow、d25、long 的顺序选择第一个当日 `ready` 且
`selected_count > 0` 的 current；均无条目时回到第一个 `ready` 策略，仍无 ready 时默认 today。
该选择只发生一次，用户手动切换后状态刷新不得抢回其它策略。
所有 HTTP 查询均为只读，不抓行情、不评分、不调用 DeepSeek、不触发冻结、恢复、归档或结算。

SSE 事件统一使用字符串 envelope `schema_version=v2_event_v1`、单调 ID、有界回放和有界客户端队列；
行级推荐/overlay patch 独立使用数字 `patch_schema_version=4`，不得用同名字段覆盖 envelope 版本。
decision 完整替换事件携带完整 identity、类型化 coverage 以及完整入选项；浏览器必须原子替换旧
coverage，不能在把快照切为 `ready` 后继续沿用上一轮候选、已评分、过滤或选择计数。coverage
缺失、无效或与完整替换项数量不一致时必须请求 current 重同步，不得由旧值或逐项相减推断；
overlay 只允许更新匹配 current version 的价格、涨跌、成交额、换手率、总市值、来源、来源时间、
报价版本和年龄，并携带与 current ETag 一致的 `projection_version` 及行级 `quotes` patch。
显式游标才回放；游标超前、过期、断裂，base/schema/identity 不匹配或慢客户端统一返回
`resync_required`，客户端以 ETag GET current。publisher 不等待客户端；SSE 连通时停止完整
current 轮询。断线后立即执行一次 status/current 对账，以 3 秒周期临时轮询，并按
1/2/4/8/15 秒上限指数重连；连接恢复后停止临时轮询并重置退避。overlay 正常路径只替换命中代码的
推荐行或观察行并更新摘要，不得重建表头和整张表；结构身份不匹配时才请求 current 重同步。

`GET /api/status` 只聚合注入的内存遥测：来源接收/源时间年龄、内部时延、当前 V2 identity、
队列与 latest-wins、DeepSeek 物理预算、Tomorrow 生产模型身份/人工授权/历史失败/监控边界、
冻结/持久化状态和最近受控失败。状态接口不读取数据库、
文件或网络，不暴露股票集合、关联载荷、密钥或完整外部响应。
DeepSeek 账本只在启动恢复及 reserve、完成、失败、批次状态变更时访问 SQLite，并在同一写锁内换入
不可变预算快照；Reviewer 和顶层 status 共享这一个快照。预算交易日统一由可注入时钟换算为
`Asia/Shanghai`，SQLite 锁定或暂时不可用不得使 status 失败。

## 10. V2 唯一桌面界面

根页面 `/` 必须直接渲染统一 V2 工作台，不保留独立 `/v2/tomorrow` 页面或旧 Dashboard。
页面统一展示 today、tomorrow、d25 和 long。评分决策与行情只来自第 9 节应用层查询与 SSE；
Long 股票身份允许读取由 `long_watchlist.json` 确定性生成并随 wheel 打包的只读资源，以便 API
暂不可用时仍展示固定名单。浏览器不得抓行情、评分、调用 DeepSeek 或决定冻结。

以下布局细节是当前用户可见验收要求；实现可以重写，但不得删减业务状态、桌面信息
层级、可访问性或三档分辨率门禁。

首页固定包含：Header 状态、结果摘要卡、日期和策略切换、当前策略说明、正式荐股表、
当前短线视图的独立观察池表、选中股票详情、系统状态/DeepSeek 预算与失败原因。当前快照状态与最近错误固定放在页面
顶部 Header 的独立双栏信息带中；左栏标题在“快照状态”后显示当前快照交易日 `YYYY-MM-DD`，
正文发布时间只显示上海时区 `HH:mm:ss`，并只描述当前选中策略的生命周期、发布时间、报价来源与数据年龄，
右栏把系统健康合并进“最近错误”标题行，以“正常 / 降级 / 错误”文字、图标和颜色共同表达，
并显示活动问题数、最高优先级中文原因、影响策略、处理阶段、发生时间和恢复状态。错误详情标题必须
把活动问题数与已恢复记录数分开显示，不能把两者相加后误写成当前错误数。主界面不得直接
显示技术原因码；“查看全部”打开独立错误详情，按严重程度、活动状态和最近发生时间排序，列出
有界的活动与已恢复问题及可复制的受控原因码。三档桌面分辨率下两栏高度固定，长文本只能在各自区域
换行并纵向滚动，不得改变下方摘要、策略或表格的纵向位置。股票列表摘要固定在四策略
按钮行上方，随后直接进入主表，三者无额外间隔相邻；主表不显示重复的“正式推荐”标题行。
普通页面不提供“当前推荐”、
`official/live` 或“收盘补算”操作入口；当前日期始终隐式使用 `view=current`。today、
tomorrow、d25 的当前正式荐股表只展示正式推荐，并在其下方展示最多 6 只
`observe` 的独立观察池，标题明确“不可执行，仅供观察”，逐股显示最终分和直接原因；只读决策
接口和 Web 均按决策已有 `rank` 展示，因此池内保持最终分降序、本地分降序、代码升序的生产稳定
排序，不得按内部持久化顺序或股票代码重新排列。
观察池仅在 today 的 09:30-11:20、tomorrow/d25 的 09:30-14:50 当前页显示，午间保留；
到冻结边界立即隐藏且摘要显示“已关闭”，显式历史摘要显示“不保存”。盘前、冻结当前、
`close_fallback`、盘后和历史均不渲染观察池；long 仍在同一主表展示当前固定研究池。
活动窗口内的观察池状态必须区分：策略 lane 正在运行/待处理且尚无草稿时显示“正在生成观察草稿”；
草稿对象已经形成但其中没有 `observe` 条目时显示“本轮无股票达到观察条件”并在摘要计数为 0；
lane 空闲且没有草稿时才显示“本轮尚无可用观察草稿，请查看运行状态”。不得把合法空草稿描述为
尚未生成，也不得在没有活动 lane 证据时持续显示生成中。
长期页把 `卡脖子行业`、`高成长赛道` 和 `低价潜力股` 三个按钮直接放在
策略控制栏中，紧跟 `长期研究 · 仅展示当前数据` 说明之后；进入 long 时三者与说明一起显示，
切换到其它策略时三者一起隐藏。按钮外层不绘制边框、背景或内嵌方框，按钮本身也不使用
矩形边框或块状激活背景，仅以文字和底部激活线表达状态。控制栏之后显示独立左右两栏：
左侧只承担当前类别的行业/赛道分组导航，默认
标题和内容为 `卡脖子行业`；右侧使用明确的 `重点股票行情` 标题并展示所选分组内固定股票
的名称、代码、行业、当前价、涨跌幅、
成交额、换手率、总市值、行情来源和行情时间，点击行打开详情。三个长期大类在两栏上方
等宽占满可用宽度，不得挤入左侧行业栏。代码、名称和固定行业/赛道归属来自版本控制的
`long_watchlist.json`；价格类事实来自后台统一行情快照并携带来源与时间，浏览器不得现场
抓取。打包使用的 `long_watchlist_data.js` 必须由
`scripts/generate_long_watchlist_asset.py` 从该 JSON 唯一来源确定性生成；
`make long-watchlist-check` 和 `make lint` 必须拒绝配置与静态资源不一致，禁止手工维护第二份名单。
公司公告、主营、财务指标等基本面事实未进入长期 `DecisionView` 前不得用静态文案
冒充实时事实。卡脖子类别增加独立 `脑机接口` 分组，并将原 `AI算力液冷/电源` 拆为
`AI算力`、`液冷` 和 `数据中心电源`；高成长类别将原 `AI算力/光模块` 收敛为独立
`光模块` 分组。长期量化复核报告中的固态电池正式通过股票必须合并到卡脖子独立
`固态电池` 分组，并从高成长混合分组迁出以保持全局唯一。卡脖子类别同时把原混合
`科学仪器/高端医疗设备` 拆为 `高端科学仪器` 与 `生命科学/高端医疗装备`，将
`精密零部件` 校正为 `高端传感器/精密测量`，并增加 `航空发动机/燃气轮机`、
`新型电力系统/储能` 和 `可控核聚变关键材料/装备`。这些分组只扩展 long 固定观察池：
页面实时刷新报价，但不进入候选、评分、DeepSeek、TopK、冻结、推荐历史或收益结算。
已有股票跨分组迁移而不复制；新增标的必须有正式披露支持产业链关系。卡脖子左侧行业栏不按来源拆出
额外 tab；同一行业股票列表中先显示扫描报告正式名单，再以一条横向分隔线接续显示当前固定
龙头补充，股票代码仍全局唯一。卡脖子/高成长每组最多展示 5 只固定观察股票；
`低价潜力股` 按五个固定子 tab 分组、合计最多展示 26 只固定细分龙头股票。左侧分组栏与
右侧股票信息区从同一行、同一顶部起点开始并保持等高；两侧内部统一使用 12px 内边距、
58px 标题行和 12px 内容间距，使左侧第一个子 tab 与右侧股票列表表头从同一高度开始。
三类长期分组的完整快照展示价格、涨跌幅、成交额、换手率、总市值、来源和时间；轻量
overlay 只刷新价格、涨跌幅、来源和时间，不因行情或评分自动换股。
每个左侧行业/赛道子 tab 在行业名称后显示该分组有效行情股票的当日涨跌幅等权算术平均值，
顶部三个长期大类按钮不显示该指标。计算只纳入当前 `items` 中属于该分组且
`pct_change` 为有限数值的股票，缺失或无效行情不得按 0% 计入，真实 0% 必须正常计入；
整组没有有效行情时显示 `--`。页面同时通过 tab 的辅助说明给出有效行情数/分组总数，
完整快照、轻量 overlay 或重同步更新涨跌幅后必须随既有重绘立即重算，不新增浏览器行情请求。
整个长期页每只股票只能归属一个分组，三大类及其子分组之间不得重复；配置加载时必须校验
所有固定名单股票恰好归属一次。长期标题、当前分组、激活子分组和股票名称使用更高对比度、
左侧强调线或悬停反馈突出重点，但不得改变两栏等高、同起点和无页面级横向溢出的约束。
摘要固定为五张等宽紧凑卡：数据新鲜度以 `h`、`m`、`s` 组合展示小时、分钟和秒，并同时展示
行情来源和行情时间；数据可用性在短线 current 因输入门禁未就绪时直接展示基础资料完整数/请求候选数，
副行展示有效行情数/请求候选数和有效历史数，不展示上市日期或交易日龄缺失构成；已发布短线、历史和 Long
仍按当前页面完整名单展示具备有效价格、涨跌幅、来源和来源时间的股票数/名单总数及行情缺失数。
推荐漏斗是候选、已评分、正式推荐、过滤数、观察数和最高评分的唯一摘要入口，不得与数据可用性重复；Long
的推荐漏斗明确显示不适用；模型预算展示已用、
剩余、每日上限和当前正式项模型复核数；冻结状态展示当前策略滚动/冻结/不适用状态及对应边界。
发布状态卡之后紧跟“评分最高”卡，按当前快照全部已评分候选的最终评分降序展示最多三只股票的“分数 - 股票代码 - 股票名称”；未就绪时使用同一快照观察草稿中的 `top_scores`。若没有已评分候选但评分诊断仍有最高最终分，显示该最高分并明确当前无达到观察门槛的股票；Long、缺失快照或无有效最终分时显示无可用评分数据。
短线 current 因覆盖门禁处于 `not_ready` 时，空 `DecisionView` 仍是正式发布边界，页面摘要改读同策略
`scheduler.input_quality` 的脱敏聚合状态：行情与身份覆盖、候选、已评分、过滤、发布前观察草稿和最高分
必须继续可见；正式推荐数读取 `selected_executable`，观察明确标为“观察草稿”，不得把未发布条目伪装成 current 名单。
主推荐区必须按确定性优先级给出单一结论：Today 的 `today_freeze_missed` 优先显示“今日未形成正式结果”
和禁止补算；候选行情或评分 pending 显示“采集中”；候选行情、证券主数据或历史覆盖不足显示“暂不可发布”
及当前值/要求值；已完成评分且正式池为空显示最高最终分、距正式线差值、达到观察线/正式线数量，并最多
列出三项按数量排序的聚合原因。普通页面不得展开上市日期或交易日龄缺失构成。成本后预期净超额、亏损
数据完整度始终按真实覆盖展示。当前可配置 Tomorrow 模型允许展示评分版本、模型信号分、预测超额、
估算成本、预测成本后净超额和模型分歧；V1/V2 都没有逐股亏损概率输出，页面不得用历史总体严重亏损率
伪造个股概率，状态 API 必须明确 `loss_probability_status=not_modeled`。
全市场完成但候选或评分尚未完成时，应用层必须先发布不含股票身份的
`candidate_quotes_pending`/`scoring_pending` 输入状态，未知的评分、过滤和身份阶段在页面显示“采集中/待评分”，
不得序列化成已经确认的业务 0；同日最近一次完整质量快照不得被下一轮临时 pending 状态覆盖。
首次评分仍在运行、`scheduler.input_quality` 尚未形成时，摘要必须回退到 `/api/status.market_data`
已有的候选行情缓存数、候选行情年龄及最新候选来源；数据可用性显示“准备中”，副行展示真实行情样本，
并把基础资料与历史标为待评分/待计算；漏斗显示“采集中/待计算”，冻结卡显示“采集中”；不得显示空白、
`0 → 0 → 0` 或把全市场来源冒充
候选最新来源。current 状态轮询在盘中窗口自动重新读取 `not_ready` 决策；独立 `draft` 出现后观察池
原位显示草稿项，正式表仍保持未就绪。缺失核心值显示“不可用”或 `—`，不得伪造为 0。Header 运行条不再重复年龄、预算和冻结信息，
只保留运行、市场阶段、推送和评分时刻。短线当前
行显示最新行情、关键分数、动作和原因，long 行显示当前行情与来源时间，历史行显示锚点价、
当前涨跌和锚点至今变化。数据年龄位于 Header/状态区，风险和下行保护进入选中股票详情；
同交易日 `today` 正式冻结时，当前正式表切换为 11:20 锚点跟踪模式，观察池立即关闭；
正式表只保留 `executable`，逐行显示排名、股票、11:20 锚点价、实际锚点报价时间、
锚点时当日涨跌、当前价、当前当日涨跌和锚点至今涨跌。名单、排名、评分和动作保持冻结，
当前价、当前涨跌、来源与时间继续由 overlay 更新；15:00 后保留最终 closing overlay，
下一交易日当前查询重新开始且不得沿用上一日。页面状态明确说明“11:20 已冻结，名单与
评分不变，行情按最新可用报价展示”；显式历史、tomorrow、d25、long 和
`close_fallback` 不进入该专用模式。冻结边界后没有正式快照时，`view=current` 与默认
正式查询都返回 `not_ready`，不得展示残留 live 草稿，也不得读取旧 schema 中曾存在的
Today 收盘补算记录。
详情固定收敛为推荐结论、核心行情、公司风险研究、评分与实际风险，
空值不生成占位区块。模型未复核和核心行情不完整只显示一条可读状态，完整技术审计不在
普通详情中展示。逐股公司风险研究必须独立显示“已核/部分/未获取”及财务、公司公告/减持、
质押、解禁四项覆盖，不能把公司研究未获取写成“模型未复核”。DeepSeek 未参与时模型分
显示“未复核”，模型风险扣分显示实际生效值 0，不得显示成另一个“未复核”。

状态使用文字、图标和颜色共同表达，不能仅依赖颜色。加载、空结果、降级、失败和
not_ready 必须可区分。页面保留“仅供研究，不构成投资建议”。三档桌面分辨率不得
白屏、重叠、出现页面级横向溢出或明显布局跳动；长快照状态和错误文本在各自独立区域
换行、滚动，不互相挤压且不挤压核心状态。long 的 `not_ready` 明确显示
“长期策略当前尚无可用数据”，不复用短线
“流水线尚未发布”的提示。

推荐 API 和状态 API 为审计保留原始受控原因码；页面不得直接显示原因码、
`last_error` 或外部异常文本。已登记原因必须映射为简短中文，带板块前缀的原因组合为
“板块：中文原因”，未知值统一显示中文兜底，原值最多保留 20 条在浏览器本地诊断对象。
普通页面的运行阶段、行情来源、固定名单来源、动作原因和模型状态同样必须映射为中文，
不得直接显示 `unavailable`、`long_watchlist`、供应商英文标识或其他内部英文状态。
正式推荐数只统计 `executable`，`observe` 单独计数；long 仍按当前观察语义展示。

盘中正式推荐为空时主区按 `selection_diagnostics.empty_reason` 区分无已评分候选、低于观察
门槛、风险/执行限制和集中度限制；有观察项时直接说明数量并指向下表，分数不足时同时显示
最高最终分、观察门槛和执行门槛；未冻结 current 同时从同交易日类型化 `input_quality.supply_funnel`
读取达到观察线/正式线的精确计数，并从 `supply_reason_counts` 展示最多三项聚合原因，不得统一误报为
“未通过下行保护”。观察池开放但无项时
保留自身精确空状态。冻结、`close_fallback` 和历史只依据已持久化的正式推荐显示精确空
状态，不引用已丢弃观察项的门槛或阻断原因，也不重新按当前动作解释旧版本冻结结果。
Tomorrow 至少形成一条模型评分条目，且全部条目均因预测成本后净超额不大于 0 而得到 0 信号分时，空结果使用
`no_positive_net_utility`，页面明确显示“模型预测成本后净超额均未转正，按固定成本规则信号分为 0”并
同时按普通评分空结果显示最高最终分、距正式线、观察线/正式线计数和聚合原因；不能误报成数据异常、评分尚未完成或模型复核缺失，也不能把固定成本前的毛预测收益
当成可执行分数。若 current 同时声称 `no_positive_net_utility` 且 `coverage.evaluated_count=0`，Web 和
统一诊断必须把它视为记录语义矛盾：未冻结 current 报告错误并等待重新评分；已冻结正式记录报告受控
降级，不修改、不覆盖正式记录，并保持空仓等待下一交易日。两种状态都必须明确说明没有形成可评分候选、
不能证明净超额均未转正，不得继续显示“评分已完成”。历史覆盖率和缺失数仍在数据可用性中独立展示。
未冻结的当前快照统一标记为“实时数据”，
并同时说明结果可能变化；`close_fallback` 只作为“已冻结 · 收盘补算”快照状态显示一次，
不得表现为按钮。

当前快照状态只描述生命周期、冻结身份、报价时效和快照级数据质量，不得把逐股选择阻断
混入同一提示。未冻结短线显示“实时数据、未冻结、结果可能变化”；today 正式记录明确
“11:20 已冻结”，tomorrow/d25 正式记录明确“14:50 已冻结”；`close_fallback` 明确
“已冻结 · 收盘补算 · 仅本地评分”；long 明确“不评分、不冻结”。冻结快照中的板块和模型
降级均以“冻结时”描述，`deepseek_pending` 不得继续显示成正在进行，而应说明冻结时复核
未完成、正式结果采用本地评分且迟到结果不会修改冻结记录。未就绪响应使用受控
`readiness_reason` 区分冻结前尚未发布、today 错过 11:20 且禁止补算、
tomorrow/d25 在 14:50-15:00 的冻结收口、15:00 后等待允许的收盘恢复以及 long 当前行情
快照未就绪。交易 session 可用时仍必须返回上述分策略原因，不能把盘后 today、tomorrow、
d25 合并为无法被桌面端解释的通用原因。

## 11. 可观测性与安全

`GET /api/status` 至少暴露线程和队列、latest-wins 替换/拒绝、来源接收与源时间年龄、
熔断、缓存命中/淘汰/字节、历史预热覆盖、策略阶段延迟、DeepSeek 物理请求与原子预算、
按策略 current/freeze/persistence 状态、SSE 客户端及慢客户端丢弃数。延迟统计保存有界
sample count、P50、P95 和 max；关联 trace、阶段名、样本和浏览器 patch 诊断都必须有上限。
公司研究协调器的纯内存状态统一置于加法字段 `company_research`，包含运行/待处理代码、批次结果、
冷却/退避/短路/淘汰计数、下一重试时间、固定批次预算，以及当前交易日的意图、周期提交和重评分计数；
不得暴露股票代码集合或外部研究载荷。
调度器摘要必须额外暴露各策略 lane、DeepSeek hybrid lane、数据任务 lane、控制执行器运行/在途/拒绝状态、
cadence 最短间隔/下一到期时间/固定时点生命周期、冻结完成/失败累计数、
刷新/构建/复核失败累计数、local/hybrid 发布累计数及
当前按策略结构化失败码；一次刷新失败只能显示其真实阶段，后续成功发布必须清除该策略的
活动失败状态。状态 API 同时返回 `health.level`、活动问题数和最多 20 条进程内
`recent_errors`；每项只包含受控原因码、严重程度、策略、阶段、首次/最近发生时间、累计次数、
活动/已恢复状态和恢复时间。相同身份重复失败必须合并计数，成功发布或对应控制阶段成功必须
标记恢复；列表按严重程度、活动状态和最近发生时间稳定排序。该诊断历史不持久化，重启后清空，
不得新增数据库或文件读取。外部异常文本、路径和载荷不得进入状态响应。
每个短线策略的 `input_quality` 必须加法公开不含股票身份的 `supply_funnel`：请求候选、候选特征、
证券主数据、有效历史、过滤通过/观察/拒绝、完成评分、DeepSeek 可审、达到观察线/正式线、
可执行/观察动作及最终两池
入选数量；同级 `summary` 以交易日绑定并只公开请求总数、有效行情数、行情缺失数、证券身份缺失数、最新行情来源/
源时间和最高最终分，不得公开股票代码或逐股值。状态还必须安全投影内存中的全市场/候选行情年龄、
候选最新来源、来源健康和历史预热聚合，但丢弃逐股 missing/conflict、外部错误文本和载荷。每个策略同时公开一个
确定性的受控 `primary_blocker` 和聚合原因计数。DeepSeek 状态必须始终公开
`enabled`、`configured`、物理调用数和零调用原因，使“没有合格候选”与“缺少密钥/禁用”可区分；
不得暴露代码、密钥或请求载荷。该诊断只能解释既有门禁，不能自动降低评分、可靠度、风险或动作阈值。
历史预热聚合必须加法公开 planned/completed/failure/inflight、一级资格排除数、退避数、唯一失败数、timeout 累计、
在途年龄、批次 deadline 和最后来源；这些字段只描述进程内聚合状态，不得包含逐股代码或外部错误文本。
同级 `issuer_eligibility` 只公开 schema、事实数、排除发行人数、原因聚合、manifest hash、完整性和持久化
错误计数；不得通过 status 暴露代码、证据正文或 SQLite 内容。
应用层运行端口必须以不可变、带类型的状态值提供 `input_quality`；线程池、来源 lane、cadence、
缓存和延迟状态遵循相同规则。组合根或显式基础设施可观测性 adapter 只在最终响应处执行 JSON 投影；
缺少必需能力属于装配错误，不得通过 `getattr`、空字典或默认 no-op 发布器静默隐藏。调度器还必须
公开 overlay 成功发布与失败累计数，保证 Web 可以区分“尚未生成”“已成功更新”和“刷新故障”。

状态 API 只返回组合根注入并已经聚合的内存事实，不读取网络、文件或数据库，也不承诺尚未
实现的指标。股票代码集合、关联身份明细、完整外部载荷和 SQLite/JSON 内容不得暴露；缓存
逻辑字节、RSS/USS、Python traced、Polars 估算和瞬时峰值原因由发布性能 runner 及验收报告提供。
状态顶层必须返回当前有效配置/策略组合的 `runtime_version`，并原样投影脱敏的 `scheduler`
摘要，以便区分旧常驻进程、刷新失败和决策构建失败；源码文件发生变化不会热加载到既有进程。
`/api/status` 的公开 schema 为 `v2_status_v13`，并必须从当前进程已导入的常量加法返回
`release.decision_view_schema`。浏览器必须同时校验 status release
身份和每份 DecisionView schema；任何缺失或不一致都属于 `release_contract_mismatch`，页面必须
停止把结果解释为行情采集或观察草稿生成，明确提示正常重启旧服务。该握手只判断进程/资源契约
一致性，不得根据工作树、Git、文件时间或 HTTP 成功状态推断运行版本。
`v2_status_v13.company_research` 必须从已有类型化 `V2ResearchRuntimeStatus` 显式白名单投影运行、待处理、
完成/部分/失败/延后、冷却/退避/短路、固定预算、当前交易日意图、周期提交与重评分聚合；不得透传
`last_error`、股票代码或内部字段。`v2_status_v13.scheduler.cadence.schedule_points[*]` 只使用 `status` 投影调度点状态，不提供旧字段、别名或
fallback；进程内对应值由 `SchedulePointStatus` 与 `SchedulePointState.status` 表达。内部状态字段变化不得
绕过显式 Web adapter 自动改变公开 JSON。
`v2_status_v13` 不公开离线研究数据库、未来采集窗口或运行期档位比较状态。显式 `research-status` 只读
检查历史研究持久化快照，缺库时不得创建目录，也不得依据计数自动生成后续工作。
Web 应用工厂创建应用时必须把模板和全部打包静态资源读入该进程的只读 release 快照，后续 HTTP
不得再次从工作树读取这些文件；源码更新只能在正常重启后整体生效。静态资源仍使用内容 ETag、
`no-cache` 和 `nosniff`，未知资源返回 404，且该快照过程不得写文件、启动线程、访问数据库或网络。
页面控制器与 SSE 传输状态必须分离：`dashboard.js` 负责页面状态和交互，`dashboard_stream.js` 负责
EventSource 游标、重连退避、断线轮询和 patch-to-paint 采样；两者通过显式依赖对象协作，缺少模块时
fail closed 并进入浏览器诊断。`market_data.market_changes` 只公开变更计数和合并身份，
`market_data.latency_waterfall` 只公开有界阶段聚合，不得泄露股票代码、关联 ID 或原始样本。
静态资源使用稳定路径，不再携带独立发布版本号；协议兼容仍由 status 与 DecisionView schema 校验。

日志只记录脱敏结构化摘要，不记录密钥、Token、完整模型请求/响应、完整供应商载荷或个人
敏感路径。所有外部 I/O 必须有 timeout、容量、熔断和明确失败策略。DeepSeek 与 Tushare
凭据优先从环境变量读取，也可从权限安全的 `.token_key` 分字段读取；绝不进入配置快照、
正式记录、研究轨迹或 API。

默认仅监听本机，不承担公网认证、授权和 TLS。扩大网络边界必须另立产品与安全契约。

## 12. 安装、运行与运维

源码更新不会替换已经运行的常驻进程；活动 Web 把模板与静态资源固定为启动时 release 快照，
不会从工作树热加载。部署新提交时必须正常停止旧 `run.sh`/`trader-server`，再依次执行
`./run.sh check` 与 `./run.sh`；启动后应核对 `/api/status` 的 `runtime_version`、
`release`、`scheduler.strategy_errors` 和各策略状态，不能只以 HTTP 200 判断更新生效。浏览器出现
“服务版本不一致”时不得继续等待或重复刷新，必须先完成上述正常重启；同一运行目录的进程锁会
正确拒绝第二个 `./run.sh`，这不表示新代码已经替换旧进程。

一键启动使用 `run.sh`、`run.ps1` 或 `run.bat`。手动流程为创建虚拟环境、从
`pyproject.toml` 安装、用绝对配置路径执行 `trader-cli validate-config`，再启动
`trader-server`。任何环境都不得依赖仓库当前工作目录才能读取资源。启动脚本的无参数行为固定为
启动看板；帮助必须把日常命令与显式离线研究命令
分组展示并逐项说明，不得再把所有名称压缩成一行伪装成必填启动参数。未知命令必须在创建虚拟环境、
安装依赖或启动入口之前失败，并只给出日常无参数启动与帮助命令指引。Linux/macOS 与 PowerShell
入口必须保持相同分类和默认语义；`run.bat` 继续委托 PowerShell 入口。当前公开脚本命令固定为
`check`、`download_history` 和 `train-tomorrow`。旧 H0 历史归档、回测和筛选命令已经退役。组合内的
底层 `trader-cli` 阶段继续保留用于测试、自动化和故障定位，但不再逐项暴露为 `run.sh` 命令。
启动器在运行所选入口前必须执行无副作用的帮助探测；入口缺失、项目元数据更新，或虚拟环境移动后因
固化解释器/editable 项目路径而无法执行时，必须用该虚拟环境当前可用的 Python 重新安装本仓库，再启动
所选入口。不得仅凭入口文件仍有可执行位就把失效环境视为健康，也不得为此删除整个虚拟环境。
`trader-server` 成功绑定监听端口后、启动 Web 服务线程前，必须向标准输出打印一次带
`http://` scheme 的实际浏览器 URL；默认配置显示
`浏览器登录地址->http://127.0.0.1:5000`，使支持终端超链接的控制台可直接点击打开 Web。
IPv6 地址必须使用方括号生成合法 URL。

首次安装与配置校验的可复制命令固定为：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/trader-cli --config "$PWD/config/v2/runtime.json" validate-config
```

开发安装或构建产生的 setuptools `trader_research_dashboard.egg-info/` 位于仓库根目录的隐藏
`.build-metadata/` 目录中；`setup.py` 在构建入口创建缺失的父目录，生成元数据继续被忽略，确保干净检出和
源码包均可直接安装。该隐藏容器已由 `.gitignore` 忽略，且容器名本身不得以 `.egg-info` 结尾，避免
`importlib.metadata` 把无 METADATA 的父目录误识别为发行包。`src/` 只保留运行包源码，不承载构建元数据。

也可以绕过启动脚本，直接运行安装后的唯一服务入口：

```bash
.venv/bin/trader-server --config "$PWD/config/v2/runtime.json"
```

日常启动只需执行 `./run.sh`，默认选择 Tomorrow V1。
`./run.sh --profile v2` 显式选择 V2；`./run.sh --profile v3` 启动时读取项目
`data/train/tomorrow-v3/` 下最新训练模型。缺少或损坏训练结果时在组合根失败关闭并给出稳定原因，
不自动回退 V1/V2。该覆盖只影响本次进程并重新生成
有效策略身份，不写回策略配置；改变档位必须正常重启。默认配置启动后访问
`http://127.0.0.1:5000/`。同一 `.runtime/v2` 只允许一个服务进程，第二个进程由
`.runtime/v2/server.lock` 拒绝。拒绝信息必须同时显示现有服务的实际浏览器 URL，并提示在原启动终端
按一次 Ctrl+C 正常停止后再重试；不得提示或自动删除 `server.lock`，因为内核文件锁只表示真实活动
进程，正常退出会自动释放。该拒绝继续返回非零，不能把“旧服务仍在运行”误报成“新代码已加载”。
外部行情、交易日历、Tushare 或 DeepSeek 暂不可用时启动失败开放：
只读 Web 保留最近有效 V2 快照并显式降级；Long 的卡脖子、高成长、低价潜力固定名单仍可展示身份，
缺失行情字段显示为不可用，不得伪造实时数据。

启动脚本公开命令分为：

| 类别 | 命令 | 运行边界 |
| --- | --- | --- |
| 日常 | `./run.sh` | 以默认 V1 启动本地 Web 看板和生产调度 |
| 日常 | `./run.sh --profile v2` / `--profile v3` | 以显式 V2/V3 启动；也可把 `--profile v1|v2|v3` 追加到其他公开命令 |
| 日常 | `./run.sh check` | 依次校验配置、只读投影研究状态并运行所选档位的离线性能门禁 |
| 离线研究 | `./run.sh download_history [--runtime-dir <路径>] --sessions 1..2000` | 显式安装 `[research]` extra 后，按固定窗口下载/续传 BaoStock 日线、逐日 ST 和历史行业；默认写入 Git 忽略的 `data/history/`，也可显式指定其它目录 |
| 离线训练结果 | `data/train/` | 受控训练结果目录；不得写入历史下载分片、WAL、缓存或供应商响应 |
| 离线训练 | `./run.sh train-tomorrow [--runtime-dir <路径>]` | 不接受阶段或 `run_id` 参数；默认读取 `data/history/`，显式路径必须与 `download_history --runtime-dir` 使用的根一致；一次调用连续完成父工件已满足的单一 V3 预注册阶段，永不自动 promotion 或激活 V3 |

当前 `check` 组合命令及 `train-tomorrow` 均由 `trader-cli` 单一编排器
统一拥有。Linux/macOS 与 PowerShell 不复制业务流程。普通阶段
返回非零时组合器仍运行剩余阶段，最终输出 `trader_command_group_v1` 汇总并以非零结束，使一次运行能
看到完整门禁分布；配置解析失败等操作性异常立即失败关闭。历史阶段绑定原 H0/R6/P2 规范，统一接受的
`--profile` 不会改写、重命名或重新封存研究工件。普通看盘不得要求任何 `research-*` 命令。未知命令
或非法档位必须在创建虚拟环境或安装依赖前快速失败，
并提示 `./run.sh help`。服务身份的最小只读核对命令为：

```bash
curl -fsS http://127.0.0.1:5000/api/status
curl -fsS http://127.0.0.1:5000/api/decisions/long/current
```

日常检查顺序：

1. 校验配置并确认交易日历、时区和运行目录可写。
2. 查看 `/api/status` 的来源、队列、缓存、预算、冻结和最近错误。
3. Web 推荐漏斗反复为 0、历史预热超时或供应商状态不明时，优先运行
   `scripts/diagnose_runtime.py --profile live --output -`。基础资料可用性可用
   `--profile security-master` 精确复测。统一入口必须只做薄编排，Web、沪深交易所证券主数据、历史、
   腾讯、Tushare 和 Firefox 由 `scripts/runtime_diagnostics/` 内部职责模块分别实现；单项失败不得中止后续扫描。
   默认报告契约为 `trader-runtime-diagnostics-v1`，只保留有界聚合状态、计数、延迟和定位结论，不转发
   股票代码、价格、Token、外部载荷或子进程 stderr。只有显式 `full` profile 才追加隔离 Firefox 刷新和
   离线生产性能门禁。
   报告文件与历史持久化复测目录必须是仓库外绝对路径；缩小后的单边界复测仍通过统一入口的精确 profile
   执行，不再保留顶层专项包装脚本。历史精确复测可用 `--history-source composite|tencent|eastmoney`
   拆分生产组合路由与单一供应商，默认仍为 `composite`；该选项只影响诊断请求，不改变服务配置或回退顺序。
4. 对行情 stale/degraded，先区分供应商延迟、熔断与内部 lane 排队。
5. 对 DeepSeek 失败，区分密钥缺失、禁用、预算、deadline、HTTP 和 schema；本地推荐
   应继续可用。
6. 对冻结异常，核对检查点时间、manifest、SHA-256、配置和策略版本；不得手工改写
   冻结文件。
7. 对 Web 历史异常，验证正式记录身份、manifest 与归档哈希，不允许 HTTP 请求现场重建推荐。

荐股漏斗诊断只在对应策略允许评分的盘中阶段判断持续归零，默认 6 次采样、5 秒间隔、连续 3 次
才认定为持续异常；事件序列回退表示运行重启，必须切断跨重启的连续窗口。候选、候选特征、证券
身份、历史或完整评分由非零回退到零，input quality 消失，release/schema 不一致，以及 status/current
策略、交易日或 projection 身份不一致均需留证。正式/观察入选数为 0 本身不是异常；`ready` 的合法空
current 必须携带 `selection_diagnostics.empty_reason`。Web 诊断模块只负责检测和归因，不抓行情、不触发
评分、不修改运行状态、阈值或冻结结果，错误发现或 API 不可达时退出码为 1。
Web 子报告契约为 `web_recommendation_health_v4`：每个策略除完整漏斗阶段外，还分别保留最多 32 项
人口过滤、候选过滤、候选瞬态、候选可选告警和供应原因聚合计数，以及当前策略/profile 的历史 session
要求，并加法输出公司研究协调器的运行/排队/退避/短路/预算/周期提交/重评分有界聚合。它会把“已有完整
评分却被旧全局历史门槛阻断”及未冻结
current“零只已评分却声称 `no_positive_net_utility`”报告为错误；同一矛盾若已经进入不可覆盖的冻结
记录则报告 warning，并由 Web 失败关闭说明。正式记录保留的其它受控数据质量降级同样报告 warning，
不得输出股票身份或逐股数据。

统一诊断的 `runtime/web` profile 只读取运行中 Web，`research` profile 复用只读
`trader-cli research-status` 权威投影并只保留活动研究窗口摘要，`sources` profile 才执行真实供应商请求并消耗适用配额，
默认 `live` 合并运行 Web 与来源探针，`full` 再追加浏览器与性能。总体 `failed` 表示至少一个检查未能执行或门禁失败；
`degraded` 表示全部检查完成但存在受控降级；两者都必须保留各子检查状态和发现，不能用首个错误掩盖
其他边界。`web/history/tencent/tushare/research/browser/performance` 精确 profile 复用相同权威实现；统一入口不得
复制供应商解析、健康判定或性能测量实现，也不得成为生产调度依赖。

正常停止时在服务终端按一次 Ctrl+C，并等待最多 30 秒；第二次 Ctrl+C 是立即强制退出，
只用于确认不再等待。浏览器关闭不会停止后台服务。正常重启后，观察池、候选、历史预热、
研究/review 临时结果、退避和 session 内存状态全部丢弃并重新预热；正式推荐、合法冻结
检查点、恢复载荷、预算、证据、overlay 和结算继续按各自持久化契约恢复。任务管理器强制
结束、断电、第二次信号和期限强退属于异常终止，只依赖冻结仓储恢复保证正式记录一致。

备份只需要 V2 配置、正式记录、manifest、必要 SQLite 和已验证归档；缓存、构建物和临时
文件不备份。普通 Web 不读取离线归档。回退必须切换完整旧 release 及其对应运行目录，
禁止只替换单个模块；V2 数据不得写回旧运行库，非独立清理任务不得破坏性删除正式历史。

## 13. 测试与发布验收

每次发布必须运行：

```bash
make format-check
make lint
make type-check
make test
make package
```

还必须验证架构 AST、`create_app()` 无副作用、固定融合向量 83.40、预算并发上限、
single-flight、latest-wins、来源乱序、SSE 游标恢复和慢客户端、冻结恢复与哈希一致性。
从仓库外安装 wheel 后验证 `trader` 导入、`trader-cli`、`validate-config`、模板、CSS、
JavaScript、图标和 `pip check`。桌面三档需要实际渲染证据；若宿主图形栈阻断，必须把
错误和未完成门禁列为剩余外部风险，不能宣称通过。
测试目录由 pytest 自动标记为 `unit`、`component`、`contract`、`integration`、`performance` 或 `js`，
其中性能测试同时标记 `slow`。`make test-*` 提供分层入口；`make test-release` 构建 wheel，并在仓库外
临时环境执行安装、CLI 配置校验、`pip check` 和包资源读取。

全工程重构期间，`make lint` 还必须执行严格复杂度与命名债务的单调收敛门禁：活动树中
`C901`、`PLR0911/0912/0913/0915` 和 `N` 系列问题数量不得高于已登记基线；每个重构批次
必须同步下调已经消除的问题额度，禁止新增同类债务。最终目录切换时这些临时额度必须
全部归零。活动源码单文件仍以第 3 节规定的 1200 行为上限，不另设任意的 500 或 800 行限制。

性能 fixture 必须固定数据、策略和配置哈希，并记录提交、工作树、源码树 SHA-256、
Python、系统、内核、架构和 CPU。runner 禁止外网，DeepSeek 用固定响应，SQLite 使用
临时库。延迟轮次关闭 tracemalloc，内存轮次单独开启；内存报告必须同时覆盖
`cache_logical_bytes <= 260046848` 和 `process_peak_rss_bytes <= 402653184`。
固定性能配置使用 `performance_budgets.schema_version=2`，并分别执行生产函数性能 CLI
与真实浏览器 runner；任一绝对预算红项必须保留在报告中并阻断发布，不能用其他阶段的
余量抵消。

### 13.1 荐股可用性防回归矩阵

永久验收同时覆盖热运行与冷启动，并贯穿“统一行情 -> 历史就绪 -> 候选 -> 评分 ->
`UnifiedDecisionIndex` -> 正式记录 -> 查询 -> Web current”整条链：

| 上海交易时段 | 热运行必须保持 | 冷启动/缺稿恢复 | 禁止行为 |
| --- | --- | --- | --- |
| 09:30-11:20 | 四策略同日 current；tomorrow/d25 为本地草稿 | 输入满足门槛后生成四类 current | 因只路由 today 而延迟其它策略 |
| 11:20-13:00 | today 有正式记录则只更新 overlay，否则 `not_ready`；其它三类保留 current | tomorrow、d25、long 缺失时各补一次本地 current | Today 检查点、午间补造或迟到发布 |
| 13:00-14:50 | today 不可变；tomorrow/d25 增量复核；long 更新 current | 缺失策略可从同日输入恢复草稿 | 用审核加严解释午前无数据 |
| 14:50-15:00 | 三类荐股使用同日正式记录；long 仍为当前观察 | tomorrow/d25 只可恢复合法同日检查点 | 把迟到结果写成正式记录 |
| 15:00 后 | 正式记录继续可查且只更新 overlay；long 当前可见 | tomorrow/d25 先读正式记录/V2 current，均缺失才创建一次 `close_fallback`；today 保持 `not_ready` | 盘后追补 today 或用上一交易日冒充当前 |

合法空集合必须返回 ready 与 `selection_diagnostics`，上游未就绪才返回 `not_ready`。
任一来源、评分、索引、持久化或 Web 环节失败时保留最近同日有效投影并报告明确降级，
不得用空或旧日结果覆盖。Long 单独验证完整固定名单、同日 current、行情时间和价格，
不按荐股评分或冻结语义验收。

所有调度、队列、缓存、评分、冻结、查询或前端 current 变更都必须覆盖上午热运行、午间
冷启动、11:20 与 14:50 边界、15:00 后热运行及正式记录命中/收盘恢复。每次相关发布仍须
启动真实 `trader-server`，核对 `/api/status` 和四策略 current；只验证 fixture 不算发布证据。

## 14. 当前交付状态与剩余路线

### 14.1 V2 工程状态

V2-only 工程能力与发布门禁验收已经闭合：活动组合只包含统一数据平面、统一决策核心、独立运行时、
四策略原生链、V2 API/SSE/Web、V2 入口和 V2 运行目录；旧 Pipeline、snapshot、旧 Web、旧配置、旧测试
和旧资源不属于活动树。完成批次、施工顺序和逐次验证记录只保存在 `CHANGELOG.md` 与
`docs/reports/`，不在本文维护第二份完成清单。当前代码仍属于 `Unreleased`；只有独立发布
批次完成版本归档并创建 tag 后，才能称为正式 `0.2.0` release。

正式 `0.2.0` 发布声明当前尚未发起。只有用户显式发起独立发布批次后，才能重新执行全部发布门禁、
把当前 `Unreleased` 归档为带日期的版本段，并在该批提交、推送及上游哈希核对完成后创建同一提交的
版本 tag；此前不得把“工程与发布门禁验收已闭合”表述为已经正式发布。

最终 release 不执行双读、双写、旧 URL 弃用窗口、运行时开关或生产指针切换，也不读取、
迁移或回放旧运行数据。工程迁移验收使用 V2 自身的点时输入、确定性重算、CAS、冻结哈希、
故障注入、时延、资源、SSE 和桌面证据，不以旧链一致率作为发布条件。

### 14.2 评分研究状态

评分验证唯一使用历史 point-in-time 回放。研究服务只读封存历史归档，按交易日顺序完成训练、可选校准、
embargo 和独立检验；合法空仓日按现金组合结算，是完整证据而不是缺失。
线上 T+1 outcome 只保存正式推荐历史和运行监控，不进入模型拟合、校准、评分门禁、自动调参或生产切换。

研究终态统一为历史不足、历史拒绝或历史验证通过；全部报告固定
`production_authority=false`。历史通过不创建自动晋级状态，也不写活动配置、冻结记录、统一决策索引、
API 或 Web。任何评分、模型、风险、阈值或动作变化都必须由用户另立高风险生产批次，先修改权威契约与机器
测试，再完成完整发布门禁。

既有 `score_p0_v1`、`score_p0_v2`、Tomorrow P1/P2 只作不可变审计。P0v1 保持历史点时证据不足，
P0v2 保持固定历史日期错失，P2 保持既有 `historical_rejected`；旧日期、hash 和失败原因不得覆盖，
但不再生成活动任务、运行计数或后续窗口。对应未来采集器、运行期 V1/V2 配对数据库、R7 晋级档案及其
CLI、状态和 Web 字段已经退役。

旧 H0 腾讯 640 日历史归档、固定回测和六项筛选入口已退役，不再创建或更新 `score-history`。
新的历史数据统一由 `download_history` 生成 BaoStock 共同日线 manifest，训练统一由
`train-tomorrow` 按已封存的 V3 前置工件执行。历史数据不足、历史事实不完整或父工件冲突时，训练失败关闭；
训练成功生成的最终 V3 `model.json` 由显式 `--profile v3` 的生产进程直接读取，不自动修改配置或切换 profile。

Score-R6 历史唯一验证使用新身份 `score_r6_historical_v2` 和 `score_r6_historical_report_v2`；旧
`score_r6_historical_v1` 目录只作不可变审计，状态读取不得把旧 `forward_required` 报告重新解释为
`historical_only`。

上述六项旧 H0 筛选/验证阶段及其 CLI 均已退役；历史研究只通过 `download_history` 和
`train-tomorrow` 执行，原始训练参数、证据和报告不得接入生产组合根或在线请求链，最终 V3 `model.json`
仅由 V3 profile 的 loader 读取。

隔离研究包继续保留原生评分因子诊断层、`ScoreTomorrowPointInTimeFeatures`、`ScoreTomorrowShadowModels`
与成本感知选择能力，分别封存 `score_factor_diagnostic_report_v1`、点时特征、
`score_tomorrow_shadow_report_v1` 和 `score_tomorrow_cost_aware_selection_report_v1`。LightGBM 工件只由
`ShadowModelArtifactStore` 管理；这些能力不接入 `bootstrap.py`、HTTP、调度、活动运行库、正式决策或 DeepSeek，
也不因工程存在而形成活动研究任务或生产权限。

`research-status` 使用 `v2_research_readiness_v9`，只读公开 H0 归档覆盖、R6/R6D/R6S、P2、V1/V2
历史留出、正式 outcome 计数和已退役研究的固定终态。它不读取网络、不评分、不创建缺失目录，也不公开
未来窗口、自动晋级或运行期配对计数；V9 投影 Tomorrow 训练 run/工件图、下一阶段和生产阻塞审计，并
以 `input_prerequisite_status`、`input_prerequisite_hash` 和有界 `input_blockers` 公开 Codex A 的 H1
元数据/标签预注册前置状态。`train-tomorrow` 与状态读取共用同一类型化前置服务；H1 不足时在资源探针和
handoff 读取前失败关闭，不写空预注册工件，也不把 `resource_probe_handoff_missing` 冒充首个数据根因。
H0 规范匹配且逐股完成覆盖率至少 95% 时，历史筛选才可执行；
其余阻塞使用受控原因码。

V2 严重亏损概率研究身份固定为 `tomorrow_v2_historical_risk_probability_v1`。类型化数据集只从 H0 独立验证段的封存 V2
历史证据提取成本后预测超额、模型分歧、信号分、ATR20 和估算成本，标签固定
`MAE / ATR20 <= -1.5`。60 日训练、20 日 Platt 校准、40 日独立检验均来自历史日期，两个边界各保留
1 个交易日 embargo；Brier 必须严格优于训练窗口基准率常数模型，ECE 不超过 0.05。日期或字段不足返回
`historical_data_insufficient`，不能拟合或输出伪概率；通过仍是无生产权限报告，当前 Web 保持
`loss_probability_status=not_modeled`。
工程链分别封存 `tomorrow_v2_historical_risk_model_v1` 与
`tomorrow_v2_historical_risk_validation_report_v1`，报告必须绑定模型工件 hash；同内容幂等、冲突或篡改
失败关闭。历史日期不足时只返回不足状态且不封存伪模型。

后续历史评分优化路线的执行顺序、样本门槛和研究终态以荐股策略文档第 15.1.21–15.1.34 节为唯一
权威。路线先完成一级永久资格名单与二级动态硬过滤，核对当前 V1/V2、P2 报告、人工授权和状态投影的
身份一致性；随后把不依赖收益数据、能够提高实时性和稳定性的评分热链等价工程前移，只在候选、分数、
风险、动作、排名和决策 hash 完全一致时优化脏集、既有缓存、single-flight、取消和持久化成本。

研究链再建立共同日线核心与 Today 11:20、Tomorrow/D25 14:50 三份独立覆盖 manifest，分别预注册
标签、60%/20%/20% 时序切分、5 日 embargo 和最终至少 200 个交易日。任一策略点时数据不足只终止该
策略，不阻塞其它策略。全候选预测—实际残差账本和过滤瀑布/候选召回消融完成后，只允许每策略最多
8 个人工预注册、可读纯函数表达的透明候选，共同进入 walk-forward、Holm 多重检验和一次确认段评价；
H1 免费来源能力探针使用 `score_h1_source_capability_audit_v2`，优先禁用环境代理继承、连接失败时以
相同请求语义有界回退，并逐来源保留 `probe_failures`；请求失败只证明当前来源未核实，不能投影为锚点
可用，成功来源的最早日期和覆盖计数仍独立封存。H1/标签/残差/C3 数据不足终态继承该失败原因且保持零
预测、零模型、留出未开启和无生产权限。
最终分别运行 Today、Tomorrow、D25 独立终端留出，并形成跨策略否定或验证结论。

第 15.1.23 节基线审计只能由显式只读 CLI 组合类型化的生产身份与研究状态端口，不进入普通 HTTP、SSE
或 Web；活动服务不可用必须记录 `live_identity_unverified`，不能伪造一致或误报冲突。第 15.1.25 至
15.1.34 节的 H1、残差、消融、候选和验证均位于隔离 `domain/application/infra/research` 和独立历史
目录，只能由显式研究 CLI 装配；`bootstrap.py`、生产调度、HTTP、SSE、Web、活动数据库和冻结链不得
持有这些研究服务。第 15.1.24 节若修改生产计算热链，必须遵守本文第 7、13 节运行、性能和发布门禁，
不得以性能名义改变推荐语义或接入新的 DeepSeek 调度。

当前第 15.1.24 节以只读 `scoring_hot_path_efficiency_baseline_v1` 报告闭合；显式
`trader-cli research-scoring-hot-path-baseline` 仅运行离线性能 workload，并在边界投影四类成本分母、
脏集收缩、延迟和决策 hash 等价证据，不写入生产状态、推荐历史或冻结记录。

第 15.1.25 节的显式 `scripts/h1_point_in_time_capability.py` 只允许把工件写到仓库外目录。来源能力报告
固定为 `score_h1_source_capability_audit_v2`：每个供应商独立执行有界探测，单个来源失败不能丢弃其它
来源的成功证据，并以有界 `probe_failures` 原因码进入 capability hash 和下游数据不足终态。脚本先使用
不继承环境代理的 `requests` 会话，连接失败时只回退到同一库、相同参数/请求头/超时的系统代理会话；
不得调用第二套外部 HTTP 客户端或保存供应商原始载荷。公开执行投影为
`codex_a_h1_capability_execution_v2`，显式列出逐来源能力、探测失败、三策略终态、父工件 hash、是否生成
OOF/model 以及生产/自动更新权限。当前真实审计已使三个策略、标签、残差账本和 C3 均以
`historical_data_insufficient` 收口，因此不得启动该旧 H1 身份的全量下载、训练、确认或终端留出；新的
BaoStock v2 日线能力只按下一段独立计划执行，不改写这个结论。

荐股策略第 15.1.38 节原计划中，现已完成 D 的依赖、显式入口、分片生命周期、状态投影和 A 数据端口集成；真实全量下载仍受
供应商登录能力和本机资源阻塞，尚未形成合格全量 manifest。2026-09-02 最新显式探针中，`--sessions 1` 返回
`supplier_login_transport_failed`；历史版本的 `--sessions 2000` 在外部 I/O 前因固定 30GB 门禁返回
`resource_blocked/disk_below_30gb`。当前命令将全量启动门槛调整为 25GiB、严格单证券顺序下载，并把不同 sessions 的 checkpoint 隔离。只允许安装 wheel 的
`[research]` optional extra 后显式执行
`trader-cli download_history --runtime-dir <仓库外绝对路径> --sessions 2000`。规范库以代码日期为
唯一行，在同一行保存未复权/前复权字段，每只股票最多 2000 个逻辑记录；新股、退市股和来源不足股票按
真实区间少于 2000 条，不补值。`--sessions` 接受 1–2000、默认 2000，超限在任何外部 I/O 前失败；只有
2000 日运行可以形成权威全量 manifest。Codex A 独占 gateway、分片/合并内容语义、覆盖审计、manifest/hash 和历史
行业/资格事实能力；Codex D 只拥有可选依赖、CLI、固定一个受控独立 SDK 子进程、每次调用 60 秒墙钟、最多两次
重试、每次查询至少间隔两秒、锁/取消/恢复和状态投影。子进程在每次 SDK 调用开始和结束时向父进程发送内部活动信号；
60 秒只约束单次供应商调用，不约束包含多次正常调用的完整阶段或单股任务，活动信号不得作为任务完成响应。普通启动、`check`、Web、`train-tomorrow`、bootstrap
和生产调度均不得隐式触发。

`download_history` 的运行可观察性固定为标准错误上的逐行 JSON 契约
`baostock_runtime_progress_v1`。`phase` 枚举固定为 `preflight`、`checkpoint_loading`、`supplier_login`、
`trading_calendar`、`security_universe`、`database_initializing`、`worker_starting`、`downloading`、`merging`；
每条事件显式投影 `source/current_code/sessions/universe_count/checkpointed_codes/remaining_codes/completed_codes/failed_codes/`
`expected_records/downloaded_records/active_workers/rate_limit_cooldown_seconds/last_failure_reason/elapsed_seconds/`
`checkpoint_database_pattern/partition_database_pattern/catalog_database/manifest_path`。`checkpointed_codes` 是成功与失败检查点总数，
`remaining_codes` 是尚未成功完成、续传时仍需处理的证券数。其中
`expected_records` 是逐证券按上市/退市有效区间求和的应有代码-日期记录数，`downloaded_records` 是已经提交
到 SQLite 的逻辑记录数。`checkpoint_database_pattern` 和 `partition_database_pattern` 均指向
`shards/<board>-<code-prefix>.sqlite3`（超过 100 只时追加百股桶后缀）；checkpoint 位于 `baostock-daily/sessions-<sessions>/`，续传优先从已验证的分片恢复冻结日历、证券池和来源版本，避免在已有
断点时再次依赖供应商证券主数据。下载期间和完成后都只存在 WAL 分片 checkpoint、`catalog.sqlite3` 与规范
`manifest.json`，不生成单一总库；单个分库损坏时移入 `quarantine/`，只重新下载该分库覆盖的股票，其他分片继续可读。供应商返回 `10001011` 时统一投影
`supplier_query_failed_blacklisted` 并立即停止整次运行，保留已提交断点；文件系统不支持进程锁时失败关闭，
不得无锁继续。

备用来源不与 BaoStock checkpoint、SQLite 或 manifest 混写。2026-09-03 的单股票 600 日真实能力探针中，
腾讯 `proxy` 和 `direct` K 线主机均未返回有效行，东方财富同样未返回有效行；120 积分 Tushare 只声明 raw 日线，
实际请求返回 `sdk_error`。因此本版本不开放伪装为正式归档的备用全量循环。统一诊断器支持
`--profile history --history-source tencent --tencent-history-host direct --history-days 600` 进行单股票、单请求、
无写入复验；只有同源多代码 600 日探针稳定通过、复权口径和覆盖审计通过后，才可由新的独立归档批次增加
该来源自己的 checkpoint、manifest、限频冷却和进度运行器。任何成功的公网备用档案都必须是独立数据集，
不得静默填入 BaoStock 分片或其 manifest。

该数据源只能提供日线、日历和供应商明确返回的基础事实，不能构造历史 11:20/14:50 锚点或单独补齐历史
行业、证券资格和风险事实 `effective_at`。全体和逐板代码日期覆盖均达到 95%、全窗口老股完整率、逐股
失败、停牌证据、最近 200 日隔离及全部 hash 必须共同进入 manifest；日线合格不得自动改变三策略旧 H1
终态、打开留出、训练或取得生产权限。V3 只有在独立历史事实能力也通过后才可消费该日线父工件。

Codex B 已封存 `tomorrow_v3_input_compatibility_v1` 只读消费契约。应用层只向未来 A 日线 port 请求一次
冻结输入描述，不读取价格行；领域校验固定核对 `score_baostock_daily_core_v2`、来源截止、2000 日身份、
`(code, trade_date)` 键、raw/qfq 同行、逐行 SHA-256、父 manifest hash，以及六个 Alpha 所需日线字段和
单位。兼容报告绑定输入描述 hash 与父 manifest hash，固定训练未开始、终端留出未开启、无生产权限。
报告同时固定 `automatic_model_update=false`，不得由兼容检查触发训练或 profile 变化。
当前验证证据包含类型化 fixture 和一次真实小批探针；供应商登录返回 `unboundlocalerror` 类失败，
因此 A 的全量 manifest、覆盖率和历史事实能力仍未合格，不得把入口成功或 fixture 结果投影为真实覆盖、训练 readiness、
收益或第 15.1.35 节解阻塞。

Codex C 已提供纯领域 `baostock_holdout_isolation_contract`，但未接入 CLI、Web、bootstrap 或生产路径。
该审计只读取 Codex A 将来封存的类型化 manifest 元数据，验证留出日期恰为完整有效日期的最新 200 日，且
训练、确认和日线代理三个消费集合均未读取它们；它不自行定义或重切数据集，也不读取行情、收益或模型。
BaoStock 日线锚点只能是 `15:00_daily_close`，不得声称 14:50 point-in-time 一致。旧留出身份固定为
`score_tomorrow_historical_candidate_v1`，新留出身份固定为 `tomorrow_v3_point_in_time_holdout_v1`；新留出
只引用新的日线和切分 manifest hash，不能复用旧留出 hash。审计以受控 blocker 失败关闭，输出始终为
`terminal_holdout_opened=false`、`point_in_time_parity=false`、`production_authority=false`。真实确认、收益
留出和影子比较仍等待 A 的合格数据/历史事实 manifest 与 B 的唯一 bundle，不能由该隔离结论提前启动。

Codex C 的显式 `scripts/codex_c_terminal_holdout.py` 只接受仓库外 Codex A 父工件目录，先校验 capability、
标签预注册和 A terminal index 的父 hash，再通过 Today、Tomorrow、D25 类型化 holdout service 封存各自
`report.json`，最后写入跨策略 `report.json`。父状态为 `historical_data_insufficient` 时只继承 hash 和原因，
输出 `terminal_holdout_opened=false`、空交易日期和 `production_authority=false`；相同输入二次执行必须幂等。
该入口不读取网络、收益、DeepSeek 或终端日期，不接入 CLI、Web、bootstrap、生产数据库或默认 profile。

Codex B 的数据不足收口由 `seal_codex_b_insufficient_batch` 生成
`historical_codex_b_insufficient_batch_v1`，只接受 Codex A completion 的 SHA-256 父引用。它按
Today/Tomorrow/D25 分别封存 `historical_data_insufficient` 策略终态和失败原因，并生成不含收益、候选、
Holm 或模型数据的联合报告 hash；候选家族、确认报告、Holm 检验和终端留出均保持未生成/未开启。对应
`CodexBTerminalArtifactStore` 使用显式字段白名单、原子写入、同内容幂等、异内容冲突和哈希篡改失败关闭，
不接入生产配置、Web、DeepSeek 或活动数据库。

旧 Codex B 的 V1/V2/C3 原始预测联合器只保留不可变历史数据不足工件
`tomorrow_joint_insufficient_terminal_v1`：父 completion 或任一 profile 原始预测不可用时，封存固定
profile 顺序、父 hash 和失败原因，`prediction_rows`、`holm_test_count`、模型 hash 与终端留出均保持未开启。
该终态只允许由显式历史审计读取；新 V3 训练、编排、预测和生产路径均不得读取、续写或改造它。

荐股策略文档第 15.1.35–15.1.36 节另立 Tomorrow 单一行业 Ridge/LightGBM 50/50 日线收盘代理路线。
该路线只允许显式离线命令装配隔离的 `domain/application/infra/research` 服务，训练参数、样本、报告和候选
工件不得进入生产组合根、HTTP、SSE、Web、活动数据库或冻结链。每股最多读取 2000 个代码日期记录；每个
行业至少有 1250 个特征/标签完整日期，先隔离最新 200 日给新的
`tomorrow_v3_point_in_time_holdout_v1`，其余日期再按 60%/20%/20% 和两个 5 日 embargo 形成开发、确认和
日线代理终端段。该新点时留出不加入或重开已经完成的第 15.1.32 节。

V3 不读取 V1/V2/C3 运行时预测，不训练二层联合器，不做 stacking，只生成一次成本调整后的预期净超额和
一次 `base_score`。标准生产路径必须依次完成日线代理、新 14:50 留出、冻结前影子和风险报告，再由用户
另立授权批次；若点时证据客观不可取得，保留的人工越权路径也必须由新指令绑定具体 model/report hash，
此前对路径的允许不构成预授权。训练模型直接从项目训练目录加载，配置只选择 profile；
V3 对外仍是组合根唯一注入的一个 `TomorrowModelPredictorPort`。当前服务器、Linux/macOS 与 PowerShell
入口均接受 `v1|v2|v3`，默认 V1；V3 缺少训练模型时以 `Tomorrow V3 training model is unavailable`
失败关闭。

该路线唯一脚本入口为 `./run.sh train-tomorrow`。用户不传内部阶段、`run_id`、模型参数或
工件路径；隔离编排器从规范和输入 hash 推导身份，一次调用连续完成所有父工件已满足的开发训练、一次
确认和一次日线代理留出；只有新的 14:50 父数据与隔离证明合格时才打开一次新点时留出。中断后同一命令
从原子检查点继续，已有终态只返回原结果。
最终目录 `data/train/tomorrow-v3/<run_id>/` 只公开 `report.json` 和 `model.json`；两者分别保存规范
`content_hash`，`model.json.report_hash` 必须绑定同批报告。全量特征、
标签及 OOF 预测位于内部 `evidence/` Parquet 分区。主程序只读取最终 `model.json`；
训练入口完成并生成有效模型后，下一次使用 `--profile v3` 的进程即可装配该 predictor，不需要额外
promotion 或复制 wheel 资源。将 `data/train/tomorrow-v3/<run_id>/model.json` 复制到另一台 PC 的同一相对
目录即可复用；模型 JSON 不包含本机绝对路径。

路线不实施历史 DeepSeek 盈利回测、自动模型搜索、自动调参、组合黑盒优化、独立逐股亏损概率模型、
定时/在线/无人授权重训、自动晋级、启动激活或自动回退；用户显式启动的 `train-tomorrow` 只能按封存
规范确定性执行预注册训练链。仅证明历史证据早于决策锚点无法排除当前大模型训练语料已知
后续结果，因此 DeepSeek 历史盈利增量不在本路线验证；生产固定复核、68/32 融合和每日 168 次物理请求
上限不变。既有 H0/R6/P2 工件保持不可变，既有 139 日窗口只能作为已观察历史审计，不能重新标为独立
盲测。不得恢复未来日采集，线上 T+1 outcome 只保存正式推荐历史与运行监控，不进入训练或参数更新。

所有研究报告固定 `production_authority=false`。历史候选即使通过，也只允许用户另立高风险生产变更
批次，明确指定单一策略和单一候选，再更新契约、机器测试、活动实现与版本并执行完整发布门禁；不得由
本路线创建注册表、生产模型、启动指针或自动更新状态，不得盘中切换 profile/模型或覆盖 current、正式/
冻结记录。当前生产继续 `automatic_model_update=false`。

生产调度继续在交易日 14:50 冻结 Tomorrow，15:00 后 outcome 结算器只处理正式冻结项的 T+1 收盘、
20bp 后净超额、MAE/ATR20 和严重回撤。该后台链不计算另一评分档位、不形成全候选配对、不自动调权、
重训、晋级、回切或覆盖冻结结果。状态 API 只公开活动模型 ID/hash、人工授权依据、
`automatic_t1_outcome_settlement`、`automatic_model_update=false` 和亏损概率未建模状态。
### 14.3 状态与历史记录边界

`CHANGELOG.md` 归档用户诉求、原因、修改、验证与剩余风险；`docs/reports/` 保存阶段性基线
和验收证据。本文第 14 节直接维护未闭合产品、发布和工程 Gate；荐股策略文档第 15.1 节直接维护
未闭合评分研究 Gate。本文不得重复事故时间线、逐版本修复、分支交接、影子/cutover 样本或完成批次
清单。实际状态变化时先核对代码与测试，再更新对应权威状态；一次性概览、计划和运行手册完成归并后
必须删除，不能再次成为活动输入。

## 15. 交付与文档治理

### 15.1 独立交付批次

用户每发送一次“继续”或语义等价指令，都形成新的独立交付批次，只处理计划或本文中
下一个完整未完成章节，以同级标题为边界，并完成章节内全部明确子项。不得只完成首个
子项后停止，不得顺带处理相邻章节。开始前记录 `HEAD`、上游、既有工作树变更和任务
文件范围；此前批次未闭合 Review、提交或推送时先闭合此前批次。

每批先更新契约与失败测试，再实现；完成后以已推送基线审查完整 diff，修复所有已知
发现，运行适用质量、测试、构建、仓库外 wheel 和桌面门禁。必须在 `CHANGELOG.md`
归纳用户提出的问题或诉求、原因或现状判断、修改说明和行为变化、验证证据、剩余风险
及后续项。提交使用一个准确的 Conventional Commit，推送当前跟踪分支并确认
`HEAD == @{upstream}`；成功后停止，等待下一条指令。

### 15.2 两份权威文档与非权威执行计划的更新边界

产品范围、架构、生命周期、时间线、数据服务、发布/冻结、API、UI、运维、性能和验收
变化更新本文；候选、过滤、因子、评分、风险、DeepSeek、融合、动作和 TopK 变化更新
荐股策略文档。跨边界变更同时更新两份。依赖和入口只更新 `pyproject.toml`；执行记录
更新 `CHANGELOG.md`，阶段性基线和交接报告可放入 `docs/reports/` 且必须标记门禁状态。
一次性实施计划和问题记录在内容按当前代码核验并归入两份权威文档、`CHANGELOG.md` 或
必要的 `docs/reports/` 后必须退役，不得继续作为活动输入；尚未获准的研究方向只能写入
对应权威文档的明确“待验证”章节，不能用单独计划文件形成第二套策略或工程状态。已经
已闭合迁移与实时链路的施工证据只归档在 `docs/reports/` 和 `CHANGELOG.md`，不在权威
文档保留版本流水账。除此之外不得在 `docs/` 新建并行
需求、计划、问题单、运行手册或归档文件。

当前不保留独立 V2 目标概览、实施计划或启动停止手册。产品、架构、时间线、API、运维、验收和
剩余工程 Gate 只由本文定义；候选、过滤、公式、风险、融合、排名和评分研究 Gate 只由荐股策略文档
定义；依赖、构建和入口只由 `pyproject.toml` 定义。历史交付事实只能从 `CHANGELOG.md` 与
`docs/reports/` 追溯。
