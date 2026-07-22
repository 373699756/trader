# Codex 执行计划：三板独立评分、六阶段递减内存池与Web性能硬化

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
| 1 | v15多源并行采集与结构化合并 | `[x]` | 已验证，随本批提交并推送 |
| 2 | v16三板独立评分与统一合并 | `[x]` | 已验证，随本批提交并推送 |
| 3 | v17六阶段递减内存池与Web性能硬化 | `[ ]` | 任务2完成并再次收到“继续” |

Codex必须从上表第一个 `[ ]` 项开始，不得跳项、合并两项或在完成后自动继续。

## 2. 不可变决策

- `docs/need.md` 决定 v16 策略范围，固定覆盖 `today`、`tomorrow` 和 `d25`；其他评分、
  门槛、选择、缓存、持久化和实现细节以本文固定口径为准。后续用户已确认的质量门、
  每板最多120只和单板 `ceil(top_n * 60%)` 上限优先于两文档旧描述。
- 当前活动生产基线在批次二原子切换完成前仍按 `strategy_v14` 处理；批次二完成后直接
  启用三策略 v16，不设置影子运行、自动晋级或运行时双策略猜测。
- 批次一生成 v15 数据与结构化合并契约，但不启用新评分。
- 批次二生成并启用 v16 三板评分与统一选择契约，固定版本为
  `strategy_v16_board_scoring_ttd25_2026_07`，回放引擎为
  `engine_v16_board_scoring_ttd25_2026_07`。
- `long`、11:20/14:50 冻结和14:48 DeepSeek截止保持原业务语义；today评分改造不得
  改变其09:36主执行、10:30降级和11:20冻结窗口。
- DeepSeek 正常目标158次、物理请求硬上限188次保持不变。
- 固定融合仍为本地68%、DeepSeek 32%，DeepSeek风险扣分为绝对分值。
- Tushare是可选慢数据增强源，不承担全市场或TopK高频实时报价。
- 沪深主板、创业板、科创板分别使用独立单worker评分lane。
- 三板lane用于隔离任务、背压和失败域，不预设CPython线程能让纯Python计算CPU并行；
  效率结论只看固定负载墙钟、进程CPU时间和实际加速比。
- 三板评分结果必须经过单一确定性合并器后才能发布或冻结。
- 同行差与换手冲击采用趋势延续方向，不实现补涨回归分支。
- d25质量价值输入固定为质量50%、价值30%、成长20%。
- v16动作门槛固定为 `today_main=70`、`today_late=76`、`tomorrow=78`、`d25=76`，
  观察带为5分。
- 实时路径采用有界内存缓存和 stale-while-revalidate，不增加第二个数据库。
- SQLite与JSON继续作为重启恢复和归档的持久化真相源；内存只负责运行期分层计算、
  发布和高速读取，不得成为唯一持久化真相源。
- “发布”与“持久化”必须分离：盘中local/hybrid版本只原子更新P6并发送SSE，不写完整
  推荐SQLite/JSON；完整领域快照只在冻结前30秒检查点和11:20/14:50正式冻结时写入，
  15:00只固化一次收盘overlay，long不写推荐历史。
- 项目自有内存池总量固定为256 MiB：P1-P6缓存条目固定占248 MiB，另保留8 MiB给
  有界队列、single-flight、在途future、缓存索引和统计；`bug.txt`中的约200M内存池按
  该既有上限执行，不另建无界对象池、第二套缓存实现或第二个数据库。
- 缓存按来源与基础数据、标准化统一行情、过滤候选、本地评分、DeepSeek复核、发布与
  Web六阶段划分。后续阶段只保存不可变精简批次和上游版本引用，不得逐层复制完整的
  5500只全市场对象、SDK载荷或冻结回放输入。
- Web在当前策略首个版本ready后，只读四个当前策略和最近20个交易日三策略时必须命中
  服务端发布投影热索引，不得
  触发行情SDK、评分、DeepSeek、SQLite查询或快照文件读取；更老归档日期首次请求可
  只读回退持久化，并一次性预取同日期三个历史策略到临时槽。
- 策略切换保留当前日期。历史日期只允许 `today`、`tomorrow` 和 `d25`；此时禁用
  `long` 并说明其只支持当前日期，禁止自动把日期重置为当前。
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

不得修改三板 v16 评分权重实现，不得提前修改
today主/降级窗口、tomorrow或d25动作门槛。

### 5.3 先写契约

将 `docs/need.md` 第26节改成明确状态：

- `baseline_v14_active`：批次一完成时仍活动的评分和TopK，仅保留为迁移记录；
- `v15_parallel_data_contract`：本批完成后活动的数据契约；
- `v16_board_scoring_contract`：由批次二一次性切换为today/tomorrow/d25活动评分契约。

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

五个逻辑lane共用组合根创建的一个 `BoundedExecutor`，固定6个worker：5个普通来源
worker与5个普通待处理槽位，加1个候选/TopK紧急worker与1个紧急待处理槽位，
线程名前缀为 `source-data`。每个逻辑lane最多一个运行任务和一个合并后的
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

四策略共享同一份点时结构化风险快照。新版本硬过滤字段固定为
`negative_announcement_level`、`shareholder_reduction_level`、`unlock_risk`、
`pledge_risk` 和 `financial_deterioration`；前四项任一大于0或
`financial_deterioration > 0.5` 即剔除，不得进入候选评分、DeepSeek或排名。

- 股东减持独立生成 `shareholder_reduction_level`：控股股东或实际控制人为3，持股5%
  及以上股东或董监高为2，其他依法披露股东为1；已明确终止且未实施的计划不触发，
  多条有效事实取最高等级。
- 解禁独立生成 `unlock_risk`：观察日起未来90个自然日累计占总股本比例低于1%为0、
  1%-5%为1、5%-10%为2、不低于10%为3。
- 每个来源只能在自身请求成功且schema有效时确认0；失败、缺失或非有限保持 `null`，
  不得借另一来源成功补0。
- 旧 `reduction_or_unlock` 只供旧冻结按旧策略版本回放；新策略不得读取它执行硬过滤、
  风险展示或离线复算。

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

- [x] 东方财富和新浪能同时进入各自worker。
- [x] 腾讯慢或失败不阻塞全市场统一快照。
- [x] Tushare未安装、无Token、429、超时均不阻塞免费链路。
- [x] 同源并发调用只产生一次物理请求。
- [x] fresh、stale、degraded和负缓存不会改变原始来源时间。
- [x] LRU容量和淘汰顺序在注入时钟下确定。
- [x] 14:49:50会绕过fresh行情缓存且不会重复物理请求。
- [x] 缓存条目、估算字节数和命中/淘汰指标有界可观测。
- [x] 5500行情和360候选的显式性能报告达到v15预算。
- [x] 队列满时保留最新观察点，不补跑旧周期。
- [x] 不同线程完成顺序产生相同快照和哈希。
- [x] 迟到、未来时间、空版本和乱序结果不能覆盖新值。
- [x] 多源偏差0.50%通过，超过0.50%且未复核只能观察。
- [x] 上市第1/5日拒绝，第6日恢复。
- [x] 主板8.00/8.01和两成长板16.00/16.01边界通过。
- [x] `create_app()`仍无I/O副作用。
- [x] 旧冻结可读，新冻结可离线校验哈希。
- [x] v14评分、门槛和TopK输出未改变。

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

