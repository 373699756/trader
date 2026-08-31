# Trader 功能拆包 Codex 执行计划

> 状态：执行中（批次 1 已完成，等待后续批次继续指令）
>
> 性质：非权威、阶段性施工计划
>
> 执行方式：用户每次发送一次“继续”，Codex 只执行下一个完整的 `## 批次`，完成该批全部子项、Review、验证、提交、推送及上游哈希核对后停止。
>
> 权威边界：本计划不得覆盖 `AGENTS.md`、`docs/software-business-design.md`、
> `docs/recommendation-strategy.md` 或 `pyproject.toml`。如计划与这些文件冲突，以权威文件为准，并在当前批次先修订计划或权威契约后再实施。

## 计划目标

在不改变产品行为、评分策略、冻结语义、API schema、运行目录和发行形态的前提下，将现有工程整理为
边界清晰、可独立定位、可按范围测试的功能包，减少后续修改时的跨目录扫描、无关文件加载和上下文消耗。

目标不是拆成多个操作系统进程、多个服务或多个 Python distribution。最终仍保持：

- 单机、单用户、单进程本地 A 股研究看板；
- 单一 `trader-research-dashboard` wheel；
- 唯一组合根 `src/trader/bootstrap.py`；
- 固定依赖方向 `entrypoints/web/infra -> application -> domain`；
- `create_app()` 无线程、无网络、无数据库、无文件写入副作用；
- HTTP 请求只读，不抓行情、不评分、不调用 DeepSeek、不写盘；
- V2-only API、运行目录、持久化 schema 和发布资源保持不变。

## 已确认现状与根因

根因状态：`confirmed`。

当前源码已经具备正确的分层方向，但功能组织不均衡：

- `domain` 已按 `market/recommendation/research/review/outcome` 初步分包；
- `infra` 已有 `market_data/deepseek/persistence/research`，但配置仍由多个 `settings*.py` 平铺；
- `application` 是主要拥挤点，运行时、决策、推荐、冻结、结算等模块大量平铺在根目录；
- `infra/market_data` 已形成大包，但供应商、规范化、历史、参考数据和服务编排仍处于同一级；
- `web` 已与业务计算隔离，但 API、序列化、SSE 和页面资源还可进一步明确边界；
- 离线研究规模很大，已经基本隔离，后续只需收拢散落在 application 根目录的研究模块，不应与生产推荐包混合。

用户原始“五包”方向有价值，但不能平铺为互相自由导入的顶层包：

- 配置属于 `infra`，不是独立业务领域；
- 过滤与评分共享不可变行情、策略、风险、审计和选择对象，应作为 `recommendation` 下的兄弟子包；
- 展示必须通过 application 只读查询边界，不能直接依赖采集、过滤或评分；
- 调度、冻结、持久化、DeepSeek、决策索引和研究链不能遗漏。

因此采用“保持分层 + 层内按能力分包”，不采用“顶层五包平铺”或“多进程/微服务化”。

## 目标架构

```text
src/trader/
├── domain/
│   ├── market/                         # 点时行情、因子和纯市场事实
│   ├── recommendation/
│   │   ├── filtering/                  # 硬过滤、三态结果和过滤审计
│   │   ├── scoring/                    # 板内横截面、候选分和本地分
│   │   ├── risk_fusion/                # 本地下行风险、DeepSeek事实映射和固定融合
│   │   ├── selection/                  # 动作池、稳定排序、TopK和集中度
│   │   ├── identity.py                 # 决策身份与规范载荷
│   │   └── models.py                   # 推荐域共享不可变值对象
│   ├── research/                       # 离线研究纯领域
│   ├── review/                         # 结构化复核领域
│   └── outcome/                        # 结果评价领域
├── application/
│   ├── runtime/                        # 调度、cadence、lane、生命周期和关闭
│   ├── market_data/                    # 数据刷新结果、输入组装和数据平面用例
│   ├── recommendation/                 # 评分选择、质量、融合、投影和冻结用例
│   ├── decisions/                      # 决策索引、查询、事件、SSE源和overlay
│   ├── research/                       # 离线研究用例
│   ├── outcomes/                       # 结果结算用例
│   └── ports/                          # 稳定的应用端口；初期不做无证据深拆
├── infra/
│   ├── settings/                       # 配置模型、解析、凭据和校验
│   ├── market_data/
│   │   ├── providers/                  # 腾讯、东方财富、新浪、AKShare、Tushare、CNInfo、交易所
│   │   ├── normalization/              # 解析、单位、规范化、合并和特征物化
│   │   ├── history/                    # 历史路由、缓存、预热和种子
│   │   ├── references/                 # 交易日历、证券主数据和参考数据
│   │   └── service/                    # gateway、coordinator和服务门面
│   ├── deepseek/                       # DeepSeek HTTP、schema、预算和复核适配器
│   ├── persistence/                    # SQLite、codec、冻结记录和恢复
│   └── research/                       # 离线研究工件适配器
├── web/
│   ├── api/                            # 路由、白名单序列化、SSE和请求校验
│   ├── templates/                      # 桌面页面
│   ├── static/                         # CSS、JavaScript和图标
│   ├── app.py                          # 无副作用应用工厂
│   └── static_assets.py                # 启动时只读release资源快照
├── entrypoints/                        # CLI、server、performance入口
├── resources/                          # wheel内模型资源
├── bootstrap.py                        # 唯一组合根
└── bootstrap_*.py                      # 只含组合根显式调用的窄构建辅助函数
```

