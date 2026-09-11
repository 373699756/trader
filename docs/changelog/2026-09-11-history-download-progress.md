# Delivery Record: History Download Progress and Timeout Diagnostics

## User Request

用户运行 `./run.sh download_history` 后长时间没有输出，最终只看到 `supplier_call_timeout`，无法知道执行阶段、
下载进度或具体超时位置；要求补全可见进度。

## Cause

根因已确认：BaoStock 子进程虽然发送了调用活动消息，但父进程只用它重置 deadline，没有交给同步编排或 CLI；
`run_history_sync` 也没有进度端口，因此公开命令只能在结束时输出一次状态。旧 deadline 还会在 SDK 返回结果句柄时
重新计时，使结果行解析阶段额外获得一整个超时窗口；失败原因统一压成 `supplier_call_timeout`。本次失败发生在
`load_context` 建立 checkpoint 之前，旧状态不足以进一步还原 calendar、universe 或 industry 子阶段。

## Added

- 新增不可变 `HistorySyncProgress` 和单一进度端口，覆盖初始化、上下文、供应商登录/日历/总体/行业、raw/qfq、
  股票下载、月分片封存和活动 snapshot 发布。
- CLI adapter 每个事件立即向 stderr 输出 `history_sync_progress` 白名单 JSON，包含阶段、状态、当前项、完成量、
  百分比、重试次数、调用耗时和总耗时；等待供应商时每 5 秒发送心跳。

## Changed

- 供应商超时按实际子阶段返回 `supplier_<stage>_timeout`，重试前显示下一 attempt；最终维护状态仍单独写 stdout，
  原有 `history_maintenance_status` schema 和退出码不变。
- 单次供应商调用从开始到结果解析共用同一个 45 秒 deadline，不再因返回惰性结果句柄而重新获得第二个窗口。
- 供应商直接接收统一的 `HistorySyncConfiguration`，重试游标改为不可变值对象；全量 Review 同时把既有训练到期
  散参收束为不可变请求，并拆开训练预检与执行职责，不改变 cadence、模型输入、评分或产物格式。

## Fixed

- 修复历史下载在登录、上下文加载、逐股下载和重试期间完全静默的问题。
- 修复 generic timeout 无法判断卡在日历、证券总体、行业、raw 或 qfq 的问题。
- 修复在供应商上下文加载期间按 Ctrl+C 会泄漏 traceback 的问题；现在投影 `cancelled` 并保留已有状态。
- 保持普通 CLI 惰性加载：进度 JSON adapter 只在 `download_history` 分支导入，不影响看板、帮助或其它命令。

## Removed

- 未删除历史数据、checkpoint 或活动 snapshot；不增加新的公开参数或旁路下载命令。

## Verification

- 测试先行证明旧实现缺少 `HistorySyncProgress`；实现后 133 项历史同步、训练读取、CLI 和架构定向测试通过，
  最终职责拆分后的 74 项训练/同步回归及不可变供应商游标的 21 项回归通过。
- `make format-check` 通过（700 文件）；`make lint` 通过（Ruff 全绿且严格复杂度债务为零）；
  `make type-check` 通过（403 个源码文件）；`make test` 通过（收集并执行 1899 项）；`make package` 通过。
- 首次隔离打包仅因沙箱禁止联网获取 `setuptools` 失败；获得联网授权后重新执行成功，最终代码再次构建成功。
  仓库外 wheel 安装验证通过，`trader-cli validate-config`、依赖检查和 6 个包资源读取均通过；wheel 包含新增进度 adapter。
- 真实 BaoStock 受控 12 秒验证观测到登录完成、日历阶段 5 秒心跳和结构化 `cancelled`；没有 traceback，
  维护状态仍输出到 stdout，结束后没有残留 CLI 或 SDK 子进程。

## Residual Risks

- 未自动跑完可能持续数小时的全市场 2000 日下载；已证明进度、超时边界与取消收尾，完整归档仍需用户再次执行命令。
- 本批不改变 BaoStock 正确性基线、2 秒限速、最多 2 次重试、自动训练、生产授权、评分或冻结行为。
- `Regression-Key: history-download-silent-supplier-timeout`。
