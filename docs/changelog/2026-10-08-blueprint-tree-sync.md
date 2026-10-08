# Synchronize the refactor blueprint tree with the delivered code

## User request

The user asked to review the project against `docs/项目重构详细.md` and then,
for the discovered drift, to rewrite the section 3.1 target tree to match the
actual delivered tree as documented in `docs/02_工程设计.md`, eliminating the
blueprint as a second naming truth source (the fallback of declaring the
blueprint superseded was not needed). The user then asked to additionally fix
two more findings: document the second stage vocabulary (the 16-key API status
projection) in the blueprint, and remove the local `__pycache__` residue
directories.

## Reason and current state

The structural skeleton, dependency direction, combination root, entrypoints,
data layout, and the 14-stage pipeline contract were delivered as planned and
verified in earlier batches. However, the blueprint's section 3.1 tree still
described planned subpackages that were never created or were later renamed
during stages 15-43 of the implementation ledger (for example
`download/domain/planning/`, `training/infra/samples/`,
`recommendation/infra/scoring/profile/v2|v3`,
`recommendation/infra/persistence/eligibility_ledger.py`,
`http_api/routes/status_routes.py`, and `web/templates/base.html`). This left
two conflicting naming authorities: the blueprint tree and the authoritative
tree in `docs/02_工程设计.md`. Separately, the blueprint's chapter 5 documented
only the 14-layer `PipelineStageSnapshot` observation contract and never
mentioned the 16-key evaluation-substage vocabulary that `GET /api/status`
exposes per short-term strategy under `input_quality.pipeline`, and six
orphaned directories under `src/trader/` contained only stale `__pycache__`
compilates of modules that had already moved to their `recommendation/`
owners.

## Changed

- Rewrote the section 3.1 tree in `docs/项目重构详细.md` to the actual tracked
  tree of `src/trader`: real flat modules for the download and training
  businesses, the real `recommendation/application/{ports,runtime,pipeline}`
  layout with the actual per-stage files (including `quality_check/` and the
  full `freeze_publish/` file set), the real `http_api/`, `web/`, `infra/`,
  and `entrypoints/` contents, and the note that the recommendation business
  has no business entrypoints of its own. The user's existing TODO comments on
  the `entrypoints/` directory and the tree rationale were preserved verbatim.
- Corrected three living-contract references that contradicted the delivered
  code: the recommendation-side profile assembly point is
  `recommendation/infra/scoring/profile_factory.py` delegating to
  `training/infra/model_bundles` (not a `scoring/profile/v2|v3` subpackage);
  in-group concentration, backfill, and final TopK are owned by
  `recommendation/domain/selection/ranking.py` (not by nonexistent
  `concentration_policy.py`/`topk_selection.py`); the first-stage eligibility
  facts are persisted by `SQLiteIssuerEligibilityRegistry` in
  `recommendation/infra/persistence/issuer_eligibility.py` (not by a
  nonexistent `EligibilityLedger`).
- Corrected the stage-10 plan line that still claimed the composition root
  assembles a `recommendation/entrypoints/commands.py` runner; the historical
  migration-source paths in chapter 10 (for example `application/history/`)
  remain untouched as immutable audit text required by the contract test.
- Added subsection 5.1.1 to the blueprint documenting the second stage
  vocabulary: the fixed 16-key `PIPELINE_STAGE_ORDER` defined in
  `recommendation/domain/evidence/pipeline.py` that `GET /api/status` exposes
  per short-term strategy under `input_quality.pipeline`, including its state
  model, facet semantics (`input_readiness` readied population vs business
  rejections, `input_coverage` parallel facets, `action_gate` and
  `concentration` facets), the partial correspondence to the 14-layer chain,
  and the prohibition on reassembling the 16 substages into the display layer.
  The 14-layer `PipelineStageSnapshot` remains the only domain pipeline
  numbering for the monitoring display.
- Follow-up alignment moved the shared `quote_normalization.py` primitive into
  the documented top-level layout, made the functional package contract assert
  the actual recommendation normalization owner, and made the Web contract
  consume `PIPELINE_STAGE_ORDER` directly instead of duplicating the 16 keys.
- Follow-up documentation now states that recommendation has no business
  entrypoint and documents the shared 16-key pipeline consumers in decision,
  SSE, and freeze projections.

## Fixed

- Deleted the six orphaned residue directories that contained only stale
  `__pycache__` compilates and no tracked files: `src/trader/web/api/`,
  `src/trader/training/evaluation/`, `src/trader/infra/artifacts/`,
  `src/trader/infra/persistence/`, `src/trader/infra/scoring/`, and
  `src/trader/infra/market_data/normalization/`. Their source modules live
  under `recommendation/` owners now, so nothing can import from these paths
  as active modules; the functional package contract asserts that the retired
  shared normalization path remains absent.
- Removed the remaining ignored cache-only directories under the retired
  recommendation profile and top-level market service paths.

## Removed

None.

## Verification

- A tree-existence check verified all 90 file-level paths listed in the
  rewritten 3.1 tree exist under `src/trader` (the single reported miss was a
  path-joining defect of the one-off checker itself for the top-level
  `bootstrap.py`, which exists).
- Ran the blueprint implementation-plan contract test
  (`tests/contract/test_refactor_blueprint_implementation_plan.py`), the
  changelog archive contract test
  (`tests/contract/test_changelog_archive_contract.py`), the functional package
  boundary and Web contracts, and the architecture naming contract test
  (`tests/contract/test_architecture.py`): all passed.
- `git diff --check` passed; the deleted residue directories never held
  tracked files, so the deletion is invisible to git.
- Full gate suite not run: this batch changes documentation, contract tests and
  local caches; production code, generated configuration and activity data are
  unchanged (risk class 5.2). The residue deletion cannot affect imports
  because a bare `__pycache__` directory without `__init__.py` is not
  importable.
- The follow-up fixes remain documentation and contract-test changes; no
  production runtime, external supplier, or activity data was changed.

## Residual Risks

- The user's open questions recorded in `docs/todo.md` (static-stage type
  inversion and count-difference inference for the first nine stages of the
  16-key projection) are implementation gaps against the documented contract
  and remain pending.
