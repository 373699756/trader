# v2 运行手册

## 启动前检查

1. 使用 Python 3.10-3.14 创建虚拟环境。
2. 执行 `./run.sh validate-config` 或对应 Windows 命令。
3. 如启用 DeepSeek，优先在进程环境设置 `DEEPSEEK_API_KEY`；也可把单行密钥或 `DEEPSEEK_API_KEY=...` 写入项目根目录 `.deepseek_key`，POSIX 权限必须为 `600`。自定义文件位置使用 `DEEPSEEK_API_KEY_FILE`，环境密钥始终优先；文件不得提交到仓库。
4. 确认目标端口只绑定回环地址，且 `.runtime/v2` 所在磁盘可写。

## 启动与停止

执行 `./run.sh`、`.\run.ps1` 或 `run.bat`。入口先初始化 SQLite 和恢复未完成 manifest，再启动流水线和 Web。按 `Ctrl+C` 停止；关闭顺序为停止调度和事件接收、由合并线程排空有界队列与冻结写入、停止计算 worker、最后停止持久化单写线程。调度或流水线关闭超时会写入 `last_error`；入口先停止流水线以解除调度依赖，再等待运行中 I/O 按其显式 timeout 退出，不能带残留 worker 返回。

调度等待会对齐 09:15、09:30、09:36、10:30、11:20、13:00、14:20、14:48、14:49:50、14:50 和 15:00 边界。11:20/14:50 冻结、14:48 DeepSeek 截止和 15:00 收盘报价使用交易日内稳定幂等单点；调度延迟或重启错过计划秒时在首次后续 tick 补提交。14:49:50 最终候选报价只允许在 14:50 前补提交，冻结后不使用迟到报价重建候选；冻结补偿只提交截止前有效草稿，不以迟到数据重算冻结。

运行配置现使用 schema v3；`pipeline.cadence_seconds` 是第 6 节刷新表的直接配置形态，缺任务、缺阶段或数值漂移都会在启动前失败。升级旧配置时必须整体切换仓库随附的 v3 文件，不能只手工追加部分字段。`/api/status` 的 `dependencies.cadence` 展示当前间隔、下一到期时间和运行中任务，动态计数使用 `cadence_<task>_planned/submitted/skipped_*`；同类任务仍运行时出现 skipped 属于预期背压，不会在随后补跑。

入口会在 `.runtime/v2/server.lock` 获取非阻塞进程锁；同一运行目录已有服务时，第二个进程立即报错退出。不要绕过入口重复创建 supervisor。

## 状态检查

```bash
curl -sS http://127.0.0.1:5000/api/status
```

重点检查：

- `runtime_started` 和 `phase`。
- 行情活动来源、失败次数和熔断状态。
- `market_data.freshness` 中全市场、候选和 TopK 的 `fresh/stale/degraded`、年龄及当前阶段阈值；`market_quote_age`、`candidate_quote_age` 和 `topk_quote_age` 的 P50/P95/最大值。
- 事件队列容量、保留容量、深度、合并数、拒绝数和重放数。
- `worker_pools` 中数据、标准化、策略、DeepSeek、long、合并和持久化 worker 的配置数、运行状态、在途数与拒绝数。
- DeepSeek 配置状态、缓存与预算 used/remaining。
- `publisher` 中 SSE 发布及 today 报价到评分发布的 P50/P95/最大延迟与目标。
- `last_error`、各策略快照时间、stale、frozen 和 fusion_mode。

## 常见降级

- 交易日历不可用：系统 fail-closed，不猜测交易日；恢复日历缓存或网络后重启。
- 东方财富失败：自动回退新浪；全市场同源并发请求由 single-flight 合并，连续失败 3 次熔断 60 秒，超时后只放行一个恢复探针。腾讯候选报价串行限流，失败、超时、返回空值或乱序旧版本时保留最近有效行情和 overlay，不清空推荐。
- `TopK live overlay degraded: data source task exceeded its batch deadline`：同时检查 `dependencies.market_data.sources.tencent` 与 `dependencies.worker_pools.data`。腾讯成功率正常且 P95 明显低于 3 秒时，若 `urgent_workers` 不为 1，说明运行进程尚未加载紧急 lane 修复，应正常停止后重启；`urgent_workers=1` 时仍超时则按真实腾讯网络超时处理，保留最近 overlay 并观察熔断恢复，不得放宽 3 秒总截止掩盖慢源。
- DeepSeek 未配置、超时或预算耗尽：整版使用 `local_degraded`，本地推荐和 Web 继续工作。
- `deepseek_incomplete`：先在 `/api/status` 核对配置、最近错误和阶段预算。网络或供应商恢复后，尚有阶段额度的后续复核会继续执行；失败物理请求已计入 188 次上限，不得删除预算库或返还额度，当前阶段额度耗尽时保留本地结果并等待下一合法阶段或下一交易日。
- `tomorrow_tail_data_incomplete`：核对分钟来源覆盖和候选数；该输入按后续刷新自动重试，覆盖恢复后警告自动消失，不得用昨日分钟数据伪装完整。
- `d25_structured_research_incomplete`：核对财务、公告、质押和解禁覆盖。成功结果使用 10 分钟缓存，周期刷新仅补缺失或过期代码；失败结果短期负缓存后重试，避免固定排序后的候选长期饥饿。
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

第 25 节最终验收逐项状态和证据入口见 `docs/operations/final-acceptance.md`。真实交易日必须至少每 5 秒采集一次 `/api/status` 中的 `dependencies.market_data.topk_quote_age`，只纳入正常交易阶段且 `sample_count > 0` 的横截面 P95，按 nearest-rank 方法汇总全天样本并要求最终 P95 不超过 10 秒；DeepSeek 使用 `physical_call_acceptance` 区分已通过、未配置、无候选、预算耗尽和未产生物理请求。

冻结后对 today、tomorrow 和 d25 的每个文件执行离线复算：

```bash
trader-cli --config /absolute/path/to/config/v2/runtime.json \
  verify-freeze --snapshot /absolute/path/to/.runtime/v2/frozen/today/YYYY-MM-DD/SNAPSHOT.json
```

只有命令返回 `status=verified`，且市场输入、候选输入计数均非零，才满足过滤、评分、风险、veto 和排名可复算门禁。旧格式快照保持可读，但因没有 replay input 不得用于该门禁。

## 回退

回退必须停止 v2 后切换到完整 v1 tag `v1-rollback-20260717`（提交 `86e3b2b1308e454adee1e1cc43fa0c8997e8bf2b`），并继续使用旧 `.runtime`。不得只恢复部分 Python 文件，也不得把 `.runtime/v2` 数据写回旧库。记录回退原因、时间和最后成功冻结快照。
