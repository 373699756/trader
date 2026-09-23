# 测试优化计划

## 目标与扫描结论

测试计划按 `train`、`history`、`recommendation` 三个业务工作流推进。`runtime`、`supplier` 是测试的执行特征，不是与业务并列的测试任务；`fast` 和 `full` 是总门禁范围。

当前 `make test` 已转为 `test-fast`，但默认仍收集所有未标记为慢速的目录。此前扫描记录的非 slow 集合约有 1945 个用例，并行 4 个 worker 实测约 4 分 43 秒；慢用例主要集中在历史归档、行情历史缓存和转换脚本。该耗时是旧基线记录，优化后应重新测量，不能视为当前实测。

现有 Makefile 的 `test-history`、`test-runtime`、`test-suppliers` 按慢速类型切分：历史归档是业务范围，runtime 和 suppliers 则横跨推荐业务内部的运行时与行情适配边界。因此这些目标适合做专项补充门禁，不适合作为主要测试规划。当前 `slow_*` 主要由 `tests/conftest.py` 按文件路径加标记，不能表达单个用例的业务 owner。

本轮扫描未发现能仅凭文件名或旧版本字样判定为无用的测试。尤其是 legacy 只读兼容、退役版本边界和迁移期架构契约，删除前必须确认对应生产合同或迁移阶段已结束，并检查是否有其它测试覆盖同一行为。此计划不以减少用例数作为目标。

## 测试任务和门禁

| 任务 | 覆盖范围 | 典型内容 | 门禁 |
| --- | --- | --- | --- |
| `train` | 训练、研究、回放、留出和结果评价 | 样本与点时人口、V2/V3 拟合和工件、研究资格、回放/留出、结果结算；按训练 owner 归类，不把历史数据供应商测试混入训练 | `make test-train`；相关改动再运行相应组件和契约测试 |
| `history` | 历史数据下载、归档、维护与转换 | BaoStock 同步、控制库、月分片、active snapshot、归档读取/重打包、历史转换，以及历史数据边界合同 | `make test-history`；归档、SQLite、转换和供应商时序用例纳入此业务门禁 |
| `recommendation` | 推荐领域、实时输入、评分、运行时、冻结、持久化、HTTP/Web | 候选到排名、V2/V3 模型消费、DeepSeek、行情接入、调度/worker、first-wins 冻结、恢复、API/SSE 和 Web 投影 | `make test-recommendation`；runtime/supplier 子集可单独运行，但仍归属推荐 |
| `fast` | 上述业务的快速本地默认集合，加共享架构/命令契约 | 纯函数、轻量单元、无需真实等待或大规模 SQLite 构造的组件、契约 | `make test` / `make test-fast`；日常反馈门禁 |
| `full` | 全部业务门禁、慢速用例及集成测试 | 完整测试树 | `make test-full`；阶段 12 发布验收使用 |

业务 marker 与耗时 marker 应正交：每个测试有一个主要业务归属（`train`、`history`、`recommendation` 或共享 `crosscut`），可另外带 `slow`、`slow_runtime`、`slow_supplier`、`slow_migration` 等执行特征。业务命令按 owner 选择测试，慢速标签用于从 `fast` 排除或运行专项子集。不要再用单一慢速标签替代业务规划。

共享的 architecture、entrypoint、packaging、跨业务数据边界测试归入 `crosscut`，随默认快测运行；全量门禁仍覆盖所有测试。目录迁移阶段要跟随源码 owner 原子迁移测试，避免用测试目录重命名制造第二套 owner。

## 实施顺序

### 1. 整理测试归属清单

- 为现有测试文件登记主要 owner：`train`、`history`、`recommendation`、`crosscut`。
- 对一个测试文件覆盖多个 owner 的情况，先拆分独立行为或明确主 owner，不按路径猜测归属。
- 标出慢速原因：真实 I/O、固定等待、并发/线程时序、大规模 fixture、重复全树扫描；分别登记，不把所有慢测试笼统归为 runtime。
- 不改测试断言和产品代码；先让每个测试的门禁归属可解释、可复查。