面向维护和问题定位时使用五个生产关注域：

| 关注域 | 主要目录 | 不应加载的无关实现 |
| --- | --- | --- |
| 配置与组合 | `infra/settings`、`bootstrap*.py` | Web渲染、离线研究算法 |
| 数据采集 | `domain/market`、`application/market_data`、`infra/market_data` | Web页面、研究工件 |
| 过滤与评分 | `domain/recommendation`、`application/recommendation`、`infra/deepseek` | 供应商解析、Web渲染 |
| 决策运行 | `application/runtime`、`application/decisions`、`infra/persistence` | 离线研究模型实现 |
| 展示 | `web`、只读 decision queries/stream | 行情HTTP、评分、DeepSeek、SQLite写入 |

离线 `research/outcome` 作为隔离的第六维护域存在，不参与普通生产问题的默认上下文。

## 全局不变量

任一批次都必须保持以下行为不变，除非用户另立独立产品/策略变更批次：

1. 固定融合公式为
   `clamp(local_score * 0.68 + deepseek_score * 0.32 - deepseek_risk_penalty, 0, 100)`，
   使用 `ROUND_HALF_UP` 保留两位，固定向量结果为 `83.40`。
2. `local_score` 已包含本地风险，融合时不得再次扣除 `local_risk_penalty`。
3. DeepSeek 自由文本不得直接扣分；每日物理请求全局上限保持 168，失败和重试同样计数。
4. Today 11:20、Tomorrow/D25 14:50 的冻结、迟到拒绝和 15:00 后恢复规则不变。
5. Long 只展示 current，不评分、不冻结、不写推荐历史。
6. 全部业务时间使用 `Asia/Shanghai`，时钟保持显式注入。
7. 供应商或 DeepSeek 失败时保留最近有效结果并显式降级，不用空结果覆盖。
8. 进程内状态保持不可变且有真实类型；JSON 只存在于配置/供应商解析、持久化 codec、事件载荷和 Web/可观测性投影边界。
9. 不新增兼容重导出模块、旧新双路径、服务定位器、隐藏 fallback、重复状态源或跨层字典协议。
10. 不改变公开 API、SSE schema、ETag、配置 schema、策略/引擎/融合版本、SQLite schema、资源路径和 CLI 命令。

## Codex 执行协议

每个批次必须遵守以下固定步骤，批次正文只补充该批特有内容：

