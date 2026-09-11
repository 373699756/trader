# Delivery Record: Compact History Download Progress and Failure Diagnostics

## User Request

用户运行 `./run.sh download_history` 时看到大量原始 JSON，其中秒数耗时、`total_units=1` 和
`max_attempts=3` 难以理解；要求输出主要围绕进度、耗时和错误定位，并以紧凑 `n/m` 方式呈现。

## Cause

根因已确认：交互式 stderr adapter 把内部 `HistorySyncProgress` 事件逐条直接投影为 JSON。
供应商日历、总体和行业子阶段的 `completed_units=0,total_units=1` 是单次调用完成哨兵，不是全市场
下载进度；`attempt/max_attempts` 是当前供应商请求尝试数。失败后外层 `loading_context` 事件还会遮蔽最后一个
更具体的行业阶段，只有最终 JSON 保留错误码。历史交付记录表明旧链曾经使用人类可读单行，当前行为属于回归。

## Added

- 新增交互式结束摘要，在最终 stdout JSON 前用一行显示同步结果；失败或阻塞时同时显示错误码。
- 新增进度 adapter 单元回归，覆盖行业快照、逐股总进度、短暂失败后重试成功、最终错误定位和成功摘要。

## Changed

- 交互式 stderr 改为单一人类可读投影：阶段、状态、当前项、进度、尝试和耗时以 ` | ` 分隔。
- 真实股票和月分片进度使用 `n/m (百分比)`；raw/qfq 调用保留最近股票总进度。供应商子阶段不再
  显示没有整体含义的 `0/1`，只显示快照日期、股票或日期范围。
- 累计与单次调用耗时统一为 `HH:MM:SS`，尝试数收敛为“尝试 n/m”。结束摘要对成功、无需更新、重叠、取消、
  阻塞和失败分别给出稳定中文状态。
- 失败摘要优先保留最后具体供应商阶段和当前项，同时输出最终稳定错误码。stdout 的
  `history_maintenance_status` JSON 及其退出码保持不变。

## Fixed

- 修复行业快照日期被误解为“历史下载到该日期”、子阶段 `0/1` 被误解为全市场进度的问题。
- 修复原始秒数与字段名噪声让长时任务难以扫读、外层失败让具体超时位置不够显眼的问题。
- 完整 lint 发现上一已推送批次的控制库测试将第一方 `trader` 导入放在第三方组；本批仅校正导入分组，
  不改变该测试或控制库行为。
- 完整测试发现实施计划已进入阶段 G，但一项文档契约仍要求“阶段 A–E 完成、下一阶段 F”；现同步为已实际交付的
  “阶段 A–F 完成、下一阶段 G”，不改动计划本身。

## Removed

- 仅从交互式 stderr 移除 `history_sync_progress` 原始 JSON 和子阶段占位计数。不删除内部有类型进度事件、
  定时任务结构化轮转日志或最终 stdout JSON。

## Verification

- 先行更新契约与回归测试后执行定向测试，确认旧 JSON stderr 投影导致 6 项预期失败；完成实现后，历史同步、
  BaoStock runtime/supplier、CLI、架构和策略回溯文档契约共 121 项定向测试通过。
- 完整门禁通过：`make format-check`、`make lint`、`make type-check`、`make test`、`make package`。首次完整门禁
  发现并修复上一批次遗留的测试导入分组，以及阶段 F 文档契约的陈旧断言；修复后重新执行受影响回归和完整门禁通过。
- 仓库外 wheel 验证通过：可安装生成的 wheel、导入包、执行 CLI，并读取 10 个打包资源。
- 真实运行 `./run.sh download_history` 验证紧凑单行会持续显示累计耗时、阶段、状态、尝试和单次调用耗时；
  在供应商总体阶段受控中断后正确显示“同步已取消”，无 traceback，且未遗留下载进程。行业超时和股票 `n/m`
  分别由确定性回归覆盖，未为实测而等待外部接口再次超时。

## Residual Risks

- BaoStock 行业接口超时仍是外部供应商风险；本批改善定位和可读性，不改变 45 秒单次期限、最多两次重试或失败关闭。
- `Regression-Key: history-download-human-progress-diagnostics`。
