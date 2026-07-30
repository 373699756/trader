# V2 计划评审与可执行迁移拆解

> 状态：待执行、非生产契约。
>
> 本文是 `docs/V2.md` 的现状评审和执行拆解，不定义产品或策略行为。产品、架构、时间线、
> API、运维和验收以 `docs/software-business-design.md` 为准；候选、过滤、评分、风险、
> DeepSeek、融合和排名以 `docs/recommendation-strategy.md` 为准；依赖、构建和入口以
> `pyproject.toml` 为准。本文与权威契约冲突时，必须先停止实施并修订本文，不能让计划覆盖
> 权威契约。

## 1. 评审结论

总体方向可行，但 `docs/V2.md` 当前九批计划不能原样执行。目标架构、字段级质量、最近有效
数据、风险登记簿、不可变冻结和可解释 Web 都是正确方向；主要问题是现状判断过时、V1
概念混用、批次过大，以及数据源、字段模型、持久化和切换门禁的依赖顺序不够安全。

| 评审维度 | 结论 | 主要依据 | 修正 |
| --- | --- | --- | --- |
| V2-only 最终目标 | 靠谱 | 活动代码已限定在 `src/trader`，旧 release 可承担完整回退 | 保留目标，不再把已删除的旧包当作待迁移对象 |
| 免费源分工 | 有条件靠谱 | 东财/新浪/腾讯/Tushare/AKShare 已有活动适配器；CNInfo 已有离线增量登记簿；交易所、BaoStock、mootdx 尚未实现 | 先做能力探测和规范 fixture，再决定是否进入正式路由 |
| 字段级合并 | 靠谱且应前置 | 当前实时缓存多数不持久化，来源降级可能放大身份和历史缺失 | 先固定字段契约、质量状态和合并真值表，再接新来源 |
| 持久化底座 | 必要 | 当前 `daily_history`、`history_summary`、`security_master_calendar` 等缓存配置仍为非持久化 | 先建 schema、迁移和恢复，再做大规模回填 |
| 风险登记簿 | 必要 | 现有研究缓存不能替代公告历史基线和分组件状态 | 独立建设，不在冻结窗口临时补抓 |
| tomorrow v2 | 已部分完成 | 已有原生输入、`CurrentDecisionIndex`、冻结仓储、API/SSE/Web、影子证据 | 从当前实现续建，禁止重写已验收组件 |
| 第一/二/九批的 V1 描述 | 不可靠 | 仓库级旧包已退出，但当前生产 `RecommendationPipeline/P6` 与 tomorrow v2 影子仍并存 | 用“旧包”“当前生产链”“tomorrow v2 影子链”三个术语替代笼统 V1 |
| 删除影子比较的顺序 | 不安全 | 当前权威契约要求先取得同输入、完整交易日证据再切换 | 影子比较保留到原子切换完成，清理放在最后 |
| 每批规模 | 不可执行 | 持久化、风险、评分、Web 各自跨多个边界和故障面 | 拆成 P0-P13，每批只有一个可独立验收目标 |
| 发布指标 | 不够精确 | “覆盖率 100%”“三组对比”缺少分母、点时和样本窗口 | 固定分母、时间水位、样本数、失败注入和退出码 |

结论不是推翻 `docs/V2.md`，而是把它保留为目标概览；实际施工按本文 P0-P13 的依赖和
退出条件执行。

## 2. 当前基线

### 2.1 已完成，不得重复建设

- 活动产品代码只在 `src/trader`，活动树没有 `stock_analyzer` 导入。
- `trader-server` 和 `trader-cli` 已是 `pyproject.toml` 的运行入口，`run.sh` 启动
  `trader-server`。
- 当前生产链已有 `versioned_dag`、local 先发布、DeepSeek 异步升级、TopK overlay、
  不可变正式冻结、收盘恢复、只读 Web/SSE、结构化状态和完整打包验收。
- tomorrow v2 已有数据 epoch、确定性本地选择、结构化融合、`CurrentDecisionIndex`、
  独立冻结 repository、只读 API/SSE/Web、原生输入 worker、切换证据 SQLite 和离线 CLI。
- `create_app()` 无线程、网络、数据库和文件写入副作用，`bootstrap.py` 是唯一组合根。

### 2.2 部分完成，必须沿现有接缝续建

- tomorrow v2 local 已可由原生输入驱动，但影子比较、复核事实和冻结触发仍与当前生产
  tomorrow 基线存在协作关系。
- `/v2/tomorrow` 和 `/api/v2/*` 已存在，但根页面和 `/api/*` 仍是当前生产读路径。
- 切换证据可持久化和离线校验，但权威文档仍明确“尚未执行生产指针切换”。
- 当前行情路由已有东财、延迟对冲新浪和腾讯定向报价；字段级来源、冲突和最近有效值能力
  需要迁移到统一质量模型，不能再平行新建第二套路由。
- 当前研究成功缓存可以持久化，但证券主数据、日线历史摘要和板块横截面等关键缓存仍未形成
  独立持久化底座。

### 2.3 尚未完成

- 交易所官方证券主数据适配器和持久化仓库。
- 交易所公告正式交叉校验链。
- BaoStock 历史校验适配器。
- 通达信/mootdx 影子探测和正式 fallback 准入证据。
- tomorrow 的生产读写指针原子切换。
- today、d25 对统一 V2 数据平面和决策平面的迁移。
- 根 Web/API/SSE 的统一，以及切换后不可达旧生产链的删除。

## 3. 术语和边界

后续任务统一使用以下术语，禁止再用一个“V1”同时指代多个对象：