1. 完整读取 `AGENTS.md`、仓库级 `trader-delivery` skill、两份权威文档、当前批相关代码、测试和配置。
2. 记录 `HEAD`、当前分支、`@{upstream}`、上游哈希、暂存/未暂存/未跟踪文件。
3. 若上一批未完成 Review、提交、推送或 `HEAD == @{upstream}` 核对，停止本批，先闭合上一批；不得混批。
4. 将本批目标、根因、文件范围、排除项、影响矩阵和完成条件写入实时计划，一次只允许一个 `in_progress`。
5. 先修改架构/行为契约和失败测试，再修改实现。
6. 所有手工文件修改使用 `apply_patch`；移动文件后必须立即更新所有生产、测试、脚本和打包引用。
7. 不保留旧模块作为 re-export shim。批次完成时旧路径必须不存在，所有调用者必须切到唯一新路径。
8. 运行本批定向测试；任何跨边界污染、导入循环或无法证明的影响都升级到完整高风险门禁。
9. Review 相对上一已推送提交的完整 diff，检查越界、死代码、重复实现、TODO、状态双表示和兼容残留；修复后重新 Review。
10. 更新 `CHANGELOG.md` 的 `Unreleased`，记录诉求、原因、实际变化、验证和剩余风险；需要时同步权威文档。
11. 执行 `git diff --check`，确认暂存区只含本批文件。
12. 每批只创建一个 Conventional Commit，立即推送当前跟踪分支，并分别读取 `HEAD` 与 `@{upstream}` 确认一致。
13. 推送完成后停止，等待下一次“继续”，不得自动执行后续批次。

### 公共高风险门禁

除批次 1 的纯文档/契约准备可按实际 diff 判定外，目录切换批次默认按高风险处理：

```bash
make format-check
make lint
make type-check
make test
make package
```

每批还必须运行：

```bash
.venv/bin/python -m pytest -q tests/contract/test_v2_architecture.py
git diff --check
```

若改动触及入口、包资源或模块发现，必须从仓库外安装新 wheel 并验证：

- `import trader`；
- `trader-cli --help`；
- `trader-cli validate-config`；
- `trader-server` 的无副作用创建边界；
- 模板、CSS、JavaScript、SVG 和模型资源可读取；
- `pip check` 通过。

### Token 与排障约束

拆包后的 token 节省依靠范围治理，不依靠少读强制契约：

- 每个目标包增加短小的局部 `AGENTS.md`，只描述所有权、允许依赖、禁止依赖、直接测试和诊断入口；
- 局部 `AGENTS.md` 不复制根规则、业务公式或权威文档内容，只提供导航；
- 每个批次新增或扩展 AST 包边界测试，使 Codex 可以先确认影响范围再加载文件；
- 问题定位优先使用 `rg`、现有定向测试和 `scripts/diagnose_runtime.py` 的精确 profile；
- 不为节省上下文跳过根 `AGENTS.md`、权威契约或实际下游；
- 不创建集中 re-export 大文件，否则修改任何符号仍会迫使加载整个功能域。

---

## 批次 1：冻结目标包契约与迁移清单

状态：`completed`（2026-08-31）

完成记录：已在软件业务设计第 3 节冻结目标包布局和完整迁移台账；新增功能包边界契约，覆盖目标包
声明、旧路径退役、显式迁移所有权、允许依赖方向和循环依赖；未移动生产模块或改变运行行为。验证通过
`tests/contract/test_v2_architecture.py`、`tests/contract/test_authoritative_document_consistency.py` 和
`tests/contract/test_functional_package_boundaries.py`。

### 目标

先建立机器可验证的目标边界和唯一迁移清单，不移动生产模块，不改变运行行为。

### 文件范围

- `docs/software-business-design.md`
- `tests/contract/test_v2_architecture.py`
- 新增 `tests/contract/test_functional_package_boundaries.py`
- `CHANGELOG.md`
- 本计划状态

### 实施步骤

1. 在软件业务设计第 3 节写入目标目录、五个生产关注域、第六研究域和迁移期约束。
2. 契约测试增加目标包存在性、旧路径退役、允许依赖方向和禁止循环导入的断言。
3. 使用显式模块集合登记迁移，不允许用不断扩大的豁免路径暂时放行。
4. 明确 `bootstrap.py` 继续是唯一组合根，`ports` 在没有耦合证据前保持稳定。
5. 为后续批次登记旧路径到新路径的完整表；每个旧路径只属于一个批次。

### 验证

```bash
.venv/bin/python -m pytest -q \
  tests/contract/test_v2_architecture.py \
  tests/contract/test_authoritative_document_consistency.py \
  tests/contract/test_functional_package_boundaries.py
git diff --check
```

### 完成条件

- 目标边界进入权威架构契约；
- 后续每个文件都有唯一目标位置；
- 没有生产文件移动或行为变化；
- 契约测试能在每个迁移批次逐步收紧，不依赖长期豁免。

建议提交：`docs(architecture): define functional package migration boundaries`

