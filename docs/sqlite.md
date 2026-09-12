# 历史 SQLite 压实、原子替换与 Tomorrow 训练优化计划

## 1. 批次基线与边界

- 本计划评审基线：`HEAD == @{upstream} == 9cfe32567c10007cd086847822f4eaa54ee90b8e`。
- 开始评审时暂存区为空；`docs/sqlite.md` 是本计划文件，用户已有 `docs/v1v2.md` 保持原样且不纳入后续实现提交。
- 本文件是待实施计划，不是第二套运行契约；实现批次必须同步更新受影响的四份现有权威/实施文档后才能生效。
- 用户可见问题：历史归档从约 9 GiB 增长到 24 GiB，但最新有效数据只增加约 0.37%；
  `./run.sh train-tomorrow` 的分片校验和训练耗时过长，并曾在桌面内存压力下连同终端被终止。
- 已确认根因：约 1 KiB 的 `WITHOUT ROWID` 行落在 4 KiB SQLite 页上时产生大量溢出页；普通 4 KiB
  `VACUUM` 对代表月只缩小约 2%，新增数据量不是 24 GiB 的主要原因。
- `Regression-Key: history-month-overflow-page-amplification`。
- 本计划覆盖影响矩阵中的历史归档、持久化恢复、离线训练、资源编排、运维诊断和文档边界。
- 不改变候选、特征、标签、训练切分、Ridge/LightGBM 数学、Tomorrow V1/V2/V3 评分身份、默认档位、
  `automatic_model_update=false`、Today/D25、融合公式、DeepSeek 预算或冻结规则。

### 已确认数据证据

- 当前生产归档为 100 个月分片，物理大小 `24,956,952,576` 字节，即 23.243 GiB。
- 当前共有 9,250,220 条物理 revision、9,097,033 条最新有效记录、5453 只证券和 2000 个交易日。
- 与旧归档相比仅增加 33,159 条最新有效记录，另有 153,187 条历史 revision；两者不足以解释约 15 GiB
  的物理增长。
- 代表月 `2025/12`：4 KiB 源库 329,494,528 字节；8 KiB 压实后 172,793,856 字节，减少约 47.6%。
- 代表月 `2026/09`：4 KiB 源库 82,038,784 字节；8 KiB 压实后 52,797,440 字节，减少约 35.6%。
- 16 KiB 代表月实测大于 8 KiB，因此固定选择 8 KiB，不继续扩大页大小。

### 目标架构与明确拒绝项

- “相同格式”固定表示继续使用 `control.sqlite3 + partitions/YYYY/MM.sqlite3`，表、字段、索引、JSON codec、
  revision/observation、主键、snapshot sequence 和读取接口保持不变；本批只改变物理页大小、压实状态和执行路径。
- 压实核心、校验、切换日志和恢复逻辑归属 `src/trader/infra/research` 的有类型组件；
  `scripts/repack_baostock_history_archive.py` 只作为显式运维入口，不复制业务规则，也不新增 `run.sh` 公开命令。
- 拒绝原地 `VACUUM`、同时写新旧归档、目录级双读 fallback、取消 SHA/完整性校验、提高训练内存上限、
  提高下载并发或以降低 `synchronous` 换速度。
- 暂不删除现有三个二级索引；它们分别服务单股、日期/板块和 observation 查询，只有完整工作负载实测证明冗余后
  才能在独立 schema 迁移中调整。
- 暂不把 hash 改为 BLOB、不拆除 JSON 重复字段、不增加压缩扩展；这些会改变 schema/codec，不属于本次同格式压实。

## 2. 实施分解与完成条件

本次数据库与训练优化作为一个高风险大任务，按以下依赖顺序实施。每个子任务只运行直接相关的定向门禁；
最后一个子任务完成后，对完整合并 diff 统一运行一次全量门禁和适用专项实证。

