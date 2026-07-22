# 实时荐股全链路优化：四 Codex 分阶段并行执行计划

状态：非权威执行计划，尚未实施。

本文合并 `plan_c.md` 的 DeepSeek 高价值协同计划和 `plan_sudu.md` 的 P1-P6 列式增量计划，
并把全部工作拆成 A、B、C、D 四个 Codex 在同一阶段并行执行的任务编号。

产品、架构、运行、API、运维和验收以[软件业务设计文档](software-business-design.md)为唯一
权威；候选、过滤、因子、评分、风险、DeepSeek、融合、动作和排名以
[荐股策略文档](recommendation-strategy.md)为唯一权威。发现冲突时以权威文档为准；实施引起
契约变化时先更新权威文档、失败测试和 `CHANGELOG.md`。

本计划是一个外部交付批次。四个 Codex 可以有内部提交，但所有阶段门禁未通过前，不得
部分上线、单独推送或宣称完成。

## 1. 四个 Codex 的固定身份

| Codex | 固定职责 | 独占生产范围 | 不得修改 |
| --- | --- | --- | --- |
| A | 契约、公共接口、集成和交付 | 权威文档、公共 ports/events、pipeline、publisher、bootstrap、配置、CHANGELOG | B/C/D 内部算法 |
| B | P1-P3 列式数据和 dirty 重算 | Provider、normalize/merge/features、列式内部类型、`MarketChangeSet` | DeepSeek、P6/Web、公共接线 |
| C | DeepSeek V4 高价值复核 | DeepSeek infra、review domain、新闻事实和本地映射 | Polars、P6/Web、公共接线 |
| D | P6 发布和 Web 增量更新 | P6 index、快照读投影、SSE、Web/DOM | P1-P3、DeepSeek、公共接线 |

没有第五个 Codex。A、B、C、D 从阶段 1 一直工作到阶段 5；每个阶段分别执行 `A阶段.x`、
`B阶段.x`、`C阶段.x`、`D阶段.x`，到达共同门禁后才能一起进入下一阶段。

## 2. 一眼开工表

| 阶段 | Codex A | Codex B | Codex C | Codex D | 共同门禁 |
| --- | --- | --- | --- | --- | --- |
| 阶段 1：盘点与契约 | A1.1-A1.5：基线、权威契约、接口冻结 | B1.1-B1.4：P1-P3 盘点和基线 | C1.1-C1.4：DeepSeek 盘点和基线 | D1.1-D1.4：P6/Web 盘点和基线 | G1：A 发布 `CONTRACT_BASE` |
| 阶段 2：并行实现 | A2.1-A2.5：公共骨架和测试替身 | B2.1-B2.7：列式和 dirty | C2.1-C2.8：V4、路由、预算、缓存 | D2.1-D2.7：P6、SSE、DOM | G2：四个实现包分别通过单域测试 |
| 阶段 3：集成与专业复验 | A3.1-A3.7：按 B -> C -> D 集成 | B3.1-B3.4：集成态等价/性能 | C3.1-C3.4：集成态请求/降级 | D3.1-D3.4：集成态 P6/Web | G3：全链功能闭环 |
| 阶段 4：全量验收与修复 | A4.1-A4.6：跨域门禁和问题分派 | B4.1-B4.4：列式修复/资源门禁 | C4.1-C4.4：成本/竞态门禁 | D4.1-D4.4：Web/桌面门禁 | G4：全部激活门禁通过 |
| 阶段 5：交付 | A5.1-A5.5：Review、文档、squash、推送 | B5.1-B5.2：列式终审签字 | C5.1-C5.2：DeepSeek 终审签字 | D5.1-D5.2：P6/Web 终审签字 | G5：`HEAD == @{upstream}` |

四个 Codex 启动后先同时执行阶段 1。B/C/D 完成各自阶段任务后向 A 交付报告并等待；只有 A
发布 G1 后四方才同时进入阶段 2。后续阶段同理，不允许某个 Codex 越过共同门禁提前施工。

### 2.1 并行工作区和交接规则

- 从同一已确认基线建立四个独立 worktree/分支：`codex/youhua-contract`、
  `codex/youhua-columnar`、`codex/youhua-deepseek`、`codex/youhua-p6-web`。