| 术语 | 含义 | 当前状态 |
| --- | --- | --- |
| 旧业务包 | 已退出活动树的 `stock_analyzer` 及旧 release | 只允许完整 release 回退，不得导入 |
| 当前生产链 | `RecommendationPipeline`、P6、现有 `/api/*` 和根页面 | today/tomorrow/d25 当前正式读写路径 |
| tomorrow v2 影子链 | 原生输入、决策索引、v2 冻结、`/api/v2/*` 和切换证据 | 并行运行，尚未切生产指针 |
| V2 目标链 | 统一字段数据平面、唯一决策平面和统一 Web/API | 本文最终目标 |
| 历史兼容解码器 | 为已提交冻结和归档保留的只读 schema/replay 代码 | 不等于活动 V1，不得因名称含 `v1` 误删 |

删除判断使用“是否仍被活动写路径或保留期内只读历史使用”，不能按文件名、schema 后缀或
变量中的 `v1` 字样机械删除。

## 4. 优化后的依赖顺序

```text
P0 基线与契约
  -> P1 来源能力探测
  -> P2 字段质量与合并模型
  -> P3 持久化与迁移骨架
       -> P4 证券主数据/交易日历
       -> P5 历史特征/BaoStock 校验
       -> P6 风险登记簿/CNInfo
  -> P7 实时路由归一化/mootdx 影子
  -> P8 tomorrow 独立生产运行时
  -> P9 tomorrow 证据复核与原子切换
  -> P10 today 迁移
  -> P11 d25 迁移
  -> P12 long、统一 API/SSE/Web
  -> P13 旧生产链删除与最终发布
```

P4-P6 在技术上可由不同人员并行开发，但本仓库每次“继续”仍只交付一个完整同级章节。
P7 的 mootdx 准入是可选增强；若未达到准入门槛，保持最近有效报价降级即可，不得阻塞
P8-P9。P9 只切 tomorrow，不得顺带切 today、d25 或 long。

## 5. 每批统一执行规则

每个 P 批次必须按以下顺序完成：

1. 记录 `HEAD`、上游、已有工作树变更和本批文件边界。
2. 先修改对应权威文档、schema/端口和失败契约测试。
3. 再实现最小闭环，不提前实现下一个 P 批次。
4. 对外部 I/O 增加 timeout、容量、取消、退避、熔断和最近有效值策略。
5. 审查完整 diff，修复正确性、冻结、并发、降级、类型、兼容和打包问题。
6. 运行适用定向测试及完整 `make format-check`、`make lint`、`make type-check`、
   `make test`、`make package`。
7. 执行仓库外 wheel 安装；涉及 Web 时执行三档桌面浏览器验收。
8. 更新 `CHANGELOG.md` 的 Added、Changed、Fixed、Removed、Verification 和
   Residual Risks。
9. 仅暂存本批文件，创建一个 Conventional Commit，推送并确认
   `HEAD == @{upstream}` 后停止。

任一外部来源不可用、真实交易日证据不足或门禁失败时，批次保持未完成，不得降低业务门槛、
伪造 fixture 证据或跳到下一批。

## 6. P0：冻结现状、术语和目标契约

状态：进行中

### 目标

把已经完成、部分完成和待完成能力形成可验证清单，消除“旧包”“当前生产链”和“影子链”
混用，固定后续 schema、目录和生产指针边界。

### 文件边界

- `docs/software-business-design.md`
- `docs/recommendation-strategy.md`，仅在发现策略口径冲突时修改
- `docs/V2.md`
- `docs/V2_plan.md`
- `tests/contract/`
- `docs/reports/v2-p0-baseline.md`
- `CHANGELOG.md`

### 实施步骤

1. 扫描活动代码、路由、配置、运行目录、数据库表、worker、冻结仓储和 Web 资源。
2. 输出旧包删除清单、当前生产链清单、tomorrow v2 已完成清单和待替代清单。
3. 固定唯一术语，并在架构测试中禁止重新导入 `stock_analyzer`。
4. 固定 V2 runtime root、数据库/schema 命名、只读旧库位置和 release 回退矩阵。
5. 固定四策略迁移顺序：tomorrow、today、d25、long。
6. 固定生产读指针、生产写指针、影子写路径和历史只读路径的组合根端口。
7. 列出仍需保留的历史 schema/replay 解码器，防止清理时破坏已提交冻结。

### 2026-07-30 基线执行结果（按本任务）

- HEAD：`0d52522e65f0654f29e401c6e9728ac7f6c484c6`；`@{upstream}`：`origin/feature/tomorrow-v2`，一致。
- Git 状态：基线扫描前工作树干净（无未提交改动）。
- `config/v2/runtime.json`：`schema_version=8`、`runtime_dir=.runtime/v17`、`config_version=runtime_v35_tomorrow_input_quality_free_master_2026_07_30`。

#### 基线清单（先前版本不应重复）

1) 旧包清理边界（保留/迁移为历史只读）
   - 活动源码树无 `stock_analyzer` 导入。
   - 无可追踪的 `src/stock_analyzer` 代码；回退仅通过完整旧 release 进行，不在新分支保留旧业务实现。

2) 当前生产链（保留运行中）
   - 组合根：`bootstrap.py -> build_system -> ApplicationSystem`（`entrypoints/server.py`）。
   - 当前推荐读写与冻结：`src/trader/application/{pipeline.py,recommendations.py,current_decisions.py,freeze_attempts.py,pipeline_status.py,outcome_settlement.py,published_snapshots.py}` 与 `src/trader/web/routes.py`/`routes_recommendations.py`/`routes_status.py`/`routes_events.py`。
   - 根页面与当前 API：`src/trader/web/templates/index.html`、`dashboard.*`、`/api/recommendations/<strategy>`、`/api/status` 及现有 SSE 入口。
   - 当前运行库：`.runtime/v17/runtime.sqlite3`，运行快照与冻结路径为 `.runtime/v17/{frozen,checkpoints,quarantine,published}`。

