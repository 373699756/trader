# Codex 执行计划：多源并行采集、三板独立评分与统一合并
TUSHARE_TOKEN=ab949aa6734976e0ae0e0caeff3e331c4da3eadcbb82e69840913674
## 1. 使用方法

本文供 Codex 直接执行。它规定交付批次、文件范围、接口、计算口径、测试、
Review、提交和停止条件。`docs/need.md` 仍是业务契约唯一权威；实现前必须先将
本文对应批次的固定口径写入 `docs/need.md`，不得只修改代码或配置。

本文不授权一次性执行全部内容。用户每发送一次“继续”时，只执行下一个完整
未完成批次。每批必须独立 Review、提交、推送并核对上游，然后停止。

状态标记：

- `[ ]`：尚未开始；
- `[~]`：当前批唯一允许的执行中任务；
- `[x]`：已完成、已验证、已提交并已推送；
- `[!]`：被外部条件阻断，必须记录证据后停止。

### 1.1 唯一任务序列

| 顺序 | 任务 | 状态 | 下一步条件 |
| ---: | --- | --- | --- |
| 0 | 将计划改成Codex可执行规范 | `[x]` | 本文提交并推送 |
| 1 | v15多源并行采集与结构化合并 | `[~]` | 当前唯一执行批次 |
| 2 | v16三板独立评分与统一合并 | `[ ]` | 任务1完成并再次收到“继续” |
| 3 | v17性能、缓存与实时性硬化 | `[ ]` | 任务2完成并再次收到“继续” |

Codex必须从上表第一个 `[ ]` 项开始，不得跳项、合并两项或在完成后自动继续。

## 2. 不可变决策

- 当前活动生产基线按 `strategy_v14` 处理。
- 批次一生成 v15 数据与结构化合并契约，但不启用新评分。
- 批次二生成 v16 三板评分与统一选择契约。
- 首期只改造 `tomorrow` 和 `d25`。
- `today`、`long`、11:20/14:50 冻结、14:48 DeepSeek 截止保持不变。
- DeepSeek 正常目标158次、物理请求硬上限188次保持不变。
- 固定融合仍为本地68%、DeepSeek 32%，DeepSeek风险扣分为绝对分值。
- Tushare是可选慢数据增强源，不承担全市场或TopK高频实时报价。
- 沪深主板、创业板、科创板分别使用独立单worker评分lane。
- 三板lane用于隔离任务、背压和失败域，不预设CPython线程能让纯Python计算CPU并行；
  效率结论只看固定负载墙钟、进程CPU时间和实际加速比。
- 三板评分结果必须经过单一确定性合并器后才能发布或冻结。
- 同行差与换手冲击采用趋势延续方向，不实现补涨回归分支。
- d25质量价值输入固定为质量50%、价值30%、成长20%。
- v16动作门槛固定为 `tomorrow=78`、`d25=76`，观察带为5分。
- 实时路径采用有界内存缓存和 stale-while-revalidate，不增加第二个数据库。
- 缓存不得改变点时结果、风险事实、融合模式、排名或冻结身份。
- 性能优化不得放宽数据年龄、超时、风险门或冻结截止。
- 普通测试验证确定性和调用次数；墙钟性能只由显式性能门禁判定。
- 不增加真实下单、产品内回测、自动调参、机器学习或收益承诺。

## 3. 每批统一执行协议

### 3.1 开始前

依次执行并记录：

```bash
git rev-parse HEAD
git rev-parse '@{upstream}'
git status --short --branch
git diff --check
```

执行要求：

1. 确认前一批本地 `HEAD` 与 `@{upstream}` 相同。
2. 列出已有修改，并区分用户修改与本批文件。
3. 不得 reset、checkout、删除或格式化用户已有修改。
4. 只把当前批一个计划项标为 `[~]`。
5. 先读 `AGENTS.md`、`docs/need.md`、本计划、相关实现和测试。

### 3.2 实施顺序

每批严格按以下顺序：

1. 修改 `docs/need.md` 契约。
2. 增加或修改失败先行测试，确认测试因目标行为缺失而失败。
3. 修改类型、端口和配置 schema。
4. 修改纯领域计算。
5. 修改基础设施和应用编排。
6. 修改持久化、API和UI展示。
7. 运行局部测试，再运行完整门禁。
8. 更新 `CHANGELOG.md` 的 Added/Changed/Fixed/Removed、Verification、
   Residual Risks 中适用内容。
9. 基于上一次已推送提交做完整 diff Review，修复到零已知问题。
10. 仅暂存本批文件，创建一个提交，推送并核对哈希。

所有手工修改使用 `apply_patch`。禁止新建第二套配置、线程池、时钟、网络客户端
组合根或持久化真相源。

### 3.3 每批完整门禁

```bash
make format-check
make lint
make type-check
make test
make package
git diff --check
```

另外必须在仓库外安装最终 wheel，验证：

- `import trader` 来源为安装目录；
- `trader-cli --help` 和 `trader-cli validate-config` 可执行；
- 模板、CSS、JavaScript和SVG均可读取；
- `pip check` 无 broken requirements；
- 1280x720、1440x900、1920x1080 无白屏、重叠或横向溢出。

### 3.4 性能测试统一规则

性能相关测试分成两类：

1. 普通门禁使用注入时钟和计数器，验证任务数、请求数、缓存命中、淘汰、
   single-flight、复制次数、队列容量和乱序保护，不断言真实毫秒值。
2. `tests/performance` 使用固定录制数据和真实 `perf_counter`，只由各批显式性能
   runner运行；v17统一收口为 `trader-cli perf-check`，输出JSON报告并按本计划阈值
   判定。

不得引入第二套 benchmark 框架或依赖；使用标准库、现有 pytest 和项目已有
统计函数。每项性能结果至少预热1轮、测量5轮，报告样本数、P50、P95、最大值、
命中率、峰值条目数和运行环境摘要。失败报告必须保留，禁止重跑到偶然通过。
P50/P95统一使用nearest-rank，即排序后取 `ceil(p * n) - 1`，不得插值。
runner必须先原子写入完整报告，再按通过返回0、预算失败返回1、输入或配置无效返回2；
不得因失败跳过报告。

所有墙钟、数据年龄、内存增长和相对退化预算只序列化在
`runtime.json.performance_budgets`。该对象固定包含 `schema_version`、`workload`、
`rounds`、`latency_p95_ms`、`data_age_p95_seconds`、`memory` 和
`relative_regression_percent`；`rounds` 固定为预热1轮、测量5轮。本文各批表格规定
实现时必须写入的值，生产代码、测试和性能脚本不得保留回退默认值或复制常量。
settings loader必须拒绝字段缺失、未知字段、非正有限值、轮数或固定工作负载漂移；
配置契约测试固定完整键集和数值，`perf-check` 报告必须记录该配置片段的规范化哈希。

## 4. 目标运行结构

