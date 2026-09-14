# Recover recommendation-pipeline evidence across freeze and restart

## User request

The scoring-input and recommendation-funnel cards still collapsed to generic missing-data text or a contradictory
`136 -> — -> 0` after a formal decision had already scored candidates. The two cards must show the previously agreed
front and back halves of the recommendation chain directly in the existing layout. Historical downloading, historical
training and model artifacts remain outside this batch.

Regression-Key: `recommendation-pipeline-stage-observability-recovery`

## Cause

The 15-stage typed pipeline was owned only by `MarketDataAdapter._input_quality`. Formal decisions persisted aggregate
coverage, filter reasons and maximum score, but not the stage audit. A normal restart restored the formal decision while
the process-local input-quality map was empty. The Web then combined the restored evaluated count and final pool count
with an unknown action stage, and the JavaScript regression suite incorrectly asserted that half-pipeline as valid.

## Added

- A domain-owned immutable pipeline audit carried by every newly built scored decision.
- Canonical persistence codec coverage for all 15 stages, including states, nullable counts, ranges, thresholds, facets
  and reason counts, with hash-preserving read compatibility for prior records that lack the additive field.
- Regression coverage for local construction, official projection, codec recovery, current/history GET, complete SSE
  replacement and browser rendering without scheduler input-quality state.

## Changed

- The scoring-input card now reads the decision audit for dynamic filter through complete evidence scoring.
- The recommendation-funnel card reads the same decision audit for model/cost gate through final concentration, including
  local-risk, DeepSeek/fusion, action classification, final score range and highest score.
- Scheduler input quality remains the source only while acquisition/scoring has not produced a decision. Current,
  frozen, restarted and historical views use the decision-owned audit; SSE replacements retain it atomically.
- Prior formal records are not rewritten. Their cards show the actually preserved population, evaluated, rejected,
  maximum-score and filter-reason aggregates and explicitly state that stage observations were not saved.

## Fixed

- A ready frozen decision can no longer render as `136 -> — -> 0` merely because the service restarted.
- Existing aggregate evidence is no longer mislabeled as “no scoring input data”.
- Browser tests no longer treat a mixture of aggregate counts and unknown stages as a valid funnel.

## Removed

- Removed the fabricated legacy `evaluated -> unknown action -> selected` half-funnel and the contradictory claim that
  a restored, already-scored decision had no scoring-input data.

## Verification

- Targeted decision identity/codec, decision query, decision stream, market-input, Web/API, bootstrap and JavaScript
  regressions passed.
- Affected Ruff and mypy checks passed.
- Full repository, package, desktop and restarted-service gates are recorded at batch completion.

## Residual Risks

- A stage audit destroyed before this change cannot be reconstructed without fabrication. Existing records therefore
  retain an explicit aggregate-only view; new records preserve the full chain across restart.
- This batch does not download history, change history readers, train models, replace bundles, or alter formulas,
  thresholds, fusion, ranking, freeze times or recommendation membership.
