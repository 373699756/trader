# 测试优化计划

## 目标与扫描结论

测试计划按 `train`、`history`、`recommendation` 三个业务工作流推进。`runtime`、`supplier` 是测试的执行特征，不是与业务并列的测试任务；`fast` 和 `full` 是总门禁范围。

阶段 35 优化前实测收集 1853 个用例：fast 1580、slow 273；单进程 fast 用时 2 分 57.9 秒并暴露 6 个失败。优化后总数仍为 1853，其中 fast 1565、slow 288；4 worker fast 实测 1 分 32.5 秒，仅保留 1 条真实产品失败。参数化 owner 映射增加了等价独立 case，同时删除或合并 4 个无效/重复测试，所以不能用总数不变误判为没有清理。纯仓库命名扫描和隔离 pip 安装已退出日常 fast，仍由专项或 full 门禁覆盖。

现有 Makefile 的 `test-history`、`test-runtime`、`test-suppliers` 按慢速类型切分：历史归档是业务范围，runtime 和 suppliers 则横跨推荐业务内部的运行时与行情适配边界。因此这些目标适合做专项补充门禁，不适合作为主要测试规划。当前 `slow_*` 主要由 `tests/conftest.py` 按文件路径加标记，不能表达单个用例的业务 owner。

本轮确认一条测试只读取已删除的 `docs/v1v2.md`，对应 Today/V1 合同已退役且现行 V2/V3 由权威评分文档、profile 和训练契约覆盖，因此删除。另有三条重复架构扫描被并入更强的依赖方向和退役 owner 契约。legacy 只读兼容、冻结、恢复、评分、迁移和公开 schema 仍不得仅凭文件名、失败或耗时删除；此计划不以减少用例总数作为目标。

## 测试任务和门禁

| 任务 | 覆盖范围 | 典型内容 | 门禁 |
| --- | --- | --- | --- |
| `train` | 训练、研究、回放、留出和结果评价 | 样本与点时人口、V2/V3 拟合和工件、研究资格、回放/留出、结果结算；按训练 owner 归类，不把历史数据供应商测试混入训练 | `make test-train`；相关改动再运行相应组件和契约测试 |
| `history` | 历史数据下载、归档、维护与转换 | BaoStock 同步、控制库、月分片、active snapshot、归档读取/重打包、历史转换，以及历史数据边界合同 | `make test-history`；归档、SQLite、转换和供应商时序用例纳入此业务门禁 |
| `recommendation` | 推荐领域、实时输入、评分、运行时、冻结、持久化、HTTP/Web | 候选到排名、V2/V3 模型消费、DeepSeek、行情接入、调度/worker、first-wins 冻结、恢复、API/SSE 和 Web 投影 | `make test-recommendation`；runtime/supplier 子集可单独运行，但仍归属推荐 |
| `fast` | 上述业务的快速本地默认集合，加共享架构/命令契约 | 纯函数、轻量单元、无需真实等待或大规模 SQLite 构造的组件、契约 | `make test` / `make test-fast`；日常反馈门禁 |
| `static` | 跨仓库命名和静态源码卫生 | 全树 AST/文本命名扫描 | `make test-static-contracts`；不进入日常 fast，仍进入 full |
| `full` | 全部业务门禁、慢速用例及集成测试 | 完整测试树 | `make test-full`；阶段 12 发布验收使用 |

业务 marker 与耗时 marker 应正交：每个测试有一个主要业务归属（`train`、`history`、`recommendation` 或共享 `crosscut`），可另外带 `slow`、`slow_runtime`、`slow_supplier`、`slow_migration` 等执行特征。业务命令按 owner 选择测试，慢速标签用于从 `fast` 排除或运行专项子集。不要再用单一慢速标签替代业务规划。

共享的 architecture、entrypoint、packaging、跨业务数据边界测试归入 `crosscut`，随默认快测运行；全量门禁仍覆盖所有测试。目录迁移阶段要跟随源码 owner 原子迁移测试，避免用测试目录重命名制造第二套 owner。

## 实施顺序

### 1. 已完成：整理测试归属和业务门禁

- pytest collection 已为每个测试登记唯一 `train`、`history`、`recommendation` 或 `crosscut` owner；`slow_*` 继续表示正交执行特征。
- 一级 Makefile 门禁已按业务 owner 建立；runtime 与 supplier 仍是 recommendation 专项，不是业务 owner。
- collection hook 由直接伪 item 测试覆盖，不再为六个代表文件重复启动嵌套 pytest。

### 2. 已完成：清理无效、重复和过时契约

- 删除只读取退役 `docs/v1v2.md` 的旧计划契约；现行档位合同继续由 V2/V3 权威契约覆盖。
- 修复四组迁移前源码 owner 路径，不以删除断言掩盖失败。
- 合并 recommendation application 反向依赖和退役目录的重复扫描；退役目录只检查活动 `.py`，不再被残留 `__pycache__` 空目录误报。
- qfq 算法只由 `docs/01_评分逻辑.md` 权威测试持有；工程文档不再被要求复制算法文字。Changelog 契约兼容仓库内已采用的两代记录结构。

### 3. 已完成：消除无意义墙钟等待和日常重型扫描

- BaoStock 限速与交易所重试测试改用注入的虚拟 sleep，仍断言 2 秒限速和 1 秒重试策略，定向墙钟从约 3 秒降至 5 毫秒内。
- 同一测试模块的 Python 源码和 AST 只解析一次；受影响契约切片由 48.7 秒降至 33.9 秒。
- 隔离 pip 安装标记为 slow，由 full 和更强的 `make test-release` 覆盖；全树专业/稳定命名扫描由 `slow_static` 和 `make test-static-contracts` 覆盖。
- intraday 超时回归先用充足预算确定性建立缓存，再只对目标调用施加 10ms 超时，消除 xdist 负载下的预热竞态。

### 4. 下一批：收敛剩余重型 fixture

- 对 4–6 秒的 shadow/holdout/cost-aware 工件测试先确认成本来自 fsync、模型拟合还是重复 fixture，再复用最小不可变样本；不得跳过篡改、幂等和冲突证据。
- `test_reusable_diagnostics_contract.py` 的多个 CLI help 子进程应在保持每个入口可启动证据的前提下合并。
- cadence 全天秒级枚举保留精确计数合同；只有建立等价事件点算法后才能替换，不能直接删除或放宽计数。
- 继续用注入时钟、事件屏障和可控调度器替换剩余固定等待；真正的线程/外部 I/O 集成才保留墙钟。

### 5. 持续规则：与业务迁移阶段同步

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

业务 owner、业务门禁、首轮退役/重复测试清理、固定等待替换、静态扫描分流和实测基线已完成。完整 fast 仍保留一条真实产品失败：14:50 freeze boundary 后 `UnifiedDecisionQueries.current()` 仍暴露 observation draft；该测试是冻结合同的唯一直接回归保护之一，不属于可删除测试，应由独立产品修复批次处理。

后续优化只处理已经剖析的剩余成本，不再按“文件大、运行慢或当前失败”直接删测试。每次继续记录替代覆盖、collection 数量、fast/full 边界和真实墙钟；工作树中用户的蓝图修改继续排除在测试批次之外。
