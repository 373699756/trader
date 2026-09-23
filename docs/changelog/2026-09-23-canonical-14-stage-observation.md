# Delivery Record: Render the Canonical 14-Stage Recommendation Snapshots

## User request

The dashboard showed an unstarted score chain while the observation list reported
`动态采集与清洗 已完成 0 → 0`. The stable and dynamic filters also appeared to
share one stage.

## Cause

The Web renderer grouped the 16 internal scoring status stages into 11 display
groups and mapped both the first and second filter rows to `dynamic_filter`.
It ignored the authoritative 14 `PipelineStageSnapshot` values. A pending
pipeline could also label a zero-input candidate refresh as completed.

## Behavior change

- The Web observation list now renders the canonical 14 stage snapshots in order.
- `static_filter` and `dynamic_filter` are displayed as separate first-level and
  second-level filters.
- Zero-input stages with no pending work, failure, or output are `not_ready`;
  a valid empty result with positive input remains completed.
- The score-chain fallback now requires positive progress, so zero-valued
  placeholders cannot imply that scoring ran.

## Verification

- Dashboard JavaScript contract: passed.
- Input runtime, Tomorrow projection, bootstrap, and Web contract tests passed.
- Affected Python Ruff, mypy, compileall, and targeted diff checks passed.

## Residual risks

- The current diagnostic environment has no reachable Trader service, so live
  runtime counts and supplier readiness remain unverified.
- Existing phase 35 test-cleanup changes were preserved and are not included in
  this batch.

`Regression-Key: canonical-14-stage-observation-v1`.
