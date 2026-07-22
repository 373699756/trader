# SDK/API 采集到 Web 实时展示的列式增量优化计划

状态：非权威执行计划，尚未实施。

本文只细化[软件业务设计文档](software-business-design.md)第 7.2 节已经登记的 P1-P6
性能、缓存、发布和 Web 热路径章节。产品、架构、运行、API、运维和验收以
[软件业务设计文档](software-business-design.md)为唯一权威；候选、过滤、因子、评分、
风险、DeepSeek、融合、动作和排名以
[荐股策略文档](recommendation-strategy.md)为唯一权威；DeepSeek 低成本协同的后续方案见
[实时荐股与 DeepSeek 高效协同优化计划](plan_c.md)。本文不得覆盖这些契约，发现冲突时
以权威文档为准；实施引起契约变化时必须先更新权威文档、测试和 `CHANGELOG.md`。

## 1. 结论

采用“列式批处理 + 事件增量失效 + P6 热投影 + SSE 差量补丁”的单机架构：

```text
SDK/API 原始响应
      |
      v
Provider Adapter：校验、标准化、来源时间和字段血缘
      |
      v
P1 ColumnarQuoteBatch / ColumnarResearchBatch
      |
      v
Polars 列式统一、冲突检测和因子计算（P2/P3）
      |
      +----> MarketChangeSet ----> dirty code/board/industry
                                  |
                                  v
                         只重算受影响评分与 TopK
                                  |
                                  v
                       P6 PublishedSnapshotIndex
                                  |
                         +--------+--------+
                         v                 v
                    HTTP 首屏/补全       SSE 差量补丁
                                             |
                                             v
                                      浏览器局部更新 DOM
```

Polars 只进入 P1-P3 的全市场和候选热路径；最多 360 只候选进入 P4 前才一次性物化为现有
不可变领域对象。P4-P6 的评分、固定融合、动作、稳定排序、冻结和哈希语义保持不变。

本计划不引入 Kafka、Redis、Flink、WebSocket、多进程分布式执行或新的交易能力，也不
新增过去 60 日全市场下载、回测、shadow、自动调权或在线学习。对于单机约 5500 行全市场
数据，这些组件会扩大部署、故障恢复和一致性成本，当前没有收益。

## 2. 要解决的现状问题

现有实现已经具备多来源采集、不可变观测、带时区的点时时间、latest-wins、有界队列、
全市场/候选/TopK 刷新 lane、只读 Web 和带游标恢复的 SSE，继续保留这些能力。主要优化点
是减少热路径中的重复工作，而不是提高外部接口调用频率：

- 全市场标准化、合并和特征构建仍会反复创建 Python 行对象与 `Mapping`，5500 行乘多个
  来源后有明显对象分配、哈希查找和垃圾回收成本。
- 行情、行业、新闻、公告和风险刷新后缺少统一的变更集合；不少变化要等下一次 SCORE
  cadence，或触发比实际影响范围更大的重算。
- 当前 SSE 主要发布版本失效通知；浏览器收到 `recommendations` 或 `live_overlay` 后仍会
  再发完整 HTTP 请求，重复传输和解析未变化的股票。
- `PublishedSnapshotIndex` 和 P1-P6 热读路径已经写入权威设计，但尚未形成活动实现；当前
  Web 热读仍不能完整证明“当前及最近 20 日驻留查询不访问 SQLite/文件”。
- 可观测性有来源延迟和发布耗时，但缺少从接收、标准化、合并、dirty 路由、评分、P6、
  SSE 入队到浏览器应用的完整阶段时间。

## 3. 固定边界与不变项

### 3.1 业务语义不变

- 不改变候选、硬过滤、板内总体、同行、截尾、因子、风险、下行保护、动作和 TopK 规则。
- 不改变固定融合公式、`ROUND_HALF_UP` 和验收向量 `83.40`。
- 不改变 DeepSeek 的准入、证据、预算、缓存、截止时间和失败降级；性能优化不能制造额外
  DeepSeek HTTP 请求。
- today 11:20、tomorrow/d25 14:50 的冻结不可变；long 仍只展示当前快照。
- 外部来源或列式路径失败时保留最近有效快照并显式降级，HTTP 请求不现场抓行情、评分、
  调用 DeepSeek 或写盘。
