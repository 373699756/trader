# Recommendation pipeline stage observability

## User request

The desktop page showed a long flat funnel with many misleading zeros, split the same decision into “main”,
“observation” and “classification” pseudo-lines, and separated the highest score from the stage where it was
produced. The requested result is one auditable recommendation chain beginning at the dynamic hard filter,
with counts, score ranges and causes visible directly in the existing cards. The large recent-error card should
be removed, while active runtime failures remain reachable. Historical download, historical training and model
artifacts are explicitly outside this batch.

Regression-Key: `recommendation-pipeline-stage-observability`

## Cause and current-state judgment

The application exposed one flat `supply_funnel` whose default integer fields made “not executed yet”
indistinguishable from a completed empty stage. The Web layer then rearranged those fields into three visual
lines, including parallel input coverage and action classifications as if they were serial filters. The browser
and the runtime diagnostic script each interpreted that flattened structure independently, so the page could
not reliably locate the first broken score-input or decision stage.

## Added

- One immutable typed recommendation-pipeline status with ordered stages, explicit lifecycle state, nullable
  input/output counts, metric ranges, thresholds, facets and bounded reason counts.
- Direct stage aggregation for board reliability, history length, input completeness, candidate/base/local/
  DeepSeek/final scores, quote age, model prediction/cost/net utility/disagreement and risk deductions.
- Regression coverage for the typed API boundary, pending-versus-zero semantics, inline Web rendering and
  read-only runtime diagnostics.

## Changed

- `scheduler.input_quality.*.pipeline` is now the only public recommendation-chain projection used by API,
  Web, SSE-triggered refresh rendering and the diagnostic report.
- The full-width scoring-input card shows dynamic filter through complete evidence scoring inline. Parallel
  quote, security-master and history coverage stays a parallel facet rather than becoming three fake filters.
- The recommendation funnel shows model/cost diagnostics through action and concentration selection. It also
  owns the unified final-score range, highest score and Top 3 list; the separate highest-score card is gone.
- Unknown or unexecuted values render as `—`, running acquisition renders as “采集中”, completed empty counts
  render as `0`, missing metric samples render as “无样本”, and a real zero score remains `0.00`.
- The large recent-error card is removed. A compact badge appears only while active issues exist and opens the
  bounded error drawer; recovered history remains available in the status contract without occupying the page.

## Fixed

- A pending downstream stage can no longer appear as a confirmed `0 → 0 → 0` result.
- Coverage dimensions, threshold classifications and pool selection are no longer displayed as one false
  twenty-plus-level serial filter.
- Runtime diagnostics now parse and report the same typed stage source used by the page instead of maintaining
  a second flattened funnel interpretation.

## Removed

- The public `supply_funnel` projection and its browser/diagnostic consumers.
- The “主线 / 观察支线 / 分类统计” rendering, the permanent recent-error card and the independent
  “统一评分最高” summary card.

## Verification

- Targeted application, Web/API contract, JavaScript state and runtime-diagnostic regression suites passed,
  including canonical-stage validation, dynamic-filter cause ownership and unknown-versus-empty rendering.
- `make format-check`, `make lint` and `make type-check` passed; strict refactor/naming debt remained zero and
  mypy checked all 396 source files successfully.
- `make package` passed and built both the source distribution and wheel with the revised Web resources.
- The Firefox desktop acceptance runner passed at 1280x720, 1440x900 and 1920x1080 with no browser error,
  page-level horizontal overflow or long-layout overlap. The live 5000-port browser refresh gate passed with
  DOM refresh P95 1.058 seconds and SSE patch-to-paint P95 16 milliseconds.
- The read-only live Web diagnostic completed without failed checks. It remained `degraded` only because all
  three restored frozen short-strategy snapshots already carry controlled snapshot-quality degradation.
- `make test` was executed across the repository. All task-related tests passed; eight pre-existing contract
  failures remain in unrelated naming and historical-training roadmap assertions. Their asserted phrases are
  already absent at the current `HEAD`, and this batch does not alter those forbidden historical boundaries.

## Residual Risks

- Stage ranges and reasons are aggregate diagnostics and intentionally contain no stock identities; individual
  evidence remains in the existing selected-stock detail and controlled runtime records.
- This batch does not download history, change historical-data readers, train a model, replace a model bundle,
  or alter scoring formulas, thresholds, fusion, TopK, scheduling or freeze behavior.
- The repository-wide test target is not fully green because of the eight pre-existing unrelated contract
  failures recorded above; correcting those contracts requires a separate historical/naming delivery batch.