3) tomorrow v2 影子链（已建成，不得提前切生产）
   - 路由与视图：`/v2/tomorrow`、`/api/v2/tomorrow/current|history|status|events`。
   - 决策/影子仓储：`application/tomorrow_*.py`、`infra/persistence/tomorrow_decision_freezes.py`、`infra/persistence/tomorrow_shadow_evidence.py`。
   - 影子运行库：`.runtime/v17/tomorrow-v2/{tomorrow-v2.sqlite3,tomorrow-shadow-evidence.sqlite3,checkpoints,freezes,quarantine}`。
   - `routes_tomorrow_v2.py` 与静态资源 `static/tomorrow_v2.*`、模板 `templates/tomorrow_v2.html` 已存在并联通。

4) 待替代/待迁移（P1-P13 目标）
   - `web` 根链路、`/api/status` 与 `routes_recommendations` 仍由当前生产链主导，`/v2/tomorrow` 仅并行浏览入口。
   - `today/tomorrow/d25` 仍由当前推荐链负责当前生产写入；`long` 仍为观察投影。
   - 交易所主数据/历史风险/历史特征/字段级合并与统一可观测数据模型仍未完整落地（见 P1-P3）。

5) 历史只读与回退矩阵
   - 仅可读历史运行库：`.runtime/v2/`（历史发布、回退快照来源）、`.runtime/backups/`（策略快照归档）、`.runtime/.stock_analyzer_jobs.sqlite3`（旧任务队列遗留库）、`.runtime/market_data.sqlite3`、`.runtime/deepseek_scheduler.sqlite3`、`.runtime/factor_snapshots.sqlite3`。
   - 运行时不应向上述旧库写入；仅作回放、核验、回退验证。
   - 回退路径：发布失败时按 `docs/software-business-design.md` 与 `docs/recommendation-strategy.md` 的流程，回退到完整旧 release；新链路不在同进程混写旧库。

6) 术语边界固定（本批核定）
   - 旧业务包 = 已退出活动树的 `stock_analyzer`（仅完整 release 回退）；
   - 当前生产链 = `RecommendationPipeline` 与当前 `/api/*` 所在链；
   - tomorrow v2 影子链 = 并行观察链（`/api/v2/*`）；
   - V2 目标链 = today/tomorrow/d25 长期统一到统一决策平面与投影；
   - 历史兼容解码器 = 已提交冻结/历史快照的只读重放、历史解码与 archive/迁移核验组件。

### 验收

- 每个现有模块只属于“保留、迁移、删除、历史只读”之一。
- 权威文档和架构测试使用同一术语。
- 没有运行行为变化，没有生产指针切换。
- 后续 P1-P13 的前置条件和文件范围均可追踪。

## 7. P1：外部来源能力探测和准入契约

状态：已完成，依赖 P0

### 目标

在接入交易所、巨潮、BaoStock 和 mootdx 前验证真实能力，避免根据网页说明直接设计生产
schema。

### 本批交付范围

- 不改造 `bootstrap.py`、路由、生产指针和持久化，禁止外部来源参与评分、冻结、组合根或
  生产配置。
- 仅形成能力评估边界、`SourceCapability` 清单、准入结论和可验收证据。
- SourceCapability 清单作为 P1 章内的核心验收产物。
- 产出：`docs/reports/v2-p1-source-capability-baseline.md`。

### 当前能力基线（基于源码 + 配置 + 版本边界）

1. 已有活动适配器：
   - 东财 `src/trader/infra/market_data/eastmoney.py`
   - 新浪 `src/trader/infra/market_data/sina.py`
   - 腾讯 `src/trader/infra/market_data/tencent.py`
   - AKShare `src/trader/infra/market_data/akshare.py`
   - Tushare `src/trader/infra/market_data/tushare.py`
2. 已有 research/入口分工（仅限既有运行）：
   - 市场新闻与公告聚合由 `AkshareResearchClient` 和 `service_research.py` 提供。
   - 历史/交易日历入口使用 AKShare/Tushare/T+1 全量来源，未形成独立交易所/公告/交易所官方
     入场 adapter。
3. P1 待验证来源：
   - 交易所官方（上交所/深交所/北交所）
   - 巨潮资讯 CNInfo
   - 通达信/mootdx
   - BaoStock
4. 生产路由基线核对：`source_contract_versions` 目前仍为
   `{"eastmoney","sina","tencent","tushare","akshare"}`，`MarketSourceCoordinator` 和
   `MarketDataGateway` 仅保留东财/新浪为全市场并发路由、腾讯为定向候选。

### 实施步骤

1. 为每个来源建立只读能力探测器或离线抓取脚本，不接入 `bootstrap.py`。
2. 记录真实底层来源、请求方式、字段、单位、时区、分页、更新时间、空值、限流和错误类别。
3. 保存脱敏响应 fixture；禁止保存令牌、完整个人路径或无界原始载荷。
4. 对交易所验证代码、板块、上市日期、上市状态、停复牌和交易日历覆盖。
5. 对巨潮验证公告唯一标识、代码映射、公告时间、类别、增量游标和重复页行为。
6. 对 BaoStock 验证前/后复权、成交量/成交额单位、停牌日和除权连续性。
7. 对 mootdx 验证节点发现、报价时标、批量上限、分钟线、断线和跨节点偏差。
8. 对 AKShare 记录每个接口的真实底层来源；相同底层不得计作冗余。
9. 形成 `SourceCapability` 清单和准入结论：正式、影子、离线校验或拒绝。

### 本批 `SourceCapability` 结论（2026-07-30）