将 `v16_board_scoring_contract` 改为覆盖today、tomorrow、d25的活动生产契约，并明确
v14/v15仅用于旧快照回放。切换必须在同一交付中原子更新配置、实现、测试、API加法
字段、冻结算法版本和文档，不允许以运行时开关只启用其中一个策略或板块。

先增加：

- `tests/unit/domain/test_board_scoring.py`：三板权重、公式和缺失；
- `tests/unit/domain/test_ranking.py`：板内候选、全局选择和稳定排序；
- `tests/unit/domain/test_risks.py`：减持/解禁独立硬过滤与七项本地风险；
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

| 板块 | today | tomorrow | d25 |
| --- | --- | --- | --- |
| 主板 | 流动30、日内25、换手20、同行15、完整10 | 流动35、同行15、趋势25、稳定15、完整10 | 流动30、残差20、趋势20、稳定15、执行10、完整5 |
| 创业板 | 流动30、日内25、换手20、同行15、完整10 | 流动20、同行30、趋势25、稳定15、完整10 | 流动20、残差30、趋势20、稳定10、执行15、完整5 |
| 科创板 | 流动30、日内25、换手20、同行15、完整10 | 流动25、同行15、趋势30、稳定20、完整10 | 流动25、残差15、趋势30、稳定15、执行10、完整5 |

today三个板块使用同一权重向量，但必须按板块分别建立总体、截尾、归一化、可靠度和
排名，禁止共享横截面结果。每行必须除以100写入配置并精确合计1.0。

候选按 `strategy + board` 独立执行固定质量门：先通过全部硬过滤，再要求
`candidate_score >= 50`、核心字段缺失比例不超过30%；通过后按候选分稳定排序，每板
最多120只且不得为了满额降低门槛。`board_data_reliability >= 0.85` 的候选才可进入
动作和DeepSeek复核队列；低可靠度候选只能在可靠候选之后作为观察项展示，不消耗
DeepSeek预算。DeepSeek不得新增池外股票。

### 6.6 固定本地评分权重

| 板块 | today | tomorrow | d25 |
| --- | --- | --- | --- |
| 主板 | 日内30、换手20、同行20、流动执行20、稳定10 | 尾盘15、同行领先15、换手量价10、趋势25、稳定25、市场10 | 残差15、趋势25、质量价值25、稳定15、量价流动10、不过热10 |
| 创业板 | 日内30、换手20、同行20、流动执行20、稳定10 | 尾盘20、同行领先25、换手量价20、趋势20、稳定10、市场5 | 残差30、趋势20、质量价值10、稳定10、量价流动20、不过热10 |
| 科创板 | 日内30、换手20、同行20、流动执行20、稳定10 | 尾盘15、同行领先15、换手量价10、趋势30、稳定25、市场5 | 残差15、趋势30、质量价值25、稳定15、量价流动10、不过热5 |

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

today子组件固定为：

- 日内结构沿用第11节5分钟涨幅、涨速、当日涨幅和量比的点时输入，并增加板内同行
  1/3/5日趋势延续结果；
- 换手状态使用登记后的换手冲击、成交额冲击和量价确认，不实现补涨或极端反转分支；
- 同行差使用同板同行业总体，正 `peer_gap` 得高分；
- 流动性与执行使用20日成交额分位、换手适中和距价格限制安全度；
- 稳定为低波动50%、低最大回撤50%。

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

today、tomorrow、d25统一使用七项短线本地风险扣分，单项和理论合计固定为：

| `risk_code` | 扣分 |
| --- | ---: |
| `near_limit_crowding` | 5 |
| `price_volume_divergence` | 4 |
| `high_volatility` | 3 |
| `short_term_overheat` | 3 |
| `intraday_reversal` | 4 |
| `liquidity_contraction` | 3 |
| `trend_breakdown` | 3 |

本地风险实际扣分在25分截断。新增四项只识别非线性极端结构，普通涨幅、收盘位置、
流动性和趋势差异仍由连续组件评分，禁止重复处罚；today的 `intraday_reversal` 至少
取得30个有效交易分钟后才可判断。缺失、非有限或样本不足只记录缺失，不触发扣分；
long不使用这七项。每个事实生成稳定 `risk_fact_id`，同一事实本地只扣一次，并与
DeepSeek事实去重。

`strategy.json` 必须为七项风险登记精确触发公式、数值阈值、适用策略、证据TTL、
最低置信度、互斥/叠加组和 `risk_fact_id` 字段。当前尚未登记的新增四项精确边界是
v16生产切换的阻断条件，禁止实现者根据描述自行猜值或只实现模糊布尔判断。

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

- today主执行：70.00分及以上可执行，65.00-69.99观察，低于65.00不可执行；
- today降级执行：76.00分及以上可执行，71.00-75.99观察，低于71.00不可执行；
- tomorrow：78.00分及以上可执行，73.00-77.99观察，低于73.00不可执行；
- d25：76.00分及以上可执行，71.00-75.99观察，低于71.00不可执行；
- stale、可靠度不足或veto优先于分数门槛执行现有降级规则；
- 默认TopK 10，接口范围0-18；
- 单一板块最多 `ceil(top_n * 60%)`；`top_n=0` 时上限为0；
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

三板与today/tomorrow/d25的九个组合必须完整，每组权重必须精确合计1.0，所有因子必须
存在于 `factor_registry`，全部字段进入策略版本哈希。

冻结、SQLite、JSON和API加法保存：

- `board`、`board_policy_id`、`board_policy_version`；
- 板内总体版本、分位、样本数和回退日期；
- `board_data_reliability`；
- `BoardScoreBatch.status` 和降级原因；
- `merge_epoch`、板内排名、全局排名；
- 竞争组来源、限制值和跳过原因。
- `shareholder_reduction_level`、`unlock_risk`、各自来源终态、证据时间和缺失原因；
- 七项本地风险事实、实际值、阈值、扣分、去重身份和截断前后合计。

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
| 本地评分 | code + strategy + board + merge_epoch + policy_id | 1080项 | 任一身份变化 |
| 竞争组映射 | industry_version + manual_group_version | 2代 | 分类或配置变化 |
| 原始DeepSeek结果 | 既有raw key + board feature version | 2000项、600秒 | 证据/模型变化 |
| 策略复核结果 | raw key + strategy + policy + challenger状态 | 2000项、600秒 | 权重/挑战者变化 |