- 禁止四个 Codex 在同一工作树执行 `git add/commit/rebase`。环境不能提供独立 worktree 时，
  B/C/D 只产出补丁，公共工作树和所有 Git 操作由 A 独占。
- A 独占 `docs/software-business-design.md`、`docs/recommendation-strategy.md`、
  `CHANGELOG.md`、`pyproject.toml`、`bootstrap.py`、`application/ports/*`、`pipeline*`、
  `publisher.py`、公共 events/status 和全局配置。
- B 只修改 `infra/market_data/` 内 provider/normalize/merge/feature/columnar 相关文件及其测试；
  C 只修改 `infra/deepseek/`、`domain/review/`、新闻/映射相关文件及其测试；D 只修改
  `web/`、P6 专用 index、快照读取/投影相关文件及其测试。
- B/C/D 需要公共接口变化时只向 A 提交接口申请，包含动机、最小签名、调用方、兼容性和
  失败测试；不得直接改 A 的文件或建立第二套 schema/version。
- 每阶段报告和 G2 标准交接包固定包含：

```text
codex_and_phase
base_commit
head_commit_or_patch
owned_paths_changed
contract_assumptions
schema_or_migration_changes
tests_run_and_results
performance_before_after（适用时）
known_failures_and_risks
requested_interface_changes
ready_for_gate=yes/no
```

### 2.2 四方共同遵守的不变项

- today 11:20、tomorrow/d25 14:50 冻结；long 只展示当前观察，不冻结、不产生动作、不写
  推荐历史且 DeepSeek 物理请求永久为 0。
- 候选、硬过滤、板内/同行、因子、风险、下行保护、动作、集中度、排序、TopK、冻结和哈希
  语义不变；相同输入必须得到相同业务 JSON 和物理请求数。
- `local_score` 已扣本地风险，融合时不得重复扣除：

```text
final_score = clamp(
    local_score * 0.68
    + deepseek_score * 0.32
    - deepseek_risk_penalty,
    0,
    100,
)
```

- 最终分继续使用 `ROUND_HALF_UP`，固定验收向量为 `83.40`。
- DeepSeek、列式或外部来源失败时保留最近有效快照并显式降级；DeepSeek 全部失败仍发布完整
  本地推荐；普通 HTTP 不现场抓行情、评分、调用 DeepSeek 或写盘。
- 依赖方向保持 `entrypoints/web/infra -> application -> domain`，Polars 不进入 domain、应用
  公共端口或 Web；`bootstrap.py` 是唯一组合根。
- `polars>=1.43,<2` 必须验证 Python 3.10-3.14 wheel；初始不引入 PyArrow。内存遵守下节的
  “248 MiB 逻辑缓存 + 384 MiB 迁移期进程峰值”双层门禁。
- 不引入 Kafka、Redis、Flink、WebSocket、分布式执行或新交易能力；不建设历史 60 日回补、
  300 条配对、Bootstrap、shadow、自动调权或在线学习。

### 2.3 迁移期 384 MiB 进程硬上限

内存使用两个不能互相替代的指标：

| 指标 | 上限 | 统计范围 |
| --- | ---: | --- |
| P1-P6 逻辑缓存载荷 | 248 MiB | 规范序列化载荷、缓存 identity 和按池计费内容 |
| 迁移期进程峰值 RSS | 384 MiB（402,653,184 字节） | Python、Polars 原生缓冲、线程栈、队列、临时副本、新旧 epoch、scalar/columnar 双路径和 P1-P6 全部进程内存 |

P1-P6 逻辑池仍为 P1 128、P2 56、P3 24、P4 16、P5 12、P6 12 MiB，合计 248 MiB。
384 MiB 是迁移期整个进程的硬上限，不是新的缓存容量；不得把多出的 136 MiB 分给缓存、
扩大候选、延长 TTL 或保留更多历史。

迁移期至少同时记录：

