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

## 9. D2 P6/SSE/DOM 实现报告

状态：D2.1-D2.7 已在 D owner 范围内完成；未修改 P1-P3、DeepSeek、bootstrap、publisher
公共接口或 A 的公共 port/schema 文件。G2 尚未发布，等待 A 汇总四方阶段 2 交接包。

### 9.1 D2 变更范围

| 子项 | 结果 |
| --- | --- |
| D2.1 P6 index | `PublishedSnapshotIndex.publish()` 保持四策略 current pin，不再让旧日期冻结历史覆盖同策略较新当前视图；旧日期仍可进入完整三策略 resident history。 |
| D2.2 热读 | 当前/驻留历史 GET 继续走 P6；修复后历史页读取当前报价 overlay/current snapshot quote 的 Web 合约通过。 |
| D2.3 持久化分流 | 未改公共冻结/检查点/持久化流程；普通 patch 仍只经 P6/SSE 投影，不新增 HTTP 写盘路径。 |
| D2.4 SSE patch | `recommendation_patch`/`overlay_patch` 增加 `patch_schema_version=2`、projection version、base projection version、`removed_codes`、overlay `data_age_seconds`；保留旧 `schema_version=2` 兼容现有消费者。 |
| D2.5 浏览器状态机 | Web 保存 `projectionVersion`，按 base/current projection CAS；patch 不匹配触发 ETag GET resync；行身份由 `strategy + trade_date + view + code` 派生并写入 DOM dataset。 |
| D2.6 故障处理 | SSE 区分 `cursor_expired` 与 `cursor_ahead`；慢客户端继续触发 `slow_subscriber`；overlay projection 不匹配触发完整 resync。 |
| D2.7 单域验证 | D 定向单测、Web 合约、JS 语法、Ruff/mypy 子集、热路径和三档截图已执行，结果见下。 |

### 9.2 D2 修改文件

```text
src/trader/application/published_snapshots.py
src/trader/application/delivery_patch.py
src/trader/web/sse.py
src/trader/web/static/dashboard.js
src/trader/web/templates/index.html
tests/unit/application/test_publisher.py
tests/unit/application/test_published_snapshots.py
tests/contract/test_v2_web_api.py
tests/contract/test_v2_app_factory.py
docs/reports/youhua-d1-p6-web.md
```

未修改：

- `src/trader/application/publisher.py`
- `src/trader/bootstrap.py`
- `src/trader/application/ports/*`
- `src/trader/infra/market_data/*`
- `src/trader/infra/deepseek/*`

### 9.3 D2 热路径基线

无外部网络、空 P6 fixture、Flask test client、100 次测量：

| 项 | D1 基线 | D2 结果 |
| --- | ---: | ---: |
| current GET p95 | `0.374ms` | `0.421ms` |
| ETag 304 p95 | `0.327ms` | `0.371ms` |
| recommendation dates GET p95 | `0.302ms` | `0.302ms` |
| SSE enqueue p95 | `0.005ms` | `0.005ms` |
| 当前 GET 持久化访问 | `frozen_loads=0`，`overlay_loads=0` | `frozen_loads=0`，`overlay_loads=0` |

当前空 fixture 抖动级别在毫秒以下；真实推荐行和真实 pipeline RSS/USS 仍需阶段 3/4 集成门禁。

### 9.4 D2 桌面基线

截图路径：

- `/tmp/trader-d2-1280x720.png`
- `/tmp/trader-d2-1440x900.png`
- `/tmp/trader-d2-1920x1080.png`

三档 not_ready 页面均可渲染；抽查 1280x720 未见白屏、页面级横向溢出或明显重叠。该基线
仍未覆盖真实推荐行、详情抽屉、长错误文本和持续动态 patch。

### 9.5 D2 验证记录