本表全部条目在v16加入同一个 `runtime.json.market_data.cache_policy.datasets`；不得把
评分或DeepSeek容量另存到策略配置或构造器默认参数。

缓存约束：

- 板内截尾、分位和同行统计只计算一次并供同epoch候选读取；
- 主板、创业板和科创板的归一化结果不得互相命中；
- today、tomorrow和d25可以复用原始历史摘要，但不能复用策略评分或排名；
- 风险事实变化、phase变化、价格变化达到1%、量比变化达到0.3、最终补审、
  policy/schema/model变化均使对应复核身份失效；
- 缓存命中与冷算必须生成完全相同的业务投影和冻结哈希；
- 缓存只保存不可变值，不保存future、线程、锁、客户端或可变FeatureSnapshot；
- DeepSeek已见代码集合按 `trade_date` 隔离且最多6000项，新交易日清空；
- 评分lane停止时取消未开始future并清空仅属于未发布epoch的临时条目；
- 已发布和冻结对象由现有仓储拥有，不复制到评分LRU。

v16性能目标使用三个板各120只候选、三个策略：

- 单板候选预选P95不超过250毫秒；
- 单板单策略本地评分P95不超过250毫秒，三个策略合计不超过750毫秒；
- 三板并行完成三个策略的本地计算墙钟P95不超过1000毫秒；
- 360只候选单策略全局稳定选择P95不超过100毫秒；
- 三板结果齐备到本地草稿发布P95不超过500毫秒；
- 候选报价变化到本地草稿发布P95不超过5秒；
- 有效DeepSeek结果到hybrid重发布P95不超过1秒；
- SSE发布延迟P95不超过2秒；
- 14:49:50最终报价、三板评分和完整草稿必须在14:50前结束，否则不补造冻结。

普通测试验证每个epoch的横截面只计算一次、跨板/跨策略不得错误命中、冷热业务
投影相同、风险与版本失效、LRU容量和停止清理。性能测试固定比较顺序参考路径与
三lane墙钟，报告队列等待、进程CPU时间和 `sequential_wall / lane_wall` 加速比；
无实测加速时只能表述为隔离和有界并发，但仍必须达到1000毫秒绝对预算。

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

- [x] 三个评分lane先全部提交再等待，每个lane只有一个worker且背压相互隔离。
- [x] 相同输入不同板块得到各自策略确定的不同分数。
- [x] 每组候选和本地权重精确合计1.0。
- [x] today、tomorrow、d25三策略与三板共九组候选和本地评分策略全部存在。
- [x] 三板横截面完全隔离，不借用其他板分位。
- [x] 同行9只缺失、10只生效；领先组2只缺失、3只生效。
- [x] 板内样本99只回退、100只直接计算。
- [x] 回退第5日有效、第6日只能观察。
- [x] 换手/成交额分母零、负数、NaN、Infinity保持缺失。
- [x] 可靠度0.8499只能观察，0.85通过可靠度门。
- [x] 候选分49.99/50.00、核心缺失30%边界和每板119/120/121只处理正确，且不足不补满。
- [x] d25路径不存在双乘数。
- [x] 固定融合向量仍得到83.40。
- [x] today主执行69.99/70.00和降级执行75.99/76.00边界正确。
- [x] tomorrow 77.99/78.00和d25 75.99/76.00边界正确。
- [x] `top_n=0/1/10/18` 时单板 `ceil(top_n * 60%)` 边界正确。
- [x] 主板竞争组第3/4只、创业板和科创板第2/3只边界正确。
- [x] 减持1/2/3级、终止未实施、解禁边界和单来源失败保持null全部正确。
- [x] 七项本地风险边界和5/4/3/3/4/3/3扣分正确，同一事实不重复且总扣分截断为25。
- [x] 一个板块失败时不发布偏置的新TopK。
- [x] 三板不同完成顺序产生相同全局排序和冻结哈希。
- [x] 冷缓存和热缓存产生相同业务投影及冻结哈希。
- [x] 同一epoch的板内横截面只计算一次。
- [x] 跨板、跨策略、跨policy和跨schema缓存均不命中。
- [x] 360候选三策略性能报告达到v16预算。
- [x] 报价到本地草稿、复核到hybrid和SSE延迟达到目标。
- [x] DeepSeek总预算仍为158/188，且不存在板块重复预算。
- [x] today已按v16三板评分启用；long、冻结时间、SSE和旧快照兼容行为不变。

### 6.13 批次二提交和停止

Review重点：板内总体泄漏、权重漂移、缺失值伪装、DeepSeek预算竞争、跨epoch
混合、失败板块偏置、冻结一致性、旧回放和UI字段兼容。

提交信息固定为：

```text
feat(strategy): add board-specific parallel scoring
```

推送后核对 `HEAD == @{upstream}`，把批次二标为 `[x]` 并停止。不得继续创建
回测、收益校准或下一版策略任务。

## 7. 批次三：v17 六阶段递减内存池与Web性能硬化

### 7.1 进入条件与目标

- v15和v16已分别提交、推送并通过全部门禁；
- 固定完整日fixture能稳定复算三板结果和冻结哈希；
- 工作树中没有未闭合的前两批修改；
- 用户再次发送“继续”。

本批在行为等价前提下把“SDK采集 -> 标准化与合并 -> 硬过滤与候选 -> 本地评分 ->
DeepSeek主审/挑战者 -> 确定性融合与Web发布”固化为六阶段有界内存流水线，并完成
启动恢复、锚点增量展示、Web热索引读取和性能验收。`strategy_version`、评分、风险、
融合、门槛、排名、冻结身份和既有API业务字段不得变化；只提升runtime/config版本、
缓存schema和只读接口内部实现。

### 7.2 允许修改的文件

- `docs/need.md` 第4、6、7、18、22、23、25、26节；
- `config/v2/runtime.json` 和 `src/trader/infrastructure/settings*.py`；
- v15的 `application/cache.py`、`infrastructure/cache.py`、`market_data/merge.py`、
  `market_data/gateway.py` 和 `market_data/service*.py`；
- v16的 `application/board_scoring.py` 和 `board_scoring_cache.py`；
- `application/ports.py`、`queries.py`、`status.py`、`publisher.py` 和 `runtime.py`；
- `application/cadence.py`、`schedule.py`、`pipeline_stages.py` 和冻结工作流；
- 新建 `application/stage_cache.py` 和 `application/published_views.py`；
- 新建 `application/freeze_snapshot.py`，只在检查点/冻结边界组装完整回放快照；
- `infrastructure/persistence/writer.py` 及其只读拆分模块；
- 新建 `infrastructure/published_index.py`；
- `infrastructure/deepseek/cache.py`，只允许迁移到统一有界缓存，不改变复核语义；
- `bootstrap.py`、`web/routes.py`、`web/sse.py`、`web/static/dashboard.js` 和必要的
  只读序列化代码；
