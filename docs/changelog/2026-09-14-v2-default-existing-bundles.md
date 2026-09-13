# Default V2 over the existing shared bundles

## User Request

Finish the remaining runtime code before any retraining work so that the normal
`run.sh` path loads the existing `data/train/{today-v3,tomorrow-v3,d25-v3}`
artifacts and can score Today, Tomorrow, and D25. Clarify that 120/250-day
features and 251-day runtime history are not part of the current artifacts.

Regression-Key: `v2-default-existing-bundle-runtime`

## Cause

Confirmed: the shared V2/V3 three-head loader and predictors were already
implemented and the tracked bundles were valid, but both startup scripts still
constructed a hidden `--profile v1` argument. That duplicated the default held
by `config/strategy.json`, prevented a configuration-only V2 default, and made
the no-argument path bypass the existing three-head artifacts.

## Added

- Added a real-artifact scoring regression that loads the tracked Today,
  Tomorrow, and D25 bundles and runs all three predictors through the production
  scoring service.
- Added documentation that defines the shared V2/V3 model path once, lists
  profile differences separately, and records future return-optimization
  directions without treating them as active features.

## Changed

- Changed the configured default scoring profile to V2.
- Changed the Bash and PowerShell launchers to omit `--profile` unless the user
  explicitly supplies it, leaving `config/strategy.json` as the only default
  profile authority. Explicit V1, V2, and V3 process overrides remain valid.
- Updated the README and the scoring, engineering, implementation, replay, and
  V1/V2 planning documents to describe the current default and shared artifact
  behavior consistently.

## Fixed

- No-argument startup no longer silently overrides the configured profile with
  V1.
- The implementation plan no longer presents 120/250-day candidates or a
  251-day reader as prerequisites for using models whose current manifests only
  contain the existing 61-session feature contract.
- The V3 capability table no longer describes separately owned predictors even
  though V2 and V3 load the same bundle bytes.

## Removed

- Removed the duplicated startup-script default profile value.
- No model artifact, history archive, runtime database, or user file was
  removed or rewritten.

## Verification

- Focused entrypoint, settings, bootstrap, model-loading, production-scoring,
  and documentation contract tests passed.
- Ruff and mypy checks for the directly affected Python boundaries passed;
  `bash -n run.sh` passed.
- A real no-argument `./run.sh` startup returned `profile_id=v2` from
  `/api/status` and exposed active Today, Tomorrow, and D25 heads with the
  tracked model hashes; the service was then stopped normally with Ctrl+C.
- `make format-check`, `make lint`, `make type-check`, `make test`, and
  `make package` passed on the final complete implementation; mypy checked 394
  source files and the strict naming/refactor check reported zero diagnostics.
- `make performance-check` and the no-argument `./run.sh check` both passed for
  V2 with `network_calls=0`; the latter completed configuration, research
  status, automation status, and performance stages successfully.
- Repository-external wheel installation passed and found all 9 required
  resources. Browser layout, supplier accuracy, schema migration, and live
  DeepSeek checks were not applicable because this batch changes no Web UI,
  supplier/parser, persistence schema, or DeepSeek behavior.
- Final complete-diff Review and `git diff --check` completed with zero known
  findings; only this batch's files are staged for delivery.

## Residual Risks

- The existing artifacts remain daily-close engineering proxies with
  `historical_data_insufficient`, `point_in_time_parity=false`,
  `production_authority=false`, and no modeled severe-loss probability. The
  default selection is an explicit user runtime decision, not a claim of
  historical profitability.
- 120/250-day features, OOF ensemble weights, a severe-loss head, and any V2/V3
  post-model policy differences still require a future shared retraining and
  evidence batch. They are not computed by the current runtime.
- The unrelated untracked `docs/train.md` remains outside this delivery.