- 相同有效输入必须得到相同字段来源、缺失、冲突、候选、分数、动作、排名、业务投影和
  冻结哈希；性能优化不能使用会改变边界结果的近似分桶。

### 3.2 工程边界不变

- 依赖方向仍为 `entrypoints/web/infra -> application -> domain`，`bootstrap.py` 仍是唯一
  组合根，`create_app()` 仍无 I/O 和线程副作用。
- Polars 是 `infra` 内部的批处理实现细节；`domain` 不导入 Polars，应用层公共端口不暴露
  `DataFrame`，Web 不读取列式缓存。
- `pyproject.toml` 仍是依赖唯一真相源。实施时建议先验证 `polars>=1.43,<2` 在 Python
  3.10-3.14 的 wheel 可安装性，再更新依赖；初始不直接引入 PyArrow。任何受支持 Python
  失败都不得激活列式路径。
- 初始门禁仍按 256 MiB 项目自有内存目标验收。若真实剖析证明列式路径在正确配置下仍无法
  满足该目标，可以在独立契约变更中提交测量证据和新分池预算；不得仅因“可以扩大”而隐藏
  泄漏、无界缓存或重复副本。

## 4. Provider Adapter 与结构化入口

参考 OpenBB 的 provider 分层，把每个 SDK/API 适配器固定为三个显式步骤：

1. `transform_query`：规范代码、市场、日期、分页和非敏感请求指纹，拒绝非法参数。
2. `extract_data`：只负责带 timeout 的物理 I/O，返回原始响应及供应商元数据。
3. `transform_data`：严格 schema 校验、单位换算、时间归一化、缺失原因和字段血缘，产生
   内部批次，不在此阶段计算策略分。

所有 provider 至少输出：

- `source`、`subject`、`observed_at`、`source_time`、`received_at`、`effective_at`；
- `data_version`、`source_contract_version`、`request_fingerprint`、`payload_hash`；
- 字段值、字段来源、单位、缺失原因、终态和脱敏错误码；
- `trade_date`、`phase`、`config_version` 和 `schema_version`。

未来时间、无时区时间、空版本、非法代码、非有限核心值和 deadline 后完成的观测在 adapter
边界拒绝。缺失值使用 `null`，不得用 `0`、空字符串或 `NaN` 冒充真实观测。

## 5. 列式数据模型

### 5.1 内部类型

新增四个 `infra` 内部不可变包装类型；包装器只提供只读投影、字节统计和身份，不向应用层
泄漏 Polars API：

- `ColumnarQuoteBatch`：全市场、候选或 TopK 行情值及字段血缘。
- `ColumnarResearchBatch`：行业、新闻、公告、财务和风险的规范长表。
- `MarketChangeSet`：新旧批次比较后的代码、板块、行业、字段族和版本变化。
- `ColumnarFeatureBatch`：P3 候选特征、板内横截面、同行统计、可靠度和缺失原因。

每个批次身份固定包含：数据集、来源集合、交易日、阶段、观察点、配置版本、来源契约版本、
schema、输入 manifest 哈希和批次内容哈希。包装器构造后不得原地修改；新数据以新批次替换。

### 5.2 dtype 契约

报价宽表至少固定以下 dtype：

| 字段族 | dtype | 规则 |
| --- | --- | --- |
| 代码、名称、行业、来源、版本 | `String`/`Categorical` | 股票代码保持六位字符串 |
| 板块、交易阶段、终态 | `Enum` 或受控 `Categorical` | 未知值拒绝，不静默扩展 |
| 价格、涨跌幅、量比、换手、因子 | `Float64` | 入口拒绝无穷，`NaN` 归一为 `null` |
| 成交量、计数、序列 | `Int64`/`UInt64` | 单位在 schema 固定，禁止隐式截断 |
| 时间 | 带时区微秒时间 | 业务解释统一为 `Asia/Shanghai` |
| 布尔状态 | `Boolean` | 缺失保持三态，不强制为 `false` |

禁止 `Object` 列和 Python UDF/lambda 进入热路径。字段血缘、缺失和冲突使用独立规范长表，
通过 `code + field_name` 关联，避免为每个报价字段创建嵌套 Python 字典。

### 5.3 eager 与 lazy 的选择