- 新建 `src/trader/application/performance.py`；
- 新建 `src/trader/infrastructure/performance.py`；
- `src/trader/entrypoints/cli.py`；
- 新建 `tests/performance/run_v17_end_to_end.py`、
  `tests/fixtures/performance/v17/manifest.json` 和脱敏录制fixture；
- 与性能审计、缓存或并发行为直接相关的现有测试。

不得修改因子公式、权重、风险规则、DeepSeek schema、动作门槛或Web布局；只允许
调整日期与策略切换状态、禁用态、说明文案和后台预取行为。

### 7.2.1 固定六阶段内存结构

必须复用v15的应用端 `BoundedCache` 端口和基础设施 `BoundedLruCache`，由组合根统一
创建和注入，不得让Web、仓储、评分、DeepSeek或来源适配器各自创建缓存。固定预算为：

| 阶段池 | 业务内容 | 上限 | 主要对象 |
| --- | --- | ---: | --- |
| P1来源与基础数据 | SDK观测、分钟、历史、研究、主数据、日历、估值和财务 | 128 MiB | `SourceObservation`及不可变来源结果 |
| P2标准化与统一行情 | 多源确定性合并、全市场当前报价索引、候选定向报价 | 56 MiB | `CanonicalMarketSnapshot`、`CurrentQuoteIndex` |
| P3过滤与候选特征 | 硬过滤、候选特征、板内横截面和竞争组 | 24 MiB | 精简候选批次和上游版本引用 |
| P4本地评分 | 板块本地评分、板块批次、全局本地草稿 | 16 MiB | `BoardScoreBatch`和紧凑分数记录 |
| P5DeepSeek复核 | 原始主审、挑战者及策略分类结果 | 12 MiB | 结构化 `DeepSeekReview` |
| P6发布与Web | 当前TopK、最近20日历史投影、日期索引和overlay身份 | 12 MiB | `PublishedRecommendationView` |
| 运行开销保留 | 队列、single-flight、future、锁、索引和统计 | 8 MiB | 不属于缓存条目 |

P1-P6缓存条目上限合计固定为248 MiB，运行开销保留固定为8 MiB，总计256 MiB。
每阶段同时受条目数、代数和估算字节数约束，任一先到上限即按本阶段策略淘汰；P1
不得抢占P6，P6也不得挤压P1。`MiB`固定为 `1024 * 1024` 字节。

### 7.2.2 固定对象所有权和去重规则

- P1拥有外部来源结果；适配器完成字段解析和基础合法性校验后形成不可变
  `SourceObservation`。缓存不得保存requests响应、SDK对象、DataFrame或数据库连接。
- P2拥有一个观察点的统一行情。标准化不是再复制一份全量原始载荷，而是把多个P1
  观测确定性合并成一个按代码索引的规范快照；同一epoch最多保留3代全市场快照。
- P3只保存候选代码、计算必需特征、过滤统计、审计引用和P2版本，不得嵌套完整
  `CanonicalMarketSnapshot`。过滤详情按股票代码和规则码紧凑存储。
- P4只保存板块、策略、局部组件分、风险扣分、可靠度、板内排名、上游批次身份和
  降级原因，不得复制分钟序列、历史日线、研究正文或全量FeatureSnapshot。
- P5只保存已通过schema验证的结构化复核结果、报价失效锚点和过期时间；不保存prompt
  全文、思维链、HTTP响应、客户端或完整证据正文。
- P6保存API和页面真正需要的表格、抽屉和元数据投影；完整冻结回放输入、5500只全
  市场特征和全部过滤审计继续只由SQLite/JSON仓储拥有。
- 新增轻量 `RuntimeRecommendationResult`，固定保存策略、日期、阶段、版本、融合模式、
  TopK、过滤汇总、降级原因和上游epoch引用，不包含 `RecommendationReplayInput`。
  普通盘中发布只能生成该类型和P6投影。
- 现有完整 `RecommendationSnapshot` 改为冻结/离线复算类型，只能由
  `FreezeSnapshotFactory` 在11:19:50、14:49:50检查点或正式冻结恢复时，按精确epoch从
  P1-P4读取仍有效的不可变输入并组装；普通评分周期不得构造完整回放对象。
- `PreparedSnapshot` 不再长期携带5500只 `market_features` 元组，改为保存
  `market_epoch_ref`、候选批次引用和必要的当前策略数据；调用结束后必须释放局部引用。
- 跨线程边界只复制一次不可变业务投影。后续阶段通过 `upstream_epoch`、
  `data_version`、`payload_hash`、`policy_id` 和 `schema_version` 引用上游，不允许通过
  可变对象共享隐式状态。

### 7.2.3 cache schema v6

`runtime.json.market_data.cache_policy` 升级为schema v6并严格包含：

```text
schema_version = 6
policy_version
datasets
groups
total_bytes = 260046848
runtime_reserve_bytes = 8388608
pool_total_bytes = 268435456
estimator_version = canonical_json_utf8_v1
```

`groups`固定为P1-P6六项且字节数必须精确为128/56/24/16/12/12 MiB；六组之和必须
等于 `total_bytes`，再加 `runtime_reserve_bytes` 必须等于 `pool_total_bytes`。loader
拒绝未知组、缺失组、数值漂移、组和不符以及保留内存被配置成缓存条目。

每个数据集增加 `retention_mode`：

- `ttl`：来源数据，必须定义刷新TTL或cadence、动作年龄、负缓存TTL和容量；继续支持
  fresh、refresh-due、stale、degraded和single-flight。
- `epoch`：派生数据，只接受完全相同的epoch身份；必须定义 `max_generations`，禁止
  stale-while-revalidate和跨epoch回退，失败不写负缓存。
- `delivery`：已发布内存投影；必须定义 `resident_trade_days`、`cold_slots`、
  `pin_current` 和 `maximum_entry_bytes`，当前项只能由更新版本原子替换。

schema v6沿用现有 `CacheIdentity`，派生数据的 `source` 固定为 `pipeline` 或
`deepseek`，`subject_key` 固定包含策略、板块或日期；不得新增第二套身份算法。

### 7.2.4 P1现有来源数据集固定映射

以下v15数据集全部归入P1，原TTL、动作年龄和容量不得因分池而改变：