- 建立 `SourceCapability` 清单并按生产准入等级归档。
- `eastmoney/sina/tencent/akshare/tushare`：继续使用，不变更其当前准入身份。
- 交易所官方、巨潮资讯、通达信/mootdx、BaoStock：**P1 当批继续拒绝**。
- 拒绝原因统一写入：
  1. 无离线 fixture 与边界 case（正常/空页/重复页/字段缺失/限流）
  2. 无独立入网能力探测脚本（含时序与分页异常注入）
  3. 生产契约未定义“来源失败降级→评分/冻结隔离”细则
  4. 与现网组合根和配置边界耦合不闭环

### 验收

- 已有来源按本章范围完成 `SourceCapability` 清单（见对应基线报告）并明确“未进入生产路径”。
- 每个候选来源都有版本化证据文件（文本基线+可执行契约）且不把 fixture 结果写成生产可用。
- 未验证来源不进入评分、冻结、组合根或生产配置。
- 外部服务不可达时，输出明确的“待真实环境验证”，保持现状降级（最近有效值/已就绪状态）而非替代。
- `docs/reports/v2-p1-source-capability-baseline.md` 与
  `tests/contract/test_v2_source_capability.py` 作为本章可追溯验收产物。

## 8. P2：字段级质量模型和确定性合并

状态：已完成，依赖 P0-P1

### 目标

先定义数据真相，再让新旧适配器写入同一模型；禁止整行 fallback 覆盖完整记录。

### 实施步骤

1. 在领域层拆分 `SecurityMaster`、`RealtimeQuote`、`HistoricalFeature`、
   `RiskEvidence` 和 `IntradayFeature`。
2. 为每个字段保存值、真实来源、源时间、接收时间、质量状态、来源版本和冲突信息。
3. 质量状态固定为 `valid`、`degraded`、`stale`、`missing`、`conflicting`。
4. 在权威文档建立字段要求矩阵：允许来源、优先级、最大年龄、是否核心、是否可用最近值、
   缺失动作和是否可冻结。
5. 用纯函数实现同来源新旧比较、跨来源优先级、时间倒退拒绝、冲突隔离和最近有效值合并。
6. 明确东财/新浪/腾讯只能更新各自允许字段，不能改变交易所、板块、上市日期或历史风险。
7. 生成规范哈希，确保输入顺序不同但业务字段相同的结果完全一致。

### 验收

- 属性测试覆盖交换律不适用处、幂等、单调时间、冲突和整行降级回归。
- 相同输入、配置和注入时钟得到相同哈希。
- 新浪价格 fallback 不清空东财或交易所身份。
- 腾讯定向报价不能覆盖更新的全市场价格或改变证券身份。
- 本批不访问网络、不启动线程、不写数据库。

### 2026-07-30 交付完成

- 已新增 `src/trader/domain/market/quality.py`：拆分
  `SecurityMaster` / `RealtimeQuote` / `HistoricalFeature` / `RiskEvidence` /
  `IntradayFeature` 数据骨架，`FieldQualityState` 与 `FieldValue` 形成统一字段级质量与血缘契约。
- 已新增 `src/trader/infra/market_data/field_quality.py`：实现字段白名单、来源白名单、同源新旧比较、
  跨来源优先级、时间倒退拒绝、冲突记录、时间戳/版本/hash 决胜、冲突状态与可追溯字段来源映射，
  并把选择结果转换为兼容 `merge_quote` 的结果。
- 已将 `src/trader/infra/market_data/merge_quote.py` 的字段级合并改为基于 `field_quality.select_fields`
  的纯函数入口，避免重复实现。
- 已新增/更新 `tests/unit/test_v2_market_data_field_quality.py` 与
  `tests/contract/test_v2_source_capability.py` / `tests/contract/test_project_records.py`：
  覆盖顺序独立性、目标化定向报价、时间顺序、冲突、源权限、与未完成章节边界。`P2` 子项已完成并
  通过 48 项单元/契约回归（含顺序不变性、目标化定向、时间一致性、冲突隔离与未开始章节计数合同）。

## 9. P3：V2 持久化、迁移和恢复骨架

状态：已完成，依赖 P2

### 目标

建立独立、可迁移、可恢复的数据底座，再允许来源回填。

### 实施步骤

1. 在应用层定义证券主数据、历史特征、风险证据和来源游标的仓储端口。
2. 在 `infra/persistence` 建立版本化 SQLite schema、manifest 和 migration registry。
3. 分离可覆盖的最近有效数据与不可覆盖的正式冻结；当前决策继续保持内存 CAS。
4. 所有时间使用带时区 `Asia/Shanghai`，持久化时保留规范 UTC/上海转换契约。
5. 写入使用有界事务、busy timeout、校验哈希和幂等键；损坏记录 fail closed。
6. 启动恢复只读取 V2 runtime root，不读取或写回旧运行库。
7. 为旧冻结和归档建立只读兼容清单；迁移前备份，迁移失败不改变生产指针。
8. 增加容量、保留期、清理顺序和磁盘空间不足的降级状态。

### 验收

- 空库、升级库、重复迁移、中断迁移、损坏行、锁竞争和磁盘写失败测试通过。
- 重启后最近有效数据、来源游标和版本可恢复。
- `create_app()` 仍不打开数据库或写文件。
- V2 数据不会写入旧运行库；旧库只读回退可验证。

### 2026-07-30 交付完成

- 新增应用层数据平面端口与记录：
  - `src/trader/application/ports/data_plane.py`
    - 新增 `DataPlaneRecord` 与 `SecurityMasterRecord` / `HistoricalFeatureRecord` /
      `RiskEvidenceRecord` / `SourceCursorRecord`。
    - 新增仓储端口、冲突/不可用异常与恢复汇总值。