| 命令 | 结果 |
| --- | --- |
| `.venv/bin/python -m pytest tests/unit/application/test_publisher.py tests/unit/application/test_published_snapshots.py tests/contract/test_v2_web_api.py tests/contract/test_v2_app_factory.py -q` | 通过，41 passed |
| `.venv/bin/python -m pytest tests/contract/test_delivery_contract.py tests/contract/test_youhua_contract_base.py -q` | 通过，6 passed |
| `node --check src/trader/web/static/dashboard.js` | 通过 |
| `.venv/bin/ruff format --check <D changed python files>` | 通过，7 files already formatted |
| `.venv/bin/ruff check <D changed python files>` | 通过 |
| `.venv/bin/mypy src/trader/application/published_snapshots.py src/trader/application/delivery_patch.py src/trader/web/sse.py` | 通过 |
| `git diff --check` | 通过 |
| `make format-check` | 失败，非 D 文件 `src/trader/infra/deepseek/evidence_router.py`、`src/trader/infra/market_data/columnar.py`、`tests/component/test_youhua_deepseek_c2.py` 需要格式化 |
| `make lint` | 失败，非 D 文件 `src/trader/infra/deepseek/schema.py`、`tests/component/test_youhua_deepseek_c2.py`、`tests/unit/test_v17_columnar_changes.py` 存在 lint 错误 |
| `make test` | 失败，8 项非 D 已知/并行失败，集中在 DeepSeek C2、application port `Mapping[str, object]`、bootstrap 构造参数和 final candidate cadence |

### 9.6 D2 标准交接包

```text
codex_and_phase
Codex D / D2.x

base_commit
45bd2fab992d36eb873b7c448fbd9739f0cad43c

head_commit_or_patch
未提交；公共工作树内 D 修改见 9.2。

owned_paths_changed
src/trader/application/published_snapshots.py
src/trader/application/delivery_patch.py
src/trader/web/sse.py
src/trader/web/static/dashboard.js
src/trader/web/templates/index.html
tests/unit/application/test_publisher.py
tests/unit/application/test_published_snapshots.py
tests/contract/test_v2_web_api.py
tests/contract/test_v2_app_factory.py
docs/reports/youhua-d1-p6-web.md

contract_assumptions
基于 G1 的 p4p5_p6_projection_event_v1、p6_overlay_event_v1 和 p6_resync_reason_v1；
D2 未创建第二套公共 port/schema。

schema_or_migration_changes
无持久化 schema/migration。SSE patch payload 增加 patch_schema_version/projection 字段，
保留 schema_version 兼容字段。

tests_run_and_results
见 9.5。

performance_before_after
见 9.3；D2 空 fixture 热路径未见明显回退。

known_failures_and_risks
全局 make format-check/lint/test 仍受非 D 文件和并行 C/A/B 变更影响；真实推荐行、持续
动态 patch、RSS/USS 和完整 pipeline 仍需 G3/G4 集成验收。

requested_interface_changes
无新增接口申请；如 A2 后续移除兼容 schema_version 字段，D 可按唯一公共 schema 再收敛。

ready_for_gate
yes
```

## 10. D3 集成态 P6/Web 复验报告

状态：D3.1-D3.4 已在 A3 集成提交后完成；等待 A 汇总 G3。D 本轮未提交、未推送。

### 10.1 工作树封存

| 项 | 值 |
| --- | --- |
| codex_and_phase | Codex D / D3.x |
| branch | `branch` |
| upstream | `origin/branch` |
| base_commit | `812de4390d42213c684b4e2096453c4775903443` |
| upstream_commit_at_start | `812de4390d42213c684b4e2096453c4775903443` |
| start_worktree | A3 交接后读取到 `HEAD == @{upstream}`；D3 执行期间出现并发非 D 变更，未修改其文件。 |
| D 本次修改 | `src/trader/application/delivery_patch.py`、`src/trader/application/publisher.py`、`tests/unit/application/test_publisher.py`、本报告 |
| P1-P3 / DeepSeek / 公共接线 | 未执行、未修改；`publisher.py` 仅为 SSE 差量 patch 保存同策略基线快照，无公共端口变更。 |

