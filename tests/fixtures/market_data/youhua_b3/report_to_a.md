# Codex B 阶段 3 专业复验报告

codex_and_phase: `Codex B / B3.x`

base_commit: `812de4390d42213c684b4e2096453c4775903443`

a3_integration_commit: `812de4390d42213c684b4e2096453c4775903443`

contract_base: `45bd2fab992d36eb873b7c448fbd9739f0cad43c`

head_commit_or_patch: `patch only; B3 adds verification artifacts only`

owned_paths_changed:

- `tests/fixtures/market_data/youhua_b3/columnar_baseline.json`
- `tests/fixtures/market_data/youhua_b3/report_to_a.md`

contract_assumptions:

- A3 integration handoff is available in `docs/reports/youhua-a3-integration.md`.
- B3 runs after A merged B2 production implementation into the integration commit.
- B3 does not modify application ports, pipeline, publisher, bootstrap, DeepSeek, P6/Web or public schema.
- No A-assigned B defect was present during this pass, so no B production fix was applied.

schema_or_migration_changes: `none`

tests_run_and_results:

- `PYTHONPATH=src .venv/bin/python -m pytest tests/unit/test_v2_market_data_normalize.py tests/unit/test_v2_market_data_merge.py tests/unit/test_v2_market_data_router.py tests/unit/test_v17_columnar_changes.py tests/unit/test_v17_columnar_provider_adapter.py tests/unit/application/test_candidate_features.py -q` passed: 45 tests.
- `PYTHONPATH=src .venv/bin/python -m pytest tests/component/test_v2_market_data.py -q` passed: 121 tests.
- `make type-check` passed: 162 source files.
- `PYTHONPATH=src .venv/bin/python tests/performance/run_v15_market_data.py --config /home/cp/Public/trader/config/v2/runtime.json --fixture /home/cp/Public/trader/tests/fixtures/performance/v15 --output /tmp/youhua_b3_v15_market_data_report.json` passed.
- 100 tick columnar dirty/RSS script passed and produced `tests/fixtures/market_data/youhua_b3/columnar_baseline.json`.

performance_before_after:

- 5500-row normalization P95: `97.376 ms` against `800 ms`.
- Two-source merge P95: `533.872 ms` against `1000 ms`.
- Canonical snapshot P95: `790.416 ms` against `1500 ms`.
- Logical cache estimate: `28,378,512 bytes`.
- 100 tick columnar dirty fixture: avg `961.897 ms`, max `1002.717 ms`, process peak RSS `204,206,080 bytes`, end USS `165,568,512 bytes`, Python traced peak `48,150,094 bytes`, Polars estimated max `642,128 bytes`.
- Process peak RSS remains below the 384 MiB migration hard limit.

known_failures_and_risks:

- Content hashing over full 5500-row expanded frames remains deterministic but expensive; no correctness issue observed. If A later requires every live tick to compute full content hash on the hot path, B should remeasure after final hash placement.
- This pass validates B integration behavior with existing scalar fixture and current market-data component suite; full P1-P6 process peak and browser/P6 behavior remain D/A integration gates.

requested_interface_changes: `none`

ready_for_gate: `yes`

## B3.1 Scalar/Columnar Equivalence

- Re-ran normalization, merge, router, candidate feature and columnar unit tests on the A3 integration commit.
- `tests/unit/test_v17_columnar_changes.py` validates deterministic quote batch identity, public A2 envelope adaptation, strict dtype and manifest/content hashes.

## B3.2 Dirty Partial/Full Consistency

- Change-set unit tests verify inserted/updated/removed/dirty codes, dirty boards, dirty industries, dirty field families, risk dirty codes, overlay-only ticks and schema/config full invalidation.
- 100 tick fixture alternates full-market and targeted snapshots; final dirty set is 360 targeted quote updates with `overlay_only=true`, matching the targeted overlay input.

## B3.3 Performance and Memory

- Fixed v15 market-data performance runner passed all configured budgets.
- 100 tick memory fixture records RSS, USS, Python traced allocation and Polars native estimate; peak RSS is `204,206,080 bytes`.

## B3.4 B Lease Fixes

- No A-assigned B lease defect was found.
- B3 only added this report and fixture.