5500 行和 360 候选属于小到中等内存批次，默认使用预编译的 eager 表达式并复用表达式图，
避免每个 tick 重复生成计划。只有固定性能 fixture 证明 `LazyFrame` 的谓词下推、列裁剪或
公共子表达式消除有净收益时，才在具体批次启用 lazy；不把 Polars streaming engine 引入
实时主链。历史批量分析属于后续独立章节。

## 6. P1-P6 数据流

### 6.1 P1：来源批次

- 各来源 adapter 直接构建有 schema 的列数组，避免“原始字典 -> 行对象 -> 字典 -> 表”的
  往返转换。
- 原始证据正文仍按现有容量和脱敏规则保存；列式表只保存筛选、排序和关联所需结构字段。
- 每个来源继续最多一个在途任务和一个 latest-wins 待处理请求，不因列式处理增加抓取频率。

### 6.2 P2：统一行情

- 一次性按 `code` 建立来源索引，列式计算每字段的最新有效观测和来源优先级。
- 继续按 `source_time -> received_at -> 同时点来源优先级 -> 同来源 data_version -> hash`
  决定胜出值，腾讯价格复核与 0.50% 冲突规则不变。
- 原子发布 `ColumnarQuoteBatch` 和 `MarketChangeSet`；读者只能看见完整旧版或完整新版。
- TopK 定向报价写入独立 overlay 列式批次，不替换评分锚点或冻结输入。

### 6.3 P3：过滤和特征

- 硬过滤、板内统计、同行统计、截尾、分位、带通输入和可靠度优先使用 Polars 原生表达式。
- 横截面严格按主板、创业板和科创板隔离；样本不足、最多 5 日回退和同行排除自身的语义
  不变。
- 只保留评分需要的列，并在最多 360 候选进入 P4 时一次性转换为现有
  `FeatureSnapshot`；之后继续调用领域纯函数，不复制一套评分实现。

### 6.4 P4/P5：评分和 DeepSeek

- P4 不直接依赖 Polars，只消费与旧标量路径等价的不可变领域输入。
- dirty 股票只重算自身策略分；板内横截面变化时重算受影响板块；全局 TopK 选择在局部
  评分完成后重新执行，确保集中度与稳定排序正确。
- 新闻、公告、财务或风险形成新有效证据时，立即提交对应股票的高优先级 SCORE/复核判断，
  不必等待下一轮普通 SCORE cadence；是否调用 DeepSeek 仍完全服从 `plan_c.md` 及其未来
  权威化后的高价值准入、缓存、预算和截止规则。
- 同一证据 manifest 和价格反应身份命中 P5 时只做本地策略投影，不新增物理请求。

### 6.5 P6：发布热索引

实现内存式 `PublishedSnapshotIndex`：

- current pin 保存 today、tomorrow、d25 和 long 当前视图；
- resident history 保存最近 20 个交易日完整三策略投影；
- 只接受版本、manifest 和 SHA-256 合格的完整发布，原子替换；
- 当前及驻留历史 GET、日期列表和 ETag 不访问 SQLite 或文件；
- 更老日期按日期级 single-flight 冷读并一次预取三策略，部分失败不冒充完整；
- 普通 local/hybrid 草稿只更新 P6 和 SSE，检查点、正式冻结、`close_fallback` 与 15:00
  overlay 才按权威设计持久化。

## 7. 精确增量失效与重算

### 7.1 `MarketChangeSet`

变更比较只针对相同 schema 和可比较身份，至少包含：

```text
data_version
changed_codes
changed_boards
changed_industries
changed_field_families
evidence_manifest_changes
risk_changes
overlay_only_codes
full_invalidation_reason
```

字段按规范值比较；浮点使用与原业务相同的规范化值，不用近似桶跳过真实变化。相同输入
manifest 直接复用 P2/P3；配置、策略、schema 或来源契约版本变化必须全量失效。

### 7.2 路由矩阵

| 变化 | 重算范围 | 发布行为 |
| --- | --- | --- |
| TopK 定向报价 | `overlay_only_codes` | 只更新 live overlay 和对应行 |
| 候选价格/成交/量比 | 股票及其板内横截面 | 重算受影响板块，再执行全局选择 |
| 全市场行情 | 实际变化代码及受影响板块 | 更新候选与必要板块，不默认九组全重算 |
| 行业热度 | 受影响行业候选及其板块 | 局部评分后全局选择 |
| 新闻/公告/财务 | 受影响股票 | 更新证据、局部评分，必要时进入高价值复核 |
| 重大风险 | 受影响股票，高优先级 | 立即局部评分/观察或 veto，再全局选择 |
| 历史/分钟因子 | 受影响股票及必要板块 | 仅在输入版本变化时重算 |
| 配置/策略/schema | 全部 | 完整失效并产生新发布身份 |

