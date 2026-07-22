# youhua A2 公共骨架和测试替身报告

状态：A2.1-A2.5 已完成 A owner 范围；公共骨架和替身测试通过；未进入 B/C/D 内部算法，
G2 仍需等待 B2/C2/D2 标准交接包。

## 1. 工作树封存

| 项 | 值 |
| --- | --- |
| codex_and_phase | Codex A / A2.x |
| start_head | `646509619bd491b9333f3361bfe6cda8bbf9ee96` |
| upstream | `origin/branch` |
| upstream_commit | `646509619bd491b9333f3361bfe6cda8bbf9ee96` |
| CONTRACT_BASE | `45bd2fab992d36eb873b7c448fbd9739f0cad43c` |
| start_worktree | 存在未暂存生产改动和 C2/B2 测试文件；本批保留且不暂存 |
| A 本次范围 | 公共 port/event/type、配置内存契约、测试替身、契约测试和文档 |
| B/C/D 内部算法 | 未执行、未修改 |

## 2. A2.1 公共类型

新增 `src/trader/application/ports/youhua.py`，集中定义唯一公共 schema/version、identity、
event 和校验函数：

- 总契约：`youhua_contract_base_v1`。
- P3 -> P4：`FeatureSnapshotEnvelope` 和 `MarketChangeSet`，版本
  `p3_p4_feature_snapshot_market_change_set_v1`、`market_change_set_v1`。
- P4 -> P5：`HighValueReviewManifest`、`HighValueReviewInput`、`EvidenceManifest` 和
  `ReviewOwnerIdentity`，版本 `p4_p5_high_value_review_manifest_v1`、
  `evidence_manifest_v1`、`review_owner_strategy_identity_v1`。
- DeepSeek V4：`DeepSeekV4Facts` 使用 `deepseek_v4_review_facts_v1`，只表达事实、证据
  和覆盖，不包含 penalty、veto、action 或 rank；证据 ID 必须落在 manifest 内。
- P6/Web：`ProjectionEvent`、`OverlayEvent`、`OverlayQuote` 和 `ResyncReason`，版本
  `p4p5_p6_projection_event_v1`、`p6_overlay_event_v1`、`p6_resync_reason_v1`。
- 内存契约：`MemoryBudgetContract`、`MemoryUsageSnapshot` 和
  `validate_memory_activation()` 固定 248 MiB 逻辑缓存、384 MiB 进程峰值 RSS。

公共类型只依赖 `domain` 和 application JSON 类型，不导入 `infra`、Flask、网络、文件或
数据库。

## 3. A2.2 测试替身

新增 `src/trader/application/youhua_test_doubles.py`：

- `P4ConsumerStub` 只记录 `merge_epoch`、`content_hash`、feature 数量和 dirty 范围，不执行
  scoring、DeepSeek、publisher、HTTP、persistence 或 Web。
- `ReviewInputStub` 只记录 `MarketChangeSet` 和复核输入 manifest，不调用 DeepSeek；long
  非空集合由公共 manifest 构造时拒绝。
- `ProjectionOverlayProducerStub` 只记录 projection/overlay 事件，不连接真实 Web DOM 或 SSE。

这些替身只用于 B2/C2/D2 隔离测试，不作为生产默认路径。

## 4. A2.3 公共失败测试

新增 `tests/contract/test_youhua_a2_public_skeleton.py`，覆盖：

- schema/version 和 resync reason 只有一套；
- P3 -> P4 envelope 与 P4 consumer 替身只交换身份、数量和 dirty 范围；
- long 复核集合永久为空，缺失 feature identity 被拒绝；
- DeepSeek V4 facts 引用 manifest 外证据被拒绝，模型输出不得携带 penalty；
- projection CAS 不允许同一代码同时 upsert/remove，overlay 只携带报价字段；
- 248 MiB 逻辑缓存和 384 MiB 进程峰值 RSS 分别校验，旧 `cache_total_bytes` 单字段配置被拒绝。

同步修复 `src/trader/application/ports/snapshots.py` 的公共状态返回类型，避免 application
公共边界继续使用 `Mapping[str, object]`。

## 5. A2.4 变更审批

本批合并 G1 中 B1/C1/D1 的接口申请后，未发现需要偏离 G1 清单的新公共签名。所有新增公共
schema 均在 A owner 文件中实现；B/C/D 不需要创建第二套 port/event/version。

## 6. A2.5 集成骨架

A2 当前建立的是可测试的公共类型、替身和契约测试骨架；真实 B2/C2/D2 实现包尚未收齐，
因此不接入真实生产默认、不声明 G2。后续 G2 合并仍按
`docs/reports/youhua-g1-contract-base.md` 中 A2 -> B2 -> C2 -> D2 -> G2 顺序执行。

## 7. 验证

- `PYTHONPATH=src .venv/bin/python -m pytest -q tests/contract/test_youhua_a2_public_skeleton.py tests/contract/test_v2_architecture.py tests/unit/test_v2_settings.py`
  通过：62 项。

- A2 范围 Ruff format/check 通过。
- `make type-check` 通过：162 个源码文件。
- 全局 `make format-check` 被非 A2 的 `src/trader/infra/deepseek/evidence_router.py`、
  `src/trader/infra/deepseek/schema.py`、`tests/component/test_youhua_deepseek_c2.py` 格式阻断。
- 全局 `make lint` 被非 A2 的 `src/trader/infra/deepseek/schema.py` import 排序和
  `tests/component/test_youhua_deepseek_c2.py` 未使用导入阻断。
- 全局 `make test` 运行完成，剩余 2 个既有失败：
  `tests/contract/test_v2_bootstrap.py::test_duplicate_system_start_does_not_stop_running_history_pool` 和
  `tests/unit/application/test_cadence.py::test_production_policy_plans_exact_full_trading_day_task_counts`。
- `make package` 沙箱内因构建依赖联网失败，提升权限后通过；生成产物已清理。

ready_for_gate: `yes; A2 public skeleton is available; waiting for B2/C2/D2 standard handoff packages`
