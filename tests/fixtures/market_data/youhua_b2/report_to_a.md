# Codex B 阶段 2 交接报告

codex_and_phase: `Codex B / B2.x`

base_commit: `03c710ee854dbead3446c0f0400ed7fe154e87b6`

contract_base: `45bd2fab992d36eb873b7c448fbd9739f0cad43c`

head_commit_or_patch: `patch only; no B commit/push in shared worktree`

owned_paths_changed:

- `src/trader/infra/market_data/columnar.py`
- `src/trader/infra/market_data/provider_adapter.py`
- `tests/unit/test_v17_columnar_changes.py`
- `tests/unit/test_v17_columnar_provider_adapter.py`
- `tests/fixtures/market_data/youhua_b2/columnar_baseline.json`
- `tests/fixtures/market_data/youhua_b2/report_to_a.md`

contract_assumptions:

- B2 starts from a commit containing `CONTRACT_BASE`; current baseline is `03c710ee854dbead3446c0f0400ed7fe154e87b6`, which includes A2 public contracts and the previous G2 gate-block record.
- A remains public owner for P3 -> P4 ports/events. B now provides infra-internal columnar batches and a conversion to the A2 `MarketChangeSet` / `FeatureSnapshotEnvelope` public types.
- B did not execute DeepSeek, P6/Web, publisher, bootstrap, application public ports, or global config wiring.
- Current shared worktree also contains non-B modified files under application, DeepSeek and Web owners; B did not edit those paths.

schema_or_migration_changes:

- No database migration.
- B-internal schema constants added: `columnar_quote_batch_v1`, `columnar_research_batch_v1`, `columnar_feature_batch_v1`, `market_change_set_v1`.
- `ColumnarBatchIdentity` now carries `manifest_hash` and `content_hash`.
- `MarketChangeSet` keeps backward-compatible positional fields and adds previous epoch, dirty boards, dirty industries, dirty field families, evidence manifest hash, risk changed codes, overlay-only flag, full invalidation reason and content hash.

tests_run_and_results:

- `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_v17_columnar_changes.py tests/unit/test_v17_columnar_provider_adapter.py -q` passed: 10 tests.
- `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_v2_market_data_normalize.py tests/unit/test_v2_market_data_merge.py tests/unit/test_v2_market_data_router.py tests/unit/test_v17_columnar_changes.py tests/unit/test_v17_columnar_provider_adapter.py tests/unit/application/test_candidate_features.py -q` passed: 45 tests.
- `PYTHONPATH=src .venv/bin/python -m pytest tests/component/test_v2_market_data.py -q` passed: 121 tests.
- `PYTHONPATH=src .venv/bin/python tests/performance/run_v15_market_data.py --config /home/cp/Public/trader/config/v2/runtime.json --fixture /home/cp/Public/trader/tests/fixtures/performance/v15 --output /tmp/youhua_b2_v15_market_data_ready_report.json` passed.
- `.venv/bin/python -m ruff format --check src/trader/infra/market_data/columnar.py src/trader/infra/market_data/provider_adapter.py tests/unit/test_v17_columnar_changes.py tests/unit/test_v17_columnar_provider_adapter.py` passed.
- `.venv/bin/python -m ruff check --select E,F,I,B,UP --ignore E501 src/trader/infra/market_data/columnar.py src/trader/infra/market_data/provider_adapter.py tests/unit/test_v17_columnar_changes.py tests/unit/test_v17_columnar_provider_adapter.py` passed.
- `git diff --check` passed.
- `make type-check` passed: 162 source files.

performance_before_after:

- Existing scalar runner still passes: 5500-row normalization P95 `92.301 ms`, two-source merge P95 `518.677 ms`, canonical snapshot P95 `761.546 ms`.
- B2 100 tick columnar dirty fixture: avg `946.024 ms`, max `1001.939 ms`, process peak RSS `204,193,792 bytes`, end USS `198,668,288 bytes`, Python traced peak `48,149,973 bytes`, Polars estimated max `642,128 bytes`.
- Logical cache estimate unchanged from v15 runner: `28,378,512 bytes`.
- Full metrics are in `tests/fixtures/market_data/youhua_b2/columnar_baseline.json`.

known_failures_and_risks:

- B2 now adapts to A2 public `MarketChangeSet`, `FeatureSnapshotEnvelope` and `P4ConsumerStub`; B still does not modify A owner public files.
- `ColumnarFeatureBatch` materializes existing scalar `FeatureSnapshot` values into strict Polars columns; it does not replace every scalar feature calculation loop yet.
- Content hashing over 5500-row expanded columnar frames is deterministic but expensive; B3 should measure hash placement again after A integration decides whether every tick needs full frame content hash.

requested_interface_changes:

- None beyond G1/A2 frozen scope. B can produce the A-owned `market_change_set_v1` fields from infra data once A provides the public envelope/stub.

ready_for_gate: `yes`

## B2.1 Provider Adapter

- Added B-internal `ProviderQuery`, `ProviderRawPayload`, `ProviderColumnarResult` and `ColumnarProviderAdapter` protocol.
- `run_columnar_provider_adapter()` enforces `transform_query -> extract_data -> transform_data`, timezone-aware requested/deadline times, lineage matching, explicit string missing reasons, valid 6-digit codes, non-empty versions and finite numeric values.
- Adapter result builds `ColumnarQuoteBatch` directly from transformed quotes and lineage hash; no public port is created.

## B2.2 Columnar Types

- Extended `ColumnarQuoteBatch` with content identity and strict schema.
- Added `ColumnarResearchBatch` and `ColumnarFeatureBatch`.
- Added manifest/content hash identity for quote, research and feature batches.

## B2.3 dtype/Expressions

- Quote, research and feature frames use explicit Polars String, Enum, Float64, Int64, Boolean and timezone datetime schemas.
- `_strict_frame()` rejects `Object` columns.
- Dirty projection uses eager Polars `select`, `join`, `filter`, `ne_missing`, `any_horizontal`, `unique` and `sort`; no Polars Python UDF or lazy frame is introduced.

## B2.4 P1/P2

- `ColumnarQuoteBatch.from_quotes()` allows provider adapters to construct columnar quote arrays directly.
- `ColumnarQuoteBatch.from_snapshot()` remains compatible with existing gateway latest-wins flow.
- Existing deterministic scalar merge order and Tencent targeted conflict behavior are unchanged and covered by existing market-data tests.

## B2.5 P3

- `ColumnarFeatureBatch.from_features()` materializes feature snapshots into typed columns with board, industry, observed time, market regime, reliability, competition group, liquidity bucket and requested feature value columns.
- `ColumnarFeatureBatch.to_public_envelope()` builds the A2 public P3 -> P4 envelope without exposing Polars outside infra.
- This preserves scalar output and avoids duplicating domain scoring.

## B2.6 Dirty

- `market_changes()` now reports inserted/updated/removed/dirty codes plus dirty boards, industries, field families, risk changed codes, evidence manifest hash, overlay-only flag, full invalidation reason and content hash.
- Schema/config version drift expands to full invalidation.
- Price/liquidity/identity-only changes are marked overlay-only.

## B2.7 Single-domain Verification

- New unit tests cover deterministic change sets, dimensions, field families, risk dirty codes, overlay-only ticks, strict dtype, manifest hashes, schema/config full invalidation and provider adapter validation.
- New A2 adaptation test covers `FeatureSnapshotEnvelope` creation and `P4ConsumerStub` receipt.
- Existing market-data component suite passes.