| 数据集 | 刷新TTL或cadence | 动作/降级边界 | 负缓存TTL | 条目容量 | 持久恢复 |
| --- | ---: | ---: | ---: | ---: | --- |
| `full_market_quotes` | 当前 `full_market` cadence | cadence 3倍 | 10秒 | 每源6000 | 否 |
| `candidate_quotes` | 当前 `candidate_quotes` cadence | cadence 3倍 | 3秒 | 每源360 | 否 |
| `intraday_minutes` | 45秒 | 90秒 | 45秒 | 每源360 | 否 |
| `research_success` | 600秒 | 1200秒 | 60秒 | 每源360 | 既有证据文件 |
| `research_failure` | 60秒 | 60秒 | 60秒 | 每源360 | 否 |
| `daily_history` | 21600秒 | 86400秒 | 60秒 | 每源360 | 否 |
| `security_master_calendar` | 86400秒 | 86400秒 | 300秒 | 每源6000 | 否 |
| `daily_valuation_financials` | 86400秒 | 86400秒 | 300秒 | 每源360 | 否 |

P1达到128 MiB时先淘汰负缓存和已degraded条目，再按确定性LRU淘汰；当前可执行候选
所引用的fresh来源条目不设置永久pin，动作是否可执行仍由来源时间和动作年龄决定。

### 7.2.5 P2-P6派生数据集固定映射

| 数据集 | 阶段池 | 保留边界 | 固定失效条件 |
| --- | --- | ---: | --- |
| `canonical_market_snapshot` | P2 | 3代 | 来源版本、交易日、阶段、配置、schema或merge epoch变化 |
| `canonical_candidate_snapshot` | P2 | 6代 | 候选报价版本、代码集合或epoch变化 |
| `current_quote_index` | P2 | 3代、每代最多6000只 | 新统一行情按版本CAS替换 |
| `history_summary` | P3 | 360项 | 股票历史版本变化 |
| `candidate_feature_batch` | P3 | 24代 | canonical、分钟、研究、规则或schema变化 |
| `hard_filter_batch` | P3 | 九组合各4代 | 特征、策略、板块、规则或epoch变化 |
| `board_cross_section` | P3 | 24代 | 日期、阶段、板块、数据版本或schema变化 |
| `candidate_preselection` | P3 | 九组合各4代 | 策略、板块、epoch或policy变化 |
| `competition_group_mapping` | P3 | 2代 | 行业或人工分组版本变化 |
| `local_score` | P4 | 1080项 | 特征、策略、板块、epoch或policy变化 |
| `board_score_batch` | P4 | 九组合各4代 | 任一本地评分身份变化 |
| `global_local_draft` | P4 | 每策略4代 | 三板批次、选择策略或epoch变化 |
| `deepseek_raw_review` | P5 | 2000项、600秒 | 证据、模型、报价或板内特征版本变化 |
| `deepseek_strategy_review` | P5 | 2000项、600秒 | 策略、policy、挑战者状态或融合分类变化 |
| `deepseek_seen_codes` | P5 | 每交易日6000项 | 新交易日清空 |
| `published_recommendation_view` | P6 | 72项 | 发布、冻结、overlay或fallback身份变化 |
| `published_date_index` | P6 | today/tomorrow/d25各1份 | 冻结提交成功后原子更新 |

P5报价失效边界继续固定为价格变化达到1%、量比变化达到0.3；证据manifest、模型、
prompt/schema、板块policy或策略变化必须失效。DeepSeek不得把候选池外股票加入任何
复核或TopK，不得把158/188预算按板块复制。

P6的72项固定分为当前四策略4项、最近20个交易日三策略60项，以及归档冷读取、原子
替换和回退8项。单个 `PublishedRecommendationView` 规范JSON不得超过160 KiB；超过
时记录 `published_view_too_large` 并保留最近有效视图，不允许截断API契约字段、突破
12 MiB或把完整回放输入塞入P6。

### 7.2.6 来源调用频率和线程模型

- 东方财富和新浪全市场同时提交：warmup 60秒、today_main 30秒、today_late 60秒、
  midday 60秒、afternoon 60秒、final_review 30秒。
- 腾讯候选报价：warmup 10秒、today_main 5秒、today_late 10秒、midday 60秒、
  afternoon 10秒、final_review 3秒、final_window 2秒。
- 腾讯TopK报价：warmup 5秒、today_main 3秒、today_late 5秒、midday 60秒、
  afternoon 5秒、final_review 2秒、final_window 3秒。
- Tushare主数据、交易日历、估值和财务只在盘前、盘后或24小时TTL触发；前复权历史
  只在盘前、盘后或6小时TTL触发，不承担全市场和TopK实时报价。
- AKShare研究成功结果10分钟，失败负缓存1分钟；市场新闻按60-120秒任务周期，个股
  风险按120-300秒任务周期。网页请求不得改变这些周期。
- 来源I/O继续使用一个共享 `source-data` 有界执行器：5个普通worker与5个普通槽位，
  加1个腾讯紧急worker与1个紧急槽位；每个来源lane最多一个运行任务和一个latest-wins
  请求，不得为股票逐一创建线程。
- 标准化继续使用现有2 worker池；v16三个板块各自使用独立单worker评分lane；
  DeepSeek继续使用4 worker全局池；SQLite/JSON继续只有1个写worker；确定性合并继续
  只有1个所有者。缓存自身不得创建线程或定时器。

### 7.2.7 端到端数据流和提交边界

1. SDK适配器生成P1不可变观测；single-flight合并相同身份，失败按来源数据集写短期
   负缓存，latest-wins丢弃旧观察点。
2. 标准化与多源合并从P1生成P2统一快照和按代码当前报价索引；未来、无时区、迟到、
   空版本和非有限值不能进入P2。
3. 硬过滤从P2及P1慢数据生成P3候选批次；输出候选代码、必要特征和审计引用，不复制
   全市场快照。v16按板块和策略独立预选，每板最多120只。
4. 三个板块lane从P3生成P4 `BoardScoreBatch`；必须先提交三个lane再等待，任一板块
   failed或epoch不一致时不生成偏置的新TopK。
5. 全局Reviewer从P4统一规划P5复核；先主审、再受控挑战者，保持158正常目标和188
   物理硬上限。DeepSeek失败保留local draft，不伪造hybrid。
6. 单一合并器按68/32固定融合、风险门和稳定排序形成轻量
   `RuntimeRecommendationResult`；local draft和
   hybrid重发布都必须具有独立版本，迟到结果不能覆盖更新版本。
7. 普通盘中发布直接从运行期结果生成精简 `PublishedRecommendationView`，CAS替换P6
   后更新RuntimeState并发送SSE；该路径不得调用完整推荐仓储、写SQLite或写JSON。
8. 进入冻结前30秒窗口时，只有该策略最新、完整、满足年龄要求且截止前生成的领域
   结果允许写一次可替换恢复检查点；正式冻结仍执行staged、原子文件、committed协议。
9. Web只组合P6发布投影和P2 `current_quote_index`。历史锚点及冻结对象不可修改；
   “今日涨跌”和“锚点至今”只在响应投影中用当前统一行情计算。

### 7.2.8 启动、归档和Web读取接口

- 仓储初始化先执行staged manifest恢复、孤儿文件隔离和SHA-256校验，再构建P6；损坏、
  缺失、未提交或哈希不符快照不得进入内存。
