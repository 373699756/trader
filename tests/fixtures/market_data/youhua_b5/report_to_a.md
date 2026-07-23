# Codex B 阶段 5 终审签字报告

status: `PASS`

ready_for_gate: `yes`

codex_and_phase: `Codex B / B5.1-B5.2`

review_base: `45bd2fab992d36eb873b7c448fbd9739f0cad43c` (`CONTRACT_BASE`)

final_integration_base: `8e7ab24985ff73f7ec54cf62c9440f97b5d179c6` (A4 pushed head)

b4_implementation_commit: `69de151c79ab9502d0742bfc52601c455ad26a2a`

## B5.1 Final diff review

The final P1-P3 production diff is limited to:

- `src/trader/infra/market_data/columnar.py`
- `src/trader/infra/market_data/columnar_merge.py`
- `src/trader/infra/market_data/gateway.py`
- `src/trader/infra/market_data/merge.py`
- `src/trader/infra/market_data/provider_adapter.py`

No B-owned production file changed between the B4 implementation commit and the A4 integration head.
The final review checked provider lineage and strict types, scalar/columnar quote and epoch equivalence,
latest-wins behavior, Tencent/reference/partial fallback, P3 dirty expansion, public envelope isolation,
Polars failure degradation, bounded memory, deterministic hashes, type coverage and installability.
Polars remains inside `infra`; no domain/application/Web public contract, scoring formula, freeze behavior,
network route, database schema or runtime configuration is changed by B5.

### Review finding B5-F01

`market_changes()` previously derived dirty boards and industries only from the new frame. A removed stock,
or a stock moving to another board or industry, therefore omitted the old cross-section from the dirty set.
Insertions and removals also marked only quote identity instead of every affected field family, and were
absent from the internal risk-dirty set. This could make partial P3 recomputation narrower than an equivalent
full recomputation.

The fix unions old and new board/industry dimensions for every dirty code, expands membership changes to all
registered field families, and includes inserted/removed codes in the risk-dirty set. The regression covers a
simultaneous insertion, removal, board move and industry move and proves both old and new cross-sections are
invalidated. Overlay-only quote ticks keep their existing narrow behavior.

The B4 report was also corrected to use the exact values already stored in its acceptance JSON and its actual
published implementation commit. This is an evidence-only correction; the historical pass/fail conclusion is
unchanged.

## B5.2 Sign-off evidence

### Correctness and dirty consistency

- The B domain suite passes 171 tests, including strict provider adaptation, normalization, scalar/columnar
  merge equivalence, malformed and Polars-failure fallback, dirty partial/full behavior, public P3 -> P4
  projection and the new B5-F01 regression.
- Fixed business snapshot SHA-256 remains
  `af791c795eb6447976b0542986c60bf85f229d771146d2b046a4abbcac9436e3`.
- Fixed canonical snapshot SHA-256 remains
  `234b923cb17d1979365892791f38545598ae2d25f0cbe14817980a3080c3329b`.
- A4-F01 remains closed: eligible columnar merge/projection failures preserve the valid scalar snapshot,
  issue a full invalidation and expose an explicit degraded reason.
- The exact `HEAD + B5` staged diff was reconstructed outside the shared worktree. `make format-check`,
  `make lint`, `make type-check`, `make test` and `make package` pass there: Ruff debt remains
  `C901=38/N818=5/PLR0911=15/PLR0912=16/PLR0913=55/PLR0915=11`, mypy checks 164 source files
  and all 738 tests pass.
- The isolated wheel installs outside the repository, imports from `site-packages`, validates the runtime
  configuration, executes `trader-cli`, exposes the template plus nine CSS/JavaScript/SVG resources and
  passes `pip check`.
- After the parallel A-side G4 report arrived, the combined shared worktree also passed all five make gates
  and the same outside-repository wheel checks, confirming the two batches coexist without a known regression.

### Performance

`tests/fixtures/market_data/youhua_b5/market_acceptance.json` records a final no-network run on Python
3.14.4:

- scalar/columnar normalization-plus-two-source-merge process CPU P95:
  `1545.992/1172.998 ms`, improvement `24.126%` against the required `20%`;
- 5500-row normalization/two-source merge/canonical snapshot wall P95:
  `140.455/545.278/1314.793 ms` against `800/1000/1500 ms`;
- business and canonical hashes match the accepted B4/A4 identities.

The fixed v16 board-scoring runner passes:

- board preselection `51.411 ms`;
- board local scoring `9.030 ms`;
- three-board/three-strategy wall P95 `519.941 ms`;
- global stable selection `3.700 ms`.

### Memory

- B-owned 100-tick market/columnar workload:
  logical cache `29,661,328 B`, allocation growth `0.0%`, peak RSS `273,195,008 B`,
  end RSS `254,447,616 B`, end USS `240,578,560 B`, Polars estimate `1,282,816 B`.
- The fixed A4 integrated P1-P6 pressure rerun passes:
  logical bytes `205,468,511 B <= 260,046,848 B`, peak RSS
  `387,186,688 B <= 402,653,184 B`, end USS `339,656,704 B`; the workload retains
  scalar/columnar old/new epochs, six pools near 70%, an eight-stock DeepSeek batch, 20 P6 dates,
  cold prefetch, atomic replacements and 32 slow clients in one process.

## Final decision

`PASS`. P1-P3, dirty projection, performance and memory are consistent with the corrected B reports and
the A4 integrated evidence. There are no known unresolved B-domain correctness defects.

## Residual risks for Codex A

- The optimized merge remains intentionally limited to complete Eastmoney/Sina full-market rows.
  Partial/reference/Tencent/degraded inputs use the scalar path and must not be described as receiving the
  same throughput improvement.
- Performance is sensitive to shared-host scheduling and frequency. Two preliminary B5 measurements retained
  identical business hashes and passing memory but did not meet the relative threshold; the final fixed-identity
  run passed at `24.126%`, while the accepted A4 run passed at `35.544%`. Keep the fixed runner and identity
  evidence for release checks rather than generalizing from one timing sample.
- Runtime evidence is from Python 3.14.4. Python 3.10-3.13 remain covered by project static/wheel constraints,
  not local execution on this host.
- Real supplier latency and data quality are external to the no-network fixture. Performance and structural
  correctness evidence does not establish investment-return improvement.
- During B5, a separate A-side shared-worktree batch began editing publisher/SSE and G4 contract tests before
  its required G4 report existed. Those files are excluded from the B5 staged diff; their transient two-test
  failure is not folded into this sign-off. The A-side report later arrived and the shared tree passed; the
  isolated exact-diff gate independently proves B5 itself remains green.
