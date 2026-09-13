# Tomorrow V1/V2 Merged V2 Profit-Optimization Plan

## User Request

Completely replace `docs/v1v2.md` with a new plan that merges the useful V1
capability into V2, expands V2 beyond its current 60-session feature horizon,
and makes cost-aware return improvement the governing objective. The follow-up
also requires the plan to explain whether 250-session features need offline
training, bound the historical-data volume, define OOF learning, retain the V3
training command, add a separate V2 training command, and make validated V2 the
default startup profile.

Regression-Key: `tomorrow-v1-v2-merged-v2-profit-plan`

## Cause

The previous plan restated the six inputs already consumed by V2, proposed a
fixed equal-weight score without sample-out-of-sample return evidence, retained
the 60-session ceiling, and would have overwritten the separate ownership of
the evidence-quality score and model alpha. It therefore did not constitute a
real V1/V2 integration and did not target V2's known severe-loss, turnover, and
negative Q5-Q1 failures.

## Added

- A target architecture in which the frozen V1 linear signal is an auditable
  candidate branch inside one expanded V2 model, with its final weight allowed
  to fall to zero when it provides no confirmed incremental value.
- A manifest-derived history requirement with a 61-session core boundary and
  a maximum 251-anchor extended boundary for features using up to 250 completed
  sessions.
- An explicit distinction between the 251-anchor runtime window and the
  1250-to-2000-session offline corpus, including bounded full-market row-count
  estimates, active-archive reuse, and gap-only synchronization.
- A chronological OOF definition in which every combination input is predicted
  by a model that did not train on that date, followed by nonnegative simplex
  weight learning and full-development refitting.
- A separate zero-argument `train-tomorrow-v2` command while the existing
  `train-tomorrow` command remains owned by V3.
- Ordered factor-family research, OOF combination, confirmation, terminal
  holdout, Shadow, cutover, impact-matrix, verification, blocker, and exit
  criteria.

## Changed

- Replaced the entire six-factor equal-weight plan with a cost-aware,
  point-in-time, out-of-sample return-validation roadmap.
- Made old V2 the primary control and old V1 the secondary control, while
  preserving evidence-quality score, risk, cost, DeepSeek fusion, action,
  TopK, concentration, and freeze ownership.
- Defined production consolidation under the existing V2 scoring-profile
  identity without creating V4 or another project-version namespace.
- Recorded the user's explicit target authorization to make validated V2 the
  configuration and no-argument startup default without enabling automatic
  training or weakening any evidence gate.

## Fixed

- Corrected the prior assumption that reusing V2's existing six inputs would
  add V1 information.
- Removed the planned overwrite of `base_score` by model factors and retained
  `model_prediction_rank` and model-disagreement diagnostics.
- Added explicit gates for severe loss, drawdown, turnover, capacity, ranking,
  point-in-time parity, resource limits, and production authorization.
- Corrected the ambiguity between a 250-session feature lookback and the much
  longer corpus required for OOF, confirmation, embargo, and terminal holdout.

## Removed

- The former fixed band-pass formula, two equal-weight factor groups, and the
  proposal to delete Ridge, LightGBM, model rank, and disagreement before
  proving a better alternative.
- The assumption that 61 anchors are the permanent maximum V2 history window.
- The requirement to request the same V2-default authorization again after all
  gates pass; failure of any gate still preserves the pre-cutover release.

## Verification

- Reviewed the rewritten plan against the authoritative scoring, engineering,
  implementation, and historical-strategy documents and the repository
  scoring-chain impact matrix.
- Confirmed the obsolete formula and its `short_return_score` and
  `residual_momentum_score` names are absent from the rewritten document.
- Confirmed the document retains the fixed `83.40` fusion vector, V1/V2/V3
  naming boundary, point-in-time failure closure, 61/251 history boundaries,
  profile-specific training commands, conditional V2 default cutover, and
  explicit lack of pre-validation production authority.
- `tests/contract/test_document_optimization_contract.py` and
  `tests/contract/test_authoritative_document_consistency.py`: 20 passed.
- Confirmed the document contains no trailing whitespace. This batch changes
  documentation only; runtime tests, packaging, model training, historical
  return validation, and browser evidence are not applicable.

## Residual Risks

- The plan is not an implementation and does not change the active V1 model,
  current V2 artifact, configuration, runtime behavior, or production
  authority.
- Real return validation remains blocked until historical 14:50 data and
  point-in-time industry, eligibility, filtering, and risk facts meet the
  authoritative qualification contract.
- Untracked runtime migration data under `data/historyless/` and the untracked
  `docs/train.md` file remain unrelated and are excluded from this delivery.
