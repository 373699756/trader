# Delivery Record: BaoStock Converted-History Layout Normalization

## User Request

定位 `./run.sh train-tomorrow` 返回 `history_manifest_unavailable` 的原因，并修改保留的一次性
`scripts/convert_baostock_history.py`，把既有转换结果按当前稳定年月文件要求重新命名，使真实归档可被当前训练读取器
使用。

## Cause

- `data/history/baostock` 的控制库和 100 个月库完整存在，但 snapshot 仍引用转换脚本旧版生成的
  `partitions/YYYY/MM/<sha>.sqlite3`。当前活动合同只接受 `partitions/YYYY/MM.sqlite3`，因此控制状态解码失败，
  训练入口把它统一投影为 `history_manifest_unavailable`；这不是 24 GiB 行情数据或月分片缺失。
- 活动读取链删除旧布局兼容后，保留的一次性转换脚本没有为自己此前已经完成的结果提供受约束的原地归一化路径。

## Added

- 新增一次性 hash 布局识别和归一化：只接受该转换器的 `legacy_daily.<source_fingerprint>` 来源身份、单一有效 active
  snapshot、全量旧路径以及文件名与控制 SHA 一致的已完成结果。
- 新增归一化故障注入和回归，覆盖正常重命名、完成后幂等回放、月文件全部移动但控制库尚未发布时的恢复，以及分片
  被修改时失败关闭。
- 新增完成态统计进度，按月输出有界进度，避免长时间只读复核没有可观察状态。

## Changed

- 每个月库在移动前通过当前 `SQLiteHistoryMonthPartitionRepository.verify` 核对 SHA-256、行数和 SQLite schema；
  文件原地移动到 `partitions/YYYY/MM.sqlite3`，全部就位后才原子替换已验证的新控制库。
- 完成态缺口统计改为 SQLite 聚合最新 observation，仍分别计算 raw、qfq 和 `is_st` 缺失量，避免 Python 解码数百万
  行造成不必要的长时间回放。
- 当前实施状态更新为“真实 active archive 已可读、真实 V3 训练工件仍待生成”；活动训练、同步和读取链不增加旧路径
  fallback，也不保留双布局。

## Fixed

- 修复已有完整转换数据因仅有路径命名与当前合同不一致而被误报为 `history_manifest_unavailable` 的问题。
- 修复归一化若在月文件移动后中断，重复执行无法安全接续并发布 current snapshot 的问题。

## Removed

- 真实目标中的 100 个 hash 文件名和其空月份子目录在验证后被稳定年月文件替代；源归档、行情记录和 revision 未删除。
- 未恢复任何活动旧 manifest codec、旧路径读取或训练 fallback。

## Verification

- 定向回归覆盖转换器、控制库、月分片、训练读取器、稳定命名和文档合同；受影响脚本 Ruff、mypy 和
  `git diff --check` 通过。
- 对真实 `data/history/baostock` 执行离线归一化：100/100 个月库逐个验证后完成原地移动和控制库原子切换；随后以同一
  命令完成幂等只读复核。结果为 9,250,220 条物理记录、9,097,033 条最新有效记录、2000 个交易日和 209 个可补充
  字段缺口；当前训练读取器成功打开 `complete_manifest`，含 5453 只代码、100 个稳定月分片，active snapshot hash
  为 `36df9ba5c2557479f67172336d0caf12b4ebbf6ea5b4d6567f5bf84a096e6e3d`。
- `scripts/diagnose_runtime.py --profile research --runtime-config config/runtime.json --output -` 将 history archive 判为
  `active`，同时保持研究输入门槛、点时证据和生产权限失败关闭。
- 完整高风险门禁通过：`make format-check`、`make lint`、`make type-check`、`LC_ALL=C make test`、`make package`。
  首次 lint 发现并修复一处异常链表达；首次默认 locale 全量测试发现实施文档契约措辞被改动并已恢复，另一个
  `systemd-analyze` 用例因沙箱中文权限错误未触发英文 skip，固定 C locale 后按合同跳过并完成全量测试。

## Residual Risks

- 本批只修复并实证历史归档读取，不启动可能耗时较长的真实训练；新模型、报告、training input、重复确定性、2048 MiB
  峰值 RSS 和运行 hash 仍待 `v3_training_artifact_rebuild` 收尾。因此默认 Tomorrow V1、
  `automatic_model_update=false`、点时证据和生产权限均不变。
- 归档仍有 209 个 raw、qfq 或 `is_st` 可补充字段缺口；本批不联网下载或回填，训练是否因有效公共交易日不足而继续
  失败关闭，以后续真实训练的 typed 结果为准。
- 既有历史行业、11:20/14:50 分钟、资格和风险事实仍缺可信点时来源；归一化不补写这些数据，也不改变
  `point_in_time_parity=false`。
- `Regression-Key: zero-argument-history-snapshot-training-alignment`。