- `cache_logical_bytes`：P1-P6 分池及合计逻辑载荷；
- `process_rss_bytes` 和 `process_peak_rss_bytes`：实际常驻和峰值内存；
- `process_uss_bytes`：环境支持时记录进程独占内存，用于解释共享库影响；
- `python_traced_bytes`：只用于 Python 分配诊断，不得代替 RSS；
- `polars_estimated_bytes`：各活动 DataFrame/LazyFrame 结果的原生列式估算；
- `transient_peak_reason`：峰值对应的阶段、输入 identity 和新旧版本并存原因。

延迟测试关闭 `tracemalloc`，内存测试单独开启；Linux 优先同时读取进程 RSS/峰值和
`/proc/self/smaps_rollup`，避免 Polars 原生内存被 Python 计量遗漏。

阶段 2-4 以及切换验收均使用 384 MiB 硬上限。删除 scalar/columnar 双路径后必须另测纯
columnar 稳态和峰值；本计划不自动把上限回落到 320 或 256 MiB，后续只能依据实测证据在
独立契约变更中收紧。

## 3. 阶段 1：并行盘点、基线和契约冻结

### Codex A：A1.x

- **A1.1 工作树封存**：记录 `HEAD`、`@{upstream}`、当前分支、全部已有修改和文件 owner。
  现有修改均视为用户资产，不 reset、checkout 覆盖、stash 或清理。
- **A1.2 全局基线**：运行当前适用的质量、测试、package、性能和三档 Web 基线；同时记录
  scalar 启动、P1 预热、全市场评分、DeepSeek 批次、P6 发布和 100 tick 的 RSS/peak RSS/USS、
  逻辑缓存及 Python 分配基线。外部服务或图形环境不可用项如实记录，不能伪造通过。
- **A1.3 权威契约**：先更新两份权威文档中 P1-P6、DeepSeek V4、P6/SSE、回退和验收契约，
  明确 248 MiB 逻辑缓存与 384 MiB 迁移期进程峰值双层内存契约，并添加公共失败测试。
- **A1.4 接口冻结**：定义并版本化三个公共接缝：
  1. P3 -> P4：`FeatureSnapshot + MarketChangeSet`；
  2. P4 -> P5：高价值复核集合、证据 manifest、价格反应桶、owner strategy；
  3. P4/P5 -> P6：完整 projection event 和 overlay event。
- **A1.5 发布门禁**：汇总 B1/C1/D1 的接口需求，形成唯一 owner 清单、schema/version 清单、
  合并顺序和 `CONTRACT_BASE=<commit>`。

A1 产物：基线报告、权威契约 diff、公共失败测试、接口清单、owner 清单、contract commit。

### Codex B：B1.x

- **B1.1 Provider 盘点**：列出 SDK/API adapter、query/extract/transform 路径、时间/版本/血缘/
  缺失处理和当前测试覆盖。
- **B1.2 P1-P3 盘点**：定位报价统一、候选过滤、板内/同行统计、特征物化、latest-wins 和
  scalar 热点，列出将修改和新增的文件。
- **B1.3 基线测量**：记录 5500 行标准化、两源合并、360 候选、100 tick 的 scalar 输出、
  逻辑缓存、RSS/peak RSS/USS 和 Python 分配 fixture；此阶段不改生产代码。
- **B1.4 接口申请**：向 A 提交 `MarketChangeSet` 最小字段、`FeatureSnapshot` 接缝和所需测试
  替身，不自行创建公共 port。

B1 产物：P1-P3 盘点报告、owned paths、scalar fixture、性能/内存基线、接口申请。

### Codex C：C1.x

- **C1.1 Review 盘点**：定位现有 schema、prompt/model 版本、Flash/Pro、缓存、预算、重试、
  局部修复和状态指标。
- **C1.2 业务盘点**：定位 long 调用入口、新闻公告证据、融合、风险处罚、旧冻结回放和全部
  失败降级路径。
- **C1.3 请求基线**：用现有 fixture 记录三策略同股请求数、窗口、软/硬桶、并发/重启计数和
  固定融合 `83.40`；此阶段不改生产代码。
- **C1.4 接口申请**：向 A 提交高价值复核输入、V4 facts 输出、manifest/价格反应桶和 owner
  strategy 的最小接口，不修改公共 port。

C1 产物：DeepSeek 盘点报告、owned paths、请求/预算基线、schema 缺口、接口申请。

