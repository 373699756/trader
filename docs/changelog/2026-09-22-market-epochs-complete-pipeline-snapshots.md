# Expose immutable market epochs and complete pipeline snapshots

## User request

Complete the active refactor batch before starting the dashboard score-summary layout change. Close the remaining
runtime work for immutable static/dynamic epochs and the complete 14-stage recommendation snapshot chain.

Regression-Key: `market-epochs-complete-pipeline-snapshots-v1`

## Cause and current-state judgment

The runtime exposed only the first nine stage snapshots, reused one batch identity across every stage, and left the
score, risk, merge, action and final-selection stages visible only through a separate audit projection. Reference-data
changes invalidated market-feature caches, but the public health projection did not expose the static reference epoch
beside the dynamic market epoch. Web consumers therefore could not verify one continuous backend-owned batch chain.

## Changed

- Every scored and pending Tomorrow/D25 runtime status now carries all 14 ordered `PipelineStageSnapshot` values.
  Adjacent stages use matching immutable handoff identities and matching output/input counts.
- Stages 10–14 are generated from the existing typed scoring audit: local score, DeepSeek risk review, fixed merge,
  downside/action gating, and the single final-selection boundary. Pending work remains `not_ready` or `degraded` and
  is not reported as business rejection.
- The static reference epoch has one content-addressed owner shared by scoring-cache identity and health reporting.
  Runtime status now explicitly exposes `reference_epoch` and `market_epoch`; HTTP keeps both behind its field
  whitelist and continues to omit source versions, missing-field identities and supplier payloads.
- The Web continues to consume backend rejection counts and does not reconstruct filtered totals from input/output
  differences. The final stage remains the one combined TopK, concentration, freeze and publication boundary.
- Runtime-health presentation was extracted to `status_health.js`, keeping the existing public helpers while restoring
  the repository's 1200-line source limit. The architecture contract now checks the current recommendation-owned
  normalization path instead of the deleted pre-migration owner.

## Verification

- Targeted application, input-runtime, Bootstrap, Web-contract and market-history tests passed.
- Ruff and mypy passed for the affected Python sources and contracts.
- `compileall`, JavaScript syntax/state checks and `git diff --check` passed.
- The isolated browser refresh gate passed after one threshold-edge retry; the confirming run measured 17 ms
  patch-to-paint P95 against the 100 ms budget, with SSE patch application and retention checks passing.

## Residual risks

- Live supplier behavior remains dependent on locally configured credentials and source availability. The isolated
  live diagnostic can report a missing canonical snapshot when no supplier completes successfully.
- Browser acceptance requires a locally available WebDriver. This batch does not change the dashboard layout; the
  score-summary overflow fix remains a separate follow-up batch.
