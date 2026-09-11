# Delivery Record: Tomorrow Training Progress and Verified-Read Performance

## User Request

用户运行 `./run.sh train-tomorrow` 半天没有输出，无法判断训练仍在推进还是已经卡住，并担心本地 PC 被训练拖死；
要求以紧凑 `n/m`、百分比和累计时分秒显示进度，最终确认固定 2 个计算线程、2048 MiB 内存门且日志不显示 RSS，
历史转换阶段必须显示需要转换的总条数和已经转换的条数。

## Cause

- 根因已确认：旧进度只在每 50 只股票后发送一次；按交易日横截面残差化完全静默；模型拟合只发送一次
  `0/股票总数`，但真实工作单位是行业，工件验证与发布也不可见。
- 首个性能故障边界位于月分片读取器：每次逐股读取都会重新对相关月分片执行整文件 SHA-256、SQLite
  `quick_check` 和行数核验。当前 5453 只股票、100 个约 25 GB 的月分片会把完整验证重复放大到约 54.5 万次，
  与工程设计“训练输入打开验证一次、逐股读取不重复 hash”的契约冲突。

## Added

- 新增不可变训练进度状态，覆盖资源策略、输入分片校验、历史行转换、横截面转换、行业模型、工件发布和结束。
- 新增交互式 stderr 投影：真实 `n/m`、两位百分比、累计 `HH:MM:SS`、有效样本或模型计数；推进按 30 秒节流，
  60 秒无推进发送存活心跳。
- 新增活动 snapshot 当前训练股票池内最新 revision 的精确行数查询，以及逐股读取的“已检查行数 + 有效训练行”有类型批次；已退出当前股票池但仍留在归档中的历史行不会使分母虚高。

## Changed

- 月分片读取器在同一实例内按不可变分片身份缓存成功验证；训练先验证每个活动分片一次，后续精确计数和逐股查询
  复用已验证 repository，不再重复读取全文件校验。
- 模型阶段分母改为实际待处理行业数；被样本资格过滤的历史行仍计入已检查条数，有效样本单独显示。
- 训练继续固定两个计算线程、LightGBM 单线程和 2048 MiB 峰值门，并降低 POSIX/Windows 进程优先级。
- Ctrl+C 返回 130 并输出紧凑取消摘要；最终 stdout `tomorrow_training_result` JSON 和零参数入口保持不变。

## Fixed

- 修复完整训练长时间无输出、横截面与行业阶段无法判断存活、模型进度分母错误以及逐股重复校验全部月分片的问题。
- 交互日志不再显示原始进度 JSON 或 RSS。

## Removed

- 移除 CLI stderr 的 `tomorrow_training_progress` 原始 JSON 投影；不删除训练结果、历史数据或活动 bundle。

## Verification

- `python -m pytest` 定向覆盖训练进度、月分片验证缓存与精确行数、训练输入、行业分母、CLI 资源策略、两份文档契约和内存脚本，全部通过。
- 受影响文件 `ruff format --check` 和 Ruff `E,F,I,B,UP` 通过；全部 394 个源文件 mypy 通过；`git diff --check` 通过。
- 真实 `./run.sh train-tomorrow` 立即输出 `00:00:00 | 资源预检 | 1/1 (100.00%) | 完成 | 计算线程 2 | 内存预算 2048 MiB`；随后因宿主机已有历史维护进程持锁，紧凑摘要明确返回 `0/1 (0.00%) | 阻塞 | 错误 history_maintenance_running`。本次未进入分片校验，因此真实 25 GB 单次验证和历史行推进仍待锁释放后复核。
- 完整门禁待当前 `v3_training_artifact_rebuild` 大任务收尾统一执行。

## Residual Risks

- 当前机械硬盘仍决定一次性 25 GB 校验和大量 SQLite 查询的绝对耗时；本批消除重复全文件验证，不承诺固定完成时间。
- 完整真实重训、重复工件 hash 和 2048 MiB 峰值门仍属于当前大任务剩余验收，不能以定向测试或锁竞争实测替代。
- `Regression-Key: tomorrow-training-progress-resource-safety`。