---

## 批次 2：配置与组合包

状态：`pending`

### 目标

将平铺的配置适配器收拢到 `trader.infra.settings`，保持配置、凭据、有效哈希和启动行为完全一致。

### 目标移动

| 旧模块 | 新模块 |
| --- | --- |
| `infra/settings_models.py` | `infra/settings/models.py` |
| `infra/settings_parser.py` | `infra/settings/parser.py` |
| `infra/settings_credentials.py` | `infra/settings/credentials.py` |
| `infra/settings_market_policy.py` | `infra/settings/market_policy.py` |
| `infra/settings_factor_validation.py` | `infra/settings/factor_validation.py` |
| `infra/settings_strategy_validation.py` | `infra/settings/strategy_validation.py` |
| `infra/settings_runtime.py` | `infra/settings/runtime.py` |
| `infra/settings.py` | `infra/settings/loading.py` 或职责更准确的窄模块 |

`infra/settings/__init__.py` 只显式导出稳定加载入口和公共类型，不复制实现，不提供旧模块别名。

### 下游检查

- `bootstrap.py`、`bootstrap_policy.py`、`bootstrap_status.py`；
- `entrypoints/server.py`、`entrypoints/cli.py`、`entrypoints/performance.py`；
- `infra/market_data`、DeepSeek 和持久化对配置类型的读取；
- `config/v2/runtime.json`、`strategy.json`、long watchlist 和性能 fixture；
- 配置 SHA-256、V1/V2 profile 覆盖和凭据脱敏。

### 验证

```bash
.venv/bin/python -m pytest -q \
  tests/unit/test_v2_settings.py \
  tests/contract/test_v2_bootstrap.py \
  tests/contract/test_v2_e9_entry_contract.py \
  tests/contract/test_v2_app_factory.py
make format-check
make lint
make type-check
make test
make package
```

### 完成条件

- 所有 `infra/settings*.py` 旧路径删除；
- 配置 schema、默认值、错误信息类别、有效哈希和凭据优先级不变；
- 所有导入只指向唯一新包；
- wheel 外 `validate-config` 通过；
- `create_app()` 副作用契约通过。

建议提交：`refactor(settings): consolidate configuration adapters`

---

## 批次 3：行情供应商与规范化包

状态：`pending`

### 目标

把外部供应商 I/O 与纯规范化/合并职责分开，使供应商故障能限定到 provider，字段问题能限定到 normalization。

### 目标移动

- `infra/market_data/providers/`：`akshare*`、`eastmoney.py`、`sina.py`、`tencent.py`、
  `tushare.py`、`tushare_records.py`、`cninfo.py`、`exchange_security_master.py`；
- `infra/market_data/normalization/`：`normalize.py`、`merge.py`、`merge_quote.py`、
  `columnar.py`、`columnar_merge.py`、`feature_math.py`、`feature_risks.py`、`features.py`、
  `field_quality.py`、供应商解析模块；
- 保留 `domain/market` 为纯值对象和纯函数，不把 requests、SDK、文件或 SQLite 移入领域层。

### 强制边界

- provider 可以依赖 application port、domain 类型和 normalization 的纯入口；
- normalization 不得导入具体 provider、网络客户端、配置 loader 或持久化；
- 单位、复权、时区、来源优先级、跨源偏差、merge epoch 和内容哈希必须保持不变；
- 不借移动文件修改供应商回退顺序或新增数据源。

### 下游检查

- gateway、source coordinator、service facade；
- cache identity、来源 health、状态投影；
- 历史和参考数据调用方；
- scheduler 的刷新结果与 Web `market_data` 状态摘要。

### 验证

```bash
.venv/bin/python -m pytest -q \
  tests/unit/test_v2_market_data_normalize.py \
  tests/unit/test_v2_market_data_merge.py \
  tests/unit/test_v2_market_data_router.py \
  tests/unit/test_v2_market_data_field_quality.py \
  tests/component/test_market_vendors.py \
  tests/component/test_market_gateway.py \
  tests/component/test_market_tushare.py
make format-check
make lint
make type-check
make test
make package
```

### 完成条件

- provider 与 normalization 不再同级混放；
- 来源乱序、单位、复权、冲突和降级测试结果不变；
- 没有旧导入路径或兼容 shim；
- 状态 API 公开字段和 schema 不变。