```text
五个来源lane
  东方财富 / 新浪 / 腾讯 / Tushare / AKShare
                    |
                    v
            SourceObservation
                    |
                    v
         CanonicalMarketSnapshot
                    |
        +-----------+-----------+
        |           |           |
        v           v           v
   main lane   chinext lane   star lane
        |           |           |
        +-----------+-----------+
                    |
                    v
          全局DeepSeek复核规划器
                    |
                    v
       单一融合、选择、发布和冻结合并器
```

`create_app()` 不启动上述任何 worker。worker 只能由 `bootstrap.py` 组合并由运行时
生命周期显式启动、停止和等待。

## 5. 批次一：v15 多源并行采集与结构化合并

### 5.1 批次目标

将当前串行行情回退改为独立来源采集，生成可复盘的单源观测与统一行情快照，
完成三板身份和第一批风险门。该批不得改变 v14 本地评分、动作门槛或 TopK。

### 5.2 允许修改的文件

契约与配置：

- `docs/need.md` 第6-9、18、22-26节；
- `pyproject.toml`；
- `config/v2/runtime.json`；
- `config/v2/strategy.json`；
- `src/trader/infrastructure/settings*.py`。

类型、端口与编排：

- `src/trader/domain/models.py`；
- 新建 `src/trader/application/cache.py`；
- `src/trader/application/ports.py`；
- `src/trader/application/pipeline*.py`；
- `src/trader/application/candidate_features.py`；
- `src/trader/bootstrap.py`。

行情与结构化合并：

- 新建 `src/trader/infrastructure/cache.py`；
- 新建 `src/trader/infrastructure/market_data/observations.py`；
- 新建 `src/trader/infrastructure/market_data/merge.py`；
- 新建 `src/trader/infrastructure/market_data/tushare.py`；
- 修改 `gateway.py`、`router.py`、`normalize.py`、`service*.py`、
  `features.py` 和 `calendar.py`。

持久化与只读接口：

- `src/trader/infrastructure/persistence/*.py`；
- `src/trader/web/schemas.py`、`serializers.py` 和静态详情展示；
- `tests/unit`、`tests/component`、`tests/contract`、`tests/integration` 中
  仅与本批行为对应的测试；
- 新建 `tests/performance/run_v15_market_data.py`、
  `tests/fixtures/performance/v15/manifest.json` 和脱敏录制fixture。

不得修改三板 v16 评分权重实现，不得提前修改 tomorrow/d25 动作门槛。

### 5.3 先写契约

将 `docs/need.md` 第26节改成明确状态：

- `baseline_v14_active`：当前活动评分和TopK；
- `v15_parallel_data_contract`：本批完成后活动的数据契约；
- `v16_board_scoring_contract`：下一批待启用评分契约。

第26节必须写明来源职责、worker生命周期、数据结构、字段合并优先级、板块身份
优先级、降级行为、API加法字段、冻结兼容和本批验收条件。

### 5.4 先写失败测试

新增测试时优先扩展现有文件，不为一个用例创建零散测试文件：

- `tests/component/test_v2_market_data.py`：来源并行、同源single-flight、
  队列容量1、latest-wins、熔断、停止等待和慢源隔离；
- `tests/unit/test_v2_market_data_normalize.py`：单源观测字段和非法时间；
- `tests/unit/test_v2_market_data_merge.py`：字段优先级、乱序、冲突、哈希；
- `tests/unit/test_v2_settings.py`：可选Tushare配置和无Token行为；
- `tests/unit/domain/test_filters.py`：三板身份、上市日龄和涨幅边界；
- `tests/component/test_v2_persistence.py`：新字段往返和旧schema读取；
- `tests/contract/test_v2_web_api.py`：只读API加法兼容；
- `tests/contract/test_v2_app_factory.py`：`create_app()`无副作用；
- `tests/integration/test_v2_pipeline.py`：单源失败不清空推荐；
- `tests/integration/test_v2_final_acceptance.py`：冻结版本与哈希。

必须先证明至少一个新测试在实现前失败，并记录失败原因是目标能力尚不存在。

### 5.5 固定数据结构

在 `market_data/observations.py` 增加不可变类型：

```python
JsonScalar = str | float | bool | None

@dataclass(frozen=True)
class SourceObservation:
    source: str
    subject_key: str
    observed_at: datetime
    source_time: datetime
    received_at: datetime
    effective_at: datetime
    data_version: str
    fields: Mapping[str, JsonScalar]
    missing_reasons: Mapping[str, str]
    payload_hash: str
    status: Literal["success", "no_data", "failed", "late"]
    error_code: str | None
```

在 `domain/models.py` 增加不可变类型：

```python
@dataclass(frozen=True)
class CanonicalMarketSnapshot:
    observed_at: datetime
    merge_epoch: str
    quotes: tuple[MarketQuote, ...]
    field_sources: Mapping[str, Mapping[str, str]]
    source_versions: Mapping[str, str]
    conflicts: tuple[str, ...]
    missing_reasons: Mapping[str, str]
    degraded_reasons: tuple[str, ...]
```

所有 datetime 必须有时区且统一到 `Asia/Shanghai` 业务语义。映射在进入跨线程
队列前复制为不可变 JSON 形态，调用方后续修改不得影响已提交任务。

### 5.6 固定来源职责和并发模型

| 来源 | lane | 职责 | 触发方式 |
| --- | --- | --- | --- |
| 东方财富 | `eastmoney-source` | 全市场、分钟、历史基础行情 | 沿用第6节周期 |
| 新浪 | `sina-source` | 全市场并行校验和回退 | 与全市场同时触发 |
| 腾讯 | `tencent-source` | 候选和TopK定向报价 | 仅候选/TopK周期 |
| Tushare | `tushare-source` | 主数据、日历、日线、估值、财务 | 盘前/盘后/TTL |
| AKShare | `akshare-source` | 行业、新闻、公告和研究 | 候选范围/TTL |

五个逻辑lane共用组合根创建的一个 `BoundedExecutor`，固定5个worker和5个待处理
槽位，线程名前缀为 `source-data`。每个逻辑lane最多一个运行任务和一个合并后的
最新请求。重复请求按主体键合并，只保留最新 `observed_at`。同源仍运行时不得
排队多个周期，也不得为股票逐一建线程。任一来源阻塞最多占用一个worker，不能
耗尽其他四个来源的执行能力。

Tushare依赖放入独立 extra：

```toml
[project.optional-dependencies]
tushare = ["tushare>=1.4,<2"]
```

Token优先从 `TUSHARE_TOKEN` 读取，其次读取 `TUSHARE_TOKEN_FILE` 指向的单行文件；
POSIX系统拒绝group/other可读文件。Token不写入JSON、日志、SQLite、冻结文件或API。
客户端使用官方SDK的显式8秒transport timeout；未安装、无Token、额度错误和超时
均视为可观察降级，不阻止免费链路。

### 5.7 固定合并规则

`market_data/merge.py` 只包含纯函数。对同一股票按以下规则合并：

