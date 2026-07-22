# Codex B 阶段 1 交接报告

codex_and_phase: `Codex B / B1.x`

base_commit: `777e73d445f88c165126d1a09d02b833453b9d3e`

head_commit_or_patch: `patch only; no commit; waiting for CONTRACT_BASE/G1`

owned_paths_changed:

- `tests/fixtures/market_data/youhua_b1/report_to_a.md`
- `tests/fixtures/market_data/youhua_b1/scalar_baseline.json`

contract_assumptions:

- B1 不修改生产代码，不触发 DeepSeek、P6/Web 或公共接线。
- P1-P3 列式实现只能留在 `src/trader/infra/market_data/` 及其测试；Polars 不进入 domain、application 公共端口或 Web。
- A 是 `FeatureSnapshot + MarketChangeSet` 公共接缝的 owner；B 只提交接口申请，不自行创建公共 port。
- B2 必须从 A 发布的 `CONTRACT_BASE` 开始，不越过 G1。

schema_or_migration_changes: `none`

tests_run_and_results:

- `PYTHONPATH=src .venv/bin/python tests/performance/run_v15_market_data.py --config /home/cp/Public/trader/config/v2/runtime.json --fixture /home/cp/Public/trader/tests/fixtures/performance/v15 --output /tmp/youhua_b1_v15_market_data_report.json` passed.
- `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_v2_market_data_normalize.py tests/unit/test_v2_market_data_merge.py tests/unit/test_v17_columnar_changes.py tests/unit/test_v2_market_data_router.py tests/unit/application/test_candidate_features.py -q` passed: 36 tests.
- `make format-check` passed: 220 files already formatted.
- `make lint` failed after Ruff passed because `scripts/check_refactor_quality.py` reports existing strict refactor debt count drift: expected `{'C901': 37, 'N818': 5, 'PLR0911': 15, 'PLR0912': 15, 'PLR0913': 55, 'PLR0915': 12}`, actual `{'C901': 39, 'N818': 5, 'PLR0911': 15, 'PLR0912': 16, 'PLR0913': 55, 'PLR0915': 11}`. B1 only added fixture/report files and did not change code counted by Ruff.
- `make type-check` passed: mypy success in 159 source files.
- Full 100 tick scalar merge under tracemalloc was interrupted after 180s; the committed fixture records the bounded 100 tick `ColumnarQuoteBatch.from_snapshot + market_changes` baseline over two premerged 5500-row scalar snapshots, with the existing v15 runner covering full scalar normalization/merge latency.

performance_before_after:

- before/current only; B1 makes no production change.
- 5500-row normalization P95: `96.322 ms` against `800 ms`.
- two-source market merge P95: `528.327 ms` against `1000 ms`.
- canonical snapshot with 360 Tencent targeted candidates P95: `781.349 ms` against `1500 ms`.
- logical cache estimate from v15 runner: `28,378,512 bytes`.
- 100 tick fixture: max tick latency `212.748 ms`, process peak RSS `188,854,272 bytes`, end USS `176,435,200 bytes`, Python traced peak `48,149,920 bytes`, Polars estimated max `363,000 bytes`.

known_failures_and_risks:

- Full scalar merge + tracemalloc 100 tick is too slow for the interactive B1 run on this host; B2 should separate latency runs from memory runs and keep tracemalloc off latency paths.
- `make lint` is not green at the repository refactor debt gate on this base; this is outside the B1 report/fixture diff but must be resolved before a shared G1/G2 gate can be declared fully clean.
- Current `src/trader/infra/market_data/columnar.py` is only an initial quote-batch/dirty-code helper; it lacks `ColumnarResearchBatch`, `ColumnarFeatureBatch`, board/industry/field-family dirty sets, manifest/content hash, and full invalidation reason.
- Current provider adapters normalize directly inside `fetch_*`; B2 must make the `transform_query -> extract_data -> transform_data` split explicit without changing network behavior.
- Current feature materialization remains scalar dictionaries/dataclasses in `FeatureBuilder`; B2 must preserve scalar output equivalence while moving P1-P3 hot paths to typed columnar internals.

requested_interface_changes:

1. A should freeze a public P3 -> P4 envelope owned outside B:
   - `schema_version`
   - `snapshot_version`
   - `feature_snapshot_version`
   - `merge_epoch`
   - `feature_snapshots: tuple[FeatureSnapshot, ...]`
   - `market_change_set: MarketChangeSet`
   - `content_hash`