1. 压实与切换基础：8 KiB 新月初始化、可续传转换、有状态切换、断电恢复和 rollback。
2. snapshot 配套：新物理 hash、active sequence 前进，以及训练到期比较的物理引用/逻辑序列解耦。
3. 归档读写性能：最新 revision 查询专用化；有实测收益后再批量化 revision 写入。
4. 训练数据管线：删除 `raw_samples` 中间表、按日直接生成最终样本，并减少行业分片重复扫描。
5. 真实转换与训练：构建 `data/historyless/baostock`、切换生产归档、在 2 GiB scope 中完成一次训练。
6. 最终验收：重复训练快速返回 `already_current`，删除旧库备份，记录大小、耗时、RSS 和工件身份。

完成条件：归档逻辑内容不变、活动身份完整、生产路径只有一个可读实现、压实库不超过 16 GiB 且至少缩小 30%、
训练峰值 RSS 不超过 2048 MiB、训练工件与新 snapshot 配对、无已知 Review 发现、适用门禁通过且提交已推送。

## 3. 数据库压实与可续传构建

### 3.1 组件和入口

- 在 `src/trader/infra/research` 新增有类型的压实协调器、转换状态、切换日志和 codec；内部状态不得以无类型字典传播。
- 新增薄入口 `scripts/repack_baostock_history_archive.py`，提供 `build`、`activate`、`rollback` 和 `finalize`。
- 进度写 stderr，最终由 CLI adapter 将稳定的有类型状态白名单投影到 stdout；外部状态身份固定为
  `history_archive_repack_status`，不增加项目版本编号。
- 性能诊断接入 `scripts/diagnose_runtime.py` 统一入口及 `scripts/runtime_diagnostics/` 所有者模块，不增加独立诊断脚本。

### 3.2 Build

1. 获取 `data/history/baostock/.maintenance.lock`。锁覆盖源身份检查、全部转换和切换，阻止下载、训练及并发压实。
2. 前置检查：
   - active snapshot、100 个月分片、2000 个交易日、父身份和路径完整；
   - 无待提交 WAL、rollback sidecar、临时分片或未恢复的旧切换日志；
   - `data/historyless/baostock` 只能为空，或包含绑定同一源 snapshot hash、页大小和 schema 的可续传状态；
   - 源与目标位于同一文件系统，以保证 activate 使用原子 rename；
   - 可用空间覆盖源库大小的保守目标上限、最大单月临时文件、转换状态和 2 GiB 保留空间。
   - 将当时实际 active hash、分片引用、物理/有效行数、证券数、交易日、字段缺口和源字节数封存为有类型源基线；
     本节列出的当前数字只是评审证据，不能替代运行时基线。
3. 串行转换每个月，避免并行 `VACUUM` 争抢磁盘和内存：
   - 以只读源连接执行 `PRAGMA page_size=8192`，随后 `VACUUM INTO` 同月 `.pending` 文件；
   - 检查目标 `page_size=8192`、`freelist_count=0`、schema、metadata、WAL、`quick_check`、外键和行数；
   - 按主键顺序分别计算源/目标 `daily_records`、`daily_observations` 的长度前缀 SHA-256；逻辑 hash 必须相同；
   - 核对最新有效行、revision sequence 和 A→B→A 回放；
   - 计算目标物理 SHA-256，fsync 文件和目录后将 `.pending` 原子改名为正式年月路径。
4. 可续传状态绑定源 active snapshot hash、schema identity、目标页大小，以及每个完成分片的逻辑/物理 hash。
   恢复时复核已完成文件；源 snapshot、配置或 schema 变化立即拒绝续传，不能混合两批数据。
5. 全部分片完成后，用新物理 SHA-256 创建 `sequence + 1` 的 active snapshot：
   - 数据截止日、标签截止日、父身份、日历、证券总体、行数和逻辑 revision 不变；
   - 保留来源、checkpoint、due、提醒和旧 snapshot 记录；
   - 最后生成目标 `control.sqlite3`，控制库继续使用 4 KiB 页。