1. 排除 `source_time`、`received_at` 或 `effective_at` 晚于观察点的结果。
2. 排除无时区、空版本、非法代码和非有限核心数值。
3. 全市场价格同一时间优先东方财富，其次新浪。
4. 候选和TopK价格在满足新鲜度时优先腾讯。
5. 慢数据不得覆盖更晚的实时价格字段。
6. 新旧比较先使用 `source_time`、`received_at`；跨来源同时点按
   `source_priority`，`data_version` 只作同来源同时点的稳定末级比较，禁止比较
   不同供应商无共同语义的版本字符串来改变来源优先级；以上键全部相同时仅用
   `payload_hash` 消除输入顺序差异，不把哈希解释为业务新旧。
7. 多源价格偏差大于 `0.5%` 且未复核时记录冲突并只能观察。
8. 所有来源失败时返回最近有效统一快照并标记降级，不返回空集合。
9. 相同有效输入无论线程完成顺序如何，`merge_epoch` 和JSON哈希必须相同。

### 5.8 固定板块身份与风险门

识别优先级：

1. Tushare带生效时间的证券主数据；
2. AKShare证券清单；
3. 行情源市场字段；
4. 六位代码前缀降级。

保存 `board`、`board_source`、`board_reliability`、`exchange`、`listing_date`、
`listing_age_sessions`、`has_price_limit`、`exchange_limit_pct`、`rule_version`
和 `rule_effective_date`。

边界固定为：

- 上市第1-5个交易日拒绝，第6日恢复；
- 重新上市首日和退市整理首日拒绝；
- 上市日期缺失或身份冲突只能观察；
- 代码前缀降级标记可靠度下降并只能观察；
- 主板8.00%通过、8.01%拒绝；
- 创业板和科创板16.00%通过、16.01%拒绝；
- 创业板和科创板使用不同过滤码；
- 无价格限制状态不得计算普通 `limit_proximity`。

### 5.9 持久化、API和状态

冻结和草稿以加法保存：

- `merge_epoch`；
- `source_versions`；
- 逐字段来源；
- 板块身份与规则版本；
- 冲突、缺失和降级原因；
- 结构化同行/领先/流量原始输入，但不保存v16派生排名。

旧快照读取时使用旧schema和旧算法，不迁移为v15。API路径不变，新字段保持可空。
新冻结的回放算法标识固定为 `engine_v15_parallel_market_data_2026_07`；既有
`engine_v10_section9_hard_filter_2026_07` 冻结继续走 v14 过滤兼容路径，不得用
v15 的板块过滤码、上市日龄门或只能观察规则重解释旧结果。
`/api/status` 增加每源计划、成功、失败、超时、熔断、P50/P95延迟、数据年龄、
合并次数、冲突数和最近 `merge_epoch`。

### 5.10 v15缓存、性能与实时性门

v15必须同时建立统一缓存策略，不能让每个适配器自行决定TTL或容量。新建
`src/trader/application/cache.py`，定义 `CachePolicy`、`CacheIdentity`、
`CacheStats` 和泛型 `BoundedCache` 端口；新建 `src/trader/infrastructure/cache.py`，
提供内部 `CacheEntry` 与唯一有界LRU实现。`bootstrap.py` 负责实例化并注入，应用层
不得导入基础设施实现。缓存使用注入的monotonic时钟判断刷新TTL，业务数据年龄仍由
带 `Asia/Shanghai` 时区的注入时钟与原始 `source_time` 计算。

缓存身份至少包含：

```text
dataset + source + subject_key + request_fingerprint + trade_date + phase
+ source_contract_version + config_version + schema_version
```

`request_fingerprint` 是排序后的代码集合、字段、复权方式、窗口和其他非敏感请求参数的
规范JSON哈希；`source_contract_version` 是请求前已知的适配器/协议版本。响应后才得到
的 `data_version` 保存于条目和值身份，不得为命中而预猜。实时行情使用实际phase；
主数据、日历、历史、估值和财务等盘中阶段无关数据统一使用 `all_day`，避免换阶段产生
无意义miss。所有身份只能由应用层一个builder构造。

固定策略：

| 数据集 | 刷新TTL | 动作/降级边界 | 内存容量 | 持久化 |
| --- | ---: | ---: | ---: | --- |
| 全市场行情 | 当前阶段cadence | cadence的3倍 | 每源6000只最新值 | 否 |
| 候选/TopK报价 | 当前阶段cadence | cadence的3倍 | 每源360只最新值 | 否 |
| 分钟序列 | 45秒 | 90秒 | 360只 | 否 |
| 新闻/公告/风险成功结果 | 600秒 | 1200秒 | 各360只 | 既有证据缓存 |
| 新闻/公告/风险失败结果 | 60秒 | 60秒 | 各360只 | 否 |
| 历史日线 | 21600秒 | 当日收盘更新前 | 360只 | 否 |
| 证券主数据/交易日历 | 86400秒 | 版本有效期 | 当前全量 | 否 |
| 日度估值/财务 | 86400秒 | 下一有效记录前 | 360只 | 否 |

以上刷新TTL、动作/降级边界、条目容量、淘汰方式、负缓存TTL和第7.6节缓存组字节上限
只序列化在 `runtime.json.market_data.cache_policy`。该对象固定包含 `policy_version`、
`datasets`、`groups`、`total_bytes` 和 `estimator_version`；settings loader必须拒绝缺失
或未知键、非正容量、数值型动作年龄小于刷新TTL、分组和不等于总量，以及与本表
不一致的数据集集合。`bootstrap.py` 只注入同一个已解析 `CachePolicy`，适配器和评分
lane不得自带默认TTL、容量或淘汰规则。

规则固定为：

- 首个调用加载数据，其他相同身份调用等待同一个future；
- monotonic经过时间未超过刷新TTL时fresh命中，超过后可返回最近有效值，并通过来源
  lane已有有界执行器安排刷新；缓存不得创建线程、线程池或不可停止的后台任务；
- 实时报价monotonic过TTL但业务年龄未超过两倍cadence记 `refresh_due_hit`；业务年龄
  超过cadence两倍标记stale，超过三倍标记degraded，精确相等时仍留在较轻状态；
- 非cadence数据过刷新TTL记 `refresh_due_hit`，超过表中动作/降级边界直接degraded；
- 超过表中动作/降级边界的数据不得生成新推荐；只读Web仍展示仓储拥有的最近有效
  已发布快照，并保留原始日期、来源时间和显式降级原因；
- stale/degraded只影响展示与动作门，不得伪造新 `source_time`；
- 失败负缓存只抑制重复请求，不能写成“无风险”或真实零值；
- 策略配置中的证据有效期仍决定风险事实能否使用，缓存TTL不得延长证据有效期；
- 交易日、规范phase、请求指纹或source/config/schema版本变化必须生成新身份；
- 14:49:50最终刷新绕过行情fresh缓存，但仍受single-flight和deadline保护；
- 刷新完成时以身份和deadline做一次CAS；身份已失效或完成时间越过deadline时只记录
  脱敏审计并删除in-flight状态，不得写入缓存或覆盖新快照；