2. A should define public `MarketChangeSet` fields independent of Polars:
   - `schema_version`
   - `merge_epoch`
   - `previous_merge_epoch: str | None`
   - `changed_codes: tuple[str, ...]`
   - `changed_boards: tuple[str, ...]`
   - `changed_industries: tuple[str, ...]`
   - `changed_field_families: tuple[str, ...]`
   - `overlay_only: bool`
   - `full_invalidation_reason: str | None`
   - `evidence_manifest_hash: str`
   - `content_hash: str`
3. A should provide a P4 test consumer stub for B2 equivalence tests:
   - Accepts the public P3 -> P4 envelope.
   - Records received `merge_epoch`, `content_hash`, feature count, changed codes, and field families.
   - Performs no scoring, DeepSeek, publishing, HTTP, persistence, or Web side effect.

ready_for_gate: `yes; waiting for A CONTRACT_BASE/G1`

## B1.1 Provider 盘点

| Provider | Adapter | Query | Extract | Transform | 时间/版本/血缘/缺失 | 当前覆盖 |
| --- | --- | --- | --- | --- | --- | --- |
| Eastmoney | `EastmoneyClient` in `src/trader/infra/market_data/eastmoney.py` | `_fetch_page()`, `_get()` build page/history/intraday HTTP params. | `_object_mapping()`, `_object_rows()`, kline/trends parsing. | `_quote_from_row()` -> `MarketQuoteInput` -> `build_market_quote()`. | `source_time` from source timestamp or received time; `data_version` source timestamp; `source="eastmoney"`; missing becomes `None`. | Normalize and merge behavior covered indirectly; no dedicated query/extract/transform adapter contract tests. |
| Sina | `SinaClient` in `src/trader/infra/market_data/sina.py` | count endpoint then paged `Market_Center.getHQNodeData`. | JSON list pages. | `_quote_from_row()` -> `MarketQuoteInput` -> `build_market_quote()`. | `source_time=received_at`; `data_version=sina:<received_ts>`; missing becomes `None`. | Normalize and router/merge paths covered indirectly; no dedicated adapter split tests. |
| Tencent | `TencentClient` in `src/trader/infra/market_data/tencent.py` | targeted quote URL and qfq history URL. | GB18030 quote payload and JSONP qfq history payload. | `_parse_payload()` and `_history_bars()`. | quote `source_time` parsed from field 30; `data_version=tencent:<source_ts>`; only requested codes accepted. | Candidate priority, deadline and overlay covered in component tests; no explicit split contract. |
| Tushare | `TushareClient` in `src/trader/infra/market_data/tushare.py` | SDK method calls for master/calendar/history/valuation/financials. | `_fetch_records()` / `_fetch_per_code()` records. | `SourceObservation` rows for reference and slow data. | Status/error health tracked; missing and degraded reasons enter observations. | Reference cache/degradation covered in component tests; no P1 columnar schema tests. |
| AKShare | `AkshareResearchClient` in `src/trader/infra/market_data/akshare.py` | research/news/financial/announcement/pledge/unlock requests. | JSON/table parsing helpers. | `ResearchObservation` / `Evidence`. | point-in-time research evidence with cacheable source times. | Research degradation/cache covered; B does not own DeepSeek use of research facts. |

## B1.2 P1-P3 盘点

- 报价统一：`normalize.py`, `merge.py`, `merge_quote.py`, `observations.py`, `gateway.py`, `source_coordinator.py`。
- 候选过滤入口：应用层 `candidate_features.py` 调用市场特征；领域 `domain/recommendation/filters.py` 执行硬过滤，B2 不复制领域评分。
- 板内/同行统计与特征物化：`features.py`, `feature_math.py`, `feature_risks.py`, `history.py`, `service_candidates.py`。
- latest-wins：`gateway.py` 的 `_latest_snapshot/_latest_batch/_latest_changes`，`overlay_canonical_snapshot()`，`QuoteStore.update_candidate_quotes()`，source lane latest pending。
- scalar 热点：逐行 `MarketQuoteInput`/`MarketQuote` 构造、`SourceObservation` per-field merge、`canonical_json_bytes` hashing、`FeatureBuilder` per-code dict 计算、cross-section percentile/industry loops。
- B2 预计修改：`src/trader/infra/market_data/{columnar.py,normalize.py,merge.py,merge_quote.py,features.py,feature_math.py,feature_risks.py,gateway.py,source_coordinator.py,service_candidates.py}` 及对应 B 测试。
- B2 预计新增：provider split helpers/tests、`ColumnarQuoteBatch` 扩展、`ColumnarResearchBatch`、`ColumnarFeatureBatch`、B-internal dirty projection tests and fixtures。

## B1.3 基线

See `tests/fixtures/market_data/youhua_b1/scalar_baseline.json`.

## B1.4 接口申请

See `requested_interface_changes` above. B will not implement public ports before A publishes `CONTRACT_BASE`.