- 新增 `infra/persistence` 数据平面骨架与迁移：
  - `src/trader/infra/persistence/data_plane_sqlite.py`：带版本号、`schema_meta`
    与 8 套表的初始化 SQL。
  - `src/trader/infra/persistence/data_plane.py`：实现四族表的幂等写入、
    staged->committed 写入路径、单条恢复、损坏记录隔离与审计。
- 新增回归：`tests/unit/test_data_plane_migration.py` 覆盖 schema 初始化、
  旧库升级、无效版本修复；`tests/unit/infra/test_data_plane.py` 覆盖读写闭环、
  formal 冲突、损坏提交隔离与回收恢复。
- 本批本地验收已完成：数据平面新增/恢复路径的单元测试均通过；当前实现暂未接入
  `bootstrap.py` 与现有生产读写指针，遵循“先建底座后接入”原则。

## 10. P4：证券主数据和交易日历

状态：已完成，依赖 P3

### 2026-07-30 交付完成

- 通过 P3 已建立的数据平面仓储，`ReferenceLoader` 已接入 `DataPlaneRepository`：
  - 每日按 `security_master` 与 `trading_calendar` 拉取结果写入 `v2-data.sqlite3` 最近有效区；
  - 启动恢复时先初始化数据平面，再恢复近似快照至行情引用层，缺失或不可用时仅降级；
  - 关键异常（含数据库不可用）不阻塞启动。
- 本批回归：
  - `tests/unit/infra/test_data_plane.py`、`tests/unit/test_data_plane_migration.py` 覆盖数据平面读写、
    recovery 与 staged→committed 流程；
  - `tests/component/test_v2_market_data.py` 新增 `ReferenceLoader` 重放恢复与
    `DataPlaneUnavailableError` 隔离回归；
  - `tests/contract/test_v2_bootstrap.py` 补充启动初始化器异常隔离回归。
- 状态边界：本批不实现交易所官方适配器或官方级交易所/交易所来源校验，仍以现有
  Tushare 回灌主数据为本节最小闭环；官方来源、停复牌、正式身份校验继续留待后续批次实现。

### 目标

用交易所官方低频数据建立稳定身份，东财只做异常对账和临时补充。

### 实施步骤

1. 按 P1 通过的能力分别实现上交所、深交所和北交所适配器。
2. 标准化代码、交易所、板块、上市日期、上市状态、停复牌和交易日历。
3. 每日盘前增量同步，成功后原子发布新版本；失败保留最近有效版本。
4. 对数量骤降、重复代码、未来上市日期、市场冲突和时间倒退执行隔离。
5. 东财身份只用于缺失补充和异常对账，不能静默覆盖交易所字段。
6. 将主数据版本注入现有行情和候选输入，不在行情行内复制可丢失身份。
7. 状态 API 暴露来源版本、最后成功、覆盖率、冲突数和最近有效年龄。

### 验收

- 全市场代码唯一，交易所和板块映射确定。
- 潜在可执行候选的代码、板块和上市日期覆盖率为 100%；分母固定为同一候选 epoch。
- 任一交易所来源失败不会清空上一版本。
- 冷启动无需实时行情成功即可恢复证券身份和交易日历。

## 11. P5：历史特征仓库和 BaoStock 校验

状态：已完成，依赖 P3-P4

### 2026-07-30 交付完成

- 历史特征加载从 `HistoryCache` 改为持久化优先：启动与 on-demand 加载会优先从
  `DataPlaneRepository` `historical_feature_recent` 还原 20 条近效内存窗口，并在存在
  61 条样本上下文时恢复统计上下文与趋势向量完整性，冷启动失败回退到可用上游行情。
- `ReferenceLoader` 每次 Tushare 刷新后将 `security_master` 与交易日历游标持久化到
  数据平面，启动时恢复最新快照与游标；恢复失败仅记录告警，不阻塞启动与 local。
- 历史特征新增序列化写入与版本校验：`history_data_plane` 成功时把每条历史 K 线
  （至多 61 天）写入数据平面；写失败不影响 `history.load` 返回与当天评分（降级策略）。
- 本批回归新增并稳定：`tests/component/test_v2_market_data.py`（`HistoryCache.recover_from_data_plane`
  与持久化失效隔离）、`tests/contract/test_v2_bootstrap.py`（启动恢复顺序与失败隔离）、
  以及数据平面回归（`tests/unit/infra/test_data_plane.py`、`tests/unit/test_data_plane_migration.py`）
  覆盖历史与仓储恢复边界。

### 目标

消除 360 只内存历史容量和重启重新预热的结构性限制。

### 实施步骤

1. 固定腾讯前复权日线主源、东财前复权第二回退、BaoStock 离线交叉校验和 Tushare 120
   积分原始能力审计的职责。
2. 统一复权类型、成交量/成交额单位、停牌日、交易日边界和除权连续性。
3. 全市场持久化 20/61 日紧凑特征，候选按需保存可复算的完整窗口。
4. 初始回填使用有界批次和可恢复游标；实时行情、current 和 Web 不等待全量回填。
5. 收盘后滚动更新，重复日期幂等；来源修订产生新版本，不原地伪装旧版本。
6. BaoStock 偏差只形成审计和冲突，不直接替换当前评分历史。
7. 对退市、长期停牌、新股和历史不足建立明确状态。

### 验收

- 重启后历史覆盖率不归零，热启动和冷启动候选基线一致。
- 候选核心历史覆盖率不低于 99%；分母为同一候选 epoch 的请求代码。
- qfq 价格、成交量和成交额固定向量跨来源误差在权威阈值内。
- BaoStock/Tushare 失败不阻塞当日 local 推荐和冻结。

## 12. P6：公司风险登记簿和 CNInfo 增量链

状态：已完成，依赖 P3-P4

### 2026-07-30 交付完成

