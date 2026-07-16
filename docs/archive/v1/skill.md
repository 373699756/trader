本文记录按 python-engineering-guide 扫描后的问题基线和持续整改状态。当前主要剩余风险集中在未托管后台线程、超大应用服务/仓储模块、弱类型契约、远程接口认证和缺少性能证据；实施继续采用可验证的类级职责重构，不以单纯移动代码作为完成标准。

## 2026-07-15 进度复核

下文保留的是最初扫描基线，其中“没有 pyproject.toml、CI、Ruff、mypy”等描述已不再代表当前工作区。后续实施应以本节状态为准。

### 已解决

- 配置覆盖契约：`runtime.json` 已提供显式 `env_overrides` 白名单，按原值类型解析环境变量，并拒绝覆盖冻结生产参数；布尔、整数、元组和非法值已有测试。
- DeepSeek 调度事务：任务成功、失败和异常结果均在事务中提交，租约和完成状态已有测试。
- Python 版本契约：最低版本统一为 Python 3.10，`pyproject.toml`、README 和 CI 使用 3.10-3.14 矩阵。
- 基础工程门禁：已增加 `pyproject.toml`、分层依赖文件、Ruff、mypy、coverage、构建检查和 GitHub Actions；启动脚本使用依赖指纹避免每次联网重装。
- 依赖就绪判定：依赖指纹除校验 Python/平台/声明哈希外，还会验证 `pyproject.toml` 中的项目 distribution 已实际安装，避免删除并重建空 `.venv` 后被旧 `.runtime` marker 误判为可用。
- 默认暴露风险：无认证服务默认拒绝非回环监听，只有显式确认风险后才允许不安全绑定。
- 实时行情类边界：`RealtimeQuoteProvider` 现在独立拥有抓取依赖、状态、快照、单飞刷新线程和停止入口，`MarketDataProvider` 仅保留兼容 facade；不再持有 `owner` 反向调用外层私有方法。
- 推荐缓存类边界：`RecommendationSnapshotService` 和 `RecommendationRefreshService` 改为能力级依赖注入，不再持有完整 `_AppServiceContext`。
- 快照写入类边界：`AsyncSnapshotWriter` 已成为独立线程所有者，显式注入持久化、冻结检查和线程工厂，调度时深拷贝取得 payload 所有权，并负责启动失败恢复、停止、等待和指标；`RuntimeSupervisor` 的瞬时 worker 停止链已纳入该组件。
- 实时调度类边界：`RealtimeMarketScheduler` 不再持有完整 `ApplicationContainer`，只依赖行情刷新、状态查询和缓存失效能力；线程启动失败会原子恢复状态。
- 推荐池行情类边界：`RecommendationQuoteRefreshService` 独立拥有 watched codes、display/candidate 快照、网络串行锁、单飞线程、失败指标和停止等待；`CandidatePipeline` 只保留兼容委托，服务通过抓取、缓存读取和失效能力注入，不持有 `container/owner/context`。
- 历史因子与舆情刷新生命周期：`FactorSentimentRefreshService` 通过历史预热、舆情评分、缓存读写和错误记录能力注入，统一拥有两类单飞线程、停止等待和成功/失败/耗时指标；健康接口可观察其状态，`RuntimeSupervisor` 的瞬时 worker 停止链负责回收。
- 开发服务器重载生命周期：Werkzeug reloader 父进程只持有端口锁，子进程只启动 runtime，避免重复后台任务及子进程误清理/终止父进程。
- 长期观察收益语义：长期综合观察分不再写入 `expected_return_net`/`predicted_net_return`，长期池明确使用 `long_term_composite_score` 排序来源，避免把未校准评分伪装成预测收益。

### 部分解决

- Provider 拆分：实时行情已完成独立类重构，历史行情、新闻/事件、基本面和第三方客户端仍集中在 `MarketDataProvider`，`providers.py` 仍约 1,300 行。
- 应用服务拆分：推荐缓存与快照写入职责已抽出，但 `_AppServiceContext` 仍约 1,760 行，承载推荐、预测、验证、回测、健康检查及大量共享 helper；现有路由 use case 多数仍只是转发 facade。
- 验证仓储拆分：文件内已有 signals、outcomes、experiments、reports 等类，但仍共享一个 3,400 余行模块，部分仓储类本身仍过大，尚未形成稳定的跨模块端口和事务所有权。
- 工程门禁覆盖：新增和已迁移模块使用严格 Ruff/mypy，仓库级 Ruff 正确性规则已启用；全仓严格类型检查和更高覆盖率门槛仍需按模块推进。
- 安装可复现：运行/开发依赖已分层，重复启动可离线且空环境不会被旧 marker 误判，但依赖范围仍较宽，尚无跨平台 lock 或 constraints 文件保证全新环境得到完全相同版本。