并发非 D 变更观测：`CHANGELOG.md`、`src/trader/infra/deepseek/reviewer.py`、
`src/trader/infra/deepseek/reviewer_requests.py`、`src/trader/infra/deepseek/schema.py`、
`tests/contract/test_delivery_contract.py`、`tests/contract/test_youhua_contract_base.py`、
`docs/reports/youhua-g3-gate-review.md`、`tests/component/test_youhua_deepseek_c3.py`、
`tests/fixtures/market_data/youhua_b3/`。

### 10.2 D3.1 P4/P5 重发布与 patch

复验发现 A3 集成态 `recommendation_patch` 仍使用 `replace=True` 并发送完整 `upserts`，不满足
“P4/P5 重发布只更新相应股票、全局选择和 patch”。D 在 D 租约内补齐内部差量投影：

- 首次发布或策略/交易日切换：`replace=True`，发送完整当前 projection。
- 同策略同交易日后续发布：`replace=False`，`base_projection_version` 指向上一 projection。
- 个股内容、排名或全局选择变化进入 `upserts`；退出当前选择的股票进入 `removed_codes`。
- 浏览器现有 CAS 继续使用 `base_projection_version`；匹配时合并 patch，不匹配时走 ETag GET resync。

新增回归 `test_publisher_emits_incremental_snapshot_patch_after_base_snapshot` 覆盖：base
`600001/600002` 到 next `600001(rank changed)/600003` 时，第二个 patch 只包含
`upserts=["600001","600003"]`、`removed_codes=["600002"]`、`replace=False`。

### 10.3 D3.2 P6 热读与 single-flight

无外部网络、Flask test client、100 次测量：

| 项 | 观测结果 |
| --- | --- |
| current GET | p50 `0.409ms`，p95 `0.527ms`，max `2.667ms` |
| ETag 304 | p50 `0.286ms`，p95 `0.348ms`，max `0.427ms` |
| recommendation dates GET | p50 `0.242ms`，p95 `0.289ms`，max `0.374ms` |
| SSE enqueue | p50 `0.023ms`，p95 `0.037ms`，max `0.185ms` |
| 当前 GET 持久化访问 | `frozen_loads=0`，`overlay_loads=0` |
| P6 状态 | `current_views=1`，`maximum_views=72`，`maximum_view_bytes=163840` |
| publisher 状态 | `last_sequence=100`，`dropped_subscribers=0`，SSE/today score p95 目标均满足 |

`tests/unit/application/test_published_snapshots.py` 覆盖 resident 热读不访问持久化，以及 cold
date 两线程并发读同一日期时 `cold_loads=1`、`cold_coalesced=1`、一次预取三策略。

P6 原子替换内存峰值采样：连续发布 200 个 18 行 today 快照，`rss_before=31812KiB`、
`rss_after=32836KiB`、`rss_peak=32836KiB`、`delta_peak=1024KiB`、`published=200`。

### 10.4 D3.3 SSE/Web/冻结/overlay/ETag

已复验：

- patch：`recommendation_patch` v2 保留 projection/base 字段；新增同日差量 patch 回归。
- 游标：过期 cursor 与超前 cursor 均返回 resync；超前场景不伪造 replay。
- 慢客户端：订阅队列满时移除 subscriber 并累加 `dropped_subscribers`。
- 冻结：冻结后迟到草稿不覆盖当前正式视图；14:50/15:00 后补算路径由 integration subset 覆盖。
- overlay：overlay 不重发快照；只应用到匹配 projection；错配通过 Web CAS/ETag 完整 resync。
- ETag：当前 GET 支持 304；overlay 后 ETag 变化由 Web API contract 覆盖。
- 正常更新零完整 GET：浏览器在 base projection 匹配时本地合并 `upserts/removed_codes`；仅 mismatch、
  cursor resync 或 overlay projection mismatch 触发完整 GET。

