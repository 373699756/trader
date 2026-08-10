# 任意启动与 30 秒安全关闭：双 Codex 并行实施计划

状态：已实施并进入主分支交付；实施基线为
`8bd3e2d980accf8f12ec5e219f0c1115e11e1367`。下文第一节保留实施前 Review 现场，
用于说明问题来源，不代表当前活动实现状态；活动契约以
`docs/software-business-design.md` 和 `docs/recommendation-strategy.md` 为准。

  ## 一、重新 Review 后的基线结论

  当前本地 HEAD 与上游均为 6acedca8，但工作树不是稳定基线：

  - 审查开始时有 17 个文件、约 277 行未提交变化。
  - 审查过程中又扩大到 24 个文件、400 余行变化。
  - 新增内容正在实现“观察池仅进程内存在，冻结和重启后不保存”，方向符合您的要求。
  - 这些变化不是本轮 Review 产生的，说明另一个进程仍可能在修改工作树。
  - 生命周期核心文件 cadence.py、runtime.py、server.py、workers.py 尚未修复，因此原生命周期风险仍成立。
  - 首次针对性测试曾发现 close_fallback 仍回放观察项；之后相关测试又被外部改为期望空正式结果。因为基线持续变化，不能把当前状态宣称为已通过。

  因此，生命周期实施前必须先固定并闭合当前“观察池不落盘”批次。

  ## 二、当前确认仍存在的问题

  ### P0：晚启动仍可能错误追补 today

  src/trader/application/cadence.py:128 在第一次调度时把所有已经过去的时间点加入任务，并在提交前直接写入 _fired_points。

  结果：

  - 11:20 后启动，initialize() 虽过滤 today，但第一个 cadence tick 又会生成 today freeze。
  - 14:50 或 15:00 后启动，会把 today、tomorrow、d25 以及 DeepSeek cutoff 一起当作到期任务。
  - “持续运行时调度晚一秒”和“边界后冷启动”没有区分。

  ### P0：冻结任务被过早视为完成，失败不会重试

  src/trader/application/cadence.py:174 在任务真正入队和持久化前就把时间点记为 fired。

  src/trader/application/snapshot_workflow.py:104 返回空集合也会被事件系统视为成功，包括：

  - 队列拒绝；
  - SQLite/JSON 临时失败；
  - 没有边界前快照；
  - 持久化成功但 P6 接纳失败。

  没有“pending/inflight/completed/missed”状态，也没有保存同一冻结对象进行重试。

  ### P0：旧冻结仓储在关键 kill point 后可能永久占用交易日

  src/trader/infra/persistence/writer.py:121 当前顺序是：

  SQLite staged manifest
  → JSON immutable file
  → SQLite committed

  如果进程在 manifest_staged 后终止：

  - 重启发现 JSON 缺失；
  - staged 行被标记为 quarantined；
  - UNIQUE(strategy, recommend_date) 仍被占用；
  - 后续相同交易日冻结会永久冲突。

  当前正在进行的 action 列和正式推荐投影修改没有解决这个问题。

  ### P0：30 秒关闭目前没有实现

  当前配置仍为 15 秒：config/v2/runtime.json:22。

  而且它不是全局期限：

  - src/trader/bootstrap.py:139 对 supervisor、source lane、history、research、shadow、cache 分别等待，超时可累加。
  - src/trader/application/runtime.py:80 先等待 scheduler，再给 pipeline 一份完整 timeout，超时后还有无界 join()。
  - src/trader/application/pipeline.py:321 超时后再次无界等待 worker。
  - src/trader/application/workers.py:177 调用 ThreadPoolExecutor.shutdown(wait=True)，没有期限控制。

  实际退出可能远超 30 秒甚至永久挂住。

  ### P1：没有完整的进程信号策略

  src/trader/entrypoints/server.py:21 只依赖 try/finally：

  - Ctrl+C 通常可以进入 finally，但没有第二次信号强制退出语义。
  - SIGTERM 默认行为不保证执行完整清理。
  - Windows SIGBREAK 未处理。
  - 清理期间再次 Ctrl+C 可能直接中断清理。
  - 浏览器关闭不等于服务关闭，这一点也未在运行说明中明确。

  ### P1：交易日历失败可以阻断整个服务启动

  src/trader/application/pipeline.py:243 在启动初始化阶段调用交易日历。日历无缓存且网络失败时，异常会阻止 supervisor 和 Web 启动。

  同时 src/trader/application/queries.py:77 使用 is_trading_day=True 判断冻结状态，周末、节假日和日历不可用时可能返回错误的 today_freeze_missed 或
  afternoon_freeze_pending。

  ### P1：跨日、休眠和系统时钟跳变未形成状态机

  当前只有 wall clock，没有 wall/monotonic 对照：

  - 睡眠后一次性跳过多个边界会被当成普通延迟 tick。
  - 系统时钟回拨后旧 _next_due、旧 inflight 和旧候选可能继续存在。
  - 跨日时 cadence 内部状态部分清理，但候选、异步 review、pending hybrid、重试时间和旧 future 没有统一 session generation 隔离。
  - 迟到的上一交易日 future 仍会消耗资源并尝试发布。

  ### P1：tomorrow-v2 独立冻结也缺少完整恢复循环

  src/trader/infra/persistence/tomorrow_decision_freezes.py:178 使用“JSON 先、SQLite 后”：

  - 比旧仓储更安全，同一对象再次提交可以复用 JSON。
  - 但 kill 后可能遗留没有 manifest 的 orphan。
  - 没有目录 fsync。
  - 没有统一 staged/committed 恢复审计。
  - TomorrowShadowRuntime 只在收到冻结 baseline 时调用一次 freezer；独立仓储临时失败后不保证继续重试。

  ## 三、已经锁定的行为决定

  1. 第一次关闭信号启动安全关闭，整个进程共享一个 30 秒绝对期限。
  2. 第二次关闭信号立即强制退出。
  3. 30 秒不是每个组件各等 30 秒，也不采用多个累加 timeout。
  4. 不新增历史行情、候选、观察池、DeepSeek review、backoff、breaker 或 session 的持久化缓存。
  5. 关闭后这些进程内数据全部丢弃，重启重新预热和计算。
  6. 只继续保存现有业务必须持久化的数据：正式推荐、合法检查点、冻结恢复载荷、预算、证据、overlay 和结算。
  7. 不新增 lifecycle marker 文件或其它重启状态文件。
  8. 不追补上一交易日推荐；缺失只能明确报告。
  9. today 边界后冷启动绝不从检查点、P6 或收盘行情补造。
  10. 浏览器关闭不停止服务；安全关闭入口是终端 Ctrl+C 或操作系统正常终止信号。
  11. 任务管理器强制结束、断电、第二次信号属于异常终止，只依赖持久化恢复保证正式记录一致性。

  ## 四、阶段 0：先闭合当前“观察池不落盘”批次

  这是双 Codex 并行前的硬门禁，不能与生命周期提交混合。

  ### 需要完成的 Review

  - 停止其它自动化进程继续写工作树，连续两次 git status/git diff --stat 结果一致。
  - 确认 official_snapshot()、official_decision() 在检查点、正常冻结、收盘 fallback、tomorrow-v2 冻结中均只保存 executable。
  - 删除或禁用 HTTP 查询中的 _recover_empty_close_fallback() 现场回放；权威契约已经明确 HTTP 不得重放或评分。
  - 将 SQLite action 列改成正式 schema migration，提升 SCHEMA_VERSION，补 v9→新版本升级测试，不能只依赖 _ensure_column()。
  - 保证旧不可变冻结文件不被改写，但 API、归档 backlog 和结算只读取 executable。
  - 为零正式推荐保存合法空冻结记录。
  - 补齐 CHANGELOG.md 的用户问题、原因、变化、验证和剩余风险。
  - 运行完整门禁、wheel 外安装和桌面验收。
  - 单独创建一个 Conventional Commit，推送并核对本地/上游哈希一致。

  只有该批次提交并推送完成，下面两个 Codex 才能开始。

  ## 五、并行前先锁定公共接口

  主 Codex 先在任务说明中固定以下类型，两个 Codex 不得自行改变字段：

  ShutdownDeadline
  - started_at_monotonic
  - timeout_seconds
  - remaining_seconds()
  - expired

  ShutdownStep
  - name
  - completed
  - timed_out
  - cancelled_count
  - detail

  ShutdownReport
  - completed
  - forced
  - elapsed_seconds
  - steps

  TradingSessionStatus
  - trade_date
  - calendar_state
  - is_trading_day
  - phase
  - evaluated_at
  - next_retry_at
  - generation
  - discontinuity_reason

  SchedulePointLifecycle
  - pending
  - inflight
  - retry_wait
  - completed
  - missed

  FreezeAttempt
  - strategy
  - trade_date
  - boundary_at
  - frozen_snapshot / frozen_decision
  - canonical_sha256
  - attempt_count
  - next_retry_at

  公共约束：

  - FreezeAttempt 在进程内不可变保存。
  - 每次重试必须使用相同 snapshot/decision ID、相同规范字节和相同 SHA-256。
  - 持久化恢复载荷属于正式冻结事务的一部分，可以写 SQLite；普通临时运行数据不得写盘。
  - 所有 API 只做加法兼容。

  ## 六、Codex A：任意启动、冻结状态机和交易 session

  ### 独占文件范围

  主要由 Codex A 修改：

  - src/trader/application/cadence.py
  - src/trader/application/schedule.py
  - 新增 src/trader/application/trading_session.py
  - 新增 src/trader/application/freeze_attempts.py
  - src/trader/application/pipeline_submission.py
  - src/trader/application/snapshot_workflow.py
  - src/trader/application/queries.py
  - src/trader/application/pipeline_status.py
  - 对应 application 和 startup integration 测试

  Codex A 不修改 bootstrap.py、pipeline.py、pipeline_stages.py、ports、API/JS、配置、
  权威文档和 CHANGELOG，由主 Codex 集成。

  ### A1. 重做时间点生命周期

  把 _fired_points 替换为显式状态：

  pending → inflight → completed
                    ↘ retry_wait → inflight
  pending → missed

  规则：

  - 任务生成不等于 fired。
  - 只有事件成功入队才进入 inflight。
  - 队列拒绝恢复 pending。
  - 只有业务结果确认完成才进入 completed。
  - 不可恢复的资格缺失进入 missed。
  - 状态和重试均按 trade_date + schedule_point + strategy 隔离。

  ### A2. 区分持续运行延迟与冷启动

  记录进程实际启动时刻：

  - 进程在 11:20 前启动，scheduler 到 11:20:01 才运行：允许提交同一 today 冻结。
  - 进程在 11:20:00 或之后启动：today 直接 missed，禁止检查点恢复。
  - 进程在 14:50 前启动：允许延迟提交 tomorrow/d25 边界冻结。
  - 14:50 后冷启动：只允许有效 14:49:50 检查点恢复；没有检查点则等待 15:00 close fallback。
  - 15:00 后任意启动：先查正式记录，再处理缺失 tomorrow/d25；today 永不 fallback。
  - DeepSeek cutoff 和 final quote 不在边界后冷启动补跑。
  - CLOSE_QUOTES 可以在 15:00 后任意冷启动执行一次。

  ### A3. 同一冻结对象重试

  首次达到边界时：

  1. 取得边界前快照。
  2. 先执行 official-only 投影。
  3. 构造不可变 FreezeAttempt。
  4. 固定 snapshot ID、发布边界、锚点和规范 SHA-256。
  5. 持久化失败时保留该对象。
  6. 按 1/2/5/10/30/30... 秒重试。
  7. 重试期间拒绝新行情、迟到 review 或新的 P6 快照替换该冻结内容。
  8. 15:00 到达时，如果 14:50 attempt 仍 pending，必须先重试该 attempt，不能创建不同的 close fallback。

  ### A4. 交易日历降级不阻塞 Web

  新增线程安全的 TradingSessionStatus：

  - scheduler/初始化代码负责调用日历。
  - HTTP 只读 status，不调用日历网络。
  - 启动时日历失败：持久化恢复和 Web 仍启动，状态为 calendar_unavailable。
  - 重试退避为 30/60/120/300 秒。
  - 有仍在有效期的本地日历缓存时可以继续使用。
  - 无可靠日历时不抓行情、不评分、不冻结、不生成 fallback。

  readiness reason 增加：

  - calendar_unavailable
  - market_closed
  - before_market_open
  - today_freeze_missed
  - afternoon_freeze_pending
  - afternoon_close_recovery_pending

  当前后端按策略返回上述 today/afternoon 原因；`official_record_missing` 仅由前端保留为旧版本
  响应的兼容输入，不再作为当前运行时的 readiness reason。

  ### A5. 跨日和时钟异常

  RuntimeSupervisor 提供 wall/monotonic 异常通知，Codex A 处理 session rotation：

  触发条件：

  - wall clock 回拨超过 1 秒；
  - wall 与 monotonic 增量偏差超过 5 秒；
  - scheduler gap 超过 max(90 秒, 3 × 原计划间隔)；
  - 上海交易日期改变。

  旋转时：

  - 清空候选、当前 market features、普通 overlay、pending hybrid/review 和普通 cadence。
  - 取消上一 generation 尚未开始的普通任务。
  - 保留已经提交的正式冻结、closing overlay、预算、证据和结算。
  - 保留同一冻结事务中精确的 pending FreezeAttempt，但跨交易日后只供恢复审计，不得补造上一日推荐。
  - 所有异步完成回调校验 session generation 和 trade_date。

  ### Codex A 测试矩阵

  至少覆盖：

  - 09:15、09:30、11:19:49、11:19:50、11:20:00、11:20:01。
  - 14:49:19、14:49:20、14:49:50、14:50:00、14:50:01。
  - 15:00、15:10、19:30、23:59、次日 09:15。
  - 周末、法定节假日、日历缓存有效、缓存过期、日历网络失败。
  - 队列拒绝后重新提交。
  - SQLite/JSON 临时失败后 snapshot ID 与 SHA-256 不变。
  - 休眠两小时、时钟向前跳、向后回拨、午夜跨日。
  - 上一 generation 的 late future 不得覆盖新交易日。
  - 14:50 attempt pending 时 15:00 fallback 不得抢占。

  ## 七、Codex B：30 秒两阶段关闭和持久化崩溃恢复

  ### 独占文件范围

  主要由 Codex B 修改：

  - 新增 src/trader/application/shutdown.py
  - src/trader/application/runtime.py
  - src/trader/application/workers.py
  - src/trader/application/events.py
  - src/trader/application/research_coordination.py
  - src/trader/application/source_lanes.py
  - src/trader/application/tomorrow_shadow_runtime.py
  - src/trader/entrypoints/server.py
  - src/trader/infra/persistence/snapshot_files.py
  - src/trader/infra/persistence/writer.py
  - src/trader/infra/persistence/sqlite.py
  - src/trader/infra/persistence/tomorrow_decision_freezes.py
  - 对应 runtime、signal、persistence、kill-point 测试

  Codex B 同样不修改共享集成文件和文档。

  ### B1. 进程信号策略

  处理：

  - POSIX SIGINT
  - POSIX SIGTERM
  - Windows SIGINT
  - Windows 可用时的 SIGBREAK

  行为：

  - 第一次信号：停止 Web 接受新请求并开始安全关闭。
  - 第二次信号：立即强制退出，不再等待。
  - SIGINT/SIGBREAK 正常退出码 130。
  - SIGTERM 正常退出码 143。
  - 30 秒到期仍未完成：记录脱敏 shutdown report，退出码 2。
  - 不增加 HTTP stop 接口。
  - 不宣称 Windows 控制台关闭按钮、任务管理器强杀或断电能优雅完成。

  ### B2. 单一 30 秒绝对期限

  所有 stop 接口接收同一个 ShutdownDeadline，只能读取剩余时间，禁止重新创建 timeout。

  关闭顺序：

  1. 停止 Web 接收新请求。
  2. 停止 scheduler 产生新事件。
  3. 关闭 pipeline 接收门。
  4. 取消尚未开始的普通行情、评分、DeepSeek 和 long 事件。
  5. 优先排空已经接受的 freeze，再排空 risk。
  6. 停止来源 lane 和外部 I/O。
  7. 停止 normalizer、strategy、review、history、research、shadow。
  8. 持久化单写线程最后停止。
  9. publisher/SSE 清理订阅者。
  10. 生成进程内 ShutdownReport 并返回入口。

  任何阶段只能使用 deadline.remaining_seconds()。

  ### B3. 有界执行器停止

  BoundedExecutor 增加：

  - 停止接受任务；
  - 跟踪 pending/running future；
  - 取消未开始任务；
  - 条件变量等待 inflight；
  - 期限到达后使用 shutdown(wait=False, cancel_futures=True)；
  - 返回 completed/timed_out/cancelled 统计；
  - 禁止 timeout 后无界 join。

  由于 Python 的非 daemon executor 线程可能在解释器退出时继续阻塞，入口在总期限到达后必须执行硬退出，不能仅依赖 shutdown(wait=False)。

  ### B4. 事件队列关闭语义

  BoundedEventQueue.close() 支持：

  - 立即拒绝新事件；
  - 删除普通事件并将其审计状态置为 cancelled/failed；
  - 保留 freeze/risk 已接收事件；
  - freeze 优先于 risk；
  - 不等待普通行情队列完全排空。

  ### B5. 修复旧冻结仓储崩溃窗口

  SQLite staged manifest 增加有界 recovery_payload 和其 SHA-256：

  事务写 staged + recovery payload
  → 写临时 JSON、flush、fsync
  → 原子创建正式 JSON
  → fsync 父目录
  → 提交 recommendations + committed
  → 清除 recovery payload

  恢复规则：

  - staged + JSON 正确：提交 manifest。
  - staged + JSON 缺失/损坏，但 recovery payload 正确：按完全相同字节重建 JSON 后提交。
  - staged + recovery payload 损坏：写入独立 quarantine audit，移走残留文件，并删除活动 staged 行，释放交易日唯一键。
  - committed 文件缺失或损坏：fail closed，不自动改写正式历史。
  - committed 行不得因启动恢复被降为可重用交易日。
  - 每个 kill point 后重启都必须幂等。

  ### B6. 对齐 tomorrow-v2 仓储

  为 checkpoint 和 freeze 增加同样的明确状态和恢复：

  - active/staged/committed/consumed。
  - 文件和 manifest 任一阶段 kill 后都可恢复。
  - orphan 必须被采用、隔离或明确审计，不能静默遗留。
  - 文件创建和目录变更均执行目录 fsync。
  - TomorrowFreezeCoordinator 保留同一 sealed decision。
  - shadow runtime 对 persistence_failed 按固定冻结对象重试，不依赖再次收到 baseline。

  ### Codex B 测试矩阵

  至少覆盖：

  - scheduler 永久阻塞。
  - merge worker 永久阻塞。
  - source、research、history、shadow、cache 分别阻塞。
  - 关闭总耗时不超过 30 秒，而不是每组件 30 秒。
  - 第一次信号完成 drain。
  - 清理期间第二次信号立即退出。
  - SIGINT、SIGTERM 子进程退出码。
  - 普通事件取消，freeze/risk 保留。
  - freeze 正在写入时关闭，允许在剩余期限内完成。
  - manifest staged、payload staged、JSON 临时文件、JSON 创建、目录 fsync、manifest committed 各 kill point。
  - same ID/same hash 重试成功。
  - different ID/same trade date 冲突。
  - committed 文件损坏 fail closed。
  - tomorrow-v2 checkpoint/freezing 相同测试。
  - Windows 实机 SIGBREAK 与无残留进程检查。

  ## 八、主 Codex 集成范围

  两个 Codex 完成各自代码和针对性测试后，主 Codex 统一修改共享文件：

  - src/trader/bootstrap.py
  - src/trader/application/pipeline.py
  - src/trader/application/pipeline_state.py
  - src/trader/application/pipeline_stages.py
  - 必要的 ports 和 settings model/loader
  - config/v2/runtime.json
  - docs/software-business-design.md
  - docs/recommendation-strategy.md
  - README.md
  - CHANGELOG.md

  集成内容：

  - 把 shutdown_timeout_seconds 改为 30，并定义为整个进程的绝对总期限。
  - 将 session tracker 同时注入 pipeline、status 和 queries。
  - 将 wall/monotonic 时钟注入 supervisor。
  - 将 FreezeAttempt 与 persistence receipt 接到 worker 完成状态。
  - 将 ApplicationSystem 的所有资源停止接入同一 deadline。
  - /api/status 加法暴露 session、calendar retry、schedule point 和 freeze retry 计数。
  - 文档明确浏览器关闭不停止服务，第一次 Ctrl+C 等待最多 30 秒，第二次立即退出。
  - 文档明确重启后观察池、候选、历史预热和 review 缓存全部重新开始。
  - CHANGELOG 分别记录用户症状、根因、实际变化、验证和剩余 Windows 外部风险。

  ## 九、双 Codex 并行执行协议

  ### 9.1 并行模型

  本任务采用“主 Codex 锁定契约并负责集成，Codex A、Codex B 在同一工作树内修改严格
  不重叠的文件”的模型，不使用两个代理各自提交后再合并的方式。

  原因是协作代理共享同一文件系统：只要文件所有权不重叠，A、B 的改动可以立即被另一方
  的类型检查和测试读取，主 Codex 也可以直接审查完整工作树；额外 worktree、临时提交和
  cherry-pick 反而会增加漂移、重复提交和接口分叉风险。

  并行期间必须遵守：

  - 主 Codex 是唯一协调者、共享文件 owner、暂存者、提交者和推送者。
  - Codex A、B 只能用 `apply_patch` 修改各自明确分配的文件。
  - 两个 Codex 都不得执行 `git add`、`git commit`、`git push`、`git stash`、rebase、
    checkout、clean 或任何破坏性 Git 命令。
  - 两个 Codex 都不得运行会修改全树的格式化命令，不得构建共享 `dist/`、`build/`，
    不得启动占用正式端口或正式运行目录的服务器。
  - 主 Codex 在 A、B 并行实现期间不修改其独占文件；确需调整时必须先暂停对应 Codex 并
    完成所有权移交。
  - 生命周期整体仍是一个独立交付批次，最终只创建一个提交并推送一次。

  根计划只把“Codex A/B 并行实现”作为一个 `in_progress` 项；A、B 各自在自己的子计划中
  保持至多一个 `in_progress` 项，避免根计划出现两个同时进行的同级计划项。

  ### 9.2 并行开始门禁

  主 Codex 只有在以下条件全部满足后才能同时启动 A、B：

  1. 阶段 0 已完成 Review、提交、推送，本地 `HEAD` 与 `@{upstream}` 一致。
  2. 工作树干净，且记录了本任务唯一基线哈希。
  3. 已记录本任务允许修改的完整文件范围。
  4. 已锁定第 5 节的公共类型、字段、返回状态和异常语义。
  5. 已建立下面的文件所有权矩阵。
  6. 已指定 A、B 各自的 failure-first 测试文件和命令。
  7. 已约定跨边界消息格式、集成顺序和缺陷归属。

  若任一条件不满足，只允许主 Codex 做只读审查或完善公共契约，不得让 A、B 提前实现。

  ### 9.3 文件所有权矩阵

  #### Codex A 独占

  ```text
  src/trader/application/cadence.py
  src/trader/application/schedule.py
  src/trader/application/trading_session.py
  src/trader/application/freeze_attempts.py
  src/trader/application/pipeline_submission.py
  src/trader/application/snapshot_workflow.py
  src/trader/application/queries.py
  src/trader/application/pipeline_status.py
  tests/unit/application/test_cadence.py
  tests/unit/application/test_schedule.py
  tests/unit/application/test_trading_session.py
  tests/integration/test_startup_scheduling.py
  ```

  A 负责业务时间、交易 session、调度点状态、冻结资格、应用层 `FreezeAttempt` 和只读
  readiness；不得修改 SQLite/JSON 实现、线程池、进程信号和组合根。

  #### Codex B 独占

  ```text
  src/trader/application/shutdown.py
  src/trader/application/runtime.py
  src/trader/application/workers.py
  src/trader/application/events.py
  src/trader/application/research_coordination.py
  src/trader/application/source_lanes.py
  src/trader/application/tomorrow_shadow_runtime.py
  src/trader/entrypoints/server.py
  src/trader/infra/persistence/snapshot_files.py
  src/trader/infra/persistence/writer.py
  src/trader/infra/persistence/sqlite.py
  src/trader/infra/persistence/tomorrow_decision_freezes.py
  tests/unit/application/test_runtime.py
  tests/unit/application/test_workers.py
  tests/integration/test_graceful_shutdown.py
  tests/component/test_freeze_crash_recovery.py
  ```

  B 负责信号、全局 deadline、资源停止、队列 drain 和持久化原子性；不得决定 today、
  tomorrow、d25 的冻结资格，也不得自行重新构造 A 提供的冻结对象。

  #### 主 Codex 独占共享文件

  ```text
  src/trader/bootstrap.py
  src/trader/application/pipeline.py
  src/trader/application/pipeline_state.py
  src/trader/application/pipeline_stages.py
  src/trader/application/ports/*
  src/trader/infra/settings_models.py
  src/trader/infra/settings_runtime.py
  config/v2/runtime.json
  docs/software-business-design.md
  docs/recommendation-strategy.md
  docs/start_stop.md
  README.md
  CHANGELOG.md
  tests/integration/test_start_stop_integration.py
  ```

  现有大型 `tests/integration/test_v2_pipeline.py` 默认也归主 Codex。A、B 应优先新建各自
  专项测试，避免同时向该文件追加用例；只有无法通过公共 fixture 表达时才申请修改。

  ### 9.4 公共契约冻结

  并行前由主 Codex 在任务消息中逐项写明以下语义，A、B 只能实现，不能单方面扩展：

  - A 创建并持有不可变 `FreezeAttempt`，其中已经完成 official-only 投影并固定规范字节。
  - B 的仓储只接收该规范对象，返回 `committed`、`already_committed`、`retryable` 或
    `conflict`；不得用当前 P6、当前时钟或活动规则重建对象。
  - `retryable` 必须保留相同 snapshot/decision ID 和 SHA-256；`conflict` 是终态，不能
    自动改成 close fallback。
  - B 在 scheduler 中检测 wall/monotonic 异常，只调用已约定的 discontinuity hook；
    session generation、任务失效和 readiness 由 A 决定。
  - A 只产生 `completed`、`retry`、`missed` 三种调度完成结果；事件账本和队列如何记录
    取消由 B 实现。
  - `ShutdownDeadline` 从第一次关闭信号创建一次，任何下游组件只能读取 remaining，
    不得从配置重新创建新的 timeout。
  - 主 Codex 负责把仓储结果、调度完成结果和 shutdown report 接入 pipeline，不允许在
    集成层增加第二套 retry、session 或 timeout 逻辑。

  若实现发现契约缺字段，发现方必须发送 `CONTRACT_REQUEST`，在主 Codex 批准并同步通知
  另一方前不得自行修改公共类型。

  ### 9.5 通信协议

  A、B 只通过以下结构化消息协调，避免用模糊的“我改好了”作为交付证据：

  ```text
  CLAIM
  - owner: A | B
  - files:
  - plan_item:

  CONTRACT_REQUEST
  - symbol:
  - missing_semantic:
  - proposed_change:
  - affected_owner:
  - affected_tests:

  SCOPE_REQUEST
  - requested_file:
  - reason:
  - minimum_change:
  - current_owner:

  BLOCKED
  - blocker:
  - evidence:
  - required_owner:
  - safe_work_still_available:

  READY_FOR_INTEGRATION
  - owner:
  - changed_files:
  - behavior_completed:
  - targeted_commands:
  - test_results:
  - public_assumptions:
  - known_findings:
  - integration_points:
  ```

  `send_message` 用于通知不需要立即打断的接口事实；只有对方必须基于新信息继续工作时才用
  `followup_task`。不得通过反复中断对方获取状态，避免丢失正在进行的 Review 上下文。

  ### 9.6 并行波次

  #### 波次 P0：主 Codex 准备

  主 Codex 完成：

  - 基线和文件范围记录；
  - 公共接口签名；
  - A/B 任务消息；
  - 共享测试 fixture 的最小调整；
  - 根计划中“并行实现”进入 `in_progress`。

  这一波完成后主 Codex 停止修改 A/B 独占文件。

  #### 波次 P1：A/B 并行编写 failure-first 测试

  Codex A 先新增能够证明以下缺陷的失败测试：

  - 冷启动与持续运行延迟 tick 的冻结资格不同；
  - schedule point 不能在入队前完成；
  - calendar unavailable 不得阻断只读状态；
  - session generation 能拒绝跨日迟到结果；
  - pending 14:50 attempt 阻止不同 close fallback 抢占。

  Codex B 先新增能够证明以下缺陷的失败测试：

  - 多组件等待共享一个绝对 deadline；
  - timeout 后不存在无界 join；
  - 第二次信号立即退出；
  - 关闭只 drain freeze/risk，不排空普通队列；
  - 所有 manifest/JSON kill point 可恢复或 fail closed。

  每个 Codex 必须先运行自己的 failure-first 测试并保存预期失败证据，再开始实现。

  #### 波次 P2：A/B 并行实现

  - A 按 A1-A5 顺序实现，每完成一个子项运行对应单元测试。
  - B 按 B1-B6 顺序实现，每完成一个子项运行对应单元或组件测试。
  - 任一方遇到跨 owner 文件需求时发送 `SCOPE_REQUEST`，继续其它安全工作，不得抢写。
  - 任一公共契约变化都必须经过主 Codex 批准；获批后双方先同步类型假设，再继续实现。

  #### 波次 P3：各自 Review

  A、B 分别对自己的完整 diff 执行：

  1. 检查越界文件、死代码、重复状态机和遗留 TODO。
  2. 检查异常因果、时区、线程安全、类型和 API 兼容。
  3. 运行拥有文件的 Ruff format check、Ruff、mypy 和针对性 pytest。
  4. 修复所有发现后重新查看完整 diff。
  5. 确认零已知发现后发送 `READY_FOR_INTEGRATION`。

  A、B 的状态只能是 `ready_for_integration`，不能自行宣称任务完成。

  #### 波次 P4：主 Codex 串行集成

  主 Codex 在收到两份 READY 后暂停 A、B 的文件写入，按以下顺序集成：

  1. 公共 ports、返回类型和状态枚举。
  2. B 的持久化恢复 primitive。
  3. A 的 `FreezeAttempt`、schedule point 和 session 状态机。
  4. B 的 shutdown、executor、queue 和 signal 生命周期。
  5. `pipeline_stages.py` 的业务结果传递。
  6. `pipeline.py`/`pipeline_state.py` 的统一状态和回调。
  7. `bootstrap.py` 的依赖注入与资源停止顺序。
  8. settings、30 秒配置、API/status。
  9. 两份权威文档、README 和 CHANGELOG。

  集成时发现的缺陷按违反的原始契约归属回派 A 或 B；如果问题只存在于组合连接，则由主
  Codex 修复共享文件。主 Codex 不得把回派缺陷直接复制成集成层特判。

  #### 波次 P5：联合回归与最终 Review

  主 Codex 先运行最小联合测试：

  ```text
  tests/integration/test_start_stop_integration.py
  tests/integration/test_startup_scheduling.py
  tests/integration/test_graceful_shutdown.py
  tests/component/test_freeze_crash_recovery.py
  ```

  然后执行完整质量、测试、构建、wheel 安装、信号子进程和桌面验收。任何失败都回到正确
  owner 修复；每轮修复后重新执行受影响测试，最后再执行全量门禁。

  ### 9.7 并行测试隔离

  A、B 可以同时运行针对性测试，但必须关闭共享 pytest cache，并使用不同临时目录：

  ```bash
  # Codex A
  PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -p no:cacheprovider \
    --basetemp=/tmp/trader-codex-a \
    tests/unit/application/test_cadence.py \
    tests/integration/test_startup_scheduling.py

  # Codex B
  PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -p no:cacheprovider \
    --basetemp=/tmp/trader-codex-b \
    tests/unit/application/test_runtime.py \
    tests/integration/test_graceful_shutdown.py \
    tests/component/test_freeze_crash_recovery.py
  ```

  并行阶段禁止运行：

  - `make test`、`make package` 和仓库外 wheel 安装；
  - 全树自动格式化；
  - 使用 `.runtime/v17` 的真实进程；
  - 使用正式 5000 端口的 server 测试；
  - 浏览器桌面验收；
  - 会写共享 `dist/`、`build/`、`.pytest_cache` 或截图目录的命令。

  这些门禁只能在 P4 集成完成后由主 Codex 串行运行。

  ### 9.8 冲突和缺陷归属

  - 时间、calendar、session、cadence、readiness、冻结资格错误归 A。
  - signal、deadline、queue drain、worker 残留、JSON/SQLite/fsync 错误归 B。
  - `FreezeWriteResult` 到 schedule completion 的连接归主 Codex。
  - 只有 A+B 组合才出现的问题，由主 Codex 先建立最小复现，再按被违反的公共契约归属。
  - 如果两边都符合契约但契约本身不足，由主 Codex 修改契约并要求 A、B 同时确认。
  - 不得按异常出现的文件位置直接归属，例如 writer 抛出的 retryable 最终被 cadence 错误
    标记 completed，责任仍归消费结果的 A。

  ### 9.9 A/B 退出条件

  Codex A 的 `READY_FOR_INTEGRATION` 必须证明：

  - 冷启动/热运行边界矩阵全部通过；
  - freeze attempt 重试保持相同 ID 和 hash；
  - calendar failure、跨日和时钟异常有确定状态；
  - 没有修改 B 或主 Codex 独占文件；
  - 没有已知未解决 Review 发现。

  Codex B 的 `READY_FOR_INTEGRATION` 必须证明：

  - 所有资源共享同一 deadline；
  - 没有 timeout 后无界等待；
  - 第一/第二信号和退出码符合契约；
  - 两套冻结仓储 kill-point 恢复通过；
  - 没有修改 A 或主 Codex 独占文件；
  - 没有已知未解决 Review 发现。

  A、B ready 不代表交付完成。只有主 Codex 完成联合 Review、全量门禁、CHANGELOG、单提交、
  推送及上游哈希核对后，整个生命周期任务才能标记 completed。

  ## 十、最终验收

  ### 自动门禁

  make format-check
  make lint
  make type-check
  make test
  make package

  另外必须执行：

  - git diff --check
  - 架构 AST 契约
  - create_app() 零副作用
  - 固定融合向量 83.40
  - DeepSeek 预算并发上限
  - SSE 游标、慢客户端
  - 冻结恢复与 SHA-256
  - 仓库外 wheel 安装、导入、CLI、静态资源
  - 1280×720、1440×900、1920×1080 桌面验收

  ### 生命周期专项验收

  - 所有启动时间矩阵。
  - 日历失败时 Web 可启动且不误冻结。
  - today 边界后冷启动永不追补。
  - 同一冻结对象跨临时失败保持相同哈希。
  - 15:00 fallback 不抢占 pending 14:50 attempt。
  - 第一次关闭信号正常排空。
  - 总关闭期限严格不超过 30 秒。
  - 第二次信号立即退出。
  - 正常关闭后没有遗留进程、端口和非 daemon worker。
  - 重启后观察池、候选、历史和 review 临时状态为空并重新预热。
  - 没有新增临时业务状态文件。

  ### 提交与推送

  - 以上 Review 到零已知问题后更新 CHANGELOG。
  - 只暂存生命周期任务文件。
  - 创建一个 Conventional Commit，例如：

  fix(runtime): make startup and shutdown lifecycle deterministic

  - 推送当前跟踪分支。
  - 分别读取本地 HEAD 与 @{upstream}，确认哈希完全一致。
  - 不创建 PR、不合并、不打 tag。
