# Require Independent Retraining for the Merged Tomorrow V2 Plan

## User Request

Update `docs/v1v2.md` so the merged Tomorrow V2 follows the independently
retrained-model approach instead of reusing the trained V3 heads.

Regression-Key: `tomorrow-v2-independent-retraining-plan`

## Cause

Confirmed: the plan required a new V2 training run but also retained a frozen
V1 prediction branch and instructed every OOF fold to fit that branch. V1's
20/40/60-session residual-momentum factors already belong to V2's feature
set, so adding the old V1 prediction would double-count the same information
and make the frozen-versus-refitted ownership contradictory.

## Added

- An explicit inventory of the four V2 retraining results: expanded Ridge,
  shallow LightGBM, severe-loss probability head, and Ridge/LightGBM OOF
  combination weights.
- A no-stacking contract that limits old V1, old V2, and V3 Tomorrow artifacts
  to same-date, same-stock read-only controls.
- A time-validity gate that rejects retrospective predictions from a model
  trained through or after the evaluated date as profit-admission evidence.
- A factor-ablation step that measures the incremental value of the V1
  20/40/60-session factors without feeding the old V1 prediction to V2.
- A document contract that prevents the removed legacy-model branch from
  reappearing in the plan.

## Changed

- Defined V1/V2 integration as retraining new V2 models on one point-in-time
  feature matrix containing the V1 factor subset and validated extensions.
- Limited chronological OOF fitting and nonnegative combination learning to
  the new V2 Ridge and LightGBM branches; the downside head remains separately
  trained and calibrated.
- Clarified that the common 2000-session history and deterministic base
  features may be reused, while model parameters, predictions, labels,
  calibration, and weights may not be reused across profiles.
- Updated the candidate sequence, model comparison, implementation chapters,
  impact matrix, verification requirements, and exit conditions accordingly.

## Fixed

- Removed the contradiction between a frozen V1 branch and fold-local V1
  fitting.
- Removed duplicate weighting of V1 information already present in the new V2
  feature matrix.
- Made clear that changing the reader from 61 to 251 anchors cannot make an old
  model understand new 120/250-session features.

## Removed

- The old V1 model output as a V2 combination branch.
- Any plan to copy, rename, initialize from, or stack old V1/V2/V3 model
  parameters or predictions into the new V2 bundle.
- No runtime code, configuration, current model artifact, persisted decision,
  history archive, or unrelated worktree file was removed.

## Verification

- Added and observed the focused document contract fail before updating the
  plan.
- `tests/contract/test_document_optimization_contract.py` and
  `tests/contract/test_authoritative_document_consistency.py`: 21 passed.
- Ruff passed for `tests/contract/test_document_optimization_contract.py`.
- `git diff --check` and final staged-diff review passed.
- Runtime, model training, package, browser, freeze, API, and performance gates
  are not applicable because this delivery changes only a future plan and its
  document contract.

## Residual Risks

- This plan update does not implement or execute `train-tomorrow-v2`, produce a
  V2 bundle, validate returns, or change production authority.
- Historical 14:50 and other point-in-time evidence blockers remain unchanged.
- The unrelated untracked `docs/train.md` remains outside this delivery.