### Codex D：D1.x

- **D1.1 发布盘点**：定位 publisher、快照读写、冻结/检查点/收盘 overlay、当前和历史 GET。
- **D1.2 SSE/Web 盘点**：定位事件 ID、游标恢复、慢客户端、ETag、完整刷新和 DOM 行身份。
- **D1.3 热路径基线**：记录 SQLite/文件访问、P6 缺口、GET/SSE 时延、正常更新完整 GET 次数
  和三档页面表现；此阶段不改生产代码。
- **D1.4 接口申请**：向 A 提交 projection/overlay event、patch schema、CAS/version 和 resync
  的最小接口，不修改 publisher 公共接口。

D1 产物：P6/Web 盘点报告、owned paths、读取/传输/桌面基线、接口申请。

### G1：阶段 1 完成条件

A 收齐 B1/C1/D1 后发布 `CONTRACT_BASE`。四方必须确认：

- 所有接口只有一个版本和 owner；
- B/C/D 各自 worktree 从同一个 contract commit 开始；
- 阶段 2 测试替身和输入 fixture 可用；
- 未覆盖用户已有修改；
- G1 未满足时，B/C/D 继续等待，不进入生产实现。

## 4. 阶段 2：四线并行实现

### Codex A：A2.x 公共骨架

- **A2.1 公共类型**：实现已冻结的 application ports/events、identity、schema/version 校验，
  并在配置和状态契约中区分 248 MiB 逻辑缓存与 384 MiB 进程峰值，拒绝把二者混为一个字段。
- **A2.2 测试替身**：为 B 提供 P4 consumer，为 C 提供 `MarketChangeSet`/复核输入，为 D 提供
  projection/overlay producer；替身只用于隔离开发。
- **A2.3 公共失败测试**：覆盖依赖方向、long 零请求、固定融合 `83.40`、冻结身份、P6 原子性、
  HTTP 只读、不增加物理 HTTP、逻辑缓存超过 248 MiB 拒绝和进程峰值超过 384 MiB 不得激活。
- **A2.4 变更审批**：处理 B/C/D 的接口变更申请；需要修改公共文件时只由 A 修改并发布新的
  contract amendment，三方不得各自分叉。
- **A2.5 集成骨架**：建立集成测试场景和合并队列，但不连接真实 B/C/D 实现、不切生产默认。

A2 完成标志：公共骨架和替身测试通过，contract 未产生未记录漂移。

### Codex B：B2.x P1-P3 列式和 dirty

- **B2.1 Provider Adapter**：固定 `transform_query -> extract_data -> transform_data`；校验代码、
  时间、版本、非有限值、deadline、字段血缘和缺失原因，缺失只用 `null`。
- **B2.2 列式类型**：实现不可变 `ColumnarQuoteBatch`、`ColumnarResearchBatch`、
  `ColumnarFeatureBatch`、`MarketChangeSet`，包含 manifest/content hash 和版本身份。
- **B2.3 dtype/表达式**：使用严格 String/Categorical/Enum/Float64/Int/时区时间/三态 Boolean；
  禁止 `Object`、热路径 Python UDF/lambda；默认 eager，lazy 必须由 fixture 证明。
- **B2.4 P1/P2**：adapter 直接构造列数组；保持一个在途加一个 latest-wins；按
  `source_time -> received_at -> 来源优先级 -> data_version -> hash` 合并，腾讯复核和 0.50%
  冲突规则不变。
- **B2.5 P3**：用 Polars 原生表达式完成硬过滤、板内/同行、截尾、分位、带通和可靠度；三板
  隔离、最多 5 日回退、同行排除自身不变；最多 360 只候选在 P4 前物化一次。
- **B2.6 Dirty**：输出 changed codes/boards/industries/field families、evidence manifest、risk、
  overlay only 和 full invalidation reason；不确定时扩大为板块或全量。
- **B2.7 单域验证**：逐字段 scalar/columnar 等价，局部/全量重算等价，5500 行性能和 100 tick
  内存测试；分别报告逻辑缓存、RSS/peak RSS/USS、Python 分配和 Polars 原生估算，覆盖新旧
  P2/P3 epoch 并存与 scalar/columnar 双路径峰值；不得复制领域评分。