- HTTP开始接收请求前，同步预热最近20个交易日的 `today/tomorrow/d25` 共60个
  committed历史投影，以及全部历史日期的轻量字符串索引；预热只读本地持久化，不
  调用外部网络。盘中当前P6槽不从普通草稿文件恢复，而是在首个有效流水线发布后变为
  ready；冻结边界后启动则先加载或补交当日committed冻结。
- 应用层新增 `PublishedSnapshotReadPort`，固定提供 `latest(strategy)`、
  `load_frozen(strategy, trade_date)`、`recommendation_dates(strategy)`、
  `current_quotes(codes)` 和 `stats()`；`RecommendationQueries` 不得持有持久化仓储、
  行情服务、评分器或DeepSeek引用。
- 基础设施实现单一 `PublishedSnapshotIndex`。盘中 `publish_view` 只更新P6；
  `save_freeze_checkpoint`、`freeze` 和 `save_closing_overlay` 由独立持久化端口负责，
  禁止用写穿仓储装饰器让每次P6发布隐式落盘。归档回退必须先校验SHA-256。
- `GET /api/recommendations/<strategy>`、`GET /api/recommendation-dates` 和既有响应字段
  保持兼容。当前P6变为ready后以及最近20日请求必须零SQLite、零文件、零网络、零评分
  和零DeepSeek；首个当前版本尚未生成时返回 `not_ready` 或既有显式冻结fallback。
- 20日以前日期首次请求允许从归档加载，并由single-flight一次性并行预取同日期三个
  历史策略到8个临时槽；预取完成后的Tab切换必须只读内存。
- 前端切换 `today/tomorrow/d25` 时保留日期，不再执行 `state.date = ""`。历史日期
  下禁用 `long` 并显示“长期策略仅支持当前日期”；回到当前日期后恢复。
- 服务端和前端缓存身份固定为 `(strategy, requested_date|current, snapshot_id,
  overlay_version, fallback_identity)`，禁止跨策略、跨日期或跨交易日复用。
- `/api/status` 以加法返回P1-P6的ready/warming状态、条目数、容量、估算字节、命中、
  未命中、待刷新、陈旧、负缓存、淘汰、加载错误、最近epoch、预热日期数和数据年龄，
  不返回完整行情、证据或推荐载荷。

### 7.2.9 发布、检查点、冻结和收盘持久化矩阵

`PublishedRecommendationView` 是P6中的内存/API投影，不是冻结JSON，也不承担离线
复算。完整领域快照是否落盘严格按下表执行：

| 时机 | P6/SSE | 完整SQLite/JSON | 用途 |
| --- | --- | --- | --- |
| 普通盘中local draft | 原子发布 | 不写 | 快速展示本地降级结果 |
| 普通盘中hybrid重发布 | 原子替换并发SSE | 不写 | 展示DeepSeek完成后的最终融合 |
| today 11:19:50单点 | 保持最新视图 | 最多写1份可替换检查点 | 崩溃后按need.md补冻结 |
| tomorrow/d25 14:49:50单点 | 保持最新视图 | 每策略最多写1份可替换检查点 | 保存最终报价后的可恢复草稿 |
| today 11:20 | 切换为committed冻结视图 | 正式冻结恰好1次 | 推荐历史和离线复算 |
| tomorrow/d25 14:50 | 切换为committed冻结视图 | 每策略正式冻结恰好1次 | 推荐历史和离线复算 |
| 冻结后盘中报价更新 | 只更新内存overlay并发SSE | 不写 | 最新价、今日涨跌、锚点至今 |
| 15:00首份有效收盘报价 | 更新closing overlay | 每冻结策略最多写1次overlay | 固化收盘展示字段 |
| long任意时刻 | 只更新当前P6视图 | 不写推荐快照和历史 | 当前观察展示 |

检查点固定写入 `checkpoints/<trade_date>/<strategy>.json`，并在现有SQLite中新增
`freeze_checkpoints` 元数据表，字段固定为 `strategy`、`trade_date`、`boundary_at`、
`snapshot_id`、`observed_at`、`relative_path`、`sha256`、`status`、`consumed_at`，主键为
`(strategy, trade_date, boundary_at)`。状态只允许 `ready/consumed/quarantined`。
11:19:50或14:49:50单点任务每策略最多写一次；普通周期不得更新文件或表。检查点必须
包含完整性、数据年龄和全部版本。冻结提交成功后将检查点标为consumed并删除文件；
删除失败只记清理错误，不回滚已committed冻结。检查点不得作为实时发布真相源。

11:19:50任务只为today定向刷新当前候选/TopK冻结锚点、复用截止前完成的评分与复核并
生成检查点，不重新开放DeepSeek挑战者；14:49:50任务沿用既有最终候选刷新，完成三板
评分和完整合并后分别为tomorrow、d25生成检查点。任一任务未在冻结边界前完成，只能
记录检查点失败，不能延迟冻结时间或在边界后补写草稿。

进程在冻结边界后重启时，只接受同交易日、截止前生成、距离固定冻结边界不超过30秒、
哈希正确且满足行情年龄要求的检查点；成功补冻结后按固定11:20或14:50时间戳提交。
其他检查点一律隔离，不能用上一交易日、迟到行情或普通盘中P6对象补造冻结。

运行库中的 `pipeline_events`、`deepseek_calls`、预算预留和来源健康审计仍按原事务规则
写SQLite；本节减少的是完整推荐快照和live overlay写放大，不得因此删除关键事件审计。

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
- today、tomorrow和d25三个策略；
- 120只分钟序列、360只历史摘要、360只研究状态；
- 最近20个交易日的三策略发布投影、四个当前投影、全部日期轻量索引和实时报价索引；
- 20个并发当前快照请求；
- 20个并发日期与策略组合请求，其中包含同一历史日期的三策略切换；
- 10个SSE客户端，其中2个慢客户端；
- 09:15至15:00虚拟交易日和14:49:50最终刷新；
- 一个超时来源、一个熔断来源和一个乱序迟到结果。
- 11:19:50 today检查点、14:49:50 tomorrow/d25检查点、三个正式冻结、三个15:00
  closing overlay，以及普通盘中100次local/hybrid内存发布。

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
样本数、P50/P95/max、队列等待、进程CPU时间、实际加速比、物理调用、P1-P6条目和
估算字节、运行开销保留峰值、single-flight数量、队列峰值、tracemalloc峰值、
按用途分类的SQLite/JSON读写次数、发布版本数、冻结哈希和通过状态。持久化计数必须
区分事件审计、DeepSeek预算/调用、冻结检查点、正式冻结、收盘overlay和普通推荐发布。
报告还必须分别记录上游原始载荷字节、进入下一阶段的精简投影字节和重复复制率；输出
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
| 单板单策略本地评分 | 250毫秒 |
| 单板三个策略本地评分 | 750毫秒 |
| 三板并行三个策略本地计算墙钟 | 1000毫秒 |
| 360只全局稳定选择 | 100毫秒 |
| 三板齐备到本地草稿发布 | 500毫秒 |
| 候选报价变化到本地草稿 | 5秒 |
| DeepSeek结果到hybrid重发布 | 1秒 |
| SSE已发布版本到客户端队列 | 2秒 |
| 当前快照API 200响应 | 200毫秒 |
| P6驻留历史快照API 200响应 | 200毫秒 |
| ETag命中304响应 | 50毫秒 |
| 驻留日期列表API | 100毫秒 |
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

