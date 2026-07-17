# v2 运行手册

## 启动前检查

1. 使用 Python 3.10-3.14 创建虚拟环境。
2. 执行 `./run.sh validate-config` 或对应 Windows 命令。
3. 如启用 DeepSeek，只在进程环境设置 `DEEPSEEK_API_KEY`。
4. 确认目标端口只绑定回环地址，且 `.runtime/v2` 所在磁盘可写。

## 启动与停止

执行 `./run.sh`、`.\run.ps1` 或 `run.bat`。入口先初始化 SQLite 和恢复未完成 manifest，再启动流水线和 Web。按 `Ctrl+C` 停止；关闭顺序为停止调度、排空有界队列、等待冻结写入、停止 worker。

入口会在 `.runtime/v2/server.lock` 获取非阻塞进程锁；同一运行目录已有服务时，第二个进程立即报错退出。不要绕过入口重复创建 supervisor。

## 状态检查

```bash
curl -sS http://127.0.0.1:5000/api/status
```

重点检查：

- `runtime_started` 和 `phase`。
- 行情活动来源、失败次数和熔断状态。
- 事件队列深度、合并数和拒绝数。
- DeepSeek 配置状态、缓存与预算 used/remaining。
- `last_error`、各策略快照时间、stale、frozen 和 fusion_mode。

## 常见降级

- 交易日历不可用：系统 fail-closed，不猜测交易日；恢复日历缓存或网络后重启。
- 东方财富失败：自动回退新浪；腾讯候选报价失败时保留最近有效快照。
- DeepSeek 未配置、超时或预算耗尽：整版使用 `local_degraded`，本地推荐和 Web 继续工作。
- SSE 中断：浏览器每 15 秒轮询；SSE 恢复后自动停止轮询。
- 冻结 manifest 为 staged：重启扫描会幂等提交完整文件，损坏或缺失项进入 quarantine。
- 冻结高优先级事件为 pending/running：重启会从运行库恢复并重新入保留队列，已冻结策略由幂等门控跳过。

## 数据与备份

运行库、发布快照、冻结快照和隔离文件都位于 `.runtime/v2`。备份时先停止进程，再整体复制该目录，保留 SQLite WAL 相关文件。旧 `.runtime` 只用于 v1 回退，不得由 v2 写入。

## 发布前影子验收

先运行固定输入的完整日冻结对照：

```bash
.venv/bin/python -m pytest -q \
  tests/integration/test_v2_shadow_cutover.py::test_recorded_full_day_shadow_is_deterministic_and_freezes_real_repository
```

该门禁从 09:20 推进到 15:00，在两个隔离运行目录使用真实 SQLite/JSON 仓库，要求 today、tomorrow、d25 manifest 全部 committed、long 不冻结，并要求两次运行的所有 JSON SHA-256 一致。

生产发布仍需一个真实交易日从 09:15 前启动并持续到 15:00。留证必须包含：

- 启动提交、配置版本、开始/结束时间和 `Asia/Shanghai` 时区。
- 11:20 today 与 14:50 tomorrow/d25 的 committed manifest、SHA-256 和记录数。
- `/api/status` 的来源成功/失败/熔断、行情年龄、队列和 DeepSeek 预算摘要。
- 1280x720、1440x900、1920x1080 三档桌面浏览器无溢出、重叠和脚本异常的记录。
- v1 `.runtime` 未被修改的前后校验，以及是否满足发布或回退条件的结论。

缺少任一项时只能维持“repository cutover gates complete”，不得宣告生产发布完成。

## 回退

回退必须停止 v2 后切换到完整 v1 tag `v1-rollback-20260717`（提交 `86e3b2b1308e454adee1e1cc43fa0c8997e8bf2b`），并继续使用旧 `.runtime`。不得只恢复部分 Python 文件，也不得把 `.runtime/v2` 数据写回旧库。记录回退原因、时间和最后成功冻结快照。