dirty 事件继续使用现有有界优先队列和幂等键。相同主体与输入指纹的待处理事件合并为最新
版本；已开始的旧版本允许完成审计，但 compare-and-set 不得覆盖更新 P6 或冻结结果。

## 8. HTTP、SSE 与浏览器局部更新

### 8.1 读取流程

- 首次打开、显式切换策略/日期/视图、SSE 断线恢复失败时，通过现有 GET 读取完整投影。
- 正常在线更新由 SSE 直接携带差量，浏览器不再对每个 `recommendations` 或
  `live_overlay` 事件执行完整 `loadRecommendations()`。
- SSE 仍使用单调事件 ID、`Last-Event-ID`、独立有界客户端缓冲和 `resync_required`；不
  改为 WebSocket。
- GET 保持 Web envelope schema v3。差量协议独立使用 `patch_schema_version=2`，避免与
  API schema 混淆。

### 8.2 差量事件

`recommendation_patch` 至少包含：

```json
{
  "patch_schema_version": 2,
  "base_projection_version": "p6-previous",
  "projection_version": "p6-current",
  "strategy": "today",
  "trade_date": "2026-07-22",
  "upserts": [],
  "removed_codes": [],
  "etag": "...",
  "published_at": "...",
  "frozen": false
}
```

`overlay_patch` 只允许更新代码、当前价、涨跌幅、来源时间、数据版本、数据年龄和 overlay
身份，不得改变锚点价、评分、动作、排名或冻结哈希。

浏览器只有在以下情况执行带 `If-None-Match` 的完整 GET：

- 本地 `projection_version` 与 `base_projection_version` 不一致；
- SSE 游标过旧、超前、不连续或收到 `resync_required`；
- patch schema、策略、日期、视图或冻结身份不匹配；
- upsert/删除后无法通过本地顺序、TopK 数量或 ETag 身份校验。

### 8.3 DOM 更新

- 以 `strategy + trade_date + view + code` 作为行身份，只替换新增、变化或删除的行。
- 汇总卡、正式推荐、观察池和详情面板分别依据变化字段局部刷新；当前选中股票被删除时按
  稳定顺序选择下一项，不保留旧详情。
- 冻结事件先替换完整 P6 身份，再接受同 snapshot 的 overlay；迟到草稿 patch 必须丢弃。
- 浏览器保存最后应用版本和事件 ID，但不把业务快照写入长期存储作为事实来源。

## 9. DeepSeek 协同与成本约束

本计划只减少 DeepSeek 前后的等待、重复数据准备和 Web 传输，不修改其评分作用：

- 本地列式 P3 先完成硬过滤和排序，只有高价值集合才进入 DeepSeek，避免为低价值候选构造
  证据包。
- `MarketChangeSet` 把“普通报价变化”与“证据 manifest/风险变化”分开；前者命中缓存时
  只重算本地投影，后者才按 `plan_c.md` 判断是否需要新请求。
- 同一股票、交易日和证据 manifest 的原始事实跨 today/tomorrow/d25 共享，策略权重在
  本地投影；long 不调用 DeepSeek。
- Flash/Pro/emergency、物理请求计数、188 全局上限、11:18/14:48 停止提交和冻结
  compare-and-set 均不因实时化而放宽。
- DeepSeek 返回后只重算该批股票、全局选择和 P6 patch；目标为外部响应校验完成到 P6
  重发布不超过 1 秒。

## 10. 可观测性

每个批次和事件记录下列带时区时间点及对应身份：

```text
request_started_at
source_received_at
normalized_at
merged_at
change_set_created_at
features_ready_at
score_ready_at
deepseek_ready_at（如适用）
p6_published_at
sse_enqueued_at
browser_applied_at（仅桌面验收/本地诊断）
```

`/api/status` 增加：