桌面 CDP 精确视口检查：

| 视口 | 截图 | 结果 |
| --- | --- | --- |
| 1280x720 | `/tmp/trader-d3-cdp-1280x720.png` | `horizontalOverflow=false`，`overlaps=[]`，`browserErrors=[]` |
| 1440x900 | `/tmp/trader-d3-cdp-1440x900.png` | `horizontalOverflow=false`，`overlaps=[]`，`browserErrors=[]` |
| 1920x1080 | `/tmp/trader-d3-cdp-1920x1080.png` | `horizontalOverflow=false`，`overlaps=[]`，`browserErrors=[]` |

### 10.5 D3 验证记录

| 命令 | 结果 |
| --- | --- |
| `.venv/bin/python -m pytest tests/unit/application/test_publisher.py tests/unit/application/test_published_snapshots.py tests/contract/test_v2_web_api.py tests/contract/test_v2_app_factory.py -q` | 通过，42 passed |
| `.venv/bin/python -m pytest tests/integration/test_v2_pipeline.py -q -k 'virtual_trading_day or frozen_topk or freeze_accepts_exact_quote_age_boundary or after_close_persists_current_run_p6 or current_quote_recovery'` | 通过，7 passed |
| `node --check src/trader/web/static/dashboard.js` | 通过 |
| `.venv/bin/python -m ruff check src/trader/application/delivery_patch.py src/trader/application/publisher.py tests/unit/application/test_publisher.py` | 通过 |
| `.venv/bin/python -m mypy src/trader/application/delivery_patch.py src/trader/application/publisher.py tests/unit/application/test_publisher.py` | 通过 |
| `make type-check` | 通过，162 source files |
| `git diff --check` | 通过 |
| `make format-check` | 失败，非 D 并发文件 `tests/component/test_youhua_deepseek_c3.py` 需格式化 |
| `make lint` | 失败，非 D 并发文件 `src/trader/infra/deepseek/reviewer_requests.py` import 顺序 |
| `make test` | 失败，非 D 并发用例 `tests/component/test_youhua_deepseek_c3.py::test_c3_same_stock_three_strategies_share_one_raw_facts_request` 把 `mappingproxy` 当作 `dict` 断言 |

### 10.6 D3 标准交接包

```text
codex_and_phase
Codex D / D3.x

base_commit
812de4390d42213c684b4e2096453c4775903443

head_commit_or_patch
未提交；公共工作树内 D 修改见 10.1。

owned_paths_changed
src/trader/application/delivery_patch.py
src/trader/application/publisher.py
tests/unit/application/test_publisher.py
docs/reports/youhua-d1-p6-web.md

contract_assumptions
沿用 A3 集成后的 SSE patch v2、projection/base、overlay、resync 和 P6 热索引契约；
未新增公共 port/schema。

schema_or_migration_changes
无持久化 schema/migration。

tests_run_and_results
见 10.5。

performance_before_after
见 10.3；D3 增量 patch 后，热读和 SSE enqueue 仍保持毫秒内。

known_failures_and_risks
全局 format/lint/test 失败均归属并发非 D 变更；D3 桌面检查仍基于 not_ready 页面，真实推荐行、
详情抽屉和长错误文本仍需 G3/G4 集成验收。

requested_interface_changes
无。

ready_for_gate
yes
```
## 11. D4 P6/SSE/API/桌面全量验收报告

状态：D4.1-D4.4 在 G3 发布提交后完成；D owner 范围内的性能、传输、状态机与桌面门禁
均通过。G4 仍由 A 汇总，D4 不提前执行 D5。

### 11.1 工作树封存与范围

