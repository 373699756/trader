# Delivery Record: Cross-platform History Automation and Due Reminders

## User Request

用户要求继续 `04_策略回溯.md` 的完整任务 F：提供 Linux、Windows、macOS 用户级自动同步、15:10/20:30
补跑、可逆安装、轮转日志、训练到期提醒去重和 `check` 只读状态，同时继续禁止无人值守训练或生产启用。

## Cause

阶段 E 之前只有手工 `download_history` 与持久化 due/reminder 值对象，没有操作系统任务模板、统一无人值守入口、
提醒前原子去重、桌面通知降级、任务日志或 `check` due 投影。仅添加 cron/systemd 示例不能解决休眠补跑、并发、
进程重启、跨日提醒、安装回退和 Windows/macOS 一致性，因此本批选择以不可变控制库为真相源的统一执行链，
三个平台只保留用户级唤起适配。

## Added

- 新增不可变 `HistoryReminderClaim`；控制库在桌面通知前按 `(due_identity, 上海日期)` 原子占有身份，通知结果再以
  `sent` 或 `notification_degraded` 独立封存。并发、同日重跑及进程重启不会重复通知，未解除 due 次日重新可提醒。
- 新增统一 `scheduled-history-maintenance`：复用零参数同步和既有维护锁，输出最终控制台 JSON、写大小轮转任务日志、
  调用平台桌面通知；通知不可用、超时或失败不改变同步结果。
- 新增 Linux systemd user service/timer、Windows Task Scheduler XML 和 macOS LaunchAgent 模板；分别固定
  `Persistent=true`、`StartWhenAvailable=true`、日历触发加 `RunAtLoad=true`，共同覆盖上海时间 15:10/20:30。
- 新增 `install-history-automation` / `uninstall-history-automation` 一键入口。安装先展示用户级文件和命令并确认，
  验证现有配置、虚拟环境和依赖；托管回执支持失败后重试，卸载只删除未被用户改写的文件且无需管理员权限。

## Changed

- `./run.sh check` 增加 `history-automation-status` 内部阶段，以专用有类型视图只查 active snapshot 与已经持久化的
  due/reminder/claim，不解码逐股 checkpoint 或 universe；它不访问供应商、不重新计算成熟日数、不创建控制库，
  公开 JSON 固定投影 `automatic_model_update=false`。
- Linux/macOS、PowerShell 启动脚本同步公开安装和卸载入口；定时任务绕过可能安装依赖的启动脚本，直接调用固定
  虚拟环境 Python 和绝对配置，因此计划执行期间不会静默联网安装或升级。
- wheel 现在携带三平台任务模板，仓库外资源验收同步读取这些文件。权威工程设计、策略回溯、实施计划和 README
  已同步任务 F 的运行、降级、安全和下一阶段边界；评分公式、候选、冻结、DeepSeek 预算和默认 V1 均未改变。

## Fixed

- 修复阶段 E 后只能手工记忆执行下载、休眠或关机后无补跑的问题。
- 修复同一训练 due 在 15:10、20:30、手工重叠和进程重启后可能重复弹出的问题；持久 claim 先于外部通知副作用。
- 修复无桌面通知服务可能把成功同步误报失败的问题；现在只记录 `notification_degraded`。
- 修复长时计划任务没有有界日志、`check` 看不到当前训练到期与当天提醒状态的问题。

## Removed

- 未删除历史归档、checkpoint、模型或旧 generation；未安装任何真实宿主任务，也未调用训练、切换档位、重启服务
  或修改生产冻结。Web、HTTP 和盘中生产调度器继续不触发离线历史同步。

## Verification

- 132 项定向领域、控制库、应用状态、自动化运行、三平台安装、CLI/启动脚本、架构与文档契约中 131 项通过、
  1 项因沙箱 socket 权限跳过；覆盖同日重复、
  原子竞争、进程重启、跨日 due、通知服务缺失、手工/计划重叠、轮转与股票明细脱敏、确认取消、幂等安装卸载、
  外来文件保护，以及 Windows XML/macOS plist 原生解析。
- 受影响文件严格 Ruff 与 mypy 通过；`git diff --check` 通过。`systemd-analyze calendar` 已原生接受两个上海时区
  表达式；完整 user unit 验证在当前沙箱因 socket 权限被跳过，而非模板语法失败。
- `make package` 与仓库外 wheel 安装专项通过：`trader-cli validate-config`、`pip check` 和 10 个资源读取成功，
  包括四个三平台任务模板；首次隔离构建仅因沙箱禁止联网获取 setuptools 失败，获授权后同一命令重跑成功。
- 真实 Linux 安装入口已显示绝对 service/timer 路径和用户级 `systemctl` 命令；输入 `n` 后返回 `cancelled`，并核对
  service、timer 与托管回执均未创建。未对宿主调度器做写操作。
- A–G 大任务的唯一一次全量 `make format-check/lint/type-check/test/package` 仍按计划留到阶段 G，任务 F 不把
  未运行的总门禁写成通过。

## Residual Risks

- 为避免未经确认修改用户操作系统，本批没有实际注册当前机器任务；Linux unit 的无权限静态核验、Windows
  Task Scheduler 与 macOS launchd 的真机安装/休眠唤醒证据需由相应宿主执行确认后的安装命令获得。
- 当前工作机仍没有可训练 active snapshot，真实数小时全市场下载、真实重训、2048 MiB RSS 和最终 V3 运行验收
  属于阶段 G；本批不以 fixture 代替这些外部证据。
- `Regression-Key: zero-argument-history-snapshot-training-alignment`。
