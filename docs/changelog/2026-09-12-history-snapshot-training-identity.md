# History SQLite and Tomorrow training pipeline optimization

## User Request

完成 `docs/sqlite.md` 未完成的代码任务：使 8 KiB 纯物理压实后的 snapshot、训练到期、缓存失效和固定训练工件
身份正确衔接，并优化月库 latest revision 读写、Tomorrow 样本管线、行业拟合扫描、暴露残差和统一性能诊断。
本批先完成全部实现，再统一补测试和验证；不运行真实转换或完整训练。

## Cause

`_revised_dates_since_bundle()` 发现分片 SHA 改变后，把旧 snapshot 整体交给稳定路径读取器。压实替换文件后，
读取器会使用旧 SHA 验证当前新文件并失败，把逻辑内容完全相同的物理压实误报为
`history_training_data_incomplete`。若仅跳过该错误并返回 `not_due`，活动 bundle 又会继续绑定旧 snapshot hash，
导致压实后的验收训练无法启动。

`Regression-Key: history-month-overflow-page-amplification`。

## Added

- 新增不可变 `HistoryPartitionRevisionComparison`，显式携带当前 `physical_reference`、`before_sequence` 和
  `after_sequence`。
- 新增真实月库 sequence 比较回归，以及 build/activate 后旧 bundle 对新物理 snapshot 的到期集成回归。
- 新增正常标签日推进的负向回归，证明普通日更仍按成熟交易日 cadence，不因 snapshot hash 改变每日重训。
- 新增 full/code/board/code+board 专用 latest revision 查询、批写临时键表，以及训练切分日期索引和有类型行业
  计数/矩阵边界。
- 扩展 `history-archive` 参数化诊断，统一输出冷热查询中位数、query plan、峰值 RSS 和一次性批写吞吐；2 GiB
  训练门记录阶段耗时与样本库峰值。

## Changed

- revision 比较只用当前 active 分片 SHA 完成一次物理验证，然后在同一个月库上重放旧/新 sequence 并比较
  `(trade_date, code) -> revision_id`。
- 纯物理压实不产生 `revised_dates` 或 `invalidated_cache_dates`；当活动 bundle 仍绑定旧 snapshot 且
  `label_cutoff` 未前进时，使用既有 `training_contract_due` 启动一次完整训练并换绑新 hash。
- 显式压实训练函数加入训练模块公开导出；普通 `run_tomorrow_training` 行为不变。
- 样本构建只保留一个交易日，直接写最终 `samples`；三列 residual momentum 共享一个不可变 exposure context。
- 全部行业/切分先一次聚合计数，达标行业只扫描一次并按准确行数分配 training/early/calibration 矩阵，validation
  只保留计数。

## Fixed

- 修复纯物理压实因旧 SHA 验证当前文件而降为 `data_incomplete`。
- 修复纯压实逻辑等价时可能返回 `not_due`、从而无法生成绑定新 snapshot 的四个固定训练文件。
- 保持真实 A→B、新增或缺失逻辑行继续产生 revision 日期及既有 60 日有界缓存失效范围。
- 保持 latest revision 历史 sequence 重放、revision 禁止回写和同序号冲突；移除 nullable SQL 分支与逐行预取查询。
  原计划 anti-join 及后续 `MAX(sync_sequence)` 聚合回连在 4 KiB 生产月全月热读均约 10.3 秒，已被性能门禁
  否决；生产保留更快的窗口语义，只移除 nullable filter，避免为了消除临时树合入已知退化。

## Removed

- 不再通过旧 snapshot 的物理引用读取稳定路径上的新文件；未增加旧目录 fallback、双读、工件 generation 或缓存兼容层。
- 删除未被活动训练链使用、且允许旧 snapshot 缓存原地换绑的 `SQLiteHistoryTrainingCache`；训练唯一使用按当前
  snapshot 完整重建并自动清理的 `.sample-workspace.*` 一次性样本库。
- 删除一次性样本库中的 `raw_samples` 表、逐日读回和删除链，以及每行业重复 count/range scan。

## Verification

- 按用户要求先完成全部生产实现，再统一补写并运行等价、故障、批写、扫描次数、内存有界与诊断契约测试。
- `make format-check`、`make lint`、`make type-check`、`make test` 和 `make package` 在全部合并 diff 上通过。
- 真实 24 GiB、4 KiB 归档的参数化诊断证明：anti-join 与 `MAX(sync_sequence)` 回连的全月热读
  均约 10.3 秒，因明显退化被拒绝；最终窗口读取保留。有界 512 行 revision 批写在一个事务内完成，
  三类已有状态均为整批预取，实测约 2,286 行/秒；诊断只修改系统临时副本。

## Residual Risks

- 生产归档仍为 4 KiB，真实 build、activate、2 GiB 训练、重复 `already_current` 和 finalize 均未执行。
- 真实 8 KiB 冷热对比与 2 GiB 完整训练尚未执行；生产库仍为 4 KiB，必须依第 10 章顺序在已推送源码上
  执行 build、activate、2 GiB 训练、重复 `already_current` 和 finalize，不得将本次代码门禁冒充为生产转换证据。
- 用户未跟踪的 `docs/v1v2.md` 原样保留且不纳入本提交。