| 项 | 值 |
| --- | --- |
| codex_and_phase | Codex D / D4.x |
| branch | `branch` |
| base_commit | `7a8a0282d025cbc23fffff5736e94c2d1bf883e0` |
| upstream_commit_at_start | `7a8a0282d025cbc23fffff5736e94c2d1bf883e0` |
| start_worktree | clean，且 `HEAD == @{upstream}` |
| D 范围 | P6 current pin、publisher/SSE、Web patch/ETag/resync、桌面夹具与 D4 测试 |
| 非 D 并发范围 | A4/B4/C4 在 D4 执行期间写入各自报告、行情、DeepSeek、配置和契约文件；D 未修改或暂存这些变更 |

### 11.2 D4.1-D4.2 延迟门禁

固定 18 行离线投影、120 次 P6 发布和各 100 次 Flask 热读的最终定向结果：

| 指标 | D4 结果 | 门限 | 结论 |
| --- | ---: | ---: | --- |
| P6 -> SSE 入队 P95 | `4.357ms` | `100ms` | 通过 |
| 权威 SSE 发布年龄 P95 | `0.000s` | `2s` | 通过 |
| 当前 API P95 | `2.382ms` | `200ms` | 通过 |
| 驻留历史 API P95 | `1.808ms` | `200ms` | 通过 |
| ETag 304 P95 | `0.797ms` | `50ms` | 通过 |
| 日期 API P95 | `1.352ms` | `100ms` | 通过 |
| 状态 API P95 | `1.758ms` | `100ms` | 通过 |

`SnapshotPublisher.status()` 新增独立 `sse_enqueue_latency`，明确记录 `target_ms=100`，原
`sse_publish_latency` 补充 `target_seconds=2`，避免用快照年龄冒充内部入队耗时。时钟与
单调计时器均可注入，统计窗口继续有界。

### 11.3 D4.3 零完整 GET 与传输节省

- 单股价格变化的 SSE 编码为 `1,133B`，同一 18 行完整 HTTP 响应为 `10,952B`，节省
  `89.655%`。
- Firefox 实际页面连接后触发两次有效增量发布：`recommendationRequests +0`、
  `recommendationFullResponses +0`、`resyncRequests +0`、每次 `patchApplied +1`，首行价格
  正确更新，页面关键区域最大布局位移 `0px`。
- 显式 `resync_required` 触发 `recommendationRequests +1`，命中 ETag 304，
  `recommendationFullResponses +0`；因此正常更新不完整 GET，只有明确 resync 才重新校验。
- 无 `Last-Event-ID`/`cursor` 的新连接从 publisher 当前序列开始，不再在完整 GET 最新投影
  后重放旧历史；显式游标的过期、超前和连续恢复语义保持不变。
- patch 要求 `patch_schema_version=2` 与兼容 `schema_version=2` 同时成立；schema、身份、
  base、TopK 或 overlay projection 错配进入有原因计数的 ETag resync，不再静默丢弃。

### 11.4 D4.4 三档桌面

使用 Firefox `152.0.4`、离线 18 行真实投影（10 条正式、8 条观察）、长错误文本、持续 SSE
和详情抽屉执行精确内容视口检查：

| 视口 | 截图 | 结果 |
| --- | --- | --- |
| 1280x720 | `/tmp/trader-d4-1280x720.png` | 无页面级横向溢出/关键区域重叠；详情抽屉含 3 个分区且完整位于视口内 |
| 1440x900 | `/tmp/trader-d4-1440x900.png` | 18 行身份完整，无页面级横向溢出/关键区域重叠 |
| 1920x1080 | `/tmp/trader-d4-1920x1080.png` | 正式与观察区同时可见，无页面级横向溢出/关键区域重叠 |

三档均非白屏；长错误使用 `overflow-wrap:anywhere`；页面诊断 `browserErrors=[]`。宿主 Firefox
仍输出自身 SWGL framebuffer warning，但本次截图、DOM、WebDriver 和页面 JavaScript 均成功，
该宿主警告未形成产品门禁失败。

### 11.5 Review 修复与交接边界

Review 额外修复：

