# Codex 执行计划：多源并行采集、三板独立评分与统一合并

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
| 1 | v15多源并行采集与结构化合并 | `[ ]` | 用户下一次发送“继续” |
| 2 | v16三板独立评分与统一合并 | `[ ]` | 任务1完成并再次收到“继续” |

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
- 三板评分结果必须经过单一确定性合并器后才能发布或冻结。
- 同行差与换手冲击采用趋势延续方向，不实现补涨回归分支。
- d25质量价值输入固定为质量50%、价值30%、成长20%。
- v16动作门槛固定为 `tomorrow=78`、`d25=76`，观察带为5分。
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
- `src/trader/application/ports.py`；
- `src/trader/application/pipeline*.py`；
- `src/trader/application/candidate_features.py`；
- `src/trader/bootstrap.py`。

行情与结构化合并：

- 新建 `src/trader/infrastructure/market_data/observations.py`；
- 新建 `src/trader/infrastructure/market_data/merge.py`；
- 新建 `src/trader/infrastructure/market_data/tushare.py`；
- 修改 `gateway.py`、`router.py`、`normalize.py`、`service*.py`、
  `features.py` 和 `calendar.py`。

持久化与只读接口：

- `src/trader/infrastructure/persistence/*.py`；
- `src/trader/web/schemas.py`、`serializers.py` 和静态详情展示；
- `tests/unit`、`tests/component`、`tests/contract`、`tests/integration` 中
  仅与本批行为对应的测试。

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
6. 新旧比较固定使用
   `(source_time, received_at, data_version, source_priority)`。
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
`/api/status` 增加每源计划、成功、失败、超时、熔断、P50/P95延迟、数据年龄、
合并次数、冲突数和最近 `merge_epoch`。

### 5.10 批次一验收矩阵

先运行本批局部回归：

```bash
.venv/bin/python -m pytest -q \
  tests/unit/test_v2_market_data_normalize.py \
  tests/unit/test_v2_market_data_merge.py \
  tests/unit/domain/test_filters.py \
  tests/unit/test_v2_settings.py \
  tests/component/test_v2_market_data.py \
  tests/component/test_v2_persistence.py \
  tests/contract/test_v2_web_api.py \
  tests/contract/test_v2_app_factory.py \
  tests/integration/test_v2_pipeline.py \
  tests/integration/test_v2_final_acceptance.py
```

局部回归通过后才能运行第3.3节完整门禁。

- [ ] 东方财富和新浪能同时进入各自worker。
- [ ] 腾讯慢或失败不阻塞全市场统一快照。
- [ ] Tushare未安装、无Token、429、超时均不阻塞免费链路。
- [ ] 同源并发调用只产生一次物理请求。
- [ ] 队列满时保留最新观察点，不补跑旧周期。
- [ ] 不同线程完成顺序产生相同快照和哈希。
- [ ] 迟到、未来时间、空版本和乱序结果不能覆盖新值。
- [ ] 多源偏差0.50%通过，超过0.50%且未复核只能观察。
- [ ] 上市第1/5日拒绝，第6日恢复。
- [ ] 主板8.00/8.01和两成长板16.00/16.01边界通过。
- [ ] `create_app()`仍无I/O副作用。
- [ ] 旧冻结可读，新冻结可离线校验哈希。
- [ ] v14评分、门槛和TopK输出未改变。

### 5.11 批次一提交和停止

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

- `src/trader/infrastructure/deepseek/reviewer*.py` 和 `schema.py`；
- `src/trader/infrastructure/persistence/*.py`；
- `src/trader/web/schemas.py`、`serializers.py` 和详情展示；
- 对应单元、组件、契约和集成测试。

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

### 6.11 批次二验收矩阵

先运行本批局部回归：

```bash
.venv/bin/python -m pytest -q \
  tests/unit/domain/test_board_scoring.py \
  tests/unit/domain/test_ranking.py \
  tests/unit/application/test_board_scoring.py \
  tests/unit/application/test_recommendations.py \
  tests/unit/test_v2_settings.py \
  tests/component/test_v2_deepseek.py \
  tests/component/test_v2_persistence.py \
  tests/contract/test_v2_web_api.py \
  tests/integration/test_v2_pipeline.py \
  tests/integration/test_v2_final_acceptance.py
```

局部回归通过后才能运行第3.3节完整门禁。

- [ ] 三个评分lane真实并行，且每个lane只有一个worker。
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
- [ ] DeepSeek总预算仍为158/188，且不存在板块重复预算。
- [ ] today、long、冻结时间、SSE和旧快照行为不变。

### 6.12 批次二提交和停止

Review重点：板内总体泄漏、权重漂移、缺失值伪装、DeepSeek预算竞争、跨epoch
混合、失败板块偏置、冻结一致性、旧回放和UI字段兼容。

提交信息固定为：

```text
feat(strategy): add board-specific parallel scoring
```

推送后核对 `HEAD == @{upstream}`，把批次二标为 `[x]` 并停止。不得继续创建
回测、收益校准或下一版策略任务。

## 7. 最终 Definition of Done

只有同时满足以下条件，整个计划才算完成：

- [ ] `docs/need.md` 已成为 v15/v16 最终唯一业务契约。
- [ ] 五个数据源lane可启动、停止、等待、熔断和观测。
- [ ] 单源观测和统一快照可确定性重放。
- [ ] 三个板块lane独立评分且无跨板统计泄漏。
- [ ] DeepSeek保持受控双模型和188次全局上限。
- [ ] 单一合并器负责融合、选择、发布和冻结。
- [ ] 新旧冻结均可按各自版本验证。
- [ ] 所有适用自动门禁和仓库外安装验收通过。
- [ ] 三档桌面验收有实际截图和无重叠证据。
- [ ] 两个批次分别只有一个提交并均已推送。
- [ ] 没有已知未解决Review发现。
- [ ] CHANGELOG记录用户诉求、修改、验证和剩余风险。

即使以上全部完成，也只能表述为数据可靠性、结构化程度和策略针对性已增强；
没有另行定义并通过点时收益验证前，不得声称实际收益已经提高。