B2 完成标志：B 的 production/test diff、schema、fixture、等价/性能报告形成标准交接包。

### Codex C：C2.x DeepSeek V4、路由和预算

- **C2.1 long 隔离**：long 复核集合永久为空，预算层拒绝 long 主审、Pro、预热和 emergency。
- **C2.2 V4 facts**：只返回催化方向/重要度/确认/周期/引用、价格反映、基本面、行业政策、
  监管/减持/解禁/质押/诉讼/业绩风险、冲突、覆盖和 `abstain`。
- **C2.3 新闻证据**：本地点时过滤、来源分级、事件去重；只有官方或两个独立可信来源支持的
  正向事实可加分，传闻/软新闻/标题情绪不加分，同一事件不重复加分。
- **C2.4 本地映射**：维度从 50 开始并限制 30-70；催化高/中/低按方向记
  `±15/±8/±3`，预期差未反映/部分/充分反映记 `±12/±5/0`，基本面改善/稳定/恶化记
  `+15/+3/-18`，政策正向/中性/负向记 `+10/0/-12`。证据质量为官方 `1.00`、两个可信
  来源 `0.85`、单一可信来源 `0.65`、软信息 `0.40`、冲突最高 `0.25`；至少三个维度有效且
  加权覆盖不低于 `0.60` 才使用模型分。实际风险只进入 penalty/veto，固定融合保持 `83.40`。

  C2.4 使用以下策略权重：

| 策略 | 催化 | 预期差 | 基本面 | 行业政策 | 风险质量 |
| --- | ---: | ---: | ---: | ---: | ---: |
| today | 30% | 25% | 10% | 15% | 20% |
| tomorrow | 25% | 25% | 15% | 15% | 20% |
| d25 | 20% | 20% | 25% | 20% | 15% |

- **C2.5 高价值路由**：只复核新重大风险/催化、动作门槛 5 分内、Top18/Top10 边界、证据或
  量价冲突、19-30 名中新事实；硬过滤失败、低可靠、数据不完整和远离边界者不调用。
- **C2.6 时间和预算**：落实 shared 10、today 22、tomorrow 14、d25 12、Pro 8、emergency 5
  软上限；11:18/14:48 截止；硬桶 today 70、tomorrow 45、d25 35、long 0、shared 15、
  emergency 5、全局 188，所有真实 HTTP/失败/重试/修复均原子计数。
- **C2.7 批处理缓存**：Flash 普通事实、Pro 预注册冲突；每股最多 12 条证据、1-8 股自适应、
  Pro 最多 4 股；逐股接受、错误子集修复；同股三策略共享原始 facts，本地投影。
- **C2.8 单域验证**：覆盖并发、重启、迟到、窗口、预算、局部修复、旧冻结回放和全部失败
  本地降级，并记录最大 8 股 Flash、最大 4 股 Pro、局部修复和响应校验的进程峰值；不得输出
  动作、排名、目标价或最终分。

C2 完成标志：C 的 production/test diff、schema/prompt/cache 版本、预算向量和请求报告形成
标准交接包。

### Codex D：D2.x P6、SSE 和浏览器

- **D2.1 P6 index**：实现四策略 current pin 和最近 20 个交易日完整三策略 resident history；
  只接受 version/manifest/SHA-256 合格的完整投影并原子替换。
- **D2.2 热读**：当前/驻留历史 GET、日期和 ETag 不访问 SQLite/文件；更老日期按日期
  single-flight 冷读并一次预取三策略，部分失败不冒充完整。
- **D2.3 持久化分流**：普通 local/hybrid 草稿只更新 P6/SSE；检查点、冻结、
  `close_fallback`、15:00 overlay 才按权威契约持久化。
- **D2.4 SSE patch**：实现 `patch_schema_version=2`、base/current projection version、upserts、
  removed codes、ETag、frozen 和 overlay 限定字段；保留事件 ID、`Last-Event-ID`、有界缓冲和
  `resync_required`。