- 冻结JSON、committed manifest和15:00收盘overlay禁止缓存重算或原地修改；
- 淘汰固定使用LRU，容量达到上限时先淘汰已过期项，再淘汰最久未访问项；
- 缓存不得持有requests响应、DataFrame、SDK客户端或数据库连接。

single-flight使用独立的临时in-flight注册表，不把future写入缓存条目；注册表按数据集
容量有界，任务完成、失败、超时或取消时都必须在callback中删除，停止后为空。

估算字节数固定为缓存身份规范JSON的UTF-8长度加不可变载荷规范JSON的UTF-8长度。
规范JSON沿用项目冻结序列化规则：键排序、紧凑分隔符、稳定时间/Decimal表示；负缓存
载荷只含脱敏错误类别。字节数在插入或替换时计算一次，命中时不得重复序列化；条目
必须同时满足数据集条目上限和所属缓存组字节上限，任一达到即按上述LRU规则淘汰。

`/api/status` 需要按数据集和来源报告 `entries`、`capacity`、`hit`、`miss`、
`refresh_due_hit`、`stale_hit`、`negative_hit`、`refresh`、`eviction`、`load_error`、
命中率和估算字节数。计数更新必须有界且不能成为高频路径的全局锁热点。

v15性能目标使用5500只全市场录制行情和360只候选：

- 单源标准化P95不超过800毫秒；
- 多源字段合并P95不超过1000毫秒；
- 来源完成到 `CanonicalMarketSnapshot` 可读P95不超过1500毫秒；
- 合并不得产生按来源乘以股票数的嵌套全表扫描；目标复杂度为O(S*N)；
- TopK价格年龄继续满足关键阶段P95不超过10秒、其他阶段不超过20秒；
- 候选报价年龄主执行不超过15秒、其他阶段不超过30秒；
- 全市场快照主阶段不超过60秒、其他阶段不超过120秒；
- 慢源或缓存清理不得占用发布线程、SQLite单写线程或SSE线程。

普通测试增加缓存身份/请求指纹、`all_day` phase复用、TTL边界、负缓存、LRU顺序、
交易日失效、最终刷新绕过、
single-flight命中、配置缺失/漂移拒绝、规范JSON字节估算、条目/字节双容量、
monotonic与业务时钟分离和确定性时钟用例。性能测试增加5500行标准化、两源合并、
缓存冷/热路径和一个永久阻塞慢源隔离场景。

### 5.11 批次一验收矩阵

先运行本批局部回归：

```bash
.venv/bin/python -m pytest -q \
  tests/unit/test_v2_market_data_normalize.py \
  tests/unit/test_v2_market_data_merge.py \
  tests/unit/test_v2_market_data_cache.py \
  tests/unit/domain/test_filters.py \
  tests/unit/test_v2_settings.py \
  tests/component/test_v2_market_data.py \
  tests/component/test_v2_persistence.py \
  tests/contract/test_v2_web_api.py \
  tests/contract/test_v2_app_factory.py \
  tests/integration/test_v2_pipeline.py \
  tests/integration/test_v2_final_acceptance.py
```

生成显式性能报告：

```bash
.venv/bin/python tests/performance/run_v15_market_data.py \
  --config "$PWD/config/v2/runtime.json" \
  --fixture "$PWD/tests/fixtures/performance/v15" \
  --output /tmp/trader-v15-performance.json
```

局部回归和性能报告通过后才能运行第3.3节完整门禁。

- [ ] 东方财富和新浪能同时进入各自worker。
- [ ] 腾讯慢或失败不阻塞全市场统一快照。
- [ ] Tushare未安装、无Token、429、超时均不阻塞免费链路。
- [ ] 同源并发调用只产生一次物理请求。
- [ ] fresh、stale、degraded和负缓存不会改变原始来源时间。
- [ ] LRU容量和淘汰顺序在注入时钟下确定。
- [ ] 14:49:50会绕过fresh行情缓存且不会重复物理请求。
- [ ] 缓存条目、估算字节数和命中/淘汰指标有界可观测。
- [ ] 5500行情和360候选的显式性能报告达到v15预算。
- [ ] 队列满时保留最新观察点，不补跑旧周期。
- [ ] 不同线程完成顺序产生相同快照和哈希。
- [ ] 迟到、未来时间、空版本和乱序结果不能覆盖新值。
- [ ] 多源偏差0.50%通过，超过0.50%且未复核只能观察。
- [ ] 上市第1/5日拒绝，第6日恢复。
- [ ] 主板8.00/8.01和两成长板16.00/16.01边界通过。
- [ ] `create_app()`仍无I/O副作用。
- [ ] 旧冻结可读，新冻结可离线校验哈希。
- [ ] v14评分、门槛和TopK输出未改变。

### 5.12 批次一提交和停止

Review重点：来源线程生命周期、SDK无法取消的尾任务、乱序覆盖、缓存污染、密钥
泄漏、旧快照兼容、错误降级和安装包可选依赖。

提交信息固定为：

```text
feat(market-data): add parallel source merge pipeline
```

推送后分别读取 `HEAD` 与 `@{upstream}`。两者相同后把批次一标为 `[x]` 并停止，
不得自动开始批次二。

## 6. 批次二：v16 三板独立评分与统一合并

### 6.1 进入条件

- 批次一已经提交、推送且哈希一致；
- v15完整门禁通过；
- 活动代码能提供三板身份、板内原始输入和统一 `merge_epoch`；
- 工作树中没有未闭合的批次一修改。

### 6.2 允许修改的文件

契约和配置：

- `docs/need.md` 第8-17、18、22-26节；
- `config/v2/runtime.json`；
- `config/v2/strategy.json`；
- `src/trader/infrastructure/settings*.py`。

领域和应用：

- `src/trader/domain/models.py`；
- `src/trader/domain/factors.py`；
- `src/trader/domain/ranking.py`；
- `src/trader/domain/strategies/*.py`；
- `src/trader/application/policy.py`；
- `src/trader/application/recommendations.py`；
- 新建 `src/trader/application/board_scoring.py`；
- `src/trader/application/pipeline*.py` 和 `bootstrap.py`。

DeepSeek、持久化和Web：

- `src/trader/infrastructure/deepseek/cache.py`、`reviewer*.py` 和 `schema.py`；
- `src/trader/infrastructure/persistence/*.py`；
- `src/trader/web/schemas.py`、`serializers.py` 和详情展示；
- 对应单元、组件、契约和集成测试；
- 新建 `tests/performance/run_v16_board_scoring.py`、
  `tests/fixtures/performance/v16/manifest.json` 和脱敏录制fixture。

### 6.3 先写契约与失败测试

将 `v16_board_scoring_contract` 改为活动契约，并明确v14/v15仅用于旧快照回放。

先增加：

