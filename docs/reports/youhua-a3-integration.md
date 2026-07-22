# youhua A3 阶段 3 集成接线报告

状态：A3.1-A3.7 已完成 A owner 集成接线；B2/C2/D2 生产实现已按 B -> C -> D 顺序纳入
当前集成工作树。G3 未发布，仍等待 B3/C3/D3 在本集成提交之后完成专业复验并回报
`ready_for_gate=yes`。

## 1. 工作树封存

| 项 | 值 |
| --- | --- |
| codex_and_phase | Codex A / A3.x |
| start_head | `f482ea1aaba157dd8e5cb416f9412b18c97d1b6a` |
| upstream | `origin/branch` |
| upstream_commit | `f482ea1aaba157dd8e5cb416f9412b18c97d1b6a` |
| CONTRACT_BASE | `45bd2fab992d36eb873b7c448fbd9739f0cad43c` |
| G2_commit | `f482ea1aaba157dd8e5cb416f9412b18c97d1b6a` |
| start_worktree | 存在 B2/C2/D2 生产补丁、B2/C2 单域测试和 C3 preflight 报告；本批按 A3 集成接线统一复核 |
| A 本次范围 | 集成 B/C/D、权威契约同步、A3 报告、契约测试、Review、提交和推送 |

## 2. A3.1 合并 B

B2 的列式行情实现按当前集成工作树纳入：

- `ColumnarQuoteBatch` 补齐严格 schema、manifest/content hash、board/industry/risk 字段和
  deterministic identity。
- `MarketChangeSet` 增加 previous epoch、dirty boards、dirty industries、dirty field
  families、risk changed codes、overlay-only、full invalidation reason 和 content hash，并
  可转换为 A2 公共 `MarketChangeSet`。
- `ColumnarResearchBatch`、`ColumnarFeatureBatch` 和 provider 三段式 adapter 保持在
  `infra/market_data` 内部，P3 -> P4 对外只暴露 A2 `FeatureSnapshotEnvelope`。
- 已运行 P1-P3、等价、change set 和 provider adapter 定向测试。

## 3. A3.2 合并 C

C2 的 DeepSeek V4 facts、证据路由、预算和 long 隔离实现按当前集成工作树纳入：

- DeepSeek schema 切换为 `deepseek_v4_review_facts_v1`，保留旧 schema 解析兼容，不允许模型
  输出 action、rank、target、final score、penalty 或 veto。
- 证据路由固定每股最多 12 条，manifest 带 `source_tier` 和 `event_key`；正向事实只由官方或
  两个独立可信来源支持时映射为加分。
- long 在 reviewer 和预算仓库双层拒绝，复核集合为空且不创建预算行。
- 软桶执行上限为 today 22、tomorrow 14、d25 12、long 0、shared_preheat 10、emergency 5；
  Pro 挑战者批次最多 4 只且全日软上限 8。
- 已验证普通 quote-only 变化命中 raw cache 时不新增 DeepSeek HTTP；manifest/risk 变化才进入
  复核路由。

## 4. A3.3 合并 D

D2 的 P6/SSE/Web 增量实现按当前集成工作树纳入：

- 推荐和 overlay patch 均携带 `patch_schema_version=2`，推荐 patch 增加
  `base_projection_version`、`projection_version`、`etag`、`view`、`removed_codes`。
- SSE 对游标超前与过期分别返回 `cursor_ahead` 和 `cursor_expired`。
- 浏览器按 `strategy + trade_date + view + code` 维护行身份，projection/base 不匹配时才触发
  ETag resync；overlay patch 只应用到匹配 projection。
- P6 current pin 不再被更旧日期冻结历史覆盖，旧日期仍可进入 resident history。

## 5. A3.4-A3.5 公共接线与身份收敛

- B 的列式 dirty/change identity、C 的 V4 facts identity、D 的 projection identity 均收敛到
  `src/trader/application/ports/youhua.py` 已冻结的公共版本，不新增第二套公共 schema。
- `docs/software-business-design.md` 同步 SSE patch v2、projection/base/overlay 和 resync 契约。
- `docs/recommendation-strategy.md` 同步 DeepSeek 证据上限、soft bucket、long 零请求、
  challenger 批次和 quote-only cache 契约。
- 生产组合根仍只通过 `bootstrap.py` 显式注入 reviewer、publisher、P6 index 和 market
  data；`create_app()` 仍保持无线程、无网络、无数据库和无文件写入副作用。

## 6. A3.6 集成测试

本批已运行：

- `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_v17_columnar_changes.py tests/unit/test_v17_columnar_provider_adapter.py tests/unit/test_v2_market_data_normalize.py tests/unit/test_v2_market_data_merge.py tests/unit/test_v2_market_data_router.py tests/unit/application/test_candidate_features.py tests/unit/test_v2_deepseek_base.py tests/component/test_v2_deepseek.py tests/component/test_v2_deepseek_v4.py tests/component/test_youhua_deepseek_c2.py tests/unit/application/test_published_snapshots.py tests/unit/application/test_publisher.py tests/contract/test_v2_web_api.py tests/contract/test_v2_app_factory.py tests/contract/test_youhua_a2_public_skeleton.py tests/contract/test_youhua_contract_base.py -q`
  通过：183 项。
- `make format-check` 通过。
- `make lint` 通过；严格债务基线同步到已推送 `HEAD` 实际计数，本批新增 PLR0913 已通过
  options 值对象降回 0 增量。
- `make type-check` 通过：162 个源码文件。
- `make test` 通过。
- `make package` 通过；生成的 `build/`、`dist/` 和 egg-info 已清理。
- 仓库外 wheel 安装到 `/tmp/trader-wheel-a3` 后可导入 `trader`、读取 `index.html` 与
  `dashboard.js` 包资源，并可执行 `trader.entrypoints.cli --help`。

## 7. A3.7 问题分派

当前 A 集成态没有已知公共接线缺陷。C3 preflight 已到达，但其自报 `ready_for_gate=no`，
等待本 A3 集成提交或明确 A handoff 后才能开始 C3。A3 推送后：

- B 应基于 A3 集成提交执行 B3.1-B3.4，并报告 scalar/columnar 等价、change set 全量一致、
  5500 行/360 候选/100 tick 和内存/RSS。
- C 应基于 A3 集成提交执行 C3.1-C3.4，并报告跨三策略 raw facts 单请求、普通 price change
  零新增 HTTP、long/window/budget/restart/late/all-fail 降级。
- D 应基于 A3 集成提交执行 D3.1-D3.4，并报告 P4/P5 重发布、P6 热读、SSE patch/游标/慢
  客户端/冻结/overlay/ETag 和 P6 原子替换峰值。

ready_for_gate: `yes; A3 integration handoff is available; G3 is pending B3/C3/D3 ready reports`