### 2. 建立业务门禁

- 增加 `make test-train`、`make test-history`、`make test-recommendation`，并保留 `make test` 指向快速默认门禁、`make test-full` 覆盖全量。
- 将原 `test-runtime`、`test-suppliers` 调整为 `test-recommendation-runtime`、`test-recommendation-suppliers` 一类可选子集，或仅以 marker 过滤，不将其作为一级任务。
- 默认快测排除需要真实等待、压力级 SQLite/归档构造或外部资源的测试；三个业务门禁应覆盖各自全部必要行为，不因默认快测排除而失去日常可运行入口。
- 更新 Makefile help 和唯一命令契约测试，确保命令选择范围清楚且彼此可组合。

### 3. 清理无效和重复测试

- 先检查同一行为是否被多个契约重复扫描：重点复核 `test_architecture.py`、`test_functional_package_boundaries.py`、`test_recommendation_modular_boundaries.py`、`test_entry_contract.py`、`test_packaging_layout.py` 和命名契约。
- 合并重复的目录遍历/源码解析 fixture；每条依赖边界、命名规则和公开行为只保留一个明确 owner 的权威断言集合。
- 只有在生产路径已删除、合同已退役且没有消费者，或行为已由更靠近边界的测试完整覆盖时，才删除旧测试。Today/V1、旧路径存在性和兼容读取测试按相应迁移阶段处理，不提前清理。
- 每次删除记录被替代的断言/门禁和剩余覆盖位置；禁止为缩短耗时而删掉唯一的业务失败、恢复、冻结或边界验证。

### 4. 消除不必要的等待和重复成本

- 对推荐运行时和行情组件中的固定 `sleep`，优先改为注入时钟、事件屏障或可控调度器；只保留确实需要证明线程/外部 I/O 交互的少量集成用例。
- 保留对重试、deadline、取消、停止顺序、供应商失败恢复和 SQLite 冲突的行为断言，用确定性控制替代墙钟等待。
- 缩减重复的大型归档/行情 fixture；按业务门禁并行运行，避免每个专项门禁都收集整棵测试树。
- 此步骤涉及 Python 测试行为时，只补最小必要测试改造并运行对应 owner 门禁及 Ruff。

### 5. 与业务迁移阶段同步

- `history` 测试随下载业务迁移；归档、BaoStock、转换测试与 download owner 同批迁移。
- `train` 测试随训练核心和研究/评价迁移；生产 server 不得因测试导入而加载离线研究实现。
- `recommendation` 测试随推荐领域、运行时、模型、冻结和只读交付边界迁移；API/Web 测试分别验证公开投影和浏览器行为。
- 旧目录、兼容导出和测试 fixture 在 owner 原子切换时一起删除，避免独立测试迁移造成错误依赖。

## 验收标准

- 每个测试均有业务 owner 或 `crosscut` 归属和清楚的运行特征；`make test-train`、`make test-history`、`make test-recommendation` 均完整覆盖各自 owner，不靠隐式文件路径集合漏测。
- `make test` 只执行快速稳定的本地集合；`make test-full` 覆盖完整测试树，阶段 12 再运行项目要求的完整发布门禁。
- 每个删除项都能指出重复覆盖或已退役合同证据；评分、风险、冻结、失败恢复、数据迁移和公开 schema 不因优化失去唯一回归保护。
- 重新测量各业务门禁和默认门禁耗时，记录用例数、总耗时、最慢用例和未纳入快测的原因；不沿用旧基线数字宣称优化完成。
- 测试 owner 跟随对应源码 owner；不增加第二套测试计划真相源，实施进度仍只登记在 `docs/03_工程实施.md`。

## 当前状态

这是测试优化方案，尚未实施 marker/Makefile 重分组、用例删除或等待替换。当前 `slow_history`、`slow_migration`、`slow_runtime`、`slow_supplier` 仍按路径映射，一级命令仍是 `test-fast/history/runtime/suppliers/full`。执行代码变更时应作为独立批次登记，并保留工作树中已有的蓝图修改。
