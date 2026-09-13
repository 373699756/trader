# V2/V3 shared three-head runtime

## User Request

Continue the executable `docs/v1v2.md` plan by making V2 and V3 consume the
same existing Today, Tomorrow, and D25 trained bundles. Do not retrain, modify
`data/train`, or switch the default profile in this batch.

Regression-Key: `v2-v3-shared-three-head-runtime`

## Cause

Confirmed: V3 owned the three-head bundle contracts, codec, locator,
publication repository, predictors, and profile assembly, while V2 still
loaded a separate packaged Tomorrow artifact. Adding more V2-specific
predictors would have preserved duplicate ownership and allowed the two
profiles to drift.

## Added

- Added the neutral `infra/scoring/head_bundles` owner for the shared head
  contracts, fixed-file codec, active-bundle locator, crash-safe publication,
  deterministic predictor, and runtime profile assembly.
- Added contract and regression coverage proving that V2 and V3 expose all
  three heads, retain their selected runtime profile identities, bind the same
  exact model hashes and feature manifests, and return equal predictions for
  equal inputs.
- Added fail-closed coverage for a missing or identity-corrupt shared bundle
  under both V2 and V3.

## Changed

- `load_scoring_profile()` now routes V2 and V3 through one shared loader over
  `data/train/{today-v3,tomorrow-v3,d25-v3}` and injects only the selected
  runtime profile ID into each predictor.
- Renamed the in-process exposure contracts to semantic legacy and shared
  trained-head names; immutable serialized `v3_head_*` identities remain
  unchanged so existing artifact hashes stay valid.
- Updated the baseline identity audit, package-resource verification, command
  descriptions, and all four current strategy/engineering/implementation/
  replay documents to reflect shared three-head ownership.

## Fixed

- V2 no longer reports Tomorrow-only capability or uses a model different from
  the V3 Tomorrow head.
- A serialized bundle's historical `profile_id=v3` provenance no longer leaks
  into the selected runtime predictor identity; V2 reports V2 while preserving
  exactly the same model bytes and prediction.
- Shared profile assembly now rejects duplicate heads instead of silently
  accepting a repeated strategy when all three strategy keys are present.
- Neutral bundle diagnostics now describe trained heads rather than reporting
  every V2 load failure as a V3 failure.
- The shared codec, locator, and publication repository no longer live below a
  V3-only runtime package.

## Removed

- Removed the packaged V2 model, its codec, its Tomorrow predictor, its profile
  builder, and its wheel resource declaration.
- Removed V3-only runtime wrappers and duplicate per-strategy predictor files;
  V3 retains only its offline training implementation and the immutable schema
  strings required to decode already-published artifacts.
- No training artifact, history archive, runtime database, frozen decision, or
  user file was removed or rewritten.

## Verification

- Contract-first observation: the new neutral-owner tests failed during
  collection before `infra/scoring/head_bundles` existed.
- Focused scoring, training, publication, architecture, bootstrap, document,
  and history-repack regressions passed after implementation.
- Real tracked-bundle smoke loaded V2 and V3 with three heads; model hashes were
  `940d251d...423` (Today), `a4f71a53...e36` (Tomorrow), and
  `83d49d93...661d` (D25), and same-input predictions matched for every head.
- `make format-check`, `make lint`, `make type-check`, `make test`, and
  `make package` passed on the final complete diff; mypy checked 394 source
  files and the strict refactor/naming lint reported zero diagnostics.
- Repository-external wheel installation passed with all 9 required resources;
  the removed V2 packaged model is no longer an installation requirement.
- `make performance-check` passed its absolute, allocation, relative-regression,
  equivalence, latency, and memory gates with `network_calls=0`.
- Browser, live-supplier, database migration, freeze, API/SSE, and DeepSeek
  runtime checks were not applicable: this batch changes no Web surface,
  external I/O, persistence schema, scheduler/freeze rule, public response, or
  DeepSeek path.
- Final complete-diff Review and `git diff --check` completed with zero known
  findings; only this batch's files are staged for delivery.

## Residual Risks

- The shared artifacts intentionally remain daily-close engineering proxies
  with `point_in_time_parity=false`, `historical_data_insufficient`,
  `production_authority=false`, and `automatic_model_update=false`.
- This batch does not implement 120/250-day features, OOF combination weights,
  a severe-loss head, terminal holdout, Shadow evidence, or default V2
  selection. Those remain subsequent complete sections of `docs/v1v2.md`.
- The unrelated untracked `docs/train.md` remains outside this delivery.
