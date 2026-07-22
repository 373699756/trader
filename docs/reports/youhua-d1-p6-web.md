# youhua D1 P6/Web 盘点报告

状态：D1.x 已完成 P6 发布、SSE 和 Web 增量更新盘点；未进入 D2.x，等待 A 发布
`CONTRACT_BASE` 与 G1。

## 1. 工作树封存

| 项 | 值 |
| --- | --- |
| codex_and_phase | D1.x |
| branch | `branch` |
| upstream | `origin/branch` |
| base_commit | `45bd2fab992d36eb873b7c448fbd9739f0cad43c` |
| upstream_commit | `45bd2fab992d36eb873b7c448fbd9739f0cad43c` |
| start_worktree | 存在非 D 未跟踪交付物：`tests/fixtures/market_data/` |
| D 本次修改 | 仅新增本报告 |
| P1-P3 / DeepSeek / 公共接线 | 未执行、未修改 |

## 2. D1.1 发布盘点

| 范围 | 当前实现 | D2 关注点 |
| --- | --- | --- |
| P6 当前/历史热索引 | `src/trader/application/published_snapshots.py` 的 `PublishedSnapshotIndex` | 保持当前视图和驻留历史热读不访问 SQLite/文件 |
| 快照读端口 | `src/trader/application/ports/snapshots.py` 的 `PublishedSnapshotReadPort` | 等 A 冻结公共端口后再适配 |
| 当前查询投影 | `src/trader/application/queries.py` | 保持 HTTP 只读，不现场抓行情、评分、DeepSeek 或写盘 |
| 冻结/检查点/overlay 流程 | `src/trader/application/snapshot_workflow.py` | D 不修改冻结公共流程；只消费 A 冻结后的 projection/overlay 事件 |
| 持久化冷读 | `src/trader/infra/persistence/snapshots.py` 及 snapshot primitives/items/replay | 历史冷读按日期 single-flight，不伪装不完整日期 |

当前 P6 特征：

- `PublishedSnapshotIndex.initialize()` 预载最近完整三策略日期。
- 当前视图、驻留历史和 cold date 缓存均受锁保护。
- cold date 使用 `Future` 按交易日合并并预取 today/tomorrow/d25。
- 单视图上限默认为 `160 KiB`，状态暴露 current/resident/cold/inflight 和拒绝计数。
- overlay 以 `snapshot_id` 匹配，错配 overlay 不进入当前查询结果。

## 3. D1.2 SSE/Web 盘点

| 范围 | 当前实现 | D2 关注点 |
| --- | --- | --- |
| SSE publisher | `src/trader/application/publisher.py` | 有界订阅队列、慢客户端丢弃、历史 replay |
| SSE route | `src/trader/web/sse.py` 与 `src/trader/web/routes.py` | `Last-Event-ID`、过期/超前游标转 `resync_required` |
| patch 投影 | `src/trader/application/delivery_patch.py` | 当前 patch 使用 `schema_version=2`；需 A 冻结是否改为 `patch_schema_version=2` |
| Web API envelope | `src/trader/web/schemas.py`、`src/trader/web/serializers.py` | 当前 HTTP envelope 为 `v3` |
| DOM 增量更新 | `src/trader/web/static/dashboard.js`、`render.js` 和 CSS | 当前推荐 patch 整体替换 items，overlay patch 更新报价；D2 需细化行级 CAS 和删除 |

当前 SSE/Web 特征：

- `SnapshotPublisher` 使用单调 `sequence` 作为 SSE id。
- `events_after()` 对过旧和超前 cursor 返回 `None`，SSE 端转 `resync_required`。
- 订阅者队列满时从 subscriber 集合移除并累加 `dropped_subscribers`。
- 浏览器用 `EventSource`；SSE 正常时停轮询，断线后 15 秒轮询和重连。
- 浏览器保存 `lastEventId`，`recommendation_patch` 基于 `base_snapshot_id` 不匹配触发完整 GET。
- 当前 DOM 行身份主要基于 `data-code`，尚未显式包含 `strategy + trade_date + view + code`。

## 4. D1.3 热路径基线

无生产代码变更，因此只有当前状态基线。

| 项 | 观测结果 |
| --- | --- |
| current GET | p50 `0.306ms`，p95 `0.374ms`，max `0.445ms` |
| ETag 304 | p50 `0.276ms`，p95 `0.327ms`，max `0.425ms` |
| recommendation dates GET | p50 `0.240ms`，p95 `0.302ms`，max `0.374ms` |
| SSE enqueue | p50 `0.002ms`，p95 `0.005ms`，max `0.016ms` |
| 当前 GET 持久化访问 | `frozen_loads=0`，`overlay_loads=0` |
| P6 状态 | `current_views=1`，`maximum_views=72`，`maximum_view_bytes=163840` |
| 正常更新完整 GET 次数 | 当前 JS 对 recommendation patch 不触发完整 GET；base mismatch/resync 才 GET |

