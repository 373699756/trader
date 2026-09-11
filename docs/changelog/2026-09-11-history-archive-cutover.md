# Delivery Record: History Archive Cutover and Release Verification

## User Request

继续 `04_策略回溯.md` 未完成的任务 G，完成旧活动历史链切换、完整门禁、仓库外 wheel 安装、最终 Review、
单提交推送和上游核对。连续反馈同时要求：初次下载磁盘门由 25 GiB 改为 10 GiB；保留
`scripts/convert_baostock_history.py`；沿用 BaoStock SDK 的安全调用间隔；下载终端只在一只股票完成、重试或
失败时输出紧凑 `n/m` 进度与 `HH:MM:SS` 耗时；月分片恢复原设计文档的 `YYYY/MM.sqlite3`，不得使用 hash 文件名。
用户随后明确本批不再等待或处理真实下载。

## Cause

- 活动树同时保留了旧父加增量归档、旧 manifest/codec、旧状态读取和新控制库/月分片链，造成重复真相源；诊断、
  点时资格、历史行业审计和训练节奏仍有旧消费者。
- 历史上下文原先按多个历史日期请求全市场行业快照，单次运行会在逐股日线前长期停留并超时；当前交易日又可能在
  BaoStock 日线尚未发布时被误纳入 2000 日窗口。
- 交互进度曾逐条打印内部等待心跳和 `0/1` 哨兵，既频繁又容易把供应商调用进度误认成全市场下载进度。
- hash 文件名偏离任务开始前权威文档的稳定年月路径。恢复稳定路径后，Review 进一步发现：已有月份被替换而控制
  指针尚未发布时，失败或异常退出可能使上一 active 的整文件 hash 暂时失效。

## Added

- 新增不可变 `HistoryArchiveStatus` 及当前 active snapshot 只读检查边界，供 research status、点时资格、历史行业
  审计和训练 due 共用。
- 新增统一 BaoStock session，集中匿名登录、登出、依赖版本和稳定错误映射；同步与保留的一次性转换脚本复用同一
  current-only session/gateway。
- 稳定月文件替换前创建固定名 `.MM.rollback.sqlite3` sidecar；控制发布失败立即恢复，异常退出则在下一次维护时按
  active 分片 hash 恢复或清理，成功发布后删除。sidecar 不是第二套归档或读取 fallback。
- 新增回归覆盖发布失败回滚、异常替换恢复、20:30 前只请求上一完整日、行业仅取最近完整交易日、断点恢复和紧凑
  进度投影。

## Changed

- 初次同步写前可用空间门固定为 10 GiB，并在任何慢速供应商上下文调用前执行；日更仍在加载上下文后再次检查。
- BaoStock 查询继续固定单 worker、至少 2 秒 SDK 调用间隔、45 秒单次 deadline 和最多两次重试。
- 行业上下文只查询最近完整交易日；不能证明历史生效时间的行业仍保持缺失，不用当前分类回填历史。
- 当日数据只在上海时间 20:30 起进入日线窗口；此前运行以上一自然日为 `as_of`，再由交易日历选择最近完整开市日。
- 月分片路径固定为 `partitions/YYYY/MM.sqlite3`；SHA-256 只保存在控制库分片引用中。
- 交互 stderr 不显示 `started`/`waiting` 心跳、raw/qfq 成功明细、内部 `0/1` 或重复外层失败；逐股和逐月完成显示
  `n/m (百分比)`，累计/调用耗时显示 `HH:MM:SS`，最终 stdout JSON 和退出码保持机器合同。

## Fixed

- 修复历史行业快照在逐股下载前遍历多个历史日期并超时、当日日线尚未发布却进入活动窗口、磁盘门过高且执行过晚、
  进度输出频繁且缺少可定位信息，以及稳定月文件发布失败后上一 active 可能失效的问题。
- 修复 research status、点时资格、历史行业审计和训练 due 仍依赖已退役归档状态的问题；所有消费者现在只读当前
  控制库与 active snapshot。

## Removed

- 删除旧活动父加增量归档、generation manifest、legacy codec/迁移、旧训练数据集、旧状态/诊断 profile 及其专属
  测试；活动源码、入口、Web、训练和诊断均不再导入这些模块。
- 未删除 `scripts/convert_baostock_history.py`；它仍是用户显式执行的一次性离线工具，不进入活动产品调用链。

## Verification

- 目标回归覆盖同步 runtime、供应商、稳定月分片、控制库、当前 archive status、CLI 进度、转换脚本和文档合同；
  新增发布失败/异常退出恢复用例通过。
- 完整高风险门禁通过：`make format-check`、`make lint`、`make type-check`、`make test`、`make package`。首次
  `make format-check` 发现并修正一个格式问题；首次 lint 发现并消除进度 adapter 的复杂度债；首次全量测试发现并
  修正 9 个仍断言旧等待输出或 hash 子目录的测试。`make package` 在受限沙箱首次因构建依赖网络被拒，允许网络后
  同一命令成功。
- 仓库外 wheel 安装通过：包从临时虚拟环境导入，`trader-cli validate-config`、`pip check` 和 10 个模板、CSS、
  JavaScript、图标、模型及自动化资源读取通过。
- `scripts/diagnose_runtime.py --profile research --output -` 正确失败关闭：archive `unavailable`、V3 `blocked`、
  `production_authority=false`，没有把未完成真实归档投影成可训练或可生产。
- 最终 Review 检查完整 diff、新增文件、退役模块死引用、稳定命名、单文件上限、`git diff --check` 和暂存范围。

## Residual Risks

- 按用户最新指令，本批不再等待或处理真实 2000 日下载，也未执行其后的真实训练、重复确定性、2048 MiB RSS 和
  运行模型/bundle hash 实证。因此 `v3_training_artifact_rebuild` 仍为 `in_progress`，默认 Tomorrow V1、
  `automatic_model_update=false`、点时证据和生产权限均不变。
- BaoStock 仍是限速慢速正确性基线，没有高效日更来源；历史行业、11:20/14:50 分钟、资格和风险事实的可信点时
  来源仍是后续外部阻塞。
- `Regression-Key: zero-argument-history-snapshot-training-alignment`。
