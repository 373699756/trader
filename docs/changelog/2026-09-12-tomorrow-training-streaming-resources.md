# Tomorrow 训练流式读取与资源边界

## User Request

用户反馈 `./run.sh train-tomorrow` 在 2 核 2 GiB 环境运行一夜后桌面终端被系统终止，要求重新 Review
训练链，并按 Linux 单进程方案实施：可用内存提高到 4 GiB、计算线程不超过 3，不引入 supervisor/worker；
同时要求新命名不得使用 `store` 泛称并将规则固化后提交。

## Current Evidence and Cause

- 系统日志表明此前是 `systemd-oomd` 在桌面内存压力下终止终端 scope；旧的 2048 MiB 常量只是验收阈值，
  不是内核硬限制，不能阻止桌面会话被回收。
- 当前 BaoStock 活动归档约 24 GB，含 100 个月分片、2000 个交易日和 5453 只证券。旧训练先按股票再按月份
  读取，约形成 545300 次小型 SQLite 月查询；这会显著放大随机 I/O 与运行时间。
- 旧样本拟合路径把单行业样本先完整解码为 Python 对象，再复制为 NumPy 数组，形成不必要的内存峰值。
- 本批仓库外真实训练尝试已两次完整通过 100/100 分片 hash、`quick_check` 和行数验证；早期实现随后在精确
  预计数阶段耗时过长，最终流式实现的完整 24 GB 转换、模型发布与峰值 RSS 尚未跑到结束，不能记为已验证完成。

## Added

- 新增按月份、日期、代码顺序单遍消费 61 日窗口的训练样本构建器；每只证券只保留有界滚动状态和一个待 T+1
  标签样本，原始样本以 4096 行批量写入一次性 SQLite repository。
- 新增按行业读取预分配 NumPy 矩阵的模型拟合模块，训练、早停和校准矩阵依次持有并及时释放；确定性
  LightGBM 固定使用 3 个线程。
- 新增训练残留 workspace 清理、流式行进度和封存分片行数上界，无需为进度再扫描一次 observation。
- 新增命名规则：项目自有的新模块和 Python 符号不得使用 `store`、`stored` 或 `*Store` 泛称，必须选择
  `Repository`、`Archive`、`Registry`、`Index`、`Cache`、`Snapshot` 或具体业务职责；外部标准字面量与
  不可变历史文本不受影响。

## Changed

- Tomorrow 训练资源策略从 2 个计算线程、2048 MiB 验收阈值调整为最多 3 个计算线程、4096 MiB 峰值 RSS
  目标；CLI 在加载训练模块前统一设置 OpenMP、OpenBLAS、MKL 和 NumExpr 线程数并降低 Linux 进程优先级。
- 历史进度先显示封存分片行数上界，主扫描结束后收口为实际 `n/n`；交互提示明确写作“峰值 RSS 目标”，
  不再暗示常量是操作系统内存硬限制。
- 训练样本边界使用 `SQLiteTomorrowTrainingSampleRepository`、`TomorrowTrainingSample` 和
  `TomorrowTrainingSampleMatrix`，旧 `sample_store.py`、`V3SampleStore` 与 `V3StoredSample` 被命名契约禁止。
- Review 期间同步修复 Linux systemd 用户任务 `WorkingDirectory` 的路径转义，使原生 unit 验证通过；
  该用户任务仍只负责历史维护，不会启动 Tomorrow 训练。

## Fixed

- 消除“5453 只证券 × 100 个月分片”的逐股小查询路径，以及训练开始前额外精确扫描全归档的等待。
- 消除单行业拟合时 Python 样本元组与多份矩阵长期并存的主要内存放大路径。
- Ctrl+C 或异常退出不会发布半成品 bundle；下次运行会删除遗留的一次性样本 workspace，已有活动 bundle 不变。

## Removed

- 删除训练活动链的 `read_training_batch`、`read_training_rows` 和 `HistoryTrainingRowBatch` 逐股读取接口。
- 删除项目自有训练样本边界中的 `store/stored` 路径、类型、参数与局部变量命名，不提供双命名兼容层。

## Verification

- 契约先行：新增命名契约首次运行准确检出旧模块、旧公共类型及样本构建/拟合中的泛化参数与局部变量。
- 118 项定向 unit/contract 回归通过，覆盖 61 日窗口、T+1 标签衔接、日期/代码顺序、批量 SQLite 写入、
  两次确定性模型拟合、3 线程 LightGBM、进度、CLI 资源策略、残留清理和命名规则。
- Review 发现并修复 2 项新增严格参数复杂度诊断，最终严格重构债务保持零；3 个全量测试失败的定向重跑
  共 8 项通过，包括 V3 离线/运行架构隔离、策略文档边界和 systemd 原生校验。
- 最终门禁：`make format-check`、`make lint`、`make type-check`、`make test`、`make package` 和
  `git diff --check` 全部通过。

## Residual Risks

- 代码路径和自动化回归支持训练完成，但本批尚未完成最终流式实现的 24 GB 端到端训练，因此不能宣称新
  `active-bundle.json` 已生成，也不能宣称 4096 MiB 峰值 RSS 已在真实全量数据上通过。
- 4096 MiB 是验收目标而非 Linux 硬内存上限；桌面同时运行其它高内存程序时，`systemd-oomd` 仍可能按系统
  压力策略终止进程。建议盘后关闭浏览器等高内存程序后运行，并保留终端进度日志判断所在阶段。
- 用户已有未跟踪文件 `docs/v1v2.md` 不属于本批，保持原样且不进入提交。
- `Regression-Key: tomorrow-training-progress-resource-safety`。
- `Regression-Key: semantic-responsibility-naming-cleanup`。
