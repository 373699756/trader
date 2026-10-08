# Delivery Record: Fair Rotation for Missing Recommendation History

## User request

Continue unfinished work from `docs/todo.md`, including the code-order bias in
bounded recovery of missing qfq history.

## Cause

Confirmed: each recovery batch selected the first missing codes again. Persistent
failures and expired successes could occupy the same bounded slots while later
issuers remained pending. Requests were also dispatched before checking the
batch deadline. The HTTP status whitelist omitted existing recovery counters.

## Behavior change

- The existing recovery owner selects least-recently-dispatched missing issuers,
  independent of input order. Failures rotate too; unstarted waves retain priority.
- Deadline budgets and cache TTL use an injectable monotonic clock. Dispatch,
  queued work and fallback requests check the remaining budget.
- In-flight codes are reserved across calls. Timed-out work releases its
  reservation on completion; late bars cannot populate the current batch or cache.
- Typed recovery status reaches market health and the HTTP whitelist, including
  requested, cached, planned, dispatched, deferred, successful, failed, timed-out,
  in-flight and elapsed counts. Stock lists remain private.
- Missing history remains pending. The download business remains the sole
  persisted history/checkpoint owner; production strategy parameters are unchanged.

## Verification

Recovery, market-feature and HTTP contracts: 29 passed. Covered recurring failures,
expired caches, reversed input order, expired deadlines, nested-inline waves,
fallback deadlines, late completion, typed health and public-field privacy.
Expanded market components, recovery, Web, architecture and Changelog contracts:
224 passed.
Affected Ruff, format-check and source mypy passed. Full diff review and
`git diff --check` passed.

Read-only runtime diagnostics used two samples in both sandbox and host-network
contexts. Both returned `connection_failed` with no successful samples or release
identity. This provides no evidence of a current product failure or live fix.

## Residual risks

Rotation metadata is process-local; restarting does not preserve a recovery
cursor. Long-term coverage still requires download checkpoint completion and
supplier evidence. Running vendor calls are not forcibly interrupted and retain
their adapter timeout; late results are discarded. No supplier quota, active data,
service restart, browser run or production performance claim is included.
The real static/dynamic pipeline and synthetic stage observations remain unfinished.

`Regression-Key: recommendation-history-recovery-readiness-v1`.
