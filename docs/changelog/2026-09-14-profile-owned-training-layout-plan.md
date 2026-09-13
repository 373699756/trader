# Profile-owned V2/V3 training layout plan

## User Request

Replace the shared-artifact training plan for the 120/250-day features and the
251-session inference window. Create `data/train/v2` and `data/train/v3`, each
with `today`, `tomorrow`, and `d25` strategy directories, and synchronize the
existing `today-v3`, `tomorrow-v3`, and `d25-v3` contents into the corresponding
V3 directories. The requested `todat` directory is interpreted as `today`, the
existing strategy identifier.

Regression-Key: `profile-owned-training-layout-plan`

## Cause

Confirmed: the current locator and training contract hard-code the three flat
`*-v3` directories, while the previous plan also required V2 and V3 to share
the same fitted artifacts. That cannot represent a V2 feature manifest with
120/250-day inputs and a 251-session inference boundary while preserving V3 as
the existing 61-session baseline.

## Added

- Added empty `data/train/v2/{today,tomorrow,d25}` targets. They contain only
  `.gitkeep`; no existing V3 artifact is presented as a trained V2 model.
- Added `data/train/v3/{today,tomorrow,d25}` and copied all four paired files
  for each head byte-for-byte from the corresponding flat V3 directory.
- Added contracts for exact V3 byte equality, empty V2 targets, explicit
  `.gitignore` allow-listing, and the profile-owned training plan.

## Changed

- Changed the target architecture so V2 and V3 share the 2000-session history
  facts and generic training/loading framework, but train, publish, and load
  their own three-head bundles.
- Defined V2 as the future 120/250-day feature profile with a bounded maximum
  of 251 inference sessions; V3 retains the current 61-session feature contract.
- Updated the scoring, engineering, implementation, replay, and V1/V2 planning
  documents to distinguish current runtime behavior from the target cutover.

## Fixed

- Removed the planning contradiction that required distinct V2 long-window
  features while also requiring V2 and V3 model, manifest, and prediction
  identities to remain equal.
- Prevented an empty V2 directory or staged V3 copy from being treated as an
  active runtime source before a complete atomic cutover.

## Removed

- No current model, history archive, runtime database, or user file was removed.
- The flat `data/train/{today-v3,tomorrow-v3,d25-v3}` directories remain the
  read-only current runtime source until the future profile-aware cutover.

## Verification

- Focused authoritative-document, V1/V2-plan, strategy-replay, download-plan,
  packaging-layout, and scoring-chain contracts passed (49 tests).
- Ruff format and lint checks passed for all five changed Python contract files.
- All twelve nested V3 files compared byte-for-byte equal to their flat source
  files; the three V2 strategy directories contain only `.gitkeep`.
- `./run.sh check` passed all four stages with `scoring_profile=v2` and
  `network_calls=0`, proving the staged layout does not interrupt current scoring.
- Final complete-diff Review and `git diff --check` passed with zero known
  findings; only this batch's files are selected for delivery.
- Full repository gates are not applicable to this staging batch because it
  changes no Python runtime, model bytes, scoring behavior, schema, or package
  resources.

## Residual Risks

- V2 contains no trained artifacts yet. The 120/250-day feature computation,
  251-session read/prewarm/cache boundary, profile-aware trainer/due state,
  locator/codec/CLI cutover, retraining, and return gates remain future work.
- Until that atomic cutover, default V2 and explicit V3 continue to load the
  old flat three-head bundle. Keeping both flat and nested V3 copies is a
  bounded migration state, not a permanent dual source of truth.
- Existing V3 artifacts remain daily-close engineering proxies with
  `historical_data_insufficient`, `point_in_time_parity=false`, and
  `production_authority=false`; byte synchronization does not improve their
  profitability evidence.
- The unrelated untracked `docs/train.md` remains outside this delivery.