固定阶段预算为：

| 阶段池 | 缓存条目上限 | 固定容量/代数补充 |
| --- | ---: | --- |
| P1来源与基础数据 | 128 MiB | 执行第7.2.4节八个来源数据集容量 |
| P2标准化与统一行情 | 56 MiB | 全市场3代、候选6代、当前报价索引3代 |
| P3过滤与候选特征 | 24 MiB | 历史摘要360项、九组合各4代、横截面24代 |
| P4本地评分 | 16 MiB | 本地评分1080项、九组合各4代、每策略草稿4代 |
| P5DeepSeek复核 | 12 MiB | raw 2000、strategy 2000、当日已见代码6000 |
| P6发布与Web | 12 MiB | 72个视图、三份历史日期索引、单视图最大160 KiB |
| 缓存条目合计 | 248 MiB | 不得借用8 MiB运行开销保留 |
| 运行开销保留 | 8 MiB | 队列、future、single-flight、锁、索引和统计 |
| 项目内存池合计 | 256 MiB | `268435456`字节 |

`runtime.json.performance_budgets.memory` 固定包含：

```text
pool_total_bytes = 268435456
cache_entry_total_bytes = 260046848
runtime_reserve_bytes = 8388608
stage_max_bytes = {p1: 134217728, p2: 58720256, p3: 25165824,
                   p4: 16777216, p5: 12582912, p6: 12582912}
published_view_max_bytes = 163840
resident_trade_days = 20
cold_archive_slots = 8
growth_percent = 20
```

配置loader必须拒绝字段缺失、未知字段、阶段和不等于248 MiB、保留值不等于8 MiB、
总量不等于256 MiB、P6条目上限与20日驻留公式不一致，以及性能配置与cache_policy
漂移。上述数值不得复制到缓存类、Web代码、测试fixture或性能runner默认参数。

淘汰和超限规则固定为：

- P1先淘汰过期负缓存，再淘汰degraded，最后按LRU；不得延长来源动作年龄。
- P2-P4先淘汰非当前epoch和最老代；相同epoch正在被下游读取的不可变值允许由调用者
  持有局部引用，但缓存索引不得为此永久pin。
- P5先清除过期review，再按LRU；预算计数和已发生物理调用不随淘汰回退。
- P6永久优先四个当前视图，其次最近20日60个历史视图；8个冷槽只能淘汰其他冷槽，
  不得驱逐当前和20日保证集合。
- 单条记录大于所属池上限直接拒绝并计 `stage_entry_too_large`；不得跨池存放或临时
  突破总量。
- P1-P6任一池满只能影响本池新写入，不得级联清空其他池；继续保留最近有效下游发布
  视图并标记对应阶段降级。

使用 `tracemalloc` 报告项目分配峰值，不把解释器、第三方库共享页或操作系统页缓存
伪装成项目缓存。第10和第100个模拟tick完成后分别强制GC并记录项目跟踪字节数，
增长率固定为 `(tick100_bytes - tick10_bytes) / max(tick10_bytes, 1)`，不得超过
0.20。性能报告必须同时证明缓存条目不超过248 MiB、项目自有运行结构没有耗尽8 MiB
保留、各阶段条目数和代数不越界；操作系统RSS只作环境审计，不替代规范JSON估算和
tracemalloc项目分配测量。

### 7.7 优化顺序

只有基线证据确认热点后，按以下顺序修改：

1. 在计时轮次之外用标准库profiler和进程CPU/墙钟比区分I/O等待、重复计算、锁竞争
   和纯Python CPU热点，不把线程开始时间重叠当作加速。
2. 用固定负载记录v5八个数据集、DeepSeek旧缓存和发布快照的真实条目大小、峰值和
   命中率；不得仅按配置上限推断使用量。
3. 消除同一epoch重复的历史摘要、截尾、分位和序列化。
4. 使用按代码索引的一次遍历代替候选与全市场的嵌套扫描。
5. 在跨线程边界只复制一次不可变业务投影，禁止深拷贝客户端对象。
6. 把当前 `PreparedSnapshot` 中仅供冻结回放的全市场输入限制在冻结检查点/正式冻结
   构造路径；普通盘中P3-P6只传递精简批次和版本引用，也不构造待落盘的完整JSON。
7. 将缓存统计按lane累计后批量合并，避免每只股票竞争全局锁。
8. 缩短锁持有区间，网络、评分、JSON编码和SQLite调用不得在共享锁内执行。
9. 将既有DeepSeek `ReviewCache` 的内部存储委托统一P5缓存；删除独立容量、TTL和LRU，
   但保持请求键、预算计数、失效阈值和命中语义。
10. 建立内存式 `PublishedSnapshotIndex`，让状态、日期列表和推荐API读取预聚合快照，
   不在HTTP请求中遍历全量运行记录、读取文件或重复反序列化。
11. 历史页面的当前价从P2按代码索引读取，禁止每次请求扫描全市场或修改P6冻结投影。
12. 保持SSE客户端独立有界缓冲，慢客户端丢弃并要求resync，不阻塞publisher。
13. 仅在归档回退路径的 `EXPLAIN QUERY PLAN` 证明查询扫描时增加SQLite索引。
14. 清除生命周期结束的future、回调、临时epoch和被替换发布版本，验证无worker或
    缓存引用泄漏。

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
- 启动预热只读取本地已提交快照；预热失败按策略和日期记录降级，不能阻止其他有效
  快照进入P6，也不能把损坏或哈希不符文件放入内存。
- Web请求不得触发来源刷新。来源lane仍完全按现有cadence和TTL后台更新，发布成功后
  只经P6内存索引和SSE通知前端。
- 最近20个交易日的三策略组合要么全部进入保证驻留集合，要么该日期整体记录
  `resident_triplet_incomplete` 并继续从最近完整日期开始向前补足20日；日期字符串索引
  仍可列出更老归档，不得伪造缺失快照。
- 20日以前日期的三个归档预取共享一个日期级single-flight；部分失败时保留已加载项
  供直接请求，但前端不得把该日期标成“三策略已预热”。

### 7.9 迁移、兼容和回退顺序