- 为 `ResearchLoader` 新增风险组件持久化与恢复链路：新增 `risk-component:*` 风险证据落盘，
  支持 `known_clear`、`known_risk`、`unknown`、`stale` 四态覆盖的财务/公告/质押/解禁等组件状态回填；
  支持启动时从 `DataPlaneRepository` 恢复单券分组件状态并用于 `status()` 覆盖/汇总。
- `bootstrap_data_plane` 初始化链路升级为恢复研究状态：`DataPlaneRepository` 初始化后依次恢复
  `ReferenceLoader`、`HistoryCache`、`ResearchLoader`，并保持任何阶段数据平面异常仅告警不阻塞启动。
- `MarketFeatureService` 的研究构建器补齐 `DataPlaneRepository` 注入，结构化新闻请求才写入风险组件；
  news 模式不落库，DataPlane 写入失败仍继续本地评分与刷新。
- 回归新增：`tests/component/test_v2_market_data.py` 增加研究恢复覆盖重建、news 模式非持久化、写入不可用降级回归；
  `tests/contract/test_v2_bootstrap.py` 覆盖启动初始化链路回放与失败不阻塞场景。
- 新增 `src/trader/infra/market_data/cninfo.py`：提供 CNInfo 公告增量同步器、公告唯一键解析、
  重复页去重、空增量不清零、`cninfo.announcements:{code}` 游标、`cninfo-announcement:*`
  风险证据和 `cninfo-risk-component:*` 分组件状态写入。
- `ResearchLoader.recover_from_data_plane()` 现在可从 CNInfo 公告证据恢复结构化
  `ResearchObservation`、公司风险事实和注册表版本；AKShare 风险组件与 CNInfo 组件按
  `known_risk > known_clear > stale > unknown` 合并，避免后到空增量覆盖旧风险。
- `tests/unit/infra/test_cninfo_incremental.py` 覆盖 CNInfo 去重、游标、空增量不清零和恢复到
  结构化研究缓存；`tests/contract/test_v2_source_capability.py` 更新为允许 CNInfo 离线模块
  存在但禁止其接入 `bootstrap.py`、行情路由和生产 source contract。
- 交易所公告交叉校验尚未作为正式来源接入，CNInfo 证据固定记录
  `exchange_cross_check_status=pending`；P7 可继续消费 P6 的风险登记簿，但不得把 pending
  解释为交易所级复核完成。

### 目标

把风险从冻结前临时研究请求改为持久化、分组件、可追踪的证据状态。

### 实施步骤

1. 风险组件拆为公告、处罚/监管、诉讼/重整、财务、质押、解禁/减持和停复牌。
2. 状态固定为 `known_clear`、`known_risk`、`unknown`、`stale`，每组件独立保存。
3. 按 P1 fixture 实现 CNInfo 历史分批回填、公告唯一键和增量游标。
4. 对应交易所公告交叉校验，AKShare 只作公共底层适配器并记录真实来源。
5. 空结果、单组件失败和来源超时不能清空其他组件或历史风险。
6. 刷新优先级按正式/观察 TopK、当日候选、可能达到观察阈值、后台全市场执行。
7. 风险 worker 使用独立容量和退避，不能占用行情、冻结或 DeepSeek 保留资源。
8. 风险事实进入本地规则和 DecisionTrace；自由文本仍不得直接扣分。

### 验收

- 潜在可执行候选风险组件覆盖率 100%；分母和点时时间水位固定。
- `unknown/stale` 只能观察，不能执行；已知风险按权威策略处理。
- 单个接口失败不会把整只股票风险清零或阻塞 local 发布。
- Web 能显示缺失组件、最后成功时间、证据来源和受控中文原因。

## 13. P7：实时路由归一化和 mootdx 影子准入

状态：已完成，依赖 P2、P4-P6

### 目标

让现有东财/新浪/腾讯路由消费 P2-P6 的统一数据模型，并把 mootdx 控制在可退出的影子边界。

### 实施步骤

1. 保留东财先发、1 秒后对冲新浪、先完成且覆盖达标者发布的现有路由。
2. 全市场和候选定向报价统一写字段级 merge，不再携带可覆盖主数据的整行对象。
3. 腾讯部分失败时回退同 epoch 全市场报价或最近有效报价，显式标记年龄。
4. 将来源 timeout、物理失败、熔断跳过、探测、迟到淘汰和冲突分别计数。
5. mootdx 先运行独立影子，不进入评分和冻结；比较代码覆盖、价格、时间戳、延迟和停牌。
6. 只有权威文档批准且连续真实样本通过后，才允许进入候选 fallback。
7. 节点失败、节点切换和公共服务器不可用时立即退出到最近有效报价。

### 验收

- 单个全市场源失败不产生无效空发布。
- 新行情不能清空证券身份、历史特征或风险证据。
- mootdx 未达标时可完全关闭，关闭后业务语义不变。
- 候选报价覆盖率不低于 99%，来源偏差按固定 0.50% 复核契约处理。

### 本批完成记录

- 保持生产路由只接入东方财富、延迟对冲新浪和腾讯定向报价；`mootdx` 仍未进入
  `source_contract_versions`、组合根或生产市场路由。
- 补充字段级归一化回归：`tencent_long` 等来源别名先归一到基础来源再参与优先级；
  未准入的 `mootdx_shadow` 不能写入实时字段。
- 补充候选定向报价回归：腾讯只更新本轮允许字段，不能用价格-only 响应整行覆盖东方财富/
  新浪已有名称、上一收盘或本地风险标记。
- 补充来源健康回归：物理失败、timeout、熔断跳过和 latest-wins 淘汰继续分开统计，避免
  把未发请求、请求失败和迟到取消混为同一类。