6. 替换前硬门禁：
   - 目标物理 revision、最新有效记录、证券、交易日和字段缺口必须与本次 build 封存的源基线完全相同；
     不能在压实时补数或修改内容；
   - 100/100 分片全部通过 schema、逻辑 hash、物理 hash、行数和 revision 回放；
   - 总大小不超过 16 GiB，且相对本次封存的源字节数至少缩小 30%；
   - 源文件 stat 和 active snapshot 在持锁构建期间保持不变。

源逻辑 hash 在首次成功后保存到绑定源 snapshot 的续传状态，恢复构建时不得重复扫描已经完成且身份未变的月份。

实施状态（2026-09-12）：本节的有类型 build 状态、严格 codec、串行 8 KiB `VACUUM INTO`、逐表逻辑 hash、
分片物理校验、空间/缩减门禁和断点续传已经实现；新建月库也固定为 8 KiB。真实 100 月构建必须在本批代码提交并
推送后显式运行，当前生产归档仍是 4 KiB、`24,956,952,576` 字节，不能把代码完成写成实物已转换。

### 3.3 后续新月和活动月份

- 修改 `SQLiteHistoryMonthPartitionRepository.initialize()`：仅在新建空库、任何建表语句之前执行
  `PRAGMA page_size=8192`，随后再设置 WAL、`synchronous=FULL` 并创建 schema。
- 已有 8 KiB 月库的旁路复制自然保持页大小；封存时必须增加页大小契约检查，禁止未来月份退回 4 KiB。
- 活动月份每次旁路修改后检查 `freelist_count/page_count`。只有碎片比例超过经基准确认的阈值时才对 pending
  副本重新压实，不能每天无条件 `VACUUM`。
- 不启用 `auto_vacuum`；封存月库主要顺序追加和旁路替换，pointer-map 只会增加空间及写放大。

## 4. 可断电恢复的 Activate、Rollback 与 Finalize

### 4.1 持久化切换状态机

在 `data/historyless` 保存原子写入并 fsync 的切换日志，绑定源/目标 snapshot hash、明确路径和以下状态：

```text
prepared
old_partitions_moved
old_control_moved
new_partitions_activated
new_control_activated
verified
finalized
rolled_back
```

- 每次文件移动前确认当前状态和目标身份，移动完成后 fsync 两侧父目录，再提交下一状态。
- `activate`、`rollback`、`finalize` 启动时必须先读取日志并恢复未完成事务；进程被 kill、机器断电或 Python
  无法捕获的异常都不能依赖当前调用栈 rollback。
- 任意状态只允许继续前进或按已验证旧身份回退；未知文件、状态跳跃、hash 冲突和跨文件系统路径全部失败关闭。
- 从 `new_control_activated` 起直到 `finalized` 或 `rolled_back` 保留持久化切换 fence：普通历史下载即使在两个
  命令之间取得进程锁也必须返回维护中；训练只有在重新取得同一维护锁并确认预期 active hash 后才能越过 fence。

### 4.2 Activate

1. 在维护锁内确认 `data/historyless/baostock-before-repack` 不存在，或正好是日志记录的同一旧归档。
2. 保留 `data/history/baostock` 目录和 `.maintenance.lock`，依次原子移动旧 `partitions/`、旧 `control.sqlite3`
   到备份，再移动新 `partitions/`、新 `control.sqlite3` 到稳定路径；每一步均更新日志和 fsync。
3. 重新打开稳定路径控制库，核对新 active snapshot、所有目标路径、文件 stat 和预先封存的物理身份。
4. build 已逐分片完成完整校验，且 activate 是同文件系统 rename，因此切换阶段不再重复全库 SHA 和
   `quick_check`；正式训练仍会在自身信任边界完整验证一次。
5. 读取、身份或路径验证失败时，根据日志恢复旧归档；如果进程中断，由下一次命令在拿锁后执行同样恢复。
6. 数据库验证成功但训练失败时保留压实库为活动库和旧备份，停止等待诊断；只有归档身份或读取失败才 rollback。
   切换 fence 保持到用户明确 finalize 或 rollback，避免失败诊断期间日更让新旧基线无法安全回退。