建议提交：`refactor(market-data): isolate providers and normalization`

---

## 批次 4：行情历史、参考数据与服务编排包

状态：`pending`

### 目标

继续拆分 `infra/market_data` 的历史、参考数据和编排门面，明确线程、缓存和资源所有权。

### 目标移动

- `history/`：`history.py`、`history_seed.py`、`service_history.py`、
  `service_history_warmup.py` 及只属于历史能力的辅助模块；
- `references/`：`calendar.py`、`security_references.py`、官方交易所主数据及参考状态；
- `service/`：`gateway*`、`source_coordinator.py`、`service*.py`、`router.py`、
  `observations.py` 和服务门面；
- `service.py` 改为 `service/facade.py`，避免文件与目录同名；
- `MarketFeatureService` 继续只协调和转发，不取得组件内部状态所有权。

### 强制边界

- HistoryWarmup 独占历史调度和 worker wave；
- reference lane、tushare lane、实时来源池、历史池和 TopK emergency worker 保持隔离；
- deadline 必须继续区分排队、供应商尝试、验证和持久化；
- 历史或主数据失败保留最近有效快照，不清空评分输入；
- 不改变 SQLite 事务次数、缓存身份和供应商调用配额。

### 验证

```bash
.venv/bin/python -m pytest -q \
  tests/unit/infra/test_history_warmup.py \
  tests/unit/infra/test_exchange_security_master.py \
  tests/component/test_market_history.py \
  tests/component/test_market_references.py \
  tests/component/test_market_exchange_references.py \
  tests/component/test_market_service.py \
  tests/component/test_market_lanes.py \
  tests/integration/test_v2_scheduler_runtime.py
make format-check
make lint
make type-check
make test
make package
```

### 运行证据

在可访问运行服务和供应商时，复用：

```bash
.venv/bin/python scripts/diagnose_runtime.py --profile sources --output -
```

如外部网络或供应商不可用，准确记录未验证门禁，不用 fixture 冒充现场验证。

### 完成条件

- 历史、参考数据和服务编排各有唯一所有者；
- scheduler、status 和 Web 下游引用全部更新；
- worker、deadline、缓存和持久化语义不变；
- 旧模块路径全部删除。

建议提交：`refactor(market-data): separate history references and services`

---

## 批次 5：推荐领域的过滤、评分、风险融合与选择包

状态：`pending`

### 目标

在 `domain/recommendation` 内按计算阶段拆包，同时保持一条确定性管道和共享不可变模型。

### 目标移动

- `filtering/`：现有 `filters.py` 中的过滤策略、规则、三态结果和审计构建；
- `scoring/`：`scoring.py`、`scoring_calculations.py` 和板内横截面计算；
- `risk_fusion/`：`downside.py`、`fusion.py`、`scored_fusion.py`；
- `selection/`：`ranking.py`、`scored_selection.py`；
- `models.py` 和决策 identity 保留为推荐域共享基础，不通过 `__init__.py` 聚合整个实现树；
- `strategies/` 继续只负责有类型策略组合，不复制评分公式。

### 强制边界

```text
shared models/identity
        ↓
filtering -> scoring -> risk_fusion -> selection
```

- 后序阶段可以依赖前序输出类型，前序阶段不得反向依赖后序实现；
- 过滤不得导入 application、infra、Web 或配置 loader；
- 评分入口仍唯一为 `score_board_strategy()`；
- 候选、过滤、风险、融合、动作门和 TopK 数值均不得改变；
- 不增加为了包拆分而存在的 DTO、副本、字典转换或兼容包装器。

### 验证

```bash
.venv/bin/python -m pytest -q \
  tests/unit/domain/test_filters.py \
  tests/unit/domain/test_board_scoring.py \
  tests/unit/domain/test_downside.py \
  tests/unit/domain/test_fusion.py \
  tests/unit/domain/test_ranking.py \
  tests/unit/domain/test_risk.py \
  tests/unit/domain/test_risks.py \
  tests/unit/domain/test_tomorrow_fusion.py \
  tests/unit/domain/test_tomorrow_selection.py \
  tests/contract/test_score_plan_contract.py \
  tests/contract/test_recommendation_sections.py
make format-check
make lint
make type-check
make test
make package
make performance-check
```

### 完成条件