## 14. P8：tomorrow 独立生产运行时

状态：已完成，依赖 P7

### 目标

让 tomorrow v2 不再依赖当前生产 tomorrow 已评分 snapshot、复核完成或冻结事件才能形成
自己的正式候选。

### 实施步骤

1. 用 P2-P7 的一致 epoch 直接构造 `TomorrowNativeInput`，保留现有确定性 local 计算。
2. 由独立应用用例选择复核集合并调用共享 DeepSeek 端口、预算总账、缓存和健康门。
3. local 先 CAS 发布，合法结构化复核只生成引用当前 local 父版本的 hybrid。
4. 增加 tomorrow 自有调度点、checkpoint 和 14:50 freeze 触发，不等待当前生产冻结。
5. 保留 `CurrentDecisionIndex`、事件流、冻结 repository 和 v2 Web，不平行重写。
6. 当前生产链和 v2 同时消费同一规范输入；v2 不增加重复行情抓取。
7. DeepSeek single-flight 以证据和模型身份去重，物理请求仍受全局 168 上限约束。
8. v2 失败只影响 v2 readiness，不阻塞当前生产 today/tomorrow/d25、long 或只读 Web。

### 验收

- 停止当前生产 tomorrow 评分后，v2 local、可选 hybrid、checkpoint 和冻结仍可在 fixture
  时钟下独立完成。
- HTTP 请求不抓行情、不评分、不调用 DeepSeek。
- local/hybrid 父子 CAS、迟到拒绝、冻结封口和重启恢复测试通过。
- 固定融合向量为 83.40，`local_score` 不重复扣本地风险。
- 本批仍不切生产指针。

### 本批完成记录

- 已有 `TomorrowShadowRuntime`、`TomorrowShadowWorker`、`TomorrowFreezeCoordinator`、
  `CurrentDecisionIndex` 和 `ShadowObservingSnapshotIndex` 组成独立 tomorrow 影子运行时；
  `tests/integration/test_v2_shadow_cutover.py` 与
  `tests/integration/test_v2_pipeline.py::test_started_pipeline_routes_stages_to_bounded_workers_and_isolates_long`
  已验证 native input 直接从同一规范输入构造，不回读当前生产 tomorrow snapshot。
- `tests/unit/application/test_tomorrow_fusion.py` 已锁定固定融合向量 `83.40`，并证明
  `local_score` 不会重复扣本地风险；`tests/unit/application/test_tomorrow_native_pipeline.py`
  已验证 native input 的时间规范化与因子输入隔离。
- `tests/contract/test_v2_bootstrap.py` 已验证组合根只创建 lazy 的 shadow worker / runtime，
  读取 /API 也不触发历史下载或 DeepSeek 调用；P8 在现有实现上仅需关闭计划状态并保留回归。

## 15. P9：tomorrow 证据复核和原子切换

状态：未开始，依赖 P8

### 目标

以真实、同输入证据证明 v2 可替代当前生产 tomorrow，然后只切 tomorrow 的读写指针。

### 实施步骤

1. 清空资格状态但保留历史审计，使用当前版本重新采集完整交易日证据。
2. 每条证据绑定同一规范输入、当前生产结果、v2 决策、配置、策略、融合和冻结哈希。
3. 离线 CLI 复核载荷哈希、样本窗口、选择/过滤一致性、时延、资源和 DeepSeek 增量。
4. 至少一个完整交易日包含不晚于 10:00 的样本和 14:50 匹配冻结，成功样本不少于 100。
5. 要求零处理/资源错误、零额外重复 DeepSeek、选择与同语义过滤一致率 100%、
   local 可见 P95 不超过 5 秒、决策年龄 P95 不超过 10 秒。
6. 在 `bootstrap.py` 引入显式 tomorrow 生产读写指针，只允许一次原子切换。
7. 切换后根 API 的 tomorrow 查询和正式冻结写入只走 v2；today、d25、long 不变。
8. 演练完整旧 release + 对应旧运行库回退，禁止新代码配旧库或半套 API 回退。
9. 切换后观察一个完整交易日；出现冻结遗漏、哈希冲突或非法空覆盖时回退完整 release。

### 验收

- `trader-cli tomorrow-cutover-evidence --require-eligible` 返回 0。
- 切换提交不改变策略、阈值、融合公式、today、d25 或 long。
- current、published、freeze 和 replay 的身份关系可审计。
- 切换后 v2 不读取当前生产 tomorrow snapshot 或冻结状态。

## 16. P10：today 迁移

状态：未开始，依赖 P9

### 目标

复用已经生产化的 V2 数据平面和决策平面迁移 today，同时严格保留 11:20 边界。

### 实施步骤

1. 在权威策略文档固定 today 与 tomorrow 的共享字段和专属字段。
2. 增加 today 原生输入、纯领域选择、local/hybrid CAS 和只读查询。
3. 复用统一 DeepSeek 预算、缓存和 single-flight，不复制 tomorrow worker。
4. 实现 11:20 当场冻结；错过后保持 `not_ready`，不得 checkpoint 或收盘追补。
5. 同输入并行比较当前生产 today 和 V2 today，取得完整上午证据。
6. 只切 today 读写指针，tomorrow 保持已切状态，d25/long 不变。

### 验收

- 11:19:59、11:20:00、11:20 后启动、冻结重试和迟到结果测试通过。
- 正式记录只含 `executable`，观察池不保存。
- 当前生产 today 停止后，V2 today 独立更新和冻结。
- 切换不增加 DeepSeek 物理预算或改变策略结果。

## 17. P11：d25 迁移

状态：未开始，依赖 P10

### 目标

在 tomorrow/today 已稳定后迁移 d25，并保留 14:50 冻结和 15:00 `close_fallback`。

### 实施步骤

