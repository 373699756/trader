# SQLite 压实与 Tomorrow 训练优化计划 Review

## User Request

用户要求 Review `docs/sqlite.md` 中的历史数据库压实计划，并在确认 SQLite 读写、文件大小和训练速度仍有优化空间后，
按照 Review 结论修改该计划。

## Current Evidence and Cause

- 当前历史归档为 100 个 SQLite 月分片，实际大小 23.243 GiB；最新有效记录相对旧归档只增加约 0.37%，
  数据增量不是体积增长的主要原因。
- 代表月实测确认约 1 KiB 的 `WITHOUT ROWID` 行在 4 KiB 页上形成大量溢出页；8 KiB 压实分别使两个代表月
  缩小约 47.6% 和 35.6%，16 KiB 反而更大。
- 当前最新 revision 的 `ROW_NUMBER()` 查询会 materialize CTE 和临时 B-tree；代表月 anti-join 探索性实测
  从 0.93 秒降到 0.80 秒、峰值 RSS 从 23,552 KiB 降到 12,544 KiB，但尚未形成多工作负载验收证据。
- 当前训练一次性 SQLite 先写 raw、按日读回、再写 final 并删除 raw；行业拟合每行业最多执行约九次范围扫描，
  两者存在可避免的磁盘读写。
- 原计划只描述捕获异常后的 rollback，没有覆盖文件移动期间进程被 kill 或断电后的持久化恢复。
- `Regression-Key: history-month-overflow-page-amplification`。

## Added

- 新增同 schema 的 8 KiB 压实、可续传构建、持久化切换日志、断电恢复和训练衔接计划。
- 新增数据库体积、逻辑等价、训练峰值 RSS、训练耗时和重复训练幂等的量化验收门禁。

## Changed

- 重写 `docs/sqlite.md`，明确同 schema 的 8 KiB 压实边界、16 GiB/缩小 30% 硬门禁、未来新月份页大小合同，
  以及不启用 auto-vacuum、不降低持久化等级、不盲删索引的边界。
- 将 activate/rollback/finalize 改为带 fsync 切换日志的可恢复状态机，覆盖进程终止、断电、污染目标、跨文件系统
  和未知备份的失败关闭及恢复要求；增加切换到训练之间的持久化 fence，禁止定时下载改变待验收 snapshot。
- 将当前机器的行数、缺口和大小降为评审证据；真实 build 必须动态封存有类型源基线并以该基线做等价和缩小门禁。
- 明确训练到期比较必须把当前物理分片引用与旧/新逻辑 snapshot sequence 分离，纯物理压实不得伪报 revision。
- 纳入最新 revision 专用 SQL、批量 revision 写入、删除 `raw_samples`、行业 split 单/少遍读取和三列残差共用暴露
  上下文的分阶段性能计划。
- 合并重复验证边界：build 每个目标分片验证一次，activate 复核状态与身份，正式训练保留唯一完整信任校验。
- 增加统一诊断入口、量化数据库/训练验收、故障注入、单次最终全量门禁和真实 build/activate/train/finalize 顺序。

## Fixed

- 修正规划中将当前机器瞬时行数、缺口和大小当作未来转换固定输入的问题，改为运行时封存有类型源基线。
- 修正只覆盖可捕获异常 rollback、未覆盖进程被 kill 或断电恢复的切换设计。

## Removed

- 移除原地 `VACUUM`、双读 fallback、降低 SQLite 持久化等级和提高训练内存上限等非目标方案。
- 移除重复全库验证和重复训练扫描的计划，保留每个信任边界一次必要校验。

## Verification

- 文档完整 diff、标题层级、重复定义、版本命名、稳定路径、交付边界和现有权威合同完成 Review。
- `.venv/bin/python -m pytest tests/contract/test_changelog_archive_contract.py
  tests/contract/test_current_product_contract.py tests/contract/test_authoritative_document_consistency.py -q`
  通过（13 项）；`docs/sqlite.md` 的路径及 Markdown 链接/格式检查通过；`git diff --check` 通过。
- 本批只修改计划和交付记录，不修改运行代码、SQLite 文件、训练工件或权威业务语义，因此不运行 Python、训练、
  打包或浏览器门禁。

## Residual Risks

- 8 KiB 全库 12–15 GiB、读取查询收益、一次性样本库降幅和真实完整训练耗时仍是实施阶段待验证预测；计划禁止将
  代表月结果冒充全库完成事实。
- 本批没有执行压实、切换或训练；`data/history/baostock` 和训练工件保持不变。
- 用户已有未跟踪文件 `docs/v1v2.md` 不属于本批，保持原样且不进入提交。