1. 首先修改 `docs/need.md` 第6、18、19、22、23、25和26节，使业务契约先于代码生效：
   - 第6节增加11:19:50 today冻结检查点单点；14:49:50明确同时生成tomorrow/d25检查点；
   - 第18节把“实时草稿写入可替换的published/”改成“普通草稿只发布P6，冻结前单点
     写 `freeze_checkpoints`，正式冻结和15:00收盘overlay才持久化”；
   - 第19节明确当前P6未ready时返回not_ready/冻结fallback，Web请求不触发落盘；
   - 第22、23、25节增加P1-P6内存、磁盘写次数和检查点恢复验收。
2. 在任何生产路径修改前，用schema v5固定fixture记录八个来源数据集、旧DeepSeek缓存、
   `PreparedSnapshot`、当前完整快照JSON和Web响应投影的P50/P95大小、峰值和磁盘写次数。
3. 增加schema v6配置类型和失败先行测试；v6 loader严格拒绝v5三组内存配置，现有v5
   配置文件在同一批次原子升级，不提供运行时双schema自动猜测。
4. 先迁移P1、P2并保持现有评分与发布调用不变；验证统一快照、字段来源、冲突集合、
   merge epoch和哈希完全一致后，再迁移P3、P4。
5. v16评分缓存迁移到P3/P4后，迁移DeepSeek `ReviewCache` 到P5；每一步都先比较缓存键、
   命中、预算计数和业务结果，再删除旧内部LRU，禁止永久双写。
6. 新增P6投影和只读端口，先影子比较旧完整快照序列化与P6 API JSON；字段一致后切换Web
   查询和SSE到P6，再删除Web对持久化仓储的直接引用。
7. 最后拆分盘中发布与持久化：普通publish只写P6，新增三个检查点单点、
   `freeze_checkpoints`表和15:00 closing overlay路径。切换前后正式冻结JSON及哈希必须一致。
8. `published_snapshots`旧表和旧 `published/` 文件在v17中停止更新并忽略为当前真相，
   本批不破坏性删除；运维文档标记为旧版本回退兼容数据，后续独立清理任务才能删除。
9. 完成所有门禁后一次性切换组合根到schema v6。回退时使用上一提交和v5配置；新增
   `freeze_checkpoints`表及 `checkpoints/` 文件对旧版本无害，禁止为回退删除冻结历史。

### 7.10 测试和验收

普通回归至少覆盖：

- [ ] schema v6三种保留模式、P1-P6固定组、248/8 MiB分界和未知字段拒绝；
- [ ] P1八个来源数据集的TTL、动作年龄、负缓存、容量和持久恢复语义不漂移；
- [ ] P2-P4跨epoch、板块、策略、policy、schema和版本不能错误命中；
- [ ] P5价格1%、量比0.3、证据、模型、prompt和策略失效边界不漂移；
- [ ] P6当前4项、20日60项、冷槽8项和单视图160 KiB边界正确；
- [ ] single-flight、latest-wins、队列峰值和停止清理；
- [ ] 每阶段只保存精简批次和上游引用，P3-P6不嵌套5500只市场快照或完整回放输入；
- [ ] 冷热业务投影、API JSON和冻结哈希完全一致；
- [ ] 状态/API读取不触发网络、评分、全表缓存扫描或文件写入；
- [ ] 当前P6 ready后、日期列表和最近20日历史请求不触发SQLite或快照文件读取；
- [ ] 启动只预热committed历史；盘中当前槽在首个发布前返回not_ready或显式冻结fallback；
- [ ] 启动恢复严格按manifest和SHA-256校验，损坏、缺失和孤儿文件不会进入P6；
- [ ] 100次普通local/hybrid发布产生100个内存版本和SSE事件，但完整推荐SQLite/JSON
  持久化调用次数严格为0；事件审计和DeepSeek预算写入单独计数且不混淆。
- [ ] 正常交易日冻结检查点最多3次：today一次、tomorrow一次、d25一次；long为0次。
- [ ] 正式冻结最多3次且每策略幂等一次；15:00 closing overlay最多3次，盘中overlay为0次落盘。
- [ ] 11:19:50和14:49:50检查点必须在边界前完成，超时后不得补写或推迟冻结。
- [ ] 在11:19:50/14:49:50后崩溃可用合格检查点补冻结；早于窗口、跨日、迟到、过旧、
  哈希错误或已consumed检查点必须拒绝并隔离。
- [ ] 正式冻结成功后检查点标记consumed；检查点文件清理失败不回滚冻结且可重试清理。
- [ ] P1-P6分别不超过128/56/24/16/12/12 MiB，条目合计不超过248 MiB，运行结构不
  耗尽8 MiB保留，100 tick后项目分配增长不超过20%。
- [ ] 发布、冻结、overlay和HTTP并发时不会出现半版本、跨日期污染或旧结果覆盖；
- [ ] 历史日期在today/tomorrow/d25之间切换不改变日期，且只显示匹配缓存身份；
- [ ] 历史日期下long不可选且不会自动切回当前，回到当前日期后long恢复；
- [ ] 最近20日三策略驻留；20日以前日期首次加载后同日三策略预取由日期级single-flight合并；
- [ ] 历史响应只叠加内存当前报价生成今日涨跌和锚点至今，不修改冻结对象及其哈希；
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

### 7.11 Review、提交和停止

Review重点：缓存污染、跨策略或跨日期错误命中、六池重复数据、预热半状态、无界容量、
锁竞争、普通盘中误落盘、检查点重复写、截止后补写、未committed冻结可见、状态读取
副作用、归档回退查询计划、SSE背压、内存引用泄漏、日期切换重置和业务漂移。

提交信息固定为：

```text
perf: add six-stage in-memory recommendation pipeline
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
- [ ] P1-P6缓存条目分别不超过128/56/24/16/12/12 MiB，合计不超过248 MiB，运行结构
  不耗尽8 MiB保留，项目内存池总量不超过256 MiB。
- [ ] schema v6三种保留模式、六阶段数据集、条目/代数/字节边界和严格配置校验已启用。
- [ ] 服务启动完成时最近20个交易日完整三策略committed投影和全部日期索引已预热；
  当前P6槽在首个有效发布前正确返回not_ready或冻结fallback。
- [ ] 当前P6 ready后和最近20日Web读取不触发SQLite、文件、网络、评分或DeepSeek；
  更老归档首次读取后同日期三策略切换只读内存。
- [ ] 100次普通local/hybrid发布只更新P6和SSE，完整推荐SQLite/JSON写入严格为0。
- [ ] 每个正常交易日最多生成3个冻结检查点、3个正式冻结和3个15:00 closing overlay；
  long检查点、冻结和推荐历史写入均为0。
- [ ] 合格检查点可在冻结边界后崩溃恢复；跨日、过旧、迟到、损坏或consumed检查点拒绝。
- [ ] 历史日期在today/tomorrow/d25间切换保持不变，long按当前日期限定正确禁用和恢复。
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
