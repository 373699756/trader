• 初步结论：当前 make test 的默认集合过大，已经不是“快速测试”。本次实测：

  - 非 slow 测试收集约 1945 个用例。
  - pytest -n 4 实际耗时 4 分 43 秒。
  - 最慢用例集中在历史归档、市场历史缓存和转换脚本，单个用例约 10–29 秒。
  - 当前基线有一个已知失败：tests/contract/test_document_optimization_contract.py 检查 docs/03_工程实施.md 不应出现 `completed`，但当前台账已经包含该文本。这与测试耗时优化无关，且工作树中已有用户修改，我没有改
    动。

  依据 docs/项目重构详细.md:1730 的阶段计划，建议按以下顺序精简。

  第一阶段：先恢复真正的快速默认测试

  当前阶段仍是阶段 1，建议只处理测试分层和公共测试基础设施，不改变业务行为。

  应从 make test 默认集合移出的文件：

  - 历史归档同步与重打包：
      - tests/unit/infra/research/test_history_archive_sync.py
      - tests/unit/infra/research/test_history_archive_repack.py
      - tests/unit/infra/research/test_history_control_repository.py
      - tests/unit/infra/research/test_history_training_input.py

  - 历史转换和物理 SQLite 验证：
      - tests/unit/scripts/test_convert_baostock_history.py
      - tests/component/test_market_history.py

  - 运行时和调度压力测试：
      - tests/integration/test_scheduler_runtime.py
      - tests/unit/application/test_input_runtime.py
      - tests/unit/application/test_workers.py
      - tests/unit/application/test_supervisor.py

  - 含真实等待或并发时序的市场组件：
      - tests/component/test_market_vendors.py
      - tests/component/test_market_gateway.py
      - tests/component/test_market_service.py
      - tests/component/test_market_references.py
      - tests/component/test_market_exchange_references.py
      - tests/unit/test_market_data_cache.py

  这些测试不是删除，而是统一增加 slow 或更明确的专项标记，例如：

  - slow_history
  - slow_runtime
  - slow_supplier
  - slow_migration

  然后把默认命令收敛为纯内存、纯函数、无真实等待、无大规模 SQLite 构造的测试。

  建议新增专项命令，而不是继续让 make test 隐式执行：

  test-fast
  test-history
  test-runtime
  test-suppliers
  test-full

  其中：

  - test-fast：unit、contract，以及无文件/无等待的轻量 component。
  - test-history：阶段 2 下载和历史归档迁移相关测试。
  - test-runtime：调度、latest-wins、资源车道、停止顺序。
  - test-suppliers：市场供应商、DeepSeek、SQLite 持久化和失败恢复。
  - test-full：发布前完整集合。

  第二阶段：减少真实等待和重复构造

  目前多处测试通过 time.sleep() 验证时序。这会让并行执行仍然产生明显延迟，也容易造成机器负载差异导致的偶发失败。

  重点优化：

  - tests/component/test_market_vendors.py
  - tests/component/test_market_gateway.py
  - tests/component/test_market_service.py
  - tests/component/test_market_references.py
  - tests/component/test_market_history.py
  - tests/unit/test_market_data_cache.py

  处理方式：

  - 用可注入时钟、事件屏障或 fake scheduler 替代固定 sleep。
  - 只保留一个真实线程/并发集成用例，其余改成确定性单线程测试。
  - 把“重试次数”“超时边界”“取消顺序”保留为行为断言，不用真实时间证明。
  - 将供应商失败恢复、SQLite 锁竞争等少数真实 I/O 用例移入专项测试。

  这与蓝图阶段 1 的共享时钟、资源上限和公共 primitive 收敛直接相关，不能通过简单删除断言来提速。

  第三阶段：按蓝图迁移边界重新归属测试

  测试目录目前仍反映旧的技术层结构，而蓝图要求迁移到 download、training、recommendation 三个业务 owner。

  建议按阶段迁移：

  - 阶段 2：将历史下载、BaoStock、控制库、月分片和转换测试迁到 download 测试目录。
  - 阶段 3：将训练样本、模型拟合、V2/V3 工件测试迁到 training。
  - 阶段 4：将回放、留出、研究报告和结果评价测试迁到 training/evaluation。
  - 阶段 5：删除活动 Today/V1 测试：
      - tests/contract/test_today_contract.py
      - tests/unit/application/test_today_freezing.py
      - tests/unit/application/test_today_projection.py
      - tests/unit/infra/scoring/test_v1_profile.py
      - tests/unit/infra/scoring/test_v1_artifact_builder.py
      - 以及其它只验证 Today/V1 活动路径的测试。

  - 阶段 6–8：将推荐端口、流水线、模型加载、DeepSeek、冻结和运行时测试归入 recommendation。
  - 阶段 9：拆分 HTTP API 契约测试和 Web 页面测试，避免页面测试重复验证 API 内部实现。
  - 阶段 11：删除旧 owner 对应的测试夹具、兼容导出测试和旧路径存在性测试。

  测试迁移必须与源码 owner 一起完成，不能先删除测试或放宽契约，再迁移实现。

  第四阶段：压缩契约测试的重复扫描

  当前 contract 测试约 55 个文件，其中部分测试都在重复扫描目录、字符串和旧路径。建议保留行为边界，但减少重复读取：

  重点检查：

  - tests/contract/test_architecture.py
  - tests/contract/test_functional_package_boundaries.py
  - tests/contract/test_recommendation_modular_boundaries.py
  - tests/contract/test_entry_contract.py
  - tests/contract/test_packaging_layout.py
  - tests/contract/test_professional_naming_contract.py

  优化方向：

  - 每个架构边界只保留一个权威测试文件。
  - 将目录扫描结果抽成共享 fixture，避免每个测试重复遍历 src。
  - 将“禁止旧路径”“禁止旧命名”“依赖方向”分成清晰的契约集合。
  - 不删除蓝图明确要求的禁止项，只消除相同断言的重复实现。
  - 阶段 11 完成旧树删除后，再清理大量“旧文件不存在”的过渡断言。

  第五阶段：拆分大文件，而不是仅减少断言

  以下文件职责过多，建议拆成多个测试文件，降低单文件启动、fixture 和定位成本：

  - tests/unit/test_settings.py
      - 配置解析
      - 版本/策略边界
      - 文件权限和环境变量
      - DeepSeek 配置

  - tests/contract/test_entry_contract.py
      - CLI 参数
      - 命令路由
      - 入口副作用
      - 训练/下载命令

  - tests/component/test_deepseek.py
      - 请求预算
      - SQLite 预算账本
      - 失败恢复
      - Flask/应用集成

  - tests/component/test_market_research.py
      - 研究事实
      - 资格注册
      - 历史研究组件

  - tests/component/test_market_references.py
      - 交易所参考
      - 并发刷新
      - 缓存和失效

  拆分后可以按 marker 精确运行，避免修改一个小配置时启动整个大文件。

  建议的实施批次

  1. 当前阶段 1：只建立 marker、fixture 和 Makefile 分层；修复默认测试集合，使 make test 只运行快速集合。
  2. 阶段 2：迁移并优化历史下载/归档测试，全部纳入 test-history。
  3. 阶段 3–4：迁移训练和研究测试，默认只保留纯函数和工件 codec 测试。
  4. 阶段 5：删除 Today/V1 活动测试，更新版本边界契约。
  5. 阶段 6–8：保留推荐核心行为测试，拆出 runtime、supplier、DeepSeek 专项门禁。
  6. 阶段 9–10：拆分 API/Web/入口测试，并验证 wheel 安装测试不进入默认快测。
  7. 阶段 11：删除旧 owner 测试、兼容路径测试和重复架构扫描。
  8. 阶段 12：统一运行 make format-check、make lint、make type-check、make test、make package 和全部专项门禁。

  目标不是盲目减少用例数量，而是让默认测试只证明快速、稳定、纯本地的核心行为；历史归档、真实 SQLite、并发、供应商失败和浏览器性能测试保留为明确的专项门禁。