### 4.3 Finalize

- 直接读取并验证活动 bundle、model、report、training-input 和训练内存结果，不能只信任一份可单独伪造的摘要 JSON。
- 只有数据库验证、训练成功、峰值 RSS、训练输入 hash、bundle 配对和重复 `already_current` 全部通过，才允许删除
  `data/historyless/baostock-before-repack`。
- finalize 必须确认当前 active hash 仍等于切换目标、活动 bundle 绑定该 hash；若中间状态发生变化则拒绝删除。
- 删除前再次核对备份真实路径和旧 snapshot hash，拒绝符号链接、路径越界或身份不明目录；删除后不可恢复。
- 删除后记录实际释放空间、活动归档大小、active snapshot、model 和 report hash 摘要，并将状态推进为
  `finalized`。

实施状态（2026-09-12）：切换日志、逐步 fsync/rename、跨命令恢复、rollback、下载/训练 fence，以及同时校验
活动训练 bundle 与 2 GiB 内存门证据后才允许删除备份的 finalize 已实现。尚未对生产目录执行 activate、rollback
或 finalize；旧归档也尚未删除。

## 5. Snapshot、训练到期和缓存身份

8 KiB 压实会改变 100 个分片的文件字节和 SHA-256，即使所有逻辑行完全相同，也必须创建新的 active snapshot，
并让训练输入绑定新 snapshot hash。

### 5.1 修复训练到期比较

当前实现使用旧 snapshot 的旧分片 SHA 去验证已经被稳定路径新文件替换的月份，可能把纯物理压实误报为
`history_training_data_incomplete`。目标接口必须分离：

- `physical_reference`：始终是当前稳定文件的新 SHA、行数和路径，负责文件信任校验；
- `before_sequence`：旧 snapshot sequence，负责读取压实前逻辑 revision 视图；
- `after_sequence`：新 snapshot sequence，负责读取压实后逻辑 revision 视图。

在同一个已通过新 SHA 验证的物理月库上比较两个 sequence 的 `(trade_date, code) -> revision_id`：

- 完全相同：判定为纯物理压实，不产生 revision 日期和缓存失效范围；
- A→B、缺行、新增行或日期变化：按现有规则计算 revision 日期及前一标签日、当日和后续最多 60 日失效范围；
- 无法用当前物理文件重放任一 sequence：失败关闭，不能假定为压实。

没有正式活动训练 bundle 时必须判定为 `initial_training_required`；已有活动 bundle 时则按其训练输入身份、
标签截止日和逻辑 revision 判断到期。该修复保证日更和再次压实时不会因纯物理 hash 变化误判。

### 5.2 缓存和旧工件

- 新 snapshot hash 形成新的训练输入身份；不得复用绑定旧 snapshot 的样本缓存。
- 训练开始时清理遗留 `.sample-workspace.*`，重新生成一次性样本库。
- 旧活动 bundle 保持可用，只有新 model、report、training-input 完整校验并原子发布后才切换活动指针。
- 失败、Ctrl+C、SIGTERM 或 2 GiB OOM 都不能覆盖旧活动 bundle，也不能清除 due。

实施状态（2026-09-12）：`HistoryPartitionRevisionComparison` 已把当前稳定文件的 `physical_reference` 与
`before_sequence/after_sequence` 分离；训练到期只用新 SHA 验证一次当前月库，再在该文件上重放两个 sequence。
纯物理压实不产生 revision 日期或缓存失效范围，但因活动 bundle 仍绑定旧 snapshot 且 `label_cutoff` 未前进，会以
既有 `training_contract_due` 启动一次身份换绑训练；普通新增标签日仍遵循 20 日 cadence。无 bundle 继续返回
`initial_training_required`，损坏、缺月、错误 sequence 或无法读取任一视图继续失败关闭。第 5 章代码已完成，
未被活动链使用且允许旧 snapshot 原地换绑的 `SQLiteHistoryTrainingCache` 已删除，训练只创建新的临时样本库。
真实 build/activate/train 尚未执行。