- `tests/unit/domain/test_board_scoring.py`：三板权重、公式和缺失；
- `tests/unit/domain/test_ranking.py`：板内候选、全局选择和稳定排序；
- `tests/unit/application/test_board_scoring.py`：三个lane及epoch；
- `tests/unit/application/test_recommendations.py`：策略注入和动作门槛；
- `tests/component/test_v2_deepseek.py`：全局预算与板块身份；
- `tests/component/test_v2_persistence.py`：`BoardScoreBatch`往返；
- `tests/contract/test_v2_web_api.py`：加法字段；
- `tests/integration/test_v2_pipeline.py`：并行、失败和最近完整快照；
- `tests/integration/test_v2_final_acceptance.py`：冻结与旧版本回放。

### 6.4 固定类型和接口

在 `domain/models.py` 增加：

```python
@dataclass(frozen=True)
class BoardStrategyPolicy:
    policy_id: str
    version: str
    board: Board
    strategy: Strategy
    candidate_weights: Mapping[str, float]
    local_weights: Mapping[str, float]

@dataclass(frozen=True)
class BoardScoreBatch:
    board: Board
    strategy: Strategy
    merge_epoch: str
    policy_id: str
    status: Literal["success", "empty", "degraded", "failed"]
    recommendations: tuple[Recommendation, ...]
    degraded_reasons: tuple[str, ...]
```

`score_strategy` 的目标签名：

```python
def score_strategy(
    strategy: Strategy,
    snapshot: FeatureSnapshot,
    component_weights: Mapping[str, float] | None = None,
    *,
    board_policy: BoardStrategyPolicy | None = None,
) -> LocalScoreResult: ...
```

v16生产调用必须传 `board_policy`。只有旧快照回放可以显式传旧组件权重；禁止在
v16缺少板块策略时静默使用通用默认值。

### 6.5 固定候选权重

| 板块 | tomorrow | d25 |
| --- | --- | --- |
| 主板 | 流动35、同行15、趋势25、稳定15、完整10 | 流动30、残差20、趋势20、稳定15、执行10、完整5 |
| 创业板 | 流动20、同行30、趋势25、稳定15、完整10 | 流动20、残差30、趋势20、稳定10、执行15、完整5 |
| 科创板 | 流动25、同行15、趋势30、稳定20、完整10 | 流动25、残差15、趋势30、稳定15、执行10、完整5 |

每行必须除以100写入配置并精确合计1.0。候选按 `strategy + board` 独立预选，
每板最多120只。DeepSeek不得新增池外股票。

### 6.6 固定本地评分权重

| 板块 | tomorrow | d25 |
| --- | --- | --- |
| 主板 | 尾盘15、同行领先15、换手量价10、趋势25、稳定25、市场10 | 残差15、趋势25、质量价值25、稳定15、量价流动10、不过热10 |
| 创业板 | 尾盘20、同行领先25、换手量价20、趋势20、稳定10、市场5 | 残差30、趋势20、质量价值10、稳定10、量价流动20、不过热10 |
| 科创板 | 尾盘15、同行领先15、换手量价10、趋势30、稳定25、市场5 | 残差15、趋势30、质量价值25、稳定15、量价流动10、不过热5 |

板内因子总体固定为：

```text
trade_date + phase + board + data_version
```

同行至少10只；领先组至少3只；目标股票从同行和领先组计算中排除。板内样本不足
100只时回退最近有效交易日；超过5个交易日后该板只能观察，不借用其他板总体。

趋势方向固定为：

- 正 `peer_gap` 得高分；
- `leader_gap` 越小得分越高；
- 换手冲击使用 `B(x;0.8,1.1,2.0,4.0)`；
- 成交额冲击使用 `B(x;0.8,1.2,3.0,6.0)`；
- 缺失原始值保持 `null`，评分使用中性50并记录原因；
- `board_data_reliability < 0.85` 时动作只能观察。

tomorrow子组件固定为：

- 尾盘沿用第12节既有30分钟收益、尾盘量比和收盘位置公式；
- 同行领先为同行5日40%、同行20日40%、领先差反向20%；
- 换手量价为换手冲击35%、成交额冲击35%、量价确认30%；
- 趋势沿用MA20/60、斜率、20日突破和行业趋势组件；
- 稳定为低波动50%、低最大回撤50%；
- 市场状态为risk-on 60、neutral 50、risk-off 40，缺失按50并留痕。

d25子组件固定为：

- 残差动量为同行20日60%、同行60日40%；
- 趋势沿用MA20/60、斜率、20日突破和行业趋势组件；
- 质量价值为质量50%、价值30%、成长20%；
- 稳定为低波动50%、低最大回撤50%；
- 量价流动为20日成交额分位50%、换手冲击25%、成交额冲击25%；
- 不过热沿用登记后的不过热分，不再乘到总分。

d25删除总分的过热和市场状态双乘数。所有板块的质量价值子组件都使用上述
50/30/20比例；不得在运行时选择另一组权重。

### 6.7 三个板块评分lane

在 `application/board_scoring.py` 实现：

- `main-score`：独立 `BoundedExecutor`，一个worker、一个待处理槽位；
- `chinext-score`：独立 `BoundedExecutor`，一个worker、一个待处理槽位；
- `star-score`：独立 `BoundedExecutor`，一个worker、一个待处理槽位；
- `BoardScoringCoordinator`：拥有上述三个执行器，广播同一不可变
  `merge_epoch`，收集三个结果；
- coordinator必须先提交三个板块再等待结果，不得按主板、创业板、科创板串行提交；
  调度重叠不等同于CPU并行，业务正确性不得依赖具体线程交错；
- 相同板块任务在途时按策略和epoch保留最新任务，不排队旧任务；
- lane失败返回 `failed`，没有候选返回 `empty`，两者不得混淆；
- `stop()` 必须停止接收、取消未开始任务并等待运行任务结束。

### 6.8 DeepSeek全局协调

三个板块先完成本地分，再由现有全局Reviewer按以下优先级生成统一复核序列：

1. 新高风险；
2. 动作门槛边界；
3. 全局TopK边界；
4. 本地与主审方向冲突；
5. 证据冲突；
6. 尚未复核；
7. 稳定候选。

不得为板块新增预算桶，不得把188乘以3。缓存键增加 `board`、`board_policy_id`
和板内特征版本。跨板、跨策略、跨epoch或迟到结果只能审计，不能参与融合。

### 6.9 固定融合与全局选择

每只候选先在所属板块完成固定融合：

```text
raw = local_score * 0.68 + deepseek_score * 0.32
      - deepseek_risk_penalty
final_score = ROUND_HALF_UP(clamp(raw, 0, 100), 2)
```

本地风险已在 `local_score` 中扣除，融合时禁止再次扣除。

全局合并只接受交易日、阶段、策略、配置、因子schema、板块策略和 `merge_epoch`
全部一致的三个 `BoardScoreBatch`：

- `success` 和 `empty` 都是完整结果；
- 任一板块 `failed` 或epoch不一致时，不发布偏置的新TopK；
- 继续展示最近完整快照并显式标记降级；
- 冻结只使用截止前满足年龄要求的最近完整三板结果；
- 不得使用迟到或不同epoch的板块结果拼接冻结。