- **D2.5 浏览器状态机**：以 `strategy + trade_date + view + code` 为行身份局部更新；只有版本/
  游标/schema/身份/TopK/ETag 失败时才完整 GET；冻结后拒绝迟到草稿。
- **D2.6 故障处理**：P6 替换失败保留旧版且不发 patch；SSE/DOM 失败触发 ETag resync；慢
  客户端不得阻塞 publisher。
- **D2.7 单域验证**：覆盖连续/重复/乱序/缺口 patch、游标恢复、慢客户端、冻结竞态、零热读、
  正常更新零完整 GET、P6/SSE 性能和三档桌面，并记录 72 个视图、新旧 P6 原子替换、冷读
  预取及慢客户端缓冲同时存在时的进程峰值。

D2 完成标志：D 的 production/test diff、patch schema、热读/传输/桌面报告形成标准交接包。

### G2：阶段 2 完成条件

- A2、B2、C2、D2 单域测试分别通过；
- B/C/D 标准交接包都基于同一个 `CONTRACT_BASE`；
- 没有越权修改、重复公共 schema 或未审批接口漂移；
- A 明确宣布进入阶段 3，B/C/D 不自行合并公共分支。

## 5. 阶段 3：集成和专业域复验

### Codex A：A3.x

- **A3.1 合并 B**：在独立集成分支合并 B，运行 P1-P3、等价和 change set 测试。
- **A3.2 合并 C**：合并 C，把 evidence/risk changes 接入高价值路由；证明普通价格 change
  命中缓存时只做本地投影，不增加 DeepSeek 请求。
- **A3.3 合并 D**：合并 D，把完整 projection/overlay event 接入 P6/SSE。
- **A3.4 全链接线**：修改 A 独占的 pipeline、ports、publisher、bootstrap 和默认配置，闭合
  source -> P1/P2/P3 -> P4 -> P5 -> P6 -> SSE。
- **A3.5 身份收敛**：删除重复 identity/schema/version，确保 manifest、trade date、policy、
  schema、projection version 和 CAS 只有一套定义。
- **A3.6 集成测试**：运行功能闭环、冻结竞态、旧 epoch、失败降级和 HTTP 只读测试。
- **A3.7 问题分派**：公共接线由 A 修；列式内部退 B；DeepSeek 内部退 C；P6/Web 内部退 D。

### Codex B：B3.x

- **B3.1** 在 A 合并 B 后，基于集成 commit 重跑 scalar/columnar 逐字段等价。
- **B3.2** 验证 change set 局部结果与相同输入全量重算一致。
- **B3.3** 运行 5500 行、360 候选、100 tick 和 scalar/columnar 双路径性能/内存测试，确认
  P1-P6 逻辑缓存 <= 248 MiB、集成进程峰值 RSS <= 384 MiB。
- **B3.4** 只修复 A 分派的 B 租约问题，提交补丁和更新报告，不直接改集成文件。

### Codex C：C3.x

- **C3.1** 在 A 合并 C 后，验证同股跨三策略只产生一次原始 facts 请求。
- **C3.2** 验证普通 price change 不新增 HTTP，manifest/risk change 才进入路由。
- **C3.3** 重跑 long 零请求、窗口、硬桶 188、并发/重启、迟到和全部失败降级。
- **C3.4** 只修复 A 分派的 C 租约问题，提交补丁和更新报告。

### Codex D：D3.x

- **D3.1** 在 A 合并 D 后，验证 P4/P5 重发布只更新相应股票、全局选择和 patch。
- **D3.2** 验证 current/resident 热读零 SQLite/文件访问及冷读 single-flight。
- **D3.3** 重跑 patch/游标/慢客户端/冻结/overlay/ETag 和正常更新零完整 GET，并把 P6 原子
  替换峰值纳入 A 的进程峰值汇总。
- **D3.4** 只修复 A 分派的 D 租约问题，提交补丁和更新报告。

### G3：阶段 3 完成条件

- source -> P6 -> Web 全链可运行；
- DeepSeek 可用和不可用两条链都产生正确完整投影；
- 四个 Codex 的集成态专业报告均通过；
- 所有已知集成问题已归属 owner，没有未解释额外 HTTP、热读或重复扣分。

## 6. 阶段 4：全量验收和修复