## 6. SQLite 读取与写入性能优化

### 6.1 最新 revision 查询

当前通用 `ROW_NUMBER()` SQL 会 materialize CTE，并为窗口排序和最终排序创建临时 B-tree。代表月只读实测：

| 查询 | 耗时 | 峰值 RSS |
| --- | ---: | ---: |
| 当前 `ROW_NUMBER()` | 0.93 秒 | 23,552 KiB |
| 等价 `NOT EXISTS` anti-join | 0.80 秒 | 12,544 KiB |

该结果只是一个热缓存代表月证据，不能直接外推为完整训练提速比例。实施时：

1. 为全月顺序扫描、单代码窗口、单日/板块截面提供各自明确 SQL，移除 `? IS NULL OR ...` 对查询计划的干扰。
2. 在真实归档上分别实测带 `snapshot_sequence` 上限的 `NOT EXISTS` anti-join 和按
   `(trade_date, code)` 聚合 `MAX(sync_sequence)` 后精确回连 observation 的方案；替代方案只有在
   保持历史 snapshot 重放语义且不退化时才取代窗口函数。
3. 在普通月、revision 密集月、A→B→A、code 和 board 过滤上证明与原查询逐行一致。
4. 比较 4 KiB/8 KiB、冷/热缓存的多轮中位数和查询计划；只有无语义变化、无工作负载退化且有稳定收益才替换。
5. 保持 `mode=ro`、8 MiB SQLite cache、`mmap_size=0` 和 `temp_store=FILE`；不以扩大页缓存换取表面速度。

### 6.2 Revision 批量写入

现有写入在一个事务内逐行查询 observation、最大 sequence 和已有 revision，再分别插入 record/observation。
正确性完整，但 SQLite 语句数随行数线性放大。

在读取优化和训练优化完成后，再实施有界批量写入：

1. 批量编码并验证确定性排序、月份、逻辑键和输入内部冲突。
2. 使用临时键表或有界 `VALUES` 一次预取已有 observation、revision 和各代码日期的最大 sequence。
3. 在有类型实现中执行同序号冲突、禁止回写、同 revision 内容一致性校验。
4. 使用 `executemany` 分别插入新增 record 和 observation，仍在一个 `BEGIN IMMEDIATE` 事务内完成。
5. 保留 WAL、`synchronous=FULL`、checkpoint、fsync、幂等和 A→B→A；异常时整批回滚。

BaoStock 全市场下载仍受逐股供应商请求和限速主导。写入优化必须以 SQLite trace/吞吐实证证明价值，不能通过增加
供应商并发、缩短安全间隔或降低持久化等级换速度。

实施状态（2026-09-12）：读取器已按 full/code/board/code+board 拆分无 nullable 分支的 SQL。原计划 anti-join 在
当前 4 KiB 生产月实测全月热读中位数约 10.3 秒，明显退化；后续 `MAX(sync_sequence)` 聚合虽能快速得到键，回连
大 JSON 记录后热读仍约 10.3 秒并产生最终排序临时树，两者都已否决。生产最终保留语义和实测均更快的窗口函数，
只合入专用 filter SQL；消除窗口临时 B-tree 这一候选因性能门禁不通过而明确不实施。revision 写入已先确定性编码整批输入，以临时键表一次预取
observation、revision 与最大 sequence，再在同一 `BEGIN IMMEDIATE` 事务中分别 `executemany`。WAL、FULL、
外键、禁止回写、同序号冲突、幂等与 A→B→A 语义保持不变。统一性能门禁已拒绝两种在真实 4 KiB
归档上退化的替代，最终保留窗口函数并只合入专用 filter SQL，不把“没有窗口树”本身当优化。

## 7. Tomorrow 训练数据管线优化

### 7.1 删除 `raw_samples` 中间表

当前每个样本经历“写 `raw_samples` → 按日读回 → 写 `samples` → 删除 raw”，形成一次额外完整写入、读回和删除，
而删除不会缩小一次性 SQLite 文件的峰值大小。