统一动作和选择规则：

- tomorrow：78.00分及以上可执行，73.00-77.99观察，低于73.00不可执行；
- d25：76.00分及以上可执行，71.00-75.99观察，低于71.00不可执行；
- stale、可靠度不足或veto优先于分数门槛执行现有降级规则；
- 默认TopK 10，接口范围0-18；
- 单板最多10只；
- 主板同一竞争组最多3只；
- 创业板、科创板同一竞争组最多2只；
- 排序为最终分降序、本地分降序、股票代码升序；
- 保存板内原排名、全局排名和每个跳过原因。

### 6.10 配置、冻结和API

`strategy.json` 增加并严格校验：

```text
board_policy_version
board_candidate_weights
board_local_strategy_weights
board_factor_overrides
```

三板与tomorrow/d25的六个组合必须完整，每组权重必须精确合计1.0，所有因子必须
存在于 `factor_registry`，全部字段进入策略版本哈希。

冻结、SQLite、JSON和API加法保存：

- `board`、`board_policy_id`、`board_policy_version`；
- 板内总体版本、分位、样本数和回退日期；
- `board_data_reliability`；
- `BoardScoreBatch.status` 和降级原因；
- `merge_epoch`、板内排名、全局排名；
- 竞争组来源、限制值和跳过原因。

旧v14/v15冻结按旧策略读取和复算，不迁移成v16。API路径和旧字段语义不变。

### 6.11 v16评分缓存、性能与实时性门

v16只能缓存不可变中间结果。新增
`src/trader/application/board_scoring_cache.py`，只依赖v15的 `BoundedCache` 应用端口
并构造评分身份；`bootstrap.py` 注入 `infrastructure.cache` 的有界LRU实例。应用层
不得导入基础设施，也不得创建另一种缓存实现。既有DeepSeek `ReviewCache` 保留公共
语义，但内部存储改为委托同一有界LRU；不得改变请求身份、预算计数或有效命中行为。

固定缓存：

| 数据 | 缓存键 | 容量 | 失效条件 |
| --- | --- | ---: | --- |
| 历史摘要 | code + history_version | 360 | 历史版本变化 |
| 板内横截面 | trade_date + phase + board + data_version + schema | 24代 | 任一身份变化 |
| 候选预选 | strategy + board + merge_epoch + policy_id | 每组合4代 | epoch或策略变化 |
| 本地评分 | code + strategy + board + merge_epoch + policy_id | 720项 | 任一身份变化 |
| 竞争组映射 | industry_version + manual_group_version | 2代 | 分类或配置变化 |
| 原始DeepSeek结果 | 既有raw key + board feature version | 2000项、600秒 | 证据/模型变化 |
| 策略复核结果 | raw key + strategy + policy + challenger状态 | 2000项、600秒 | 权重/挑战者变化 |

本表全部条目在v16加入同一个 `runtime.json.market_data.cache_policy.datasets`；不得把
评分或DeepSeek容量另存到策略配置或构造器默认参数。

缓存约束：

- 板内截尾、分位和同行统计只计算一次并供同epoch候选读取；
- 主板、创业板和科创板的归一化结果不得互相命中；
- tomorrow和d25可以复用原始历史摘要，但不能复用策略评分或排名；
- 风险事实变化、phase变化、价格变化达到1%、量比变化达到0.3、最终补审、
  policy/schema/model变化均使对应复核身份失效；
- 缓存命中与冷算必须生成完全相同的业务投影和冻结哈希；
- 缓存只保存不可变值，不保存future、线程、锁、客户端或可变FeatureSnapshot；
- DeepSeek已见代码集合按 `trade_date` 隔离且最多6000项，新交易日清空；
- 评分lane停止时取消未开始future并清空仅属于未发布epoch的临时条目；
- 已发布和冻结对象由现有仓储拥有，不复制到评分LRU。

v16性能目标使用三个板各120只候选、两个策略：

- 单板候选预选P95不超过250毫秒；
- 单板两个策略本地评分P95不超过500毫秒；
- 三板并行本地计算墙钟P95不超过800毫秒；
- 360只候选全局稳定选择P95不超过100毫秒；
- 三板结果齐备到本地草稿发布P95不超过500毫秒；
- 候选报价变化到本地草稿发布P95不超过5秒；
- 有效DeepSeek结果到hybrid重发布P95不超过1秒；
- SSE发布延迟P95不超过2秒；
- 14:49:50最终报价、三板评分和完整草稿必须在14:50前结束，否则不补造冻结。

普通测试验证每个epoch的横截面只计算一次、跨板/跨策略不得错误命中、冷热业务
投影相同、风险与版本失效、LRU容量和停止清理。性能测试固定比较顺序参考路径与
三lane墙钟，报告队列等待、进程CPU时间和 `sequential_wall / lane_wall` 加速比；
无实测加速时只能表述为隔离和有界并发，但仍必须达到800毫秒绝对预算。

### 6.12 批次二验收矩阵

先运行本批局部回归：

```bash
.venv/bin/python -m pytest -q \
  tests/unit/domain/test_board_scoring.py \
  tests/unit/domain/test_ranking.py \
  tests/unit/application/test_board_scoring.py \
  tests/unit/application/test_board_scoring_cache.py \
  tests/unit/application/test_recommendations.py \
  tests/unit/test_v2_settings.py \
  tests/component/test_v2_deepseek.py \
  tests/component/test_v2_persistence.py \
  tests/contract/test_v2_web_api.py \
  tests/integration/test_v2_pipeline.py \
  tests/integration/test_v2_final_acceptance.py
```

生成显式性能报告：

```bash
.venv/bin/python tests/performance/run_v16_board_scoring.py \
  --config "$PWD/config/v2/runtime.json" \
  --fixture "$PWD/tests/fixtures/performance/v16" \
  --output /tmp/trader-v16-performance.json
```

局部回归和性能报告通过后才能运行第3.3节完整门禁。

- [ ] 三个评分lane先全部提交再等待，每个lane只有一个worker且背压相互隔离。
- [ ] 相同输入不同板块得到各自策略确定的不同分数。
- [ ] 每组候选和本地权重精确合计1.0。
- [ ] 三板横截面完全隔离，不借用其他板分位。
- [ ] 同行9只缺失、10只生效；领先组2只缺失、3只生效。
- [ ] 板内样本99只回退、100只直接计算。
- [ ] 回退第5日有效、第6日只能观察。
- [ ] 换手/成交额分母零、负数、NaN、Infinity保持缺失。
- [ ] 可靠度0.8499只能观察，0.85通过可靠度门。
- [ ] d25路径不存在双乘数。
- [ ] 固定融合向量仍得到83.40。
- [ ] tomorrow 77.99/78.00和d25 75.99/76.00边界正确。
- [ ] 单板第10/11只、竞争组第2/3或3/4只边界正确。
- [ ] 一个板块失败时不发布偏置的新TopK。
- [ ] 三板不同完成顺序产生相同全局排序和冻结哈希。
- [ ] 冷缓存和热缓存产生相同业务投影及冻结哈希。
- [ ] 同一epoch的板内横截面只计算一次。
- [ ] 跨板、跨策略、跨policy和跨schema缓存均不命中。
- [ ] 360候选双策略性能报告达到v16预算。
- [ ] 报价到本地草稿、复核到hybrid和SSE延迟达到目标。
- [ ] DeepSeek总预算仍为158/188，且不存在板块重复预算。
- [ ] today、long、冻结时间、SSE和旧快照行为不变。