桌面基线截图：

- `/tmp/trader-d1-1280x720.png`
- `/tmp/trader-d1-1440x900.png`
- `/tmp/trader-d1-1920x1080.png`

三档 not_ready 页面均可渲染，未见白屏或明显重叠。该基线未覆盖真实推荐行、详情抽屉和长
错误文本。

## 5. D1.4 接口申请

请求 A 在 G1/CONTRACT_BASE 冻结以下最小接口；D2 只消费唯一公共版本，不自行修改公共 port。

### 5.1 projection event

必需字段：

- `patch_schema_version`
- `event_id` 或单调 `sequence`
- `projection_version`
- `base_projection_version`
- `etag`
- `snapshot_id`
- `strategy`
- `trade_date`
- `view`
- `phase`
- `published_at`
- `strategy_version`
- `fusion_mode`
- `stale`
- `frozen`
- `degraded_reasons`
- `filtered_count`
- `upserts`
- `removed_codes`

CAS 规则：

- `base_projection_version` 与浏览器当前 projection 不一致时必须 resync。
- 冻结 projection 优先于同策略同日迟到草稿。
- `etag` 用于完整 GET 后的身份校验，不代替 projection CAS。

### 5.2 overlay event

必需字段：

- `patch_schema_version`
- `event_id` 或单调 `sequence`
- `projection_version` 或 `snapshot_id`
- `overlay_version`
- `strategy`
- `trade_date`
- `observed_at`
- `closing`
- `quotes`

`quotes` 只允许包含：

- `code`
- `price`
- `pct_change`
- `source`
- `source_time`
- `quote_data_version`
- `data_age_seconds`（可选，由 A 决定是否服务端计算）

overlay 不能改变锚点价、评分、动作、排名、冻结身份或冻结哈希。

### 5.3 resync reason

请求冻结枚举：

- `cursor_expired`
- `cursor_ahead`
- `cursor_gap`
- `slow_subscriber`
- `base_mismatch`
- `schema_mismatch`
- `identity_mismatch`

## 6. 已知失败与风险

- 完整 `tests/contract/test_v2_web_api.py` 当前有 2 个既有失败：
  - `test_historical_snapshot_has_exact_identity_and_current_quote_overlay`
  - `test_historical_snapshot_uses_current_snapshot_quote_without_overlay`
- 失败现象：历史页未拿到当前同日 quote overlay/current snapshot quote。
- 当前 D1 未修复上述失败，因为阶段 1 不改生产代码，且 G1 未发布。
- patch schema 当前与 API schema 字段同名 `schema_version`，D2 需要按 A 冻结结果消除歧义。
- `.agents/` 和 `.codex/` 当前不可写且被 `.gitignore` 忽略，因此本报告放入 `docs/reports/`。

## 7. 验证记录

| 命令 | 结果 |
| --- | --- |
| `.venv/bin/python -m pytest tests/unit/application/test_publisher.py tests/unit/application/test_published_snapshots.py -q` | 通过，9 passed |
| `.venv/bin/python -m pytest tests/contract/test_v2_web_api.py -q -k 'not historical_snapshot_has_exact_identity_and_current_quote_overlay and not historical_snapshot_uses_current_snapshot_quote_without_overlay'` | 通过，26 passed |
| `git diff --check` | 通过 |
| headless Chrome 三档截图 | 通过生成截图；not_ready 页面非白屏 |

## 8. 标准交接包

```text
codex_and_phase
Codex D / D1.x

base_commit
45bd2fab992d36eb873b7c448fbd9739f0cad43c

head_commit_or_patch
未提交；公共工作树内 D 只新增 docs/reports/youhua-d1-p6-web.md 交接报告。

owned_paths_changed
docs/reports/youhua-d1-p6-web.md

contract_assumptions
P6 热读不访问持久化；SSE 使用单调事件 ID 和 Last-Event-ID；HTTP envelope v3；patch
协议等待 A 冻结 projection/overlay v1。

schema_or_migration_changes
无。

tests_run_and_results
见本报告第 7 节。

performance_before_after
无代码变更；见本报告第 4 节当前基线。

known_failures_and_risks
完整 Web contract 仍有 2 个历史当前报价失败；D2 等 G1 后处理或按 A 分派处理。

requested_interface_changes
projection/overlay event、patch schema、CAS/version、resync reason 最小接口申请见第 5 节。

ready_for_gate
yes
```