### 尚未解决

- 类级垂直拆分：继续从 `_AppServiceContext` 提取真正拥有依赖和行为的推荐、预测、验证、回测、健康用例；禁止只移动方法后通过 `context/owner` 回调原类。
- 错误与类型契约：宽泛异常捕获和 `Dict[str, object]` 仍大量存在，应优先建模 RecommendationPayload、SignalBatch、ExecutionRecord、ProviderHealth、ValidationResult，并在外部边界统一异常分类。
- 远程写接口认证：当前只有默认拒绝非回环监听；显式允许非回环后仍没有应用层认证。对外部署必须增加认证/授权或移除不安全绕过。
- 性能证据：缓存深拷贝、全量大小统计和 SSE 轮询尚未建立基准、容量上限与背压指标，不能仅凭代码直觉优化。
- 文档单一事实源：配置说明已趋于一致，但运行参数和调度说明仍未由机器可读配置自动校验生成。

  ## 扫描结果

  - 123 个 Python 源模块，约 43,408 行业务代码。
  - 51 个测试文件，约 14,137 行测试代码。
  - 共收集 452 项测试；最终工作区快照下全部通过：
      - 快速测试：287
      - 集成测试：46
      - 慢速非集成测试：10
      - 慢速集成测试：109

  - pip check 通过。
  - 未发现模块导入环或可变默认参数。
  - 约 74% 函数具有完整参数及返回值标注，但有 1,480 处 Dict[str, object]，类型契约实际上仍较弱。
  - 没有 pyproject.toml、CI、覆盖率门禁、Ruff、mypy/Pyright 配置。
  - 工作区已有未提交修改；本次没有修改或回退任何文件。


  1. 环境变量配置没有真正生效

  stock_analyzer/config.py:47 直接将 runtime.json 写入全局配置，除了 TUSHARE_TOKEN 外没有通用环境变量覆盖逻辑。

  但 README.md:84 和 run.sh:392 都宣称可以通过 ENABLE_HISTORY_FACTORS、ENABLE_DEEPSEEK_RUNTIME 等环境变量控制功能。

  实际验证中：

  ENABLE_HISTORY_FACTORS=0                 -> 仍为 True
  VALIDATION_AUTO_UPDATE_INTERVAL_SECONDS=123 -> 仍为 600
  ENABLE_DEEPSEEK_RUNTIME=0                -> 仍为 True

  这会让运维人员误以为已经关闭后台任务或昂贵功能。

  建议：建立显式、带类型的环境变量白名单，优先级采用 runtime.json < env；冻结策略配置禁止被环境变量覆盖，并为布尔、整数、列表和非法值增加测试。

  2. DeepSeek 调度结果未提交数据库

  stock_analyzer/deepseek_scheduler.py:117 执行状态更新后没有 commit()。实测任务执行完成后，数据库记录仍停留在：

  running, 无完成时间, 无结果

  建议：使用 with conn: 事务或显式提交，并覆盖成功、失败、超时和租约回收测试。

  3. Python 版本声明不一致

  README 宣称支持 Python 3.9–3.14，但 stock_analyzer/snapshot.py:377 和 stock_analyzer/app_container.py:101 使用了没有延迟求值保护的 X | None 标注，对 Python 3.9 存在导入期兼容风险。

  建议二选一：

  - 保留 Python 3.9：增加 from __future__ import annotations 或改为 Optional；
  - 将最低版本明确提升至 Python 3.10。

  随后用真实 CI 版本矩阵验证，不只做语法检查。

  ### P1：生命周期和架构

  4. 后台线程缺少统一所有权

  stock_analyzer/app.py:17 在应用工厂中启动调度器，而根入口 app.py:227 又在导入时创建应用。实时行情和验证线程存在无限循环、daemon 模式以及缺少统一 stop/join 的问题。

  风险包括：

  - 多次调用应用工厂时重复创建线程；
  - Gunicorn 多 worker 下重复执行任务；
  - 测试退出和优雅停机不稳定；
  - 后台任务所有权难以追踪。

  建议增加唯一的 RuntimeSupervisor，统一管理启动、停止、stop_event、等待和 join。应用工厂默认只组装依赖，不自动启动后台线程。

  5. 核心模块过度集中

  主要热点：

  - stock_analyzer/validation_repository.py：约 3,423 行。
  - stock_analyzer/services/app_services.py：约 2,262 行，_AppServiceContext 约 1,725 行。
  - stock_analyzer/providers.py：约 1,628 行。
  - validation_outcomes.update_outcomes：397 行。
  - validation_metrics.metrics：354 行。
  - strategy_validation_gate_decision：302 行。

  建议目标依赖方向：

  入口/CLI
    └── RuntimeSupervisor
        ├── Web Routes
        ├── Application Use Cases
        ├── Domain Strategies / Validation
        └── Infrastructure
            ├── Market Data
            ├── SQLite
            ├── DeepSeek
            └── Cache

  路由只负责 HTTP 转换；用例层负责流程编排；领域层不依赖 Flask、SQLite 或具体行情提供者；基础设施通过接口注入。

  当前新增的 recommendation_cache.py 和 today_policies.py 已经沿着正确方向拆分，可以继续推进，不必推倒重来。

  6. 异常处理和类型契约偏弱

  仓库约有 266 个宽泛异常捕获，主要集中在 provider、应用服务和外部数据解析。部分属于合理降级，但还有不少路径没有稳定错误码、日志或状态指标。

  建议：

  - 只有外部系统边界允许宽泛捕获；
  - 区分配置错误、数据错误、上游错误、暂时性错误和内部缺陷；
  - 为后台任务记录任务名、耗时、重试次数和最终状态；
  - 优先将关键边界上的 Dict[str, object] 替换为 TypedDict 或 dataclass。

  优先建模：RecommendationPayload、SignalBatch、ExecutionRecord、ProviderHealth、ValidationResult。

  7. 安装启动不可复现

  requirements.txt 混合运行和测试依赖，版本范围较宽；run.sh:336 在正常启动时升级安装工具并重新安装依赖，使启动依赖网络且可能悄悄改变环境。

  建议：

  - 用 pyproject.toml 声明项目和工具；
  - 分离 runtime/dev 依赖；
  - 使用锁文件或 constraints；
  - 仅在 Python 版本或依赖文件哈希变化时安装；
  - 未变化的第二次启动必须可以完全离线。

  ### P2：安全、性能和文档

  - stock_analyzer/routes/validation.py:29 存在多个无认证的写操作接口。默认监听 127.0.0.1 时风险可控；若绑定非回环地址，应强制认证或拒绝启动。
  - SSE 每个客户端维持轮询循环；若未来对外部署，需要连接数限制、背压和事件唤醒。
  - 缓存写入中存在多次深拷贝和全量大小重新计算，建议先基准测试，再决定是否保存单条大小或减少拷贝。
  - 设计文档对“进程内调度还是 cron/systemd”以及配置文件来源存在互相矛盾，应统一由机器可读配置生成运维文档。

  ## 推荐实施路线

  ### 阶段一：1–2 天，修复正确性

  - 修复 DeepSeek 状态事务提交。
  - 实现配置覆盖白名单和类型校验。
  - 确定 Python 最低版本并修复兼容声明。
  - 修正文档和启动脚本中的配置契约。

  验收：452 项测试通过；调度成功和失败状态均可持久化；README 中所有环境变量示例有自动化测试。

  ### 阶段二：2–4 天，建立工程门禁

  - 增加 pyproject.toml。
  - 引入 Ruff、mypy、pytest-cov。
  - 新代码和修改代码先启用严格检查，旧代码使用基线逐步收紧。
  - 完成依赖分层和离线重复启动。

  ### 阶段三：1–2 周，按垂直业务切片拆分

  - 先落地 RuntimeSupervisor。
  - 从 _AppServiceContext 提取推荐、预测、验证、健康检查用例。
  - 提取纯逻辑 QuoteOverlayService。
  - 将验证持久化拆成 signals、outcomes、experiments、reports 等仓储。
  - 将 provider 拆成实时、历史、新闻、基本面适配器。
  - 保留原有 facade 作为兼容层，逐个迁移路由。

  每个拆分提交都应保持 HTTP JSON、SQLite schema 和策略版本不变。
  - 统一异常分类、结构化日志和任务指标。
  - 增加超时、重试、幂等和背压约束。
  - 非本机监听时启用认证和写接口保护。
  - 基于性能数据优化缓存、SSE 和数据库批处理。

  整体优先级建议是：配置与事务正确性 → 生命周期 → 工程门禁 → 模块拆分 → 安全与性能。这样能用最小迁移风险解决当前最可能造成生产误行为的问题。