- 各阶段 P50/P95、source-to-P6、P6-to-SSE 和浏览器本地应用耗时；
- 输入行数、输出行数、dirty code/board/industry 数、局部与全量重算次数；
- 列式批次字节、P1-P6 分池字节、峰值临时副本和 100 tick 分配增长；
- SSE patch 数、平均 upsert/删除数、字节节省、正常更新完整 GET 次数和 resync 原因；
- 标量/列式路径、回退原因、等价校验失败和 provider/schema 拒绝计数。

日志只保存身份、阶段、计数、耗时和脱敏错误，不写完整外部载荷、证据正文、密钥或 Token。

## 11. 单一原子实施章节

本计划不能拆成多个独立“继续”批次；它是权威设计第 7.2 节 P1-P6 未完成章节的内部施工
顺序，必须在一个交付批次中完成、Review、提交和推送：

1. 先更新权威架构/API/验收契约、`pyproject.toml`、失败测试和固定性能 fixture。
2. 建立 provider schema、列式包装类型和 scalar/columnar 双路径，不改变生产默认路径。
3. 实现 P1-P3 列式标准化、确定性合并、特征和 `MarketChangeSet`。
4. 接入 dirty 路由、受影响板块重算和 DeepSeek 结果局部重发布。
5. 完成 P6 热索引、持久化分流、检查点恢复和最近 20 日驻留读取。
6. 实现 SSE patch、浏览器局部 DOM 更新、版本失配 resync 和 ETag 条件补全。
7. 完成可观测性、内存治理、故障注入、等价/性能/桌面验收。
8. 只有全部激活门禁通过才切换列式生产路径并删除临时双跑；否则保持 scalar 生产路径，
   记录失败证据，不交付半套 P1-P6 或部分 Web 路径。

迁移期间可以用 feature flag 做测试和对照，但配置只能有一个权威默认值，正式交付前不得
长期保留无人负责的双实现或无退出条件开关。

## 12. 测试与激活门禁

### 12.1 正确性

- 固定 fixture 对标量与列式的标准化、合并、缺失、冲突、字段来源、板块、候选、因子和
  领域输入逐字段比较。
- 中间有限浮点允许绝对误差不超过 `1e-12`；进入评分前按现有规范化边界消除差异。
- `local_score`、`deepseek_score`、risk penalty、`final_score`、动作、排名、TopK、业务
  JSON 和冻结 SHA-256 必须完全相同。
- 覆盖乱序来源、同时间点优先级、腾讯交叉复核、null/NaN/无穷、时区、deadline、板内
  最小样本、同行排除自身和跨板隔离。
- 覆盖 dirty 路由矩阵，证明局部重算结果与同输入全量重算完全一致。
- 覆盖 SSE 连续 patch、重复 patch、乱序 patch、版本缺口、游标恢复、慢客户端、冻结竞态
  和 overlay 不改变评分身份。
- DeepSeek 请求数、缓存键、预算、截止和降级结果与优化前完全一致。

### 12.2 性能

继续满足权威固定上限：

- 5500 行标准化 P95 不超过 800ms，两源合并不超过 1000ms，统一快照可读不超过 1500ms；
- 单板 120 候选预选不超过 250ms，单板单策略评分不超过 250ms，三板三策略墙钟不超过
  1000ms，360 只稳定选择不超过 100ms；
- 本地草稿发布不超过 500ms，DeepSeek 结果重发布不超过 1s，SSE 入队不超过 2s；
- 当前/驻留历史 API 不超过 200ms，ETag 304 不超过 50ms，日期和状态 API 不超过 100ms；
- 同身份基线关键路径不得退化超过 5%，100 tick 项目分配增长不得超过 20%。

列式路径的额外激活条件：

- 5500 行标准化加两源合并的 P95 相对 scalar 基线至少改善 20%；
- P6 发布到 SSE 入队 P95 不超过 100ms；
- 正常在线推荐和 overlay 更新不产生完整 GET，只有明确 resync 才允许；
- 已收到的新闻/风险形成有效变更到 P6 发布 P95 不超过 1s，不含外部供应商等待；
- 固定 100 tick 后 P1-P6 总字节仍符合活动 256 MiB 契约，无无界增长或重复全量副本。

### 12.3 兼容与交付

