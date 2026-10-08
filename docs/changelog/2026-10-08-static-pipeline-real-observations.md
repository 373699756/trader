# Delivery Record: Lightweight Static Pipeline and Actual Observations

## User request

Continue unfinished work in `docs/todo.md`: lightweight static inputs, filtering
before expensive history requests, immutable handoffs and truthful pipeline audit.

## Cause

Confirmed: static collection, normalization and filtering accepted complete
historical feature objects and had no production callers. Pipeline status rebuilt
static population facts from scored features and reported synthetic zero latency.
The selector also produced a second set of static observations.

## Behavior change

- Immutable `StaticIssuer` records contain issuer facts without dynamic quotes
  or historical features. A resident static baseline reuses its identity when
  only prices/timestamps change; accepted reference or issuer facts invalidate it.
- The market coordinator runs the actual first four stages before history I/O.
  Missing code/name enters normalization pending; permanent exclusions retain
  their registry-owned reasons. No additional supplier client/request is added.
- Each stage emits a new immutable output with continuous IDs/counts, measured
  monotonic latency and baseline source age. Reuse preserves the original success
  time. Empty input remains not ready; a filter rejecting positive input can finish.
- Full-market batches carry these observations through pending/scored status and
  decision updates to the existing read-only API/Web projection. Missing static
  observations fail explicitly. Scored candidate facts now own only stages 5–9.
- Original market inputs and feature-cache publication are paired under one lock;
  excluded and pending issuers remain visible in cached-read audit. The obsolete
  static snapshot alias module is deleted with its consumers.

## Verification

Expanded market, input-runtime, selection/fusion, bootstrap, Web, architecture and
documentation contracts: 347 passed, one known pre-existing projection case
excluded (`invalid_candidate_quote_as_transient`, recorded in stage 45).
Affected Ruff/format and source mypy passed. Full diff review and
`git diff --check` passed.

Regression coverage includes dynamic-only baseline reuse, reference/fact
invalidation, pending identities before history, permanent exclusion before all
downstream requests, original input immutability, measured latency and age,
failed refresh retaining cached features, static audit preservation in status and
decision updates, missing observations and empty input.

Read-only runtime diagnostics sampled twice in sandbox and host-network contexts;
both reported `connection_failed`, zero successful samples and no release identity.
No live-service verification is claimed.

## Residual risks

The baseline population still comes from accepted full-market quotes; independently
driving it from the complete official issuer universe remains unfinished. Source
health covers accepted records, not all attempted suppliers. Stages 5–9 still use
the existing dynamic projection with synthetic latency/health. Real supplier,
two-cycle service, browser, freeze/recovery and long-term coverage gates remain open.
No production parameter, supplier quota, active data or service restart is changed.
The user's scoring-ratio TODO remains outside this commit.

`Regression-Key: market-epochs-complete-pipeline-snapshots-v1`.
