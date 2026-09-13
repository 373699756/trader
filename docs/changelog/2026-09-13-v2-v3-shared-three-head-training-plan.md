# Share all three trained model heads between V2 and V3

## User Request

Replace the independent V2 retraining plan so V2 and V3 reuse the same trained
data under `data/train`, including the Today and D25 heads in V2 rather than
sharing only Tomorrow.

Regression-Key: `v2-v3-shared-three-head-training-plan`

## Cause

Confirmed: the prior plan duplicated a Tomorrow model with a separate
`tomorrow-v2` bundle and left V2 without Today and D25 model heads. That did
not match the requested ownership: training facts, fitted parameters, and the
three head bundles should be generated once and consumed by both scoring
profiles.

## Added

- A three-directory ownership table showing that V2 and V3 both consume
  `today-v3`, `tomorrow-v3`, and `d25-v3`.
- Identity gates requiring both profiles to expose identical model, feature
  manifest, training-input, and source-snapshot hashes for each strategy head.
- An explicit post-model policy boundary: profile differences may begin only
  after identical model predictions have been produced.
- Implementation and verification sections for a common loader, one full
  training entrypoint, prediction parity, failure closure, and eventual V2
  default selection.

## Changed

- Rewrote `docs/v1v2.md` around one shared three-head training result instead
  of one V2-specific Tomorrow result.
- Made V2 a three-head profile for the future implementation plan.
- Assigned `train-v3` as the sole complete three-head training entrypoint;
  `train-tomorrow` may only maintain the same shared Tomorrow bundle.
- Kept the common 2000-session history source, maximum 251-session feature
  input, close-proxy training, risk gates, SQLite boundaries, and manual
  production activation requirements.

## Fixed

- Removed the contradiction where V2 and V3 shared history but refitted and
  stored equivalent Tomorrow models separately.
- Removed the outdated assumption that Today and D25 trained outputs belong
  only to V3.
- Clarified that a policy comparison cannot count the same shared prediction
  as evidence from two independent models.

## Removed

- The planned `train-tomorrow-v2` command and dedicated
  `data/train/tomorrow-v2/` output.
- The plan to train a second V2 copy of Ridge, LightGBM, OOF weights,
  calibration, and downside-risk parameters.
- No runtime code, existing model artifact, history database, frozen decision,
  or user worktree file was removed.

## Verification

- The focused document contract was observed failing before the plan rewrite.
- Focused document, authoritative consistency, and Changelog archive
  contracts: 24 passed.
- Ruff passed for
  `tests/contract/test_document_optimization_contract.py`.
- `git diff --check` passed.
- Runtime, training, package, browser, freeze, and API gates are not applicable
  because this batch only revises a future implementation plan.

## Residual Risks

- This delivery does not yet make V2 load the three bundles, retrain any model,
  validate returns, or switch the default profile.
- The current three bundles retain their existing close-proxy evidence and
  production-authority limitations.
- The earlier independent-retraining delivery record remains immutable
  historical evidence and is superseded by this newer plan.
- The unrelated untracked `docs/train.md` remains outside this delivery.
