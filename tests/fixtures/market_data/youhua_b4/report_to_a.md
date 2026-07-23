# Codex B 阶段 4 性能与内存验收报告

codex_and_phase: `Codex B / B4.x`

base_commit: `7a8a0282d025cbc23fffff5736e94c2d1bf883e0`

head_commit_or_patch: `patch only; B4 implementation and acceptance artifacts`

owned_paths_changed:

- `src/trader/infra/market_data/columnar_merge.py`
- `src/trader/infra/market_data/gateway.py`
- `src/trader/infra/market_data/merge.py`
- `scripts/check_refactor_quality.py`
- `tests/component/test_v2_market_data.py`
- `tests/unit/test_v2_market_data_merge.py`
- `tests/performance/run_youhua_b4_market.py`
- `tests/fixtures/market_data/youhua_b4/acceptance.json`
- `tests/fixtures/market_data/youhua_b4/report_to_a.md`

contract_assumptions:

- B4 runs from the last pushed G3 baseline and stays inside B4.1-B4.4.
- The fast path is infra-internal and only handles complete, one-row-per-code Eastmoney/Sina full-market observations; partial rows, reference metadata, Tencent overlays, duplicate identities and degraded inputs retain the existing scalar field-level merge.
- The public market snapshot schema, application ports, pipeline, publisher, bootstrap, DeepSeek, P6/Web and runtime configuration are unchanged.
- Process CPU time is the primary relative comparison clock because other Codex sessions shared the same host during the measurement. The relative path includes normalization, observation construction and two-source merge; absolute B4.2 limits remain wall-clock P95 measurements.

schema_or_migration_changes: `none; infra-internal normalization/projection types and scalar fallback change set only`

tests_run_and_results:

- `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_v2_market_data_merge.py -q` passed: 19 tests.
- `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_v2_market_data_normalize.py tests/unit/test_v2_market_data_merge.py tests/unit/test_v2_market_data_router.py tests/unit/test_v17_columnar_changes.py tests/unit/test_v17_columnar_provider_adapter.py tests/unit/application/test_candidate_features.py -q` passed: 48 tests.
- `PYTHONPATH=src .venv/bin/python -m pytest tests/component/test_v2_market_data.py -q` passed: 122 tests.
- `.venv/bin/python -m ruff check --select E,F,I,B,UP --ignore E501` on B4 source/test files passed.
- `.venv/bin/python -m mypy src/trader/infra/market_data/columnar_merge.py src/trader/infra/market_data/merge.py src/trader/infra/market_data/gateway.py` passed.
- `PYTHONPATH=src .venv/bin/python tests/performance/run_youhua_b4_market.py --config config/v2/runtime.json --fixture tests/fixtures/performance/v15 --output /tmp/youhua_b4_acceptance_f01.json` passed.
- `PYTHONPATH=src .venv/bin/python tests/performance/run_v16_board_scoring.py --config config/v2/runtime.json --fixture tests/fixtures/performance/v16 --output /tmp/youhua_b4_scoring.json` passed.

performance_before_after:

- Pre-fix host baseline: two-source merge P95 `1470.722 ms` and canonical snapshot P95 `2115.994 ms`, both above their B4.2 limits.
- Identical interleaved normalization-plus-two-source-merge workload: scalar process CPU P95 `1555.037 ms`, columnar process CPU P95 `1131.749 ms`, relative improvement `27.22%`; informational wall P95 was `1561.387 ms` versus `1128.513 ms`.
- 5500-row normalization wall P95 `149.045 ms` against `800 ms`.
- Two-source merge wall P95 `573.552 ms` against `1000 ms`.
- Canonical snapshot wall P95 `1130.823 ms` against `1500 ms`; fixed canonical snapshot SHA-256 remains `234b923cb17d1979365892791f38545598ae2d25f0cbe14817980a3080c3329b`.
- Board preselection P95 `90.460 ms`, board local scoring P95 `15.354 ms`, three-board/three-strategy wall P95 `872.648 ms`, global selection P95 `9.961 ms`; all B4.3 limits pass. The sequential/lane wall ratio is reported for observation only and is not claimed as CPU acceleration.
- 100 tick allocation growth `0.0%`, B-owned logical cache `29,661,328 bytes`, process peak RSS `288,702,464 bytes`, end RSS `268,767,232 bytes`, end USS `255,959,040 bytes`, Python traced peak `1,782,835 bytes`; all B4.4 limits pass.

known_failures_and_risks:

- The optimized path is intentionally narrow. Partial/reference/Tencent/degraded or otherwise non-equivalent shapes fall back to the established scalar merge and may not receive the same throughput improvement.
- B4 validates the B-owned 100 tick market/columnar workload. Integrated P1-P6 pressure with maximum DeepSeek concurrency, maximum P6 cold-read concurrency and slow clients remains the A4.5 whole-system gate.
- This run used Python `3.14.4`; cross-version installation/compatibility evidence for Python 3.10-3.13 and 3.15 belongs to the A4.4 matrix.
- No external network request was made by the B4 fixture.

requested_interface_changes: `none`

ready_for_gate: `yes`

## B4.1 Columnar Merge

- Complete canonical provider rows are normalized with typed Polars expressions, and complete Eastmoney/Sina observations are projected with typed winner selection and direct canonical quote construction.
- Regression tests prove quote, field-source, source-version, conflict and canonical epoch equivalence against the scalar merge; malformed or incomplete shapes prove scalar fallback.
- The fixed normalization-plus-merge process CPU P95 improves by `27.22%`, exceeding the required `20%`.

## B4.2 Absolute Market Budgets

- Normalization, two-source merge and canonical snapshot wall-clock P95 are `134.059 ms`, `586.035 ms` and `1130.823 ms`.
- All configured B4.2 budgets pass and the canonical snapshot hash is unchanged.

## B4.3 Board Scoring Budgets

- Board preselection/local scoring, three-board/three-strategy wall time and deterministic global selection all pass their fixed budgets.
- Existing scoring implementation needed no B-owned production change.

## B4.4 Memory and B-Owned Fixes

- The 100 tick retained market/columnar workload remains below the 248 MiB logical cache and 384 MiB process peak RSS limits, with no measured retained-allocation growth.
- A4-F01 now catches Polars/columnar projection construction failures, commits the valid scalar canonical market, emits a full-invalidation change set and records `columnar_projection_failed:scalar_fallback`; the exact injected component regression passes.
- The eligible merge path separately records `columnar_merge_failed:scalar_fallback` when Polars winner projection fails; malformed/partial data continues to use silent scalar eligibility fallback without treating valid input degradation as a provider failure.
- The other B-owned failure was the scalar complete-market normalization/merge/canonicalization hot path. The narrow columnar normalization/projection and streaming canonical epoch hash fix that failure without changing public behavior.
