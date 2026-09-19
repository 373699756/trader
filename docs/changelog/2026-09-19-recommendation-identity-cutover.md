# Retire Today and V1 from the active recommendation product

## User request

Continue the next incomplete implementation stage in section 10 of the detailed refactor blueprint.

## Evidence

- The stage 4 baseline still exposed Today through scheduling, scoring, freezing, HTTP/Web projections, tests, configuration, and packaged training artifacts.
- The runtime still accepted V1 and retained its package resources alongside the V2/V3 profile-owned artifacts.
- Recommendation domain ownership was split across the legacy top-level domain packages instead of the recommendation business boundary.

## Added

- Recommendation domain types and pure rules now live under `trader.recommendation.domain`.
- Long HTTP responses use an explicit no-score whitelist and expose no rank, TopK, selection diagnostics, scores, or freeze fields.

## Changed

- The active product strategies are now Tomorrow, D25, and current-only Long. Today is no longer parsed, scheduled, scored, frozen, serialized, rendered, or packaged.
- The only scoring profiles are V2 and V3, each with independent Tomorrow and D25 heads under `data/train/<profile>/<strategy>`.
- Long still has no recommendation history.
- The authoritative scoring and engineering documents now describe the same Tomorrow/D25/Long and V2/V3 boundaries.

## Fixed

- Long projections can no longer inherit scored-strategy ranking, selection, score, or freeze fields at the HTTP boundary.

## Removed

- Removed the legacy top-level market, recommendation, and review domain owners.
- Removed Today runtime, configuration, tests, Web delivery, and V2/V3 training heads.
- Removed the V1 profile implementation, package resource, CLI acceptance, and tests.

## Verification

- Targeted recommendation, research, DeepSeek, scheduler, bootstrap, Web, JavaScript, profile, and persistence tests.
- Contract suite excluding the user's separate test-command and document-optimization batch.
- Ruff, mypy, compileall, collection, diff checks, and complete stage review are recorded in `docs/03_工程实施.md`.

## Residual Risks

- Stages 6-11 still contain planned ownership and package-boundary migrations; this stage does not claim a releasable repository state.
- Historical changelog records retain their original Today/V1 wording and do not form active compatibility behavior.