历史归档已经按月份、日期、代码顺序扫描；当扫描到下一交易日时，为上一交易日生成的样本仍按样本日期成组。
目标流程改为：

1. 每只证券继续只保留 61 行窗口和一个待 T+1 标签样本。
2. 内存只缓冲一个样本交易日，最大约为当天证券数，不缓存全历史。
3. 当样本日期变化时，完整计算该日 benchmark、三列动量残差和扣成本前 alpha。
4. 在单一事务中批量写入最终 `samples`，不再创建、查询或删除 `raw_samples`。
5. 当日完成即释放 Python 对象；异常时一次性 workspace 整体丢弃，旧活动 bundle 不变。

必须用同一历史夹具比较新旧最终样本的日期、代码、行业、六特征、标签、行数和内容 hash，证明训练语义未变。

### 7.2 减少行业模型重复扫描

当前每个行业最多执行约九次日期范围扫描：训练矩阵先计数再读取，early/calibration/validation 分别计数，
early/calibration 矩阵再各计数和读取。

在一次性样本 SQLite 中新增约 2000 行的 `sample_split_dates(trade_date, split_name)`：

- 一次聚合得到每个行业、每个 split 的准确行数；
- 对满足门槛的行业按 `industry, trade_date, code` 顺序读取一次，或最多按 training/early/calibration 各读取一次；
- 使用已知计数预分配 NumPy 数组，validation 只保留计数，不创建无用矩阵；
- 每个行业训练、早停和校准结束后立即释放数组及 LightGBM dataset。

目标是把每行业约九次范围扫描降为一至三次，同时保持行业顺序、日期切分、embargo 和确定性模型工件不变。

### 7.3 三列残差共用暴露上下文

三列 momentum 当前分别重建板块分组、行业分组和流动性 exposure。调整纯函数边界，使同一交易日只构建一次：

- 板块和行业的稳定分组索引；
- `log(average_amount_20d)` 及其板块/行业中心化 exposure；
- 三列值仍按现有 market → board → industry → liquidity 顺序分别计算残差。

实现必须保持 `math.fsum` 顺序和现有黄金向量结果；如果不能证明逐元素等价，则保留现有实现，不以数值漂移换速度。

### 7.4 明确保持不变的训练资源

- 计算线程固定为 2。
- Linux scope 保持 `MemoryHigh=1792M`、`MemoryMax=2048M`、`MemorySwapMax=2048M`、CPU/IO 权重 20。
- 一次性 SQLite cache 保持 32 MiB，LightGBM 保持列式直方图和 64 MiB pool。
- 不改为 float32，不提高 mmap/cache，不并行训练多个行业，不扩大 2 GiB 上限。
- 模型公式、特征 manifest、标签、切分和确定性参数不因 SQLite 页大小变化而改变。

实施状态（2026-09-12）：`raw_samples` 表及其逐日读回/删除链已经移除；扫描按日期/代码推进时只缓冲一个样本日，
同日直接计算 benchmark、三列残差与扣成本前 alpha，并在一次事务内写最终 `samples`。`sample_split_dates` 约束
训练/早停/校准/验证日期，一次聚合形成全部行业计数，每个达标行业只顺序读取一次并按已知行数预分配三个矩阵，
validation 只计数。三列 momentum 复用同一不可变 exposure context，仍逐列保持原 `math.fsum` 次序。线程、
SQLite/LightGBM cache 与 2 GiB 上限未改；固定夹具的样本、残差和模型确定性已纳入统一回归，真实 2 GiB
完整训练仍按第 10 章在生产转换后单独执行。

## 8. 避免重复全库验证

完整 SHA-256 和 SQLite `quick_check` 都是必要信任边界，但同一不可变文件在一次持锁流程中不应无条件重复。

