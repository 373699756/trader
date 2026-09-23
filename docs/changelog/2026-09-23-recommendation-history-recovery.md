# Delivery Record: Recover Missing Recommendation History at Runtime

## User request

An outdated or incomplete local history archive must not leave the recommendation
chain with no usable feature rows. The archive should remain the acceleration path,
while missing history can be refreshed for the current recommendation batch.

## Cause

`PublishedHistoryCache` treated every archive miss as permanently unavailable.
That made `amount_median_20d` absent, which was reported as
`missing_liquidity_history` before scoring. The reason is an input-readiness
failure, not a scoring result or a business rejection.

## Behavior change

- The active download archive remains the first and only persisted history fact
  owner.
- Missing codes are recovered with bounded qfq requests using the composed
  Tencent client first and Eastmoney as fallback.
- Recovery is deadline-bounded, cancellable, wave-limited, and exposed through
  market-data health counters. It uses the same `HistoryContext` builder as
  archive rows for the current feature batch.
- Recovery results are short-lived in-memory acceleration only. They are not
  written to the archive or recommendation database.
- Failed or timed-out recovery leaves `history_data_pending`; no synthetic
  liquidity or score fields are created.

## Verification

- History recovery unit tests: `4 passed`.
- Affected market/history, input, and scoring tests: `31 passed`.
- Ruff, mypy for affected source, compileall, and affected-file diff checks
  passed.

## Residual risks

- No live supplier or full-market run was performed in this batch. Vendor
  availability, full-market latency, and real archive coverage remain unverified.
- The next independent batch must replace the synthetic first-nine stage
  snapshots and separate the stable and dynamic filter owners before claiming a
  fully serialized 14-stage runtime chain.

`Regression-Key: recommendation-history-recovery-readiness-v1`.