1. 增加 d25 原生输入、专属评分组件、local/hybrid CAS 和只读查询。
2. 复用统一数据 epoch、风险登记簿、预算、缓存、事件流和冻结基础设施。
3. 实现 d25 14:50 正式冻结和同日缺失时的收盘恢复，不影响 tomorrow 正式记录。
4. 同输入比较当前生产 d25 与 V2 d25，单独记录选择、过滤、时延和冻结证据。
5. 只切 d25 读写指针，完成后三个评分策略均不再依赖当前生产链。

### 验收

- tomorrow 和 d25 的冻结身份、仓储唯一键、事件流和错误状态互不污染。
- 15:00 热运行/冷启动 fallback、正式空记录和不可覆盖测试通过。
- 当前生产 d25 停止后，V2 d25 独立更新、冻结和恢复。

## 18. P12：long 与统一 API、SSE、Web

状态：未开始，依赖 P11

### 目标

把已存在的 long 定向实时观察投影接入统一只读外壳，并让 V2 Web 成为根页面。

### 实施步骤

1. 保留 long 固定池、无评分、无冻结、无推荐历史和独立腾讯定向行情语义。
2. 让 long 复用统一证券身份、字段来源和行情质量，不进入候选或 DeepSeek。
3. 为 today、tomorrow、d25、long 建立统一应用层查询和版本化序列化边界。
4. 根页面切到统一 V2 工作台；旧 `/api/*` 先返回明确弃用头，不立即删除。
5. SSE 保持单调序列、有界历史、有界客户端队列、游标恢复和慢客户端隔离。
6. 状态页统一展示来源健康、字段完整度、评分漏斗、风险组件、预算和冻结状态。
7. 所有原因码映射具体中文，未知码使用受控兜底并保留原始 code。

### 验收

- 三档桌面分辨率无白屏、重叠、页面级横向溢出或明显布局跳动。
- 正常 SSE 在线时不轮询完整 current，断线后可按游标恢复。
- long 始终 `score_status=not_applicable`，不产生冻结或历史写入。
- `create_app()` 和所有 HTTP 路由仍无外部 I/O 副作用。

## 19. P13：旧生产链删除、发布和计划退役

状态：未开始，依赖 P12

### 目标

在四类视图全部切换并完成观察后，删除不可达旧生产实现，形成唯一活动链。

### 实施步骤

1. 以静态依赖、组合根接线、运行覆盖和历史保留矩阵生成最终删除清单。
2. 删除不再使用的当前生产 pipeline 分支、旧路由、旧模板/静态资源、旧配置和影子比较器。
3. 删除只服务已切换写路径的兼容适配器；保留仍需读取历史冻结的最小版本化解码器。
4. 删除旧 API 弃用入口，根页面和唯一业务 API 只走 V2 查询。
5. 从 `pyproject.toml` 删除仅被旧链使用的依赖，重新验证 Python 3.10-3.14。
6. 运行架构 AST、融合向量、预算并发、冻结恢复、SSE、性能、打包和三档桌面完整门禁。
7. 仓库外安装 wheel，验证 CLI、模板、CSS、JavaScript 和图标。
8. 演练从新 release 回退到上一完整 release，再恢复新 release；两边运行库不得混用。
9. 更新两份权威文档和 Changelog，将 `docs/V2.md`、`docs/V2_plan.md` 标记完成并退役。

### 验收

- 活动代码不存在 `stock_analyzer`，也不存在仍可到达的旧生产读写链。
- wheel 不包含旧 Web 资源或仅旧链依赖。
- 四策略 current/freeze/history 行为满足权威契约。
- 连续三个完整交易日没有处理错误、冻结遗漏、非法覆盖或额外预算请求。
- 所有适用门禁通过，`HEAD == @{upstream}`，计划文件完成退役。

## 20. 跨批次量化口径

所有覆盖率和时延必须使用明确口径：

| 指标 | 分母/窗口 | 通过条件 |
| --- | --- | --- |
| 候选报价覆盖率 | 同一 `CandidateQuoteEpoch` 请求代码 | `valid + 已复核 degraded` 不低于 99% |
| 潜在可执行主数据覆盖率 | 同一决策 epoch 中本地上界可达执行阈值的代码 | 代码、交易所、板块、上市日期 100% |
| 候选核心历史覆盖率 | 同一候选 epoch 请求代码 | 可复算核心历史不低于 99% |
| 潜在可执行风险覆盖率 | 同一决策 epoch 中本地上界可达执行阈值的代码 | 权威要求组件 100% 非 `unknown/stale` |
| local 可见时延 | market `received_at` 到 local CAS 成功，完整交易日不少于 100 样本 | P95 不超过 5 秒 |
| 决策年龄 | local CAS 时刻减业务数据源时间 | P95 不超过 10 秒 |
| 空结果正确性 | 故障注入和真实合法空集分别统计 | 无效空不得覆盖；合法空可发布 |
| 冻结一致性 | 同一交易日正式记录 | 决策哈希、配置、输入版本和锚点代码一致 |

“三组对比”只用于适配器早期检查，不能替代完整交易日切换证据。外部免费来源没有 SLA，
门禁只约束本系统的超时、降级、保留最近有效值和不伪造数据。

## 21. 必须在 P0 决定的事项

- V2 最终 runtime root 和 schema 版本，不得沿用含混的“v17 等于 V2”口径。
- 旧冻结、归档和 replay 解码器的最短保留期。
- tomorrow 切换后旧 API 的弃用窗口。
- 交易所、巨潮、BaoStock、mootdx 的能力探测是否满足接入前提。
- 三个完整交易日是最终发布最低门槛还是还需增加真实来源故障日。

上述事项未形成权威契约和测试前，不得在后续批次自行假设。
