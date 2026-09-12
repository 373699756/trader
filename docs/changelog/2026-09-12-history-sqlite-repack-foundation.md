# History SQLite repack foundation

## User request

按 `docs/sqlite.md` 开始实施 SQLite 压实与训练优化计划。本批先闭合计划第 3、4 节的 8 KiB 可续传构建、
可断电切换/回退/收尾基础，以及直接覆盖该边界的诊断和训练内存证据；真实 100 月转换只允许在代码提交推送后运行。

## Evidence and cause

- 只读诊断确认当前 100 个生产月库总计 `24,956,952,576` 字节、`6,093,006` 页、仅 2 个 freelist 页，页大小
  全部为 4 KiB；问题不是普通空闲页碎片，而是小页下的 overflow 放大。
- 三个月 `dbstat` 样本耗时约 68 秒，因此统一诊断默认只抽活动月 1 个样本，同时仍快速读取全部月库的文件大小、
  page count、freelist 和 page size；完整验收可显式将样本数提高到 100。
- `Regression-Key: history-month-overflow-page-amplification`。

## Added

- 有类型的压实 build 状态、切换日志、训练内存证据及严格 hash codec。
- `HistoryArchiveRepackCoordinator` 的 `build`、`activate`、`rollback`、`finalize`，以及薄运维入口
  `scripts/repack_baostock_history.py`。
- `history-archive` 只读诊断 profile，报告全归档物理页摘要和有界 `dbstat` 样本。
- kill/断电故障注入、跨命令恢复、回退、维护 fence、8 KiB 初始化和安全删除回归测试。

## Changed

- 新建空月库在建表前固定 `PRAGMA page_size=8192`；既有月库不会被初始化逻辑原地改写。
- 下载和 Tomorrow 训练取得维护锁后还必须确认不存在未完成压实切换日志。
- 普通训练在切换 fence 期间保持阻塞；训练内存门必须显式携带目标 snapshot hash，并在维护锁内重新打开活动归档
  确认一致后才可执行新库验收训练，避免 activate 与 finalize 形成死锁或训练错库。
- 训练内存门在首次工程训练后立即重复一次并要求 `already_current`，将带内容 hash 的结果原子写入
  `data/historyless/training-memory-result.json`。
- 活动训练 bundle 的进程内类型同时携带已校验的 model、report 和 training-input document hash；公开
  `active-bundle.json` 仍只保留原有四个必要 hash，没有新增 schema/version 字段或 generation 目录。

## Fixed

- 分片文件或目标控制库已经 fsync/rename、但 build 状态尚未提交时，重试会识别并安全清理对应未登记输出。
- activate 每一步 rename 后提交持久化状态；下次命令可完成新归档验证或恢复完整旧归档，不依赖异常栈回退。
- finalize 在递归删除前严格拒绝符号链接、额外文件、额外目录、旧 snapshot/hash 不匹配或训练证据不配对。

## Removed

- 未引入原地 `VACUUM`、新旧双读、自动训练、降低 SQLite durability、扩大训练内存或 hash/generation 目录。

## Verification

- 定向 unit/contract 测试覆盖压实、断点恢复、切换/回退/finalize、维护 fence、训练内存门和统一诊断。
- 受影响文件 Ruff format/check、目标模块 mypy、架构/专业命名契约、refactor quality 与 `git diff --check`。
- 对真实归档运行只读诊断；结果按预期为 `degraded`，因为生产文件仍是待压实的 4 KiB 布局。
- 全量门禁待 `docs/sqlite.md` 大任务最后一个子任务统一执行；本批按用户要求不运行完整测试。

## Residual Risks

- 尚未运行真实 100 月 build、生产 activate、2 GiB 正式训练或 finalize，旧归档未删除。
- `dbstat` 完整 100 月分析成本较高，只应在最终验收显式启用；默认诊断保持有界。
- 用户未跟踪的 `docs/v1v2.md` 原样保留且不纳入本提交。