### 6.13 批次二提交和停止

Review重点：板内总体泄漏、权重漂移、缺失值伪装、DeepSeek预算竞争、跨epoch
混合、失败板块偏置、冻结一致性、旧回放和UI字段兼容。

提交信息固定为：

```text
feat(strategy): add board-specific parallel scoring
```

推送后核对 `HEAD == @{upstream}`，把批次二标为 `[x]` 并停止。不得继续创建
回测、收益校准或下一版策略任务。

## 7. 批次三：v17 性能、缓存与实时性硬化

### 7.1 进入条件与目标

- v15和v16已分别提交、推送并通过全部门禁；
- 固定完整日fixture能稳定复算三板结果和冻结哈希；
- 工作树中没有未闭合的前两批修改；
- 用户再次发送“继续”。

本批只做行为等价优化和性能验收。`strategy_version`、评分、风险、融合、门槛、
排名、冻结身份和API业务字段不得变化；只提升runtime/config版本并增加性能审计。

### 7.2 允许修改的文件

- `docs/need.md` 第4、6、7、18、22、23、25、26节；
- `config/v2/runtime.json` 和 `src/trader/infrastructure/settings*.py`；
- v15的 `application/cache.py`、`infrastructure/cache.py`、`market_data/merge.py`、
  `market_data/gateway.py` 和 `market_data/service*.py`；
- v16的 `application/board_scoring.py` 和 `board_scoring_cache.py`；
- `application/status.py`、`publisher.py`、`web/sse.py` 和只读查询路径；
- 新建 `src/trader/application/performance.py`；
- 新建 `src/trader/infrastructure/performance.py`；
- `src/trader/entrypoints/cli.py`；
- 新建 `tests/performance/run_v17_end_to_end.py`、
  `tests/fixtures/performance/v17/manifest.json` 和脱敏录制fixture；
- 与性能审计、缓存或并发行为直接相关的现有测试。

不得修改因子公式、权重、风险规则、DeepSeek schema、动作门槛或Web布局。

### 7.3 先固化基线

先按第3.2节完成契约、失败测试、`performance_budgets` schema和第7.4节测量脚手架，
但不得修改被测生产路径。测量脚手架必须先通过固定业务投影、API JSON和冻结哈希
等价测试；它自身的fixture加载和报告开销不得计入任何优化收益。

在任何优化前，同一次runner进程、同一fixture连续运行冷缓存1轮、预热1轮、测量5轮：

```bash
PYTHONHASHSEED=0 .venv/bin/python -m trader.entrypoints.cli \
  --config "$PWD/config/v2/runtime.json" perf-check \
  --fixture "$PWD/tests/fixtures/performance/v17" \
  --suite all \
  --output /tmp/trader-performance-before.json
```

基线fixture固定包含：

- 5500只全市场行情，东方财富和新浪各一版；
- 360只候选，三板各120只；
- tomorrow和d25两个策略；
- 120只分钟序列、360只历史摘要、360只研究状态；
- 20个并发当前快照请求；
- 10个SSE客户端，其中2个慢客户端；
- 09:15至15:00虚拟交易日和14:49:50最终刷新；
- 一个超时来源、一个熔断来源和一个乱序迟到结果。

基线报告保存配置、策略和fixture哈希、当前提交、工作树是否脏，以及按相对路径排序后
对 `src/trader` 文件名与内容计算的 `source_tree_sha256`。还要保存Python实现/版本、
操作系统、内核、机器架构、CPU型号和逻辑核数形成的环境指纹；缺少任一身份时报告
无效。

### 7.4 `perf-check` 接口

在现有CLI增加：

```text
trader-cli --config <absolute-runtime-json> perf-check
  --fixture <absolute-fixture-directory>
  --suite market-data|board-scoring|api-sse|end-to-end|all
  --output <absolute-json-path>
  [--baseline <absolute-json-path>]
```

`--config` 和 `--fixture` 必填且必须为绝对路径。fixture目录必须含manifest与逐文件
SHA-256，入口层完成只读加载并把不可变输入传给应用用例，活动包不得导入 `tests`。
预算只从
`runtime.json.performance_budgets` 读取，代码不保留第二套默认值。输出包含每阶段
样本数、P50/P95/max、队列等待、进程CPU时间、实际加速比、物理调用、缓存统计、
队列峰值、缓存估算字节数、tracemalloc峰值、发布版本数、冻结哈希和通过状态。输出
不得含密钥或完整外部载荷。

`application/performance.py` 只定义不可变样本/结果和纯预算判定；真实 `perf_counter`、
`tracemalloc`、fixture读取和受控工作负载放在 `infrastructure/performance.py`，JSON写入
和退出码由CLI负责。Web和 `create_app()` 不得装配或触发性能runner。

延迟轮次关闭 `tracemalloc`，内存轮次单独开启，避免跟踪开销污染P95。fixture校验与
解析、预热、GC和报告写入不进入延迟计时窗口；runner禁止外部网络，DeepSeek使用固定
响应，持久化使用临时SQLite，API/SSE使用进程内客户端。每个路径只包含表中命名阶段，
开始和结束事件必须在报告中明确。

### 7.5 固定性能预算

| 路径 | P95上限 |
| --- | ---: |
| 5500行单源标准化 | 800毫秒 |
| 两源行情确定性合并 | 1000毫秒 |
| 来源完成到统一快照可读 | 1500毫秒 |
| 单板120只候选预选 | 250毫秒 |
| 单板两个策略本地评分 | 500毫秒 |
| 三板并行本地计算墙钟 | 800毫秒 |
| 360只全局稳定选择 | 100毫秒 |
| 三板齐备到本地草稿发布 | 500毫秒 |
| 候选报价变化到本地草稿 | 5秒 |
| DeepSeek结果到hybrid重发布 | 1秒 |
| SSE已发布版本到客户端队列 | 2秒 |
| 当前快照API 200响应 | 200毫秒 |
| ETag命中304响应 | 50毫秒 |
| `/api/status` | 100毫秒 |

继续执行 `docs/need.md` 第7节的外部时效目标：TopK关键阶段10秒、其他阶段20秒；
候选主执行15秒、其他阶段30秒；全市场主阶段60秒、其他阶段120秒。内部性能
达标不能替代这些端到端数据年龄门。

### 7.6 固定缓存与内存预算

在固定重复工作负载第二轮：