- Python 3.10-3.14 均验证 Polars wheel、项目 wheel 和最小导入；任一版本失败则不激活。
- 运行 `make format-check`、`make lint`、`make type-check`、`make test` 和 `make package`。
- 仓库外安装 wheel 后验证 `trader`、`trader-cli`、`perf-check`、配置校验和全部 Web 资源。
- 1280x720、1440x900、1920x1080 实际渲染验证无白屏、重叠、页面级横向溢出或明显
  布局跳动，并验证 patch 更新时行、摘要和详情身份一致。

## 13. 故障与回退

- provider schema 失败：拒绝新批次，保留最近有效批次并标记来源降级。
- Polars 计算异常或等价断言失败：迁移验证期继续使用 scalar 结果；正式激活后该 tick 不
  发布新结果，保留最近有效快照并打开本地熔断，恢复失败时回退完整旧 release，不长期
  维护两套同时运行的生产计算。
- dirty 路由身份不完整：升级为确定性的全量重算，不猜测影响范围。
- P6 原子替换失败：保留上一版本；不得发送指向不存在版本的 patch。
- SSE 缺口或浏览器 patch 失败：发送 `resync_required`，客户端用 ETag 条件 GET 恢复。
- 正式冻结、检查点或收盘 overlay 失败：继续执行权威持久化恢复流程，不能以 SSE 成功
  代替持久化成功。
- 回退按完整旧 release 和对应配置执行；新列式缓存不写回旧运行库，也不删除冻结历史。

## 14. 开源参考与吸收边界

| 项目 | 吸收内容 | 明确不吸收 |
| --- | --- | --- |
| [Polars](https://docs.pola.rs/) / [Apache Arrow](https://arrow.apache.org/docs/format/Columnar.html) | 严格 dtype、列式内存、表达式、列裁剪和批处理 | Python UDF 热路径、未经测量的 streaming/lazy 复杂度 |
| [NautilusTrader](https://nautilustrader.io/docs/latest/concepts/message_bus/) | Data/Event/Command 区分、不可变消息、topic、correlation ID、adapter/DataEngine 边界 | 实盘执行、交易总线、Redis 和分布式部署 |
| [vn.py](https://github.com/vnpy/vnpy) | EventEngine/Gateway 分层、事件生命周期和可停止 worker | 券商接口、下单、桌面交易 UI |
| [OpenBB Provider](https://docs.openbb.co/odp/python/developer/extension_types/provider) | query 变换、数据提取、结果标准化和 provider schema | 平台运行时、外部交易结论和整套依赖 |
| [Qlib](https://github.com/microsoft/qlib/blob/main/docs/index.rst) | 点时数据、DataHandler、processor 和特征组织 | 训练、回测页面、在线调参和模型服务 |
| `daily_stock_analysis` | 本地仓库中的统一报价元数据、批处理、缓存、熔断和 provider 测试思路 | 可变字典/Pandas/智能体直接进入实时热路径 |
| CZSC | 标准 K 线、信号结构和时间序列测试思路 | 复制其交易策略或替代本项目评分契约 |
| TradingAgents 系列 | 研究证据组织和 provider 角色划分 | 多智能体生产编排、模型直接选股和低延迟 Web 主链 |

第三方机制只能重新实现为本项目边界；采用源码前必须固定 commit、核实许可证并记录归属。
社区 Star、外部回测和第三方推荐结果都不是本项目收益证据。

## 15. 假设与剩余风险

- 本计划优化的是数据新鲜度、重复计算、传输和资源效率；在完成至少 60 个交易日、300 条
  有效配对的点时样本外验证前，不能声称它提高实际荐股收益。
- 预期最大收益来自减少 Python 对象物化、按 dirty 集合重算和消除 SSE 后完整 GET；Polars
  对 5500 行的实际收益必须由固定 fixture 证明，不能凭库的宣传推断。
- 全市场横截面因子存在扩散效应，错误缩小 dirty 范围会改变排名；因此必须保留局部与全量
  等价测试，并在依赖不明确时保守升级为受影响板块或全量重算。
- SDK/API 的外部网络延迟不能由列式计算消除；latest-wins、timeout、熔断和 stale/degraded
  仍是实时性的主要保护。
- 256 MiB 可以在后续有测量证据时调整，但本章节先证明没有泄漏、无界队列或重复全量副本；
  增大内存不是替代数据生命周期治理的手段。
- 该计划尚未实施；当前活动代码仍使用既有标量数据路径和 SSE 失效通知，不能把目标指标
  描述为当前能力。