- build：每个目标分片完成一次物理/逻辑/SQLite 完整校验，并把结果封存到绑定源/目标身份的状态中。
- build 收口：只检查 100 个已封存结果、总体身份和控制库引用，不再重新读取全部目标文件。
- activate：同文件系统 rename 后检查切换日志、路径、stat、控制库和封存身份，不再重复完整 SHA/quick_check。
- train-tomorrow：保留且只保留训练信任边界的一次 SHA-256、quick_check 和行数验证，后续计数及扫描复用同一进程
  已验证 repository。
- 第二次 `./run.sh train-tomorrow`：在 due/bundle 身份匹配后快速返回 `already_current`，不能重新验证 100 个分片。

相对原计划，这会少做两轮活动归档全量 SHA 和 `quick_check`；按 12–15 GiB 目标库估算，可避免约 48–60 GiB
额外顺序读取，同时不删除正式训练的信任校验。

实施状态（2026-09-12）：build 收口和 activate 已复用封存校验，训练 archive 在一次进程内缓存已验证 repository；
due/bundle 身份检查位于分片验证之前，第二次训练命中当前 bundle 时直接返回 `already_current`。正式训练仍完整执行
且只执行一次 100 月 SHA、`quick_check` 和行数验证，没有放宽任何跨进程信任边界。

## 9. 性能诊断与验收门禁

### 9.1 参数化诊断

扩展 `scripts/diagnose_runtime.py` 和 `scripts/runtime_diagnostics/` 的归档性能 profile，所有探针只读、输出有界摘要，
不输出股票明细、SQLite 内容或完整 hash。固定记录：

- 总库/分片/page/freelist/overflow/index 大小；
- 全月最新行、单代码 61 日、单日板块三类查询的冷/热多轮中位数和峰值 RSS；
- 有界 revision 批写夹具的 SQL 数、吞吐、事务数和文件增长；
- 历史校验、样本生成、横截面、行业拟合、工件发布各阶段耗时；
- 一次性样本库峰值大小和进程峰值 RSS。

真实训练进度除 `n/m` 外继续显示当前分片字节进度；各阶段记录开始/结束和吞吐，避免“仍存活”被误解为“速度正常”。

实施状态（2026-09-12）：`history-archive` profile 已扩展为参数化冷热多轮 full month、单代码 61 交易日、单日板块
查询，输出实际 query plan、行数、中位数、临时 B-tree 与峰值 RSS；另把活动库的有界真实 revision 复制到系统临时
目录，记录批写 SQL、事务、吞吐和文件增长，不修改活动归档。2 GiB 训练门结果新增各阶段耗时与一次性样本库峰值。
真实 8 KiB 数据库和完整训练证据尚未运行，验收数值不得提前填写。

### 9.2 数据库验收

- 100/100 分片 `page_size=8192`、`freelist_count=0`、schema/metadata/hash/行数/外键/`quick_check` 通过。
- 源/目标两张业务表逻辑 hash、snapshot sequence 视图和 A→B→A 回放完全一致。
- 总大小不超过 16 GiB且至少缩小 30%；报告实际值，不把 12–15 GiB 预测写成完成事实。
- 三类读取工作负载均无显著退化；full/code/board/code+board 专用 filter SQL 的语义和中位数必须稳定。
  如消除窗口临时 B-tree 的候选产生退化，必须拒绝该候选，不得为满足查询计划外观而合入。
- 批量写入如实施，必须降低语句/事务开销且吞吐稳定提高；没有实测收益则不合入。

### 9.3 训练验收

数据库切换后只完整训练一次：

- 使用实现提交并已推送的源码 commit 作为审计用 `source_commit`，但不把普通 commit 变化纳入重训合同；
- 独立 systemd scope、2 个计算线程、峰值 RSS 不超过 2048 MiB；
- `training_status=engineering_ready`；
- `training_input_hash` 等于新 active snapshot hash；
- 四个固定文件 `model.json`、`report.json`、`training-input.json`、`active-bundle.json` 完整配对，hash 不进入路径；
- 新旧样本构建器在固定夹具上内容一致，新管线的一次性 SQLite 峰值和阶段耗时低于旧管线；
- 无遗留 `.sample-workspace.*`、staging 或未恢复的切换日志；
- 再运行一次命令快速返回 `already_current`，不改写活动 bundle。