- 四个子包边界无循环依赖；
- 固定融合向量仍为 `83.40`；
- 九组权重、50/30%、0.85、动作门槛、Top6、60% 和行业最多 2 不变；
- local risk 只扣一次，DeepSeek 自由文本仍无扣分权限；
- 性能预算无回退；
- 旧模块路径全部删除。

建议提交：`refactor(recommendation): separate filtering scoring and selection`

---

## 批次 6：应用层推荐与决策包

状态：`pending`

### 目标

收拢 application 根目录的推荐用例和决策只读/发布能力，降低修改某一用例时对整个 application 的扫描。

### 目标移动

- `application/recommendation/`：`scored_selection.py`、`scored_quality.py`、
  `scored_deepseek_fusion.py`、`scored_v2_projection.py`、`scored_v2_freezing.py`、
  `today_v2_freezing.py`、`tomorrow_model_scoring.py`、policy 和对应 codec；
- `application/decisions/`：`decision_core.py`、`decision_queries.py`、`decision_stream.py`、
  `decision_events.py`、`decision_drafts.py`、`decision_coverage.py`、`decision_observers.py`、
  `decision_overlay_refresh.py`、`v2_decision_adapters.py`；
- Long 保持独立 recommendation projection 能力，不进入评分和冻结。

### 强制边界

- recommendation 用例只依赖 application ports、domain 和显式注入选项；
- decisions 拥有 UnifiedDecisionIndex、current/history 查询、事件和 overlay CAS；
- Web 只依赖 decisions 的只读查询/stream 接口；
- application 不导入 infra、Flask 或旧路径；
- 不改变 DecisionView、ETag、projection version、正式/观察分池或冻结投影。

### 验证

```bash
.venv/bin/python -m pytest -q \
  tests/unit/application/test_unified_decision_core.py \
  tests/unit/application/test_decision_queries.py \
  tests/unit/application/test_decision_stream.py \
  tests/unit/application/test_decision_observers.py \
  tests/unit/application/test_today_v2_freezing.py \
  tests/unit/application/test_tomorrow_v2_freezing.py \
  tests/unit/application/test_tomorrow_v2_projection.py \
  tests/contract/test_v2_e2_decision_contract.py \
  tests/contract/test_v2_e4_tomorrow_contract.py \
  tests/contract/test_v2_e5_today_contract.py \
  tests/contract/test_v2_e6_d25_contract.py \
  tests/contract/test_v2_e7_long_contract.py
make format-check
make lint
make type-check
make test
make package
```

### 完成条件

- application 根目录不再平铺推荐和 decision 模块；
- Web、bootstrap、runtime、persistence 和测试全部切到唯一新路径；
- GET/SSE 使用同一 coverage 和 projection identity；
- 冻结、current、history、overlay 和合法空集行为不变。

建议提交：`refactor(application): group recommendation and decision use cases`

---

## 批次 7：运行时、调度与生命周期包

状态：`pending`

### 目标

把 application 根目录的调度、lane、生命周期、输入刷新和运行状态收拢到明确的 runtime/market_data 包。

### 目标移动

- `application/runtime/`：`v2_runtime.py`、`runtime.py`、`v2_lifecycle.py`、
  `system_lifecycle.py`、`schedule.py`、`cadence.py`、`workers.py`、`source_lanes.py`、
  `shutdown.py`、`v2_runtime_issues.py`、`latency.py`；
- `application/market_data/`：`v2_input_runtime.py`、cache/use-case 边界及只属于输入组装的模块；
- `bootstrap.py` 保持唯一组合根，只更新显式导入和装配，不向新包转移外部客户端构造权限。

### 强制边界

- 三策略 scoring lane、三策略 hybrid lane 和八类物理数据 lane 保持隔离；
- latest-wins、single-flight、优先级、轮转和不饥饿语义不变；
- queue wait、vendor attempt、validation、persistence、publication、cancellation 和 shutdown deadline 分别保留；
- local 先发布，DeepSeek 后异步升级，迟到 hybrid 仍由 CAS 拒绝；
- 第一次信号共享 30 秒 ShutdownDeadline，第二次信号立即退出；
- 冻结重试复用同一对象，不重新评分。

### 验证

