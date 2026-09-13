# Clarify Shared V2/V3 Training History and Strategy Boundaries

## User Request

Update `docs/v1v2.md` to make clear why a V2 feature can look back 250
sessions while offline research uses a 2000-session archive, whether V2 and V3
share that history, and what actually distinguishes the two scoring profiles.

Regression-Key: `v2-v3-training-history-clarification`

## Cause

Confirmed: the existing plan mentioned both limits in separate sections but did
not centrally define their different ownership. That allowed the 250-session
per-sample feature dependency to be mistaken for the complete training corpus,
and the shared 2000-session archive to be mistaken for a V3-only model input.
The plan also did not directly compare the Tomorrow-only V2 target with the
three-head V3 target.

## Added

- A single diagram separating the shared 2000-session active snapshot from V2's
  250-session maximum per-sample lookback and from development, embargo,
  confirmation, and terminal-holdout date sets.
- A V2/V3 comparison covering strategy scope, decision anchors, labels, feature
  windows, model ownership, and product positioning.
- Explicit contracts that both profiles reuse the same history archive while
  retaining separate manifests, labels, models, reports, hashes, and production
  evidence.

## Changed

- Clarified that `download_history` remains the only history-maintenance entry
  and that no scoring profile receives a duplicated database or separate
  history cutoff.
- Aligned the plan's command description with the separate V2 trainer, the
  transitional V3 Tomorrow trainer, and the planned unified three-head V3
  trainer.

## Fixed

- Removed the implication that all 2000 archived dates are fitted into one
  model or read by online inference.
- Removed the implication that V2 and V3 mainly differ by training-history
  length; their primary difference is Tomorrow-only versus independent
  Today/Tomorrow/D25 strategy heads and corresponding targets.

## Removed

- No runtime code, model artifact, configuration, schema, persisted decision,
  or historical data was removed.

## Verification

- Reviewed the clarification against the scoring-chain guide and the current
  scoring, engineering, implementation, and strategy-replay contracts.
- `tests/contract/test_document_optimization_contract.py` and
  `tests/contract/test_authoritative_document_consistency.py`: 20 passed.
- `git diff --check` passed for the four in-scope documentation files.
- Runtime, model-training, package, and browser gates are not applicable because
  this batch changes documentation only.

## Residual Risks

- This clarification does not implement or run V2/V3 training and does not
  establish historical profitability or production authority.
- Real point-in-time 14:50, industry, eligibility, filtering, and risk evidence
  remains required before either profile can claim validated returns.
- Existing unstaged scoring implementation work, `data/historyless/`, and
  `docs/train.md` are preserved and excluded from this delivery.
