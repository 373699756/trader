# V2 P0 基线快照（2026-07-30）

本基线覆盖现统一归档为 `V2-E0：唯一产品契约重置`（`docs/implementation-plan.md`）的
实施前快照。
目的：把当前代码、路由、持久化与运行资产归入可追踪的“四象限”。

## 1. 基线元信息

- 仓库：`/home/c/linux/trader`
- Git：
  - HEAD：`0d52522e65f0654f29e401c6e9728ac7f6c484c6`
  - `@{upstream}`：`origin/feature/tomorrow-v2`
  - 比对：一致
- 执行起点：
  - 工作树清洁（未发现未提交变更）
  - 当前分支：`feature/tomorrow-v2`
- 目标配置：
  - `config/v2/runtime.json`
  - `schema_version: 8`
  - `runtime_dir: .runtime/v17`
  - `config_version: runtime_v35_tomorrow_input_quality_free_master_2026_07_30`

## 2. 基线资产归类（先按本任务定义）

### A. 保留（保留+继续验证）

- 组合根仍为 `bootstrap.py` / `entrypoints/server.py` / `create_app()`；
  `create_app()` 无网络/数据库/文件写入副作用。
- 当前生产链依赖完整保留：
  - `src/trader/application/{pipeline.py,pipeline_workers.py,pipeline_status.py,recommendations.py,recommendation_finalization.py,current_decisions.py,published_snapshots.py}`
  - `src/trader/web/{routes.py,routes_recommendations.py,routes_events.py,routes_status.py,app.py}`
  - 运行入口 `trader-server` / `trader-cli`。
- 既有历史兼容性结构保留用于审计与核验：
  - `src/trader/infra/persistence/snapshot_replay.py`
  - `src/trader/infra/persistence/recommendation_archive.py`
  - `tests/contract/test_v17_recommendation_sections.py` 等相关契约测试。

### B. 当前生产链（现网读写对象）

- HTTP 路径与主 Web：
  - 根页面：`/`（`src/trader/web/templates/index.html`）
  - 生产 API：`/api/recommendations/<strategy>`、`/api/recommendation-dates`、`/api/status`
  - SSE：`/api/events`
- 运行仓储：
  - `.runtime/v17/runtime.sqlite3`
  - `.runtime/v17/frozen/`（today/tomorrow/d25 冻结）
  - `.runtime/v17/published/`（推荐发布快照）
  - `.runtime/v17/checkpoints/`（冻结/恢复检查点）
  - `.runtime/v17/quarantine/`

### C. tomorrow v2 已建成影子链（不改生产指针）

- 路由与展示：
  - `/v2/tomorrow`
  - `/api/v2/tomorrow/current`
  - `/api/v2/tomorrow/history`
  - `/api/v2/status`
  - `/api/v2/events`
  - `src/trader/web/templates/tomorrow_v2.html`
  - `src/trader/web/static/tomorrow_v2.css|js`
  - `src/trader/web/tomorrow_v2_serializers.py`、`tomorrow_v2_sse.py`
- 决策/证据/事件：
  - `src/trader/application/tomorrow_*` 全链路（`tomorrow_shadow*`、`tomorrow_views*`）
  - `src/trader/application/tomorrow_events.py`
  - `src/trader/infra/persistence/tomorrow_decision_freezes.py`
  - `src/trader/infra/persistence/tomorrow_shadow_evidence.py`
- 影子运行库：
  - `.runtime/v17/tomorrow-v2/tomorrow-v2.sqlite3`
  - `.runtime/v17/tomorrow-v2/tomorrow-shadow-evidence.sqlite3`
  - `.runtime/v17/tomorrow-v2/{checkpoints,freezes,quarantine}`

### D. 待替代（P1-P13 需要继续推进）

- 根页面与 API 汇聚仍在当前生产链；`/v2/tomorrow` 仍为并行观察入口。
- today/tomorrow/d25 的统一 V2 决策平面和统一读指针尚未切换。
- 交易所主数据持久化、风险登记簿分组件化、历史特征持久化、字段级合并仍按 P1-P3
  之后的任务推进。

## 3. 运行库与历史只读矩阵

| 类别 | 运行资产 | 本批处理结论 |
| --- | --- | --- |
| 现运行身份 | `.runtime/v17` | 活动运行根；当批继续读写 |
| 影子身份 | `.runtime/v17/tomorrow-v2` | tomorrow 证据与影子冻结专用 |
| 历史兼容库 | `.runtime/v2` | 只读；用于旧 release 与历史快照核验 |
| 历史归档 | `.runtime/backups/*` | 只读；用于回退复核和比对 |
| 旧进程遗留库 | `.runtime/.stock_analyzer_jobs.sqlite3`、`.runtime/market_data.sqlite3`、`.runtime/deepseek_scheduler.sqlite3`、`.runtime/factor_snapshots.sqlite3`、`.runtime/strategy_validation*.sqlite3` | 只读；本批不写入，不作为当前生产写路径 |

## 4. 术语边界固定（本批确认）

- 旧业务包：活动树外 `stock_analyzer`，仅完整旧 release 回退可用，不在本分支内并行维护新行为。
- 当前生产链：today/tomorrow/d25 当前生产 API、SSE、冻结与当前快照链。
- tomorrow v2 影子链：`/v2/tomorrow` 与 `tomorrow_v2` 读路径。
- V2 目标链：today→tomorrow→d25→long 的统一字段/决策/展示链（P0 仅定义，不切换）。
- 历史兼容解码器：用于已提交冻结和历史快照的审计读取，不因名称含 `v1` 误删。

## 5. P0 下一步边界（后续章程触发条件）

1. P1：完成来源能力确认后再决定正式与影子准入；
2. P2：先固定字段级质量模型；
3. P3：先建持久化/迁移骨架；
4. P8-P12：按 tomorrow→today→d25→long 的顺序切换决策与展示；
5. P13：在回放/切换证据齐备后清理旧生产依赖。 