真实训练内存结果保存到 `data/historyless/training-memory-result.json`，但 finalize 必须重新读取并验证实际 bundle，
不能仅凭该文件删除备份。

## 10. 测试、Review、提交和真实运行顺序

### 10.1 契约先行和故障回归

- 4 KiB 大行溢出夹具转换为 8 KiB 后至少缩小 30%，4 KiB/8 KiB 读取结果完全一致。
- 新建月份在 schema 前固定 8 KiB；已有月份旁路复制、修改和封存后仍为 8 KiB。
- schema、全部行、sequence 视图和 A→B→A revision 回放一致。
- 可续传、源 snapshot 变化、目标污染、空间不足、跨文件系统和未知备份均失败关闭。
- 在切换状态机每个文件移动和日志提交点注入中断；重试必须恢复为完整旧库或完整新库，不能留下混合状态。
- 纯物理压实不产生 `input_revision_due`；真实 A→B、缺行和新增行仍产生正确 revision/缓存失效日期。
- 最新 revision 新旧 SQL 覆盖 full/code/board、多个 sequence 和 revision 密集数据，结果逐行相同。
- 新旧训练样本和模型输入等价；单日缓冲有明确上限，失败不发布半成品。
- finalize 在训练 hash、RSS、bundle、备份路径或切换状态不匹配时拒绝删除。

### 10.2 门禁纪律

每个实现切片运行直接相关的 unit/component/contract、Ruff、mypy、`git diff --check` 和参数化性能夹具，记录“全量门禁待大任务收尾统一执行”。最后统一运行一次：

```bash
make format-check
make lint
make type-check
make test
make package
```

追加真实 8 KiB 归档验证、2 GiB 完整训练、重复 `already_current` 和仓库外 wheel 的受影响命令 smoke；
不因本批不改 Web 而运行浏览器布局验收。

### 10.3 交付和运行顺序

1. 完成代码、契约、定向测试和文档 Review，更新 `docs/01_评分逻辑.md`、`docs/02_工程设计.md`、
   `docs/03_工程实施.md`、`docs/04_策略回溯.md`、`CHANGELOG.md` 和交付记录；如新增诊断 profile，同步更新
   `trader-delivery` 的运行诊断路由；提交并推送实现切片。
2. 使用该已推送提交执行真实 `build`，只在 `data/historyless/baostock` 生成目标库；满足全部硬门禁后才 `activate`。
3. 从 build 状态读取目标 snapshot hash，并通过 `scripts/check_tomorrow_training_memory.py` 的
   `--expected-history-snapshot-hash` 显式传入；只有该 hash 与维护锁内重新打开的活动归档完全一致，验收训练才可
   越过切换 fence。在新活动库上执行一次 2 GiB 完整训练；失败时保留新活动归档、旧归档备份和旧活动 bundle，停止诊断。
4. 训练成功后执行重复 `already_current`、最终 Review和大任务完整门禁，提交并推送真实证据记录。
5. 核对 `HEAD == @{upstream}` 后执行 `finalize` 删除旧归档备份，报告删除目标、不可恢复性和释放空间。
6. 任务完成后停止，不自动启用 Tomorrow V3、不重启服务、不实施后续 schema 迁移。

## 11. 后续独立 schema 优化候选

如果同格式 8 KiB 压实后仍需要显著小于 12–15 GiB，应另立经用户确认的 schema 迁移批次，评估：

- 将 64 字符十六进制 `revision_id/content_hash` 改为 32 字节 BLOB；
- 消除 payload 与表列中重复的 code、trade_date、board、sequence；
- 将训练必需数值直接列式保存，或评估有上限的压缩 payload 及其解码 CPU 成本；
- 重新设计索引后，用 full/code/board/revision 四类工作负载证明收益。

该候选会改变 codec、schema、物理身份和所有消费者，不能作为本计划的隐藏扩展，也不能为了体积牺牲点时重放、
可审计性或训练读取速度。
