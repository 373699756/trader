# Delivery Changelog Archive

`CHANGELOG.md` is the bounded entry point for the latest delivery batch. This
directory holds the detailed audit record for each delivery batch, while
`archive/` holds immutable legacy snapshots migrated from the former monolithic
root changelog.

Each current delivery record includes the user request, evidence, behavior
change, verification, and residual risks under the standard sections. Search
this directory with `rg` and open only the matching record; do not load the
legacy snapshot for routine delivery work.

## Records

- [Reconstruct explicit early-listing BaoStock qfq gaps](2026-09-23-baostock-early-listing-qfq-reconstruction.md)
- [Bound routine history maintenance to changed data](2026-09-22-bounded-history-incremental-maintenance.md)
- [Show the 14-stage funnel counts on the dashboard](2026-09-22-dashboard-14-stage-count-funnel.md)
- [Separate score leaders and restore the header funnel summary](2026-09-22-score-leaders-header-funnel.md)
- [Unify recommendation history with the published download archive](2026-09-22-published-history-single-owner.md)
- [Stack dashboard Top scores vertically](2026-09-22-dashboard-top-scores-vertical-layout.md)
- [Expose immutable market epochs and complete pipeline snapshots](2026-09-22-market-epochs-complete-pipeline-snapshots.md)
- [Reposition dashboard market and score status](2026-09-21-dashboard-status-information-placement.md)
- [Retire Today and V1 from the active recommendation product](2026-09-19-recommendation-identity-cutover.md)
- [Detail the refactor blueprint implementation plan](2026-09-17-refactor-blueprint-implementation-plan.md)
- [Clarify recommendation delivery module ownership](2026-09-17-recommendation-delivery-module-ownership.md)
- [Resolve refactor blueprint architecture conflicts](2026-09-17-refactor-blueprint-conflict-resolution.md)
- [Separate data readiness from business filtering](2026-09-15-data-readiness-filter-separation.md)

- [History, training, and recommendation application boundaries](2026-09-15-history-training-application-boundaries.md)
- [Recommendation modular boundaries and training isolation](2026-09-15-recommendation-modular-boundaries.md)
- [Version profile-owned training artifacts](2026-09-14-profile-training-artifacts.md)
- [Remove public history automation installation commands](2026-09-14-history-automation-entrypoint-cleanup.md)
- [Remove the public Tomorrow-only training entrypoint](2026-09-14-training-entrypoint-cleanup.md)
- [Normalize the history maintenance command to download](2026-09-14-download-command-normalization.md)
- [Profile-owned V2/V3 training commands](2026-09-14-profile-owned-training-commands.md)
- [Recover recommendation-pipeline evidence across freeze and restart](2026-09-14-recommendation-pipeline-recovery.md)
- [Expose the typed recommendation pipeline directly in the desktop dashboard](2026-09-14-recommendation-pipeline-stage-observability.md)
- [Restore industry-aware model inputs and truthful funnel branches](2026-09-14-model-industry-input-recovery.md)
- [Recover the three-strategy recommendation funnel and expose its true first blocker](2026-09-14-three-strategy-funnel-recovery.md)
- [Profile-owned V2/V3 training layout plan](2026-09-14-profile-owned-training-layout-plan.md)
- [Default V2 over the existing shared bundles](2026-09-14-v2-default-existing-bundles.md)
- [V2/V3 shared three-head runtime](2026-09-13-v2-v3-shared-three-head-runtime.md)
- [Share all three trained model heads between V2 and V3](2026-09-13-v2-v3-shared-three-head-training-plan.md)
- [Require independent retraining for the merged Tomorrow V2 plan](2026-09-13-tomorrow-v2-independent-retraining-plan.md)
- [Clarify V2/V3 history input and training-artifact directories](2026-09-13-v2-v3-history-input-artifact-boundary.md)
- [V3 three-head training and runtime chain](2026-09-13-v3-three-head-training-runtime.md)
- [V3 three-head scoring profile contract](2026-09-13-v3-three-head-profile-contract.md)
- [Clarify shared V2/V3 training history and strategy boundaries](2026-09-13-v2-v3-training-history-clarification.md)
- [Tomorrow V1/V2 merged V2 profit-optimization plan](2026-09-13-tomorrow-v1-v2-merged-v2-plan.md)
- [History repack finalize memory-evidence compatibility](2026-09-13-history-repack-finalize-memory-evidence.md)
- [Repository-wide Python naming consistency](2026-09-12-repository-wide-python-naming.md)
- [History SQLite and Tomorrow training pipeline optimization](2026-09-12-history-snapshot-training-identity.md)
- [History SQLite repack foundation](2026-09-12-history-sqlite-repack-foundation.md)
- [Tomorrow V3 fixed training artifact files](2026-09-12-tomorrow-v3-fixed-training-artifacts.md)
- [SQLite repack and Tomorrow training optimization plan](2026-09-12-sqlite-repack-training-plan.md)
- [Tomorrow training 2 GiB OOM isolation](2026-09-12-tomorrow-training-oom-isolation.md)
- [Tomorrow training streaming and resource bounds](2026-09-12-tomorrow-training-streaming-resources.md)
- [Professional business naming](2026-09-12-professional-business-naming.md)
- [Semantic research artifact identities](2026-09-12-semantic-research-identities.md)
- [Tomorrow training progress and verified-read performance](2026-09-11-tomorrow-training-progress.md)
- [BaoStock QFQ gap repair](2026-09-11-baostock-qfq-gap-repair.md)
- [BaoStock converted-history layout normalization](2026-09-11-baostock-history-layout-normalization.md)
- [Trader delivery scoring-chain routing](2026-09-11-trader-delivery-scoring-chain-routing.md)
- [Unified short-horizon score scale](2026-09-11-unified-short-horizon-score-scale.md)
- [Tomorrow relative-score freeze evidence and Web explanation](2026-09-11-tomorrow-relative-score-freeze-evidence.md)
- [History archive cutover and release verification](2026-09-11-history-archive-cutover.md)
- [Compact history download progress and failure diagnostics](2026-09-11-compact-history-progress.md)
- [Cross-platform history automation and due reminders](2026-09-11-history-automation.md)
- [History download progress and timeout diagnostics](2026-09-11-history-download-progress.md)
- [Tomorrow training cadence](2026-09-11-tomorrow-training-cadence.md)
- [2026-09-11 delivery-log archive](2026-09-11-delivery-log-archive.md)
- [Legacy delivery history through 2026-09-10](archive/legacy-through-2026-09-10.md)