```bash
.venv/bin/python -m pytest -q \
  tests/unit/application/test_cadence.py \
  tests/unit/application/test_schedule.py \
  tests/unit/application/test_workers.py \
  tests/unit/application/test_v2_input_runtime.py \
  tests/unit/application/test_v2_lifecycle.py \
  tests/unit/application/test_runtime.py \
  tests/integration/test_v2_scheduler_runtime.py \
  tests/contract/test_realtime_pipeline_contract.py \
  tests/contract/test_v2_e3_runtime_contract.py \
  tests/contract/test_v2_bootstrap.py \
  tests/contract/test_v2_app_factory.py
make format-check
make lint
make type-check
make test
make package
make performance-check
```

### 运行证据

在可用环境中运行：

```bash
.venv/bin/python scripts/diagnose_runtime.py --profile live --output -
```

并按权威五时段矩阵验证上午热运行、午间冷启动、11:20、14:50、15:00 后及正式记录命中/收盘恢复。

### 完成条件

- application 根目录不再平铺 runtime 模块；
- `bootstrap.py` 仍是唯一组合根且不超过 1200 行；
- 线程、lane、deadline、停止和恢复契约全部通过；
- status、API、SSE 和浏览器能看到一致的运行状态；
- 没有旧模块路径或隐藏构造器。

建议提交：`refactor(runtime): isolate scheduler and lifecycle packages`

---

## 批次 8：Web API 与展示包

状态：`pending`

### 目标

把 HTTP/API/SSE 边界与页面资源明确分开，保持 Web 只读和 release 资源整体换版语义。

### 目标移动

- `web/api/`：`routes_v2.py`、`decision_serializers.py`、`decision_sse.py`、
  `route_services.py` 及请求校验辅助；
- `web/app.py` 继续只创建应用和注册 blueprint；
- `web/templates`、`web/static`、`static_assets.py` 保持打包资源边界；
- 若 `routes.py` 只负责 blueprint 组合，可并入 `web/api/routes.py`；不得保留重复路由注册入口。

### 强制边界

- Web 只能调用 application decisions 的只读用例；
- JSON 必须继续由 serializer/observability adapter 按白名单显式投影；
- HTTP 不访问供应商、DeepSeek、SQLite写入或评分器；
- current/history/dates/status/events URL、schema、ETag 和错误语义不变；
- `create_app()` 仍无副作用；
- 静态资源仍在启动时形成只读 release 快照，HTTP 不重新读取工作树。

### 验证

```bash
.venv/bin/python -m pytest -q \
  tests/contract/test_v2_e8_web_contract.py \
  tests/contract/test_v2_app_factory.py \
  tests/unit/application/test_decision_queries.py \
  tests/unit/application/test_decision_stream.py
node --test tests/js/test_dashboard_state.js
make format-check
make lint
make type-check
make test
make package
```

### 桌面与运行证据

- wheel 外安装后验证模板、CSS、JS、SVG；
- 使用现有浏览器诊断验证 SSE 重连、cursor resync 和 patch-to-paint；
- 1280x720、1440x900、1920x1080 无白屏、重叠、页面级横向溢出或明显跳动；
- 若图形栈阻断，记录精确未完成门禁，不宣称通过。

### 完成条件

- API/SSE 与页面资源职责清晰；
- 浏览器行为、release handshake 和四策略视图不变；
- Web 不新增任何写路径或业务计算；
- 所有旧导入路径删除。

建议提交：`refactor(web): isolate api and presentation adapters`

---

## 批次 9：研究、结算与入口收拢

状态：`pending`

### 目标

收拢仍散落在 application 根目录和 CLI 中的研究/结算职责，使普通生产排障默认不加载约 1.9 万行离线研究实现。

### 目标移动

- `application/outcomes/`：`outcome_settlement.py`；
- `application/research/`：`research_audit.py`、`research_coordination.py`、
  Tomorrow profile comparison/reporting/settlement 等研究专用模块；
- 研究端口放入明确的 research ports 位置，但不为迁移复制公共协议；
- CLI 仅在文件职责和复杂度证据支持时拆为窄 command handler；入口参数、退出码和公开命令不变；
- `domain/research`、`application/research`、`infra/research` 继续保持生产隔离。

### 强制边界