- P6 current pin 不再被同交易日迟到草稿或不同身份冻结替换，拒绝计数进入状态。
- publisher 同样拒绝较旧日期、冻结后迟到草稿和冻结身份替换，不产生 SSE 事件。
- 超过 P6 单视图上限的当前发布从静默拒绝改为显式 `ValueError`，使调用链在 publisher 前
  停止；旧 P6 保持可读。
- patch ETag 与实际 `snapshot:trade_date:view` 热读身份一致；浏览器只把匹配当前 view 的
  patch ETag 写入对应缓存。
- 浏览器 TopK 在合并后同时校验最多 18 行、股票代码唯一和正整数 rank 唯一；重复或非法
  rank 与代码错配一样进入有原因的 resync。
- 权威架构文档把 P6 -> SSE 内部入队 `100ms` 与权威发布年龄 `2s` 分离，并补齐无游标
  新连接从当前 sequence 开始的 API 语义。

A4 已登记的跨 owner 原子性仍需 A 在集成层确认：当前流水线先更新 `RuntimeState` 再调用
P6；D 已提供显式接纳失败并保证 D publisher 不自行广播，但 A 仍需决定把 P6 接纳前置或
回滚 RuntimeState/session/checkpoint。该事项不属于 D4 内部实现，保持为 G4 阻塞而不是由 D
越权修改公共 pipeline。

### 11.6 验证记录

| 命令/检查 | 结果 |
| --- | --- |
| `pytest tests/performance/test_youhua_d4_web.py -s` | D4 当前/驻留延迟与传输门限通过，数值见 11.2-11.3 |
| `pytest tests/unit/application/test_publisher.py tests/unit/application/test_published_snapshots.py` | publisher/P6 入队、冻结 pin、超限与拒绝状态通过 |
| `pytest tests/contract/test_v2_web_api.py -k 'sse_'` | 无游标新连接、过期/超前恢复和容量门通过 |
| Node dashboard state contract | schema/identity/base/overlay/TopK 决策通过 |
| Firefox 精确三档视口 | 18 行、长错误、详情抽屉、零正常 GET、304 resync、零布局跳动通过 |
| D 文件 Ruff/mypy/JS syntax | 通过 |
| `make format-check && make lint && make type-check && make test && make package` | 最终共享树五项门禁全部通过；严格债务 C901 降为 38，其余计数不变 |
| 仓库外 wheel 安装 | 可从安装目录导入 `trader`、执行 `trader-cli --help`，并读取模板及 5 项 CSS/JS/图标资源 |

### 11.7 D4 标准交接包

```text
codex_and_phase
Codex D / D4.x

base_commit
7a8a0282d025cbc23fffff5736e94c2d1bf883e0

owned_paths_changed
src/trader/application/delivery_patch.py
src/trader/application/published_snapshots.py
src/trader/application/publisher.py
src/trader/web/routes.py
src/trader/web/static/dashboard.js
src/trader/web/templates/index.html
docs/software-business-design.md
tests/contract/test_v2_app_factory.py
tests/contract/test_v2_web_api.py
tests/js/test_dashboard_d4.js
tests/performance/test_youhua_d4_web.py
tests/performance/youhua_d4_browser_fixture.py
tests/unit/application/test_published_snapshots.py
tests/unit/application/test_publisher.py
docs/reports/youhua-d1-p6-web.md
CHANGELOG.md

schema_or_migration_changes
无持久化 schema/migration；SSE patch 仍为 v2，Web envelope 仍为 v3。

performance_and_browser
见 11.2-11.4；全部 D4 门限通过。

requested_interface_changes
A 在 G4 集成层原子处理 PublishedSnapshotIndex.publish() 的接纳失败，禁止 RuntimeState、
session/checkpoint 与 P6/SSE 身份分裂。

known_failures_and_risks
D-owned 门禁无已知失败；G4 仍等待 A 完成上述跨 owner 原子接线并汇总 B4/C4/A4。

ready_for_gate
yes; D4-owned gates pass, while G4 remains blocked on the recorded A integration handoff
```
