# Delivery Record: Tomorrow Training Cadence

## User Request

继续 `04_策略回溯.md` 第 12.7 节阶段 E，完成 Tomorrow 训练节奏的整节实现、验证、提交与推送。

## Cause

远端阶段性实现已建立控制面 due 对象和新月分片训练读取面，但训练结果尚未投影 due 详情，活动 bundle 的
`label_cutoff` 仍可为空，`train-tomorrow` 仍接受测试路径参数；同时仅以 snapshot hash 变化判断 revision 会把
正常日更误报为 `input_revision_due`，且新 snapshot 被填入旧父/增量字段，形成隐藏双语义。

## Added

- active snapshot 对比只扫描发生变化且与最后成功 cutoff 相交的月分片，精确找出影响已训练输入的 revision。
- revision 失效范围包括前一 T+1 标签日、修订日及后续最多 60 个特征依赖日；SQLite 训练缓存可原子保留未受
  影响日期并绑定新 snapshot。
- CLI 结果按白名单投影 `label_cutoff`、成熟日数、due 原因和缓存失效日期。

## Changed

- 训练只读取固定项目历史根的完整 active snapshot；Shell、PowerShell 和底层 CLI 都拒绝路径、profile 和其它
  尾随参数。
- 控制面从最后成功 bundle 的 `label_cutoff` 统计成熟交易日：第 19 日为 `not_due`，第 20 日为
  `cadence_due`；无 bundle、修订和不完整输入分别投影稳定原因。
- 模型、报告、训练输入和活动指针统一绑定
  `training_input_hash/source_identity_hash/label_cutoff`，不再把新归档伪装成旧父加增量身份。

## Fixed

- 训练失败或验证拒绝不再清除 due，也不改写上一活动 bundle；只有三件工件完整发布成功才把本次结果清零。
- 正常新增交易日与历史 revision 分开判断，避免在未满 20 个成熟标签日时错误启动训练。
- 活动 bundle 指针与 model/report/training-input 的 cutoff 和来源身份任一缺失或错配时统一失败关闭。
- 训练与下载共用历史维护锁，训练拿锁后重新打开活动快照；竞争者明确返回
  `history_maintenance_running`，避免指针切换破坏固定输入。

## Removed

- 删除阶段 E 新增但与领域控制面重复的应用层节奏实现，不保留双状态源。
- 删除 `train-tomorrow --runtime-dir` 和旧 bundle 父/增量身份兼容。

## Verification

- 合并最新 B–D 上游后，训练、bundle、控制面、月分片、CLI、架构和文档共 171 项定向测试通过。
- 受影响文件 Ruff format/check、mypy 与 `run.sh` 语法通过；真实零参数训练在无归档时稳定失败关闭，非法参数在
  入口解析期退出且未创建历史数据。
- 只读研究诊断如实返回历史不足与生产未授权；PowerShell 在本机不可用，其入口由静态契约测试覆盖。
- 完整高风险门禁按 A–G 大任务约定留到阶段 G 对合并 diff 统一执行。

## Residual Risks

- 本机 `data/history` 为空，无法执行真实 2000 日训练、重复确定性或 2048 MiB 成功训练内存门。
- 阶段 F 自动化与提醒、阶段 G 切换和总验收仍未完成；默认 V1、`automatic_model_update=false`、生产权限、
  Today/D25、68/32、DeepSeek 168、Top6 和冻结规则均未改变。
- `Regression-Key: zero-argument-history-snapshot-training-alignment`。