- 离线研究不得注入生产 `bootstrap.py`，除权威契约明确要求的只读后台证据消费者；
- 不启动额外网络、DeepSeek、冻结、Web或活动数据库写入；
- 工件 identity、hash、原子幂等、防篡改和 `production_authority=false` 不变；
- outcome 只结算已冻结证据，不反向改变生产策略；
- HTTP 不读取研究 SQLite。

### 验证

```bash
.venv/bin/python -m pytest -q \
  tests/unit/application/research \
  tests/unit/domain/research \
  tests/unit/infra/research \
  tests/unit/application/test_outcome_settlement.py \
  tests/unit/infra/test_outcomes.py \
  tests/component/test_v2_research_trace_store.py \
  tests/contract/test_score_plan_contract.py \
  tests/contract/test_score_research_detailed_strategy_contract.py \
  tests/contract/test_v2_e9_entry_contract.py
make format-check
make lint
make type-check
make test
make package
```

### 完成条件

- production runtime 的默认依赖图不再导入离线研究实现；
- CLI、工件、研究状态和结算结果不变；
- research/outcome 旧根级模块删除；
- 无生产写权限、自动调权或自动 profile 切换。

建议提交：`refactor(research): consolidate research and outcome boundaries`

---

## 批次 10：最终切换、清理与发布级验收

状态：`pending`

### 目标

删除所有迁移期痕迹，证明新包结构是唯一活动实现，并完成安装、运行、浏览器和架构验收。

### 清理清单

1. 搜索并删除所有旧导入路径、旧模块名、兼容别名、重复 `__all__` 聚合和临时豁免。
2. 检查 `application`、`infra/market_data` 根目录是否只保留真正属于该层根的稳定入口。
3. 检查所有 `__init__.py`：只导出窄公共接口，不执行 I/O，不构造客户端，不形成循环导入。
4. 检查所有单文件不超过 1200 行，复杂度/命名债务保持零或按契约下降。
5. 更新软件业务设计中的最终目录状态；策略文档只在实际策略契约变化时更新。
6. 删除本计划中已失效的迁移清单；若全部批次完成，根据权威文档治理要求删除本文件，并在 Changelog 保留最终事实。

### 全量验证

```bash
make format-check
make lint
make type-check
make test
make package
make performance-check
```

另外必须完成：

- 架构 AST 和 JSON 序列化边界；
- `create_app()` 无副作用；
- 固定融合 `83.40`；
- DeepSeek 168 请求并发原子预算；
- single-flight、latest-wins、来源乱序、停止和资源回收；
- 冻结恢复、first-wins、内容 hash 和冲突拒绝；
- SSE cursor 恢复、慢客户端和浏览器 replacement/patch；
- wheel 外安装、`pip check`、CLI、模板、静态资源、图标和模型资源；
- 三档桌面分辨率；
- 可用环境中的 `scripts/diagnose_runtime.py --profile full --output -`；
- `git diff --check`、暂存区范围、单提交、推送及 `HEAD == @{upstream}`。

### 最终完成条件

- 目标目录全部存在，旧路径全部不存在；
- 没有循环依赖、兼容 shim、双实现、重复状态源或越层导入；
- 五个生产关注域和隔离研究域均能通过窄入口定位及定向测试；
- 所有适用自动门禁通过，现场不可用门禁有精确剩余风险记录；
- 权威文档、代码、测试、wheel 和运行时状态一致；
- 最终提交成功推送且本地/上游哈希一致。

建议提交：`refactor(architecture): complete functional package cutover`

## 计划完成判定

只有批次 1–10 均为 `completed`，且批次 10 的最终完成条件全部满足，才能宣告拆包完成。

以下情况不得视为完成：

- 仅移动了目录或批量替换 import；
- 旧模块仍以 re-export 或 alias 保留；
- 单元测试通过但架构、打包、运行或浏览器门禁未验证；
- 为减少 diff 保留双路径或隐藏 fallback；
- 将过滤、评分、冻结或 Web 行为变化混入结构重构；
- 未推送或未确认 `HEAD == @{upstream}`；
- 用“以后清理”代替明确退出条件。

完成后，拆包效果应表现为：配置问题、供应商问题、过滤/评分问题、运行/冻结问题、Web问题和研究问题
各自拥有稳定入口、窄依赖图、直接测试与诊断路径；后续 Codex 仍遵守全局契约，但不再需要为一个局部问题
扫描整个活动源码树。
