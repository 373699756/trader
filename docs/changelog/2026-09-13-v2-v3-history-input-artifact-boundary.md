# Clarify V2/V3 History Input and Training-Artifact Directories

## User Request

Update `docs/v1v2.md` to state how V2 and V3 reuse the 2000-session training
data in relation to `data/train/today-v3`, `data/train/tomorrow-v3`, and
`data/train/d25-v3`.

Regression-Key: `v2-v3-history-input-artifact-boundary`

## Cause

Confirmed: the three profile directories contain model-delivery artifacts, not
the 2000-session observations. Their four fixed JSON files record input
identity, model payload, report evidence, and bundle hashes. The directories
total about 4.4 MB, while the active BaoStock history is about 13 GB. Treating
the profile outputs as the V2 history source would reverse the dependency and
introduce an undeclared V3-to-V2 stacking path.

## Added

- A physical data-flow diagram separating the BaoStock history input,
  reproducible temporary samples, V3 head outputs, and the planned V2 output.
- Explicit V2/V3 comparison rows for training-fact input and model-bundle
  output directories.
- Acceptance checks preventing V2 from reading V3 models, reports, metadata,
  labels, or predictions as training facts.

## Changed

- Kept the user's intended source unification: V2 and V3 bind the same rolling
  2000-session active history snapshot and may reuse deterministic base-feature
  computation identified by snapshot, fact revision, and FeatureSpec.
- Clarified that V3 bundles may be used only for identity verification,
  loading, and paired benchmark evidence; V2 constructs its own Tomorrow
  samples and writes its own bundle.

## Fixed

- Prevented the misleading interpretation that a file named
  `training-input.json` contains daily observations. It contains input scope,
  codes, dates, contracts, and hashes, not the historical training rows.

## Removed

- No runtime code, model artifact, configuration, schema, persisted decision,
  history database, or unrelated worktree file was removed.

## Verification

- Inspected all four artifact kinds under the three V3 head directories and
  compared their keys and aggregate size with the active history directory.
- `tests/contract/test_document_optimization_contract.py` and
  `tests/contract/test_authoritative_document_consistency.py`: 20 passed.
- Scoped `git diff --check` passed for the four delivery files.
- Runtime, training, package, and browser gates are not applicable because this
  delivery changes documentation only.

## Residual Risks

- This documentation correction does not implement the V2 trainer or prove
  return improvement.
- The already-delivered V3 three-head implementation and the unrelated
  untracked `docs/train.md` remain outside this documentation-only delivery.