### Codex A：A4.x

- **A4.1 正确性**：验证 local/deepseek/risk/final score、动作、排名、TopK、业务 JSON、冻结
  SHA-256 完全一致，固定向量 `83.40`。
- **A4.2 跨域故障**：注入 provider、Polars、DeepSeek、P6、持久化、SSE、进程重启、旧 epoch
  和冻结失败，验证统一回退。
- **A4.3 完整质量门禁**：运行 format、lint、type、test、package 和仓库外 wheel。
- **A4.4 兼容门禁**：验证 Python 3.10-3.14、CLI、perf-check、配置和 Web 资源。
- **A4.5 性能总门禁**：汇总 B/C/D 数据，验证关键路径、API 和发布；在缓存接近各池上限时，
  覆盖 P1 预热、P2/P3 全量计算、新旧 epoch、scalar/columnar 双路径、最大 DeepSeek 批次、
  P6 原子替换、冷读预取和慢客户端并存场景，要求逻辑缓存 <= 248 MiB 且任一场景进程峰值
  RSS <= 384 MiB。
- **A4.6 问题闭环**：建立失败清单，按 owner 分派；修复后从失败用例开始并重跑完整门禁。

### Codex B：B4.x

- **B4.1** 列式标准化加两源合并相对 scalar 至少改善 20%。
- **B4.2** 5500 行标准化 P95 <= 800ms、两源合并 <= 1000ms、统一快照 <= 1500ms。
- **B4.3** 单板预选/评分 <= 250ms、三板三策略 <= 1000ms、稳定选择 <= 100ms。
- **B4.4** 100 tick 后 P1-P6 逻辑缓存 <= 248 MiB、分配增长 <= 20%；迁移期和集成压力场景
  `process_peak_rss_bytes <= 402653184`，修复并复验 B 域失败。

### Codex C：C4.x

- **C4.1** 验证 normal <= 58、含 Pro <= 66、含 emergency <= 71 和全局硬上限 188。
- **C4.2** 验证传闻不加分、事件不重复加分、Pro 不解除硬过滤/下行保护。
- **C4.3** 验证请求、失败、重试、schema 修复原子计数及缓存/本地投影不计数。
- **C4.4** 验证 DeepSeek 结果重发布 <= 1s，修复并复验 C 域失败。

### Codex D：D4.x

- **D4.1** P6 -> SSE 入队 P95 <= 100ms，权威 SSE 上限 <= 2s。
- **D4.2** 当前/驻留 API <= 200ms、ETag 304 <= 50ms、日期/状态 <= 100ms。
- **D4.3** 正常在线更新完整 GET 为 0，仅明确 resync 允许；验证传输字节节省。
- **D4.4** 1280x720、1440x900、1920x1080 桌面验收，修复并复验 D 域失败。

### G4：阶段 4 完成条件

```text
make format-check
make lint
make type-check
make test
make package
```

以上命令、正确性、性能、内存、Python 兼容、仓库外 wheel 和三档桌面全部通过；任何失败都
必须有 owner 并完成复验。G4 的内存证据必须同时包含各池逻辑字节、RSS/peak RSS、可用时的
USS、Polars 原生估算和峰值原因；缺少任一关键证据或超过 384 MiB 时保持 scalar 默认和现有
Web 路径，不得部分激活、扩大缓存或仅用 `tracemalloc` 结果宣称通过。

## 7. 阶段 5：终审、提交和推送

### Codex A：A5.x

- **A5.1 完整 diff Review**：检查依赖方向、额外 HTTP、重复扣分、冻结竞态、SQLite 热读、
  无界队列/缓存、临时双跑、调试开关、测试豁免和敏感日志。
- **A5.2 文档闭合**：同步权威文档、schema、配置、迁移、测试和 `CHANGELOG.md`，记录迁移期
  384 MiB 硬上限、实测峰值、峰值场景、纯 columnar 稳态以及后续收紧上限的独立决策项。
- **A5.3 签字收集**：收齐 B5/C5/D5 的终审报告和剩余风险。
- **A5.4 单一提交**：保留内部 commit 对照后 squash，用一个准确的 Conventional Commit 提交。
- **A5.5 推送确认**：推送跟踪分支并确认 `HEAD == @{upstream}`，然后停止等待用户指令。

