# Separate data readiness from business filtering

## Added

The scoring-input funnel reported roughly `5287→56`, attributing thousands of
stocks to missing history and stale quotes. Missing data must be downloaded or
refreshed instead of being presented as a business rejection.

## Changed

The history warmup path was capped by the per-board candidate capacity and the
runtime passed `candidate_pool_size * 3` as its preload limit. The same capped
set was used to build the full-market feature population, so infrastructure
coverage appeared as dynamic-filter rejection.

- Added a typed deferred filter severity and separated data/quote readiness
  audits from confirmed business rejection audits.
- Missing or invalid liquidity, stale/future/invalid quote inputs and failed
  normalization no longer count as `rejected_count` or consume board capacity;
  they are marked for `data_pending` or `refresh_pending` handling.
- Added the `input_readiness` funnel stage as the first typed pipeline stage, so
  the population of issuers with complete trustworthy input is reported
  separately from dynamic business filtering. `ScoredCandidateStageCounts` now
  carries `input_ready_population`, and `split_filter_reason_counts` partitions
  population filter reasons into business rejection and readiness gaps.
- Updated the input-quality card, status and SSE projections to lead with the
  readiness stage while keeping the previous aggregate views compatible.
- Added full supported-population history-code selection for compact summary
  preparation while retaining the bounded raw-bar candidate helper.
- Updated the scoring and engineering contracts to document readiness states,
  background acquisition, and truthful funnel reporting.

## Fixed

- Prevented missing or stale input from inflating dynamic business rejection
  counts and from consuming board capacity.

## Removed

- No scoring formula, threshold, freeze, model, risk, or action rule was
  removed or loosened.

## Verification

- `python -m compileall` for all changed Python modules.
- `.venv/bin/python -m pytest -q tests/unit/domain/test_filters.py tests/unit/domain/test_tomorrow_selection.py tests/unit/application/test_tomorrow_projection.py`
- Component history tests updated for full-population preparation (full suite
  and release gates remain to be run after the batch is complete).

## Residual risks

- Existing runtime suppliers can still fail or remain stale; the scheduler's
  retry and provider diagnostics must be exercised in the final live-process
  gate.
- API/SSE status projections and desktop wording need the final end-to-end
  contract/browser pass before release.