- 历史摘要、板内横截面和本地评分相同身份命中率必须为100%；
- 同源相同请求的物理调用次数必须为1；
- 跨板、跨策略、跨epoch、跨schema和跨policy命中率必须为0%；
- 负缓存不得阻止TTL后重新探测；
- 最终刷新不得命中fresh行情结果。

缓存估算容量固定为：

| 缓存组 | 上限 |
| --- | ---: |
| 实时行情与统一观测 | 64 MiB |
| 历史、分钟和研究 | 128 MiB |
| 板内统计、候选和评分 | 64 MiB |
| 全部项目自有内存缓存 | 256 MiB |

`MiB` 固定为 `1024 * 1024` 字节；上述四个值写入
`runtime.json.market_data.cache_policy`，不得在缓存类或性能脚本中复制。

使用 `tracemalloc` 报告项目分配峰值，不把解释器、第三方库共享页或操作系统页缓存
伪装成项目缓存。第10和第100个模拟tick完成后分别强制GC并记录项目跟踪字节数，
增长率固定为 `(tick100_bytes - tick10_bytes) / max(tick10_bytes, 1)`，不得超过
0.20；`runtime.json.performance_budgets.memory.growth_percent` 固定写入20，条目数始终
不得越界。

### 7.7 优化顺序

只有基线证据确认热点后，按以下顺序修改：

1. 在计时轮次之外用标准库profiler和进程CPU/墙钟比区分I/O等待、重复计算、锁竞争
   和纯Python CPU热点，不把线程开始时间重叠当作加速。
2. 消除同一epoch重复的历史摘要、截尾、分位和序列化。
3. 使用按代码索引的一次遍历代替候选与全市场的嵌套扫描。
4. 在跨线程边界只复制一次不可变业务投影，禁止深拷贝客户端对象。
5. 将缓存统计按lane累计后批量合并，避免每只股票竞争全局锁。
6. 缩短锁持有区间，网络、评分、JSON编码和SQLite调用不得在共享锁内执行。
7. 让状态/API读取预聚合快照，不在HTTP请求中遍历全量运行记录。
8. 保持SSE客户端独立有界缓冲，慢客户端丢弃并要求resync，不阻塞publisher。
9. 仅在 `EXPLAIN QUERY PLAN` 证明查询扫描时增加SQLite索引。
10. 清除生命周期结束的future、回调和临时epoch，验证无worker或缓存引用泄漏。

每项优化后运行固定业务投影比较；股票、分数、风险、动作、排名、版本和冻结哈希
任何一项变化都必须撤销该优化，不允许更新fixture接受漂移。

### 7.8 实时性和背压

- cadence仍完全由 `runtime.json` 决定，不实现根据机器速度自动调频；
- 周期任务在途时跳过旧周期并保留最新观察点，不排队补跑；
- 冻结、风险变化和最终报价继续拥有高优先级保留容量；
- 数据队列满时合并普通行情，不能丢弃已持久化的冻结或风险事件；
- 14:49:50任务必须绕过fresh缓存，并为14:50冻结保留完成窗口；
- deadline后完成的数据只记审计，不更新缓存、候选、评分或冻结；
- stale-while-revalidate只能返回最近有效显示数据，不能绕过动作新鲜度门；
- SSE慢客户端不得增加发布P95，断线轮询与SSE不能同时持续运行。

### 7.9 测试和验收

普通回归至少覆盖：

- [ ] 缓存键、TTL、LRU、负缓存和版本失效；
- [ ] single-flight、latest-wins、队列峰值和停止清理；
- [ ] 冷热业务投影、API JSON和冻结哈希完全一致；
- [ ] 状态/API读取不触发网络、评分、全表缓存扫描或文件写入；
- [ ] 慢SSE客户端不阻塞快客户端和publisher；
- [ ] 最终刷新、deadline和冻结竞态保持原行为；
- [ ] 100 tick后worker、future、连接和缓存条目均有界；
- [ ] 性能预算和缓存策略缺失、未知或漂移时配置加载失败；
- [ ] 所有性能指标在无敏感载荷的状态API和报告中可追踪。

优化后运行：

```bash
PYTHONHASHSEED=0 .venv/bin/python -m trader.entrypoints.cli \
  --config "$PWD/config/v2/runtime.json" perf-check \
  --fixture "$PWD/tests/fixtures/performance/v17" \
  --suite all \
  --baseline /tmp/trader-performance-before.json \
  --output /tmp/trader-performance-after.json
```

然后运行第3.3节全部门禁、仓库外wheel和桌面验收。性能报告必须全部达到绝对
预算；相对基线不得出现任何关键路径P95退化超过5%。比较前必须确认配置、策略、
fixture和环境指纹相同、`PYTHONHASHSEED=0`，并确认优化后 `source_tree_sha256` 与基线
的关系符合报告：应用过生产优化时必须不同；基线已达标且热点分析决定零生产优化时
必须相同并记录 `optimization_count=0`。当前提交可以相同，因为优化在提交前验收。
不满足比较身份时退出2，不能输出相对通过结论。

### 7.10 Review、提交和停止

Review重点：缓存污染、错误命中、无界容量、锁竞争、截止后写入、状态读取副作用、
SQLite查询计划、SSE背压、内存引用泄漏和优化导致的业务漂移。

提交信息固定为：

```text
perf: harden real-time recommendation pipeline
```

推送后核对 `HEAD == @{upstream}`，把任务3标为 `[x]` 并停止。不得自动开始收益
验证、调参或新策略任务。

## 8. 最终 Definition of Done

只有同时满足以下条件，整个计划才算完成：

- [ ] `docs/need.md` 已成为 v15/v16和v17性能边界的唯一业务契约。
- [ ] 五个数据源lane可启动、停止、等待、熔断和观测。
- [ ] 单源观测和统一快照可确定性重放。
- [ ] 三个板块lane独立评分且无跨板统计泄漏。
- [ ] DeepSeek保持受控双模型和188次全局上限。
- [ ] 单一合并器负责融合、选择、发布和冻结。
- [ ] 所有缓存有稳定身份、TTL、容量、淘汰和命中观测。
- [ ] 冷热路径业务投影及冻结哈希完全一致。
- [ ] v15、v16和端到端性能报告全部达到绝对预算。
- [ ] 关键阶段数据年龄、草稿发布和SSE延迟达到P95目标。
- [ ] 100 tick后worker、future、连接、缓存和项目内存均保持有界。
- [ ] 新旧冻结均可按各自版本验证。
- [ ] 所有适用自动门禁和仓库外安装验收通过。
- [ ] 三档桌面验收有实际截图和无重叠证据。
- [ ] 三个批次分别只有一个提交并均已推送。
- [ ] 没有已知未解决Review发现。
- [ ] CHANGELOG记录用户诉求、修改、验证和剩余风险。

即使以上全部完成，也只能表述为数据可靠性、结构化程度和策略针对性已增强；
没有另行定义并通过点时收益验证前，不得声称实际收益已经提高。