### Codex B：B5.x

- **B5.1** 审查最终 diff 中 P1-P3、dirty、性能和内存是否与 B 报告一致。
- **B5.2** 向 A 提交“通过/不通过、证据、剩余风险”，不得自行推送。

### Codex C：C5.x

- **C5.1** 审查最终 diff 中 V4、映射、路由、缓存和预算是否与 C 报告一致。
- **C5.2** 向 A 提交“通过/不通过、证据、剩余风险”，不得自行推送。

### Codex D：D5.x

- **D5.1** 审查最终 diff 中 P6、持久化分流、SSE、DOM 和 resync 是否与 D 报告一致。
- **D5.2** 向 A 提交“通过/不通过、证据、剩余风险”，不得自行推送。

### G5：最终完成条件

- B/C/D 均明确签字通过；
- A4 全部门禁仍通过；
- 文档、代码、测试、配置和 CHANGELOG 一致；
- 最终只有一个交付 commit，且 `HEAD == @{upstream}`。

## 8. 四个 Codex 的启动指令

同时打开四个 Codex，分别发送以下内容：

```text
给 Codex A：
你是本任务 Codex A。严格按 docs/plan_youhua.md 执行 A1.x 到 A5.x。现在先完成 A1.x；
收齐 B1/C1/D1 报告后发布 CONTRACT_BASE 和 G1。每个阶段等待共同门禁，不执行 B/C/D 内部
算法。你独占公共文件、集成工作树、提交和推送。

给 Codex B：
你是本任务 Codex B。严格按 docs/plan_youhua.md 执行 B1.x 到 B5.x。现在只完成 B1.x 并把
报告交给 A；收到 CONTRACT_BASE/G1 后再执行 B2.x。每个阶段等待共同门禁，只修改 B 文件
租约，不执行 DeepSeek、P6/Web 或公共接线。

给 Codex C：
你是本任务 Codex C。严格按 docs/plan_youhua.md 执行 C1.x 到 C5.x。现在只完成 C1.x 并把
报告交给 A；收到 CONTRACT_BASE/G1 后再执行 C2.x。每个阶段等待共同门禁，只修改 C 文件
租约，不执行 Polars、P6/Web 或公共接线。

给 Codex D：
你是本任务 Codex D。严格按 docs/plan_youhua.md 执行 D1.x 到 D5.x。现在只完成 D1.x 并把
报告交给 A；收到 CONTRACT_BASE/G1 后再执行 D2.x。每个阶段等待共同门禁，只修改 D 文件
租约，不执行 P1-P3、DeepSeek 或公共接线。
```

## 9. 统一回退、可观测性和延期项

每个阶段记录 request started、source received、normalized、merged、change set、features、score、
DeepSeek ready、P6 published、SSE enqueued 和桌面诊断 browser applied 时间。`/api/status` 汇总
阶段 P50/P95、dirty 范围、局部/全量重算、P1-P6 逻辑字节、RSS/peak RSS/USS、Python 分配、
Polars 原生估算、峰值原因、SSE patch/resync、scalar/columnar、DeepSeek 批次/缓存/token/
预算/降级。日志不写载荷正文、密钥、Token、prompt 或模型自由文本。

统一回退：Provider schema 失败保留最近有效批次；Polars 失败验证期用 scalar、激活后保留
旧快照并按完整 release 回退；dirty 身份不完整则扩大重算；DeepSeek 失败发布完整本地推荐；
P6 失败保留旧版；SSE/DOM 失败执行 ETag resync；冻结/检查点/收盘失败继续权威持久化恢复；
逻辑缓存超过 248 MiB 时只按本池规则拒绝/淘汰，进程峰值超过 384 MiB 时本轮验收
失败、禁止激活，不得通过提高缓存上限绕过。

收益验证另立用户明确授权批次：向前收集至少 60 个交易日、300 条有效配对并按交易日分块
Bootstrap。完成前只能声明结构、成本、实时性和可靠性改善，不能宣称提高荐股收益。

本计划尚未实施；当前活动代码和指标仍以仓库现状为准。
